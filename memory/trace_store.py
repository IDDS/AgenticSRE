"""
AgenticSRE Trace Store
Execution trace storage for agent observability and behavior validation.
SOW: "多智能体行为的可观测性与验证技术"
"""

import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class AgentTrace:
    """A single agent execution trace record."""
    trace_id: str = ""
    agent_name: str = ""
    action: str = ""
    input_summary: str = ""
    output_summary: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    token_usage: int = 0
    status: str = "success"     # success | error | timeout
    error: str = ""
    metadata: Dict = field(default_factory=dict)


@dataclass
class PipelineTrace:
    """Complete trace of a pipeline execution."""
    pipeline_id: str = ""
    incident_query: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    phases: List[Dict] = field(default_factory=list)
    agent_traces: List[AgentTrace] = field(default_factory=list)
    result: Dict = field(default_factory=dict)
    total_tokens: int = 0
    total_tool_calls: int = 0
    

class TraceStore:
    """
    Stores execution traces for observability, validation, and evolution.
    Enables post-hoc analysis of agent behavior and performance.
    """

    def __init__(self, config=None):
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.db_path = Path(cfg.memory.db_path) / "traces"
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._traces: List[PipelineTrace] = []
        self._load()

    def _load(self):
        """Load traces from disk."""
        trace_file = self.db_path / "pipeline_traces.json"
        if trace_file.exists():
            try:
                data = json.loads(trace_file.read_text())
                # Keep last 100 traces in memory
                self._traces = []
                for t in data[-100:]:
                    pt = PipelineTrace(**{k: v for k, v in t.items() if k != "agent_traces"})
                    pt.agent_traces = [AgentTrace(**at) for at in t.get("agent_traces", [])]
                    self._traces.append(pt)
            except Exception as e:
                logger.warning(f"Failed to load traces: {e}")

    def save(self):
        """Persist traces to disk."""
        trace_file = self.db_path / "pipeline_traces.json"
        data = []
        for t in self._traces[-200:]:
            td = {
                "pipeline_id": t.pipeline_id,
                "incident_query": t.incident_query,
                "start_time": t.start_time,
                "end_time": t.end_time,
                "phases": t.phases,
                "result": t.result,
                "total_tokens": t.total_tokens,
                "total_tool_calls": t.total_tool_calls,
                "agent_traces": [asdict(at) for at in t.agent_traces],
            }
            data.append(td)
        trace_file.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False))

    def start_pipeline(self, pipeline_id: str, incident_query: str) -> PipelineTrace:
        """Start tracking a new pipeline execution."""
        trace = PipelineTrace(
            pipeline_id=pipeline_id,
            incident_query=incident_query,
            start_time=time.time(),
        )
        self._traces.append(trace)
        return trace

    def add_agent_trace(self, pipeline_id: str, agent_trace: AgentTrace):
        """Add an agent trace to a pipeline."""
        for t in reversed(self._traces):
            if t.pipeline_id == pipeline_id:
                t.agent_traces.append(agent_trace)
                t.total_tokens += agent_trace.token_usage
                t.total_tool_calls += 1
                return
        logger.warning(f"Pipeline {pipeline_id} not found for agent trace")

    def complete_pipeline(self, pipeline_id: str, result: Dict):
        """Mark a pipeline as complete."""
        for t in reversed(self._traces):
            if t.pipeline_id == pipeline_id:
                t.end_time = time.time()
                t.result = result
                self.save()
                return

    def get_trace_by_id(self, pipeline_id: str) -> Optional[PipelineTrace]:
        """Find a pipeline trace by its id — used by expert_feedback to look up real RCA results."""
        if not pipeline_id:
            return None
        for t in reversed(self._traces):
            if t.pipeline_id == pipeline_id:
                return t
        return None

    def get_recent_traces(self, n: int = 10) -> List[Dict]:
        """Get recent pipeline traces."""
        return [
            {
                "pipeline_id": t.pipeline_id,
                "incident_query": t.incident_query[:200],
                "start_time": t.start_time,
                "duration_s": round(t.end_time - t.start_time, 1) if t.end_time else 0,
                "agent_count": len(t.agent_traces),
                "total_tokens": t.total_tokens,
                "result_summary": str(t.result.get("root_cause", ""))[:200],
            }
            for t in reversed(self._traces[-n:])
        ]

    def get_performance_stats(self) -> Dict:
        """Get aggregate performance statistics."""
        if not self._traces:
            return {"total_pipelines": 0}
        
        durations = [t.end_time - t.start_time for t in self._traces if t.end_time]
        tokens = [t.total_tokens for t in self._traces]
        
        return {
            "total_pipelines": len(self._traces),
            "avg_duration_s": round(sum(durations) / max(len(durations), 1), 1),
            "avg_tokens": int(sum(tokens) / max(len(tokens), 1)),
            "total_agent_calls": sum(len(t.agent_traces) for t in self._traces),
        }
