from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "node", "gpu_uuid", "scenario_name")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    metrics = _metrics(payload)
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
            if key in metrics:
                return float(metrics.get(key))
        except (TypeError, ValueError):
            pass
    return default


def _ecc_counts(payload: Dict[str, Any]) -> Dict[str, float]:
    # DBE and SBE are independent counters, not aliases for one metric.
    dbe = _num(payload, "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL")
    sbe = _num(payload, "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL")
    return {"ecc_errors": max(_num(payload, "ecc_errors"), dbe + sbe),
            "ecc_dbe_errors": dbe, "ecc_sbe_errors": sbe}


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    xid = _num(payload, "xid_errors", "DCGM_FI_DEV_XID_ERRORS")
    ecc = _ecc_counts(payload)["ecc_errors"]
    temp = _num(payload, "gpu_temperature_c", "DCGM_FI_DEV_GPU_TEMP")
    matched = any(token in text for token in (
        "gpu_ecc_error", "gpu_xid_error", "retired_pages", "thermal throttle",
        "power throttle", "gpu_clock_throttle", "pcie_throttle", "clock throttle",
        "nvlink error", "pcie replay", "xid", "ecc", "温度", "降频",
    )) or xid > 0 or ecc > 0 or temp >= 85
    evidence: List[str] = []
    if xid:
        evidence.append(f"Xid 错误计数={xid:g}")
    if ecc:
        evidence.append(f"ECC 错误计数={ecc:g}")
    if temp:
        evidence.append(f"GPU 温度={temp:g}C")
    if payload.get("gpu_uuid"):
        evidence.append(f"GPU UUID={payload.get('gpu_uuid')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.88 if matched else 0.0,
        "summary": "检测到 GPU 硬件健康异常信号" if matched else "未检测到 GPU 硬件健康异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    xid = _num(payload, "xid_errors", "DCGM_FI_DEV_XID_ERRORS")
    ecc = _ecc_counts(payload)["ecc_errors"]
    temp = _num(payload, "gpu_temperature_c", "DCGM_FI_DEV_GPU_TEMP")
    throttle = any(token in text for token in (
        "thermal throttle", "power throttle", "gpu_clock_throttle",
        "pcie_throttle", "clock throttle", "throttle", "降频",
    ))
    if xid > 0 or "xid" in text:
        fault_type = "LLM::gpu_xid_hardware_or_driver_fault"
        root_cause = "GPU Xid 错误表明硬件、驱动或运行时链路异常，可能导致推理进程失败或卡死"
    elif ecc > 0 or "ecc" in text or "retired_pages" in text:
        fault_type = "LLM::gpu_ecc_or_memory_health_fault"
        root_cause = "GPU ECC/retired pages 异常，显存可靠性风险升高，推理结果和稳定性受影响"
    elif temp >= 85 or throttle:
        fault_type = "LLM::gpu_thermal_or_power_throttle"
        root_cause = "GPU 温度或功耗策略触发降频，Decode 吞吐和尾延迟会退化"
    elif "nvlink" in text or "pcie replay" in text:
        fault_type = "LLM::gpu_interconnect_health_fault"
        root_cause = "GPU NVLink/PCIe 链路错误增加，多卡通信和 Host-Device 传输受影响"
    else:
        fault_type = "LLM::gpu_hardware_health_degraded"
        root_cause = "GPU 硬件健康异常，需要结合 DCGM、nvidia-smi 和节点日志确认"
    return {
        "status": "ok",
        "confidence": min(0.94, 0.7 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("gpu_uuid") or payload.get("node") or "gpu",
        "observed": {"xid_errors": xid, **_ecc_counts(payload), "gpu_temperature_c": temp},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    node = _payload(context).get("node") or "<gpu-node>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 GPU 硬件健康恢复建议；节点隔离和 GPU reset 需审批。",
        "evidence": ["硬件类异常优先隔离风险、保留证据、转人工维护"],
        "suggestions": [
            {"step": "查看 GPU 硬件状态", "command": "nvidia-smi -q -d ECC,TEMPERATURE,POWER,CLOCK,PAGE_RETIREMENT,PCI", "risk": "low"},
            {"step": "查看节点事件", "command": f"kubectl describe node {node} | egrep -i 'gpu|nvidia|xid|temperature|pressure|taint' || true", "risk": "low"},
            {"step": "审批后隔离异常节点", "command": f"kubectl cordon {node}", "rollback_command": f"kubectl uncordon {node}", "risk": "critical"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 GPU 错误计数、温度/功耗和 vLLM 业务指标恢复。",
        "evidence": ["GPU 硬件健康验证计划"],
        "checks": [
            "DCGM Xid/ECC/retired pages 计数不再增长",
            "GPU 温度、功耗、时钟回到正常范围",
            "vLLM 成功率、TTFT/E2E P95 恢复",
        ],
    }
