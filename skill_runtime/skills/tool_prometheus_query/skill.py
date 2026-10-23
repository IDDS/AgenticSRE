from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "query", "promql", "metric", "purpose")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _queries(payload: Dict[str, Any]) -> List[str]:
    raw = payload.get("queries")
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    query = payload.get("query") or payload.get("promql")
    if query:
        return [str(query)]
    metric = payload.get("metric")
    return [str(metric)] if metric else []


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    queries = _queries(payload)
    prom_context = any(token in text for token in (
        "promql", "prometheus", "metric", "query_range", "threshold", "time window",
        "current value", "peak value", "指标查询", "阈值", "时间窗口",
    ))
    looks_like_promql = any(any(token in query for token in ("{", "}", "[", "]", "rate(", "sum(", "max_over_time", "histogram_quantile")) for query in queries)
    matched = prom_context or (bool(queries) and looks_like_promql)
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.8 if matched else 0.0,
        "summary": "命中 Prometheus 查询工具 Skill" if matched else "未检测到 Prometheus 查询需求",
        "evidence": [f"查询数量 {len(queries)}"] if queries else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    prom = payload.get("prometheus_url") or "http://127.0.0.1:9090"
    queries = _queries(payload)
    tools = context.get("tools")
    results = []
    if tools and hasattr(tools, "execute"):
        for query in queries:
            results.append(tools.execute(
                "prometheus",
                url=prom,
                query=query,
                start=payload.get("start"),
                end=payload.get("end"),
                step=payload.get("step"),
                mode=payload.get("mode") or "auto",
            ))
    succeeded = [item for item in results if item.get("executed") and item.get("success")]
    all_succeeded = bool(queries) and len(succeeded) == len(queries)
    attempted = any(item.get("executed") for item in results)
    if all_succeeded:
        summary, status, confidence = "已执行 Prometheus 查询。", "ok", 0.86
    elif attempted:
        summary, status, confidence = "Prometheus 查询未全部成功，无法形成完整指标证据。", "error", 0.0
    else:
        summary, status, confidence = "Prometheus 查询尚未执行，仅生成查询计划。", "skipped", 0.0
    evidence = [f"Prometheus 查询成功: {queries[index]}"
                for index, item in enumerate(results)
                if item.get("executed") and item.get("success")]
    return {
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "error": "prometheus_query_failed" if status == "error" else "",
        "fault_type": "Tool::prometheus_query_validation",
        "root_cause": "需要 Prometheus 指标作为假设验证和置信度证据",
        "root_cause_component": "prometheus",
        "queries": queries,
        "query_results": results,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.68,
        "summary": "验证 Prometheus 查询结果可解释、可复现、可用于告警判断。",
        "evidence": ["PromQL 证据验证清单"],
        "checks": [
            "查询窗口覆盖故障前、故障中和恢复后",
            "记录当前值、峰值、阈值和单位",
            "查询失败时返回明确错误，不伪造指标值",
        ],
    }
