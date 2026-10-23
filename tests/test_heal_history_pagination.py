from fastapi.testclient import TestClient
import web_app.app as dashboard


def test_recovery_filter_searches_all_history_before_paginating(monkeypatch):
    runs = [dict(id=f'dry-{i}', dry_run=True, status='dry_run') for i in range(12)]
    runs += [dict(id=f'real-{i}', dry_run=False, success=True,
                  verification={'recovered': True}) for i in range(23)]
    monkeypatch.setattr(dashboard, '_load_heal_runs', lambda: runs)
    client = TestClient(dashboard.app)
    seen = []
    offset = 0
    while offset is not None:
        result = client.get('/api/heal/runs', params={'filter': 'recovered', 'offset': offset, 'limit': 10}).json()
        assert result['total'] == 35
        assert result['filtered_total'] == 23
        assert result['summary']['verified_recovered'] == 23
        assert all(r['verification']['recovered'] for r in result['runs'])
        seen.extend(r['id'] for r in result['runs'])
        offset = result['next_offset']
    assert seen == [f'real-{i}' for i in range(23)]
    assert client.get('/api/heal/runs?filter=failed').json()['runs'] == []
    assert client.get('/api/heal/runs?offset=999').json()['runs'] == []
    assert client.get('/api/heal/runs?offset=-1').status_code == 422
    assert client.get('/api/heal/runs?filter=typo').status_code == 400
