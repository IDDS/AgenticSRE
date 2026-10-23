"""
AgenticSRE Log Agent
Fetches and analyzes logs from Elasticsearch with Hero pattern analysis and Drain3 clustering.
"""

import logging
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.hero_analysis import HeroLogAnalyzer
from agents.evidence import make_evidence_item

logger = logging.getLogger(__name__)


def select_log_samples(lines, pod_name):
    """Retain recent scoped state changes alongside current request observations.

    These are untrusted application observations, not verified causal labels.
    """
    transitions = []
    requests = []
    for index, line in enumerate(lines):
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (isinstance(item, dict) and item.get('event') == 'state_transition'
                and item.get('pod') == pod_name and item.get('timestamp')
                and 'before' in item and 'after' in item):
            transitions.append(index)
        if (isinstance(item, dict) and item.get('pod') == pod_name
                and type(item.get('code')) is int and item.get('trace_id')):
            requests.append(index)
    # Keep the latest transition/request pair ahead of access-log noise and
    # older transitions so the bounded evidence text retains complete records.
    indexes = list(dict.fromkeys(transitions[-1:] + requests[-1:] + transitions[-2:-1]
                               + list(range(max(0, len(lines)-2), len(lines)))))
    return [dict(line_number=index+1, text=lines[index][:600], state_transition=index in transitions)
            for index in indexes]


class LogAgent:
    """
    Log analysis agent: fetches logs from Elasticsearch, applies Hero pattern
    analysis + Drain3 clustering, identifies error patterns, and produces
    LLM-summarized log insights.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE log analysis expert.
Analyze the provided log data, patterns, and clusters.
Identify:
1. Error patterns and their frequency
2. Anomalous log entries (rare patterns)
3. Root cause indicators from log messages
4. Timeline of error propagation
5. Affected components/pods/services

Be specific about error messages and their implications. Format as structured analysis."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.analyzer = HeroLogAnalyzer()
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, namespace: str = "",
                      time_range: str = "1h", pod_name: str = "") -> Dict:
        """Run log analysis for the given incident query."""
        
        # Strategy 1: Elasticsearch search
        es_results = self._search_elasticsearch(query, namespace, time_range)
        
        # Strategy 2: Direct pod logs via kubectl
        kubectl_logs = {}
        if pod_name:
            kubectl_logs = self._fetch_pod_logs(pod_name, namespace)
        
        # Combine all log entries
        all_entries = []
        if es_results:
            all_entries.extend([
                entry.get("message", "") for entry in es_results.get("entries", [])
            ])
        if kubectl_logs:
            all_entries.extend(kubectl_logs.get("lines", []))
        if not all_entries:
            return {'agent': 'log_agent', 'query': query, 'total_entries': 0,
                    'evidence_items': [], 'error': True, 'summary': '未取得日志，不能据此判断服务正常或确认根因。'}
        evidence_items = []
        if kubectl_logs:
            samples = select_log_samples(kubectl_logs['lines'], pod_name)
            evidence_items.append(make_evidence_item('pod_logs_observed', '\n'.join(item['text'] for item in samples)[:1500],
                service=pod_name, source='kubectl', timestamp=datetime.now(timezone.utc).isoformat(),
                raw_ref={'namespace': namespace, 'pod': pod_name, 'line_count': kubectl_logs['count'],
                         'command': kubectl_logs['command'],
                         'sample_line_numbers': [item['line_number'] for item in samples]}))

        # Run analysis
        pattern_result = self.analyzer.pattern_analysis(all_entries) if all_entries else {}
        cluster_result = self.analyzer.drain3_cluster(all_entries) if len(all_entries) > 10 else {}

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"Total log entries: {len(all_entries)}\n"
        context += f"Pod log samples (untrusted observations): {select_log_samples(all_entries, pod_name)}\n"
        if pattern_result:
            context += f"\nPattern Analysis:\n"
            context += f"- Unique patterns: {pattern_result.get('unique_patterns', 0)}\n"
            context += f"- Error count: {pattern_result.get('error_count', 0)}\n"
            context += f"- Error samples: {pattern_result.get('error_samples', [])}\n"
            context += f"- Rare patterns: {pattern_result.get('rare_patterns', [])}\n"
        if cluster_result:
            context += f"\nLog Clusters:\n"
            for c in cluster_result.get("clusters", [])[:10]:
                context += f"  [{c.get('size', 0)}x] {c.get('template', '')[:100]}\n"

        summary_error = ''
        try:
            summary = await self.llm.async_chat([
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": context[:8000]}
            ], max_tokens=self.summary_max_tokens, thinking=False)
        except Exception as exc:
            summary, summary_error = '', type(exc).__name__

        return {
            "agent": "log_agent",
            "query": query,
            "total_entries": len(all_entries),
            "pattern_analysis": pattern_result,
            "cluster_analysis": cluster_result,
            "summary": summary or f'已取得 {len(all_entries)} 条日志；摘要不可用，请核对原始观测。',
            "summary_error": summary_error,
            "evidence_items": evidence_items,
        }

    def _search_elasticsearch(self, query: str, namespace: str, time_range: str) -> Optional[Dict]:
        """Search Elasticsearch for logs."""
        es = self.registry.get("elasticsearch")
        if es is None:
            return None
        # Search for errors first, then general query
        result = es.execute(query=query, time_range=time_range, namespace=namespace, level="error")
        if result.success and result.data:
            return result.data
        # Fallback: general search
        result = es.execute(query=query, time_range=time_range, namespace=namespace)
        return result.data if result.success else None

    def _fetch_pod_logs(self, pod_name: str, namespace: str) -> Optional[Dict]:
        """Fetch logs directly from a pod via kubectl."""
        if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', pod_name):
            return None
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command=f"logs {pod_name} --tail=500", namespace=namespace)
        if result.success and result.data:
            lines = result.data.strip().split("\n")
            command = f"logs {pod_name} --tail=500"
            if len(lines) >= 500 and not any(
                sample['state_transition']
                for sample in select_log_samples(lines, pod_name)
            ):
                expanded = kubectl.execute(command=f"logs {pod_name} --tail=2000", namespace=namespace)
                if expanded.success and isinstance(expanded.data, str) and expanded.data.strip():
                    lines = expanded.data.strip().split("\n")[-2000:]
                    command = f"logs {pod_name} --tail=2000"
            return {"pod": pod_name, "lines": lines, "count": len(lines), "command": command}
        return None
