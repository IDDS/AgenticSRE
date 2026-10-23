import copy

from eval.alert_compression_acceptance import record, score_partition
from agents.alert_compression import compress_alerts


def test_acceptance_detects_false_merge_independently_of_algorithm():
    records = [record(), record()]
    assert not score_partition(records, ['different-a', 'different-b'], compress_alerts(records))['passed']


def test_acceptance_detects_duplicate_or_missing_membership():
    records = [record(), record()]
    result = compress_alerts(records)
    result['groups'][0]['alert_indices'] = [0, 0]
    assert not score_partition(records, ['same', 'same'], result)['partition_valid']


def test_acceptance_detects_raw_evidence_changes():
    records = [record()]
    result = compress_alerts(records)
    result['raw_alerts'][0]['raw']['message'] = 'edited'
    assert not score_partition(records, ['one'], result)['passed']


def test_acceptance_detects_hidden_critical_alert():
    records = [record(severity='critical')]
    result = compress_alerts(records)
    result['groups'][0]['severity'] = 'warning'
    assert not score_partition(records, ['one'], result)['passed']


def test_pair_metrics_measure_actual_pairs_and_no_pair_is_not_perfect_precision():
    records = [record(), record(), record()]
    result = score_partition(records, ['a', 'a', 'b'], compress_alerts(records))
    assert result['false_merge_pairs'] == 2
    assert result['pair_precision'] == 1/3
    singleton = score_partition(records[:1], ['a'], compress_alerts(records[:1]))
    assert singleton['pair_precision'] is None and singleton['pair_recall'] is None
