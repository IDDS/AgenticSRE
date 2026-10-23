"""Dashboard regressions for live inventory and cross-node resource pressure."""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

import web_app.app as dashboard


def test_inventory_excludes_terminal_pods_at_source_but_keeps_unhealthy_live_pods(monkeypatch):
    pods = [
        {'metadata': {'name': name}, 'status': {'phase': phase},
         'spec': {'containers': [{'name': 'model'}]}}
        for name, phase in [('serving', 'Running'), ('pending', 'Pending'), ('unknown', 'Unknown'),
                            ('old-evicted', 'Failed'), ('old-job', 'Succeeded')]
    ]
    calls = []

    def shell(command, timeout, cluster_id):
        calls.append((command, cluster_id))
        items = pods if 'get pods' in command else []
        if '--field-selector' in command:
            items = [p for p in items if p['status']['phase'] not in ('Succeeded', 'Failed')]
        return {'ok': True, 'stdout': json.dumps({'items': items}), 'stderr': ''}

    monkeypatch.setattr(dashboard, '_shell_sync', shell)
    result = asyncio.run(dashboard._collect_ai_k8s_inventory({
        'id': 'ai-selected', 'context': 'shared-name', 'default_namespace': 'llm-k8s',
    }))

    assert [p['name'] for p in result['pods']] == ['serving', 'pending', 'unknown']
    pod_command = next(c for c, _ in calls if 'get pods' in c)
    assert 'status.phase!=Succeeded,status.phase!=Failed' in pod_command
    assert all(cluster == 'ai-selected' for _, cluster in calls)
    assert result['pod_scope'] == 'non_terminal'


@pytest.mark.parametrize('reverse', [False, True])
def test_dashboard_reports_highest_latest_node_usage_with_source(monkeypatch, tmp_path, reverse):
    cluster = {'id':'ai-selected','kind':'ai','default_namespace':'llm-k8s',
               'prometheus_url':'http://metrics','teller_trace_dir':str(tmp_path)}
    monkeypatch.setattr(dashboard, '_find_k8s_cluster', lambda _: cluster)
    monkeypatch.setattr(dashboard, '_reachable_cluster_prometheus_url', lambda *args: 'http://metrics')
    monkeypatch.setattr(dashboard, '_prometheus_api_ok', lambda _: True)

    async def charts(url, specs, **kwargs):
        series = [
            {'label':'dds-127','metric':{'instance':'dds-127'},'timestamps':[1,2],'values':[99,61.7]},
            {'label':'dds-128','metric':{'instance':'dds-128'},'timestamps':[1,2],'values':[50,94.9]},
        ]
        return {key: {'series': list(reversed(series)) if reverse else series} for key in specs}

    async def inventory(cluster):
        return {'available':True,'nodes':[],'pods':[],'services':[]}

    monkeypatch.setattr(dashboard, '_query_metric_charts', charts)
    monkeypatch.setattr(dashboard, '_collect_ai_k8s_inventory', inventory)
    monkeypatch.setattr(dashboard, '_summarize_prometheus_inventory', lambda _: {})
    monkeypatch.setattr(dashboard, '_summarize_teller_trace', lambda *args, **kwargs: {})
    monkeypatch.setattr(dashboard, '_build_teller_vllm_integration_plan', lambda *args, **kwargs: {})

    response = TestClient(dashboard.app).get(
        '/api/ai-clusters/ai-selected/dashboard', params={'teller_trace_dir': str(tmp_path)})

    assert response.status_code == 200, response.text
    body = response.json()
    for key in ['node_cpu_usage', 'node_memory_usage', 'node_rootfs_usage']:
        assert body['summary'][key] == 94.9
        assert body['summary_nodes'][key] == 'dds-128'


@pytest.mark.parametrize('values,expected', [
    ([None, float('nan'), float('inf'), 'bad'], None),
    ([None, 0, float('-inf')], 0),
])
def test_highest_latest_node_ignores_missing_and_nonfinite_samples(values, expected):
    chart = {'usage': {'series': [
        {'label':f'node-{i}', 'values':[99, value]} for i, value in enumerate(values)
    ]}}
    result = dashboard._chart_highest_latest(chart, 'usage')
    assert result['value'] == expected
    assert result['node'] == ('node-1' if expected == 0 else '')
