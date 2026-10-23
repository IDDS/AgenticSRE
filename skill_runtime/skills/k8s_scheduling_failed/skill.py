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
        payload.get("pod"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend([item.get("source"), item.get("signal"), item.get("value"), item.get("message")])
        elif item:
            parts.append(str(item))
    return " ".join(str(item or "") for item in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in (
        "schedulingfailed",
        "pendingscheduling",
        "failedscheduling",
        "unschedulable",
        "insufficient cpu",
        "insufficient memory",
        "insufficient nvidia.com/gpu",
        "insufficient gpu",
        "node(s) had taint",
        "didn't match pod affinity",
    ))
    evidence: List[str] = []
    if "failedscheduling" in text or "unschedulable" in text:
        evidence.append("Kubernetes 事件显示调度失败或 Pod Unschedulable")
    if "insufficient cpu" in text or "insufficient memory" in text or "insufficient nvidia.com/gpu" in text or "insufficient gpu" in text:
        evidence.append("调度失败原因包含节点资源不足")
    if "taint" in text:
        evidence.append("调度失败原因包含节点 taint/toleration 不匹配")
    if "affinity" in text:
        evidence.append("调度失败原因包含亲和性约束")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 Pod 调度失败信号" if matched else "未检测到调度失败信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    pod = payload.get("pod") or "unknown"
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if pod != "unknown":
        evidence.append(f"目标 Pod: {pod}")
    evidence.append(f"目标命名空间: {namespace}")
    if deployment:
        evidence.append(f"关联工作负载: {deployment}")
    commands = [
        f"kubectl describe pod {pod} -n {namespace}",
        "kubectl top node",
        "kubectl get nodes -o wide",
        f"kubectl describe resourcequota -n {namespace}",
    ]
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.84 if len(evidence) >= 2 else 0.66)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.93),
        "summary": f"Pod {pod} 调度失败，需检查事件、节点资源、taint/toleration、亲和性和命名空间配额。",
        "evidence": evidence,
        "fault_type": "SchedulingFailed",
        "root_cause": diagnosis.get("root_cause") or f"Pod {pod} 因资源不足、污点/容忍或亲和性约束无法调度",
        "root_cause_component": pod if pod != "unknown" else deployment,
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    pod = _payload(context).get("pod") or "<pod>"
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证 Pod 是否离开 Pending/Unschedulable 状态，调度失败事件是否消失。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get pod {pod} -n {namespace} -o wide",
            f"kubectl describe pod {pod} -n {namespace}",
            f"kubectl get events -n {namespace} --sort-by=.lastTimestamp | tail -40",
        ],
    }
