from __future__ import annotations

from typing import Any, Dict, List


AISH_DOMAINS = {
    "gpu": "GPU 硬件/驱动/状态异常",
    "network": "以太网络/DNS/链路异常",
    "storage": "GPFS/NFS/磁盘/挂载异常",
    "kubernetes": "Kubernetes 工作负载或服务编排异常",
    "container-runtime": "容器运行时或镜像/资源配置异常",
    "distributed-training": "分布式训练/NCCL/RDMA 通信异常",
    "rdma": "RDMA/NCCL/HCCL 通信异常",
    "process": "关键进程异常退出或卡死",
}

AIOPS_EVAL_METRICS = {
    "diagnosis_pass_rate": {"unit": "ratio", "direction": "higher_is_better", "label": "诊断通过率"},
    "fault_injection_success_rate": {"unit": "ratio", "direction": "higher_is_better", "label": "故障注入成功率"},
    "recovery_success_rate": {"unit": "ratio", "direction": "higher_is_better", "label": "恢复成功率"},
    "avg_turns": {"unit": "turns", "direction": "lower_is_better", "label": "平均交互轮次"},
    "avg_latency_s": {"unit": "s", "direction": "lower_is_better", "label": "平均诊断耗时"},
    "error_rate": {"unit": "ratio", "direction": "lower_is_better", "label": "错误率"},
}

INFERENCE_LLM_METRICS = {
    "TTFT_Mean_ms": {"unit": "ms", "direction": "lower_is_better", "label": "首 token 平均时延"},
    "TPOT_Mean_ms": {"unit": "ms", "direction": "lower_is_better", "label": "单输出 token 平均间隔"},
    "Output_tok_throughput_tok_per_s": {"unit": "tok/s", "direction": "higher_is_better", "label": "输出 token 吞吐"},
    "Total_tok_throughput_tok_per_s": {"unit": "tok/s", "direction": "higher_is_better", "label": "总 token 吞吐"},
    "error_rate": {"unit": "ratio", "direction": "lower_is_better", "label": "请求错误率"},
}

