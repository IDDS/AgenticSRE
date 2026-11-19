"""Exercise server startup and protocol validation without external services."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def test_read_tools_use_registered_backends_and_translate_public_arguments(monkeypatch, tmp_path):
    import mcp_server as server
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolResult

    cfg = AppConfig()
    cfg.runtime.enable_self_healing = True
    config_file = tmp_path / "platform_config.json"
    config_file.write_text(json.dumps({"llm": {"model": "test-model"},
                                       "observability": {"grafana_url": "https://dashboard.invalid"}}))
    monkeypatch.setattr(server, "_PLATFORM_CONFIG_FILE", config_file)
    monkeypatch.setattr(server, "get_config", lambda: cfg)
    for key in ("_cfg", "_registry", "_llm"):
        monkeypatch.setattr(server, key, None)
    server._init()
    reader = server._registry.get("kubectl")
    assert not reader.allow_write and cfg.runtime.enable_self_healing
    assert server._llm is None and server._cfg.llm.model == "test-model"
    assert server._cfg.observability.grafana_url == "https://dashboard.invalid" and not cfg.observability.grafana_url

    def kubectl(command, namespace):
        assert command == "get services -o json" and namespace == "test"
        return ToolResult(success=True, data='{"items":[]}')

    monkeypatch.setattr(reader, "execute", kubectl)
    assert json.loads(server.k8s_resource_info("list", "services", namespace="test")) == {"items": []}
    with pytest.raises(RuntimeError, match="not configured"):
        server.search_logs("exception", "test")
    with pytest.raises(RuntimeError, match="not configured"):
        server.prometheus_query("up")
    with pytest.raises(ValueError, match="time_range"):
        server.prometheus_query("up", "yesterday")

    def logs(*, query, level, namespace, time_range):
        assert (query, level, namespace, time_range) == ("exception", "warn", "test", "30m")
        return ToolResult(success=True, data={"entries": []})

    def metrics(*, query, query_type, start, end, step):
        assert query == "up" and query_type == "range"
        assert int(end) - int(start) == 15 * 60 and step == "60s"
        return ToolResult(success=True, data={"result_type": "matrix"})

    monkeypatch.setattr(server._registry.get("elasticsearch"), "_execute", logs)
    monkeypatch.setattr(server._registry.get("prometheus"), "_execute", metrics)
    assert json.loads(server.search_logs("exception", "test", "warn", "30m")) == {"entries": []}
    assert json.loads(server.prometheus_query("up"))["result_type"] == "matrix"


def test_stdio_server_startup_and_validation():
    async def check():
        server = os.environ.get('AGENTICSRE_MCP_TEST_SERVER') or str(ROOT / 'mcp_server.py')
        env = dict(os.environ)
        async with stdio_client(StdioServerParameters(command=sys.executable, args=['-u', server], env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)
                tools = await asyncio.wait_for(session.list_tools(), 15)
                assert 'kubectl_run' in {tool.name for tool in tools.tools}
                response = await asyncio.wait_for(session.call_tool('kubectl_run', {}), 15)
                assert response.isError

    asyncio.run(check())


def test_analysis_tools_await_their_backend_and_report_read_failures():
    # Real SDK transport with deterministic backends, independent of cluster and model access.
    bootstrap = '''
from types import SimpleNamespace
import mcp_server as service
import orchestrator.rca_engine as engine
class Agent:
    def __init__(self, *args):
        pass

    async def analyze(self, query, namespace=""):
        return {"query": query, "namespace": namespace}

async def rca(query, *, namespace, config):
    return {"query": query, "namespace": namespace}

class Reader:
    def execute(self, command, namespace):
        return SimpleNamespace(success=command == "read", data={"items": []}, error="reader failed")

service.MetricAgent = service.TraceAgent = service.EventAgent = service.LogAgent = Agent
service._init = lambda: None
service._registry = {"kubectl": Reader()}
engine.run_rca = rca
service.mcp.run(transport="stdio")
'''

    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-c', bootstrap], cwd=ROOT, env=dict(os.environ))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await asyncio.wait_for(client.initialize(), 30)
            args = {'query': 'test question', 'namespace': 'test-ns'}
            for name in ('analyze_metrics', 'analyze_logs', 'analyze_traces', 'analyze_events', 'run_rca_analysis'):
                response = await asyncio.wait_for(client.call_tool(name, args), 15)
                assert not response.isError and json.loads(response.content[0].text) == args, name
            for name, arguments in [('kubectl_run', {'command': 'failed-read'}), ('prometheus_query', {'query': 'up'})]:
                response = await asyncio.wait_for(client.call_tool(name, arguments), 15)
                assert response.isError, name
            response = await asyncio.wait_for(client.call_tool('kubectl_run', {'command': 'read'}), 15)
            assert not response.isError and json.loads(response.content[0].text) == {'items': []}

    asyncio.run(check())


def test_shell_launcher_first_stdout_line_is_an_mcp_response():
    async def check():
        launcher = ROOT / 'run_mcp.sh'
        env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
        proc = await asyncio.create_subprocess_exec(
            'bash', str(launcher), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        try:
            request = {
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'protocol-check', 'version': '1'},
                },
            }
            proc.stdin.write((json.dumps(request) + '\n').encode())
            await proc.stdin.drain()
            first = json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))
            assert first['id'] == 1 and first['result']['serverInfo']['name'] == 'AgenticSRE'
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    asyncio.run(check())
