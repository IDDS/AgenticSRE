"""
AgenticSRE Event Agent
Fetches and analyzes Kubernetes events and resource descriptions.
"""

import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from agents.evidence import make_evidence_item
from agents.pod_state_evidence import MESSAGE_LIMIT, problem_containers, pod_spec_checks
from skill_runtime.service_topology import MAX_RESOURCES, project_resource

logger = logging.getLogger(__name__)


class EventAgent:
    """
    K8s event analysis agent: fetches Warning events, resource status,
    and pod conditions to identify cluster-level issues.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE event analysis expert.
Analyze the provided Kubernetes events and resource states.
Identify:
1. Warning/Error events and their targets (pods, nodes, deployments)
2. Event patterns (repeated events indicating persistent issues)
3. Resource status anomalies (NotReady nodes, CrashLoopBackOff pods)
4. Recent scheduling/scaling/health-check failures
5. Timeline of events related to the incident

Current resource state is authoritative. A Warning event for a Pod that no
longer exists is stale context and MUST NOT be used as the current root cause.
In particular, do not diagnose ImagePullBackOff or CrashLoopBackOff from an
orphaned event when the current Deployment replica state or Endpoint state
explains the incident.

Be specific about event types, involved objects, and counts. Write a concise
incident-focused summary in at most 250 Chinese characters or 150 English words.
Do not enumerate every unrelated workload or repeat the input. State missing
evidence explicitly; the complete observations remain available separately."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.summary_max_tokens = 1024
        self.summary_timeout_s = 20
        self.summarize = True

    async def analyze(self, query: str, namespace: str = "") -> Dict:
        """Run K8s event analysis for the given incident query."""
        
        # Fetch current state before events so deleted-object warnings cannot
        # override the live Deployment/Endpoint state.
        workload_state = self._fetch_workload_state(namespace)
        live_pods = self._fetch_live_pods(namespace)

        # Retain orphaned Pod warnings only as explicitly stale audit context.
        event_evidence = []
        events, stale_events = self._fetch_events(
            namespace, live_pods=live_pods, evidence_items=event_evidence)
        
        # Fetch node status
        nodes = self._fetch_node_status()
        
        # Fetch problem pods
        pods = self._fetch_problem_pods(namespace)

        # Network faults may leave pods healthy while an explicit policy or
        # ChaosMesh object blocks service traffic. Collect those objects only
        # for network-related incidents to keep normal RCA scans lightweight.
        network_objects = self._fetch_network_objects(query, namespace)
        evidence_items = self._build_current_state_evidence_items(workload_state, pods)
        evidence_items.extend(event_evidence)
        evidence_items.extend(self._build_network_evidence_items(network_objects))
        
        # Fetch resource descriptions for problem resources
        descriptions = {}
        if pods:
            for pod in [p for p in pods if not p.get('previous')][:5]:
                desc = self._describe_resource("pod", pod.get("name", ""), pod.get("namespace", namespace))
                if desc:
                    descriptions[pod["name"]] = desc[:1000]

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"K8s Events:\n{events[:3000] if events else 'No active Warning events found'}\n\n"
        context += f"Current Workload State:\n{workload_state.get('summary') or 'No workload state found'}\n\n"
        if stale_events:
            context += (
                "Stale/Orphaned Pod Warning Events (audit context only; do not use as the current root cause):\n"
                f"{stale_events[:1800]}\n\n"
            )
        context += f"Node Status:\n{nodes[:2000] if nodes else 'No node data'}\n\n"
        context += f"Problem Pods ({len(pods) if pods else 0}):\n"
        if pods:
            for pod in pods[:10]:
                context += f"  - {pod}\n"
        if descriptions:
            context += f"\nResource Descriptions:\n"
            for name, desc in descriptions.items():
                context += f"\n[{name}]:\n{desc}\n"
        if network_objects:
            context += "\nNetwork Fault Objects:\n"
            for kind, data in network_objects.items():
                context += f"\n[{kind}]:\n{data[:2000] if data else 'None found'}\n"

        summary_error = ""
        summary = ""
        if self.summarize:
            try:
                summary = await asyncio.wait_for(self.llm.async_chat([
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": context[:8000]}
                ], max_tokens=self.summary_max_tokens, thinking=False), timeout=self.summary_timeout_s)
                if not summary.strip():
                    summary_error = "empty_summary"
            except Exception as exc:
                summary_error = type(exc).__name__
        else:
            summary = f"已采集 {len(evidence_items)} 项事件观测；请使用带来源的原始证据进行推理。"
        if not summary:
            # Return collected facts even when optional prose times out.
            summary = workload_state.get("summary", "") or "事件摘要不可用；请检查结构化观测及采集错误。"

        return {
            "agent": "event_agent",
            "query": query,
            "warning_events": events,
            "stale_warning_events": stale_events,
            "problem_pods": pods,
            "node_status": nodes,
            "workload_state": workload_state,
            "network_objects": network_objects,
            "evidence_items": evidence_items,
            "collection_errors": [dict(source=source, error_code=status.get('error_code', 'topology_collection_partial'))
                for source, status in workload_state.get('topology_collection', {}).items()
                if status.get('status') in {'failed', 'partial'}],
            "summary": summary,
            "summary_error": summary_error,
            "summary_status": "not_requested" if not self.summarize else "unavailable" if summary_error else "complete",
        }

    def _fetch_events(
        self,
        namespace: str = "",
        *,
        live_pods: Optional[set[tuple[str, str]] | Dict[tuple[str, str], str]] = None,
        evidence_items: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[str, str]:
        """Fetch active warnings and separate events for deleted Pods."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return "", ""
        cmd = "get events --sort-by='.lastTimestamp' --field-selector type=Warning -o json"
        if not namespace:
            cmd += " --all-namespaces"
        result = kubectl.execute(command=cmd, namespace=namespace or "")
        if not result.success:
            return "", ""
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
        except (json.JSONDecodeError, TypeError):
            return str(result.data or ""), ""

        active: List[str] = []
        stale: List[str] = []
        for event in (data or {}).get("items", [])[-80:]:
            if not isinstance(event, dict):
                continue
            involved = event.get("involvedObject") if isinstance(event.get("involvedObject"), dict) else {}
            kind = str(involved.get("kind") or "Object")
            name = str(involved.get("name") or "unknown")
            ns = str(involved.get("namespace") or namespace or "default")
            reason = str(event.get("reason") or "Warning")
            message = str(event.get("message") or "").replace("\n", " ")[:500]
            timestamp = str(
                event.get("lastTimestamp")
                or event.get("eventTime")
                or event.get("metadata", {}).get("creationTimestamp")
                or ""
            )
            count = event.get("count") or 1
            line = f"{timestamp} {ns} {kind}/{name} {reason} count={count}: {message}".strip()
            pod_uid = str(involved.get("uid") or "")
            current_uid = live_pods.get((ns, name), "") if isinstance(live_pods, dict) else ""
            stale_pod = kind.casefold() == "pod" and live_pods is not None and (
                (ns, name) not in live_pods or (pod_uid and current_uid and pod_uid != current_uid))
            if stale_pod:
                stale.append(line)
            else:
                active.append(line)
                # A failed live lookup cannot establish that an event's Pod
                # still exists. Retain its text for audit, not as a current fact.
                if evidence_items is not None and (kind.casefold() != "pod" or live_pods is not None):
                    evidence_items.append(make_evidence_item(
                        "pod_warning_event" if kind.casefold() == "pod" else "resource_warning_event",
                        service=name, source="kubectl get events -o json", timestamp=timestamp,
                        observation=f"{kind}/{name} {reason} count={count}: {message}",
                        severity="warning", raw_ref={
                            "namespace": ns, "kind": kind, "name": name,
                            "pod_uid": pod_uid if kind.casefold() == "pod" else "",
                            "event_uid": event.get("metadata", {}).get("uid", ""),
                            "reason": reason, "count": count,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                            "message_truncated": len(str(event.get("message") or "").replace("\n", " ")) > 500,
                        }))
        return "\n".join(active), "\n".join(stale)

    def _fetch_live_pods(self, namespace: str = "") -> Optional[Dict[tuple[str, str], str]]:
        """Return current Pod identities, or None when collection failed."""
        k8s_res = self.registry.get("k8s_resources")
        if k8s_res is None:
            return None
        result = k8s_res.execute(
            action="list",
            resource_type="pods",
            namespace=namespace or "",
            output="json",
        )
        if not result.success:
            return None
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
        except (json.JSONDecodeError, TypeError):
            return None
        identities: Dict[tuple[str, str], str] = {}
        for pod in (data or {}).get("items", []):
            if not isinstance(pod, dict):
                continue
            meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
            name = str(meta.get("name") or "")
            ns = str(meta.get("namespace") or namespace or "default")
            if name:
                identities[(ns, name)] = str(meta.get("uid") or "")
        return identities

    def _fetch_workload_state(self, namespace: str = "") -> Dict[str, Any]:
        """Collect compact live Deployment, Service and Endpoint state."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return {"deployments": [], "services": [], "endpoints": [], "summary": ""}
        result = kubectl.execute(
            command="get deployment,service,endpoints -o json",
            namespace=namespace or "",
        )
        if not result.success:
            return {
                "deployments": [],
                "services": [],
                "endpoints": [],
                "summary": f"workload state collection failed: {result.error}",
            }
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
        except (json.JSONDecodeError, TypeError):
            return {
                "deployments": [],
                "services": [],
                "endpoints": [],
                "summary": str(result.data or "")[:3000],
            }

        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            return {'deployments': [], 'services': [], 'endpoints': [],
                    'summary': 'workload state collection returned no valid resource list',
                    'topology_collection': {'workload_resources': {
                        'status': 'failed', 'error_code': 'topology_collection_invalid'}}}
        state: Dict[str, Any] = {"deployments": [], "services": [], "endpoints": [],
                                "topology_resources": [], "topology_collection": {}}
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
        state['topology_collection']['workload_resources'] = {'status': 'partial' if
            metadata.get('continue') or len(data.get('items') or []) > MAX_RESOURCES else 'complete'}
        lines: List[str] = []
        for item in (data or {}).get("items", []):
            if not isinstance(item, dict):
                state['topology_collection']['workload_resources']['status'] = 'partial'
                continue
            kind = str(item.get("kind") or "")
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            name = str(meta.get("name") or "")
            ns = str(meta.get("namespace") or namespace or "default")
            projected = project_resource(item)
            if projected is None or not projected['topology_fields_complete']:
                state['topology_collection']['workload_resources']['status'] = 'partial'
            if projected and len(state['topology_resources']) < MAX_RESOURCES:
                projected['observed_at'] = datetime.now(timezone.utc).isoformat()
                projected['topology_source'] = 'kubectl get deployment,service,endpoints -o json'
                state['topology_resources'].append(projected)
            if kind == "Deployment":
                record = {
                    "kind": kind, "uid": meta.get("uid", ""),
                    "resource_version": meta.get("resourceVersion", ""),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "namespace": ns,
                    "name": name,
                    "desired_replicas": int(spec.get("replicas") or 0),
                    "ready_replicas": int(status.get("readyReplicas") or 0),
                    "available_replicas": int(status.get("availableReplicas") or 0),
                    "unavailable_replicas": int(status.get("unavailableReplicas") or 0),
                }
                state["deployments"].append(record)
                lines.append(
                    f"Deployment/{name} desired={record['desired_replicas']} "
                    f"ready={record['ready_replicas']} available={record['available_replicas']}"
                )
            elif kind == "Service":
                selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
                record = {"namespace": ns, "name": name, "selector": selector}
                state["services"].append(record)
                lines.append(f"Service/{name} selector={json.dumps(selector, ensure_ascii=False, sort_keys=True)}")
            elif kind == "Endpoints":
                ready = sum(
                    len(subset.get("addresses") or [])
                    for subset in item.get("subsets") or []
                    if isinstance(subset, dict)
                )
                not_ready = sum(
                    len(subset.get("notReadyAddresses") or [])
                    for subset in item.get("subsets") or []
                    if isinstance(subset, dict)
                )
                record = {
                    "namespace": ns,
                    "name": name,
                    "ready_addresses": ready,
                    "not_ready_addresses": not_ready,
                }
                state["endpoints"].append(record)
                lines.append(f"Endpoints/{name} ready_addresses={ready} not_ready_addresses={not_ready}")
        # Preserve the old collector when legacy responses have no identities.
        # Healthy Pods and ReplicaSets are required to verify a Service binding;
        # problem-Pod summaries alone cannot represent a healthy control.
        if any(r['kind'] == 'Service' and r['metadata'].get('uid') for r in state['topology_resources']):
            for source, command in (
                    ('pods_and_controllers', 'get pods,replicasets -o json'),
                    ('endpoint_slices', 'get endpointslices.discovery.k8s.io -o json')):
                try:
                    result = kubectl.execute(command=command, namespace=namespace or '', timeout=10)
                    payload = json.loads(result.data) if result.success and isinstance(result.data, str) else result.data
                    if not result.success or not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
                        state['topology_collection'][source] = {'status': 'failed', 'error_code': 'topology_collection_unavailable'}
                        continue
                    remaining = max(0, MAX_RESOURCES - len(state['topology_resources']))
                    partial = bool((payload.get('metadata') or {}).get('continue')) or len(payload['items']) > remaining
                    for row in payload['items'][:remaining]:
                        projected = project_resource(row)
                        if projected is None:
                            partial = True
                            continue
                        if not projected['topology_fields_complete']:
                            partial = True
                        projected['observed_at'] = datetime.now(timezone.utc).isoformat()
                        projected['topology_source'] = 'kubectl ' + command
                        state['topology_resources'].append(projected)
                    state['topology_collection'][source] = {'status': 'partial' if partial else 'complete'}
                except Exception:
                    state['topology_collection'][source] = {'status': 'failed', 'error_code': 'topology_collection_invalid'}
        state["summary"] = "\n".join(lines)
        return state

    def _build_current_state_evidence_items(
        self,
        workload_state: Dict[str, Any],
        problem_pods: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize live workload state for ranking and calibration."""
        items: List[Dict[str, Any]] = []
        for deployment in workload_state.get("deployments") or []:
            desired = int(deployment.get("desired_replicas") or 0)
            ready = int(deployment.get("ready_replicas") or 0)
            name = str(deployment.get("name") or "")
            if desired == 0:
                items.append(make_evidence_item(
                    "deployment_scaled_to_zero",
                    f"Deployment/{name} current desired replicas is 0 and ready replicas is {ready}.",
                    service=name,
                    source="kubectl get deployment -o json",
                    severity="critical",
                    raw_ref=deployment,
                ))
            elif ready < desired:
                items.append(make_evidence_item(
                    "deployment_not_ready",
                    f"Deployment/{name} ready replicas {ready}/{desired}.",
                    service=name,
                    source="kubectl get deployment -o json",
                    severity="warning",
                    raw_ref=deployment,
                ))
        for endpoint in workload_state.get("endpoints") or []:
            if int(endpoint.get("ready_addresses") or 0) == 0:
                name = str(endpoint.get("name") or "")
                items.append(make_evidence_item(
                    "service_endpoints_empty",
                    f"Endpoints/{name} has 0 ready addresses.",
                    service=name,
                    source="kubectl get endpoints -o json",
                    severity="critical",
                    raw_ref=endpoint,
                ))
        for resource in workload_state.get('topology_resources') or []:
            meta = resource['metadata']
            if not meta.get('uid'):
                continue
            items.append(make_evidence_item('resource_topology_observed',
                f"{resource['kind']}/{meta['name']} observed identity and topology projection; "
                "selectors and endpoint readiness do not prove business health or causation.",
                service=meta['name'], source=resource.get('topology_source', 'kubectl structured read'),
                timestamp=resource.get('observed_at', ''), raw_ref=resource))
        spec_seen=set()
        for pod in problem_pods:
            reason = str(pod.get("reason") or pod.get("phase") or "")
            name = str(pod.get("name") or "")
            identity=(pod.get('namespace'),name,pod.get('pod_uid'))
            checks=pod.get('spec_checks')
            if not pod.get('previous') and isinstance(checks,dict) and identity not in spec_seen:
                spec_seen.add(identity)
                items.append(make_evidence_item('pod_spec_checks',
                    f"Pod/{name} complete API spec projection: "+json.dumps(checks,ensure_ascii=False,sort_keys=True),
                    service=name,source='kubectl get pods -o json',severity='info',
                    timestamp=pod.get('observed_at',''),raw_ref={
                        'kind':'Pod','name':name,'namespace':pod.get('namespace'),
                        'pod_uid':pod.get('pod_uid'),'resource_version':pod.get('resource_version'),
                        'observed_at':pod.get('observed_at'),'spec_checks':checks}))
            selector = pod.get('node_selector')
            if pod.get('phase') == 'Pending' and not pod.get('previous') and isinstance(selector, dict) and selector:
                items.append(make_evidence_item(
                    'pod_scheduling_constraints',
                    f"Pod/{name} declares spec.nodeSelector={json.dumps(selector, ensure_ascii=False, sort_keys=True)}. "
                    "Declared configuration only; combine with current scheduler observations to assess failure.",
                    service=name, source='kubectl get pods -o json', severity='info',
                    timestamp=pod.get('observed_at', ''),
                    raw_ref={**pod, 'kind': 'Pod'},
                ))
            message = pod.get('message', '')
            detail = f" Reported message: {message[:MESSAGE_LIMIT]}" if message else ""
            if pod.get('message_truncated'):
                detail += ' [message excerpt; remaining text omitted]'
            container = f" {pod.get('container_kind', 'application')} container/{pod['container']}" if pod.get('container') else ''
            if pod.get('previous'):
                items.append(make_evidence_item(
                    'container_previous_termination',
                    f"Pod/{name}{container} previously terminated with exit code {pod['exit_code']} ({reason})"
                    f" at {pod.get('finished_at', '')}; historical observation, not current failure.{detail}",
                    service=name, source='kubectl get pods -o json', severity='info',
                    timestamp=pod.get('observed_at', ''), raw_ref=pod,
                ))
                continue
            if 'exit_code' in pod:
                items.append(make_evidence_item(
                    'container_terminated',
                    f"Pod/{name}{container} currently terminated with exit code {pod['exit_code']} ({reason}).{detail}",
                    service=name, source='kubectl get pods -o json', severity='warning',
                    timestamp=pod.get('observed_at', ''), raw_ref=pod,
                ))
                continue
            if reason:
                items.append(make_evidence_item(
                    "pod_waiting_state",
                    f"Pod/{name}{container} current state is {reason}.{detail}",
                    service=name,
                    source="kubectl get pods -o json",
                    severity="critical" if reason in {"ImagePullBackOff", "CrashLoopBackOff"} else "warning",
                    timestamp=pod.get('observed_at', ''),
                    raw_ref=pod,
                ))
        return items

    def _fetch_node_status(self) -> Optional[str]:
        """Fetch node status."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command="get nodes -o wide")
        return result.data if result.success else None

    def _fetch_problem_pods(self, namespace: str = "") -> List[Dict]:
        """Find pods in problematic states."""
        k8s_res = self.registry.get("k8s_resources")
        if k8s_res is None:
            return []
        result = k8s_res.execute(
            action="list", resource_type="pods",
            namespace=namespace or "", output="json"
        )
        if not result.success:
            return []
        
        import json
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
        except (json.JSONDecodeError, TypeError):
            return []
        
        pods = data.get('items', [])
        problems = problem_containers(pods, namespace)
        selectors = {}
        snapshots = {}
        for pod in pods:
            if not isinstance(pod, dict):
                continue
            meta, spec = pod.get('metadata') or {}, pod.get('spec') or {}
            snapshot=pod_spec_checks(pod)
            if snapshot is not None:
                snapshots[(meta.get('name'),meta.get('namespace') or namespace,meta.get('uid',''))]=snapshot
            selector = spec.get('nodeSelector')
            if isinstance(selector, dict) and selector and all(
                    isinstance(k, str) and k and isinstance(v, str) for k, v in selector.items()):
                selectors[(meta.get('name'), meta.get('namespace') or namespace, meta.get('uid', ''))] = dict(selector)
        for item in problems:
            identity = (item.get('name'), item.get('namespace'), item.get('pod_uid', ''))
            if not item.get('previous') and identity in snapshots:
                item['spec_checks']=snapshots[identity]
            if item.get('phase') == 'Pending' and not item.get('previous') and identity in selectors:
                item['node_selector'] = selectors[identity]
        return problems

    def _describe_resource(self, kind: str, name: str, namespace: str) -> Optional[str]:
        """Get kubectl describe output for a resource."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command=f"describe {kind} {name}", namespace=namespace)
        return result.data if result.success else None

    def _fetch_network_objects(self, query: str, namespace: str) -> Dict[str, str]:
        """Fetch policy and ChaosMesh objects for network-related incidents."""
        text = str(query or "").lower()
        if not any(h in text for h in [
            "network", "dns", "timeout", "connection", "chaosmesh",
            "networkchaos", "networkpolicy", "network policy", "partition",
            "网络", "超时", "连接",
        ]):
            return {}
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return {}
        ns = namespace or "default"
        objects: Dict[str, str] = {}
        for kind in ("networkchaos", "networkpolicy"):
            result = kubectl.execute(command=f"get {kind} -n {ns} -o wide")
            if result.success:
                objects[kind] = str(result.data or "")
        return objects

    def _build_network_evidence_items(self, network_objects: Dict[str, str]) -> List[Dict]:
        """Normalize explicit network fault objects for correlation and tracing."""
        items: List[Dict] = []
        chaos = str(network_objects.get("networkchaos") or "").strip()
        if chaos and "No resources found" not in chaos:
            items.append(make_evidence_item(
                "network_chaos",
                f"Active ChaosMesh NetworkChaos objects detected: {chaos[:500]}",
                source="kubectl get networkchaos",
                severity="critical",
            ))
        policies = str(network_objects.get("networkpolicy") or "").strip()
        if policies and "No resources found" not in policies:
            items.append(make_evidence_item(
                "network_policy",
                f"Active Kubernetes NetworkPolicy objects detected: {policies[:500]}",
                source="kubectl get networkpolicy",
                severity="warning",
            ))
        return items
