import json
import time

import pytest

from orchestrator.skill_followup import collect_followup
from agents.hypothesis_agent import Hypothesis, HypothesisAgent


ROOT = {'cluster_id': 'cluster-a', 'namespace': 'shop', 'kind': 'Deployment',
        'name': 'checkout', 'uid': 'deployment-uid', 'resource_version': '42'}
SCOPE = {'status': 'collected', 'candidate_workloads': ['checkout']}
PENDING = [{'id': 'H2', 'status': 'active', 'proposed_status': 'rejected',
            'validation_errors': ['unverified_rejection']}]


def deployment(uid='deployment-uid', version='42'):
    return {'kind': 'Deployment', 'metadata': {'name': 'checkout', 'namespace': 'shop',
            'uid': uid, 'resourceVersion': version}, 'projection': 'service-evidence/v1'}


def run_followup(runner, **kwargs):
    return collect_followup(namespace='shop', cluster_id='cluster-a', root_entity=ROOT,
        service_scope=SCOPE, hypotheses=PENDING, run_read=runner, **kwargs)


def test_scoped_event_read_is_fenced_by_two_identity_reads():
    calls = []
    def runner(command):
        calls.append(command)
        data = 'No resources found in shop namespace.' if command.startswith('get events') else json.dumps(deployment())
        return {'command': command, 'success': True, 'executed': True, 'data': data, 'sample': data}
    result, reads = run_followup(runner)
    assert result['stop_reason'] == 'followup_collected_review_required'
    assert len(calls) == len(reads) == 3 and calls[0] == calls[2]
    assert 'involvedObject.uid=deployment-uid' in calls[1]
    assert all(row['timestamp'] and row['cluster_id'] == 'cluster-a' for row in reads)
    assert result['execution_authorized'] is False


@pytest.mark.parametrize('change', [{'uid': 'replacement'}, {'version': '43'}])
def test_changed_identity_or_resource_version_stops_without_event_read(change):
    calls = []
    def runner(command):
        calls.append(command)
        return {'command': command, 'success': True, 'executed': True, 'data': json.dumps(deployment(**change))}
    result, reads = run_followup(runner)
    assert result['stop_reason'] == 'target_changed_or_unverifiable'
    assert len(calls) == len(reads) == 1


def test_late_identity_change_is_recorded_and_blocks():
    calls = []
    def runner(command):
        calls.append(command)
        data = 'events' if command.startswith('get events') else json.dumps(deployment(version='43' if len(calls)==3 else '42'))
        return {'command': command, 'success': True, 'executed': True, 'data': data}
    result, reads = run_followup(runner)
    assert result['stop_reason'] == 'target_changed_or_unverifiable' and len(reads) == 3


def test_timeout_does_not_consume_late_result_or_repeat():
    calls = []
    def runner(command):
        calls.append(command)
        time.sleep(.15)
        return {'command': command, 'success': True, 'executed': True, 'data': json.dumps(deployment())}
    result, reads = run_followup(runner, per_read_timeout_s=.01, total_budget_s=.02)
    assert result['stop_reason'] == 'followup_read_timeout' and reads == [] and len(calls) == 1


@pytest.mark.parametrize('root,scope', [({**ROOT, 'uid': 'bad;command'}, SCOPE),
    ({**ROOT, 'namespace': 'elsewhere'}, SCOPE), (ROOT, {'status': 'collected', 'candidate_workloads': ['other']}),
    (ROOT, {'status': 'incomplete', 'candidate_workloads': ['checkout']})])
def test_untrusted_or_ambiguous_target_never_executes(root, scope):
    calls = []
    result, reads = collect_followup(namespace='shop', cluster_id='cluster-a', root_entity=root,
        service_scope=scope, hypotheses=PENDING, run_read=lambda command: calls.append(command))
    assert not result['attempted'] and not calls and not reads


def test_reassessment_cannot_erase_prior_unverified_rejection():
    hyp = Hypothesis('H2', 'candidate requires direct evidence', .4, status='active', proposed_status='rejected',
        validation_errors=['unverified_rejection'], unresolved_questions=['direct evidence required'])
    agent = HypothesisAgent(None)
    agent.set_evidence_catalog([{'id': 'obs-1'}])
    update = {'id': 'H2', 'status': 'active', 'confidence': .4, 'supporting_evidence_ids': ['obs-1'],
              'contradicting_evidence_ids': [], 'unresolved_questions': [], 'next_agents': []}
    result = agent._grounded_updates([hyp], {'hypotheses': [update]}, False, 6)[0]
    assert result.status == 'active' and result.proposed_status == 'rejected'
    assert 'unverified_rejection' in result.validation_errors and result.unresolved_questions
