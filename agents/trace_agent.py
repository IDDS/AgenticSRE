"""
AgenticSRE Trace Agent
Fetches and analyzes distributed traces from Jaeger with Hero latency analysis.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.hero_analysis import HeroTraceAnalyzer
from agents.evidence import make_evidence_item

logger = logging.getLogger(__name__)


class TraceAgent:
    """
    Trace analysis agent: fetches traces from Jaeger, runs Hero latency
    analysis + WeRCA time-window comparison, identifies slow spans and
    service bottlenecks.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE distributed tracing expert.
Analyze the provided trace data and latency statistics.
Identify:
1. Services with abnormal latency (p95/p99 violations)
2. Bottleneck spans in the request chain
3. Latency degradation compared to baseline
4. Service-to-service call patterns that indicate issues
5. Error spans and their propagation

Be precise about latency values and service names. Format as structured analysis."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.analyzer = HeroTraceAnalyzer()
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, service: str = "",
                      namespace: str = "", trace_ids: Optional[List[str]] = None) -> Dict:
        """Run trace analysis for the given incident query."""
        if trace_ids is not None:
            return await self._analyze_exact(query, namespace, trace_ids)
        
        # Get available services
        services = self._list_services()
        
        # Fetch traces for specified or all services
        all_traces = {}
        target_services = [service] if service else (services or [])
        
        for svc in target_services[:10]:  # Limit
            traces = self._fetch_traces(svc)
            if traces:
                all_traces[svc] = traces
        if not all_traces:
            return {'agent':'trace_agent','services_analyzed':0,'error':True,'evidence_items':[],
                    'summary':'未取得匹配链路，不能确认传播。'}

        # Run Hero latency analysis per service
        latency_results = {}
        for svc, traces in all_traces.items():
            analysis = self.analyzer.latency_analysis(traces)
            if analysis and analysis.get("count", 0) > 0:
                latency_results[svc] = analysis

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"Services analyzed: {len(all_traces)}\n"
        for svc, result in latency_results.items():
            context += f"\n[{svc}]:\n"
            context += f"  Traces: {result.get('count', 0)}\n"
            context += f"  Mean: {result.get('mean_us', 0)}μs\n"
            context += f"  P95: {result.get('p95_us', 0)}μs\n"
            context += f"  P99: {result.get('p99_us', 0)}μs\n"
            context += f"  Slow traces: {result.get('slow_trace_count', 0)}\n"

        summary = await self.llm.async_chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=self.summary_max_tokens, thinking=False)

        return {
            "agent": "trace_agent",
            "query": query,
            "services_analyzed": len(all_traces),
            "latency_results": latency_results,
            "summary": summary,
        }

    async def _analyze_exact(self, query, namespace, trace_ids):
        """Read original spans; service summaries omit errors and parent links."""
        jaeger = self.registry.get('jaeger')
        ids = list(dict.fromkeys(t for t in trace_ids if isinstance(t,str)
                   and re.fullmatch(r'(?:[a-fA-F0-9]{16}|[a-fA-F0-9]{32})',t)))[:3]
        items, missing, seen = [], [], set()
        for trace_id in ids:
            response = jaeger.execute(trace_id=trace_id) if jaeger else None
            data = response.data if response and response.success and isinstance(response.data,dict) else {}
            traces = [t for t in data.get('data',[]) if t.get('traceID')==trace_id]
            if not traces:
                missing.append(trace_id)
                continue
            for trace in traces[:1]:
                for span in trace.get('spans',[])[:1000]:
                    process=trace.get('processes',{}).get(span.get('processID'),{})
                    resource={t['key']:t.get('value') for t in process.get('tags',[]) if isinstance(t,dict) and 'key' in t}
                    if not namespace or resource.get('k8s.namespace.name')!=namespace:
                        continue
                    key=(trace_id,span.get('spanID'))
                    if key in seen:continue
                    seen.add(key)
                    tags={t['key']:t.get('value') for t in span.get('tags',[]) if isinstance(t,dict) and 'key' in t}
                    observation=dict(operation=span.get('operationName'),duration_us=span.get('duration'),
                        error=tags.get('error'),http_status=tags.get('http.status_code',tags.get('http.response.status_code')),
                        parent_links=span.get('references',[]))
                    items.append(make_evidence_item('trace_span_observed',str(observation),
                        service=resource.get('k8s.pod.name') or process.get('serviceName',''),source='jaeger',
                        timestamp=str(span.get('startTime','')),raw_ref=dict(trace_id=trace_id,span_id=span.get('spanID'),
                            namespace=namespace,pod_uid=resource.get('k8s.pod.uid'),**observation)))
        if not items:
            return dict(agent='trace_agent',error=True,evidence_items=[],missing_trace_ids=missing,
                        summary='未取得同 namespace 的原始 Span，传播假设未验证。')
        context='原始 Span 观测，不代表因果确认。\n'+str(items[:12])
        summary_error=''
        try:
            summary=await self.llm.async_chat([{'role':'system','content':self.SYSTEM_PROMPT},
                {'role':'user','content':context[:8000]}],max_tokens=self.summary_max_tokens,thinking=False)
        except Exception as exc:
            summary,summary_error='',type(exc).__name__
        return dict(agent='trace_agent',query=query,services_analyzed=len({i['service'] for i in items}),
            evidence_items=items[:30],missing_trace_ids=missing,partial=bool(missing) or len(items)>30,
            summary=summary or f'已取得 {len(items)} 份原始 Span；请核对错误标志与父子引用。',summary_error=summary_error)

    def _list_services(self) -> List[str]:
        """List available services in Jaeger."""
        jaeger = self.registry.get("jaeger")
        if jaeger is None:
            return []
        result = jaeger.execute()
        if result.success and isinstance(result.data, dict):
            return result.data.get("data", [])
        return []

    def _fetch_traces(self, service: str) -> List[Dict]:
        """Fetch traces for a service."""
        jaeger = self.registry.get("jaeger")
        if jaeger is None:
            return []
        result = jaeger.execute(service=service, limit=50, lookback="1h")
        if result.success and isinstance(result.data, dict):
            return result.data.get("traces", [])
        return []
