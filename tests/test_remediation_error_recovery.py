"""Remote-only regression cases for durable, non-replayed remediation."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from agents.remediation_agent import RemediationAgent
from tools.base_tool import ToolResult
from orchestrator.skill_bridge import AgentSkillBridge
import web_app.app as dashboard


PLAN = {'actions': [
    {'description': 'first', 'command': 'scale deployment/a --replicas=2',
     'rollback_command': 'scale deployment/a --replicas=1'},
    {'description': 'second', 'command': 'scale deployment/b --replicas=2',
     'rollback_command': 'scale deployment/b --replicas=1'},
]}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    calls = []
    failures = set()
    def execute(command, **kwargs):
        calls.append(command)
        return ToolResult(success=command not in failures, data='done', error='injected failure' if command in failures else '')
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(execute=execute) if name == 'kubectl' else None)
    cfg = SimpleNamespace(remediation=SimpleNamespace(enabled=True, require_approval=True, confidence_threshold=.8), llm=None)
    model = SimpleNamespace(json_chat=lambda *a, **kw: copy.deepcopy(PLAN))
    def agent():
        obj=RemediationAgent(model, registry, cfg)
        obj._verify_health=lambda: {'status': 'healthy'}
        return obj
    monkeypatch.setattr(dashboard, '_RCA_HISTORY_FILE', tmp_path/'history.json')
    monkeypatch.setitem(dashboard._state, 'rca_runs', {})
    monkeypatch.setattr(dashboard, '_get_config', lambda: cfg)
    monkeypatch.setattr(dashboard, '_cluster_scoped_config', lambda cluster: cfg)
    import tools
    monkeypatch.setattr(tools, 'build_tool_registry', lambda *a, **kw: registry)
    monkeypatch.setattr(tools, 'LLMClient', lambda *a: model)
    return SimpleNamespace(calls=calls, failures=failures, agent=agent, model=model)


def pending_run():
    run={'id':'test-rem', 'status':'completed', 'namespace':'default', 'cluster_id':'',
         'started_at':1, 'result':{'root_cause':'test'},
         'events':[{'event':'remediation', 'data':{'status':'pending_approval', 'plan':copy.deepcopy(PLAN)}}]}
    dashboard._state['rca_runs'][run['id']]=run
    return run


def test_action_failure_is_not_reported_as_executed(setup):
    setup.failures.add(PLAN['actions'][0]['command'])
    result=asyncio.run(setup.agent().remediate({}, confidence=1, approved=True))
    assert result['status']=='failed'
    assert result['requires_manual_review'] is True
    assert len(setup.calls)==1


def test_empty_plan_is_not_success(setup):
    setup.model.json_chat=lambda *a, **kw: {'actions':[], 'error':'model unavailable'}
    result=asyncio.run(setup.agent().remediate({}, confidence=1, approved=True))
    assert result['status']=='failed'
    assert setup.calls==[]


def test_unhealthy_verification_is_not_execution_success(setup):
    agent=setup.agent()
    agent._verify_health=lambda: {'status':'error', 'error':'health unavailable'}
    result=asyncio.run(agent.remediate({}, confidence=1, approved=True))
    assert result['status'] != 'executed'
    assert result['requires_manual_review'] is True


def test_approval_executes_exact_plan_without_model_regeneration(setup):
    run=pending_run()
    setup.model.json_chat=lambda *a, **kw: {'actions':[dict(PLAN['actions'][0], command='delete pod unapproved')]}
    asyncio.run(dashboard.rca_remediation_approve(run['id']))
    assert setup.calls==[a['command'] for a in PLAN['actions']]


def test_repeated_approval_does_not_reexecute(setup):
    run=pending_run()
    asyncio.run(dashboard.rca_remediation_approve(run['id']))
    with pytest.raises(dashboard.HTTPException) as exc:
        asyncio.run(dashboard.rca_remediation_approve(run['id']))
    assert exc.value.status_code==409
    assert len(setup.calls)==2


def test_rollback_survives_reload_and_runs_once_in_reverse_order(setup):
    run=pending_run()
    asyncio.run(dashboard.rca_remediation_approve(run['id']))
    dashboard._state['rca_runs'].clear()
    dashboard._load_rca_history()
    result=asyncio.run(dashboard.rca_remediation_rollback(run['id']))
    assert result['status']=='rolled_back'
    assert setup.calls[2:]==[a['rollback_command'] for a in reversed(PLAN['actions'])]
    asyncio.run(dashboard.rca_remediation_rollback(run['id']))
    assert len(setup.calls)==4


def test_failed_rollback_is_not_success_or_blindly_retried(setup):
    run=pending_run()
    asyncio.run(dashboard.rca_remediation_approve(run['id']))
    setup.failures.add(PLAN['actions'][1]['rollback_command'])
    result=asyncio.run(dashboard.rca_remediation_rollback(run['id']))
    assert result['status'] != 'rolled_back'
    assert result['requires_manual_review'] is True
    asyncio.run(dashboard.rca_remediation_rollback(run['id']))
    assert len(setup.calls)==3


def test_checkpoint_failure_prevents_writes(setup, monkeypatch):
    run=pending_run()
    monkeypatch.setattr(dashboard, '_save_rca_history', lambda: False)
    with pytest.raises(dashboard.HTTPException) as exc:
        asyncio.run(dashboard.rca_remediation_approve(run['id']))
    assert exc.value.status_code==503
    assert setup.calls==[]


def test_remote_write_timeout_never_replays_locally(monkeypatch):
    bridge=AgentSkillBridge(skills_dir='skill_runtime/skills', runtime_url='http://unavailable')
    monkeypatch.setattr(bridge, '_post', lambda body: (_ for _ in ()).throw(TimeoutError('unknown remote outcome')))
    calls=[]
    monkeypatch.setattr(bridge, '_local', lambda body: calls.append(body) or {})
    with pytest.raises(RuntimeError, match='unknown'):
        bridge.run('test', dry_run=False)
    assert calls==[]


def test_read_only_runtime_failure_still_falls_back(monkeypatch):
    bridge=AgentSkillBridge(skills_dir='skill_runtime/skills', runtime_url='http://unavailable')
    monkeypatch.setattr(bridge, '_post', lambda body: (_ for _ in ()).throw(TimeoutError('timeout')))
    monkeypatch.setattr(bridge, '_local', lambda body: {'status':'completed'})
    result=bridge.run('test', dry_run=True)
    assert result['agent_skill_bridge']['mode']=='local_fallback'


def test_checkpoint_loss_after_write_blocks_further_actions_and_replay(setup, monkeypatch):
    run=pending_run()
    save=dashboard._save_rca_history
    def fail_after_write():
        return False if setup.calls else save()
    monkeypatch.setattr(dashboard, '_save_rca_history', fail_after_write)
    with pytest.raises(dashboard.HTTPException) as exc:
        asyncio.run(dashboard.rca_remediation_approve(run['id']))
    assert exc.value.status_code==503
    assert len(setup.calls)==1
    monkeypatch.setattr(dashboard, '_save_rca_history', save)
    dashboard._state['rca_runs'].clear()
    dashboard._load_rca_history()
    with pytest.raises(dashboard.HTTPException) as exc:
        asyncio.run(dashboard.rca_remediation_approve(run['id']))
    assert exc.value.status_code==409
    result=asyncio.run(dashboard.rca_remediation_rollback(run['id']))
    assert result['error']=='action_outcome_unknown'
    assert len(setup.calls)==1


def test_concurrent_operation_is_rejected_before_writes(setup):
    run=pending_run()
    assert dashboard._RCA_REMEDIATION_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(dashboard.HTTPException) as exc:
            asyncio.run(dashboard.rca_remediation_approve(run['id']))
        assert exc.value.status_code==409
        assert setup.calls==[]
    finally:
        dashboard._RCA_REMEDIATION_LOCK.release()


def test_missing_undo_blocks_entire_plan_before_first_write(setup):
    plan=copy.deepcopy(PLAN)
    plan['actions'][1]['rollback_command']=''
    result=setup.agent().execute_plan(plan)
    assert result['status']=='failed'
    assert setup.calls==[]


def test_unknown_health_component_not_hidden_by_overall_healthy(setup):
    agent=setup.agent()
    agent._verify_health=lambda: {'overall_status':'healthy','checks':{'pods':{'status':'unknown'}}}
    result=agent.execute_plan(PLAN)
    assert result['status']=='verification_failed'
    assert result['requires_manual_review'] is True


def test_failed_forward_action_requires_reconciliation_before_undo(setup):
    agent=setup.agent()
    setup.failures.add(PLAN['actions'][1]['command'])
    result=agent.execute_plan(PLAN)
    assert result['status']=='failed'
    response=agent.rollback(result)
    assert response['error']=='action_outcome_unknown'
    assert len(setup.calls)==2
