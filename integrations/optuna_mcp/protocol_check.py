"""Exercise the real MCP stdio protocol, including invalid and failed trials."""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('server.py'))], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {'create_study', 'suggest_trial', 'report_trial', 'study_summary', 'close_study'} <= names

            async def invoke(name, arguments, error=False):
                result = await session.call_tool(name, arguments)
                assert bool(result.isError) is error, (name, result)
                return result.structuredContent if not error else None

            created = await invoke('create_study', {'objective': 'get_qps', 'direction': 'maximize', 'choices': {'io_threads': [1, 2], 'hz': [10, 50, 100]}, 'seed': 42})
            sid = created['study_id']
            assert created['persistent'] is False
            first = await invoke('suggest_trial', {'study_id': sid})
            assert first['applied'] is False and first['parameters']['io_threads'] in (1, 2)
            await invoke('close_study', {'study_id': sid}, error=True)
            await invoke('report_trial', {'study_id': sid, 'trial_id': first['trial_id'], 'status': 'complete', 'value': 42.0, 'evidence_ref': 'trial-1.json'})
            await invoke('report_trial', {'study_id': sid, 'trial_id': first['trial_id'], 'status': 'complete', 'value': 43.0}, error=True)
            second = await invoke('suggest_trial', {'study_id': sid})
            await invoke('report_trial', {'study_id': sid, 'trial_id': second['trial_id'], 'status': 'failed'})
            summary = await invoke('study_summary', {'study_id': sid})
            assert summary['completed'] == 1 and summary['failed'] == 1 and summary['pending'] == 0
            assert summary['best']['value'] == 42.0 and summary['evidence_verified'] is False
            await invoke('create_study', {'objective': 'bad', 'direction': 'maximize', 'choices': {'x': [True, 1]}}, error=True)
            await invoke('suggest_trial', {'study_id': 'missing'}, error=True)
            await invoke('close_study', {'study_id': sid})
            await invoke('study_summary', {'study_id': sid}, error=True)
            print('MCP stdio protocol check: PASS; create/suggest/report/summary and failure paths')


if __name__ == '__main__':
    asyncio.run(main())
