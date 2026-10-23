from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    parts = [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("hpa"),
        payload.get("deployment"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in (
        "hpascalingfailed",
        "failedgetscale",
        "failedgetresourcemetric",
        "failedcomputemetricsreplicas",
        "unable to get target metrics",
        "unable to get metrics",
        "unable to get cpu utilization",
        "metrics api",
        "metrics-server",
    ))
    evidence: List[str] = []
    if "failedgetscale" in text:
        evidence.append("HPA 事件显示无法获取 scaleTargetRef")
    if "failedgetresourcemetric" in text or "unable to get target metrics" in text or "unable to get metrics" in text or "unable to get cpu utilization" in text:
        evidence.append("HPA 事件显示无法获取目标指标")
    if "failedcomputemetricsreplicas" in text:
        evidence.append("HPA 事件显示无法基于指标计算副本数")
    if "metrics-server" in text or "metrics api" in text:
        evidence.append("故障信息指向 metrics-server 或 metrics API")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 HPA 扩缩容或指标采集异常信号" if matched else "未检测到 HPA 扩缩容异常信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    hpa = payload.get("hpa") or payload.get("hpa_name") or payload.get("deployment") or "unknown"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or hpa
    metric_name = payload.get("metric") or payload.get("metric_name") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if hpa != "unknown":
        evidence.append(f"目标 HPA: {hpa}")
    if deployment and deployment != "unknown":
        evidence.append(f"目标 Deployment: {deployment}")
    if metric_name:
        evidence.append(f"异常指标: {metric_name}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.86 if len(evidence) >= 2 else 0.68)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.94),
        "summary": f"HPA {hpa} 扩缩容失败，优先检查 scaleTargetRef、metrics API、metrics-server 和目标 Deployment。",
        "evidence": evidence,
        "fault_type": "HPAScalingFailed",
        "root_cause": diagnosis.get("root_cause") or "HPA cannot read scale target or metrics",
        "root_cause_component": deployment,
        "hpa": hpa,
        "metric": metric_name,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    hpa = payload.get("hpa") or payload.get("hpa_name") or payload.get("deployment") or "<hpa>"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    replicas = int(payload.get("fallback_replicas") or payload.get("desired_replicas") or 1)
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成 HPA 扩缩容失败恢复建议；临时固定副本数会改变业务容量，真实执行必须审批。",
        "evidence": ["包含 HPA/metrics 只读检查和审批后的临时 scale 动作"],
        "suggestions": [
            {
                "step": "查看 HPA 状态",
                "command": f"kubectl get hpa {hpa} -n {namespace} -o wide",
                "risk": "low",
            },
            {
                "step": f"检查 HPA {hpa} 事件和 scaleTargetRef",
                "command": f"kubectl describe hpa {hpa} -n {namespace}",
                "risk": "low",
            },
            {
                "step": "查看 metrics APIService",
                "command": "kubectl get apiservice v1beta1.metrics.k8s.io",
                "risk": "low",
            },
            {
                "step": f"查看 Deployment {deployment} 当前副本",
                "command": f"kubectl get deployment/{deployment} -n {namespace} -o wide",
                "risk": "low",
            },
            {
                "step": f"审批后临时固定 Deployment {deployment} 副本数",
                "command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas={replicas}",
                "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
                "risk": "medium",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    hpa = payload.get("hpa") or payload.get("hpa_name") or payload.get("deployment") or "<hpa>"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 HPA 指标是否恢复、metrics API 是否 Available、Deployment 副本是否达到预期。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get hpa {hpa} -n {namespace} -o wide",
            "kubectl get apiservice v1beta1.metrics.k8s.io",
            f"kubectl rollout status deployment/{deployment} -n {namespace}",
        ],
    }
