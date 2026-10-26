"""Reproduce the actual HTTP-502/empty-Endpoints YAML evidence integration failure."""
import json
from pathlib import Path
import pytest
import skill_runtime
from skill_runtime.executor import SkillExecutor

SKILLS = Path(skill_runtime.__file__).resolve().parent / 'skills'


@pytest.mark.parametrize('output', ['-o yaml', '-o yml', '--output=yaml', '--output yml'])
@pytest.mark.parametrize('ready', [False, True])
def test_model_requested_yaml_reaches_structured_service_predicate(output, ready):
    runtime = SkillExecutor.from_dir(SKILLS)
    commands = []
    def tool(tool, arguments, payload):
        command = arguments['command']
        commands.append(command)
        obj = {'kind': 'Endpoints', 'metadata': {'name': 'api', 'namespace': 'audit', 'uid': 'endpoint-one'},
               'subsets': [{'addresses': [{'ip': '192.0.2.1'}]}] if ready else []}
        # Model output format is honored by the real command runner. The
        # collector must request JSON for the downstream structural contract.
        body = json.dumps(obj) if 'json' in command else (
            'kind: Endpoints\nmetadata:\n  name: api\n  namespace: audit\nsubsets: ' + json.dumps(obj['subsets']) + '\n')
        return {'success': True, 'executed': True, 'data': body}
    payload = {'namespace': 'audit', 'cluster_id': 'east', 'service': 'api',
               'message': '业务请求 HTTP 502',
               'investigation_commands': [f'kubectl get endpoints api {output}']}
    collected = runtime.run('generic.evidence_collection', payload, stages=['detect', 'diagnose'],
                            dry_run=True, tool_runner=tool).to_dict()
    results = collected['stages']['diagnose']['output']['tool_results']
    assert len(commands) == 1 and 'json' in commands[0] and 'yaml' not in commands[0] and 'yml' not in commands[0]
    payload['agent_evidence'] = {'agent_evidence_collection': {'commands': [
        {**r, 'command': r['args']['command'], 'sample': r.get('data'), 'namespace': 'audit', 'cluster_id': 'east'}
        for r in results]}}
    specific = runtime.run('k8s.service_unavailable', payload, stages=['detect', 'diagnose'],
                           dry_run=True).to_dict()
    assert specific['stages']['detect']['status'] == ('skipped' if ready else 'matched')
    if not ready:
        assert specific['stages']['diagnose']['status'] == 'ok'


def test_cross_namespace_yaml_proposal_stays_blocked():
    runtime = SkillExecutor.from_dir(SKILLS)
    def forbidden(*args): pytest.fail('cross-namespace proposal must never reach a tool')
    result = runtime.run('generic.evidence_collection', {'namespace': 'audit', 'service': 'api',
        'message': 'Check service', 'investigation_commands': ['kubectl get endpoints api -n peer -o yaml']},
        stages=['detect', 'diagnose'], dry_run=True, tool_runner=forbidden).to_dict()
    assert not result['stages']['diagnose']['output']['tool_results']
