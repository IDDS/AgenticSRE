from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _target(payload: Dict[str, Any]) -> Dict[str, str]:
    return {
        "namespace": str(payload.get("namespace") or "default"),
        "pod": str(payload.get("pod") or ""),
        "deployment": str(payload.get("deployment") or ""),
        "service": str(payload.get("service") or payload.get("object") or ""),
    }


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = " ".join(str(payload.get(k, "")) for k in ("message", "fault_type", "object", "pod", "deployment", "service")).lower()
    matched = any(token in text for token in ("pod", "deployment", "service", "endpoint", "readiness", "k8s", "kubernetes"))
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.78 if matched else 0.0,
        "summary": "命中 K8s 只读巡检工具 skill" if matched else "未命中 K8s 工具 skill",
        "evidence": ["payload 包含 K8s 对象或 readiness 信号"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    target = _target(_payload(context))
    ns = target["namespace"]
    commands = [
        f"kubectl get pods -n {ns} -o wide",
        f"kubectl get events -n {ns} --sort-by=.lastTimestamp",
        f"kubectl get endpoints -n {ns}",
    ]
    if target["deployment"]:
        commands.append(f"kubectl describe deployment/{target['deployment']} -n {ns}")
    if target["pod"]:
        commands.append(f"kubectl describe pod/{target['pod']} -n {ns}")
    tool_results = []
    tools = context.get("tools")
    if tools and hasattr(tools, "execute"):
        for command in commands[:3]:
            kubectl_command = command
            if kubectl_command.startswith("kubectl "):
                kubectl_command = kubectl_command[len("kubectl "):]
            tool_results.append(tools.execute("kubectl", command=kubectl_command, namespace=ns))
    evidence = ["只读 kubectl 命令，不改变集群状态"]
    executed = [item for item in tool_results if item.get("executed")]
    successful = [item for item in tool_results if item.get("success")]
    if executed:
        evidence.append(f"已实际执行 {len(executed)} 个只读 kubectl 调查命令")
    if successful:
        evidence.append(f"{len(successful)} 个 kubectl 调查命令返回成功")
    return {
        "status": "ok",
        "confidence": 0.86 if successful else 0.8,
        "summary": "已执行 K8s readiness 只读诊断命令。" if executed else "生成 K8s readiness 只读诊断命令。",
        "evidence": evidence,
        "commands": commands,
        "tool_results": tool_results,
        "target": target,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    ns = _target(_payload(context))["namespace"]
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "验证 readiness 只读巡检结果是否已恢复。",
        "evidence": ["只读验证计划"],
        "checks": [
            f"kubectl get pods -n {ns}",
            f"kubectl get endpoints -n {ns}",
        ],
    }
