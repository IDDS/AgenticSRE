from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "model", "repository", "object_store", "error")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _status(payload: Dict[str, Any]) -> int:
    try:
        return int(payload.get("http_status") or payload.get("status_code") or 0)
    except (TypeError, ValueError):
        return 0


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    status = _status(payload)
    matched = any(token in text for token in (
        "model repository", "object storage", "huggingface token", "hf token",
        "model cache", "checksum mismatch", "shard download", "download timeout",
        "模型仓库", "对象存储", "分片下载",
    )) or status in {401, 403, 404, 408, 500, 503} and any(token in text for token in ("model", "repo", "bucket", "cache"))
    evidence: List[str] = []
    if status:
        evidence.append(f"模型仓库 HTTP 状态码={status}")
    if payload.get("model"):
        evidence.append(f"模型={payload.get('model')}")
    if payload.get("repository"):
        evidence.append(f"仓库={payload.get('repository')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到模型仓库/对象存储访问异常" if matched else "未检测到模型仓库访问异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    status = _status(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if status in {401, 403} or "token" in text or "permission" in text:
        fault_type = "LLM::model_repository_auth_failed"
        root_cause = "模型仓库或对象存储访问凭据无效，vLLM 无法下载模型资产"
    elif status == 404 or "not found" in text:
        fault_type = "LLM::model_artifact_not_found"
        root_cause = "目标模型、分片或对象存储路径不存在"
    elif "checksum" in text or "corrupt" in text:
        fault_type = "LLM::model_cache_or_checksum_corrupted"
        root_cause = "模型 cache 或下载分片损坏，checksum 校验失败"
    elif "timeout" in text or status in {408, 500, 503}:
        fault_type = "LLM::model_repository_download_timeout"
        root_cause = "模型仓库/对象存储下载超时或服务不可用，冷启动和扩容受阻"
    else:
        fault_type = "LLM::model_repository_access_degraded"
        root_cause = "模型供应链访问退化，需要核对仓库、凭据、cache 和网络"
    return {
        "status": "ok",
        "confidence": min(0.92, 0.68 + 0.05 * len(evidence) + (0.06 if status else 0.0)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("model") or payload.get("repository") or "model-repository",
        "observed": {"http_status": status, "cache_path": payload.get("cache_path")},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    secret = payload.get("secret") or "<model-secret>"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "生成模型仓库访问恢复建议；清 cache 或换 Secret 需审批。",
        "evidence": ["先验证凭据和对象存在，再处理 cache"],
        "suggestions": [
            {"step": "查看模型 Secret", "command": f"kubectl -n {ns} get secret {secret} -o yaml", "risk": "low"},
            {"step": "查看 vLLM/init 日志", "command": f"kubectl -n {ns} logs deploy/vllm-server --tail=200 | egrep -i 'model|download|cache|token|checksum|s3|hf'", "risk": "low"},
            {"step": "审批后重建模型 cache", "command": "在低流量窗口清理损坏 cache 并重新预热模型分片", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证模型资产完整性、加载成功和推理请求恢复。",
        "evidence": ["模型仓库访问验证计划"],
        "checks": [
            "模型分片存在且 checksum 正确",
            "/v1/models 返回目标模型",
            "冷启动加载时间和短请求延迟恢复",
        ],
    }
