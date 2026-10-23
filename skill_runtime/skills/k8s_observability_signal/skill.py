from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    return " ".join(str(item or "") for item in [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("pod"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
    ]).lower()


def _fault_type(text: str, payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("fault_type") or "").strip()
    if explicit in {"MetricAnomaly", "LogErrorSpike", "HighLatency"}:
        return explicit
    if "targetdown" in text or "scrape failed" in text or "target disappeared" in text:
        return "TargetDown"
    if "error spike" in text or "log error" in text or "error rate" in text:
        return "LogErrorSpike"
    if "latency" in text or "timeout" in text or "p99" in text or "slow" in text:
        return "HighLatency"
    return "MetricAnomaly"


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched = any(token in text for token in (
        "metric",
        "prometheus",
        "anomaly",
        "指标",
        "targetdown",
        "scrape failed",
        "latency",
        "timeout",
        "p99",
        "slow",
        "error spike",
        "log error",
        "error rate",
    ))
    evidence: List[str] = []
    if any(token in text for token in ("metric", "prometheus", "anomaly", "指标")):
        evidence.append("存在指标异常或 Prometheus 观测信号")
    if any(token in text for token in ("targetdown", "scrape failed")):
        evidence.append("监控目标 TargetDown 或 scrape failed")
    if any(token in text for token in ("latency", "timeout", "p99", "slow")):
        evidence.append("业务延迟、超时或 P99 异常")
    if any(token in text for token in ("error spike", "log error", "error rate")):
        evidence.append("错误日志或错误率突增")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.78 if matched else 0.0,
        "summary": "检测到观测信号异常" if matched else "未检测到观测信号异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    text = _text(payload)
    fault_type = _fault_type(text, payload)
    namespace = payload.get("namespace") or "default"
    service = payload.get("service") or ""
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or ""
    pod = payload.get("pod") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    evidence.append(f"目标命名空间: {namespace}")
    if service:
        evidence.append(f"目标 Service: {service}")
    if deployment:
        evidence.append(f"目标 Deployment: {deployment}")
    if pod:
        evidence.append(f"目标 Pod: {pod}")
    commands = [
        f"kubectl get svc,endpoints -n {namespace} -o wide",
        f"kubectl get pods -n {namespace} -o wide",
        f"kubectl top pod -n {namespace} --sort-by=cpu",
    ]
    if service:
        commands.append(f"kubectl get endpoints {service} -n {namespace} -o wide")
    if pod:
        commands.append(f"kubectl logs {pod} -n {namespace} --tail=500")
    if deployment and fault_type in {"HighLatency", "LogErrorSpike"}:
        commands.append(f"kubectl describe deployment/{deployment} -n {namespace}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.76 if evidence else 0.55)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.9),
        "summary": f"{fault_type} 需要先确认监控目标、端点、资源热点和最近日志，再决定是否恢复业务工作负载。",
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": diagnosis.get("root_cause") or f"{fault_type} observed from monitoring signals",
        "root_cause_component": deployment or service or pod or namespace,
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "验证监控目标、端点、Pod 状态和资源热点是否恢复。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get svc,endpoints -n {namespace} -o wide",
            f"kubectl get pods -n {namespace} -o wide",
            f"kubectl top pod -n {namespace}",
        ],
    }
