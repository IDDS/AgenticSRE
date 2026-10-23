#!/usr/bin/env python3
"""Verify that the frontend recovery button targets the selected fault run."""

from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = os.getenv("AGENTICSRE_UI_URL", "http://127.0.0.1:18080").rstrip("/")
RUN_ID = os.getenv("AGENTICSRE_FAULT_RUN_ID", "fault-1cdd488008")
OUT = Path("outputs/demo/kv-cache/recovery_button_verification.json")


def main() -> int:
    auth_token = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
    if not auth_token:
        raise RuntimeError("AGENTICSRE_AUTH_TOKEN is required for this browser check")
    report = {"run_id": RUN_ID, "checks": {}, "page_errors": [], "console_errors": []}
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
        page.set_default_timeout(90_000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        page.on(
            "console",
            lambda message: report["console_errors"].append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        if "/login" in page.url:
            page.fill("input[name=username], #username", "admin")
            password = os.environ.get("AGENTICSRE_DEMO_PASSWORD", "")
            if not password:
                raise RuntimeError("AGENTICSRE_DEMO_PASSWORD is required for demo login")
            page.fill("input[name=password], #password", password)
            page.click("button[type=submit], button:has-text('登录')")
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

        page.evaluate("window.switchView('llmfaultlab')")
        page.wait_for_function(
            "runId => [...document.querySelectorAll('#llmfaultlab-active-run option')].some(option => option.value === runId)",
            arg=RUN_ID,
            timeout=120_000,
        )
        page.select_option("#llmfaultlab-active-run", RUN_ID)
        page.evaluate("window.applySelectedLLMFaultRun()")
        selected = page.evaluate(
            """() => ({
                run_id: document.querySelector('#llmfaultlab-active-run')?.value,
                scenario: document.querySelector('#llmfaultlab-scenario')?.value,
                cluster: document.querySelector('#llmfaultlab-cluster')?.value,
                namespace: document.querySelector('#llmfaultlab-namespace')?.value,
                deployment: document.querySelector('#llmfaultlab-deployment')?.value,
                button_disabled: document.querySelector('#llmfaultlab-recover-selected')?.disabled
            })"""
        )
        report["checks"]["selection"] = selected
        if selected["run_id"] != RUN_ID or selected["scenario"] != "sw-003" or selected["button_disabled"]:
            raise RuntimeError(f"recovery selection was not applied: {selected}")

        page.uncheck("#llmfaultlab-dryrun")
        page.check("#llmfaultlab-confirm")
        page.evaluate("_lastLLMFaultResult = null")
        page.get_by_role("button", name="恢复所选故障", exact=True).click()
        page.wait_for_function(
            "_lastLLMFaultResult?.status === 'executed' && _lastLLMFaultResult?.action === 'cleanup'",
            timeout=180_000,
        )
        result = page.evaluate("_lastLLMFaultResult")
        source_ids = [item.get("source_fault_run_id") for item in result.get("results") or []]
        report["checks"]["cleanup"] = {
            "status": result.get("status"),
            "action": result.get("action"),
            "source_fault_run_ids": source_ids,
            "fault_run_id": result.get("fault_run_id"),
            "observation": result.get("observation"),
        }
        if RUN_ID not in source_ids:
            raise RuntimeError(f"cleanup did not target selected fault run: {source_ids}")

        page.evaluate("window.loadActiveLLMFaultRuns()")
        page.wait_for_timeout(1500)
        remaining = page.locator(f'#llmfaultlab-active-run option[value="{RUN_ID}"]').count()
        report["checks"]["removed_from_active_list"] = remaining == 0
        if remaining:
            raise RuntimeError("recovered fault run is still listed as active")

        context.close()
        browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
