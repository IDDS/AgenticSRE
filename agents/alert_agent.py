"""
AgenticSRE Alert Agent
告警分组与证据保留适配器。
The public entry uses conservative grouping and unverified relation proposals.
Legacy private LLM grouping helpers remain for compatibility and are not called.
"""

import asyncio
import json
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from agents.alert_compression import compress_alerts, propose_relations

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient, run_sync_in_daemon_thread

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """Structured alert representation."""
    alert_id: str = ""
    name: str = ""
    severity: str = "warning"   # critical | warning | info
    source: str = ""            # prometheus | k8s_event | es_log
    timestamp: float = 0.0
    labels: Dict[str, str] = field(default_factory=dict)
    message: str = ""
    fingerprint: str = ""
    group_id: str = ""          # After compression
    cluster_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.fingerprint:
            content = f"{self.name}:{self.source}:{sorted(self.labels.items())}"
            self.fingerprint = hashlib.md5(content.encode()).hexdigest()[:12]
        if not self.alert_id:
            self.alert_id = f"alert-{self.fingerprint}-{int(self.timestamp)}"


@dataclass
class AlertGroup:
    """Compressed alert group — multiple alerts collapsed to one."""
    group_id: str
    representative: Alert            # Most informative alert in group
    alerts: List[Alert] = field(default_factory=list)
    root_cause_suggestion: str = ""
    confidence: float = 0.0
    affected_services: List[str] = field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        return len(self.alerts) / 1.0 if self.alerts else 0

    def summary(self) -> Dict:
        return {
            "group_id": self.group_id,
            "alert_count": len(self.alerts),
            "representative": self.representative.name,
            "severity": max((a.severity for a in self.alerts), key=lambda s: {"critical": 3, "warning": 2, "info": 1}.get(s, 0)),
            "root_cause": self.root_cause_suggestion,
            "confidence": self.confidence,
            "affected_services": self.affected_services,
            "time_range": f"{min(a.timestamp for a in self.alerts):.0f} - {max(a.timestamp for a in self.alerts):.0f}",
        }


