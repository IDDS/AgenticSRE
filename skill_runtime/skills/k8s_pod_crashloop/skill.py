from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    raw = payload.get("alert", {}).get("raw_data", {}) if isinstance(payload.get("alert"), dict) else {}
    return " ".join(str(x or "") for x in [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("pod"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in ("crashloopbackoff", "back-off restarting", "podcrashloop", "exit code"))
    evidence = []
    if "crashloopbackoff" in text or "back-off restarting" in text:
        evidence.append("Kubernetes 事件显示容器反复重启")
    if "exit code" in text:
        evidence.append("容器退出码异常")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 Pod CrashLoop 信号" if matched else "未检测到 CrashLoop 信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    pod = payload.get("pod") or "unknown"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if pod != "unknown":
        evidence.append(f"目标 Pod: {pod}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.87 if evidence else 0.6)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.95),
        "summary": f"Pod {pod} 反复崩溃，优先检查 previous 日志和 Deployment 发布状态。",
        "evidence": evidence,
        "fault_type": "PodCrashLoop",
        "root_cause": diagnosis.get("root_cause") or f"{pod} 容器反复退出并进入 CrashLoopBackOff",
        "root_cause_component": deployment,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    deployment = payload.get("deployment") or "<deployment>"
    return {
        "status": "ok",
        "confidence": 0.84,
        "summary": "生成 CrashLoop 自愈建议；先采日志，再审批回滚。",
        "evidence": ["包含只读诊断动作和可回滚修复动作"],
        "suggestions": [
            {
                "step": f"采集 {pod} previous 日志",
                "command": f"kubectl logs {pod} -n {namespace} --tail=120 --previous",
                "risk": "low",
            },
            {
                "step": f"回滚 Deployment {deployment}",
                "command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
                "rollback_command": "",
                "risk": "medium",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "执行后验证 Pod restart count 和 rollout 状态。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get pods -n {namespace}",
            f"kubectl rollout status deployment/<deployment> -n {namespace}",
        ],
    }
