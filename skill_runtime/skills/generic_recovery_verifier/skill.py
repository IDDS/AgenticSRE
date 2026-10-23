from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or ""
    service = payload.get("service") or ""
    endpoint = payload.get("endpoint") or payload.get("vllm_endpoint") or ""
    checks: List[str] = [f"kubectl get pods,svc,endpoints -n {namespace} -o wide"]
    if deployment:
        checks.append(f"kubectl rollout status deployment/{deployment} -n {namespace}")
    if service:
        checks.append(f"kubectl get endpoints {service} -n {namespace} -o wide")
    if endpoint:
        checks.append(f"curl -fsS {endpoint}")
    checks.append("confirm error rate, latency, and saturation return to baseline")
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "验证服务对象、健康端点和 SLA 指标是否恢复。",
        "evidence": ["恢复验证计划已生成"],
        "checks": checks,
    }
