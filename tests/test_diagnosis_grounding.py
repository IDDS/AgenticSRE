import copy
import json

import pytest

from agents.diagnosis_grounding import apply_alert_grounding, claim_context, evidence_catalog, alert_report_prompt


AID = 'a-' + '1' * 24


def fixture(signal='frozen_metric_observed', observation='latency_ms=350', namespace='ns'):
    payload = {'source': 'alert_evidence_dossier', 'scan_id': 's1', 'revision': 2,
        'group_id': 'g1', 'namespace': 'ns', 'entity': 'pod-a', 'alert_count': 1,
        'alert_samples': [{'alert_id': AID, 'name': 'ProbeLatency', 'message': 'latency_ms=350'}]}
    evidence = {'agent_skill_context': {'payload': payload}, 'metric_agent': {'evidence_items': [{
        'source': 'collector', 'signal_type': signal, 'service': 'pod-a',
        'observation': observation, 'raw_ref': {'namespace': namespace}}]}}
    ref = next(k for k in evidence_catalog(evidence) if k.startswith('ev-'))
    claim = {'id': 'C1', 'kind': 'mechanism', 'statement': 'CPU contention causes delay',
        'supports': [{'evidence_id': ref, 'quote': observation}], 'missing_evidence': []}
    report = {'root_cause': claim['statement'], 'root_entity': {'namespace': 'ns', 'name': 'pod-a', 'kind': 'Pod'},
        'confidence': .99, 'alert_citations': [AID], 'diagnosis_claims': [claim],
        'root_cause_claim_ids': ['C1'], 'remediation_suggestion': 'restart pod-a',
        'evidence_summary': {'metrics': 'CPU confirmed'}, 'reasoning_chain': 'invented CPU claim'}
    judge = {'needs_review': False, 'combined_score': .99, 'review_blockers': [],
        'llm_based': {'claim_reviews': [{'claim_id': 'C1', 'statement': claim['statement'], 'status': 'supported'}]}}
    return evidence, report, judge


@pytest.mark.parametrize('mechanism', ['CPU starvation', 'Kubernetes readiness probe failure',
    'database lock contention', 'memory exhaustion', 'network packet loss'])
def test_high_score_and_valid_quote_do_not_promote_symptoms_to_mechanism(mechanism):
    evidence, report, judge = fixture()
    report['root_cause'] = report['diagnosis_claims'][0]['statement'] = mechanism
    judge['llm_based']['claim_reviews'][0]['statement'] = mechanism
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert quality['needs_review'] and final['diagnosis_status'] == 'requires_review'
    assert mechanism not in final['root_cause'] and final['root_entity'] is None
    assert final['unverified_analysis']['root_cause'] == mechanism
    assert final['claim_validation']['rejected'][0]['reason'] == 'symptoms_do_not_establish_mechanism'
    assert 'restart' not in final['remediation_suggestion']
    assert final['confidence'] == .65 and report['confidence'] == .99


def positive():
    evidence, report, judge = fixture('pod_waiting_state', 'Pod/pod-a current state is ImagePullBackOff.')
    statement = '镜像拉取失败使 pod-a 无法启动；拉取失败的底层原因尚未证实。'
    report['diagnosis_claims'][0]['statement'] = statement
    judge['llm_based']['claim_reviews'][0]['statement'] = statement
    return evidence, report, judge


def test_specific_observed_failure_can_remain_supported_hypothesis():
    evidence, report, judge = positive()
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert not quality['needs_review']
    assert final['diagnosis_status'] == 'supported_hypothesis'
    assert '镜像拉取失败' in final['root_cause'] and 'CPU' not in final['root_cause']
    assert final['claim_validation']['mechanism_confirmed'] is False
    assert final['citation_validation']['status'] == 'passed'


