"""Image-pull candidates must not invent a registry absent from the incident."""
import pytest

from agents.hypothesis_agent import HypothesisAgent


@pytest.mark.parametrize('incident', [
    'ImagePullBackOff: Pod/private-api failed to pull registry.example.test/team/api:v1',
    'ErrImagePull: Pod/api lookup registry.invalid: no such host',
    'ImagePullBackOff: Pod/api; registry hostname not yet collected',
])
def test_image_pull_candidates_do_not_introduce_unobserved_docker_hub(incident):
    hypotheses = HypothesisAgent(None).generate(incident)
    assert len(hypotheses) >= 2
    text = '\n'.join(h.description + '\n' + '\n'.join(h.investigation_plan) for h in hypotheses)
    assert all(host not in text.lower() for host in ('docker.io', 'docker hub'))
    assert any(h.fault_type == 'dns_failure' for h in hypotheses)


def test_reranked_dns_candidate_retains_actual_support_without_inventing_endpoint():
    evidence = 'lookup registry.invalid: no such host'

    class Reviewer:
        def json_chat(self, *args, **kwargs):
            return {'hypotheses': [{'id': 'H2', 'confidence': .95, 'status': 'confirmed',
                                   'supporting_evidence': [evidence]}]}

    agent = HypothesisAgent(Reviewer())
    hypotheses = agent.generate('ImagePullBackOff Pod/api: ' + evidence)
    ranked = agent.rerank(hypotheses, {}, allow_branch_expansion=False)
    candidate = next(h for h in ranked if h.id == 'H2')
    assert evidence in candidate.supporting_evidence and candidate.status == 'confirmed'
    assert 'docker.io' not in candidate.description.lower()
