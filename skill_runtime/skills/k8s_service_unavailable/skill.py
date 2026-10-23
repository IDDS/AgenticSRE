from __future__ import annotations

import json
import shlex
from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    fields = [
        payload.get("fault_type"),
        payload.get("raw_fault_type"),
        payload.get("alert_name"),
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("object"),
        payload.get("observed_evidence_summary"),
    ]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            fields.extend([item.get("source"), item.get("signal"), item.get("value"), item.get("message"), item.get("summary")])
        elif item:
            fields.append(str(item))
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    fields.extend([labels.get("alertname"), labels.get("job"), labels.get("instance")])
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    fields.extend([diagnosis.get("fault_type"), diagnosis.get("root_cause"), diagnosis.get("remediation_suggestion")])
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    fields.extend([alert.get("title"), alert.get("description"), alert.get("service")])
    evidence = payload.get("agent_evidence") if isinstance(payload.get("agent_evidence"), dict) else {}
    collection = evidence.get("agent_evidence_collection") if isinstance(evidence.get("agent_evidence_collection"), dict) else {}
    for item in collection.get("commands") or []:
        if isinstance(item, dict):
            fields.extend([item.get("command"), item.get("summary"), item.get("sample")])
    return " ".join(str(item or "") for item in fields).lower()


def _primary_text(payload: Dict[str, Any]) -> str:
    """Text that belongs to the current alert/target, excluding broad namespace scans."""
    fields = [
        payload.get("fault_type"),
        payload.get("raw_fault_type"),
        payload.get("alert_name"),
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("object"),
    ]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            fields.extend([item.get("source"), item.get("signal"), item.get("value"), item.get("message"), item.get("summary")])
        elif item:
            fields.append(str(item))
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    fields.extend([labels.get("alertname"), labels.get("job"), labels.get("instance")])
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    fields.extend([alert.get("title"), alert.get("description"), alert.get("service")])
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    fields.extend([diagnosis.get("fault_type"), diagnosis.get("root_cause"), diagnosis.get("remediation_suggestion")])
    return " ".join(str(item or "") for item in fields).lower()


def _agent_kubectl_evidence(payload: Dict[str, Any]) -> list[str]:
    evidence = payload.get("agent_evidence") if isinstance(payload.get("agent_evidence"), dict) else {}
    collection = evidence.get("agent_evidence_collection") if isinstance(evidence.get("agent_evidence_collection"), dict) else {}
    facts: list[str] = []
    for item in collection.get("commands") or []:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        summary = str(item.get("summary") or "").strip()
        sample = str(item.get("sample") or "").strip()
        if summary:
            facts.append(summary)
        lower = f"{command}\n{summary}\n{sample}".lower()
        if "endpoints" in lower and ("<none>" in lower or "notfound" in lower or "no endpoints" in lower):
            facts.append("kubectl 证据显示 Service endpoints 为空或不可获取")
        if "notfound" in lower or "not found" in lower:
            facts.append(f"kubectl 证据显示目标对象不存在: {command}")
        if "ready" in lower and ("0/" in lower or "false" in lower):
            facts.append("kubectl 证据显示后端工作负载存在未就绪对象")
    seen = set()
    unique: list[str] = []
    for fact in facts:
        if fact and fact not in seen:
            seen.add(fact)
            unique.append(fact)
    return unique[:8]


def _confidence_from_evidence(base: float, evidence: list[str]) -> float:
    score = base
    if len(evidence) >= 2:
        score += 0.04
    if any("kubectl" in item for item in evidence):
        score += 0.04
    if any("endpoints" in item.lower() and ("为空" in item or "不可获取" in item) for item in evidence):
        score += 0.05
    if any("不存在" in item for item in evidence):
        score -= 0.08
    return max(0.0, min(score, 0.96))


def _observed_empty_target_endpoints(payload: Dict[str, Any]) -> bool:
    """Accept current, scoped Skill tool evidence even without an English alert label."""
    service = str(payload.get("service") or "")
    namespace = str(payload.get("namespace") or "default")
    cluster = str(payload.get("cluster_id") or "")
    if not service:
        return False
    commands = (payload.get("agent_evidence") or {}).get("agent_evidence_collection", {}).get("commands", [])
    for item in commands:
        if not isinstance(item, dict) or item.get("executed") is not True or item.get("success") is not True:
            continue
        if str(item.get("namespace") or "") != namespace or str(item.get("cluster_id") or "") != cluster:
            continue
        try:
            args = shlex.split(str(item.get("command") or ""))
        except ValueError:
            continue
        if args and args[0] == "kubectl":
            args = args[1:]
        targeted = args[:2] == ["get", f"endpoints/{service}"] or args[:3] == ["get", "endpoints", service]
        if not targeted:
            continue
        raw = str(item.get("sample") or item.get("data") or "")
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            meta = obj.get("metadata") or {}
            if meta.get("name") == service and meta.get("namespace") == namespace:
                return not any(subset.get("addresses") for subset in obj.get("subsets", []) if isinstance(subset, dict))
        # kubectl's default Endpoints table has NAME and ENDPOINTS columns.
        lines = [line.split() for line in raw.splitlines() if line.strip()]
        if lines and lines[0][:2] == ["NAME", "ENDPOINTS"]:
            return any(row[:2] == [service, "<none>"] for row in lines[1:])
    return False


