from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "service", "root_cause", "symptom")
    return " ".join(str(payload.get(key, "")) for key in keys).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    tokens = (
        "dns", "coredns", "kube-dns", "name resolution", "servfail", "nxdomain",
        "cannot resolve", "resolve", "lookup failure", "lookup", "解析",
    )
    matched = any(token in text for token in tokens)
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 DNS/CoreDNS 解析异常信号" if matched else "未检测到 DNS 解析异常信号",
        "evidence": ["incident mentions DNS/CoreDNS resolution failure"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    evidence.append(f"业务命名空间: {namespace}")
    return {
        "status": "ok",
        "confidence": 0.86,
        "summary": "疑似集群 DNS 解析链路异常，需检查 CoreDNS Pod、kube-dns Endpoints、CoreDNS 日志和上游解析。",
        "evidence": evidence,
        "fault_type": "DNSResolutionFailure",
        "root_cause": "cluster dns resolution degraded",
        "root_cause_component": "coredns",
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 DNS 只读排查和受控 CoreDNS 重启建议；重启必须先审批。",
        "evidence": ["CoreDNS rollout restart includes Kubernetes rollback visibility"],
        "suggestions": [
            {
                "step": "查看 kube-dns Endpoints",
                "command": "kubectl get endpoints kube-dns -n kube-system -o wide",
                "risk": "low",
            },
            {
                "step": "查看 CoreDNS Pod 状态",
                "command": "kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide",
                "risk": "low",
            },
            {
                "step": "查看 CoreDNS 最近日志",
                "command": "kubectl logs -n kube-system -l k8s-app=kube-dns --tail=120",
                "risk": "low",
            },
            {
                "step": "从业务命名空间发起 DNS 探测",
                "command": f"kubectl run dns-check -n {namespace} --rm -it --restart=Never --image=busybox:1.36 -- nslookup kubernetes.default",
                "risk": "low",
            },
            {
                "step": "受控重启 CoreDNS",
                "command": "kubectl rollout restart deployment/coredns -n kube-system",
                "rollback_command": "kubectl rollout undo deployment/coredns -n kube-system",
                "risk": "medium",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _payload(context).get("namespace") or "default"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "验证 CoreDNS rollout、kube-dns Endpoints 和业务命名空间 DNS 探测。",
        "evidence": ["验证计划已生成"],
        "checks": [
            "kubectl rollout status deployment/coredns -n kube-system",
            "kubectl get endpoints kube-dns -n kube-system -o wide",
            f"kubectl run dns-check -n {namespace} --rm -it --restart=Never --image=busybox:1.36 -- nslookup kubernetes.default",
        ],
    }
