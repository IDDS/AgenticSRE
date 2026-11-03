"""Bounded MCP client for the local, suggestion-only Optuna server."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class OptunaMCPError(RuntimeError):
    """The MCP transport or tool did not produce a usable result."""


class OptunaMCPClient:
    def __init__(self, session: ClientSession, timeout_seconds: float = 15) -> None:
        self._session = session
        self._timeout = timeout_seconds
        self._unusable = False

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in {'create_study', 'suggest_trial', 'report_trial', 'study_summary', 'close_study'}:
            raise ValueError('tool not allowed')
        if self._unusable:
            raise OptunaMCPError('MCP session is unusable after an uncertain outcome')
        try:
            response = await asyncio.wait_for(self._session.call_tool(tool, arguments), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            self._unusable = True
            raise OptunaMCPError('MCP call timed out; outcome unknown') from exc
        if response.isError or not isinstance(response.structuredContent, dict):
            raise OptunaMCPError('MCP tool failed or returned invalid content')
        return response.structuredContent


@asynccontextmanager
async def connect_optuna(*, timeout_seconds: float = 15) -> AsyncIterator[OptunaMCPClient]:
    """Start a private local server for one experiment; never pass secrets to it."""
    if not 0 < timeout_seconds <= 120:
        raise ValueError('invalid timeout')
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'PYTHONPATH') if key in os.environ}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('server.py'))], env=environment)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            try:
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise OptunaMCPError('MCP initialization timed out') from exc
            yield OptunaMCPClient(session, timeout_seconds)
