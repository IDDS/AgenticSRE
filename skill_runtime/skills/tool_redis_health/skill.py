from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "error", "service", "database", "component")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
            if key in metrics:
                return float(metrics.get(key))
        except (TypeError, ValueError):
            pass
    return 0.0


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    blocked = _num(payload, "blocked_clients", "redis_blocked_clients")
    evicted = _num(payload, "evicted_keys", "redis_evicted_keys")
    matched = any(token in text for token in (
        "redis", "cache", "evicted", "blocked_clients", "slowlog", "readonly", "moved", "ask", "缓存",
    )) or blocked > 0 or evicted > 0
    evidence: List[str] = []
    if blocked:
        evidence.append(f"blocked_clients={blocked:g}")
    if evicted:
        evidence.append(f"evicted_keys={evicted:g}")
    if "timeout" in text:
        evidence.append("Redis 请求超时")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 Redis 依赖异常" if matched else "未检测到 Redis 异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    memory = _num(payload, "memory_usage_ratio", "redis_memory_usage_ratio")
    blocked = _num(payload, "blocked_clients", "redis_blocked_clients")
    evicted = _num(payload, "evicted_keys", "redis_evicted_keys")
    if "connection refused" in text or "unavailable" in text:
        fault_type = "Redis::unavailable"
        root_cause = "Redis endpoint 不可用或连接被拒绝"
    elif memory >= 0.9 or evicted > 0:
        fault_type = "Redis::memory_pressure_eviction"
        root_cause = "Redis 内存压力或淘汰增加，缓存命中和业务延迟受到影响"
    elif blocked > 0 or "slowlog" in text:
        fault_type = "Redis::latency_or_blocked_clients"
        root_cause = "Redis 存在阻塞客户端或慢命令，导致请求排队和超时"
    elif "moved" in text or "ask" in text or "readonly" in text:
        fault_type = "Redis::cluster_routing_or_replica_error"
        root_cause = "Redis Cluster 路由或只读副本访问异常"
    else:
        fault_type = "Redis::dependency_degraded"
        root_cause = "Redis 依赖退化，需要结合连接、内存、慢日志和业务错误率确认"
    return {
        "status": "ok",
        "confidence": min(0.9, 0.66 + 0.06 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("component") or payload.get("service") or "redis",
        "observed": {"memory_usage_ratio": memory, "blocked_clients": blocked, "evicted_keys": evicted},
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.72,
        "summary": "验证 Redis 连接、内存、慢日志和业务错误率恢复。",
        "evidence": ["Redis 恢复验证计划"],
        "checks": ["redis-cli PING", "INFO clients memory stats", "业务错误率和 P95 恢复"],
    }
