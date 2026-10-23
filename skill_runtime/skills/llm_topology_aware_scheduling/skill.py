from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "deployment", "node", "scenario_name")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
            if key in metrics:
                return float(metrics.get(key))
        except (TypeError, ValueError):
            pass
    return default


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    cross_nodes = _num(payload, "cross_node_parallel_groups", "cross_node_links")
    rdma_latency = _num(payload, "rdma_latency_ms", "nccl_latency_ms")
    matched = any(token in text for token in (
        "topology aware", "numa", "nvlink locality", "rdma locality", "cross-node",
        "topology spread", "node affinity", "拓扑感知", "跨节点",
    )) or cross_nodes > 0 or rdma_latency >= 10
    evidence: List[str] = []
    if cross_nodes:
        evidence.append(f"跨节点并行组={cross_nodes:g}")
    if rdma_latency:
        evidence.append(f"通信延迟={rdma_latency:g}ms")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到智算拓扑感知调度异常信号" if matched else "未检测到拓扑感知调度异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    cross_nodes = _num(payload, "cross_node_parallel_groups", "cross_node_links")
    rdma_latency = _num(payload, "rdma_latency_ms", "nccl_latency_ms")
    if cross_nodes > 0 or "cross-node" in text or "跨节点" in text:
        fault_type = "LLM::cross_node_parallel_placement"
        root_cause = "多卡并行组跨节点放置，RDMA/NCCL 通信路径变长并拖慢推理"
    elif "numa" in text or "nvlink locality" in text or "rdma locality" in text:
        fault_type = "LLM::gpu_nic_numa_locality_mismatch"
        root_cause = "GPU/NIC/NUMA locality 不匹配，Host-Device 或跨 GPU 通信延迟升高"
    elif "affinity" in text or "topology spread" in text:
        fault_type = "LLM::topology_scheduling_policy_misconfigured"
        root_cause = "节点亲和、反亲和或拓扑 spread 约束配置不合理，导致 Pod placement 不符合智算拓扑"
    else:
        fault_type = "LLM::topology_aware_scheduling_degraded"
        root_cause = "智算拓扑感知调度退化，需要核对 Pod placement、节点标签和通信指标"
    return {
        "status": "ok",
        "confidence": min(0.92, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("node") or "scheduler-topology",
        "observed": {"cross_node_parallel_groups": cross_nodes, "communication_latency_ms": rdma_latency},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "生成拓扑感知调度恢复建议；调整调度策略需审批。",
        "evidence": ["先核对 placement、节点标签和通信路径"],
        "suggestions": [
            {"step": "查看 Pod placement", "command": f"kubectl -n {ns} get pods -l app={deploy} -o wide", "risk": "low"},
            {"step": "查看调度约束", "command": f"kubectl -n {ns} get deployment/{deploy} -o yaml | grep -E 'nodeSelector|affinity|topologySpreadConstraints' -A20", "risk": "low"},
            {"step": "审批后调整 topology/affinity", "command": "按 GPU/NIC/NVLink/RDMA locality 调整 node labels、affinity 或 topology spread", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证 Pod placement、通信延迟和推理指标恢复。",
        "evidence": ["拓扑感知调度验证计划"],
        "checks": [
            "Pod 落在预期节点/GPU/NIC locality",
            "NCCL/RDMA latency 和跨节点流量回落",
            "TTFT/E2E P95 恢复到基线",
        ],
    }
