from __future__ import annotations

from typing import Any, Dict, List
import re


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    return " ".join(str(item or "") for item in [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("pod"),
        payload.get("deployment"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    # Negated/history-only mentions are not positive termination observations.
    positive_clauses = [part for part in re.split(r"[.;。；\n]", text)
                        if not re.search(r"\b(no|not|without|never|historical|previously)\b|没有|未见|不存在|历史", part)]
    text = " ".join(positive_clauses)
    reason = str(payload.get("last_terminated_reason") or "")
    reasons = payload.get("container_terminated_reasons") or []
    observed = reason.casefold() == "oomkilled" or "OOMKilled" in reasons
    matched = observed or any(token in text for token in ("oomkilled", "oom killed", "exit code 137", "out of memory"))
    evidence: List[str] = []
    if observed:
        evidence.append("容器终止状态 reason=OOMKilled")
    elif "oomkilled" in text or "oom killed" in text:
        evidence.append("故障文本包含 OOMKilled 待验证线索")
    if "exit code 137" in text:
        evidence.append("容器退出码为 137")
    if "memory limit" in text or "out of memory" in text:
        evidence.append("故障信息指向内存限制或内存耗尽")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到容器 OOMKilled 信号" if matched else "未检测到 OOMKilled 信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    detected = context.get("detect") or detect(context)
    if detected.get("status") != "matched":
        return {"status": "skipped", "confidence": 0.0, "summary": "缺少 OOMKilled 正向信号", "evidence": []}
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    pod = payload.get("pod") or "unknown"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    container = payload.get("container") or payload.get("container_name") or ""
    evidence = list(detected.get("evidence") or [])
    if pod != "unknown":
        evidence.append(f"目标 Pod: {pod}")
    if deployment != "unknown":
        evidence.append(f"目标 Deployment: {deployment}")
    if container:
        evidence.append(f"目标容器: {container}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.86 if len(evidence) >= 2 else 0.68)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.94),
        "summary": f"Pod {pod} 容器被 OOMKilled，需核对内存限制、实时资源使用和最近日志。",
        "evidence": evidence,
        "fault_type": "OOMKilled",
        "root_cause": diagnosis.get("root_cause") or f"{pod} 容器内存使用超过限制并被 OOMKilled",
        "root_cause_component": deployment,
        "container": container,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成 OOMKilled 恢复建议；重启可恢复可用性，提升内存 limit 需要人工变更。",
        "evidence": ["包含资源使用检查和审批后的 rollout 恢复动作"],
        "suggestions": [
            {"step": f"查看 Pod {pod} 资源使用", "command": f"kubectl top pod {pod} -n {namespace}", "risk": "low"},
            {"step": f"查看 Pod {pod} 最近事件", "command": f"kubectl describe pod {pod} -n {namespace}", "risk": "low"},
            {"step": f"查看 Pod {pod} previous 日志", "command": f"kubectl logs {pod} -n {namespace} --tail=120 --previous", "risk": "low"},
            {
                "step": f"审批后滚动重启 Deployment {deployment}",
                "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
                "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
                "risk": "medium",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 rollout 完成、Pod 重启后不再 OOMKilled、内存使用恢复。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl rollout status deployment/{deployment} -n {namespace}",
            f"kubectl get pod {pod} -n {namespace} -o wide",
            f"kubectl top pod {pod} -n {namespace}",
        ],
    }