DOMAIN_EXPECTED_EVIDENCE = {
    "gpu": ["DCGM/nvidia-smi GPU 利用率、显存、温度、XID/ECC", "Pod/容器 GPU 资源分配", "vLLM TTFT/TPOT/E2E 延迟变化"],
    "rdma": ["RDMA/NCCL/HCCL 错误日志", "mlx5/GID/链路状态", "分布式通信吞吐或超时"],
    "network": ["Service/Endpoint 连通性", "DNS/MTU/丢包/限速指标", "业务请求失败率和延迟"],
    "storage": ["PVC/挂载事件", "NFS/GPFS/磁盘错误", "I/O 延迟和应用错误日志"],
    "kubernetes": ["Pod/Deployment/Service/Endpoint 状态", "K8s Event", "Prometheus target 和业务 SLO"],
    "container-runtime": ["镜像拉取/容器运行时事件", "容器重启与退出码", "节点 runtime 日志"],
    "distributed-training": ["NCCL/HCCL/all_reduce 日志", "RDMA/PCIe/NVLink 链路状态", "训练/推理吞吐下降"],
    "process": ["关键进程存活状态", "systemd/进程退出码", "业务接口可用性"],
}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    parts = [
        payload.get("source"),
        payload.get("external_source"),
        payload.get("fault_type"),
        payload.get("fault_domain"),
        payload.get("scenario"),
        payload.get("scenario_id"),
        payload.get("message"),
        diagnosis.get("root_cause"),
        diagnosis.get("fault_type"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def _domains(payload: Dict[str, Any], text: str) -> List[str]:
    raw = payload.get("domains") or payload.get("fault_domains") or []
    domains = [str(item).lower() for item in raw if str(item).strip()] if isinstance(raw, list) else []
    keywords = {
        "gpu": ("gpu", "nvidia", "xid", "ecc", "temperature", "显卡"),
        "rdma": ("rdma", "nccl", "hccl", "roce", "gid", "mlx5"),
        "network": ("network", "dns", "mtu", "tc", "packet", "网络"),
        "storage": ("storage", "gpfs", "nfs", "disk", "nvme", "pvc", "存储"),
        "kubernetes": ("k8s", "kubernetes", "pod", "deployment", "service"),
        "container-runtime": ("imagepull", "container", "harbor", "runtime"),
        "distributed-training": ("distributed", "training", "all_reduce", "torchrun", "mpirun"),
        "process": ("process", "zombie", "sshd", "systemd", "进程"),
    }
    for domain, keys in keywords.items():
        if any(key in text for key in keys) and domain not in domains:
            domains.append(domain)
    return domains


def _metric_observations(payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    observed: Dict[str, Any] = {}
    for name in INFERENCE_LLM_METRICS:
        if name in metrics:
            observed[name] = metrics[name]
    aliases = {
        "ttft_p95_seconds": ("TTFT_Mean_ms", 1000),
        "tpot_p95_seconds": ("TPOT_Mean_ms", 1000),
        "e2e_p95_seconds": ("E2E_P95_ms", 1000),
        "e2e_latency_p95_s": ("E2E_P95_ms", 1000),
        "request_success_rate_5m": ("request_success_rate", 1),
        "loadgen_errors": ("loadgen_errors", 1),
    }
    for key, (metric_name, scale) in aliases.items():
        value = payload.get(key, metrics.get(key))
        if value is not None and value != "":
            try:
                observed[metric_name] = round(float(value) * scale, 3)
            except (TypeError, ValueError):
                observed[metric_name] = value
    return observed


def _expected_evidence(domains: List[str]) -> List[str]:
    items: List[str] = []
    for domain in domains or ["generic"]:
        for item in DOMAIN_EXPECTED_EVIDENCE.get(domain, []):
            if item not in items:
                items.append(item)
    if not items:
        items.extend(["AEMU 注入/恢复记录", "K8s 事件", "Prometheus 观测指标", "业务请求 SLO"])
    return items


def _evaluation_gate(payload: Dict[str, Any], evidence: List[str], observed_metrics: Dict[str, Any]) -> Dict[str, Any]:
    signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
    ready_checks = {
        "has_aishperf_case": bool(payload.get("scenario_id") or payload.get("aemu_fault_id")),
        "has_dry_run_plan": bool(payload.get("dry_run_plan") or signals.get("aemu_plan_ready")),
        "has_observable_metrics": bool(observed_metrics or payload.get("prometheus_url")),
        "has_diagnosis_evidence": len(evidence) >= 3,
    }
    ready = all(ready_checks.values())
    return {
        "method": "AISHPerf aiops-eval diagnosis gate",
        "ready": ready,
        "checks": ready_checks,
        "required_metrics": AIOPS_EVAL_METRICS,
        "inference_metric_spec": INFERENCE_LLM_METRICS,
        "score_hint": "诊断通过率、恢复成功率越高越好；平均轮次、平均诊断耗时、错误率越低越好。",
    }


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    domains = _domains(payload, text)
    is_aish = any(token in text for token in ("aishperf", "aemu", "alcor", "aiops-chaos", "alcor_ops"))
    is_aish = is_aish or bool(payload.get("aemu_fault_id") or payload.get("dry_run_plan"))
    matched = is_aish
    evidence = []
    if is_aish:
        evidence.append("输入包含 AISHPerf/AEMU/Alcor AIOps 来源标识")
    if domains:
        evidence.append("故障域: " + "、".join(domains))
    if payload.get("aemu_fault_id") or payload.get("scenario_id"):
        evidence.append(f"外部场景 ID: {payload.get('aemu_fault_id') or payload.get('scenario_id')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 AISHPerf AI Infra 故障诊断上下文" if matched else "未检测到 AISHPerf AI Infra 上下文",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    domains = _domains(payload, text)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    if payload.get("expected_root_cause"):
        evidence.append(f"AISHPerf 期望根因标签: {payload.get('expected_root_cause')}")
    if signals:
        evidence.append("存在外部故障信号: " + "、".join(sorted(signals)[:6]))
    if metrics:
        evidence.append("存在外部评测/观测指标: " + "、".join(sorted(metrics)[:6]))
    observed_metrics = _metric_observations(payload)
    if observed_metrics:
        evidence.append("推理性能指标可用于 AISHPerf inference_llm 评分: " + "、".join(sorted(observed_metrics)[:6]))

    primary = domains[0] if domains else "generic"
    root_cause = AISH_DOMAINS.get(primary, "AI Infra 复合故障，需要结合 AEMU 注入记录、K8S 事件、GPU/网络/存储指标联合定位")
    if "rdma" in domains or "distributed-training" in domains:
        fault_type = "LLM::distributed_training_or_rdma_fault"
        root_cause = "分布式训练或推理通信链路异常，重点检查 RDMA/NCCL/HCCL、GID、PCIe ACS、链路 down 和训练日志"
    elif "gpu" in domains:
        fault_type = "LLM::gpu_hardware_or_driver_fault"
    elif "storage" in domains:
        fault_type = "LLM::aiinfra_storage_fault"
    elif "network" in domains:
        fault_type = "LLM::aiinfra_network_fault"
    elif "kubernetes" in domains:
        fault_type = "LLM::k8s_workload_or_service_fault"
    else:
        fault_type = "LLM::aishperf_aiinfra_fault"

    expected_evidence = _expected_evidence(domains)
    evaluation_gate = _evaluation_gate(payload, evidence, observed_metrics)
    confidence = 0.91 if evaluation_gate["ready"] else (0.86 if len(evidence) >= 3 else 0.78)
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("component") or payload.get("node") or payload.get("deployment") or primary,
        "expected_evidence": expected_evidence,
        "observed_metrics": observed_metrics,
        "evaluation_gate": evaluation_gate,
        "external_method": {
            "source": "AISHPerf aiops-chaos + aiops-eval",
            "scenario": "alcor-ops-diagnosis-full",
            "diagnosis_domains": domains,
            "fault_domains": ["gpu", "network", "storage", "kubernetes", "container-runtime", "distributed-training"],
            "metrics": AIOPS_EVAL_METRICS,
            "inference_llm_metrics": INFERENCE_LLM_METRICS,
            "expected_evidence": expected_evidence,
            "note": "复用 AISHPerf 的故障域、诊断通过率/恢复成功率/耗时等评价准则；真实评分仍以 AgenticSRE 采集证据和恢复验证为准。",
        },
    }
