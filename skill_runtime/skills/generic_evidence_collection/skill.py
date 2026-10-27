from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List
from skill_runtime.kubectl_safety import readonly_kubectl_args
from skill_runtime.service_evidence import collect_service_evidence, MAX_TOOL_CALLS


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _namespace(payload: Dict[str, Any]) -> str:
    return str(payload.get("namespace") or "default")


def _target_commands(payload: Dict[str, Any]) -> List[str]:
    namespace = _namespace(payload)
    service = payload.get("service") or ""
    deployment = payload.get("deployment") or ""
    pod = payload.get("pod") or ""
    supplied = payload.get("investigation_commands")
    if isinstance(supplied, list) and supplied:
        return ["kubectl " + command.removeprefix("kubectl ") for command in supplied[:4] if isinstance(command, str)]
    commands = [f"kubectl get pods -n {namespace} -o wide",
                f"kubectl get events -n {namespace} --sort-by=.lastTimestamp"]
    if pod:
        commands.extend([f"kubectl logs {pod} -n {namespace} --tail=40",
                         f"kubectl describe pod {pod} -n {namespace}"])
    elif service:
        commands.extend([f"kubectl describe svc {service} -n {namespace}",
                         f"kubectl get endpoints {service} -n {namespace} -o wide"])
    elif deployment:
        commands.append(f"kubectl describe deployment {deployment} -n {namespace}")
    return commands


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    matched = bool(payload.get("fault_type") or payload.get("message") or payload.get("diagnosis"))
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.74 if matched else 0.0,
        "summary": "生成通用证据采集计划" if matched else "缺少故障上下文，无法生成证据采集计划",
        "evidence": ["存在故障上下文"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    commands = _target_commands(payload)
    tool_results = []
    tools = context.get("tools")
    service_scope = None
    if tools and hasattr(tools, "execute") and payload.get('collect_service_topology') is True and payload.get('service'):
        tool_results, service_scope = collect_service_evidence(tools, payload)
    if tools and hasattr(tools, "execute"):
        # Preserve the Agent's additional discriminating checks (for example
        # logs/events on a healthy-looking backend) within the shared call cap.
        limit = MAX_TOOL_CALLS if service_scope is not None else 4
        # If even the Service read is unavailable (RBAC/client capability),
        # keep the previous read-only investigation available. Its scope gap
        # remains unresolved and cannot bind a root object. A known selectorless
        # Service must never trigger the default namespace-wide fallback.
        unavailable = service_scope is not None and 'service_unavailable' in service_scope.get('gaps', [])
        extra_commands = (commands if service_scope is None or payload.get('investigation_commands') or unavailable
                          else ['kubectl '+c.removeprefix('kubectl ') for c in service_scope.get('supplemental_commands', [])])
        for command in extra_commands:
            if len(tool_results) >= limit:
                break
            stripped = command.strip()
            if not stripped.startswith("kubectl "):
                continue
            kubectl_command = stripped[len("kubectl "):]
            try:
                args = readonly_kubectl_args(kubectl_command, _namespace(payload))
            except ValueError:
                continue
            if any(a in {"-A", "--all-namespaces"} or a.startswith("--all-namespaces=") for a in args):
                continue
            if any(a.startswith(("--raw", "--filename", "--kustomize")) or a in {"-f", "-k"} for a in args):
                continue
            if any(resource.split("/")[0].split(".")[0].casefold() in {"secret", "secrets"}
                   for arg in args for resource in arg.split(",")):
                continue
            # Structured consumers validate object identity and endpoint state
            # from JSON. Model-selected YAML is valid kubectl syntax, but used
            # to silently bypass those predicates. Normalize only get YAML;
            # record and execute the resulting command, preserving scope guards.
            if args[0] == 'get':
                converted = False
                for index, arg in enumerate(args):
                    if arg in {'-o', '--output'} and index + 1 < len(args) and args[index + 1] in {'yaml', 'yml'}:
                        args[index + 1] = 'json'
                        converted = True
                    elif arg in {'--output=yaml', '--output=yml'}:
                        args[index] = '--output=json'
                        converted = True
                if converted:
                    kubectl_command = shlex.join(args)
            verb = kubectl_command.split()[0] if kubectl_command.split() else ""
            if verb not in {"get", "describe", "logs", "top", "events", "cluster-info", "version"}:
                continue
            tool_results.append(tools.execute("kubectl", command=kubectl_command, namespace=_namespace(payload)))
            if len(tool_results) >= limit:
                break
        # Model-proposed names/selectors are hypotheses too. Empty selections do
        # not establish that a workload is absent: discover actual Pod names and
        # read a bounded set of current logs before giving up.
        def useful(result):
            sample = str(result.get("data") or result.get("sample") or "").strip()
            return result.get("executed") and result.get("success") and sample and not sample.startswith("No resources found")

        if service_scope is None and tool_results and not any(useful(result) for result in tool_results):
            discovery = "get pods -o jsonpath='{.items[*].metadata.name}'"
            inventory = tools.execute("kubectl", command=discovery, namespace=_namespace(payload))
            tool_results.append(inventory)
            if useful(inventory):
                names = [name for name in str(inventory.get("data") or inventory.get("sample") or "").split()
                         if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)]
                target = str(payload.get("pod") or "")
                names = sorted(set(names), key=lambda name: (name != target, name))
                for name in names[:3]:
                    tool_results.append(tools.execute("kubectl", command=f"logs {name} --tail=40", namespace=_namespace(payload)))
    evidence = ["证据采集命令已生成"]
    executed = [item for item in tool_results if item.get("executed") and item.get("success")
                and str(item.get("data") or item.get("sample") or "").strip()
                and not str(item.get("data") or item.get("sample") or "").startswith("No resources found")]
    if executed:
        evidence.append(f"已执行 {len(executed)} 个只读证据采集工具调用")
    return {
        "status": "ok",
        "confidence": 0.76 if executed else 0.68,
        "summary": f"需要采集 {len(commands)} 类证据，再进入根因诊断。" if not executed else f"已采集 {len(executed)} 个只读证据样本，仍需结合完整证据诊断。",
        "evidence": evidence,
        "fault_type": payload.get("fault_type") or "EvidenceCollection",
        "root_cause": "RCA 前证据不足，需要先采集多源证据",
        "root_cause_component": payload.get("deployment") or payload.get("service") or _namespace(payload),
        "commands": commands,
        "tool_results": tool_results,
        **({'service_scope': service_scope} if service_scope is not None else {}),
    }
