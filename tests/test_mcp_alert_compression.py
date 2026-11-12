"""Exercise alert compression through the MCP protocol without external sources."""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_compress_alerts_awaits_agent_and_rejects_time_range():
    bootstrap = '''
import mcp_server
class FakeAlertAgent:
    def __init__(self, *args): pass
    async def compress_and_recommend(self, namespace=""):
        return {"namespace": namespace, "groups": [{"id": "g1"}]}
mcp_server._init = lambda: None
mcp_server.AlertAgent = FakeAlertAgent
mcp_server.mcp.run(transport="stdio")
'''
    async def check():
        async with stdio_client(StdioServerParameters(command=sys.executable, args=['-c', bootstrap], env=dict(os.environ))) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)
                result = await asyncio.wait_for(session.call_tool('compress_alerts', {'namespace': 'demo'}), 15)
                assert not result.isError
                data = json.loads(result.content[0].text)
                assert data == {'namespace': 'demo', 'groups': [{'id': 'g1'}]}
                rejected = await asyncio.wait_for(session.call_tool('compress_alerts', {'time_range': '2h'}), 15)
                assert rejected.isError
    asyncio.run(check())
