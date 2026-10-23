from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.event_agent import EventAgent
from orchestrator.skill_bridge import build_skill_payload
from skill_runtime.executor import SkillExecutor
from skill_runtime.workflow import SkillWorkflow


SKILLS = Path(__file__).resolve().parents[1] / 'skill_runtime' / 'skills'


def observed_job(namespace='batch', owner='probe', code=7, reason='Error'):
    pod = {'metadata': {'name': 'probe-pod', 'namespace': namespace,
                        'ownerReferences': [{'kind': 'Job', 'name': owner, 'uid': 'job-uid', 'controller': True}]},
           'status': {'phase': 'Failed', 'containerStatuses': [{'name': 'main', 'state': {
               'terminated': {'exitCode': code, 'reason': reason}}}]}}
    tool = SimpleNamespace(execute=lambda **kw: SimpleNamespace(success=True, data={'items': [pod]}))
    agent = EventAgent(None, SimpleNamespace(get=lambda name: tool))
    items = agent._build_current_state_evidence_items({}, agent._fetch_problem_pods(namespace))
    return {'event_agent': {'evidence_items': items}}


@pytest.mark.parametrize('allow_legacy', [True, False])
def test_job_bridge_routes_to_job_skill_with_owned_exit_evidence(allow_legacy):
    payload = build_skill_payload('调查 job/probe 告警', 'batch', observed_job())
    assert payload['job'] == 'probe'
    assert payload['fault_type'] == 'job_failure_investigation'
    result = SkillWorkflow(SkillExecutor.from_dir(SKILLS)).run(payload, domain='k8s', allow_legacy_fallback=allow_legacy)
    assert result['skill_runtime']['primary_skill_id'] == 'k8s.job_failed'
    assert 'k8s.service_unavailable' not in [r['skill']['id'] for r in result['skill_runtime']['runs']]
    assert result['diagnosis']['fault_type'] == 'job_failed'
    assert '7' in result['diagnosis']['root_cause']
    assert all('scale ' not in str(a) and 'delete ' not in str(a) for a in result['execution_plan'].values())


@pytest.mark.parametrize('namespace,owner,code', [('other','probe',7), ('batch','unrelated',7), ('batch','probe',0)])
def test_job_skill_rejects_unrelated_or_successful_exit(namespace, owner, code):
    payload = build_skill_payload('调查 job/probe 告警', 'batch', observed_job(namespace, owner, code))
    run = SkillExecutor.from_dir(SKILLS).run('k8s.job_failed', payload).to_dict()
    assert run['stages']['diagnose']['output']['fault_type'] == 'unknown'
    assert run['stages']['diagnose']['confidence'] == 0
    assert not run['policy']['supports_heal']
    assert 'heal' not in run['stages']


@pytest.mark.parametrize('query,expected', [
    ('Service frontend unavailable service_unavailable', 'service_unavailable'),
    ('Pod frontend ImagePullBackOff', 'image_pull_error'),
    ('Pod frontend OOMKilled', 'oom_killed'),
    ('Pod frontend CrashLoopBackOff', 'pod_crashloop'),
    ('job/probe ImagePullBackOff', 'image_pull_error'),
    ('job/probe OOMKilled', 'oom_killed'),
])
def test_existing_fault_routes_are_unchanged(query, expected):
    payload = build_skill_payload(query, 'batch')
    assert payload['fault_type'] == expected


def test_ambiguous_or_unsafe_job_targets_do_not_route():
    for query in ('compare job/first and job/second', 'job/probe;delete', 'prefixjob/probe'):
        assert not build_skill_payload(query, 'batch').get('job')


def test_explicit_service_context_is_not_overridden_by_job_mention():
    payload = build_skill_payload('job/probe impacts service', 'batch', {'payload': {'fault_type': 'service_unavailable', 'service': 'frontend'}})
    assert payload['fault_type'] == 'service_unavailable'


@pytest.mark.parametrize('missing', ['job_name', 'job_uid', 'container', 'namespace'])
def test_missing_owner_or_container_identity_cannot_confirm_job_failure(missing):
    evidence = observed_job()
    evidence['event_agent']['evidence_items'][0]['raw_ref'].pop(missing, None)
    payload = build_skill_payload('调查 job/probe 告警', 'batch', evidence)
    result = SkillExecutor.from_dir(SKILLS).run('k8s.job_failed', payload).to_dict()
    assert result['stages']['diagnose']['output']['fault_type'] == 'unknown'


def test_recalled_hypothesis_is_not_current_job_exit_evidence():
    evidence = {'agent_workflow': {'hypotheses': [{'description': 'Job probe exit code 7'}]}}
    payload = build_skill_payload('调查 job/probe 告警', 'batch', evidence)
    result = SkillExecutor.from_dir(SKILLS).run('k8s.job_failed', payload).to_dict()
    assert result['stages']['diagnose']['confidence'] == 0
