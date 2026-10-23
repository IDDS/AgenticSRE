"""History discovery must preserve identity and page boundaries; run remotely."""
import base64
import copy
import json

import pytest
from fastapi.testclient import TestClient

import web_app.app as dashboard


@pytest.fixture
def history(monkeypatch):
    runs = {f'rca-{i:04d}': {
        'id': f'rca-{i:04d}', 'query': f'worker {i} Pending', 'status': 'completed',
        'namespace': 'shop' if i % 2 else 'edge', 'cluster_id': 'east',
        'started_at': 100 + i // 4,
        'result': {'duration_s': 3, 'session_id': f'session-{i}', 'private': 'NOT_IN_LIST'},
        'events': [{'secret_payload': 'NOT_IN_LIST'}],
    } for i in range(57)}
    monkeypatch.setitem(dashboard._state, 'rca_runs', runs)
    return TestClient(dashboard.app), runs


def fetch(client, **params):
    response = client.get('/api/rca/history', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def token(**values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip('=')


def test_default_still_returns_twenty_summaries_with_a_continuation(history):
    client, runs = history
    response = client.get('/api/rca/history')
    data = response.json()
    assert response.status_code == 200
    assert len(data['runs']) == 20 and data['total'] == 57 and data['retained_total'] == 57
    assert data['has_more'] and data['next_cursor']
    assert data['runs'][0]['session_id'].startswith('session-')
    assert 'NOT_IN_LIST' not in response.text and 'events' not in data['runs'][0]
    assert response.headers['cache-control'] == 'private, no-store'


def test_all_retained_runs_reachable_once_with_equal_timestamps(history):
    client, runs = history
    expected = sorted(runs, key=lambda key: (runs[key]['started_at'], key), reverse=True)
    seen, cursor = [], ''
    while True:
        page = fetch(client, limit=7, cursor=cursor)
        seen.extend(row['id'] for row in page['runs'])
        if not page['has_more']:
            assert page['next_cursor'] is None
            break
        cursor = page['next_cursor']
    assert seen == expected and len(set(seen)) == 57


def test_new_run_insertion_does_not_shift_or_duplicate_remaining_pages(history):
    client, runs = history
    first = fetch(client)
    before = set(runs)
    runs['rca-new'] = dict(runs['rca-0000'], id='rca-new', started_at=999)
    second = fetch(client, cursor=first['next_cursor'], limit=100)
    ids = [row['id'] for row in first['runs'] + second['runs']]
    assert set(ids) == before and len(ids) == 57
    assert fetch(client)['runs'][0]['id'] == 'rca-new'


@pytest.mark.parametrize('q,count', [('RCA-0001', 1), ('shop', 28), ('EAST', 57),
                                    ('不存在', 0), ('.*', 0), ('  rca-0001  ', 1)])
def test_literal_case_insensitive_search_includes_id_namespace_cluster(history, q, count):
    client, runs = history
    data = fetch(client, q=q, limit=100)
    assert data['total'] == len(data['runs']) == count
    assert data['retained_total'] == 57


def test_query_unicode_and_cursor_are_bound_to_the_same_search(history):
    client, runs = history
    for row in runs.values():
        row['query'] = '容器重启 调查'
    first = fetch(client, q='容器重启', limit=3)
    assert first['total'] == 57
    assert len(fetch(client, q='容器重启', cursor=first['next_cursor'], limit=100)['runs']) == 54
    assert client.get('/api/rca/history', params={'q': 'Pending', 'cursor': first['next_cursor']}).status_code == 400


@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': -1}, {'limit': 101},
                                   {'q': 'x' * 201}, {'cursor': 'x' * 2049}])
def test_unbounded_or_invalid_request_is_rejected(history, params):
    assert history[0].get('/api/rca/history', params=params).status_code == 422


@pytest.mark.parametrize('cursor', ['not-base64!', token(v=2, before=[1, 'rca-1'], q=''),
    token(v=True, before=[1, 'rca-1'], q=''), token(v=1, before=['NaN', 'rca-1'], q=''),
    token(v=1, before=[float('inf'), 'rca-1'], q=''), token(v=1, before=[True, 'rca-1'], q=''),
    token(v=1, before=[-1, 'rca-1'], q=''), token(v=1, before=[1, ''], q=''),
    token(v=1, before=[1], q=''), token(v=1, before=[1, 'rca-1'], q=[]),
    base64.urlsafe_b64encode(b'[]').decode()])
def test_malformed_cursor_cannot_break_or_broaden_history(history, cursor):
    assert history[0].get('/api/rca/history', params={'cursor': cursor}).status_code == 400


def test_missing_legacy_fields_and_invalid_time_do_not_break_listing(history):
    client, runs = history
    runs.clear()
    for i, timestamp in enumerate([None, 'bad', float('nan'), -1, True]):
        runs[str(i)] = {'id': str(i), 'started_at': timestamp, 'result': None}
    data = fetch(client)
    assert len(data['runs']) == 5 and all(row['started_at'] == 0 for row in data['runs'])
    assert all(row['status'] == 'unknown' and row['query'] == '' for row in data['runs'])


def test_history_queries_never_mutate_saved_diagnosis_or_trigger_collection(history, monkeypatch):
    client, runs = history
    original = copy.deepcopy(runs)
    def forbidden(*args, **kwargs):
        raise AssertionError('history must not start collection or persistence')
    monkeypatch.setattr(dashboard, '_get_pipeline', forbidden)
    monkeypatch.setattr(dashboard, '_save_rca_history', forbidden)
    fetch(client, q='worker')
    assert runs == original


def test_empty_history_is_explicit_and_terminal(history):
    client, runs = history
    runs.clear()
    data = fetch(client)
    assert data['runs'] == [] and data['total'] == 0 and data['retained_total'] == 0
    assert not data['has_more'] and data['next_cursor'] is None
