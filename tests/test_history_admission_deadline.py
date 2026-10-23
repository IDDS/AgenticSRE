"""A policy check must not hold the caller's result/deadline lock (remote only)."""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace

from memory.history_retrieval import HistoryRetrievalGate, retrieve_history
from memory.memory_scope import MemoryScope


def test_blocked_publish_admission_returns_on_budget_without_late_snapshot_mutation(tmp_path, monkeypatch):
    import memory.memory_scope as policy

    monkeypatch.setenv('AGENTICSRE_FEEDBACK_DIR', str(tmp_path / 'feedback'))
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    gate = HistoryRetrievalGate()
    original_filter = policy.filter_memory
    scope = MemoryScope(cluster_id='controlled-cluster', namespace='audit')
    record = dict(rule_id='controlled-rule', condition='pool full', conclusion='pool exhausted',
                  cluster_id=scope.cluster_id, namespace=scope.namespace,
                  status='active', timestamp=scope.as_of - 1)
    query_calls = []

    def rules(*args, **kwargs):
        query_calls.append('rules')
        return [record]

    def faults(*args, **kwargs):
        query_calls.append('faults')
        return []

    # This provider declares scope support and returns promptly. It does not
    # itself call filter_memory: the blocked check is retrieve_history's own
    # detached-result revalidation immediately before publication.
    provider = SimpleNamespace(supports_memory_scope=True,
                               query_similar_rules=rules, query_similar_faults=faults)

    def blocked_admission(rows, request_scope, kind):
        if kind == 'rules':
            entered.set()
            if not release.wait(2):
                raise AssertionError('controlled admission was not released')
        return original_filter(rows, request_scope, kind)

    monkeypatch.setattr(policy, 'filter_memory', blocked_admission)
    outcome = {}

    def call():
        try:
            started = time.monotonic()
            outcome['lookup'] = retrieve_history(lambda: provider, 'pool full',
                timeout_s=.05, gate=gate, memory_scope=scope)
            outcome['elapsed_s'] = time.monotonic() - started
        except BaseException as exc:
            outcome['error'] = exc
        finally:
            returned.set()

    caller = threading.Thread(target=call, daemon=True, name='controlled-history-caller')
    caller.start()
    snapshot = None
    try:
        assert entered.wait(1), 'provider never reached the publication admission check'
        # Generous scheduling margin, independent of the blocked check's two
        # second escape hatch. The old implementation waits on its result lock.
        assert returned.wait(.5), 'history caller exceeded its budget while publication policy held the result lock'
        assert 'error' not in outcome, outcome.get('error')
        result = outcome['lookup']
        assert outcome['elapsed_s'] < .5
        assert result.context['retrieval']['status'] == 'timed_out'
        assert result.context['rules'] == result.context['faults'] == []
        assert result.store is None
        snapshot = deepcopy(result.context)
        # The blocked worker still owns its single-flight lease after the
        # caller returns; another lookup cannot create an additional worker.
        busy = retrieve_history(lambda: provider, 'pool full', timeout_s=.01,
                                gate=gate, memory_scope=scope)
        assert busy.context['retrieval']['status'] == 'busy'
        assert query_calls == ['rules']
    finally:
        # Always release/join, including the expected pre-fix assertion failure.
        # A failing regression must neither hang pytest nor poison later tests.
        release.set()
        caller.join(2)
        acquired = gate.lease.acquire(timeout=2)
        if acquired:
            gate.lease.release()

    assert not caller.is_alive()
    assert acquired, 'history worker did not relinquish its lease after release'
    assert outcome['lookup'].context == snapshot, 'late policy completion mutated the returned snapshot'
    assert query_calls == ['rules'], 'expired publication started a later fault query'
