from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "pvc", "pv", "node", "metric", "symptom")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
            if key in metrics:
                return float(metrics.get(key))
        except (TypeError, ValueError):
            pass
    return 0.0


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    iowait = _num(payload, "iowait", "node_iowait_ratio")
    latency = _num(payload, "disk_latency_ms", "storage_latency_ms")
    matched = any(token in text for token in (
        "storage", "pvc", "pv", "disk io", "iowait", "fsync", "iops", "filesystem",
        "磁盘", "存储", "读写", "延迟",
    )) or iowait >= 0.2 or latency >= 50
    evidence: List[str] = []
    if iowait:
        evidence.append(f"iowait={iowait:g}")
    if latency:
        evidence.append(f"存储延迟={latency:g}ms")
    if payload.get("pvc"):
        evidence.append(f"PVC={payload.get('pvc')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到存储或磁盘 I/O 性能异常" if matched else "未检测到存储性能异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    usage = _num(payload, "disk_usage_ratio", "pvc_usage_ratio")
    iowait = _num(payload, "iowait", "node_iowait_ratio")
    latency = _num(payload, "disk_latency_ms", "storage_latency_ms")
    if usage >= 0.9 or "no space" in text:
        fault_type = "K8S::storage_capacity_pressure"
        root_cause = "PVC 或节点文件系统容量接近耗尽，应用写入和模型加载受到影响"
    elif iowait >= 0.2 or latency >= 50 or "fsync" in text:
        fault_type = "K8S::storage_io_latency_high"
        root_cause = "存储后端或节点磁盘 I/O 延迟升高，业务读写路径变慢"
    elif "mount" in text:
        fault_type = "K8S::storage_mount_degraded"
        root_cause = "卷挂载或挂载路径异常导致应用访问存储不稳定"
    else:
        fault_type = "K8S::storage_performance_degraded"
        root_cause = "存储性能退化，需要结合 PVC、节点和应用 I/O 证据确认"
    return {
        "status": "ok",
        "confidence": min(0.92, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("pvc") or payload.get("node") or payload.get("pod") or "storage",
        "observed": {"usage": usage, "iowait": iowait, "latency_ms": latency},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    pvc = payload.get("pvc") or "<pvc>"
    pod = payload.get("pod") or "<pod>"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "生成存储恢复建议；扩容、迁移和删除数据必须审批。",
        "evidence": ["默认不执行删除数据或存储变更"],
        "suggestions": [
            {"step": "查看 PVC/PV", "command": f"kubectl -n {ns} get pvc {pvc} -o wide", "risk": "low"},
            {"step": "查看 Pod 挂载事件", "command": f"kubectl -n {ns} describe pod {pod}", "risk": "low"},
            {"step": "审批后扩容 PVC", "command": f"kubectl -n {ns} patch pvc {pvc} -p '{{\"spec\":{{\"resources\":{{\"requests\":{{\"storage\":\"<new-size>\"}}}}}}}}'", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证 PVC、节点磁盘和业务延迟恢复。",
        "evidence": ["存储恢复验证计划"],
        "checks": [
            f"kubectl -n {ns} get pvc -o wide",
            "Prometheus 查询 node disk io/iowait 和 filesystem usage",
            "检查应用读写错误、fsync 慢日志和业务 P95",
        ],
    }
