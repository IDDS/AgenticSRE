#!/usr/bin/env python3
"""Verify KV Cache injection from the AI fault topology and always clean up."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


BASE_URL = os.getenv("AGENTICSRE_UI_URL", "http://127.0.0.1:18081").rstrip("/")
OUT = Path("outputs/demo/kv-cache/topology_frontend_verification.json")
TARGET = {
    "cluster_id": "t4-vllm-k8s",
    "namespace": "llm-k8s",
    "deployment": "vllm-server",
    "service": "vllm-server",
    "model": "qwen2.5-coder-3b-k8s",
}


def login(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    if "/login" not in page.url:
        return
    page.fill("input[name=username], #username", "admin")
    password = os.environ.get("AGENTICSRE_DEMO_PASSWORD", "")
    if not password:
        raise RuntimeError("AGENTICSRE_DEMO_PASSWORD is required for demo login")
    page.fill("input[name=password], #password", password)
    page.click("button[type=submit], button:has-text('登录')")
    page.wait_for_load_state("domcontentloaded", timeout=30_000)


def dashboard_summary(page: Page) -> dict:
    response = page.request.get(
        f"{BASE_URL}/api/ai-clusters/{TARGET['cluster_id']}/dashboard?duration=15m",
        timeout=60_000,
    )
    if not response.ok:
        return {"http_status": response.status}
    payload = response.json()
    summary = payload.get("summary") or {}
    return {
        "kv_cache": summary.get("kv_cache"),
        "waiting": summary.get("waiting_requests", summary.get("waiting")),
        "running": summary.get("running_requests", summary.get("running")),
        "ttft_p95": summary.get("ttft_p95"),
        "endpoint_ok": summary.get("endpoint_ok"),
        "mode": payload.get("mode"),
    }


def cleanup_body() -> dict:
    return {
        "dry_run": False,
        "target_mode": "k8s",
        "confirm": True,
        "risk_ack": True,
        **TARGET,
    }


def main() -> int:
    auth_token = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
    if not auth_token:
        raise RuntimeError("AGENTICSRE_AUTH_TOKEN is required for this browser check")
    report = {
        "target": TARGET,
        "checks": {},
        "page_errors": [],
        "console_errors": [],
        "cleanup": {},
    }
    page = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Authorization": f"Bearer {auth_token}"},
        )
        page = context.new_page()
        page.set_default_timeout(60_000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        page.on(
            "console",
            lambda message: report["console_errors"].append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("dialog", lambda dialog: dialog.accept())
        failure = None
        try:
            login(page)
            page.request.post(
                f"{BASE_URL}/api/llm-faultlab/cleanup/sw-003",
                data=cleanup_body(),
                timeout=180_000,
            )

            page.evaluate("window.switchView('llmfaultlab')")
            page.wait_for_function(
                "document.querySelector('#llmfaultlab-scenario option[value=\"sw-003\"]')",
                timeout=90_000,
            )
            page.evaluate(
                """async (clusterId) => {
                    const select = document.querySelector('#llmfaultlab-cluster');
                    if (select) select.value = clusterId;
                    await window.loadLLMFaultTopology();
                }""",
                TARGET["cluster_id"],
            )
            topology_state = page.evaluate(
                """() => ({
                    status: document.querySelector('#llmfaultlab-topology-status')?.innerText || '',
                    text: (document.querySelector('#llmfaultlab-topology')?.innerText || '').slice(0, 1200),
                    html_length: document.querySelector('#llmfaultlab-topology')?.innerHTML.length || 0,
                    all_nodes: document.querySelectorAll('#llmfaultlab-topology g.ai-topology-node').length,
                    models: document.querySelectorAll('#llmfaultlab-topology g.ai-topology-node.model').length,
                    services: document.querySelectorAll('#llmfaultlab-topology g.ai-topology-node.service').length,
                    pods: document.querySelectorAll('#llmfaultlab-topology g.ai-topology-node.pod').length,
                    data_counts: {
                        models: (_lastLLMFaultTopologyData?.inventory?.models || _lastLLMFaultTopologyData?.models || []).length,
                        services: (_lastLLMFaultTopologyData?.topology?.services || []).length,
                        pods: (_lastLLMFaultTopologyData?.topology?.pods || []).length
                    }
                })"""
            )
            report["checks"]["topology_render"] = {
                "passed": topology_state["models"] + topology_state["services"] + topology_state["pods"] > 0,
                "state": topology_state,
            }
            if not report["checks"]["topology_render"]["passed"]:
                raise RuntimeError(f"topology rendered no selectable workload nodes: {topology_state}")

            selected_node = None
            selected_kind = ""
            for kind, expected_text in (
                ("model", TARGET["model"]),
                ("service", TARGET["service"]),
                ("pod", TARGET["deployment"]),
            ):
                candidates = page.locator(f"#llmfaultlab-topology g.ai-topology-node.{kind}")
                matching = candidates.filter(has_text=expected_text)
                if matching.count():
                    selected_node = matching.first
                    selected_kind = kind
                    break
                if candidates.count() and selected_node is None:
                    selected_node = candidates.first
                    selected_kind = kind
            if selected_node is None:
                raise RuntimeError("no model, service, or pod topology target is available")
            model_text = selected_node.inner_text()
            selected_node.click()
            page.wait_for_selector("#llm-fault-target-picker", timeout=30_000)
            report["checks"]["topology_click"] = {
                "passed": True,
                "kind": selected_kind,
                "node_text": model_text,
            }

            options = page.locator("#llm-fault-target-scenario option").evaluate_all(
                "els => els.map(e => ({value:e.value, text:e.textContent}))"
            )
            if not any(item["value"] == "sw-003" for item in options):
                raise RuntimeError("sw-003 is not available for the selected topology target")
            page.select_option("#llm-fault-target-scenario", "sw-003")
            report["checks"]["scenario_select"] = {
                "passed": True,
                "option_count": len(options),
                "selected": "sw-003",
            }

            page.get_by_role("button", name="使用该目标", exact=True).click()
            page.wait_for_function(
                "document.querySelector('#llmfaultlab-scenario')?.value === 'sw-003'",
                timeout=30_000,
            )
            filled = page.evaluate(
                """() => ({
                    cluster: document.querySelector('#llmfaultlab-cluster')?.value,
                    namespace: document.querySelector('#llmfaultlab-namespace')?.value,
                    deployment: document.querySelector('#llmfaultlab-deployment')?.value,
                    service: document.querySelector('#llmfaultlab-service')?.value,
                    scenario: document.querySelector('#llmfaultlab-scenario')?.value
                })"""
            )
            if filled["namespace"] != TARGET["namespace"] or filled["deployment"] != TARGET["deployment"]:
                raise RuntimeError(f"topology target was not correctly populated: {filled}")
            report["checks"]["target_population"] = {"passed": True, "values": filled}

            page.select_option("#llmfaultlab-model", TARGET["model"])
            page.select_option("#llmfaultlab-engine", "skill")
            page.uncheck("#llmfaultlab-load")
            page.check("#llmfaultlab-dryrun")
            page.uncheck("#llmfaultlab-confirm")
            page.evaluate("_lastLLMFaultPreflight = null")
            page.get_by_role("button", name="预检", exact=True).click()
            page.wait_for_function("_lastLLMFaultPreflight !== null", timeout=120_000)
            preflight = page.evaluate("_lastLLMFaultPreflight")
            if not preflight.get("ok"):
                raise RuntimeError(f"preflight failed: {preflight}")
            report["checks"]["preflight"] = {"passed": True, "result": preflight}

            page.uncheck("#llmfaultlab-dryrun")
            page.check("#llmfaultlab-confirm")
            page.evaluate("_lastLLMFaultResult = null")
            page.get_by_role("button", name="注入", exact=True).click()
            page.wait_for_function(
                "_lastLLMFaultResult?.status === 'executed' && _lastLLMFaultResult?.action === 'inject'",
                timeout=180_000,
            )
            injected = page.evaluate("_lastLLMFaultResult")
            report["checks"]["real_injection"] = {
                "passed": True,
                "fault_run_id": injected.get("fault_run_id"),
                "observation": injected.get("observation"),
            }

            signal_samples = []
            for _ in range(5):
                time.sleep(12)
                sample = dashboard_summary(page)
                signal_samples.append(sample)
                kv = float(sample.get("kv_cache") or 0)
                waiting = float(sample.get("waiting") or 0)
                if kv >= 60 or waiting > 0:
                    break
            report["checks"]["observable_signal"] = {
                "passed": any(
                    float(sample.get("kv_cache") or 0) >= 60
                    or float(sample.get("waiting") or 0) > 0
                    for sample in signal_samples
                ),
                "samples": signal_samples,
            }
            if not report["checks"]["observable_signal"]["passed"]:
                raise RuntimeError(f"KV Cache or waiting signal was not observed: {signal_samples}")
        except Exception as exc:
            failure = exc
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if page is not None:
                try:
                    cleanup = page.request.post(
                        f"{BASE_URL}/api/llm-faultlab/cleanup/sw-003",
                        data=cleanup_body(),
                        timeout=180_000,
                    )
                    report["cleanup"] = {
                        "http_status": cleanup.status,
                        "result": cleanup.json() if cleanup.ok else cleanup.text(),
                    }
                except Exception as exc:
                    report["cleanup"] = {"error": str(exc)}
            context.close()
            browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failure is not None:
        raise failure
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
