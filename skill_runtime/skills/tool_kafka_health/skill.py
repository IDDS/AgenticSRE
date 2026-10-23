from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "error", "topic", "consumer_group", "component")
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
    lag = _num(payload, "consumer_lag", "kafka_consumer_lag")
    offline = _num(payload, "offline_partitions", "kafka_offline_partitions")
    matched = any(token in text for token in (
        "kafka", "consumer lag", "under replicated", "offline partition", "broker", "rebalance", "消息队列",
    )) or lag > 0 or offline > 0
    evidence: List[str] = []
    if lag:
        evidence.append(f"consumer_lag={lag:g}")
    if offline:
        evidence.append(f"offline_partitions={offline:g}")
    if "timeout" in text:
        evidence.append("Kafka 请求超时")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 Kafka 依赖异常" if matched else "未检测到 Kafka 异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    lag = _num(payload, "consumer_lag", "kafka_consumer_lag")
    offline = _num(payload, "offline_partitions", "kafka_offline_partitions")
    under = _num(payload, "under_replicated_partitions", "kafka_under_replicated_partitions")
    if offline > 0 or "offline partition" in text:
        fault_type = "Kafka::offline_partitions"
        root_cause = "Kafka 存在 offline partitions，消息生产或消费链路不可用"
    elif under > 0 or "under replicated" in text:
        fault_type = "Kafka::under_replicated_partitions"
        root_cause = "Kafka ISR/副本不足，broker 或副本同步链路异常"
    elif lag > 0 or "consumer lag" in text:
        fault_type = "Kafka::consumer_lag_high"
        root_cause = "Kafka consumer lag 积压，消费能力低于生产速率或消费者异常"
    elif "rebalance" in text:
        fault_type = "Kafka::rebalance_storm"
        root_cause = "Consumer group 频繁 rebalance，导致消费抖动和延迟"
    else:
        fault_type = "Kafka::dependency_degraded"
        root_cause = "Kafka 依赖退化，需要结合 broker、topic、consumer group 和客户端指标确认"
    return {
        "status": "ok",
        "confidence": min(0.9, 0.66 + 0.06 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("topic") or payload.get("consumer_group") or "kafka",
        "observed": {"consumer_lag": lag, "offline_partitions": offline, "under_replicated_partitions": under},
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.72,
        "summary": "验证 Kafka lag、ISR、offline partitions 和业务延迟恢复。",
        "evidence": ["Kafka 恢复验证计划"],
        "checks": ["consumer lag 持续下降", "under replicated/offline partitions 为 0", "业务错误率和 P95 恢复"],
    }
