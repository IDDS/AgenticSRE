#!/usr/bin/env python3
"""
AgenticSRE MCP Server
Exposes tools as Model Context Protocol (MCP) endpoints for Claude / Copilot integration.

Run:
    python mcp_server.py
    # or: fastmcp run mcp_server.py
"""

import json
import logging
import sys
from typing import Any, Dict, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: fastmcp not installed. Run: pip install mcp[cli]")
    sys.exit(1)

from configs.config_loader import get_config
from tools import build_tool_registry, LLMClient
from agents import (
    MetricAgent, LogAgent, TraceAgent, EventAgent,
    AlertAgent, HypothesisAgent, DetectionAgent,
)

logger = logging.getLogger(__name__)

# ── Init ──
mcp = FastMCP("AgenticSRE", description="Multi-Agent Intelligent Operations System")
_cfg = None
_registry = None
_llm = None


def _init():
    global _cfg, _registry, _llm
    if _cfg is None:
        _cfg = get_config()
        _registry = build_tool_registry(_cfg)
        _llm = LLMClient(_cfg.llm)


# ─────────────────────────────────────────
# Kubernetes Tools
# ─────────────────────────────────────────

@mcp.tool()
def kubectl_run(command: str, namespace: str = "") -> str:
    """Execute a safe kubectl command on the K8s cluster.
    Only read commands (get, describe, logs, top) are allowed.
    """
    _init()
    tool = _registry.get("kubectl")
    if not tool:
        return "kubectl tool not available"
    result = tool.execute(command=command, namespace=namespace)
    return result.data if result.success else f"Error: {result.error}"


@mcp.tool()
def k8s_health_check() -> str:
    """Run comprehensive K8s cluster health check.
    Returns node status, pod health, warnings, and resource usage.
    """
    _init()
    tool = _registry.get("k8s_health")
    if not tool:
        return "k8s_health tool not available"
    result = tool.execute()
    return json.dumps(result.data, indent=2, default=str) if result.success else f"Error: {result.error}"


@mcp.tool()
def k8s_resource_info(action: str, resource: str, name: str = "", namespace: str = "") -> str:
    """Get K8s resource info.
    Actions: list, describe, logs, events, top
    Resources: pods, nodes, services, deployments, etc.
    """
    _init()
    tool = _registry.get("k8s_resource")
    if not tool:
        return "k8s_resource tool not available"
    result = tool.execute(action=action, resource=resource, name=name, namespace=namespace)
    return result.data if result.success else f"Error: {result.error}"


# ─────────────────────────────────────────
# Observability Tools
# ─────────────────────────────────────────

@mcp.tool()
def prometheus_query(query: str, time_range: str = "15m") -> str:
    """Execute a PromQL query or natural language metric query.
    Supports both raw PromQL and natural language (auto-translated to PromQL).
    """
    _init()
    tool = _registry.get("prometheus")
    if not tool:
        return "prometheus tool not available"
    result = tool.execute(query=query, time_range=time_range)
    return json.dumps(result.data, indent=2, default=str) if result.success else f"Error: {result.error}"


@mcp.tool()
def search_logs(keyword: str, namespace: str = "", level: str = "", time_range: str = "15m") -> str:
    """Search logs in Elasticsearch.
    Supports filtering by keyword, namespace, log level, and time range.
    """
    _init()
    tool = _registry.get("elasticsearch")
    if not tool:
        return "elasticsearch tool not available"
    result = tool.execute(keyword=keyword, namespace=namespace, level=level, time_range=time_range)
    return json.dumps(result.data, indent=2, default=str) if result.success else f"Error: {result.error}"


@mcp.tool()
def query_traces(service: str = "", operation: str = "", lookback: str = "1h", limit: int = 20) -> str:
    """Query distributed traces from Jaeger.
    Returns trace summaries with span counts and durations.
    """
    _init()
    tool = _registry.get("jaeger")
    if not tool:
        return "jaeger tool not available"
    result = tool.execute(service=service, operation=operation, lookback=lookback, limit=limit)
    return json.dumps(result.data, indent=2, default=str) if result.success else f"Error: {result.error}"


