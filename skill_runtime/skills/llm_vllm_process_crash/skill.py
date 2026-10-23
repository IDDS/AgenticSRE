from __future__ import annotations

from typing import Any, Dict

from skill_runtime.confidence import calibrate_llm_fault_confidence
from skill_runtime.runtime_evidence import container_lifecycle


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    parts = [
        payload.get("fault_type"),
        payload.get("raw_fault_type"),
        payload.get("message"),
        payload.get("endpoint_status"),
        payload.get("process_status"),
        payload.get("observation"),
        payload.get("agent_evidence_summary"),
        payload.get("observed_evidence_summary"),
        payload.get("pod_recreated_in_window"),
        payload.get("container_restarted_in_window"),
        payload.get("last_terminated_reason"),
        payload.get("last_exit_code"),
    ]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend([item.get("source"), item.get("signal"), item.get("value"), item.get("message")])
        elif item:
            parts.append(str(item))
    return " ".join(str(x or "") for x in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    lifecycle_observed = bool(container_lifecycle(payload)["abnormal"])
    if not lifecycle_observed and any(token in text for token in (
        "waiting_requests", "num_requests_waiting", "ttft", "tpot", "e2e p95",
        "queue", "scheduler", "dcgm", "gpu_metric_absent", "gpu metrics missing",
        "dcgm_fi_dev_gpu_util",
    )):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "检测到队列/调度或 GPU 指标采集信号，且没有容器异常终止证据，跳过 vLLM 进程崩溃 Skill",
            "evidence": ["队列或指标信号不能单独证明进程崩溃"],
        }
    matched = lifecycle_observed or any(token in text for token in (
        "process_crash",
        "vllm_process_crash",
        "vllm process",
        "进程崩溃",
        "crashloopbackoff",
        "stopping container vllm",
        "killing pod/vllm",
        "last_terminated_reason",
    ))
    evidence = []
    if "connection refused" in text or "http 503" in text:
        evidence.append("vLLM 推理端点不可用")
    if "process_crash" in text or "vllm_process_crash" in text or "vllm process" in text or "进程崩溃" in text:
        evidence.append("实验场景或观测指向 vLLM 进程崩溃")
    if payload.get("process_count") == 0:
        evidence.append("vLLM 进程数为 0")
    if payload.get("pod_recreated_in_window"):
        evidence.append("观测窗口内 vLLM Pod 被重建，需结合容器终止状态判断是否异常退出")
    if payload.get("container_restarted_in_window"):
        evidence.append("观测窗口内 vLLM 容器发生重启")
    if payload.get("last_terminated_reason"):
        evidence.append(f"容器最近终止原因: {payload.get('last_terminated_reason')}")
    if payload.get("last_exit_code") is not None:
        evidence.append(f"容器最近退出码: {payload.get('last_exit_code')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 vLLM 进程崩溃信号" if matched else "未检测到 vLLM 进程崩溃信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    endpoint_status = payload.get("endpoint_status") or "unknown"
    metrics_status = payload.get("metrics_status") or "unknown"
    process_count = int(payload.get("process_count") or 0)
    gpu_count = int(payload.get("gpu_count") or 0)
    metrics_count = int(payload.get("metrics_count") or 0)
    waiting_requests = int(payload.get("waiting_requests") or 0)
    running_requests = int(payload.get("running_requests") or 0)
    dry_run = bool(context.get("dry_run", True))
    confidence, explanation = calibrate_llm_fault_confidence(
        base_confidence=0.9,
        scenario_present=False,
        observation_present=bool((context.get("detect") or {}).get("evidence")),
        endpoint_status=endpoint_status,
        metrics_status=metrics_status,
        process_count=process_count,
        gpu_count=gpu_count,
        metrics_count=metrics_count,
        waiting_requests=waiting_requests,
        running_requests=running_requests,
        dry_run=dry_run,
    )
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if metrics_count:
        evidence.append(f"Prometheus/vLLM 指标点 {metrics_count} 个")
    if gpu_count:
        evidence.append(f"GPU 数量 {gpu_count}")
    recovered = str(endpoint_status).lower() in {"ok", "healthy", "up", "200"} and process_count > 0
    root_cause = (
        "vLLM 主进程在观测窗口内异常退出，Kubernetes 已重启容器并恢复推理端点"
        if recovered
        else "vLLM 主进程异常退出，当前推理端点仍不可用"
    )
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": "LLM::vllm_process_crash",
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("host_id") or "vllm",
        "confidence_explanation": explanation,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or "vllm-server"
    host_id = payload.get("host_id") or ""
    service_name = payload.get("service_name") or payload.get("systemd_service") or "vllm"
    endpoint = payload.get("endpoint") or payload.get("vllm_endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:8000/health}"
    replica_count = int(payload.get("replica_count") or payload.get("desired_replicas") or 1)
    suggestions = [
        {
            "step": "检查 vLLM 指标端点和健康接口",
            "command": f"curl -fsS {endpoint}",
            "risk": "low",
        },
        {
            "step": f"恢复 vLLM Deployment {deployment} 副本数",
            "command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas={replica_count}",
            "rollback_command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=0",
            "risk": "medium",
        },
        {
            "step": f"重启 vLLM Deployment {deployment}",
            "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
            "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "risk": "medium",
        },
    ]
    if host_id:
        suggestions.insert(1, {
            "step": f"查看裸机目标 {host_id} vLLM 进程",
            "command": "pgrep -af 'vllm|python.*openai' || true",
            "risk": "low",
        })
        suggestions.insert(2, {
            "step": f"重启裸机目标 {host_id} vLLM 服务",
            "command": f"systemctl restart {service_name}",
            "rollback_command": f"systemctl status {service_name} --no-pager",
            "risk": "high",
        })
    return {
        "status": "ok",
        "confidence": 0.84,
        "summary": "生成 vLLM 进程恢复建议；真实执行必须审批，避免误重启推理服务。",
        "evidence": ["恢复动作含健康检查和 rollout 回滚"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    deployment = _payload(context).get("deployment") or "vllm-server"
    endpoint = _payload(context).get("endpoint") or _payload(context).get("vllm_endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:8000/health}"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "执行后验证 vLLM 健康接口、运行中请求、TTFT/TPOT 和 Pod ready。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl rollout status deployment/{deployment} -n {namespace}",
            f"curl -fsS {endpoint}",
        ],
    }
