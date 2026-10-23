from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "root_cause", "symptom", "policy")
    return " ".join(str(payload.get(key, "")) for key in keys).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    tokens = (
        "networkpolicy",
        "network policy",
        "network_policy_deny",
        "egress denied",
        "ingress denied",
        "network deny",
        "deny",
        "policy deny",
        "networkchaos",
        "network chaos",
        "network partition",
        "connection timeout",
    )
    matched = any(token in text for token in tokens)
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.9 if "network_policy_deny" in text else (0.86 if matched else 0.0),
        "summary": "检测到网络策略或网络故障注入导致的连通性异常" if matched else "未检测到网络策略阻断信号",
        "evidence": ["故障上下文包含网络策略、拒绝访问或网络故障注入信号"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    source = payload.get("source_service") or payload.get("source_workload") or payload.get("client") or "<source-workload>"
    target = payload.get("target_service") or payload.get("target") or payload.get("service") or "<target-service>"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    evidence.append(f"连通性路径: {source} -> {target}")
    commands = [
        f"kubectl get networkpolicy -n {namespace} -o wide",
        f"kubectl describe networkpolicy -n {namespace}",
        f"kubectl get networkchaos -n {namespace} -o wide",
        f"kubectl get events -n {namespace} --sort-by=.lastTimestamp | tail -40",
    ]
    return {
        "status": "ok",
        "confidence": 0.84,
        "summary": f"命名空间 {namespace} 中 {source} 到 {target} 的流量可能被 NetworkPolicy/NetworkChaos 阻断。",
        "evidence": evidence,
        "fault_type": "NetworkPolicyDeny",
        "root_cause": f"{source} 到 {target} 的流量被网络策略或网络故障注入规则阻断",
        "root_cause_component": namespace,
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    service = _payload(context).get("service") or "<service>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证策略审计结果、事件和业务服务连通性。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get networkpolicy -n {namespace} -o wide",
            f"kubectl get networkchaos -n {namespace} -o wide",
            f"kubectl get endpoints {service} -n {namespace} -o wide",
        ],
    }