def _is_control_plane_storage_signal(text: str) -> bool:
    return any(token in text for token in (
        "etcdinsufficientmembers",
        "insufficient members",
        "etcd quorum",
        "quorum etcd",
        "kube-etcd",
        "etcdserver",
        "kube-apiserver",
        "kubeapiserver",
        "apiserverdown",
    ))


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    primary_text = _primary_text(payload)
    if any(token in primary_text for token in (
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
            "summary": "Prometheus 抓取目标异常应交由 k8s.prometheus_scrape_target 处理",
            "evidence": ["检测到 scrape/metricsBindAddress/ServiceMonitor 观测链路信号，跳过 ServiceUnavailable Skill"],
        }
    if _is_control_plane_storage_signal(primary_text):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "控制面/etcd 告警应交由 k8s.control_plane_unhealthy 处理",
            "evidence": ["检测到 etcd/apiserver/quorum 控制面信号，跳过 ServiceUnavailable Skill"],
        }
    if any(token in primary_text for token in (
        "imagepullbackoff",
        "errimagepull",
        "image pull",
        "pull image",
        "pulling image",
        "failed to pull image",
        "back-off pulling image",
    )):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "镜像拉取异常应交由 k8s.image_pull_error 处理",
            "evidence": ["检测到 ImagePull/拉取镜像失败信号，跳过 ServiceUnavailable Skill"],
        }
    matched = any(token in primary_text for token in (
        "service_unavailable",
        "serviceunavailable",
        "service unavailable",
        "no endpoints",
        "no ready endpoints",
        "503",
        "scaled to zero",
    )) or _observed_empty_target_endpoints(payload)
    evidence = []
    if "503" in primary_text or "service unavailable" in primary_text or "service_unavailable" in primary_text:
        evidence.append("请求返回 503 或服务不可用")
    if "no endpoints" in primary_text or "no ready endpoints" in primary_text or "scaled to zero" in primary_text:
        evidence.append("服务无可用 endpoints 或 Deployment 副本为 0")
    evidence.extend(_agent_kubectl_evidence(payload))
    return {
        "status": "matched" if matched else "skipped",
        "confidence": _confidence_from_evidence(0.82, evidence) if matched else 0.0,
        "summary": "检测到 ServiceUnavailable 信号" if matched else "未检测到 ServiceUnavailable 信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    service = payload.get("service") or payload.get("object") or "unknown"
    detect_result = context.get("detect") or {}
    evidence = list(detect_result.get("evidence") or [])
    if deployment != "unknown":
        evidence.append(f"目标 Deployment: {deployment}")
    confidence = max(
        float(diagnosis.get("confidence") or 0.0),
        _confidence_from_evidence(0.84 if evidence else 0.65, evidence),
    )
    target_missing = any("不存在" in item for item in evidence)
    # HTTP 503 identifies a symptom; it does not prove absent ready backends.
    backend_evidence = any(any(marker in item for marker in (
        "无可用 endpoints", "副本为 0", "endpoints 为空", "未就绪对象")) for item in evidence)
    if not backend_evidence and not target_missing:
        return {
            "status": "ok", "confidence": 0.35,
            "summary": "已观测服务不可用；后端就绪状态和应用错误机制尚未证实。",
            "evidence": evidence, "fault_type": "ServiceUnavailable",
            "root_cause": "HTTP 服务不可用，根因待验证；需核对目标后端和应用日志。",
            "root_cause_component": payload.get("pod") or service,
            "requires_more_evidence": True,
        }
    root_cause = diagnosis.get("root_cause") or f"{deployment} 无可用就绪副本或 endpoints 为空"
    if target_missing:
        root_cause = f"{deployment} 目标对象不可用或类型不匹配，Service 后端 endpoints 异常"
    return {
        "status": "ok",
        "confidence": min(confidence, 0.96),
        "summary": f"服务 {service} 不可用，疑似由 {deployment} 无可用副本或 endpoints 为空导致。",
        "evidence": evidence,
        "fault_type": "ServiceUnavailable",
        "root_cause": root_cause,
        "root_cause_component": deployment,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    suggestions = [
        {
            "step": f"确认 {namespace} endpoints",
            "command": f"kubectl get endpoints -n {namespace}",
            "risk": "low",
        },
        {
            "step": f"恢复 Deployment {deployment} 至 1 个副本",
            "command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=1",
            "rollback_command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=0",
            "risk": "medium",
        },
    ]
    return {
        "status": "ok",
        "confidence": 0.86,
        "summary": "生成 ServiceUnavailable 自愈建议；真实执行需审批。",
        "evidence": ["scale 恢复动作带回滚命令"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "执行后验证 endpoints、pod ready 和服务 5xx 是否恢复。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl get endpoints -n {namespace}",
            f"kubectl get pods -n {namespace}",
        ],
    }
