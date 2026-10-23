"""Deterministic labelled compression acceptance; run on the deployment host.

Labels describe merge-compatible observations, never proven incident root causes.
Truth is held separately and is never supplied to the compressor.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import random
import statistics
import tempfile
import time

from agents.alert_compression import CompressionStore, compress_alerts


def choose2(n):
    return n * (n - 1) // 2


def score_partition(records, truth, result):
    if len(records) != len(truth):
        raise ValueError('one external label per input record is required')
    indices = [i for g in result['groups'] for i in g['alert_indices']]
    partition_valid = sorted(indices) == list(range(len(records)))
    if not partition_valid:
        return {'passed': False, 'partition_valid': False}
    predicted = sum(choose2(len(g['alert_indices'])) for g in result['groups'])
    true_positive = sum(sum(choose2(n) for n in Counter(truth[i] for i in g['alert_indices']).values()) for g in result['groups'])
    actual = sum(choose2(n) for n in Counter(truth).values())
    critical = {i for i, r in enumerate(records) if r.get('severity') == 'critical'}
    covered_critical = {i for g in result['groups'] if g['severity'] == 'critical' for i in g['alert_indices'] if i in critical}
    preserved = [r['raw'] for r in result['raw_alerts']] == records
    false_pairs = predicted - true_positive
    return {'passed': partition_valid and preserved and not false_pairs and critical == covered_critical,
            'partition_valid': True, 'raw_exactly_preserved': preserved,
            'input': len(records), 'groups': len(result['groups']), 'expected_groups': len(set(truth)),
            'compression_ratio': 1 - len(result['groups']) / len(records) if records else 0,
            'false_merge_pairs': false_pairs, 'false_merge_pair_rate': false_pairs / predicted if predicted else None,
            'pair_precision': true_positive / predicted if predicted else None,
            'pair_recall': true_positive / actual if actual else None,
            'missed_compatible_pairs': actual - true_positive,
            'critical_input': len(critical), 'critical_visible_in_groups': len(covered_critical)}


def record(**changes):
    return dict({'name': 'ApplicationLog', 'source': 'logs', 'timestamp': 1700000000,
                 'cluster_id': 'c1', 'namespace': 'shop', 'labels': {'pod': 'api'},
                 'message': 'database connection refused', 'severity': 'warning'}, **changes)


def datasets():
    rows, truth = [], []
    for family in range(100):
        for i in range(100):
            rows.append(record(name='DependencyUnavailable', labels={'pod': f'api-{family}'},
                               timestamp=1700000000+i, severity='critical' if i == 50 else 'warning'))
            truth.append(f'pod-{family}')
    yield 'burst_10000', rows, truth

    rows, truth = [], []
    for i in range(10000):
        rows.append(record(labels={'pod': f'pod-{i}'}, severity='critical' if i % 1000 == 0 else 'warning'))
        truth.append(str(i))
    yield 'independent_10000', rows, truth

    rows, truth = [], []
    for family, template in enumerate((
        'request {rid} failed after {n} ms contacting db',
        'request {rid} succeeded after {n} ms contacting db',
        'request {rid} not failed after {n} ms contacting db',
        'request {rid} failed after {n} ms HTTP 503',
        'request {rid} failed after {n} ms HTTP 404',
        'version 1 request {rid} failed after {n} ms',
        'version 2 request {rid} failed after {n} ms',
    )):
        for i in range(100):
            rows.append(record(message=template.format(rid=f'{i:08x}-1234-1234-1234-123456789abc', n=i+1), timestamp=1700000000+i))
            truth.append(f'template-{family}')
    for i in range(50):
        rows.append(record(message=f'GET /orders/{i} returned HTTP 503'))
        truth.append(f'path-{i}')
    yield 'template_contrasts', rows, truth

    rows, truth = [], []
    for cluster in ('c1', 'c2'):
        for namespace in ('shop', 'payments'):
            for target in ('backup', 'invoice'):
                for container in ('main', 'sidecar'):
                    for state in ('firing', 'resolved'):
                        for i in range(8):
                            rows.append(record(name='KubeJobFailed', source='prometheus', cluster_id=cluster, namespace=namespace,
                                               state=state, timestamp=1700000000+i,
                                               labels={'job_name': target, 'pod': 'kube-state-metrics', 'container': container}, message=''))
                            truth.append('|'.join((cluster, namespace, target, container, state)))
    yield 'scope_and_state_boundaries', rows, truth

    rows, truth = [], []
    for family, stamps in enumerate(((0, 299, 300), (301, 599, 601), (602, 901, 902))):
        for t in stamps:
            rows.append(record(timestamp=1700000000+t))
            truth.append(str(family))
    for i, fields in enumerate(({'timestamp': None}, {'cluster_id': ''}, {'namespace': ''}, {'labels': {}})):
        for j in range(2):
            rows.append(record(**fields))
            truth.append(f'unknown-{i}-{j}')
    yield 'time_and_missing_identity', rows, truth

    rows, truth = [], []
    for family in range(8):
        for i in range(200):
            rows.append(record(name='LatencyHigh', source='metrics', labels={'pod': f'api-{family}'},
                               metric_name='request_latency', unit='ms', metric_value=9999 if i == 99 else i,
                               timestamp=1700000000+i, severity='critical' if i == 99 else 'warning'))
            truth.append(f'metric-{family}')
    rows.append(record(message='rare data corruption', severity='critical'))
    truth.append('rare-critical')
    yield 'metric_peaks_and_rare_critical', rows, truth


def membership(result):
    return sorted(sorted(result['raw_alerts'][i]['raw']['alert_id'] for i in g['alert_indices']) for g in result['groups'])


def evaluate(name, records, truth, repeats=3):
    records = [dict(r, alert_id=f'{name}-{i}') for i, r in enumerate(records)]
    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = compress_alerts(records)
        timings.append((time.perf_counter() - start) * 1000)
    report = score_partition(records, truth, result)
    shuffled = copy.deepcopy(records)
    random.Random(20260916).shuffle(shuffled)
    report['order_invariant'] = membership(result) == membership(compress_alerts(shuffled))
    report['grouping_ms'] = {'median': round(statistics.median(timings), 3), 'min': round(min(timings), 3), 'max': round(max(timings), 3), 'runs': repeats}
    selected = result['groups'][:20]  # Bound disk/context inspection, including worst-case 10k singleton groups.
    with tempfile.TemporaryDirectory() as directory:
        store = CompressionStore(directory)
        scan = store.create(result)
        start = time.perf_counter()
        dossiers = [store.dossier(scan['scan_id'], g['group_id'], 1) for g in selected]
        report['dossier_ms'] = round((time.perf_counter() - start) * 1000, 3)
        samples = [{a['alert_id'] for a in d['alert_samples']} for d in dossiers]
        report['sampled_peak_retention'] = all(g['metric_summary']['max_alert_id'] in ids for g, ids in zip(selected, samples) if 'metric_summary' in g)
        report['sampled_critical_representatives'] = all(g['representative_alert_id'] in ids for g, ids in zip(selected, samples) if g['severity'] == 'critical')
    subset = [records[i] for g in selected for i in g['alert_indices']]
    size = lambda value: len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    report.update(case=name, provenance='controlled_synthetic', dataset_sha256=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                  inspected_groups=len(selected), inspected_input_records=len(subset), inspected_raw_bytes=size(subset), inspected_dossier_bytes=size(dossiers))
    report['passed'] &= report['order_invariant'] and report['sampled_peak_retention'] and report['sampled_critical_representatives']
    # All listed synthetic families have an explicit expected partition, including conservative singleton cases.
    report['passed'] &= report['missed_compatible_pairs'] == 0
    return report, records, truth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for name, records, truth in datasets():
        report, inputs, labels = evaluate(name, records, truth, args.repeats)
        reports.append(report)
        (args.output / f'{name}.inputs.json').write_text(json.dumps(inputs, ensure_ascii=False))
        (args.output / f'{name}.labels.json').write_text(json.dumps(labels))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    summary = {'passed': all(r['passed'] for r in reports), 'cases': reports,
               'scope': 'Grouping only; no semantic retrieval/LLM calls. Labels are synthetic merge compatibility, not production root-cause truth. Byte sizes are not tokens. Timing is not an end-to-end SLA.'}
    (args.output / 'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
