from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    parts = [
        payload.get("fault_type"), payload.get("message"), payload.get("pod"), payload.get("deployment"),
        diagnosis.get("fault_type"), diagnosis.get("root_cause"), raw.get("reason"), raw.get("message"),
    ]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend([item.get("source"), item.get("signal"), item.get("value"), item.get("message")])
        elif item:
            parts.append(str(item))
    return " ".join(str(x or "") for x in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    import json
    import re
    payload = _payload(context)
    pod = str(payload.get("pod") or "")
    namespace = str(payload.get("namespace") or "default")
    tools = context.get("tools")
    rows = []
    observed = False
    if tools and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", pod):
        # One API snapshot binds the status to the actual object incarnation.
        # Project only identity and container status, never env or annotations.
        projection = r'{.metadata.name}{"\n"}{.metadata.namespace}{"\n"}{.metadata.uid}{"\n"}{.metadata.resourceVersion}{"\n"}{.spec.nodeName}{"\n"}{.status.containerStatuses}'
        result = tools.execute("kubectl", command=f"get pod/{pod} -o jsonpath='{projection}'", namespace=namespace)
        if result.get("executed") and result.get("success"):
            try:
                raw = result.get("data") or result.get("sample") or "[]"
                if isinstance(raw, str) and len(raw.split("\n", 5)) == 6:
                    name, ns, uid, version, node, raw = raw.split("\n", 5)
                    if name != pod or ns != namespace or not uid or not version:
                        raise ValueError("Pod snapshot identity differs from target")
                rows = json.loads(raw) if isinstance(raw, str) else raw
                observed = isinstance(rows, list)
            except (ValueError, TypeError):
                rows = []
    matches = [row for row in rows if isinstance(row, dict) and
               row.get("state", {}).get("waiting", {}).get("reason") in {"ErrImagePull", "ImagePullBackOff"}]
    # Alert text is only a routing hint; live evidence must establish the fault.
    matched = bool(matches)
    return {"status": "matched" if matched else "skipped", "confidence": 0.86 if matched else 0.0,
        "summary": "已观测目标容器镜像拉取失败" if matched else "未取得当前镜像拉取失败证据",
        "evidence": [str(row.get("state", {}).get("waiting", {})) for row in matches],
        "observed": observed, "containers": matches, "pod": pod, "namespace": namespace}


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    detection = context.get("detect") or {}
    details = detection.get("output") or {}
    containers = details.get("containers") or []
    pod = details.get("pod") or payload.get("pod") or ""
    if not details.get("observed") or not containers:
        return {"status": "skipped", "confidence": 0.0, "summary": "缺少当前目标的镜像拉取证据", "evidence": []}
    evidence = []
    causes = []
    for row in containers:
        message = str(row.get("state", {}).get("waiting", {}).get("message") or "")
        image = str(row.get("image") or "")
        text = message.casefold()
        if any(token in text for token in ("not found", "manifest unknown", "name unknown")):
            cause = "镜像仓库返回镜像引用不存在"
        elif any(token in text for token in ("unauthorized", "authentication required", "pull access denied", "forbidden")):
            cause = "镜像仓库拒绝拉取访问；具体认证或授权原因待核对"
        elif any(token in text for token in ("timeout", "connection refused", "no such host", "network is unreachable")):
            cause = "镜像仓库连接失败；网络或服务端原因待核对"
        else:
            cause = "镜像拉取失败，现有错误尚不能确定具体原因"
        evidence.append(f"Pod/{pod} 容器/{row.get('name')} 镜像 {image}: {message}")
        causes.append(f"Pod/{pod} 容器/{row.get('name')}: {cause}（{image}）")
    return {"status": "ok", "confidence": 0.85,
        "summary": "；".join(causes), "evidence": evidence, "fault_type": "ImagePullError",
        "root_cause": "；".join(causes), "root_cause_component": pod,
        "observations": containers, "observation_source": "kubectl", "requires_more_evidence": False}


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    pod = payload.get("pod") or "<pod>"
    deployment = payload.get("deployment") or "<deployment>"
    container = payload.get("container") or payload.get("container_name") or "nginx"
    current_image = payload.get("current_image") or payload.get("bad_image") or ""
    target_image = payload.get("expected_image") or payload.get("last_good_image") or ""
    suggestions = [
        {"step": f"查看 {pod} 镜像拉取事件", "command": f"kubectl describe pod {pod} -n {ns}", "risk": "low"},
        {"step": "检查 imagePullSecret", "command": f"kubectl get secret -n {ns}", "risk": "low"},
    ]
    if target_image:
        suggestions.append(
            {
                "step": f"将 Deployment {deployment} 的 {container} 容器恢复到已验证镜像",
                "command": f"kubectl set image deployment/{deployment} {container}={target_image} -n {ns}",
                "rollback_command": f"kubectl set image deployment/{deployment} {container}={current_image} -n {ns}" if current_image else "",
                "risk": "medium",
            }
        )
    else:
        suggestions.append(
            {
                "step": f"重启 Deployment {deployment} 重新拉取镜像",
                "command": f"kubectl rollout restart deployment/{deployment} -n {ns}",
                "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {ns}",
                "risk": "medium",
            }
        )
    return {
        "status": "ok",
        "confidence": 0.84,
        "summary": "生成镜像拉取失败恢复建议；提供已验证镜像时可受控修复 image tag。",
        "evidence": ["包含只读事件检查和可回滚 rollout 动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    ns = _payload(context).get("namespace") or "default"
    deployment = _payload(context).get("deployment") or "<deployment>"
    return {
        "status": "ok",
        "confidence": 0.8,
        "summary": "验证 Pod 是否脱离 ImagePullBackOff，Deployment rollout 是否完成。",
        "evidence": ["验证计划已生成"],
        "checks": [f"kubectl get pods -n {ns}", f"kubectl rollout status deployment/{deployment} -n {ns}"],
    }
