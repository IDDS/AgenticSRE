from paradigms.evidence_budget import build_evidence_context
from agents.evidence import format_evidence
from eval.paradigm_real_injection_replay import _evidence_contract_status


def test_evidence_beyond_old_limit_survives_when_budget_allows():
    evidence = {'log_agent': {'summary': 'normal\n' * 100 + 'frontend errors=730'},
                'metric_agent': {'summary': 'metric data'}, 'trace_agent': {'summary': 'trace data'}}
    text, truncated = build_evidence_context(evidence)
    assert 'frontend errors=730' in text
    assert truncated == []


def test_budget_is_shared_and_omitted_evidence_is_explicit():
    evidence = {'log_agent': {'summary': 'L' * 10000}, 'metric_agent': {'summary': 'metric'},
                'trace_agent': {'summary': 'T' * 10000}}
    text, truncated = build_evidence_context(evidence, max_chars=1200)
    assert len(text) <= 1200
    assert truncated == ['log_agent', 'trace_agent']
    assert '[metric_agent]: metric' in text
    assert text.count('omitted evidence is unknown') == 2
    assert 'L' * 100 in text and 'T' * 100 in text


def test_reranking_receives_later_error_and_marks_longer_omissions():
    summary = 'healthy\n' * 90 + 'frontend errors=730'
    assert 'frontend errors=730' in format_evidence({'log_agent': {'summary': summary}})
    text = format_evidence({'log_agent': {'summary': 'a' * 2000}}, limit=5000)
    assert 'omitted evidence is unknown' in text


def test_required_truncated_evidence_cannot_count_as_complete():
    case = {'evidence_contract': {'required': ['log'], 'supporting': [], 'inapplicable': []}}
    collected = {'log_agent': [{'available': True}]}
    status = _evidence_contract_status(case, collected, ['log_agent'])
    assert status['complete'] is False
    assert status['missing'] == ['log'] and status['truncated'] == ['log']