@pytest.mark.parametrize('mutation,reason', [
    ('typo', 'unverified_alert_citations'), ('missing', 'unverified_alert_citations'),
    ('quote', 'unverified_diagnosis_claims'), ('ev_id', 'unverified_diagnosis_claims'),
    ('wrong_namespace', 'unverified_diagnosis_claims'), ('wrong_entity', 'unverified_diagnosis_claims'),
    ('review_statement', 'unverified_diagnosis_claims'), ('review_missing', 'unverified_diagnosis_claims'),
    ('review_duplicate', 'unverified_diagnosis_claims'), ('duplicate', 'unverified_diagnosis_claims'),
    ('root_id', 'mechanism_unconfirmed'), ('review_required', 'mechanism_unconfirmed'),
])
def test_invalid_reference_or_review_never_authorizes_mechanism(mutation, reason):
    evidence, report, judge = positive()
    claim = report['diagnosis_claims'][0]
    if mutation == 'typo': report['alert_citations'] = ['a-' + '2'*24]
    if mutation == 'missing': report.pop('alert_citations')
    if mutation == 'quote': claim['supports'][0]['quote'] = 'CPU=100%'
    if mutation == 'ev_id': claim['supports'][0]['evidence_id'] = 'ev-unobserved'
    if mutation == 'wrong_namespace': evidence['agent_skill_context']['payload']['namespace'] = 'other'
    if mutation == 'wrong_entity': report['root_entity']['name'] = 'other-pod'
    if mutation == 'review_statement': judge['llm_based']['claim_reviews'][0]['statement'] = 'different claim'
    if mutation == 'review_missing': judge['llm_based']['claim_reviews'] = []
    if mutation == 'review_duplicate': judge['llm_based']['claim_reviews'] *= 2
    if mutation == 'duplicate': report['diagnosis_claims'] *= 2
    if mutation == 'root_id': report['root_cause_claim_ids'] = ['C404']
    if mutation == 'review_required': judge['needs_review'] = True
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert quality['needs_review'] and reason in quality['review_blockers']
    assert final['diagnosis_status'] == 'requires_review'


def test_incomplete_membership_does_not_call_unseen_original_id_fabricated():
    evidence, report, judge = positive()
    evidence['agent_skill_context']['payload'].update(raw_alert_ids_total=50, alert_count=50)
    report['alert_citations'] = ['a-' + '3'*24]
    final, _ = apply_alert_grounding(report, judge, evidence)
    assert final['citation_validation']['unresolved_reason'] == 'not_in_available_membership'
    assert final['citation_validation']['cited'] == report['alert_citations']


def test_arbitrary_statement_cannot_hide_inside_observation_quote():
    evidence, report, judge = fixture()
    report['diagnosis_claims'][0]['kind'] = 'observation'
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert final['claim_validation']['observations_rendered_from_quotes'] == ['C1']
    assert final['diagnosis_claims'][0]['statement'] == 'latency_ms=350'
    assert final['diagnosis_claims'][0]['statement_origin'] == 'verbatim_support_quotes'
    assert final['unverified_analysis']['diagnosis_claims'][0]['statement'] == 'CPU contention causes delay'
    assert final['root_cause_claim_ids'] == [] and quality['needs_review']


def test_literal_observation_is_preserved_without_confirming_cause():
    evidence, report, judge = fixture()
    report['diagnosis_claims'][0].update(kind='observation', statement='latency_ms=350')
    report['root_cause_claim_ids'] = []
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert final['claim_validation']['status'] == 'passed'
    assert final['reasoning_chain'] == 'latency_ms=350' and quality['needs_review']


def test_explicit_unknown_hypothesis_can_request_evidence_but_cannot_authorize_cause():
    evidence, report, judge = fixture()
    report['diagnosis_claims'][0].update(kind='hypothesis', supports=[], missing_evidence=['CPU measurements'])
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert final['claim_validation']['status'] == 'passed'
    assert final['diagnosis_claims'][0]['kind'] == 'hypothesis'
    assert final['root_cause_claim_ids'] == [] and quality['needs_review']
    assert 'CPU contention' not in final['root_cause']


