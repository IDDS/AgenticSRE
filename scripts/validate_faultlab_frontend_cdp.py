#!/usr/bin/env python3
"""Validate fault-lab UI clicks through a disposable Chrome CDP target."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import websocket


def _json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=15, suppress_origin=True)
        self.sequence = 0

    def call(self, method: str, params=None):
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result") or {}

    def evaluate(self, expression: str, await_promise: bool = False):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
        return (result.get("result") or {}).get("value")


def _wait(cdp: CDP, expression: str, description: str, timeout_s: int = 30):
    deadline = time.time() + timeout_s
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {description}; last={value!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", default="http://127.0.0.1:9223")
    parser.add_argument("--url", default="http://127.0.0.1:18084")
    parser.add_argument("--artifact", default="")
    parser.add_argument("--real-safe-scenario", default="")
    args = parser.parse_args()
    if args.real_safe_scenario not in {"", "precise-application-failpoint-error"}:
        raise RuntimeError("only the reviewed application failpoint is allowed in frontend real-flow validation")

    page = next(item for item in _json(f"{args.cdp}/json/list") if item.get("type") == "page")
    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": args.url})
    _wait(cdp, "['interactive', 'complete'].includes(document.readyState)", "page load")
    _wait(cdp, "typeof switchView === 'function' && typeof openPrecisionFaultLab === 'function'", "fault-lab scripts")

    cdp.evaluate("switchView('faultlab')")
    _wait(cdp, "document.getElementById('view-faultlab')?.classList.contains('active')", "fault-lab view")
    cdp.evaluate("document.querySelector('[data-faultlab-tab=\"precision\"]')?.click()")
    _wait(
        cdp,
        "document.getElementById('faultlab-tab-precision')?.classList.contains('active')",
        "integrated precision fault-lab tab",
    )
    count = _wait(
        cdp,
        "document.querySelectorAll('#faultlab-precision-panel .fault-card').length === 18 && document.querySelectorAll('#faultlab-precision-panel .fault-card').length",
        "18 precision scenario cards",
    )
    action_count = _wait(
        cdp,
        "document.querySelectorAll('#faultlab-precision-panel .fault-action-buttons button').length === 126 && document.querySelectorAll('#faultlab-precision-panel .fault-action-buttons button').length",
        "seven reviewed actions for every precision scenario",
    )
    first_card = "document.querySelector('#faultlab-precision-panel .fault-card')"
    first_label = cdp.evaluate(f"{first_card}?.querySelector('.fault-card-title')?.childNodes[0]?.textContent.trim()")
    action_labels = cdp.evaluate(
        f"[...{first_card}.querySelectorAll('.fault-action-buttons button')].map(button => button.textContent.trim())"
    )
    expected_actions = ["合同", "环境预检", "预演", "真实闭环", "自愈校验", "强制清理", "残留检查"]
    if action_labels != expected_actions:
        raise RuntimeError(f"unexpected precision action labels: {action_labels!r}")

    cdp.evaluate(f"{first_card}.querySelector('.fault-action-buttons button:nth-child(1)').click()")
    _wait(
        cdp,
        "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'planned' && value.guarded_plan?.real_execution_ready === true; } catch (_) { return false; } })()",
        "reviewed precision contract",
    )
    contract = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))

    cdp.evaluate(f"{first_card}.querySelector('.fault-action-buttons button:nth-child(2)').click()")
    _wait(
        cdp,
        "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'ready' && value.preflight?.ready === true; } catch (_) { return false; } })()",
        "precision runtime preflight",
        45,
    )
    preflight = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))

    cdp.evaluate(f"{first_card}.querySelector('.fault-action-buttons button:nth-child(3)').click()")
    _wait(
        cdp,
        "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'dry_run' && value.result?.transaction?.stages?.length === 7; } catch (_) { return false; } })()",
        "precision transaction preview",
    )
    preview = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))

    real_result = None
    heal_result = None
    if args.real_safe_scenario:
        scenario_literal = json.dumps(args.real_safe_scenario)
        cdp.evaluate("document.getElementById('precisionfaultlab-confirm').checked = true; window.confirm = () => true")
        clicked = cdp.evaluate(
            f"(() => {{ const id = {scenario_literal}; const card = [...document.querySelectorAll('#faultlab-precision-panel .fault-card')].find(item => item.querySelector(`[onclick*=\"${{id}}\"]`)); const button = card?.querySelector('.fault-action-buttons button:nth-child(4)'); if (!button) return false; button.click(); return true; }})()"
        )
        if not clicked:
            raise RuntimeError(f"real-flow button not found for {args.real_safe_scenario}")
        _wait(
            cdp,
            "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'succeeded' && value.result?.verification?.cleanup_complete === true; } catch (_) { return false; } })()",
            "safe real precision transaction",
            90,
        )
        real_result = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))
        clicked = cdp.evaluate(
            f"(() => {{ const id = {scenario_literal}; const card = [...document.querySelectorAll('#faultlab-precision-panel .fault-card')].find(item => item.querySelector(`[onclick*=\"${{id}}\"]`)); const button = card?.querySelector('.fault-action-buttons button:nth-child(5)'); if (!button) return false; button.click(); return true; }})()"
        )
        if not clicked:
            raise RuntimeError(f"self-healing verification button not found for {args.real_safe_scenario}")
        _wait(
            cdp,
            "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'already_recovered' && value.recovery?.passed === true && value.transactional_heal?.source_recovery_already_completed === true; } catch (_) { return false; } })()",
            "precision self-healing verification",
            90,
        )
        heal_result = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))

    cdp.evaluate(f"{first_card}.querySelector('.fault-action-buttons button:nth-child(7)').click()")
    _wait(
        cdp,
        "(() => { try { const value = JSON.parse(document.getElementById('precisionfaultlab-output')?.textContent || '{}'); return value.status === 'passed' && value.verification?.passed === true; } catch (_) { return false; } })()",
        "precision residue check",
    )
    verification = json.loads(cdp.evaluate("document.getElementById('precisionfaultlab-output').textContent"))
    title = cdp.evaluate("document.title")
    view_active = cdp.evaluate("document.getElementById('view-faultlab').classList.contains('active')")
    panel_visible = cdp.evaluate("getComputedStyle(document.getElementById('faultlab-precision-panel')).display !== 'none'")
    result = {
        "status": "passed",
        "title": title,
        "faultlab_view_active": view_active,
        "precision_tab_active": cdp.evaluate("document.getElementById('faultlab-tab-precision').classList.contains('active')"),
        "precision_panel_visible": panel_visible,
        "precision_card_count": count,
        "precision_action_count": action_count,
        "action_labels": action_labels,
        "clicked_scenario": first_label,
        "contract_ready": contract["guarded_plan"]["real_execution_ready"],
        "preflight_ready": preflight["preflight"]["ready"],
        "preview_status": preview["status"],
        "preview_stages": preview["result"]["transaction"]["stages"],
        "real_safe_scenario": args.real_safe_scenario or None,
        "real_safe_status": real_result["status"] if real_result else None,
        "real_safe_cleanup_complete": real_result["result"]["verification"]["cleanup_complete"] if real_result else None,
        "self_healing_status": heal_result["status"] if heal_result else None,
        "self_healing_passed": heal_result["recovery"]["passed"] if heal_result else None,
        "residue_check_passed": verification["verification"]["passed"],
    }
    if args.artifact:
        artifact = Path(args.artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
