import copy

from eval.alert_rca_replay import frozen_domains, inspect_result


def fixture():
    group = dict(namespace='ns', entity='pod-a', entity_kind='pod', cluster_id='c',
                 group_id='g-1', alert_indices=[0], alert_ids=['a-'+'1'*24])
    scan = dict(scan_id='scan-1', revision=1, created_at=10,
        raw_alerts=[dict(source='prometheus', timestamp=9, name='ProbeError', severity='warning',
                        message='request errors observed', alert_id=group['alert_ids'][0]),
                    dict(source='prometheus', timestamp=9, name='other', severity='critical',
                         message='unrelated', alert_id='a-'+'2'*24)],
        metric_evidence={'groups': {'g-1': {'evidence_id': 'm-1', 'query': 'error_ratio',
                                           'points': [[8, 0], [9, 1]]}}},
        window_evidence={'proofs': [{'spans': [
            dict(entity_ref='c|ns|pod|pod-a', trace_id='t-1', span_id='s-1', start=8, error=True),
            dict(entity_ref='c|other|pod|pod-a', trace_id='t-2', span_id='s-2', start=8, error=True)]}]})
    return scan, group


def test_frozen_evidence_keeps_identity_extrema_and_excludes_other_entities():
    scan, group = fixture()
    before = copy.deepcopy(scan)
    domains = frozen_domains(scan, group)
    assert scan == before
    metrics = domains['metric_agent']['evidence_items']
    assert len(metrics) == 2
    assert metrics[0]['raw_ref']['alert_id'] == group['alert_ids'][0]
    assert metrics[1]['raw_ref']['points'] == [[8, 0], [9, 1]]
    spans = domains['trace_agent']['evidence_items']
    assert len(spans) == 1 and spans[0]['raw_ref']['trace_id'] == 't-1'
    assert all(domains[name]['error'] and domains[name]['evidence_items'] == []
               for name in ('log_agent', 'event_agent', 'llm_inference_agent'))


def test_absent_spans_remain_unavailable_not_healthy():
    scan, group = fixture()
    scan['window_evidence'] = {'proofs': []}
    result = frozen_domains(scan, group)['trace_agent']
    assert result['error'] and not result['evidence_items']
    assert '不表示正常' in result['summary']


def test_span_budget_marks_truncation_and_removes_duplicates():
    scan, group = fixture()
    spans = [dict(entity_ref='c|ns|pod|pod-a', trace_id=f't-{i}', span_id=f's-{i}', start=8)
             for i in range(35)]
    scan['window_evidence'] = {'proofs': [{'spans': spans}, {'spans': spans}]}
    result = frozen_domains(scan, group)['trace_agent']
    assert len(result['evidence_items']) == 30 and result['truncated']


def test_completed_pipeline_is_not_causal_accuracy_or_citation_proof():
    label = {'entity': 'pod-a', 'namespace': 'ns', 'alert_ids': ['a-'+'1'*24]}
    result = {'status': 'completed', 'result': {'status': 'completed', 'result': {
        'root_entity': {'namespace': 'ns', 'name': 'pod-a'},
        'reasoning_chain': 'observed a-'+'2'*24}, 'judge': {'needs_review': False}}}
    row = inspect_result(result, [{'provider': {}}], label)
    assert row['pipeline_completed'] and row['root_entity_matches_target']
    assert row['unknown_literal_alert_citations'] == ['a-'+'2'*24]
    assert not row['provider_complete'] and row['input_tokens'] is None
    assert row['mechanism_accuracy'] is None


def test_usage_unknown_stays_unknown_instead_of_zero():
    row = inspect_result({}, [], {'entity': 'pod-a', 'namespace': 'ns', 'alert_ids': []})
    assert row['input_tokens'] is None and row['output_tokens'] is None
    assert not row['pipeline_completed'] and not row['provider_complete']
