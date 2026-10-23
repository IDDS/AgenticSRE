from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    return " ".join(str(item or "") for item in [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("node"),
        payload.get("pod"),
        payload.get("deployment"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]).lower()


def _number(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for name in names:
        try:
            if name in payload:
                return float(payload.get(name))
            if name in metrics:
                return float(metrics.get(name))
            if str(metrics.get("metric") or metrics.get("name") or metrics.get("signal") or "") == name:
                return float(metrics.get("value"))
        except (TypeError, ValueError):
            continue
    return default


def _threshold(payload: Dict[str, Any], name: str, default: float) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    try:
        if str(metrics.get("metric") or metrics.get("name") or metrics.get("signal") or "") == name and metrics.get("threshold") is not None:
            return float(metrics.get("threshold"))
        if thresholds.get(name) is not None:
            return float(thresholds.get(name))
    except (TypeError, ValueError):
        return default
    return default


def _resource_snapshot(payload: Dict[str, Any]) -> Dict[str, Tuple[float, float, str, str]]:
    return {
        "HighCPUUsage": (
            _number(payload, "node_cpu_usage", "container_cpu_usage"),
            _threshold(payload, "node_cpu_usage", 85.0),
            "%",
            "CPU",
        ),
        "HighMemoryUsage": (
            _number(payload, "node_memory_usage", "container_memory_usage"),
            _threshold(payload, "node_memory_usage", 85.0),
            "%",
            "内存",
        ),
        "DiskPressure": (
            _number(payload, "node_rootfs_usage"),
            _threshold(payload, "node_rootfs_usage", 85.0),
            "%",
            "根分区",
        ),
        "DiskReadPressure": (
            _number(payload, "node_disk_read"),
            _threshold(payload, "node_disk_read", 200.0),
            "MB/s",
            "磁盘读取",
        ),
        "DiskWritePressure": (
            _number(payload, "node_disk_write"),
            _threshold(payload, "node_disk_write", 200.0),
            "MB/s",
            "磁盘写入",
        ),
    }


def _breached_resources(payload: Dict[str, Any]) -> List[Tuple[str, float, float, str, str]]:
    breached = []
    for fault_type, (value, threshold, unit, label) in _resource_snapshot(payload).items():
        if value > 0 and value >= threshold:
            breached.append((fault_type, value, threshold, unit, label))
    return sorted(breached, key=lambda item: item[1] / max(item[2], 0.0001), reverse=True)


def _infer_fault_type(text: str, payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("fault_type") or "").strip()
    if explicit in {"HighCpu", "HighMemory", "HighCPUUsage", "HighMemoryUsage", "DiskPressure"}:
        return explicit
    breached = _breached_resources(payload)
    if breached:
        return breached[0][0]
    if "diskpressure" in text or "disk pressure" in text or "disk full" in text:
        return "DiskPressure"
    if "highmemory" in text or "memory usage" in text or "node_memory_usage" in text or "container_memory" in text:
        return "HighMemoryUsage"
    if "highcpu" in text or "cpu usage" in text or "node_cpu_usage" in text or "container_cpu" in text:
        return "HighCPUUsage"
    if "memory" in text or "内存" in text:
        return "HighMemory"
    return "ResourcePressureObserved"


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    node_cpu = _number(payload, "node_cpu_usage", "container_cpu_usage")
    node_memory = _number(payload, "node_memory_usage", "container_memory_usage")
    node_rootfs = _number(payload, "node_rootfs_usage")
    node_disk_read = _number(payload, "node_disk_read")
    node_disk_write = _number(payload, "node_disk_write")
    matched = any(token in text for token in (
        "highcpu",
        "highmemory",
        "cpu usage",
        "memory usage",
        "node_cpu_usage",
        "node_memory_usage",
        "container_cpu",
        "container_memory",
        "diskpressure",
        "disk pressure",
        "filesystem pressure",
        "disk full",
        "资源水位",
        "内存",
    ))
    matched = matched or bool(_breached_resources(payload))
    evidence: List[str] = []
    if any(token in text for token in ("cpu", "node_cpu_usage", "container_cpu")):
        evidence.append("故障信号包含 CPU 资源压力")
    if any(token in text for token in ("memory", "内存", "node_memory_usage", "container_memory")):
        evidence.append("故障信号包含内存资源压力")
    if any(token in text for token in ("diskpressure", "disk pressure", "filesystem pressure", "disk full")):
        evidence.append("节点存在磁盘压力或磁盘空间不足信号")
    for _fault_type, value, threshold, unit, label in _breached_resources(payload):
        evidence.append(f"{label} {value:.1f}{unit} 超过阈值 {threshold:.1f}{unit}")
    if node_cpu > 0 and all(item[0] != "HighCPUUsage" for item in _breached_resources(payload)):
        evidence.append(f"节点 CPU 使用率 {node_cpu:.1f}% 未超阈值")
    if node_memory > 0 and all(item[0] != "HighMemoryUsage" for item in _breached_resources(payload)):
        evidence.append(f"节点内存使用率 {node_memory:.1f}% 未超阈值")
    if node_rootfs > 0 and all(item[0] != "DiskPressure" for item in _breached_resources(payload)):
        evidence.append(f"节点根分区使用率 {node_rootfs:.1f}% 未超阈值")
    if node_disk_read > 0 or node_disk_write > 0:
        evidence.append(f"节点磁盘吞吐 read={node_disk_read:.1f}MB/s write={node_disk_write:.1f}MB/s")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.82 if matched else 0.0,
        "summary": "检测到资源压力信号" if matched else "未检测到资源压力信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    text = _text(payload)
    fault_type = _infer_fault_type(text, payload)
    namespace = payload.get("namespace") or "default"
    node = payload.get("node") or ""
    pod = payload.get("pod") or ""
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    node_cpu = _number(payload, "node_cpu_usage", "container_cpu_usage")
    node_memory = _number(payload, "node_memory_usage", "container_memory_usage")
    node_rootfs = _number(payload, "node_rootfs_usage")
    evidence.append(f"目标命名空间: {namespace}")
    if node:
        evidence.append(f"目标节点: {node}")
    if pod:
        evidence.append(f"目标 Pod: {pod}")
    if deployment:
        evidence.append(f"目标 Deployment: {deployment}")
    for _fault_type, value, threshold, unit, label in _breached_resources(payload):
        evidence.append(f"观测{label}={value:.1f}{unit}，阈值={threshold:.1f}{unit}")
    root_cause_by_type = {
        "HighCPUUsage": "HighCPUUsage 资源压力：节点或容器 CPU 使用率超过阈值，需要定位热点 Pod/进程",
        "HighCpu": "HighCpu 资源压力：节点或容器 CPU 使用率超过阈值，需要定位热点 Pod/进程",
        "HighMemoryUsage": "HighMemoryUsage 资源压力：节点或容器内存使用率超过阈值，需要定位内存热点和 OOM 风险",
        "HighMemory": "HighMemory 资源压力：节点或容器内存使用率超过阈值，需要定位内存热点和 OOM 风险",
        "DiskPressure": "DiskPressure 资源压力：节点根分区使用率超过阈值，可能影响镜像拉取、日志写入和容器运行时",
        "DiskReadPressure": "DiskReadPressure 资源压力：节点磁盘读取吞吐超过阈值，可能影响模型加载或数据读取路径",
        "DiskWritePressure": "DiskWritePressure 资源压力：节点磁盘写入吞吐超过阈值，可能影响日志、缓存或容器运行时",
    }
    summary = root_cause_by_type.get(fault_type, "检测到基础资源压力信号，需要结合节点和工作负载指标确认")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.82 if len(evidence) >= 2 else 0.64)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.92),
        "summary": f"{summary}，恢复动作需先经过审批。",
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": diagnosis.get("root_cause") or summary,
        "root_cause_component": deployment or pod or node or namespace,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    node = payload.get("node") or "<node>"
    pod = payload.get("pod") or ""
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or ""
    fault_type = _infer_fault_type(_text(payload), payload)
    suggestions = [
        {"step": "按 CPU 排序定位热点 Pod", "command": f"kubectl top pod -n {namespace} --sort-by=cpu", "risk": "low"},
        {"step": "按内存排序定位热点 Pod", "command": f"kubectl top pod -n {namespace} --sort-by=memory", "risk": "low"},
        {"step": "查看节点资源水位", "command": "kubectl top node", "risk": "low"},
    ]
    if pod:
        suggestions.append({"step": f"查看 Pod {pod} 事件", "command": f"kubectl describe pod {pod} -n {namespace}", "risk": "low"})
    if deployment:
        suggestions.append({
            "step": f"审批后滚动重启 Deployment {deployment}",
            "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
            "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "risk": "medium",
        })
    if fault_type == "DiskPressure":
        suggestions.extend([
            {"step": f"查看节点 {node} 磁盘压力和驱逐事件", "command": f"kubectl describe node {node}", "risk": "low"},
            {
                "step": f"审批后隔离磁盘压力节点 {node}",
                "command": f"kubectl cordon {node}",
                "rollback_command": f"kubectl uncordon {node}",
                "risk": "high",
            },
            {
                "step": f"审批后驱逐节点 {node} 工作负载",
                "command": f"kubectl drain {node} --ignore-daemonsets --delete-emptydir-data --force --timeout=120s",
                "rollback_command": f"kubectl uncordon {node}",
                "risk": "high",
            },
        ])
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成资源压力恢复建议；不会自动修改资源 limit，节点隔离/驱逐和重启均需审批。",
        "evidence": ["包含资源水位只读检查和审批后的受控恢复动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    node = payload.get("node") or ""
    checks = [
        f"kubectl top pod -n {namespace}",
        "kubectl top node",
        f"kubectl get pods -n {namespace} -o wide",
    ]
    if node:
        checks.append(f"kubectl describe node {node}")
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证资源热点是否下降，节点压力 condition 是否恢复，Pod 是否稳定运行。",
        "evidence": ["验证计划已生成"],
        "checks": checks,
    }
