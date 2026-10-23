from __future__ import annotations

import re
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
        payload.get("pvc"),
        payload.get("volume"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    return " ".join(str(item or "") for item in parts)


def _extract(patterns: List[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    lower = text.lower()
    matched = any(token in lower for token in (
        "volumemountfailed",
        "failedmount",
        "mountvolume",
        "failedattachvolume",
        "unable to attach or mount volumes",
        "timed out waiting for the condition",
        "pvc",
        "persistentvolumeclaim",
    ))
    evidence: List[str] = []
    if "failedmount" in lower or "mountvolume" in lower:
        evidence.append("Kubernetes 事件显示 Volume 挂载失败")
    if "failedattachvolume" in lower:
        evidence.append("Kubernetes 事件显示 Volume attach 失败")
    if "pvc" in lower or "persistentvolumeclaim" in lower:
        evidence.append("故障信息包含 PVC/PersistentVolumeClaim")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 VolumeMountFailed 存储挂载异常信号" if matched else "未检测到存储挂载异常信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    text = _text(payload)
    pod = payload.get("pod") or "unknown"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    pvc = payload.get("pvc") or _extract([r"pvc[ /\"]+([a-z0-9._-]+)", r"persistentvolumeclaim[ /\"]+([a-z0-9._-]+)"], text)
    volume = payload.get("volume") or _extract([r"volume[ /\"]+([a-z0-9._-]+)"], text)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if pod != "unknown":
        evidence.append(f"目标 Pod: {pod}")
    if deployment != "unknown":
        evidence.append(f"关联工作负载: {deployment}")
    if pvc:
        evidence.append(f"疑似 PVC: {pvc}")
    if volume:
        evidence.append(f"疑似 Volume: {volume}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.86 if len(evidence) >= 2 else 0.68)
    target = pvc or volume or pod
    return {
        "status": "ok",
        "confidence": min(confidence, 0.94),
        "summary": f"{pod} 存在 Volume/PVC 挂载异常，优先检查 Pod 事件、PVC/PV 绑定和存储后端状态。",
        "evidence": evidence,
        "fault_type": "VolumeMountFailed",
        "root_cause": diagnosis.get("root_cause") or f"volume mount or attach failed for {target}",
        "root_cause_component": pod if pod != "unknown" else deployment,
        "pvc": pvc,
        "volume": volume,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    pvc = payload.get("pvc") or ""
    suggestions = [
        {
            "step": f"查看 Pod {pod} 挂载失败事件",
            "command": f"kubectl describe pod {pod} -n {namespace}",
            "risk": "low",
        },
        {
            "step": "查看命名空间 PVC 状态",
            "command": f"kubectl get pvc -n {namespace} -o wide",
            "risk": "low",
        },
        {
            "step": "查看 PV/PVC 绑定详情",
            "command": f"kubectl describe pvc -n {namespace} {pvc}" if pvc else f"kubectl describe pvc -n {namespace}",
            "risk": "low",
        },
        {
            "step": f"审批后删除挂载异常 Pod {pod} 触发 kubelet 重新挂载",
            "command": f"kubectl delete pod {pod} -n {namespace} --grace-period=30",
            "rollback_command": "",
            "risk": "medium",
        },
    ]
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成存储挂载异常恢复建议；不会修改 PV/PVC，只在审批后重建异常 Pod 触发重新挂载。",
        "evidence": ["包含 Pod/PVC 只读检查和受控 Pod 重建动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    pvc = payload.get("pvc") or ""
    checks = [
        f"kubectl get pod {pod} -n {namespace} -o wide",
        f"kubectl describe pod {pod} -n {namespace}",
        f"kubectl get pvc -n {namespace} -o wide",
    ]
    if pvc:
        checks.append(f"kubectl describe pvc {pvc} -n {namespace}")
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 Pod 是否重新创建成功、Volume 挂载事件是否消失、PVC 是否 Bound。",
        "evidence": ["验证计划已生成"],
        "checks": checks,
    }
