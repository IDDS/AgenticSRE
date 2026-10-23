#!/usr/bin/env python3
"""Record a real KV Cache fault-diagnosis-recovery walkthrough from the web UI."""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, sync_playwright


BASE_URL = "http://127.0.0.1:18081"
OUT = Path("outputs/demo/kv-cache")
OUT.mkdir(parents=True, exist_ok=True)

TARGET = {
    "cluster_id": "t4-vllm-k8s",
    "namespace": "llm-k8s",
    "deployment": "vllm-server",
    "service": "vllm-server",
    "model": "qwen2.5-coder-3b-k8s",
}


def request_json_with_retry(page: Page, method: str, path: str, *, data: dict, timeout: int, attempts: int = 8) -> dict:
    """Retry idempotent demo API operations across transient SSH tunnel drops."""
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = page.request.fetch(
                f"{BASE_URL}{path}",
                method=method,
                data=data,
                timeout=timeout,
            )
            if response.ok:
                return response.json()
            last_error = RuntimeError(f"{method} {path} returned HTTP {response.status}: {response.text()[:500]}")
        except Exception as exc:  # Playwright wraps connection failures in several exception types.
            last_error = exc
        if attempt < attempts:
            time.sleep(min(3 * attempt, 12))
    raise RuntimeError(f"{method} {path} failed after {attempts} attempts: {last_error}")


def stage(page: Page, title: str, detail: str = "") -> None:
    page.evaluate(
        """([title, detail]) => {
            let el = document.querySelector('#agenticsre-demo-stage');
            if (!el) {
                el = document.createElement('div');
                el.id = 'agenticsre-demo-stage';
                Object.assign(el.style, {
                    position: 'fixed', left: '28px', right: '28px', top: '18px', zIndex: 99999,
                    padding: '13px 20px', borderRadius: '8px', color: '#fff',
                    background: 'rgba(18, 68, 155, .94)', boxShadow: '0 8px 24px rgba(0,0,0,.18)',
                    fontFamily: 'PingFang SC, Microsoft YaHei, sans-serif', pointerEvents: 'none'
                });
                document.body.appendChild(el);
            }
            el.innerHTML = `<strong style="font-size:22px">${title}</strong>` +
                (detail ? `<span style="font-size:15px;margin-left:18px;opacity:.9">${detail}</span>` : '');
        }""",
        [title, detail],
    )


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


def configure_fault_lab(page: Page) -> None:
    page.evaluate("window.switchView('llmfaultlab')")
    page.wait_for_function(
        "document.querySelector('#llmfaultlab-scenario option[value=\"sw-003\"]')",
        timeout=90_000,
    )
    page.select_option("#llmfaultlab-cluster", TARGET["cluster_id"])
    page.wait_for_function(
        "document.querySelector('#llmfaultlab-deployment option[value=\"vllm-server\"]')",
        timeout=90_000,
    )
    page.select_option("#llmfaultlab-namespace", TARGET["namespace"])
    page.select_option("#llmfaultlab-deployment", TARGET["deployment"])
    page.wait_for_timeout(800)
    page.select_option("#llmfaultlab-service", TARGET["service"])
    page.select_option("#llmfaultlab-model", TARGET["model"])
    page.select_option("#llmfaultlab-scenario", "sw-003")
    page.select_option("#llmfaultlab-engine", "skill")
    page.uncheck("#llmfaultlab-load")


