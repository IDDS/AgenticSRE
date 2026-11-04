from paradigms.evidence_budget import build_evidence_context
from agents.evidence import format_evidence
from eval.paradigm_real_injection_replay import _evidence_contract_status


def test_evidence_beyond_old_limit_survives_when_budget_allows():
    evidence = {'log_agent': {'summary': 'normal\n' * 100 + 'frontend errors=730'},
                'metric_agent': {'summary': 'metric data'}, 'trace_agent': {'summary': 'trace data'}}
    text, truncated = build_evidence_context(evidence)
    assert 'frontend errors=730' in text
    assert truncated == []


def test_no_truncation_when_all_summaries_fit_without_markers():
    evidence = {'metric_agent': {'summary': 'M' * 1916},
                'log_agent': {'summary': 'L' * 1123},
                'trace_agent': {'summary': 'T' * 2581},
                'event_agent': {'summary': 'E' * 78}}
    text, truncated = build_evidence_context(evidence)
    assert truncated == []
    assert len(text) <= 6000
    assert all(row['summary'] in text for row in evidence.values())
    assert '[TRUNCATED:' not in text


def test_exact_budget_fits_but_one_more_character_reports_omission():
    header = '\n[trace_agent]: '
    size = 6000 - len(header) - 1
    text, truncated = build_evidence_context({'trace_agent': {'summary': 'T' * size}})
    assert len(text) == 6000 and truncated == []
    text, truncated = build_evidence_context({'trace_agent': {'summary': 'T' * (size + 1)}})
    assert len(text) <= 6000 and truncated == ['trace_agent']
    assert '[TRUNCATED:' in text


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
    text = format_evidence({'log_agent': {'summary': 'a' * 6000}}, limit=5000)
    assert 'omitted evidence is unknown' in text


def test_reranking_retains_longer_sources_that_fit_and_shares_tight_budget():
    sources = {'metric_agent': {'summary': 'M' * 1800}, 'log_agent': {'summary': 'late error'},
               'trace_agent': {'summary': 'T' * 1100}}
    complete = format_evidence(sources, limit=5000)
    assert 'M' * 1800 in complete and 'T' * 1100 in complete and 'omitted evidence is unknown' not in complete
    tight = format_evidence(sources, limit=1400)
    assert len(tight) <= 1400 and 'late error' in tight
    assert 'M' * 100 in tight and 'T' * 100 in tight
    assert tight.count('omitted evidence is unknown') == 2


def test_required_truncated_evidence_cannot_count_as_complete():
    case = {'evidence_contract': {'required': ['log'], 'supporting': [], 'inapplicable': []}}
    collected = {'log_agent': [{'available': True}]}
    status = _evidence_contract_status(case, collected, ['log_agent'])
    assert status['complete'] is False
    assert status['missing'] == ['log'] and status['truncated'] == ['log']
