import copy
import json

import pytest

from agents.diagnosis_grounding import resolve_quote_selections, claim_context, apply_alert_grounding, evidence_catalog
from test_diagnosis_grounding import positive


def test_explicit_selection_expands_exact_displayed_text_before_review_and_is_repeatable():
    evidence, report, judge = positive()
    support = report['diagnosis_claims'][0]['supports'][0]
    support.pop('quote'); support['quote_index'] = 0
    before = copy.deepcopy(report)
    resolved = resolve_quote_selections(report, evidence)
    expected = next(r['observation'] for r in json.loads(claim_context(evidence))['observations'] if r['id'] == support['evidence_id'])
    picked = resolved['diagnosis_claims'][0]['supports'][0]
    assert picked['quote'] == expected and picked['quote_origin'] == 'selected_catalog_excerpt'
    assert report == before and resolve_quote_selections(resolved, evidence) == resolved
    final, quality = apply_alert_grounding(resolved, judge, evidence)
    assert not quality['needs_review'] and final['diagnosis_status'] == 'supported_hypothesis'


@pytest.mark.parametrize('mutation', ['unknown_id', 'wrong_index', 'boolean_index', 'quote_and_index', 'undisplayed'])
def test_invalid_selection_cannot_pass_a_high_model_review(mutation):
    evidence, report, judge = positive()
    support = report['diagnosis_claims'][0]['supports'][0]
    support.pop('quote'); support['quote_index'] = 0
    if mutation == 'unknown_id': support['evidence_id'] = 'ev-nonexistent'
    if mutation == 'wrong_index': support['quote_index'] = 1
    if mutation == 'boolean_index': support['quote_index'] = False
    if mutation == 'quote_and_index': support['quote'] = 'a differently written quote'
    if mutation == 'undisplayed':
        # Existing fact stays in the full catalog but cannot fit the displayed scope.
        evidence['metric_agent']['evidence_items'][0]['service'] = 'x' * 9000
        support['evidence_id'] = next(k for k in evidence_catalog(evidence) if k.startswith('ev-'))
    resolved = resolve_quote_selections(report, evidence)
    assert resolved['diagnosis_claims'][0]['supports'][0]['quote_selection_error']
    final, quality = apply_alert_grounding(resolved, judge, evidence)
    assert quality['needs_review'] and final['diagnosis_status'] == 'requires_review'


def test_wrong_legacy_free_text_is_not_silently_repaired():
    evidence, report, judge = positive()
    report['diagnosis_claims'][0]['supports'][0]['quote'] = 'ImagePullBackOff: wrongly combined text'
    assert resolve_quote_selections(report, evidence) == report
    final, quality = apply_alert_grounding(report, judge, evidence)
    assert quality['needs_review'] and final['claim_validation']['status'] == 'requires_review'


def test_selection_does_not_promote_alert_source_or_bypass_individual_review():
    evidence, report, judge = positive()
    support = report['diagnosis_claims'][0]['supports'][0]
    support.pop('quote'); support['quote_index'] = 0
    support['evidence_id'] = evidence['agent_skill_context']['payload']['alert_samples'][0]['alert_id']
    resolved = resolve_quote_selections(report, evidence)
    final, _ = apply_alert_grounding(resolved, judge, evidence)
    assert final['claim_validation']['rejected'][0]['reason'] == 'symptoms_do_not_establish_mechanism'
    evidence, report, judge = positive()
    support = report['diagnosis_claims'][0]['supports'][0]
    support.pop('quote'); support['quote_index'] = 0
    judge['llm_based']['claim_reviews'] = []
    final, _ = apply_alert_grounding(resolve_quote_selections(report, evidence), judge, evidence)
    assert final['claim_validation']['rejected'][0]['reason'] == 'mechanism_not_individually_reviewed'
