"""Workflow orchestration for skill-based SRE operations.

This module is the migration layer between the old monolithic AgenticSRE flow
and the standalone skill runtime. It keeps skills small, then composes their
detect/diagnose/heal/verify outputs into a single operational plan.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from .confidence import calibrate_skill_diagnosis_confidence
from .executor import SkillExecutor
from .fault_taxonomy import classify_domain, classify_payload, preferred_skill_for_payload, skill_aliases
from .runtime_evidence import has_abnormal_container_termination, has_cuda_runtime_evidence
from .tool_context import ToolRunner


RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
HELPER_SKILL_IDS = {
    "generic.risk_gatekeeper",
    "generic.recovery_verifier",
    "generic.evidence_collection",
}
HELPER_TAGS = {"gatekeeper", "verification", "risk", "evidence"}


def _risk_rank(value: str) -> int:
    return RISK_RANK.get(str(value or "low").lower(), 2)


def _stage(run: Dict[str, Any], stage: str) -> Dict[str, Any]:
    return ((run.get("stages") or {}).get(stage) or {})


def _output(run: Dict[str, Any], stage: str) -> Dict[str, Any]:
    return _stage(run, stage).get("output") or {}


def _confidence(run: Dict[str, Any]) -> float:
    return float(_stage(run, "diagnose").get("confidence") or 0.0)


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_action(item: Dict[str, Any]) -> bool:
    return bool(str(item.get("command") or "").strip() or str(item.get("operation") or "").strip())


def _action_key(item: Dict[str, Any]) -> str:
    command = str(item.get("command") or "").strip()
    if command:
        return f"command:{command}"
    operation = str(item.get("operation") or "").strip()
    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    return f"operation:{operation}:{json.dumps(parameters, ensure_ascii=False, sort_keys=True, default=str)}"


def _suggestions(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _output(run, "heal").get("suggestions") or []
    selected = [item for item in raw if isinstance(item, dict) and _has_action(item)]
    declared = set(run.get("declared_stages") or (run.get("skill") or {}).get("stages") or [])
    if "heal" in declared:
        return selected

    diagnose = _output(run, "diagnose")
    diagnostic_actions = diagnose.get("suggestions") or diagnose.get("recommended_actions") or []
    for item in diagnostic_actions:
        if isinstance(item, dict) and _has_action(item) and not _is_mutating(item):
            selected.append(item)
    for command in diagnose.get("commands") or []:
        item = {"step": "执行只读诊断检查", "command": str(command), "risk": "low"}
        if item["command"].strip() and not _is_mutating(item):
            selected.append(item)
    return selected


def _checks(run: Dict[str, Any]) -> List[str]:
    raw = _output(run, "verify").get("checks") or []
    return [str(item) for item in raw if str(item).strip()]


def _evidence(run: Dict[str, Any]) -> List[str]:
    seen = set()
    merged: List[str] = []
    for stage in ("detect", "diagnose", "heal", "verify"):
        for item in _stage(run, stage).get("evidence") or []:
            text = str(item)
            if text and text not in seen:
                seen.add(text)
                merged.append(text)
    return merged


def _diagnostic_evidence(run: Dict[str, Any]) -> List[str]:
    seen = set()
    merged: List[str] = []
    for stage in ("detect", "diagnose"):
        for item in _stage(run, stage).get("evidence") or []:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                merged.append(text)
    return merged


def _skill_id(run: Dict[str, Any]) -> str:
    return str((run.get("skill") or {}).get("id") or "")


def _candidate_skill_score(skill_id: str, payload: Dict[str, Any]) -> int:
    """Score soft skill candidates without treating them as final diagnosis."""
    if not skill_id:
        return 0
    candidate_ids = []
    raw = payload.get("candidate_skills")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                value = item.get("id") or item.get("skill_id") or item.get("candidate_skill")
                score = item.get("score") or item.get("confidence") or item.get("weight")
            else:
                value = item
                score = None
            text = str(value or "").strip()
            if text:
                candidate_ids.append((text, score))
    for item in payload.get("observation_signals") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("candidate_skill") or "").strip()
        if text:
            candidate_ids.append((text, item.get("score") or item.get("confidence") or item.get("weight")))
    best = 0
    for candidate_id, score in candidate_ids:
        if candidate_id != skill_id:
            continue
        try:
            weight = float(score) if score is not None else 1.0
        except (TypeError, ValueError):
            weight = 1.0
        best = max(best, int(8 + max(0.0, min(1.0, weight)) * 6))
    return best


def _has_vllm_k8s_context(payload: Dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key, "")) for key in (
        "domain", "namespace", "deployment", "service", "pod", "message", "model",
    )).lower()
    return str(payload.get("domain") or "").lower() == "llm" and any(
        payload.get(key) for key in ("namespace", "deployment", "service", "pod")
    ) and any(token in text for token in ("vllm", "llm", "inference", "推理", "智算"))


def _has_node_resource_observation(payload: Dict[str, Any]) -> bool:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    metric_name = str(metrics.get("metric") or metrics.get("name") or metrics.get("signal") or payload.get("metric") or "").lower()
    if metric_name in {
        "node_cpu_usage", "node_memory_usage", "node_rootfs_usage",
        "node_disk_read", "node_disk_write", "node_network_receive", "node_network_transmit",
    }:
        return True
    if metric_name.startswith("node_") and any(token in metric_name for token in ("cpu", "memory", "rootfs", "disk", "network", "pressure")):
        return True
    background_signals = {
        str(item.get("signal") or item.get("metric") or "").lower()
        for item in payload.get("background_observation_signals") or []
        if isinstance(item, dict)
    }
    for item in payload.get("observation_signals") or []:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or item.get("metric") or "").lower()
        if signal.startswith("node_") and signal not in background_signals:
            return True
    raw = " ".join(str(payload.get(key) or "") for key in ("fault_type", "raw_fault_type", "event_type")).lower()
    if "node_resource_pressure" in raw or "observable_resource_pressure" in raw:
        return True
    if "fault_experiment_signal" in raw:
        return False
    for key in (
        "node_cpu_usage", "node_memory_usage", "node_rootfs_usage",
        "node_disk_read", "node_disk_write", "node_network_receive", "node_network_transmit",
    ):
        if key in payload and key not in background_signals:
            return True
    return False


def _has_teller_trace_context(payload: Dict[str, Any]) -> bool:
    fault_text = " ".join(str(payload.get(key, "")) for key in (
        "fault_type", "raw_fault_type", "message", "metric",
    )).lower()
    if any(token in fault_text for token in (
        "teller", "request_span", "request_context", "kernel_p95",
        "teller_request_latency_high", "teller_kernel_bottleneck",
        "teller_request_context_missing", "请求级", "调用路径",
    )):
        return True
    return any(isinstance(payload.get(key), dict) and payload.get(key) for key in (
        "fine_grained_trace", "request_context", "fine_grained_trace_summary",
    ))


def _endpoint_unavailable(payload: Dict[str, Any]) -> bool:
    endpoint_status = str(payload.get("endpoint_status") or "").lower()
    try:
        endpoint_count = int(float(payload.get("endpoint_count")))
    except (TypeError, ValueError):
        endpoint_count = -1
    return endpoint_status in {"failed", "down", "unavailable"} or endpoint_count == 0


def _payload_number(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for name in names:
        value = payload.get(name) if name in payload else metrics.get(name)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _signal_threshold(payload: Dict[str, Any], signal: str, default: float) -> float:
    for item in payload.get("observation_signals") or []:
        if not isinstance(item, dict) or str(item.get("signal") or "") != signal:
            continue
        try:
            return float(item.get("threshold"))
        except (TypeError, ValueError):
            break
    return default


def _has_business_slo_degradation(payload: Dict[str, Any]) -> bool:
    success_keys = (
        "request_success_ratio", "loadgen_success_rate", "success_rate",
        "request_success_rate", "request_success_rate_5m",
    )
    success_present = any(key in payload or key in (payload.get("metrics") or {}) for key in success_keys)
    success_rate = _payload_number(payload, *success_keys, default=1.0)
    if 1.0 < success_rate <= 100.0:
        success_rate /= 100.0
    if success_present and success_rate < _signal_threshold(payload, "success_rate", 0.99):
        return True
    thresholds = (
        ("ttft_p95_seconds", "ttft_p95_threshold_seconds", 1.5),
        ("tpot_p95_seconds", "tpot_p95_threshold_seconds", 0.08),
        ("e2e_p95_seconds", "e2e_p95_threshold_seconds", 2.0),
    )
    for signal, threshold_key, default_threshold in thresholds:
        value = _payload_number(payload, signal)
        threshold = _payload_number(
            payload,
            threshold_key,
            default=_signal_threshold(payload, signal, default_threshold),
        )
        if value >= threshold > 0:
            return True
    return False


def _has_scheduler_causality(payload: Dict[str, Any]) -> bool:
    if _payload_number(payload, "waiting_requests", "vllm:num_requests_waiting") > 0:
        return True
    if _candidate_skill_score("llm.vllm_scheduler_queue", payload):
        return True
    text = " ".join(str(payload.get(key) or "") for key in (
        "fault_type", "raw_fault_type", "message", "observed_evidence_summary",
    )).lower()
    return any(token in text for token in (
        "request_queue_overflow", "continuous_batch", "prefill_decode",
        "attention_backend", "scheduler backlog", "queue backlog", "队列堆积", "调度退化",
    ))


def _has_process_crash_observation(payload: Dict[str, Any]) -> bool:
    """Return whether runtime state shows an unexpected container termination."""
    return has_abnormal_container_termination(payload)


def _resource_candidate_should_cross_domain(payload: Dict[str, Any]) -> bool:
    raw = " ".join(str(payload.get(key, "")) for key in (
        "fault_type", "raw_fault_type", "event_type",
    )).lower()
    if _endpoint_unavailable(payload):
        return False
    if "node_resource_pressure" in raw:
        return False
    return "observable_resource_pressure" in raw or "fault_experiment_signal" in raw


def _is_bridge_run(run: Optional[Dict[str, Any]]) -> bool:
    if not run:
        return False
    skill = run.get("skill") or {}
    tags = {str(item) for item in skill.get("tags") or []}
    return "bridge" in tags or _skill_id(run).endswith("legacy_recipe_bridge")


def _is_tool_run(run: Optional[Dict[str, Any]]) -> bool:
    if not run:
        return False
    skill = run.get("skill") or {}
    tags = {str(item).lower() for item in skill.get("tags") or []}
    return "tool" in tags or _skill_id(run).startswith("tool.")


def _is_generic_run(run: Optional[Dict[str, Any]]) -> bool:
    if not run:
        return False
    skill = run.get("skill") or {}
    return skill.get("domain") == "generic" or str(skill.get("id") or "").startswith("generic.")


def _is_helper_run(run: Optional[Dict[str, Any]]) -> bool:
    if not run:
        return False
    skill = run.get("skill") or {}
    skill_id = str(skill.get("id") or "")
    tags = {str(item).lower() for item in skill.get("tags") or []}
    return skill_id in HELPER_SKILL_IDS or bool(tags.intersection(HELPER_TAGS))


def _diagnosis_quality(run: Optional[Dict[str, Any]]) -> int:
    if not run:
        return 0
    diagnose = _stage(run, "diagnose")
    output = diagnose.get("output") or {}
    score = 0
    if diagnose.get("status") in {"ok", "matched"}:
        score += 1
    if output.get("fault_type"):
        score += 1
    if output.get("root_cause"):
        score += 2
    if output.get("root_cause_component"):
        score += 1
    score += min(len(_diagnostic_evidence(run)), 3)
    return score


def _skill_specificity_rank(run: Optional[Dict[str, Any]]) -> int:
    if not run:
        return 0
    if _is_generic_run(run):
        return 0
    if _is_tool_run(run):
        return 1
    if _is_bridge_run(run):
        return 2
    return 3


def _is_mutating(item: Dict[str, Any]) -> bool:
    command = f" {item.get('command') or ''} ".lower()
    operation = str(item.get("operation") or "").strip().lower()
    if str(item.get("risk") or "").lower() in {"medium", "high", "critical"}:
        return True
    if operation and any(
        token in operation
        for token in (".inject", ".execute", ".cleanup", ".rollback", ".restart", ".scale", ".delete")
    ):
        return True
    tokens = (" scale ", " restart ", " delete ", " drain ", " cordon ", " uncordon ", " set image ", " set resources ")
    return any(token in command for token in tokens)


def _reuses_approved_transaction_undo(item: Dict[str, Any]) -> bool:
    """Recognize the one mutation allowed to reuse an approved fault transaction."""
    if str(item.get("operation") or "") != "agenticsre.k8s.remediation.execute_undo":
        return False
    if str(item.get("authorization_basis") or "") != "approved_fault_transaction_undo":
        return False
    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    undo = parameters.get("undo_action") if isinstance(parameters.get("undo_action"), dict) else {}
    return str(undo.get("operation") or "") in {
        "agenticsre.k8s.fault.cleanup",
        "agenticsre.precision.cleanup",
    }


def _payload_affinity(run: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> int:
    """Prefer skills whose scope matches explicit structured context."""
    if not run:
        return 0
    skill_id = _skill_id(run)
    operation = str(payload.get("operation") or payload.get("intent") or "").lower()
    if operation in {"fault_injection", "inject", "chaos", "chaos_experiment"}:
        return 500 if skill_id == "k8s.guarded_fault_injection" else 0
    if operation in {"transactional_self_healing", "execute_undo", "agenticsre.k8s.remediation.plan_undo"}:
        return 500 if skill_id == "k8s.transactional_self_healing" else 0
    taxonomy = classify_payload(payload)
    preferred_skill = preferred_skill_for_payload(payload)
    if _has_vllm_k8s_context(payload) and _has_node_resource_observation(payload):
        if (
            _candidate_skill_score("k8s.resource_pressure", payload)
            and skill_id == "k8s.resource_pressure"
            and _resource_candidate_should_cross_domain(payload)
        ):
            return 34
        if skill_id == "llm.k8s_vllm_observability":
            return 32
        if skill_id == "k8s.resource_pressure":
            return 12
    if preferred_skill and skill_id == preferred_skill:
        return 15
    if skill_id == "llm.business_slo" and _has_business_slo_degradation(payload):
        raw_signal = " ".join(str(payload.get(key) or "") for key in (
            "fault_type", "raw_fault_type", "event_type",
        )).lower()
        observation_driven = (
            "fault_experiment_signal" in raw_signal
            or bool(_candidate_skill_score("llm.business_slo", payload))
        )
        if observation_driven:
            if _endpoint_unavailable(payload) or _has_node_resource_observation(payload):
                return 8
            return 36 if not _has_scheduler_causality(payload) else 20
    if skill_id == "llm.vllm_scheduler_queue" and _has_scheduler_causality(payload):
        return 34
    candidate_score = _candidate_skill_score(skill_id, payload)
    if candidate_score:
        return candidate_score
    fault_text = " ".join(str(payload.get(key, "")) for key in (
        "fault_type", "raw_fault_type", "scenario_id", "scenario_name", "message",
        "agent_evidence_summary",
    )).lower()
    observed_text = " ".join(str(payload.get(key, "")) for key in (
        "fault_type", "raw_fault_type", "message", "observed_evidence_summary",
    )).lower()
    scrape_signal = any(token in fault_text for token in (
        "metricsbindaddress",
        "metrics bind",
        "/metrics",
        "servicemonitor",
        "podmonitor",
        "scrape",
        "prometheus cannot scrape",
        "prometheus target",
        "scrape_error",
    ))
    observed_scrape_signal = any(token in observed_text for token in (
        "metricsbindaddress",
        "metrics bind",
        "/metrics",
        "servicemonitor",
        "podmonitor",
        "scrape",
        "prometheus cannot scrape",
        "prometheus target",
        "scrape_error",
    ))
    aliases = skill_aliases(skill_id)
    if skill_id.startswith("k8s.") and aliases and any(token in fault_text for token in aliases):
        if skill_id == "k8s.prometheus_scrape_target" and observed_scrape_signal:
            return 28
        if skill_id == "k8s.observability_signal" and observed_scrape_signal:
            return 2
        return 10
    if skill_id == "k8s.prometheus_scrape_target" and observed_scrape_signal:
        return 28
    if skill_id == "k8s.observability_signal" and observed_scrape_signal:
        return 2
    if skill_id == "llm.vllm_process_crash" and aliases and any(token in fault_text for token in aliases):
        return 12
    if skill_id == "llm.vllm_process_crash" and _has_process_crash_observation(payload):
        return 40
    if skill_id == "llm.aishperf_aiinfra_diagnosis":
        source_text = " ".join(str(payload.get(key, "")) for key in (
            "source", "external_source", "scenario_id", "aemu_fault_id", "scenario_name", "fault_type", "message"
        )).lower()
        if any(token in source_text for token in ("aishperf", "aemu", "alcor", "aiops-chaos")):
            return 12
    if skill_id == "llm.teller_request_trace_diagnosis":
        trace = payload.get("fine_grained_trace") if isinstance(payload.get("fine_grained_trace"), dict) else {}
        request_context = payload.get("request_context") if isinstance(payload.get("request_context"), dict) else {}
        has_trace = bool(trace or request_context or payload.get("fine_grained_trace_summary"))
        has_teller_signal = any(token in fault_text for token in (
            "teller", "request_span", "request_context", "kernel_p95",
            "teller_request_latency_high", "teller_kernel_bottleneck",
            "teller_request_context_missing", "请求级", "调用路径",
        ))
        if has_trace or has_teller_signal:
            return 25
    if skill_id == "llm.teller_trace_collection":
        trace = payload.get("fine_grained_trace") if isinstance(payload.get("fine_grained_trace"), dict) else {}
        if trace or any(key in payload for key in ("trace_age_seconds", "cuda_events", "nvtx_events", "request_count")):
            return 30
    if skill_id == "llm.cuda_driver_failure":
        if has_cuda_runtime_evidence(payload) or any(key in payload for key in ("kernel_p95_ms", "teller_kernel_p95_ms")):
            return 30
    if skill_id == "llm.distributed_communication":
        if any(key in payload for key in ("nccl_error_count", "rdma_error_count", "packet_loss_pct", "tensor_parallel_size")):
            return 30
    if skill_id == "llm.model_asset_failure":
        if any(key in payload for key in ("model_asset_error", "tokenizer_error_count", "model_load_error")):
            return 30
    if skill_id == "llm.gpu_hardware_health":
        if any(key in payload for key in ("gpu_throttle_reasons", "gpu_temperature_c", "xid_errors", "ecc_errors", "pcie_replay_errors")):
            return 30
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    if skill_id == "tool.llm_host_readiness":
        if str(payload.get("target_mode") or "").lower() == "k8s" or any(payload.get(key) for key in ("namespace", "deployment", "service", "pod")):
            return 0
        host_keys = {
            "host_id", "host", "ssh_status", "gpu_count", "process_count",
            "endpoint_status", "metrics_status", "metrics_count",
        }
        if str(payload.get("target_mode") or "").lower() == "host" or any(key in payload for key in ("host_id", "host", "ssh_status")):
            return 5 if any(key in payload for key in host_keys) else 0
        return 0
    if skill_id == "llm.k8s_vllm_observability":
        has_k8s_object = any(payload.get(key) for key in ("deployment", "service", "pod"))
        text = " ".join(str(payload.get(key, "")) for key in ("domain", "fault_type", "message", "deployment", "service")).lower()
        if _has_teller_trace_context(payload):
            return 0
        if not _has_node_resource_observation(payload) and any(token in fault_text for token in (
            "waiting_requests", "num_requests_waiting", "ttft", "tpot", "queue",
            "scheduler", "调度", "队列", "dcgm", "gpu_metric_absent",
            "gpu metrics missing", "dcgm_fi_dev_gpu_util",
        )):
            return 0
        if any(token in fault_text for token in ("vllm_process_crash", "process_crash", "vllm process crash")):
            return 0
        if taxonomy.get("llm_experiment"):
            return 14 if has_k8s_object else 9
        if any(token in fault_text for token in (
            "endpoint_timeout", "http_503", "grpc_failure", "connection refused",
            "no endpoints", "service selector", "service endpoint", "端点", "服务发现",
        )):
            return 13 if has_k8s_object else 3
        has_observability_context = any(payload.get(key) for key in (
            "metrics_status", "metrics_count", "endpoint_status",
            "deployment_ready_replicas", "deployment_desired_replicas",
            "e2e_p95_seconds", "e2e_latency_p95_s",
        )) or "prometheus metrics" in fault_text
        if not has_observability_context and any(token in fault_text for token in (
            "gpu memory high", "gpu pressure", "kv cache pressure", "gpu_oom",
            "cuda out of memory", "business_slo", "timeout", "latency", "degraded",
        )):
            return 0
        has_vllm = any(token in text for token in ("vllm", "llm", "inference", "ttft", "tpot", "e2e"))
        has_metric = any(key in payload or key in metrics for key in (
            "e2e_p95_seconds", "e2e_latency_p95_s", "ttft_p95_seconds", "tpot_p95_seconds",
            "request_failures", "loadgen_errors", "loadgen_success", "request_success_rate_5m",
            "vllm:num_requests_running", "vllm:num_requests_waiting",
        ))
        if has_k8s_object and (has_vllm or has_metric):
            return 9 if str(payload.get("target_mode") or "").lower() == "k8s" else 7
        return 0
    if skill_id == "llm.model_serving_endpoint":
        endpoint_status = str(payload.get("endpoint_status") or "").lower()
        try:
            endpoint_count = int(float(payload.get("endpoint_count")))
        except (TypeError, ValueError):
            endpoint_count = -1
        if endpoint_status in {"failed", "down", "unavailable"} or endpoint_count == 0:
            if str(payload.get("domain") or "").lower() != "llm" or _has_teller_trace_context(payload):
                return 0
            return 26
        if any(token in fault_text for token in (
            "endpoint_timeout", "observable_endpoint_unavailable", "http_503",
            "connection refused", "/v1/models", "/v1/completions", "model_not_found",
            "openai_api_error", "bad_request",
        )):
            if str(payload.get("domain") or "").lower() != "llm" or _has_teller_trace_context(payload):
                return 0
            return 18
        return 0
    if skill_id == "llm.business_slo":
        if any(token in fault_text for token in (
            "endpoint_timeout", "http_503", "grpc_failure",
            "vllm_process_crash", "process_crash", "gpu_oom", "resource_pressure",
        )):
            return 0
        slo_keys = {
            "success_rate", "request_success_rate", "ttft_p95_seconds",
            "tpot_p95_seconds", "e2e_p95_seconds", "waiting_requests",
            "running_requests", "kv_cache_usage", "gpu_utilization",
            "gpu_memory_usage",
        }
        score = 5 if any(key in payload or key in metrics for key in slo_keys) else 0
        if "business_slo" in fault_text or "slo" in fault_text:
            score = max(score, 8)
        elif any(token in fault_text for token in ("ttft", "tpot", "latency", "queue", "p95")):
            score = max(score, 5)
        return score
    if skill_id == "llm.vllm_scheduler_queue":
        if any(token in fault_text for token in (
            "waiting_requests", "num_requests_waiting", "ttft", "tpot", "queue",
            "scheduler", "调度", "队列",
        )):
            return 24
        return 0
    if skill_id == "llm.dcgm_exporter_observability":
        if any(token in fault_text for token in (
            "dcgm", "gpu_metric_absent", "gpu metrics missing", "dcgm_fi_dev_gpu_util",
            "gpu 指标", "显卡指标", "target_health",
        )):
            return 24
        return 0
    if skill_id == "llm.gpu_resource_pressure":
        if any(token in fault_text for token in (
            "dcgm", "gpu_metric_absent", "gpu metrics missing", "dcgm_fi_dev_gpu_util",
            "gpu 指标缺失", "显卡指标缺失",
        )):
            return 0
        if "business_slo" in fault_text:
            return 0
        if any(token in fault_text for token in ("gpu_oom", "cuda out of memory", "kv cache pressure", "kv_cache", "gpu memory high", "gpu pressure", "显存")):
            return 12
        if ("gpu memory" in fault_text or "kv cache" in fault_text) and any(token in fault_text for token in ("pressure", "high", "oom", "exhaust")):
            return 12
        gpu_keys = {"gpu_memory_usage", "gpu_memory_used_ratio", "gpu_memory_used_pct", "kv_cache_usage", "vllm:gpu_cache_usage_perc"}
        return 5 if any(key in payload or key in metrics for key in gpu_keys) else 0
    return 0


def _explicit_fault_affinity(run: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> int:
    """Give structured fault_type a stronger vote than broad text matches."""
    if not run:
        return 0
    skill_id = _skill_id(run)
    raw = " ".join(str(payload.get(key, "")) for key in (
        "fault_type", "raw_fault_type", "k8s_fault_type", "llm_fault_type"
    )).lower()
    if not raw:
        return 0
    if str(payload.get("domain") or "").lower() != "llm" or _has_teller_trace_context(payload):
        if skill_id == "llm.model_serving_endpoint":
            return 0
    if skill_id == "llm.cuda_driver_failure" and has_cuda_runtime_evidence(payload):
        return 240
    if skill_id == "llm.vllm_process_crash" and _has_process_crash_observation(payload):
        return 220
    if skill_id == "llm.model_serving_endpoint" and _endpoint_unavailable(payload):
        return 180
    if _has_vllm_k8s_context(payload) and _has_node_resource_observation(payload):
        if (
            _candidate_skill_score("k8s.resource_pressure", payload)
            and skill_id == "k8s.resource_pressure"
            and _resource_candidate_should_cross_domain(payload)
        ):
            return 150
        if skill_id == "llm.k8s_vllm_observability":
            return 130
        if skill_id == "k8s.resource_pressure":
            return 80
    if skill_id == "llm.k8s_vllm_observability":
        has_workload_object = any(payload.get(key) for key in ("deployment", "service", "pod"))
        if not has_workload_object and any(token in raw for token in ("endpoint_timeout", "http_503", "grpc_failure")):
            return 0
    aliases = skill_aliases(skill_id)
    if aliases and any(token in raw for token in aliases):
        return 100
    return 0


def _infer_domain(payload: Dict[str, Any], explicit: str = "") -> str:
    if explicit:
        return explicit
    return classify_domain(payload=payload)


def _runtime_payload_without_upstream_confidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Prevent an upstream diagnosis score from becoming self-confirming input.

    Skills may reuse the upstream fault type, root cause, and component as a
    hypothesis, but must derive their own raw confidence before workflow-level
    calibration.
    """
    sanitized = dict(payload or {})
    operation = str(sanitized.get("operation") or sanitized.get("intent") or "").strip().lower()
    if (
        operation in {
            "transactional_self_healing",
            "execute_undo",
            "agenticsre.k8s.remediation.plan_undo",
        }
        and str(sanitized.get("authorization_basis") or "").strip()
        == "approved_fault_transaction_undo"
    ):
        # This recovery-only contract consumes an already calibrated diagnosis
        # and validates the exact approved Undo. It does not produce a new RCA
        # score, so retaining the gate input is necessary and non-circular.
        return sanitized
    diagnosis = sanitized.get("diagnosis")
    if isinstance(diagnosis, dict):
        diagnosis = dict(diagnosis)
        for key in ("confidence", "confidence_raw", "confidence_explanation"):
            diagnosis.pop(key, None)
        sanitized["diagnosis"] = diagnosis
    return sanitized