@pytest.mark.parametrize('malformed', ['missing_requirements', 'fake_quote'])
def test_uncertainty_label_does_not_exempt_invalid_references(malformed):
    evidence, report, judge = fixture()
    report['diagnosis_claims'][0].update(kind='hypothesis', missing_evidence=['CPU measurements'])
    if malformed == 'missing_requirements': report['diagnosis_claims'][0]['missing_evidence'] = []
    if malformed == 'fake_quote': report['diagnosis_claims'][0]['supports'][0]['quote'] = 'CPU=100%'
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert final['claim_validation']['status'] == 'requires_review' and quality['needs_review']


def test_unconfirmed_alternative_does_not_erase_separately_supported_mechanism():
    evidence, report, judge = positive()
    report['diagnosis_claims'].append({'id': 'C2', 'kind': 'hypothesis',
        'statement': 'underlying configuration may be incorrect; unverified',
        'supports': [], 'missing_evidence': ['configuration and audit history']})
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert final['diagnosis_status'] == 'supported_hypothesis' and not quality['needs_review']
    assert 'configuration' not in final['root_cause']


def test_catalog_excludes_failed_collectors_summaries_and_stays_stable():
    evidence, _, _ = fixture()
    expected = evidence_catalog(evidence)
    evidence['generated'] = {'summary': 'invented mechanism'}
    evidence['failed'] = dict(evidence['metric_agent'], error='failed')
    evidence['duplicate'] = copy.deepcopy(evidence['metric_agent'])
    assert evidence_catalog(evidence) == expected
    assert evidence_catalog(dict(reversed(list(evidence.items())))) == expected


def test_prompt_budget_keeps_valid_json_and_omissions_without_losing_full_catalog():
    evidence, _, _ = fixture()
    evidence['metric_agent']['evidence_items'][0]['observation'] = 'observed ' * 2000
    full = evidence_catalog(evidence)
    context = claim_context(evidence, limit=800)
    assert len(context) <= 800
    packet = json.loads(context)
    assert packet['omitted'] > 0
    assert len(next(v['observation'] for k, v in full.items() if k.startswith('ev-'))) > 10000


def test_non_alert_diagnosis_keeps_legacy_report():
    report, judge = {'root_cause': 'legacy'}, {'needs_review': True}
    final, quality = apply_alert_grounding(report, judge, {})
    assert final is report and quality is judge


def test_report_and_closure_share_one_compact_evidence_contract():
    from orchestrator.diagnosis_closure import DiagnosisClosureManager, ClosurePlan
    evidence, report, _ = positive()
    class Model:
        def json_chat(self, messages, **kwargs):
            self.prompt = messages[1]['content']
            return report
    model = Model()
    DiagnosisClosureManager(model).revise_report('incident', report, evidence, {}, ClosurePlan())
    initial = alert_report_prompt('incident', evidence)
    for prompt in (initial, model.prompt):
        assert prompt.count('观测引用目录：') == 1
        assert prompt.count('"alert_citations":') == 1
        assert '"root_entity":' in prompt
        assert len(prompt) < 6500


def test_claim_reviewer_receives_same_literal_ids_as_report_without_second_catalog():
    from types import SimpleNamespace
    from memory.rca_judge import RCAJudge
    evidence, report, quality = positive()
    class Model:
        def json_chat(self, messages, **kwargs):
            self.messages = messages
            return dict(judge_level=0, coherence_score=.9, evidence_score=.9,
                specificity_score=.9, actionability_score=.9, supported_evidence_ids=['E1'],
                unsupported_claims=[], contradictions=[], **quality['llm_based'])
    model = Model()
    result = RCAJudge(model, SimpleNamespace(memory=SimpleNamespace(judge_threshold=.65))).judge(
        'reasoning', 'root', .9, evidence=evidence, claims=report['diagnosis_claims'])
    refs = {row['evidence_id'] for row in result['review_evidence']}
    assert set(evidence_catalog(evidence)) == refs
    assert all(s['evidence_id'] in refs for c in report['diagnosis_claims'] for s in c['supports'])
    assert 'Claim evidence catalog:' not in model.messages[1]['content']
    final, judge = apply_alert_grounding(report, result, evidence)
    assert final['diagnosis_status'] == 'supported_hypothesis' and not judge['needs_review']
