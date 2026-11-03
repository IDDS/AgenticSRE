"""Explicitly configured ToolRegistry bridge for a trusted tuning executor."""
from __future__ import annotations

import asyncio
import copy
import threading
from typing import Any

from tools.base_tool import SRETool, ToolResult


class OptunaExperimentTool(SRETool):
    """Run one pre-approved policy; callers cannot choose targets or commands."""

    name = 'optuna_experiment'
    description = 'Run the pre-approved bounded configuration experiment with the configured trusted executor.'

    def __init__(self, *, policy: dict[str, Any], executor: Any) -> None:
        if not isinstance(policy, dict) or not callable(executor):
            raise ValueError('a trusted policy and executor are required')
        if set(policy) - {'objective', 'direction', 'choices', 'max_trials', 'seed'}:
            raise ValueError('unexpected policy fields')
        if not all(key in policy for key in ('objective', 'direction', 'choices')):
            raise ValueError('incomplete policy')
        if not isinstance(policy['objective'], str) or not policy['objective']:
            raise ValueError('invalid objective')
        if policy['direction'] not in ('minimize', 'maximize') or not isinstance(policy['choices'], dict) or not policy['choices']:
            raise ValueError('invalid policy')
        if type(policy.get('max_trials', 4)) is not int or not 1 <= policy.get('max_trials', 4) <= 16:
            raise ValueError('invalid trial limit')
        self._policy = copy.deepcopy(policy)
        self._executor = executor
        self._run_lock = threading.Lock()

    async def run_async(self) -> ToolResult:
        """Use from async callers; prevent overlapping actions on the same tool."""
        if not self._run_lock.acquire(blocking=False):
            return ToolResult(success=False, error='experiment already running', source=self.name)
        try:
            from integrations.optuna_mcp.workflow import run_experiment
            result = await run_experiment(executor=self._executor, **copy.deepcopy(self._policy))
            summary = result.get('summary', {})
            success = result.get('status') == 'completed' and summary.get('completed', 0) > 0 and summary.get('failed') == 0
            return ToolResult(success=success, data=result, error='' if success else 'experiment incomplete or failed', source=self.name)
        finally:
            self._run_lock.release()

    def _execute(self, **kwargs: Any) -> ToolResult:
        if kwargs:
            return ToolResult(success=False, error='experiment policy cannot be changed by a caller')
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async())
        return ToolResult(success=False, error='async callers must await run_async')

    def _parameters_schema(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}, 'additionalProperties': False}
