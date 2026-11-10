"""Exercise server startup and protocol validation without external services."""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_startup_and_validation():
    async def check():
        server = os.environ.get('AGENTICSRE_MCP_TEST_SERVER') or str(Path(__file__).resolve().parents[1] / 'mcp_server.py')
        env = dict(os.environ)
        async with stdio_client(StdioServerParameters(command=sys.executable, args=['-u', server], env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)
                tools = await asyncio.wait_for(session.list_tools(), 15)
                assert 'kubectl_run' in {tool.name for tool in tools.tools}
                response = await asyncio.wait_for(session.call_tool('kubectl_run', {}), 15)
                assert response.isError

    asyncio.run(check())
