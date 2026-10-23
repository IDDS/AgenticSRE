"""History availability must be observable; execute on the remote test host."""
from types import SimpleNamespace
from pathlib import Path
import threading
import time
import pytest

from memory.history_retrieval import HistoryRetrievalGate, retrieve_history, history_budget_s, recorded_history

from memory.fault_context_store import FaultContextStore
from orchestrator.skill_agent import SkillAgent, SkillAgentRequest


def test_combined_lookup_does_not_disguise_backend_failure_as_empty_success():
    store = object.__new__(FaultContextStore)
    store._backend = 'chromadb'
    store.mem_cfg = SimpleNamespace(max_similar_results=5)
    class Broken:
        def count(self):
            raise RuntimeError('controlled backend failure')
    store._rules_col = store._faults_col = Broken()
    result = store.get_historical_context('Pod Pending')
    assert result.get('retrieval', {}).get('status') == 'unavailable'


def test_skill_provider_failure_is_not_reported_as_success():
    def broken(request):
        raise RuntimeError('controlled provider failure')
    agent = SkillAgent(registry=SimpleNamespace(), skills_dir=Path(__file__).resolve().parents[1] / 'skill_runtime/skills', context_provider=broken)
    agent._retrieve_context(SkillAgentRequest(query='Pod Pending'), {})
    event = next(item for item in agent.events if item['event'] == 'context')
    assert event['success'] is False
    assert event['history_retrieval']['status'] == 'unavailable'
    assert agent._compact_context_for_evidence([])['history_retrieval']['status'] == 'unavailable'


class Store:
    def __init__(self):
        self.calls = []
    def query_similar_rules(self, query, n=5):
        self.calls.append('rules')
        return [{'text': 'historical rule'}]
    def query_similar_faults(self, query, n=5):
        self.calls.append('faults')
        return []


def lookup(factory, **kwargs):
    return retrieve_history(factory, 'Pod Pending', gate=HistoryRetrievalGate(), **kwargs)


def test_complete_snapshot_reused_without_duplicate_lookup():
    from memory.context_builder import ContextBuilder
    store = Store()
    result = lookup(lambda: store)
    assert result.store is store
    ctx = ContextBuilder(fault_store=store).build_context('Pod Pending', historical_context=result.context)
    assert store.calls == ['rules', 'faults']
    assert ctx.history_retrieval['status'] == 'complete'
    ctx.historical_rules[0]['text'] = 'mutated'
    assert result.context['rules'][0]['text'] == 'historical rule'


@pytest.mark.parametrize('broken', ['rules', 'faults'])
def test_one_failure_preserves_other_component_without_leaking_exception(broken):
    store = Store()
    def fail(*args, **kwargs):
        raise RuntimeError('sensitive-url-or-token-must-not-escape')
    setattr(store, 'query_similar_' + broken, fail)
    result = lookup(lambda: store)
    assert result.store is None
    assert result.context['retrieval']['status'] == 'partial'
    assert result.context['retrieval']['components'][broken]['error_code'] == 'history_backend_error'
    assert 'sensitive' not in str(result)


@pytest.mark.parametrize('block_at', ['initialization', 'rules', 'faults'])
def test_timeout_keeps_lease_and_late_results_cannot_change_snapshot(block_at):
    entered, release = threading.Event(), threading.Event()
    store, gate = Store(), HistoryRetrievalGate()
    def block(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [{'text': 'late result'}]
    def factory():
        if block_at == 'initialization':
            block()
        return store
    if block_at != 'initialization':
        setattr(store, 'query_similar_' + block_at, block)
    try:
        start = time.monotonic()
        result = retrieve_history(factory, 'Pod Pending', timeout_s=.08, gate=gate)
        assert entered.is_set() and time.monotonic() - start < .7
        assert result.context['retrieval']['status'] == ('partial' if block_at == 'faults' else 'timed_out')
        assert result.store is None
        saved = str(result.context)
        def forbidden():
            raise AssertionError('busy gate must not start another worker')
        busy = retrieve_history(forbidden, 'Pod Pending', timeout_s=.02, gate=gate)
        assert busy.context['retrieval']['status'] == 'busy'
    finally:
        release.set()
    # Acquiring the lease proves the actual worker exited, without polling.
    assert gate.lease.acquire(timeout=1)
    gate.lease.release()
    assert str(result.context) == saved
    recovered = retrieve_history(lambda: Store(), 'Pod Pending', gate=gate)
    assert recovered.context['retrieval']['status'] == 'complete'


def test_initialization_failure_is_explicit_and_safe():
    def fail():
        raise RuntimeError('sensitive-configuration')
    result = lookup(fail)
    assert result.context['retrieval']['status'] == 'unavailable'
    assert result.context['retrieval']['error_code'] == 'history_initialization_error'
    assert 'sensitive' not in str(result)


def test_zero_budget_never_starts_initialization():
    def forbidden():
        raise AssertionError('zero budget')
    assert lookup(forbidden, timeout_s=0).context['retrieval']['status'] == 'timed_out'


@pytest.mark.parametrize('value,expected', [(None, 5), ('bad', 5), (float('inf'), 5),
    (float('nan'), 5), (True, 5), (-1, 0), (1000, 30), (.5, .5)])
def test_invalid_budget_cannot_create_unbounded_wait(value, expected):
    assert history_budget_s(value) == expected


def test_worker_start_failure_releases_lease(monkeypatch):
    gate = HistoryRetrievalGate()
    def fail(*args):
        raise RuntimeError('controlled start failure')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    result = retrieve_history(Store, 'Pod Pending', gate=gate)
    assert result.context['retrieval']['status'] == 'unavailable'
    assert gate.lease.acquire(timeout=.01)
    gate.lease.release()


def test_replay_uses_recorded_status_and_cannot_invent_legacy_success():
    assert recorded_history({'result': {}})['status'] == 'not_recorded'
    old = {'schema': 'history-retrieval/v1', 'status': 'timed_out'}
    run = {'result': {}, 'events': [{'event': 'history_retrieval', 'data': old}]}
    value = recorded_history(run)
    assert value == old
    value['status'] = 'complete'
    assert old['status'] == 'timed_out'


def test_report_and_html_preserve_failure_metadata(monkeypatch):
    import asyncio
    import web_app.app as dashboard
    run = {'id': 'controlled-history-budget', 'status': 'completed', 'result': {},
           'events': [{'event': 'history_retrieval', 'data': {'status': 'timed_out', 'historical_only': True}}]}
    monkeypatch.setitem(dashboard._state['rca_runs'], run['id'], run)
    assert dashboard._build_report(run)['history_retrieval']['status'] == 'timed_out'
    html = asyncio.run(dashboard.export_report(run['id'])).body.decode()
    assert '历史知识检索' in html and 'timed_out' in html