# ─────────────────────────────────────────
# Agent Endpoints
# ─────────────────────────────────────────

@mcp.tool()
def analyze_metrics(query: str, namespace: str = "") -> str:
    """Run metric analysis using the MetricAgent.
    Queries Prometheus, detects anomalies with Hero 3σ and WeRCA onset detection.
    """
    _init()
    import asyncio
    agent = MetricAgent(_llm, _registry)
    result = asyncio.run(agent.analyze(query, namespace))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def analyze_logs(query: str, namespace: str = "") -> str:
    """Run log analysis using the LogAgent.
    Searches logs, clusters patterns with Drain3, identifies error patterns.
    """
    _init()
    import asyncio
    agent = LogAgent(_llm, _registry)
    result = asyncio.run(agent.analyze(query, namespace))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def analyze_traces(query: str, namespace: str = "") -> str:
    """Run trace analysis using the TraceAgent.
    Queries Jaeger, detects latency anomalies per service.
    """
    _init()
    import asyncio
    agent = TraceAgent(_llm, _registry)
    result = asyncio.run(agent.analyze(query, namespace=namespace))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def analyze_events(query: str, namespace: str = "") -> str:
    """Run K8s event analysis using the EventAgent.
    Detects warning events, node issues, and problem pods.
    """
    _init()
    import asyncio
    agent = EventAgent(_llm, _registry)
    result = asyncio.run(agent.analyze(query, namespace))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def compress_alerts(namespace: str = "", time_range: str = "15m") -> str:
    """Run intelligent alert compression (SOW core capability).
    Groups noisy alerts by temporal-spatial patterns, LLM semantic clustering,
    and generates root cause recommendations per group.
    Target: ≥80% compression accuracy.
    """
    _init()
    agent = AlertAgent(_llm, _registry)
    result = agent.compress_and_recommend(namespace=namespace, time_range=time_range)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def detect_anomalies(namespace: str = "") -> str:
    """Run anomaly detection scan.
    Checks Prometheus alerts, K8s events, pod health, and node status.
    """
    _init()
    agent = DetectionAgent(_llm, _registry, _cfg)
    signals = agent.detect(namespace=namespace)
    return json.dumps([s.__dict__ if hasattr(s, '__dict__') else str(s) for s in signals],
                      indent=2, ensure_ascii=False, default=str)


# ─────────────────────────────────────────
# RCA Pipeline
# ─────────────────────────────────────────

@mcp.tool()
def run_rca_analysis(query: str, namespace: str = "") -> str:
    """Run full hypothesis-driven Root Cause Analysis pipeline.
    Executes 5 phases: Detection → Hypothesis → Investigation → Reasoning → Recovery.
    Returns structured RCA report with root cause, confidence, evidence, and remediation.
    """
    _init()
    import asyncio
    from orchestrator.rca_engine import run_rca
    result = asyncio.run(run_rca(query, namespace=namespace, config=_cfg))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


# ─────────────────────────────────────────
# Memory & Context
# ─────────────────────────────────────────

@mcp.tool()
def query_historical_context(query: str, namespace: str = "") -> str:
    """Query historical fault context and learned rules from memory.
    Returns similar past incidents and learned diagnostic rules.
    """
    _init()
    from memory import FaultContextStore
    store = FaultContextStore(_cfg)
    from memory.memory_scope import MemoryScope
    result = store.get_historical_context(query, memory_scope=MemoryScope(
        cluster_id=str(getattr(_cfg.kubernetes, 'cluster_id', '') or ''), namespace=namespace))
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


# ─────────────────────────────────────────
# Resources
# ─────────────────────────────────────────

@mcp.resource("config://agenticsre")
def get_config_resource() -> str:
    """Current AgenticSRE configuration."""
    _init()
    return json.dumps({
        "llm_model": _cfg.llm.model,
        "kubernetes": {"use_ssh": _cfg.kubernetes.use_ssh},
        "pipeline": {
            "max_iterations": _cfg.pipeline.max_evidence_iterations,
            "confidence_threshold": _cfg.pipeline.hypothesis_confidence_threshold,
        },
    }, indent=2)


# ── Entry Point ──

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")