class SkillWorkflow:
    """Compose matching skills into one diagnosis/healing workflow result."""

    def __init__(self, executor: SkillExecutor):
        self.executor = executor

    def run(
        self,
        payload: Dict[str, Any],
        *,
        domain: str = "",
        stages: Optional[Iterable[str]] = None,
        dry_run: bool = True,
        allow_legacy_fallback: bool = True,
        tool_runner: Optional[ToolRunner] = None,
    ) -> Dict[str, Any]:
        selected_domain = _infer_domain(payload, domain)
        runtime_payload = _runtime_payload_without_upstream_confidence(payload)
        skill_result = self.executor.run_matching(
            runtime_payload,
            stages=stages,
            domain=selected_domain,
            dry_run=dry_run,
            tool_runner=tool_runner,
        )
        runs = list(skill_result.get("runs") or [])
        failed_runs = list(skill_result.get("failed_runs") or [])
        primary = self._select_primary(runs, payload)
        skill_suggestions = self._collect_skill_suggestions(runs, primary=primary)
        legacy_result = self._legacy_plan(payload) if allow_legacy_fallback and not failed_runs else {"status": "disabled"}

        use_legacy = False
        if not skill_suggestions and legacy_result.get("status") == "ok":
            use_legacy = True
        elif primary is None and legacy_result.get("status") == "ok":
            use_legacy = True

        suggestions = skill_suggestions if not use_legacy else self._legacy_suggestions(legacy_result)
        checks = self._collect_checks(runs)
        if not checks and use_legacy:
            checks = self._legacy_checks(legacy_result)

        diagnosis = self._diagnosis(primary, payload, legacy_result if use_legacy else None)
        capability = self._capability(primary, suggestions, diagnosis, use_legacy=use_legacy)
        if failed_runs:
            capability.update(heal_ready=False, auto_execution_eligible=False, requires_approval=True)
            capability.setdefault("heal_blockers", []).append("skill_execution_failed")
            capability["auto_execution_gate"] = {"ready": False, "blockers": ["skill_execution_failed"]}
        execution_plan = self._execution_plan(suggestions, dry_run=dry_run)
        verification_plan = {
            "status": "ready" if checks else "missing",
            "checks": checks,
            "source": "skill_runtime" if checks and not use_legacy else ("legacy_fallback" if checks else "none"),
        }
        migration = self._migration(
            runs=runs,
            primary=primary,
            suggestions=suggestions,
            legacy_result=legacy_result,
            use_legacy=use_legacy,
        )
        return {
            "status": ("partial" if primary or suggestions else "failed") if failed_runs else ("completed" if primary or suggestions else "no_match"),
            "requires_manual_review": bool(failed_runs),
            "domain": selected_domain or "generic",
            "dry_run": dry_run,
            "diagnosis": diagnosis,
            "capability": capability,
            "execution_plan": execution_plan,
            "heal_compat_payload": self._heal_compat_payload(
                payload,
                diagnosis,
                suggestions,
                execution_plan,
                dry_run=dry_run,
            ),
            "verification_plan": verification_plan,
            "migration": migration,
            "skill_runtime": {
                "matched_count": len(runs),
                "primary_skill_id": _skill_id(primary) if primary else "",
                "runs": runs,
                "failed_runs": failed_runs,
            },
            "legacy": legacy_result,
            "trace": self._trace(runs, primary, use_legacy),
        }

    def _select_primary(self, runs: List[Dict[str, Any]], payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not runs:
            return None
        payload = payload or {}
        operation = str(payload.get("operation") or payload.get("intent") or "").lower()
        explicit_primary = {
            "fault_injection": "k8s.guarded_fault_injection",
            "inject": "k8s.guarded_fault_injection",
            "chaos": "k8s.guarded_fault_injection",
            "chaos_experiment": "k8s.guarded_fault_injection",
            "transactional_self_healing": "k8s.transactional_self_healing",
            "execute_undo": "k8s.transactional_self_healing",
            "agenticsre.k8s.remediation.plan_undo": "k8s.transactional_self_healing",
        }.get(operation)
        if explicit_primary:
            selected = next((run for run in runs if _skill_id(run) == explicit_primary), None)
            if selected:
                return selected

        def score(run: Dict[str, Any]) -> tuple:
            policy = run.get("policy") or {}
            suggestions = _suggestions(run)
            diagnose_ok = _stage(run, "diagnose").get("status") == "ok"
            helper = _is_helper_run(run)
            return (
                0 if helper else 1,
                _explicit_fault_affinity(run, payload),
                _payload_affinity(run, payload),
                _skill_specificity_rank(run),
                1 if suggestions else 0,
                _diagnosis_quality(run),
                1 if policy.get("heal_ready") else 0,
                1 if diagnose_ok else 0,
                _confidence(run),
                -_risk_rank((run.get("skill") or {}).get("risk") or "medium"),
            )

        return sorted(runs, key=score, reverse=True)[0]

    def _collect_skill_suggestions(
        self,
        runs: List[Dict[str, Any]],
        *,
        primary: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        seen = set()
        primary_id = _skill_id(primary) if primary else ""
        primary_has_suggestions = bool(primary and _suggestions(primary))
        primary_only = bool(primary_id and primary_has_suggestions and not _is_bridge_run(primary))
        sorted_runs = sorted(
            runs,
            key=lambda item: (
                1 if _skill_id(item) == primary_id else 0,
                0 if _is_bridge_run(item) else 1,
                0 if _is_tool_run(item) else 1,
                _confidence(item),
            ),
            reverse=True,
        )
        for run in sorted_runs:
            if primary_id and not _is_bridge_run(primary) and _is_bridge_run(run):
                continue
            if primary_only and _skill_id(run) != primary_id:
                continue
            skill = run.get("skill") or {}
            policy = run.get("policy") or {}
            for item in _suggestions(run):
                action_key = _action_key(item)
                if not _has_action(item) or action_key in seen:
                    continue
                seen.add(action_key)
                explicit_approval = item.get("requires_approval")
                if explicit_approval is None:
                    explicit_approval = bool(
                        skill.get("requires_approval", policy.get("requires_approval", True))
                        and _is_mutating(item)
                    )
                reuses_approval = _reuses_approved_transaction_undo(item)
                selected.append({
                    **item,
                    "source": item.get("source") or "skill_runtime",
                    "skill_id": skill.get("id"),
                    "skill_name": skill.get("name"),
                    "requires_approval": False if reuses_approval else bool(explicit_approval or _is_mutating(item)),
                    "heal_ready": bool(policy.get("heal_ready")),
                    "auto_heal_ready": bool(policy.get("auto_heal_ready")),
                    "executable": True,
                })
        return selected

    def _collect_checks(self, runs: List[Dict[str, Any]]) -> List[str]:
        checks: List[str] = []
        seen = set()
        for run in sorted(runs, key=lambda item: _confidence(item), reverse=True):
            for check in _checks(run):
                if check not in seen:
                    seen.add(check)
                    checks.append(check)
        return checks

    def _legacy_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        legacy = self.executor.adapters.get("legacy")
        if not legacy:
            return {"status": "unavailable", "error": "legacy adapter is not configured"}
        try:
            fault_text = " ".join(str(payload.get(key, "")) for key in ("fault_type", "message", "root_cause", "diagnosis"))
            heal = legacy.build_heal_suggestions(payload)
            return {
                "status": "ok",
                "fault_type": legacy.normalize_heal_fault_type(fault_text),
                "heal": heal,
            }
        except Exception as exc:  # pragma: no cover - API isolation
            return {"status": "error", "error": str(exc)}

    def _legacy_suggestions(self, legacy_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        heal = legacy_result.get("heal") if isinstance(legacy_result.get("heal"), dict) else {}
        selected = []
        for item in heal.get("suggestions") or []:
            if isinstance(item, dict) and item.get("command"):
                selected.append({
                    **item,
                    "source": item.get("source") or "legacy_fallback",
                    "skill_id": "",
                    "skill_name": "",
                    "requires_approval": bool((heal.get("recipe") or {}).get("requires_approval", True) or _is_mutating(item)),
                    "heal_ready": True,
                    "auto_heal_ready": False,
                    "executable": True,
                })
        return selected

    def _legacy_checks(self, legacy_result: Dict[str, Any]) -> List[str]:
        heal = legacy_result.get("heal") if isinstance(legacy_result.get("heal"), dict) else {}
        plan = heal.get("verification_plan") if isinstance(heal.get("verification_plan"), dict) else {}
        checks = plan.get("checks") or []
        return [str(item) for item in checks if str(item).strip()]

    def _diagnosis(
        self,
        primary: Optional[Dict[str, Any]],
        payload: Dict[str, Any],
        legacy_result: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if primary:
            diagnose = _stage(primary, "diagnose")
            output = diagnose.get("output") or {}
            raw_confidence = float(diagnose.get("confidence") or 0.0)
            confidence_explanation = output.get("confidence_explanation") or {}
            confidence = raw_confidence
            if not confidence_explanation:
                confidence, confidence_explanation = calibrate_skill_diagnosis_confidence(
                    raw_confidence=raw_confidence,
                    diagnosis={
                        "fault_type": output.get("fault_type") or payload.get("fault_type") or "",
                        "root_cause": output.get("root_cause") or payload.get("root_cause") or diagnose.get("summary") or "",
                        "root_cause_component": output.get("root_cause_component") or payload.get("root_cause_component") or "",
                        "evidence_summary": (payload.get("diagnosis") or {}).get("evidence_summary")
                        if isinstance(payload.get("diagnosis"), dict)
                        else {},
                    },
                    evidence=_diagnostic_evidence(primary),
                    payload=payload,
                    skill_id=_skill_id(primary),
                )
            confidence_explanation = dict(confidence_explanation)
            upstream_diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
            upstream_confidence = upstream_diagnosis.get("confidence")
            if upstream_confidence is not None:
                confidence_explanation["upstream_confidence_ignored"] = _float_or_zero(upstream_confidence)
            evidence = _diagnostic_evidence(primary)
            diagnosis = {
                "status": diagnose.get("status") or "unknown",
                "source": "skill_runtime",
                "skill_id": _skill_id(primary),
                "fault_type": output.get("fault_type") or payload.get("fault_type") or "",
                "root_cause": output.get("root_cause") or payload.get("root_cause") or diagnose.get("summary") or "",
                "root_cause_component": output.get("root_cause_component") or payload.get("root_cause_component") or "",
                "confidence": confidence,
                "summary": diagnose.get("summary") or "",
                "evidence": evidence,
                "confidence_explanation": confidence_explanation,
            }
            for key in (
                "external_method",
                "evaluation_gate",
                "expected_evidence",
                "observed_metrics",
                "remediation_policy",
            ):
                if key in output:
                    diagnosis[key] = output[key]
            return diagnosis
        if legacy_result:
            heal = legacy_result.get("heal") if isinstance(legacy_result.get("heal"), dict) else {}
            return {
                "status": "ok",
                "source": "legacy_fallback",
                "skill_id": "",
                "fault_type": legacy_result.get("fault_type") or heal.get("fault_type") or payload.get("fault_type") or "",
                "root_cause": payload.get("root_cause") or "",
                "root_cause_component": payload.get("root_cause_component") or "",
                "confidence": 0.0,
                "summary": "Skill Runtime 未命中可用诊断，回退 legacy 自愈知识库。",
                "evidence": [],
                "confidence_explanation": {"method": "legacy_fallback"},
            }
        return {
            "status": "missing",
            "source": "none",
            "skill_id": "",
            "fault_type": payload.get("fault_type") or "",
            "root_cause": payload.get("root_cause") or "",
            "root_cause_component": payload.get("root_cause_component") or "",
            "confidence": 0.0,
            "summary": "未找到匹配 skill 或 legacy 恢复计划。",
            "evidence": [],
            "confidence_explanation": {},
        }

    def _capability(
        self,
        primary: Optional[Dict[str, Any]],
        suggestions: List[Dict[str, Any]],
        diagnosis: Dict[str, Any],
        *,
        use_legacy: bool,
    ) -> Dict[str, Any]:
        primary_policy = (primary or {}).get("policy") or {}
        skill = (primary or {}).get("skill") or {}
        requires_approval = any(item.get("requires_approval") for item in suggestions)
        max_risk = "low"
        for item in suggestions:
            risk = str(item.get("risk") or "low").lower()
            if _risk_rank(risk) > _risk_rank(max_risk):
                max_risk = risk
        ignored_policy_blockers = {"low_confidence_for_heal", "insufficient_evidence", "heal_stage_not_declared"}
        ignored_auto_blockers = {"low_confidence_for_auto_heal", "heal_gate_not_ready"}
        blockers = [
            item for item in primary_policy.get("heal_blockers") or []
            if item not in ignored_policy_blockers
        ]
        messages = [
            item for item in primary_policy.get("heal_blocker_messages") or []
            if "诊断置信度" not in str(item)
            and "证据数" not in str(item)
            and "未声明 heal 阶段" not in str(item)
        ]
        confidence = float(diagnosis.get("confidence") or 0.0)
        evidence_count = len(diagnosis.get("evidence") or [])
        min_confidence = float(skill.get("min_confidence_for_heal") or primary_policy.get("min_confidence_for_heal") or 0.85)
        min_auto_confidence = float(skill.get("min_confidence_for_auto_heal") or primary_policy.get("min_confidence_for_auto_heal") or 0.9)
        min_evidence = int(skill.get("min_evidence_sources") or 1)
        if not use_legacy:
            if diagnosis.get("status") not in {"ok", "matched"} and "missing_diagnosis" not in blockers:
                blockers.append("missing_diagnosis")
                messages.append("skill 未产生有效诊断结果。")
            if confidence < min_confidence:
                blockers.append("low_confidence_for_heal")
                messages.append(f"校准后诊断置信度 {confidence:.0%} 低于自愈阈值 {min_confidence:.0%}。")
            if evidence_count < min_evidence:
                blockers.append("insufficient_evidence")
                messages.append(f"诊断证据数 {evidence_count} 低于 skill 要求的 {min_evidence}。")
        if primary_policy:
            messages.extend([
                item for item in primary_policy.get("heal_blocker_messages") or []
                if item not in messages
                and "诊断置信度" not in str(item)
                and "证据数" not in str(item)
                and "未声明 heal 阶段" not in str(item)
            ])
        if not suggestions:
            blockers.append("no_executable_actions")
            messages.append("没有可执行或可预演的自愈动作。")
        auto_blockers = [
            item for item in primary_policy.get("auto_heal_blockers") or []
            if item not in ignored_auto_blockers
        ]
        auto_messages = [
            item for item in primary_policy.get("auto_heal_blocker_messages") or []
            if "诊断置信度" not in str(item) and "自愈门禁未通过" not in str(item)
        ]
        if not use_legacy:
            if confidence < min_auto_confidence:
                auto_blockers.append("low_confidence_for_auto_heal")
                auto_messages.append(f"校准后诊断置信度 {confidence:.0%} 低于全自动阈值 {min_auto_confidence:.0%}。")
            if blockers:
                auto_blockers.append("heal_gate_not_ready")
                auto_messages.append("自愈门禁未通过。")
        elif use_legacy:
            auto_blockers.append("legacy_fallback")
        return {
            "available": bool(suggestions),
            "supports_heal": bool(suggestions),
            "primary_supports_heal": bool(primary_policy.get("supports_heal")),
            "source": "legacy_fallback" if use_legacy else "skill_runtime",
            "mode": "dry_run",
            "requires_approval": requires_approval,
            "requires_risk_ack": _risk_rank(max_risk) >= _risk_rank("high"),
            "supports_rollback": any(bool(item.get("rollback_command") or item.get("rollback")) for item in suggestions),
            "diagnosis_ready": bool(use_legacy or not blockers),
            "heal_ready": bool(use_legacy or not blockers) and bool(suggestions),
            "heal_blockers": blockers,
            "heal_blocker_messages": messages,
            "auto_execution_eligible": not auto_blockers,
            "auto_execution_gate": {
                "ready": not auto_blockers,
                "blockers": auto_blockers or [],
                "blocker_messages": auto_messages,
                "confidence": round(confidence, 3),
                "confidence_threshold": min_auto_confidence,
            },
            "confidence": round(confidence, 3),
            "confidence_threshold": min_confidence,
            "evidence_count": evidence_count,
            "min_evidence_sources": min_evidence,
            "max_risk": max_risk,
        }

    def _execution_plan(self, suggestions: List[Dict[str, Any]], *, dry_run: bool) -> Dict[str, Any]:
        commands = [str(item.get("command")) for item in suggestions if item.get("command")]
        operations = [
            {
                "operation": str(item.get("operation")),
                "parameters": dict(item.get("parameters") or {}),
                "target_entity_id": str(item.get("target_entity_id") or ""),
                "risk": str(item.get("risk") or "low"),
                "requires_approval": bool(item.get("requires_approval")),
                "skill_id": str(item.get("skill_id") or ""),
            }
            for item in suggestions
            if item.get("operation")
        ]
        rollback = [str(item.get("rollback_command")) for item in suggestions if item.get("rollback_command")]
        return {
            "dry_run": dry_run,
            "action_count": len(suggestions),
            "command_count": len(commands),
            "operation_count": len(operations),
            "commands": commands,
            "operations": operations,
            "rollback_commands": rollback,
            "suggestions": suggestions,
        }

    def _heal_compat_payload(
        self,
        payload: Dict[str, Any],
        diagnosis: Dict[str, Any],
        suggestions: List[Dict[str, Any]],
        execution_plan: Dict[str, Any],
        *,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """Build a payload compatible with the existing /api/heal/execute API."""
        compatible_diagnosis = dict(payload.get("diagnosis") or {})
        upstream_confidence = compatible_diagnosis.get("confidence")
        compatible_diagnosis.update({
            "fault_type": diagnosis.get("fault_type") or compatible_diagnosis.get("fault_type") or payload.get("fault_type") or "",
            "root_cause": diagnosis.get("root_cause") or compatible_diagnosis.get("root_cause") or payload.get("root_cause") or "",
            "root_cause_component": diagnosis.get("root_cause_component") or compatible_diagnosis.get("root_cause_component") or payload.get("root_cause_component") or "",
            "confidence": float(diagnosis.get("confidence") or 0.0),
            "confidence_explanation": diagnosis.get("confidence_explanation") or {},
        })
        if upstream_confidence is not None:
            compatible_diagnosis["confidence_raw_upstream"] = _float_or_zero(upstream_confidence)
        if "evidence_summary" not in compatible_diagnosis:
            compatible_diagnosis["evidence_summary"] = {
                f"skill_evidence_{idx + 1}": item
                for idx, item in enumerate(diagnosis.get("evidence") or [])
            }
        return {
            **payload,
            "source": payload.get("source") or "skill_workflow",
            "fault_type": compatible_diagnosis.get("fault_type") or payload.get("fault_type") or "",
            "root_cause": compatible_diagnosis.get("root_cause") or payload.get("root_cause") or "",
            "root_cause_component": compatible_diagnosis.get("root_cause_component") or payload.get("root_cause_component") or "",
            "diagnosis": compatible_diagnosis,
            "diagnosis_mode": "skill_workflow",
            "commands": execution_plan.get("commands") or [],
            "operations": execution_plan.get("operations") or [],
            "rollback_commands": execution_plan.get("rollback_commands") or [],
            "dry_run": dry_run,
            "edited_actions": suggestions,
        }

    def _migration(
        self,
        *,
        runs: List[Dict[str, Any]],
        primary: Optional[Dict[str, Any]],
        suggestions: List[Dict[str, Any]],
        legacy_result: Dict[str, Any],
        use_legacy: bool,
    ) -> Dict[str, Any]:
        legacy_suggestions = []
        if isinstance(legacy_result.get("heal"), dict):
            legacy_suggestions = legacy_result["heal"].get("suggestions") or []
        bridge = _is_bridge_run(primary)
        if use_legacy:
            mode = "legacy_fallback"
        elif bridge and suggestions:
            mode = "skill_bridge"
        elif primary and suggestions:
            mode = "skill_primary"
        elif primary:
            mode = "skill_diagnosis_only"
        else:
            mode = "no_match"
        coverage_gap = bridge or not primary or (primary is not None and not suggestions)
        return {
            "mode": mode,
            "skill_matched_count": len(runs),
            "primary_skill_id": _skill_id(primary) if primary else "",
            "legacy_status": legacy_result.get("status"),
            "legacy_suggestion_count": len(legacy_suggestions),
            "legacy_used": use_legacy,
            "bridge_skill_used": bridge,
            "coverage_gap": coverage_gap,
            "next_action": "将桥接场景拆成专用 skill 并做前向验证" if bridge else ("补齐或发布对应 skill" if coverage_gap else "可继续扩大真实前向验证覆盖"),
        }

    def _trace(self, runs: List[Dict[str, Any]], primary: Optional[Dict[str, Any]], use_legacy: bool) -> List[Dict[str, Any]]:
        trace = []
        for run in runs:
            trace.append({
                "skill_id": _skill_id(run),
                "selected_primary": bool(primary and _skill_id(run) == _skill_id(primary)),
                "diagnose_confidence": _confidence(run),
                "suggestion_count": len(_suggestions(run)),
                "policy": run.get("policy") or {},
            })
        if use_legacy:
            trace.append({
                "skill_id": "legacy_fallback",
                "selected_primary": False,
                "diagnose_confidence": 0.0,
                "suggestion_count": 0,
                "policy": {"auto_heal_ready": False, "requires_approval": True},
            })
        return trace