class AlertAgent:
    """Collect observations, retain originals, and delegate grouping to alert_compression.

    Root-cause verification belongs to the RCA pipeline, not the grouping model.
    """

    COMPRESSION_PROMPT = """你是资深 SRE 告警分析专家。请按根因对告警进行压缩分组。
除告警名、服务名、资源名等机器字段外，所有解释性内容必须使用中文。
每个分组需要：
1. 识别由同一底层问题引发的告警
2. 选择最具代表性的告警
3. 给出中文根因建议
4. 给出置信度 (0.0-1.0)

Input alerts:
{alerts}

只返回 JSON：
{{
    "groups": [
        {{
            "group_name": "中文分组名称",
            "alert_indices": [0, 2, 5],
            "root_cause": "中文根因解释",
            "confidence": 0.85,
            "affected_services": ["svc1", "svc2"]
        }}
    ],
    "compression_summary": "中文压缩摘要"
}}"""

    RCA_PROMPT = """你是资深 SRE 根因分析专家。请基于一个压缩后的告警组，输出详细根因分析。
除告警名、服务名、资源名、命令等机器字段外，所有解释性内容必须使用中文。

Alert Group: {group_name}
Alerts ({count}):
{alert_details}

Cluster Context:
{cluster_context}

请分析并提供：
1. 最可能根因，要求具体
2. 支持该结论的证据
3. 建议的排查步骤
4. 建议的修复动作

只返回 JSON：
{{
    "root_cause": "中文根因解释",
    "evidence": ["中文证据 1", "中文证据 2"],
    "investigation_steps": ["中文步骤 1", "中文步骤 2"],
    "remediation": "中文修复建议",
    "confidence": 0.85,
    "severity": "critical|high|medium|low"
}}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.semantic_timeout_s = max(1, min(180, float(getattr(cfg.alert, "semantic_timeout_s", 60))))
        self.collection_timeout_s = 20
        self.collection_status = {}
        self.cluster_id = getattr(cfg.kubernetes, "cluster_id", "")
        self.time_window = cfg.alert.time_window
        self.similarity_threshold = cfg.alert.similarity_threshold
        self.max_group_size = cfg.alert.max_group_size

    async def compress_and_recommend(self, alerts: List[Alert] = None,
                                      namespace: str = "", enrich: bool = True) -> Dict:
        """Fetch alerts, preserve originals, group repeats and propose unverified relations."""
        
        # Phase 1: Collect alerts from all sources
        if alerts is None:
            alerts = await self._collect_alerts(namespace)
        
        result = compress_alerts([vars(a) for a in alerts], cluster_id=self.cluster_id, namespace=namespace, time_window=self.time_window)
        if enrich:
            from agents.alert_semantics import enrich_retrieval, build_scag
            await enrich_retrieval(result)
            await propose_relations(result, self.llm, timeout_s=self.semantic_timeout_s)
            build_scag(result)
        result["collection"] = dict(self.collection_status)
        incomplete = [name for name, status in self.collection_status.items() if status != "ok"]
        if incomplete:
            result.update(degraded=True, warning="部分来源采集未完成：" + ", ".join(incomplete) + "；当前仅展示成功采集的告警。")
        return result

    async def _collect_alerts(self, namespace: str = "") -> List[Alert]:
        """Collect sources concurrently; a failed source cannot hide a successful one."""
        async def collect(name, **kwargs):
            tool = self.registry.get(name)
            if tool is None:
                self.collection_status[name] = "unavailable"
                return None
            try:
                result = await asyncio.wait_for(
                    run_sync_in_daemon_thread(tool.execute, **kwargs), self.collection_timeout_s)
                self.collection_status[name] = "ok" if result.success else "error"
                return result.data if result.success else None
            except Exception as exc:
                self.collection_status[name] = type(exc).__name__
                return None

        prometheus, events = await asyncio.gather(
            collect("prometheus", query='ALERTS{alertstate="firing"' +
                    (',namespace='+json.dumps(namespace) if namespace else '') + '}', max_results=5000),
            collect("kubectl", command="get events --field-selector type=Warning --sort-by='.lastTimestamp' -o json", namespace=namespace),
        )
        alerts = []
        for source, payload in (('prometheus', prometheus), ('kubectl', events)):
            if payload is None and self.collection_status.get(source) == 'ok':
                self.collection_status[source] = 'invalid_response'
        if prometheus is not None:
            try:
                if not isinstance(prometheus, dict) or not isinstance(prometheus.get('results'), list):
                    raise ValueError('missing Prometheus result vector')
                if prometheus.get('truncated') or prometheus.get('result_count', len(prometheus['results'])) > len(prometheus['results']) or len(prometheus['results']) > 5000:
                    self.collection_status['prometheus'] = 'truncated'
                for item in prometheus['results'][:5000]:
                    metric = item.get("metric", {})
                    if namespace and metric.get("namespace") != namespace:
                        continue
                    alerts.append(Alert(name=metric.get("alertname", "unknown"),
                        severity=metric.get("severity", "warning"), source="prometheus", timestamp=time.time(),
                        labels=metric, message=metric.get("description", metric.get("summary", "")), raw=item))
            except (AttributeError, TypeError, ValueError):
                self.collection_status["prometheus"] = "invalid_response"
        if events is not None:
            try:
                data = json.loads(events) if isinstance(events, str) else events
                if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                    raise ValueError('missing Kubernetes event list')
                if len(data['items']) > 5000 or (data.get('metadata') or {}).get('continue'):
                    self.collection_status['kubectl'] = 'truncated'
                for event in data['items'][:5000]:
                    obj = event.get("involvedObject", {})
                    stamp = event.get("lastTimestamp") or event.get("eventTime") or event.get("metadata", {}).get("creationTimestamp")
                    try:
                        event_time = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        event_time = 0
                    alerts.append(Alert(name=event.get("reason", "K8sWarning"), severity="warning", source="k8s_event",
                        timestamp=event_time, labels={"kind": obj.get("kind", ""), "name": obj.get("name", ""),
                        "namespace": obj.get("namespace", namespace)}, message=event.get("message", ""), raw=event))
            except (AttributeError, TypeError, ValueError):
                self.collection_status["kubectl"] = "invalid_response"
        return alerts

    def _temporal_spatial_group(self, alerts: List[Alert]) -> Dict[str, List[int]]:
        """Pre-group alerts by time window + spatial locality."""
        groups = defaultdict(list)
        
        for i, alert in enumerate(alerts):
            # Group key: source + node/service + time bucket
            svc = (alert.labels.get("service", "") or 
                   alert.labels.get("pod", "") or 
                   alert.labels.get("name", "") or
                   alert.labels.get("instance", ""))
            ns = alert.labels.get("namespace", "")
            time_bucket = int(alert.timestamp // self.time_window)
            key = f"{alert.source}:{ns}:{svc}:{time_bucket}"
            groups[key].append(i)
        
        return dict(groups)

    async def _semantic_compress(self, pre_groups: Dict[str, List[int]],
                                   alerts: List[Alert]) -> List[AlertGroup]:
        """Use LLM to semantically compress pre-grouped alerts."""
        # If few enough alerts, use LLM to do global compression
        if len(alerts) <= 50:
            return await self._llm_compress(alerts)
        
        # Otherwise, compress each pre-group separately
        final_groups = []
        for key, indices in pre_groups.items():
            group_alerts = [alerts[i] for i in indices]
            if len(group_alerts) == 1:
                final_groups.append(AlertGroup(
                    group_id=f"grp-{key[:8]}",
                    representative=group_alerts[0],
                    alerts=group_alerts,
                ))
            else:
                sub_groups = await self._llm_compress(group_alerts)
                final_groups.extend(sub_groups)
        
        return final_groups

    async def _llm_compress(self, alerts: List[Alert]) -> List[AlertGroup]:
        """Use LLM for semantic alert compression."""
        alert_text = "\n".join([
            f"[{i}] name={a.name} severity={a.severity} source={a.source} "
            f"labels={a.labels} message={a.message[:200]}"
            for i, a in enumerate(alerts)
        ])
        
        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE alert compression expert."},
                {"role": "user", "content": self.COMPRESSION_PROMPT.format(alerts=alert_text[:6000])}
            ])
            
            groups = []
            for g in result.get("groups", []):
                indices = g.get("alert_indices", [])
                group_alerts = [alerts[i] for i in indices if i < len(alerts)]
                if group_alerts:
                    groups.append(AlertGroup(
                        group_id=f"grp-{hashlib.md5(g.get('group_name', '').encode()).hexdigest()[:8]}",
                        representative=group_alerts[0],
                        alerts=group_alerts,
                        root_cause_suggestion=g.get("root_cause", ""),
                        confidence=g.get("confidence", 0.0),
                        affected_services=g.get("affected_services", []),
                    ))
            return groups
            
        except Exception as e:
            logger.error(f"LLM compression failed: {e}")
            # Fallback: one group per alert
            return [
                AlertGroup(group_id=f"grp-{a.fingerprint}", representative=a, alerts=[a])
                for a in alerts
            ]

    async def _recommend_root_cause(self, group: AlertGroup) -> Dict:
        """Recommend root cause for an alert group."""
        alert_details = "\n".join([
            f"- [{a.severity}] {a.name}: {a.message[:200]} (source: {a.source})"
            for a in group.alerts[:20]
        ])
        
        # Get cluster context
        cluster_context = ""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health:
            result = k8s_health.execute(component="pods")
            if result.success:
                cluster_context = str(result.data)[:2000]

        try:
            return self.llm.json_chat([
                {"role": "system", "content": "你是资深 SRE 根因分析专家。除机器字段外，输出内容必须使用中文。"},
                {"role": "user", "content": self.RCA_PROMPT.format(
                    group_name=group.representative.name,
                    count=len(group.alerts),
                    alert_details=alert_details,
                    cluster_context=cluster_context[:2000],
                )}
            ])
        except Exception as e:
            logger.error(f"RCA recommendation failed: {e}")
            return {"root_cause": "分析失败，未能生成根因建议", "confidence": 0.0}
