from __future__ import annotations

from typing import Any, Dict, Iterable, List

from skill_runtime.fault_taxonomy import is_k8s_orchestration_text


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    fields = (
        "message",
        "fault_type",
        "scenario",
        "host_id",
        "endpoint_status",
        "metrics_status",
        "ssh_status",
        "process_status",
    )
    return " ".join(str(payload.get(field, "")) for field in fields).lower()


def _int(payload: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(payload.get(key) or default)
    except (TypeError, ValueError):
        return default


def _metric(payload: Dict[str, Any], *keys: str) -> int:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for key in keys:
        if key in payload:
            return _int(payload, key)
        if key in metrics:
            try:
                return int(metrics.get(key) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _lines(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _endpoint(payload: Dict[str, Any]) -> str:
    return str(payload.get("endpoint") or payload.get("vllm_endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:8000/health}")


def _metrics_endpoint(payload: Dict[str, Any]) -> str:
    return str(payload.get("metrics_endpoint") or payload.get("vllm_metrics_endpoint") or "${VLLM_METRICS:-http://127.0.0.1:8000/metrics}")


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    if payload.get("deployment") or payload.get("pod") or is_k8s_orchestration_text(text):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "K8s 工作负载或编排事件由 Kubernetes Skill 处理，跳过裸机主机巡检 Skill",
            "evidence": [],
        }
    matched = any(
        token in text
        for token in (
            "llm",
            "vllm",
            "gpu",
            "nvidia",
            "host",
            "baremetal",
            "bare-metal",
            "readiness",
            "metrics",
        )
    )
    if payload.get("host_id") or payload.get("gpu_count") is not None:
        matched = True
    evidence = []
    if payload.get("host_id"):
        evidence.append(f"目标主机 {payload.get('host_id')}")
    if payload.get("gpu_count") is not None:
        evidence.append(f"GPU 数量观测值 {payload.get('gpu_count')}")
    if payload.get("metrics_status"):
        evidence.append(f"vLLM 指标状态 {payload.get('metrics_status')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.78 if matched else 0.0,
        "summary": "命中裸机 LLM/GPU 主机只读巡检工具 skill" if matched else "未命中裸机 LLM/GPU 主机巡检信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    host_id = str(payload.get("host_id") or payload.get("host") or "target-host")
    gpu_count = _int(payload, "gpu_count")
    process_lines = _lines(payload.get("processes") or payload.get("process_list"))
    process_count = _int(payload, "process_count", len(process_lines))
    metrics_count = _int(payload, "metrics_count")
    running_requests = _metric(payload, "running_requests", "num_requests_running", "vllm:num_requests_running")
    waiting_requests = _metric(payload, "waiting_requests", "num_requests_waiting", "vllm:num_requests_waiting")
    endpoint_status = str(payload.get("endpoint_status") or "unknown")
    metrics_status = str(payload.get("metrics_status") or "unknown")
    ssh_status = str(payload.get("ssh_status") or "unknown")

    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if ssh_status != "unknown":
        evidence.append(f"SSH 状态 {ssh_status}")
    if gpu_count:
        evidence.append(f"GPU 可见数量 {gpu_count}")
    if process_count:
        evidence.append(f"vLLM 相关进程 {process_count} 个")
    if metrics_count:
        evidence.append(f"vLLM 指标样本 {metrics_count} 个")
    if endpoint_status != "unknown":
        evidence.append(f"健康接口状态 {endpoint_status}")

    if ssh_status in {"failed", "timeout", "unreachable"}:
        status_summary = "裸机主机 SSH 不可达，无法继续读取 GPU/vLLM 运行状态。"
        confidence = 0.82
        health = "ssh_unreachable"
    elif gpu_count <= 0:
        status_summary = "裸机主机未观测到可用 GPU，需要先确认驱动、容器运行时或 nvidia-smi。"
        confidence = 0.8
        health = "gpu_missing"
    elif process_count <= 0:
        status_summary = "GPU 可见但未观测到 vLLM 进程，推理服务可能未启动或已退出。"
        confidence = 0.78
        health = "vllm_process_missing"
    elif metrics_status not in {"ok", "healthy", "available"} and metrics_count <= 0:
        status_summary = "vLLM 进程存在但指标端点不可用，前端趋势可能无法展示。"
        confidence = 0.76
        health = "metrics_unavailable"
    else:
        status_summary = "裸机 GPU/vLLM 主机只读巡检通过，具备指标采集和后续故障实验前置条件。"
        confidence = 0.84
        health = "ready"

    commands = [
        "hostname && date",
        "nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits",
        "ps -eo pid,args | awk '/[v]llm|python.*openai|[a]pi_server/ && $0 !~ /awk/ {print}'",
        f"curl -fsS {_endpoint(payload)}",
        f"curl -fsS {_metrics_endpoint(payload)} | grep '^vllm:' | head -40",
    ]
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": status_summary,
        "evidence": evidence or ["生成裸机 LLM/GPU 主机只读巡检计划"],
        "host_id": host_id,
        "health": health,
        "observed": {
            "ssh_status": ssh_status,
            "gpu_count": gpu_count,
            "process_count": process_count,
            "endpoint_status": endpoint_status,
            "metrics_status": metrics_status,
            "metrics_count": metrics_count,
            "running_requests": running_requests,
            "waiting_requests": waiting_requests,
        },
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    return {
        "status": "ok",
        "confidence": 0.72,
        "summary": "验证裸机 GPU 可见、vLLM 进程存在、健康接口和指标端点可读。",
        "evidence": ["只读验证计划"],
        "checks": [
            "nvidia-smi -L",
            "ps -eo pid,args | awk '/[v]llm|python.*openai|[a]pi_server/ && $0 !~ /awk/ {print}'",
            f"curl -fsS {_endpoint(payload)}",
            f"curl -fsS {_metrics_endpoint(payload)} | grep '^vllm:' | head -20",
        ],
    }
