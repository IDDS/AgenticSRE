from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    return " ".join(str(x or "") for x in [
        payload.get("fault_type"), payload.get("message"), payload.get("root_cause"),
        diagnosis.get("fault_type"), diagnosis.get("root_cause"), diagnosis.get("evidence_summary"),
    ]).lower()


def _classify(text: str) -> Dict[str, str]:
    if any(t in text for t in ("oom", "memory", "cuda out of memory", "gpu memory")):
        return {"fault_type": "ResourceExhausted", "root_cause": "内存或 GPU 显存资源耗尽"}
    if any(t in text for t in ("imagepull", "pull image", "registry")):
        return {"fault_type": "ImagePullError", "root_cause": "镜像仓库、镜像 tag 或拉取凭据异常"}
    if any(t in text for t in ("endpoint", "503", "unavailable", "no endpoints")):
        return {"fault_type": "ServiceUnavailable", "root_cause": "Service endpoint 缺失或后端就绪状态异常"}
    if any(t in text for t in ("dns", "resolve", "coredns")):
        return {"fault_type": "DNSResolution", "root_cause": "集群 DNS 解析异常"}
    if any(t in text for t in ("vllm", "llm", "gpu", "cuda", "nccl")):
        return {"fault_type": "LLMRuntime", "root_cause": "LLM 运行时、GPU 或推理端点异常"}
    if any(t in text for t in ("latency", "timeout", "p99", "slow")):
        return {"fault_type": "HighLatency", "root_cause": "请求时延或超时指标异常升高"}
    return {"fault_type": "Unknown", "root_cause": "当前证据不足，需要补充更多观测后确认根因"}


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    classified = _classify(text)
    component = payload.get("deployment") or payload.get("service") or payload.get("pod") or payload.get("node") or "unknown"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if payload.get("message"):
        evidence.append(str(payload.get("message"))[:180])
    confidence = 0.72 if classified["fault_type"] != "Unknown" else 0.45
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": f"通用 RCA 判断为 {classified['fault_type']}，建议优先匹配专用 skill 或进入人工确认。",
        "evidence": evidence,
        "fault_type": classified["fault_type"],
        "root_cause": classified["root_cause"],
        "root_cause_component": component,
    }
