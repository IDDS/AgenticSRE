from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    return " ".join(str(item or "") for item in [
        payload.get("fault_type"),
        payload.get("raw_fault_type"),
        payload.get("alert_name"),
        payload.get("message"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("object"),
        payload.get("node"),
        payload.get("observed_evidence_summary"),
        labels.get("alertname"),
        labels.get("job"),
        labels.get("instance"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        diagnosis.get("root_cause_component"),
    ]).lower()


def _component(text: str, payload: Dict[str, Any]) -> str:
    fault_type = str(payload.get("fault_type") or "").lower()
    if "etcd" in text or "etcd" in fault_type:
        return "etcd"
    if "kube-proxy" in text or "kubeproxydown" in text:
        return "kube-proxy"
    if "kubelet" in text or "kubeletdown" in text or "node agent" in text:
        return "kubelet"
    return "kube-apiserver"


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    if "kube-proxy" in text and any(token in text for token in (
        "metricsbindaddress",
        "metrics bind",
        "/metrics",
        "scrape failed",
        "prometheus scrape",
        "servicemonitor",
        "podmonitor",
    )):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "kube-proxy metrics 抓取异常应交由 k8s.prometheus_scrape_target 处理",
            "evidence": ["检测到 kube-proxy metrics/scrape 观测链路信号，跳过控制面健康 Skill"],
        }
    component_matched = any(token in text for token in (
        "apiserver",
        "kube-apiserver",
        "kubeapiserver",
        "control_plane_unhealthy",
        "etcd",
        "etcdserver",
        "etcdinsufficientmembers",
        "insufficient members",
        "quorum",
        "no leader",
        "leader changed",
        "kube-proxy",
        "kubeproxydown",
        "proxy rules",
        "kubelet",
        "kubeletdown",
        "node agent",
    ))
    matched = component_matched
    component = _component(text, payload)
    evidence = [f"故障信号指向 {component}"] if matched else []
    if "targetdown" in text:
        evidence.append("监控信号包含 TargetDown")
    if component == "etcd" and any(token in text for token in ("etcdinsufficientmembers", "insufficient members", "quorum")):
        evidence.append("Prometheus 告警显示 etcd 成员不足或多数派不可用")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": f"检测到 {component} 健康异常信号" if matched else "未检测到控制面健康异常信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    text = _text(payload)
    component = _component(text, payload)
    node = payload.get("node") or diagnosis.get("root_cause_component") or ""
    fault_map = {
        "kube-apiserver": "ApiServerUnhealthy",
        "etcd": "EtcdUnhealthy",
        "kube-proxy": "KubeProxyUnhealthy",
        "kubelet": "KubeletUnhealthy",
    }
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if node:
        evidence.append(f"目标节点/组件: {node}")
    if component == "etcd" and any(token in text for token in ("etcdinsufficientmembers", "insufficient members", "quorum")):
        evidence.append("etcd quorum 信号优先级高于普通 Service endpoints 信号")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.84 if len(evidence) >= 1 else 0.62)
    root_cause_map = {
        "kube-apiserver": "kube-apiserver 健康异常，可能导致 Kubernetes API 请求失败或控制面不可用",
        "etcd": "etcd 成员不足或多数派不可用，可能导致控制面存储不可用",
        "kube-proxy": "kube-proxy 健康异常，可能导致 Service 转发规则不可用",
        "kubelet": "kubelet 健康异常，可能导致节点 Pod 状态和探针结果失真",
    }
    return {
        "status": "ok",
        "confidence": min(confidence, 0.93),
        "summary": f"{component} 健康异常属于高风险控制面故障，应先只读确认，再审批执行恢复动作。",
        "evidence": evidence,
        "fault_type": fault_map[component],
        "root_cause": diagnosis.get("root_cause") or root_cause_map[component],
        "root_cause_component": node or component,
        "component": component,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    component = _component(_text(payload), payload)
    node = payload.get("node") or "<node>"
    if component == "etcd":
        suggestions = [
            {"step": "查看 etcd 静态 Pod", "command": "kubectl get pods -n kube-system -l component=etcd -o wide", "risk": "low"},
            {"step": "查看 etcd Pod 事件", "command": "kubectl describe pod -n kube-system -l component=etcd", "risk": "low"},
            {"step": "审批后重建异常 etcd Pod", "command": "kubectl delete pod -n kube-system -l component=etcd", "risk": "high"},
        ]
    elif component == "kube-proxy":
        suggestions = [
            {"step": "查看 kube-proxy Pod 分布", "command": "kubectl get pods -n kube-system -l k8s-app=kube-proxy -o wide", "risk": "low"},
            {"step": "审批后重启 kube-proxy DaemonSet", "command": "kubectl rollout restart ds/kube-proxy -n kube-system", "risk": "medium"},
            {"step": "等待 kube-proxy 发布完成", "command": "kubectl rollout status ds/kube-proxy -n kube-system", "risk": "low"},
        ]
    elif component == "kubelet":
        suggestions = [
            {"step": f"查看节点 {node} Conditions", "command": f"kubectl describe node {node}", "risk": "low"},
            {"step": f"审批后隔离 kubelet 异常节点 {node}", "command": f"kubectl cordon {node}", "rollback_command": f"kubectl uncordon {node}", "risk": "high"},
            {"step": f"审批后驱逐节点 {node} 工作负载", "command": f"kubectl drain {node} --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", "rollback_command": f"kubectl uncordon {node}", "risk": "high"},
        ]
    else:
        suggestions = [
            {"step": "查看 API Server 静态 Pod", "command": "kubectl get pods -n kube-system -l component=kube-apiserver -o wide", "risk": "low"},
            {"step": "查看 API Server 事件详情", "command": "kubectl describe pod -n kube-system -l component=kube-apiserver", "risk": "low"},
            {"step": "审批后删除异常 API Server Pod 触发 kubelet 重建", "command": "kubectl delete pod -n kube-system -l component=kube-apiserver", "risk": "high"},
        ]
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "生成控制面/节点组件恢复建议；高风险动作必须审批并人工确认影响范围。",
        "evidence": ["包含控制面组件只读检查和审批后的恢复动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    component = _component(_text(_payload(context)), _payload(context))
    checks = ["kubectl get nodes -o wide", "kubectl get pods -n kube-system -o wide"]
    if component == "kube-proxy":
        checks.append("kubectl rollout status ds/kube-proxy -n kube-system")
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证控制面组件 Pod、节点 condition 和相关 rollout 状态是否恢复。",
        "evidence": ["验证计划已生成"],
        "checks": checks,
    }
