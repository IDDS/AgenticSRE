from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    return " ".join(str(payload.get(k, "")) for k in ("fault_type", "message", "node", "root_cause")).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in (
        "nodenotready", "node not ready", "node notready", "notready",
        "kubelet not ready", "node is not ready",
    ))
    evidence = ["Node condition indicates NotReady"] if matched else []
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.88 if matched else 0.0,
        "summary": "检测到 NodeNotReady 信号" if matched else "未检测到 NodeNotReady 信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    node = _payload(context).get("node") or "<node>"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    evidence.append(f"目标节点: {node}")
    return {
        "status": "ok",
        "confidence": 0.89,
        "summary": f"节点 {node} NotReady，需检查 kubelet、网络、磁盘/内存压力和节点事件。",
        "evidence": evidence,
        "fault_type": "NodeNotReady",
        "root_cause": "node condition NotReady",
        "root_cause_component": node,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    node = _payload(context).get("node") or "<node>"
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成节点隔离建议；节点级写操作风险高，必须审批和风险确认。",
        "evidence": ["cordon 带 uncordon 回滚"],
        "suggestions": [
            {"step": f"查看节点 {node} 状态", "command": f"kubectl describe node {node}", "risk": "low"},
            {"step": "查看 NotReady 节点", "command": "kubectl get nodes -o wide", "risk": "low"},
            {
                "step": f"隔离节点 {node}",
                "command": f"kubectl cordon {node}",
                "rollback_command": f"kubectl uncordon {node}",
                "risk": "high",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    node = _payload(context).get("node") or "<node>"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "验证节点 condition 和业务 Pod 迁移状态。",
        "evidence": ["验证计划已生成"],
        "checks": [f"kubectl get node {node} -o wide", f"kubectl describe node {node}"],
    }
