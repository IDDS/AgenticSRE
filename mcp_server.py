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
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: MCP SDK not installed. Run: pip install mcp[cli]", file=sys.stderr)
    sys.exit(1)

from configs.config_loader import apply_connection_overrides, get_config
from tools import build_tool_registry, configured_llm
from agents import (
    MetricAgent, LogAgent, TraceAgent, EventAgent,
    AlertAgent, HypothesisAgent, DetectionAgent,
)

logger = logging.getLogger(__name__)

# ── Init ──
mcp = FastMCP("AgenticSRE")
_cfg = None
_registry = None
_llm = None
_PLATFORM_CONFIG_FILE = Path(__file__).resolve().parent / "data" / "platform_config.json"


def _init():
    global _cfg, _registry, _llm
    if _cfg is None:
        base = get_config()
        config = replace(base, llm=replace(base.llm), observability=replace(base.observability))
        try:
            overrides = json.loads(_PLATFORM_CONFIG_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            overrides = {}
        if not isinstance(overrides, dict):
            raise ValueError("Invalid platform configuration")
        apply_connection_overrides(config, overrides)
        reader_config = replace(config, runtime=replace(config.runtime, enable_self_healing=False))
        registry = build_tool_registry(reader_config, isolated=True)
        _cfg, _registry, _llm = config, registry, configured_llm(config.llm)


def _read_tool(tool_name: str, *, raw_text: bool = False, **arguments) -> str:
    _init()
    tool = _registry.get(tool_name)
    if tool is None:
        raise RuntimeError(f"{tool_name} tool not available")
    result = tool.execute(**arguments)
    if not result.success:
        raise RuntimeError(result.error or f"{tool_name} read failed")
    if isinstance(result.data, dict) and result.data.get("stub") is True:
        raise RuntimeError(f"{tool_name} backend is not configured")
    if raw_text and isinstance(result.data, str):
        return result.data
    return json.dumps(result.data, indent=2, default=str)


# ─────────────────────────────────────────
# Kubernetes Tools
# ─────────────────────────────────────────

@mcp.tool()
def kubectl_run(command: str, namespace: str = "") -> str:
    """Execute a safe kubectl command on the K8s cluster.
    Only read commands (get, describe, logs, top) are allowed.
    """
    return _read_tool("kubectl", raw_text=True, command=command, namespace=namespace)


@mcp.tool()
def k8s_health_check() -> str:
    """Run comprehensive K8s cluster health check.
    Returns node status, pod health, warnings, and resource usage.
    """
    return _read_tool("k8s_health")


@mcp.tool()
def k8s_resource_info(action: str, resource: str, name: str = "", namespace: str = "") -> str:
    """Get K8s resource info.
    Actions: list, describe, logs, events, top
    Resources: pods, nodes, services, deployments, etc.
    """
    return _read_tool("k8s_resources", raw_text=True, action=action, resource_type=resource, name=name, namespace=namespace)


# ─────────────────────────────────────────
# Observability Tools
# ─────────────────────────────────────────

@mcp.tool()
def prometheus_query(query: str, time_range: str = "15m", natural_language: bool = False) -> str:
    """Run PromQL over a duration such as 15m, 1h or 1d. Set natural_language for a text question.
    """
    duration = re.fullmatch(r"([1-9][0-9]{0,9})([smhdw])", time_range)
    if not duration:
        raise ValueError("time_range must be a positive duration ending in s, m, h, d or w")
    end = int(time.time())
    seconds = int(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[duration[2]]
    if seconds > end:
        raise ValueError("time_range starts before the Unix epoch")
    arguments = {"natural_language" if natural_language else "query": query}
    return _read_tool("prometheus", query_type="range", start=str(end - seconds), end=str(end),
                      step=f"{max(min(seconds, 60), (seconds + 999) // 1000)}s", **arguments)


@mcp.tool()
def search_logs(keyword: str, namespace: str = "", level: str = "", time_range: str = "15m") -> str:
    """Search logs in Elasticsearch.
    Supports filtering by keyword, namespace, log level, and time range.
    """
    return _read_tool("elasticsearch", query=keyword, namespace=namespace, level=level, time_range=time_range)


@mcp.tool()
def query_traces(service: str = "", operation: str = "", lookback: str = "1h", limit: int = 20) -> str:
    """Query distributed traces from Jaeger.
    Returns trace summaries with span counts and durations.
    """
    return _read_tool("jaeger", service=service, operation=operation, lookback=lookback, limit=limit)


# ─────────────────────────────────────────
# Agent Endpoints
# ─────────────────────────────────────────

@mcp.tool()
async def analyze_metrics(query: str, namespace: str = "") -> str:
    """Run metric analysis using the MetricAgent.
    Queries Prometheus, detects anomalies with Hero 3σ and WeRCA onset detection.
    """
    _init()
    agent = MetricAgent(_llm, _registry)
    result = await agent.analyze(query, namespace)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def analyze_logs(query: str, namespace: str = "") -> str:
    """Run log analysis using the LogAgent.
    Searches logs, clusters patterns with Drain3, identifies error patterns.
    """
    _init()
    agent = LogAgent(_llm, _registry)
    result = await agent.analyze(query, namespace)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def analyze_traces(query: str, namespace: str = "") -> str:
    """Run trace analysis using the TraceAgent.
    Queries Jaeger, detects latency anomalies per service.
    """
    _init()
    agent = TraceAgent(_llm, _registry)
    result = await agent.analyze(query, namespace=namespace)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def analyze_events(query: str, namespace: str = "") -> str:
    """Run K8s event analysis using the EventAgent.
    Detects warning events, node issues, and problem pods.
    """
    _init()
    agent = EventAgent(_llm, _registry)
    result = await agent.analyze(query, namespace)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def compress_alerts(namespace: str = "", time_range: str = "") -> str:
    """Run intelligent alert compression.
    Groups noisy alerts by temporal-spatial patterns, LLM semantic clustering,
    and generates root cause recommendations per group.
    The collector does not support a caller-specified time range.
    """
    if time_range:
        raise ValueError("time_range is not supported by the alert collector")
    _init()
    agent = AlertAgent(_llm, _registry)
    result = await agent.compress_and_recommend(namespace=namespace)
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
async def run_rca_analysis(query: str, namespace: str = "") -> str:
    """Run full hypothesis-driven Root Cause Analysis pipeline.
    Executes 5 phases: Detection → Hypothesis → Investigation → Reasoning → Recovery.
    Returns structured RCA report with root cause, confidence, evidence, and remediation.
    """
    _init()
    from orchestrator.rca_engine import run_rca
    result = await run_rca(query, namespace=namespace, config=_cfg)
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
