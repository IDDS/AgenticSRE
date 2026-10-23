from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    fields = (
        "query", "message", "fault_type", "purpose", "component", "model",
        "error", "raw_fault_type", "scenario",
    )
    return " ".join(str(payload.get(field, "")) for field in fields).lower()


def _query(payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("query") or "").strip()
    if explicit:
        return explicit
    component = str(payload.get("component") or payload.get("service") or payload.get("deployment") or "").strip()
    error = str(payload.get("error") or payload.get("message") or payload.get("fault_type") or "").strip()
    model = str(payload.get("model") or "").strip()
    terms = [item for item in (component, model, error) if item]
    return " ".join(terms) or "AgenticSRE AIOps skill fault diagnosis"


def _domains(payload: Dict[str, Any]) -> List[str]:
    domains = payload.get("domains")
    if isinstance(domains, list):
        return [str(item).strip() for item in domains if str(item).strip()]
    purpose = str(payload.get("purpose") or "").lower()
    text = _text(payload)
    if "vllm" in text:
        return ["docs.vllm.ai", "github.com"]
    if "k8s" in text or "kubernetes" in text:
        return ["kubernetes.io", "github.com"]
    if "paper" in purpose or "论文" in text or "arxiv" in text:
        return ["arxiv.org"]
    return []


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    query = _query(payload)
    domains = _domains(payload)
    recency_days = payload.get("recency_days")
    tools = context.get("tools")
    search_results = []
    if tools and hasattr(tools, "execute"):
        search_results.append(tools.execute(
            "web_search",
            query=query,
            domains=domains,
            recency_days=recency_days,
            limit=int(payload.get("limit") or 5),
        ))
    executed = [item for item in search_results if item.get("executed")]
    successful = [item for item in search_results if item.get("success")]
    evidence = ["外部检索计划已生成"]
    if executed:
        evidence.append("已请求 web_search 工具执行互联网检索")
    if successful:
        evidence.append("web_search 返回候选外部来源")
    return {
        "status": "ok",
        "confidence": 0.84 if successful else 0.7,
        "summary": "已执行互联网检索。" if executed else "生成互联网检索计划；当前运行环境未注入 web_search runner。",
        "evidence": evidence,
        "fault_type": "InternetSearchResearch",
        "root_cause": "需要外部知识补强假设或 Skill 演进依据",
        "root_cause_component": payload.get("component") or payload.get("service") or "external_knowledge",
        "query": query,
        "domains": domains,
        "search_results": search_results,
    }
