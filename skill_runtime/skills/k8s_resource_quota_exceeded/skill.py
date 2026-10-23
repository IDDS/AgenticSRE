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
        payload.get("quota"),
        payload.get("resource"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in (
        "resourcequotaexceeded",
        "resourcequota",
        "exceeded quota",
        "forbidden: quota",
        "quota exceeded",
        "limited:",
    ))
    evidence: List[str] = []
    if "exceeded quota" in text or "quota exceeded" in text or "forbidden: quota" in text:
        evidence.append("Kubernetes admission 事件显示命名空间配额不足")
    if "resourcequota" in text:
        evidence.append("故障信息包含 ResourceQuota")
    if "limited:" in text:
        evidence.append("事件包含被配额限制的资源明细")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 ResourceQuota 配额不足信号" if matched else "未检测到 ResourceQuota 配额不足信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    namespace = payload.get("namespace") or "default"
    quota = payload.get("quota") or payload.get("quota_name") or ""
    resource = payload.get("resource") or payload.get("resource_name") or ""
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    evidence.append(f"目标命名空间: {namespace}")
    if quota:
        evidence.append(f"疑似 ResourceQuota: {quota}")
    if resource:
        evidence.append(f"受限资源: {resource}")
    if deployment:
        evidence.append(f"受影响工作负载: {deployment}")
    commands = [
        f"kubectl get resourcequota -n {namespace}",
        f"kubectl describe resourcequota -n {namespace}",
        f"kubectl top pod -n {namespace}",
    ]
    if quota:
        commands.insert(1, f"kubectl describe resourcequota/{quota} -n {namespace}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.84 if len(evidence) >= 2 else 0.66)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.93),
        "summary": f"命名空间 {namespace} 资源配额不足，需核对 ResourceQuota hard/used 和当前资源使用量。",
        "evidence": evidence,
        "fault_type": "ResourceQuotaExceeded",
        "root_cause": diagnosis.get("root_cause") or f"namespace {namespace} exceeded ResourceQuota",
        "root_cause_component": namespace,
        "quota": quota,
        "resource": resource,
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证 ResourceQuota used/hard 是否恢复到允许范围，受影响 Pod 是否可重新创建。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get resourcequota -n {namespace}",
            f"kubectl describe resourcequota -n {namespace}",
            f"kubectl get events -n {namespace} --sort-by=.lastTimestamp | tail -40",
        ],
    }
