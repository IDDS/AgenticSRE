from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "message", "error", "secret", "host", "tls", "certificate")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched = any(token in text for token in (
        "tls", "certificate", "cert", "x509", "handshake", "unknown authority",
        "not yet valid", "expired", "sni", "证书", "握手",
    ))
    evidence: List[str] = []
    if "expired" in text or "过期" in text:
        evidence.append("证书过期信号")
    if "x509" in text or "unknown authority" in text:
        evidence.append("x509/CA 信任异常")
    if payload.get("host"):
        evidence.append(f"Host={payload.get('host')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.88 if matched else 0.0,
        "summary": "检测到 TLS 证书或握手异常" if matched else "未检测到 TLS 异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if "expired" in text or "过期" in text:
        fault_type = "K8S::tls_certificate_expired"
        root_cause = "入口或服务证书已过期，客户端 TLS 握手失败"
    elif "unknown authority" in text or "ca" in text:
        fault_type = "K8S::tls_ca_trust_error"
        root_cause = "客户端或网关不信任当前证书链，CA 配置不匹配"
    elif "sni" in text or "hostname" in text or "san" in text:
        fault_type = "K8S::tls_sni_or_san_mismatch"
        root_cause = "证书 SAN/SNI/Host 不匹配，入口 TLS 握手被拒绝"
    else:
        fault_type = "K8S::tls_handshake_failure"
        root_cause = "TLS Secret、证书链或握手参数异常"
    return {
        "status": "ok",
        "confidence": min(0.93, 0.72 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("secret") or payload.get("ingress") or payload.get("host") or "tls-secret",
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    secret = payload.get("secret") or "<tls-secret>"
    host = payload.get("host") or "<host>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 TLS 修复建议；替换证书或 Secret 需审批。",
        "evidence": ["证书变更必须保留旧 Secret 作为回滚"],
        "suggestions": [
            {"step": "查看 TLS Secret", "command": f"kubectl -n {ns} get secret {secret} -o yaml", "risk": "low"},
            {"step": "检查入口 TLS 配置", "command": f"kubectl -n {ns} get ingress,gateway -o yaml | grep -A8 -B4 {secret}", "risk": "low"},
            {"step": "审批后更新证书 Secret", "command": f"kubectl -n {ns} create secret tls {secret} --cert=<cert.pem> --key=<key.pem> --dry-run=client -o yaml | kubectl apply -f -", "risk": "high"},
            {"step": "外部握手验证", "command": f"openssl s_client -connect {host}:443 -servername {host} -showcerts </dev/null", "risk": "low"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    host = _payload(context).get("host") or "<host>"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 TLS 握手、证书有效期和入口 HTTPS 探测。",
        "evidence": ["TLS 恢复验证计划"],
        "checks": [
            f"openssl s_client -connect {host}:443 -servername {host} -showcerts </dev/null",
            f"curl -vkI https://{host}/",
            "确认证书 NotAfter、SAN 和 issuer 正确",
        ],
    }
