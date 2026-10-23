from __future__ import annotations

from typing import Any, Dict

def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    return " ".join(str(payload.get(k, "")) for k in (
        "fault_type", "raw_fault_type", "scenario_id", "scenario_name", "message", "model", "model_path", "error"
    )).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched = any(token in text for token in (
        "model_load_failure", "model_permission_error", "tokenizer_error", "weight_corruption",
        "tokenizer", "model path", "safetensors", "permission denied", "no such file",
        "模型加载", "模型权限", "权重", "损坏",
    ))
    evidence = []
    if payload.get("model"):
        evidence.append(f"目标模型 {payload.get('model')}")
    if payload.get("model_path"):
        evidence.append(f"模型路径 {payload.get('model_path')}")
    if any(token in text for token in ("permission", "权限")):
        evidence.append("文本包含模型权限异常")
    if any(token in text for token in ("tokenizer", "分词")):
        evidence.append("文本包含 Tokenizer 异常")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到模型资产或 Tokenizer 故障信号" if matched else "未检测到模型资产故障信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    text = _text(payload)
    if any(token in text for token in ("tokenizer", "分词")):
        fault_type = "LLM::tokenizer_asset_failure"
        root = "Tokenizer 文件、配置或版本不匹配，导致请求预处理或模型加载失败"
    elif any(token in text for token in ("permission denied", "权限")):
        fault_type = "LLM::model_asset_permission_error"
        root = "模型文件或挂载目录权限异常，导致 vLLM 无法读取模型资产"
    elif any(token in text for token in ("safetensors", "weight", "权重", "corrupt", "损坏")):
        fault_type = "LLM::model_weight_corruption_or_incomplete"
        root = "模型权重文件损坏、不完整或格式不兼容，导致模型加载失败"
    else:
        fault_type = "LLM::model_asset_or_tokenizer_failure"
        root = "模型资产、Tokenizer 或权重路径异常，导致 vLLM 无法稳定加载目标模型"
    return {
        "status": "ok",
        "confidence": 0.88 if evidence else 0.78,
        "summary": root,
        "evidence": evidence or ["命中模型资产故障类型"],
        "fault_type": fault_type,
        "root_cause": root,
        "root_cause_component": payload.get("model") or payload.get("deployment") or "model-assets",
        "model": payload.get("model") or "",
        "model_path": payload.get("model_path") or "",
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    pod = payload.get("pod") or "-l app=vllm"
    model_path = payload.get("model_path") or "${MODEL_PATH}"
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成模型资产恢复建议；权限和版本变更必须审批。",
        "evidence": ["建议先查日志、挂载和模型路径，再审批恢复"],
        "suggestions": [
            {"step": "查看 vLLM 模型加载日志", "command": f"kubectl -n {ns} logs {pod} --tail=200 | egrep -i 'model|tokenizer|safetensors|permission|No such file'", "risk": "low"},
            {"step": "检查 Deployment 模型路径和挂载", "command": f"kubectl -n {ns} describe deployment/{deploy}", "risk": "low"},
            {"step": "在容器内只读检查模型目录", "command": f"kubectl -n {ns} exec deploy/{deploy} -- sh -lc 'ls -lah {model_path} | head'", "risk": "low"},
            {"step": "审批后回滚到上一模型配置或重启服务", "command": f"kubectl rollout restart deployment/{deploy} -n {ns}", "rollback_command": f"kubectl rollout undo deployment/{deploy} -n {ns}", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证模型加载日志、Pod Ready 和最小推理请求。",
        "evidence": ["模型资产恢复验证计划"],
        "checks": [
            f"kubectl -n {ns} rollout status deployment/{deploy}",
            f"kubectl -n {ns} logs deploy/{deploy} --tail=100 | egrep -i 'error|tokenizer|model|permission'",
            "执行目标模型最小推理请求并确认返回 200",
        ],
    }
