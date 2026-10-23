"""Bound the caller's wait and the number of in-flight history reads.

A daemon cannot cancel a blocked native/network call. Its lease remains held
until it exits; later callers wait within their own budget and then report busy.
Only snapshots published before the deadline can enter a diagnosis.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Dict


class HistoryRetrievalGate:
    def __init__(self):
        self.lease = threading.BoundedSemaphore(1)


_GATE = HistoryRetrievalGate()


@dataclass
class HistoryLookup:
    context: Dict[str, Any]
    store: Any = None


def empty_history(status: str) -> Dict[str, Any]:
    return {'rules': [], 'faults': [], 'rules_count': 0, 'faults_count': 0,
            'retrieval': {'schema': 'history-retrieval/v1', 'status': status,
                          'components': {}, 'historical_only': True}}


def history_budget_s(timeout_s):
    try:
        budget = float(timeout_s) if not isinstance(timeout_s, bool) else 5.0
        if not math.isfinite(budget):
            budget = 5.0
    except (ValueError, TypeError, OverflowError):
        budget = 5.0
    return min(30.0, max(0.0, budget))


def retrieve_history(factory, query, *, n_rules=5, n_faults=5,
                     timeout_s=5.0, gate=None, memory_scope=None) -> HistoryLookup:
    budget = history_budget_s(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    gate = gate or _GATE
    context = empty_history('timed_out')
    meta = context['retrieval']
    meta.update(budget_s=budget, components={key: {'status': 'not_started'} for key in ('rules', 'faults')})
    if memory_scope is not None:
        meta['scope'] = memory_scope.to_dict()
        meta['admission'] = {}
        meta['admission_policy'] = 'scope-and-time-before-top-k/v1'
    def finish(status, store=None):
        snapshot = deepcopy(context)
        snapshot['retrieval'].update(status=status, elapsed_s=round(time.monotonic() - started, 4))
        return HistoryLookup(snapshot, store if status == 'complete' else None)
    if memory_scope is not None and not memory_scope.complete:
        meta['error_code'] = 'memory_scope_conflict' if memory_scope.conflicted else 'memory_scope_required'
        return finish('blocked_scope')
    if budget == 0:
        return finish('timed_out')
    if not gate.lease.acquire(timeout=budget):
        return finish('busy')
    lock = threading.Lock()
    done = threading.Event()
    cancelled = threading.Event()
    holder = {}

    def publish(key, rows=None, error=False):
        # Admission can read the feedback ledger or wait on its policy cache.
        # Keep that work outside the result lock: the caller must still be able
        # to freeze its timeout snapshot while the worker remains in flight.
        if cancelled.is_set() or time.monotonic() >= deadline:
            return
        valid_rows = isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        audit = None
        if not error and valid_rows and memory_scope is not None:
            from memory.memory_scope import filter_memory
            rows, audit = filter_memory(rows, memory_scope, key)
        with lock:
            if cancelled.is_set() or time.monotonic() >= deadline:
                return
            if error:
                meta['components'][key] = {'status': 'unavailable', 'error_code': 'history_backend_error'}
            else:
                if not valid_rows:
                    meta['components'][key] = {'status': 'unavailable', 'error_code': 'invalid_history_result'}
                    return
                if memory_scope is not None:
                    meta['admission'][key] = audit
                context[key] = deepcopy(rows)
                context[key + '_count'] = len(rows)
                meta['components'][key] = {'status': 'complete', 'count': len(rows)}

    def worker():
        try:
            store = factory()
            with lock:
                holder['store'] = store
            if cancelled.is_set() or time.monotonic() >= deadline:
                return
            if memory_scope is not None and getattr(store, 'supports_memory_scope', False) is not True:
                with lock:
                    meta['error_code'] = 'memory_scope_unsupported'
                for key in ('rules', 'faults'):
                    publish(key, error=True)
                return
            # Older integrations expose one combined method. Call it once.
            if not callable(getattr(store, 'query_similar_rules', None)):
                try:
                    options = {'memory_scope': memory_scope} if memory_scope is not None else {}
                    combined = store.get_historical_context(query, **options)
                    for key in ('rules', 'faults'):
                        publish(key, combined.get(key, []))
                except Exception:
                    for key in ('rules', 'faults'):
                        publish(key, error=True)
                return
            for key, limit in (('rules', n_rules), ('faults', n_faults)):
                if cancelled.is_set() or time.monotonic() >= deadline:
                    break
                with lock:
                    meta['components'][key] = {'status': 'running'}
                try:
                    strict = getattr(store, '_history_query_' + key, None)
                    options = {'memory_scope': memory_scope} if memory_scope is not None else {}
                    rows = (strict or getattr(store, 'query_similar_' + key))(query, n=limit, **options)
                    publish(key, rows)
                except Exception:
                    publish(key, error=True)
        except Exception:
            with lock:
                if not cancelled.is_set() and time.monotonic() < deadline:
                    meta['error_code'] = 'history_initialization_error'
                    for key in ('rules', 'faults'):
                        meta['components'][key] = {'status': 'unavailable'}
        finally:
            gate.lease.release()
            done.set()

    try:
        threading.Thread(target=worker, name='history-retrieval', daemon=True).start()
    except Exception:
        gate.lease.release()
        meta['error_code'] = 'history_worker_start_error'
        return finish('unavailable')
    done.wait(max(0.0, deadline - time.monotonic()))
    with lock:
        cancelled.set()
        states = [item['status'] for item in meta['components'].values()]
        completed = states.count('complete')
        if completed == 2:
            return finish('complete', holder.get('store'))
        timed_out = not done.is_set() or time.monotonic() >= deadline
        if timed_out:
            for item in meta['components'].values():
                if item['status'] in ('running', 'not_started'):
                    item['status'] = 'timed_out'
        return finish('partial' if completed else ('timed_out' if timed_out else 'unavailable'))


def recorded_history(run):
    """Read recorded metadata only; never re-query memory during replay/export."""
    result = run.get('result') or {}
    for source in (result, result.get('result') or {}):
        value = source.get('history_retrieval')
        if isinstance(value, dict):
            return deepcopy(value)
    for event in reversed(run.get('events') or []):
        if event.get('event') == 'history_retrieval' and isinstance(event.get('data'), dict):
            return deepcopy(event['data'])
    return empty_history('not_recorded')['retrieval']
