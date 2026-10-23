"""Controlled tool access for executable skills.

Skills should not shell out directly.  The runtime exposes this small context so
skills can request approved tools and so every tool call is captured in the run
trace.  Host-specific execution, such as cluster-aware kubectl, is injected by
the Agent side.
"""

from __future__ import annotations

import time
from .kubectl_safety import readonly_kubectl_args
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


ToolRunner = Callable[[str, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


READ_ONLY_KUBECTL_VERBS = {"get", "describe", "logs", "top", "events", "cluster-info", "version"}


@dataclass
class SkillToolContext:
    runner: Optional[ToolRunner] = None
    dry_run: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def execute(self, tool: str, **kwargs: Any) -> Dict[str, Any]:
        """Execute a controlled tool request or record a dry-run plan."""
        started = time.time()
        request = {
            "tool": str(tool or "").strip(),
            "args": dict(kwargs),
            "dry_run": bool(self.dry_run),
            "success": False,
            "executed": False,
            "summary": "",
        }
        if not request["tool"]:
            request["summary"] = "工具名为空，未执行。"
            request["error"] = "empty_tool"
            self.calls.append(request)
            return dict(request)

        denied = self._deny_reason(request["tool"], kwargs)
        if denied:
            request["summary"] = denied
            request["error"] = "tool_policy_denied"
            self.calls.append(request)
            return dict(request)

        if not self.runner:
            request["summary"] = f"工具 {request['tool']} 未配置运行器，已记录计划。"
            self.calls.append(request)
            return dict(request)

        try:
            result = self.runner(request["tool"], dict(kwargs), self.payload)
        except Exception as exc:  # pragma: no cover - defensive host isolation
            result = {"success": False, "error": str(exc), "summary": f"工具 {request['tool']} 执行异常: {exc}"}
        if not isinstance(result, dict):
            result = {"success": False, "error": "tool_runner_returned_non_dict", "data": result}

        ok = bool(result.get("ok", result.get("success", False)))
        request.update({
            "success": ok,
            "executed": bool(result.get("executed", True)),
            "duration_ms": int((time.time() - started) * 1000),
            "summary": str(result.get("summary") or ("执行成功" if ok else result.get("error") or "执行失败")),
            "data": result.get("data", result.get("stdout", "")),
            "sample": str(result.get("sample") or result.get("stdout") or result.get("stderr") or result.get("data") or "")[:1200],
            "error": str(result.get("error") or result.get("stderr") or ""),
            "returncode": result.get("returncode"),
            "source": result.get("source") or request["tool"],
        })
        self.calls.append(request)
        return dict(request)

    def _deny_reason(self, tool: str, kwargs: Dict[str, Any]) -> str:
        if tool == "kubectl":
            command = str(kwargs.get("command") or "").strip()
            try:
                readonly_kubectl_args(command, str(kwargs.get("namespace") or ""))
            except ValueError as exc:
                return f"Skill 工具上下文只允许只读 kubectl，已拒绝: {exc}"
        return ""
