from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    return " ".join(str(payload.get(key, "")) for key in (
        "message", "fault_type", "page", "url", "action", "scenario", "raw_fault_type",
    )).lower()


def _url(payload: Dict[str, Any]) -> str:
    return str(payload.get("url") or payload.get("frontend_url") or payload.get("base_url") or "http://127.0.0.1:8080")


def _pages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    base = _url(payload).rstrip("/")
    requested = payload.get("pages")
    if isinstance(requested, list) and requested:
        return [
            {"name": str(item.get("name") or item.get("path") or idx), "url": str(item.get("url") or f"{base}/{str(item.get('path') or '').lstrip('/')}")}
            for idx, item in enumerate(requested, start=1)
            if isinstance(item, dict)
        ]
    return [
        {"name": "工作台", "url": base},
        {"name": "通算告警", "url": f"{base}/#alerts"},
        {"name": "智算事件", "url": f"{base}/#ai-events"},
        {"name": "GPU Lab", "url": f"{base}/#gpu-lab"},
        {"name": "Skill Catalog", "url": f"{base}/#skills"},
    ]


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched = any(token in text for token in (
        "browser", "playwright", "frontend", "ui", "modal", "screenshot",
        "前端", "浏览器", "页面", "按钮", "点击", "弹窗", "截图", "没变化",
    ))
    if payload.get("url") or payload.get("frontend_url"):
        matched = True
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.78 if matched else 0.0,
        "summary": "命中前端浏览器验证工具 Skill" if matched else "未命中前端浏览器验证需求",
        "evidence": ["输入包含前端/浏览器/页面验证信号"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    pages = _pages(payload)
    tools = context.get("tools")
    browser_results = []
    if tools and hasattr(tools, "execute"):
        for page in pages[:3]:
            browser_results.append(tools.execute(
                "browser",
                action="open_and_snapshot",
                url=page["url"],
                expect_text=payload.get("expect_text") or "",
            ))
            if len(browser_results) >= 3:
                break
    executed = [item for item in browser_results if item.get("executed")]
    successful = [item for item in browser_results if item.get("success")]
    evidence = ["浏览器验证计划已生成"]
    if executed:
        evidence.append(f"已执行 {len(executed)} 个浏览器只读验证动作")
    if successful:
        evidence.append(f"{len(successful)} 个页面验证返回成功")
    return {
        "status": "ok",
        "confidence": 0.86 if successful else 0.72,
        "summary": "已执行前端浏览器验证。" if executed else "生成前端浏览器验证计划；当前运行环境未注入 browser/playwright runner。",
        "evidence": evidence,
        "fault_type": "FrontendValidation",
        "root_cause": "前端可用性需要通过浏览器路径验证，而不是只检查后端 API",
        "root_cause_component": payload.get("page") or "web_app",
        "pages": pages,
        "browser_results": browser_results,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    pages = (context.get("diagnose") or {}).get("output", {}).get("pages", [])
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "复核页面、按钮、弹窗、工作流和截图证据。",
        "evidence": ["前端复测清单"],
        "checks": [
            f"{page.get('name')} 可打开且核心内容可见: {page.get('url')}"
            for page in pages
        ] + [
            "Agent+Skill 分析按钮点击后出现运行状态和最终结果",
            "RCA 工作流或报告详情可展开、可关闭",
            "智算依赖图、指标曲线和事件列表无明显空白或重叠",
        ],
    }
