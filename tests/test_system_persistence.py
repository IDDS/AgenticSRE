"""Persistence and release evidence regressions; run on the remote test host."""
import json

import pytest

import web_app.app as dashboard
from skill_runtime.evolution import SkillDraftGenerator, SkillDraftVerifier


@pytest.fixture
def history(tmp_path, monkeypatch):
    path = tmp_path / 'rca.json'
    monkeypatch.setattr(dashboard, '_RCA_HISTORY_FILE', path)
    monkeypatch.setitem(dashboard._state, 'rca_runs', {})
    return path


def test_running_rca_survives_restart_as_interrupted(history):
    dashboard._state['rca_runs']['rca-test'] = {
        'id': 'rca-test', 'status': 'running', 'started_at': 1,
        'events': [{'event': 'evidence', 'raw_ref': 'trace-123'}],
        'logs': ['collected trace'], 'result': None,
    }
    dashboard._save_rca_history()
    dashboard._state['rca_runs'].clear()
    dashboard._load_rca_history()
    run = dashboard._state['rca_runs']['rca-test']
    assert run['status'] == 'failed'
    assert run['result']['error_code'] == 'process_interrupted'
    assert run['events'][0]['raw_ref'] == 'trace-123'
    assert run['requires_manual_review'] is True


def test_history_replace_failure_keeps_previous_snapshot(history, monkeypatch):
    history.write_text('[{"id":"old","status":"completed"}]')
    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(dashboard.os, 'replace', fail)
    dashboard._save_rca_history()
    assert json.loads(history.read_text())[0]['id'] == 'old'


def test_active_runs_not_evicted_by_completed_history_limit(history):
    dashboard._state['rca_runs'].update({str(i): {
        'id': str(i), 'status': 'completed', 'started_at': i,
    } for i in range(210)})
    dashboard._state['rca_runs']['active'] = {
        'id': 'active', 'status': 'running', 'started_at': -1,
    }
    dashboard._save_rca_history()
    assert any(run['id'] == 'active' for run in json.loads(history.read_text()))


def test_release_flags_without_content_binding_are_not_approval():
    draft = SkillDraftGenerator().draft({'fault_type': 'test'})
    evidence = dict.fromkeys(['unit_tests_passed', 'dry_run_passed',
                             'forward_tests_passed', 'rollback_verified', 'human_approved'], True)
    evidence['publish_target'] = 'review-only'
    assert not SkillDraftVerifier().release_gate(draft, evidence)['publish_ready']


@pytest.mark.parametrize('field', ['skill_py', 'manifest', 'tests', 'directory'])
def test_approved_draft_changes_invalidate_release(field):
    draft = SkillDraftGenerator().draft({'fault_type': 'test'}).to_dict()
    verifier = SkillDraftVerifier()
    evidence = dict.fromkeys(['unit_tests_passed', 'dry_run_passed',
                             'forward_tests_passed', 'rollback_verified', 'human_approved'], True)
    evidence.update(publish_target='review-only', draft_sha256=verifier.release_gate(draft, {})['draft_sha256'])
    assert verifier.release_gate(draft, evidence)['publish_ready']
    if field == 'manifest':
        draft[field]['description'] = 'changed'
    elif field == 'tests':
        draft[field].append('new test')
    else:
        draft[field] += '\n# changed' if field == 'skill_py' else '_changed'
    changed = verifier.release_gate(draft, evidence)
    assert changed['checks']['draft_content_bound'] is False
    assert changed['publish_ready'] is False