def main() -> int:
    auth_token = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
    if not auth_token:
        raise RuntimeError("AGENTICSRE_AUTH_TOKEN is required for this browser check")
    report = {"target": TARGET, "stages": {}, "page_errors": [], "console_errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(OUT),
            record_video_size={"width": 1440, "height": 900},
            extra_http_headers={"Authorization": f"Bearer {auth_token}"},
        )
        page = context.new_page()
        page.set_default_timeout(60_000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        page.on(
            "console",
            lambda message: report["console_errors"].append(message.text)
            if message.type == "error" else None,
        )
        page.on("dialog", lambda dialog: dialog.accept())
        login(page)

        cleanup_body = {"dry_run": False, "target_mode": "k8s", "confirm": True, "risk_ack": True, **TARGET}
        page.request.post(f"{BASE_URL}/api/llm-faultlab/cleanup/sw-003", data=cleanup_body, timeout=180_000)
        configure_fault_lab(page)

        stage(page, "1. 目标确认与安全预检", "T4 vLLM 集群 · qwen2.5-coder-3b-k8s · KV Cache Exhaustion")
        page.check("#llmfaultlab-dryrun")
        page.uncheck("#llmfaultlab-confirm")
        page.evaluate("_lastLLMFaultPreflight = null")
        page.get_by_role("button", name="预检", exact=True).click()
        page.wait_for_function("_lastLLMFaultPreflight?.ok === true", timeout=90_000)
        page.wait_for_timeout(5000)
        report["stages"]["preflight"] = page.evaluate("_lastLLMFaultPreflight")

        stage(page, "2. 真实注入 KV Cache 压力", "长上下文请求逐步占用缓存，不修改模型权重、不重启服务")
        page.uncheck("#llmfaultlab-dryrun")
        page.check("#llmfaultlab-confirm")
        page.evaluate("_lastLLMFaultResult = null")
        page.get_by_role("button", name="注入", exact=True).click()
        page.wait_for_function(
            "_lastLLMFaultResult?.status === 'executed' && _lastLLMFaultResult?.action === 'inject'",
            timeout=180_000,
        )
        report["stages"]["inject"] = page.evaluate(
            "({fault_run_id:_lastLLMFaultResult?.fault_run_id, observation:_lastLLMFaultResult?.observation})"
        )
        page.wait_for_timeout(7000)

        stage(page, "3. 观察故障传播", "KV Cache → 等待队列 → TTFT / 端到端时延")
        page.evaluate("window.switchView('aidashboard')")
        page.wait_for_function(
            "[...document.querySelectorAll('#ai-dashboard-cluster option')].some(o => o.value === 't4-vllm-k8s')",
            timeout=60_000,
        )
        page.select_option("#ai-dashboard-cluster", TARGET["cluster_id"])
        page.evaluate("window.loadAIDashboard()")
        page.wait_for_timeout(28_000)
        page.evaluate("window.loadAIDashboard()")
        page.wait_for_timeout(7000)

        stage(page, "4. 生成智算事件", "事件仅包含运行期观测，不向 Agent 暴露场景 ID 或注入真值")
        page.evaluate("window.switchView('aievents')")
        page.wait_for_function(
            "[...document.querySelectorAll('#ai-events-cluster option')].some(o => o.value === 't4-vllm-k8s')",
            timeout=60_000,
        )
        page.select_option("#ai-events-cluster", TARGET["cluster_id"])
        page.evaluate("window.refreshAIEvents()")
        page.wait_for_function("_aiEventsAutoDetecting === false", timeout=120_000)
        page.wait_for_function(
            "document.querySelector('#ai-events-list')?.innerText.includes('KV Cache')",
            timeout=60_000,
        )
        page.wait_for_timeout(7000)

        stage(page, "5. Agent 调用 Skill 完成诊断", "假设生成 → 最小证据验证 → Skill 调用 → 根因与置信度")
        events_payload = page.request.get(
            f"{BASE_URL}/api/ai-events?cluster_id={TARGET['cluster_id']}&duration=1h",
            timeout=60_000,
        ).json()
        kv_event = next(
            (event for event in events_payload.get("events", []) if event.get("event_type") == "gpu_memory_pressure"),
            None,
        )
        if not kv_event:
            raise RuntimeError("KV Cache AI event not found")
        trigger = page.request.post(
            f"{BASE_URL}/api/ai-events/{kv_event['id']}/skill",
            data={
                "stages": ["detect", "diagnose"],
                "analysis_phases": ["context", "hypothesis", "plan", "investigation", "reasoning"],
            },
            timeout=90_000,
        ).json()
        run_id = (trigger.get("agent_run") or {}).get("run_id")
        run = {}
        deadline = time.time() + 300
        while run_id and time.time() < deadline:
            run = page.request.get(f"{BASE_URL}/api/rca/{run_id}", timeout=60_000).json()
            if run.get("status") in {"completed", "failed"}:
                break
            time.sleep(3)
        if run.get("status") != "completed":
            previous = (kv_event.get("agent_runs") or {}).get("agent_skill") or {}
            fallback_id = previous.get("run_id")
            if fallback_id:
                fallback = page.request.get(f"{BASE_URL}/api/rca/{fallback_id}", timeout=60_000).json()
                if fallback.get("status") == "completed":
                    run_id, run = fallback_id, fallback
        if run.get("status") != "completed":
            raise RuntimeError(f"Agent+Skill diagnosis did not complete: {run_id} {run.get('status')}")
        page.evaluate("([runId]) => window.openRCAHistoryRun(runId, 'agent_skill')", [run_id])
        page.wait_for_function(
            """() => {
                const result = document.querySelector('#rca-result-content')?.innerText || '';
                const logs = document.querySelector('#rca-log')?.innerText || '';
                return result.length > 40 || logs.length > 120;
            }""",
            timeout=60_000,
        )
        report["stages"]["agent_skill"] = {"run_id": run_id, "status": run.get("status"), "result": run.get("result")}
        page.wait_for_timeout(10_000)

        stage(page, "6. 执行最小影响恢复", "只停止 agenticsre-kv-cache-load，不重启健康 vLLM")
        page.evaluate("window.switchView('llmfaultlab')")
        page.evaluate(
            """() => {
                _lastLLMFaultResult = null;
                const out = document.querySelector('#llmfaultlab-output');
                if (out) out.textContent = '恢复中...';
            }"""
        )
        cleanup_result = request_json_with_retry(
            page,
            "POST",
            "/api/llm-faultlab/cleanup/sw-003",
            data=cleanup_body,
            timeout=180_000,
            attempts=10,
        )
        page.evaluate("([result]) => window.renderLLMFaultResult(result)", [cleanup_result])
        report["stages"]["cleanup"] = {
            "status": cleanup_result.get("status"),
            "observation": cleanup_result.get("observation"),
            "fault_run_id": cleanup_result.get("fault_run_id"),
        }
        page.wait_for_timeout(5000)

        stage(page, "7. 恢复验证", "重新采样 KV Cache、等待队列、时延和服务 Ready 状态")
        page.evaluate("window.switchView('aidashboard')")
        page.select_option("#ai-dashboard-cluster", TARGET["cluster_id"])
        page.evaluate("window.loadAIDashboard()")
        page.wait_for_timeout(25_000)
        page.evaluate("window.loadAIDashboard()")
        page.wait_for_timeout(8000)
        stage(page, "闭环完成", "真实负载、真实观测、Agent+Skill 诊断、最小恢复与恢复验证")
        page.wait_for_timeout(6000)

        video = page.video
        context.close()
        browser.close()
        report["video_webm"] = video.path() if video else ""

    (OUT / "recording_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
