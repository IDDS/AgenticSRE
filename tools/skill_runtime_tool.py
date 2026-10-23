"""ToolRegistry adapter for AgenticSRE Skill Runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from skill_runtime.tool_context import ToolRunner
from tools.base_tool import SRETool, ToolResult


class SkillRuntimeWorkflowTool(SRETool):
    """Expose Skill Runtime as a first-class RCA agent tool."""

    name = "skill_runtime_workflow"
    description = (
        "Invoke Skill Runtime to select and run operational skills for fault "
        "detection, diagnosis, dry-run self-healing planning, and verification."
    )

    def __init__(
        self,
        *,
        skills_dir: Path,
        runtime_url: str = "",
        timeout_s: int = 15,
        tool_runner: Optional[ToolRunner] = None,
    ) -> None:
        self.skills_dir = Path(skills_dir)
        self.runtime_url = runtime_url or os.environ.get("SKILL_RUNTIME_URL", "http://127.0.0.1:8090")
        self.timeout_s = timeout_s
        self.tool_runner = tool_runner

    def _execute(
        self,
        query: str,
        namespace: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        stages: Optional[Iterable[str]] = None,
        dry_run: bool = True,
        tool_runner: Optional[ToolRunner] = None,
    ) -> ToolResult:
        from orchestrator.skill_bridge import AgentSkillBridge, summarize_skill_result

        bridge = AgentSkillBridge(
            skills_dir=self.skills_dir,
            runtime_url=self.runtime_url,
            timeout_s=self.timeout_s,
            tool_runner=tool_runner or self.tool_runner,
        )
        result = bridge.run(
            query,
            namespace=namespace,
            evidence=evidence,
            stages=stages or ["detect", "diagnose", "heal", "verify"],
            dry_run=dry_run,
        )
        summary = summarize_skill_result(result)
        result.setdefault("agent_tool_invocation", {})
        result["agent_tool_invocation"].update({
            "tool": self.name,
            "mode": "tool_registry",
            "summary": summary,
        })
        return ToolResult(success=True, data=result)

    def _parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Incident or alert text used for skill matching.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace or logical tenant.",
                    "default": "default",
                },
                "evidence": {
                    "type": "object",
                    "description": "Evidence already collected by RCA agents.",
                },
                "stages": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["detect", "diagnose", "heal", "verify"]},
                    "description": "Skill workflow stages to run.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Keep remediation as planned commands without changing the system.",
                    "default": True,
                },
            },
            "required": ["query"],
        }
