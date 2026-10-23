from __future__ import annotations

import re
from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    parts = [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("agent_evidence_summary"),
        payload.get("pod"),
        payload.get("deployment"),
        payload.get("config_name"),
        payload.get("secret_name"),
        payload.get("config_key"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    return " ".join(str(item or "") for item in parts)


def _extract_name(patterns: List[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    lower = text.lower()
    if any(token in lower for token in (
        "metricsbindaddress",
        "metrics bind",
        "/metrics",
        "scrape failed",
        "prometheus scrape",
        "servicemonitor",
        "podmonitor",
    )):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "Prometheus 抓取配置异常应交由 k8s.prometheus_scrape_target 处理",
            "evidence": ["检测到 scrape/metricsBindAddress 观测链路信号，跳过 ConfigMissing Skill"],
        }
    has_config_context = any(token in lower for token in (
        "config_missing",
        "config missing",
        "configmissing",
        "createcontainerconfigerror",
        "configmap",
        "secret",
        "config key",
        "配置缺失",
    ))
    matched = any(token in lower for token in (
        "config_missing",
        "config missing",
        "configmissing",
        "createcontainerconfigerror",
        "missing configmap",
        "missing secret",
        "configmap missing",
        "secret missing",
        "configmap not found",
        "secret not found",
        "not found",
        "not registered",
        "non-existent config",
        "references non-existent",
        "couldn't find key",
        "could not find key",
    )) and has_config_context
    evidence: List[str] = []
    if "configmap" in lower and ("not found" in lower or "non-existent" in lower):
        evidence.append("事件显示 ConfigMap 不存在或引用了不存在的键")
    if "secret" in lower and ("not found" in lower or "non-existent" in lower):
        evidence.append("事件显示 Secret 不存在或引用了不存在的键")
    if "not registered" in lower:
        evidence.append("配置项未注册或未同步到目标命名空间")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.82 if matched else 0.0,
        "summary": "检测到 ConfigMap/Secret 配置缺失信号" if matched else "未检测到配置缺失信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    text = _text(payload)
    pod = payload.get("pod") or "unknown"
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    config_name = (
        payload.get("config_name")
        or _extract_name([r"configmap[ /\"]+([a-z0-9._-]+)", r"configmap ([a-z0-9._-]+) not found"], text)
    )
    secret_name = (
        payload.get("secret_name")
        or _extract_name([r"secret[ /\"]+([a-z0-9._-]+)", r"secret ([a-z0-9._-]+) not found"], text)
    )
    config_key = payload.get("config_key") or _extract_name([r"key[ /\"]+([a-z0-9._-]+)", r"non-existent .* key ([a-z0-9._-]+)"], text)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if pod != "unknown":
        evidence.append(f"目标 Pod: {pod}")
    if deployment != "unknown":
        evidence.append(f"目标 Deployment: {deployment}")
    if config_name:
        evidence.append(f"疑似缺失 ConfigMap: {config_name}")
    if secret_name:
        evidence.append(f"疑似缺失 Secret: {secret_name}")
    if config_key:
        evidence.append(f"疑似缺失配置键: {config_key}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.84 if len(evidence) >= 2 else 0.68)
    target = config_name or secret_name or config_key or "被引用的配置对象"
    return {
        "status": "ok",
        "confidence": min(confidence, 0.94),
        "summary": f"{pod} 引用了缺失配置 {target}，需先核对 ConfigMap/Secret 和键名，再受控重启工作负载。",
        "evidence": evidence,
        "fault_type": "ConfigMissing",
        "root_cause": diagnosis.get("root_cause") or f"{pod} 引用了缺失的 Kubernetes 配置对象或配置键: {target}",
        "root_cause_component": deployment,
        "config_name": config_name,
        "secret_name": secret_name,
        "config_key": config_key,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    config_name = payload.get("config_name") or ""
    secret_name = payload.get("secret_name") or ""
    suggestions = [
        {
            "step": "列出命名空间 ConfigMap 和 Secret",
            "command": f"kubectl get configmap,secret -n {namespace}",
            "risk": "low",
        },
        {
            "step": f"查看 {pod} 配置引用事件",
            "command": f"kubectl describe pod {pod} -n {namespace}",
            "risk": "low",
        },
        {
            "step": "导出 ConfigMap 供人工核对",
            "command": f"kubectl get configmap -n {namespace} -o yaml",
            "risk": "low",
        },
    ]
    if config_name:
        suggestions.insert(1, {
            "step": f"检查 ConfigMap {config_name}",
            "command": f"kubectl get configmap/{config_name} -n {namespace} -o yaml",
            "risk": "low",
        })
    if secret_name:
        suggestions.insert(1, {
            "step": f"检查 Secret {secret_name}",
            "command": f"kubectl get secret/{secret_name} -n {namespace} -o yaml",
            "risk": "low",
        })
    suggestions.append({
        "step": f"配置修复后重启 Deployment {deployment}",
        "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
        "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
        "risk": "medium",
    })
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "生成配置缺失恢复建议；创建或修正 ConfigMap/Secret 需要人工确认，重启动作需审批。",
        "evidence": ["包含配置只读核对和可回滚 rollout 动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    pod = payload.get("pod") or "<pod>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证配置对象存在、Pod 事件不再报配置缺失、Deployment rollout 完成。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get configmap,secret -n {namespace}",
            f"kubectl describe pod {pod} -n {namespace}",
            f"kubectl rollout status deployment/{deployment} -n {namespace}",
        ],
    }
