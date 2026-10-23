"""Remote, opt-in sustained traffic validation using isolated real services.

Exercises the deployed compressor unchanged. Reports compression separately
from preservation checks; request-bound gauges are NOT aggregate metrics.
"""
import json
import os
import threading
import time
from urllib.parse import urlencode

from trace_alert_acceptance import main


def sustained(*, out, report, probe, get, namespace, stable_window=False, extended=False, window_merge=False, all_scenarios=False, sample_time_negative=False):
    if extended and not stable_window:
        raise ValueError('extended scenarios require stable window measurements')
    report['experiment'] = 'sustained-stable-window-metrics' if stable_window else 'sustained-request-bound-metrics'
    report['target_rps'] = 6
    shared_service = os.environ.get('AGENTICSRE_SHARED_TRACE_SERVICE') == '1'
    if shared_service:
        report['shared_trace_service'] = namespace+'-shared-service'
        pod_uids = {p['metadata']['name']:p['metadata']['uid'] for p in json.loads((out/'inventory.json').read_text())['items']}
    report['limitations'] = [('10-second per-Pod rolling means, no causal ground truth' if stable_window
                             else 'last-request gauges, not aggregate service metrics'),
                              'controlled faults, not natural incidents',
                              'no cross-service causal propagation ground truth']
    phases = [('healthy-load', [(0, False)] * 3),
              ('single-fault', [(350, True), (0, False), (0, False)]),
              ('independent-faults', [(350, True), (350, True), (0, True)]),
              ('recovered-load', [(0, False)] * 3),
              ('recurrence', [(350, True), (0, False), (0, False)]),
              ('final-recovery', [(0, False)] * 3)]
    if extended:
        report['experiment'] = 'stable-window-boundary-scenarios'
        phases = [('healthy-load', [(0, False)] * 3),
                  ('disjoint-requests-same-pod', [(0, False)] * 3),
                  ('missing-traces', [(350, True), (350, True), (0, True)]),
                  ('burst-load', [(350, True), (350, True), (0, True)]),
                  ('final-recovery', [(0, False)] * 3)]
    if all_scenarios:
        report['experiment'] = 'verified-window-complete-regression'
        phases = [phases[0], ('single-fault', [(350, True), (0, False), (0, False)]),
                  ('independent-faults', [(350, True), (350, True), (0, True)]),
                  *phases[1:-1], ('recurrence', [(350, True), (0, False), (0, False)]), phases[-1]]
    if sample_time_negative:
        assert stable_window and window_merge and shared_service
        report['experiment'] = 'mixed-source-sample-times-negative'
        phases = [('single-fault', [(350, True), (0, False), (0, False)])]
    for phase, settings in phases:
        stop = threading.Event()
        errors, observations = [], []
        started = time.monotonic()

        def worker(port, delay, fail):
            due = time.monotonic()
            sequence = 0
            while not stop.is_set():
                begin = time.monotonic()
                try:
                    if phase == 'disjoint-requests-same-pod' and port == 18101:
                        # Separate mechanisms: successful slow calls vs fast failed calls.
                        delay, fail = ((1000, False) if sequence % 7 in (0, 3) else (0, True))
                    value = probe(port, delay, fail, emit_trace=phase != 'missing-traces')
                    observations.append(dict(port=port, elapsed_ms=(time.monotonic()-begin)*1000,
                                             trace_id=value['trace_id'], status=value['status'],
                                             duration_ms=value['duration_ms'], trace_exported=value['trace_exported']))
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': ' + str(exc)[:150])
                sequence += 1
                due += .5
                stop.wait(max(0, due-time.monotonic()))

        concurrency = 4 if phase == 'burst-load' else 1
        workers = [threading.Thread(target=worker, args=(port, *setting))
                   for port, setting in zip((18101, 18102, 18103), settings) for _ in range(concurrency)]
        for thread in workers:
            thread.start()
        entry = dict(phase=phase, scans=[], target_rps=6*concurrency)
        try:
            time.sleep(45 if stable_window else 32)  # Include rolling-window transition, then full evidence window.
            if os.environ.get('AGENTICSRE_PROBE_METRICS_DELAY_MS'):
                duration = get('http://127.0.0.1:18100/api/v1/query?'+urlencode({'query':'scrape_duration_seconds{job="trace-span-test"}'}))
                values = [float(row['value'][1]) for row in duration['data']['result']]
                assert duration['status']=='success' and len(values)==3
                assert min(values) >= int(os.environ['AGENTICSRE_PROBE_METRICS_DELAY_MS'])/1000-.01
                entry['actual_scrape_duration_seconds'] = values
            for sample in range(2):
                begin = time.monotonic()
                result = get('http://127.0.0.1:18086/api/alerts/scan?' + urlencode(
                    dict(cluster_id='default', namespace=namespace)))
                elapsed = time.monotonic()-begin
                (out/f'{phase}-{sample}-scan.json').write_text(json.dumps(result, ensure_ascii=False))
                raw, groups = result['raw_alerts'], result['groups']
                assert not result.get('degraded'), 'degraded collection'
                assert sorted(i for g in groups for i in g['alert_indices']) == list(range(len(raw)))
                for group in groups:
                    members = [raw[i] for i in group['alert_indices']]
                    assert len({(r.get('cluster_id'), r.get('namespace'), r.get('entity')) for r in members}) == 1
                    assert not any(r['severity']=='critical' for r in members) or group['severity']=='critical'
                    if group.get('event_merge', {}).get('span'):
                        assert len({(r['labels'].get('trace_id'), r['labels'].get('span_id')) for r in members}) == 1
                if shared_service:
                    for group in groups:
                        proof = group.get('event_merge', {}).get('window')
                        if proof:
                            assert proof['trace_service'] == namespace+'-shared-service'
                            assert proof['pod_uid'] == pod_uids[group['entity']]
                            assert all(span['pod_uid'] == proof['pod_uid'] for span in proof['spans'])
                            snapshot = proof['measurement_snapshot']
                            sample_times = snapshot['source_timestamps']
                            initial_anchor = snapshot.get('initial_evaluation_at', proof['evaluation_at'])
                            assert initial_anchor-2 <= proof['end'] <= proof['evaluation_at'] <= initial_anchor
                            if snapshot.get('selection') == 'previous_completed_scrape':
                                excluded = snapshot['excluded_inflight_snapshot']
                                assert excluded['values']['end_metric'] > initial_anchor
                                assert proof['evaluation_at'] < min(excluded['source_timestamps'].values())
                            assert set(sample_times) == set(snapshot['values'])
                            assert len(set(sample_times.values())) == 1
                            assert all(initial_anchor-2 <= stamp <= proof['evaluation_at'] for stamp in sample_times.values())
                expected = sum(4 if delay else int(fail) for delay, fail in settings)
                # Count only actual Prometheus alerts; retain/report Kubernetes events too.
                prom = [r for r in raw if r.get('source') == 'prometheus']
                if phase == 'disjoint-requests-same-pod':
                    names = {r['name'] for r in prom}
                    assert names <= {'ProbeError', 'ProbeLatency100', 'ProbeLatency200', 'ProbeLatency300'}
                    assert {'ProbeError', 'ProbeLatency100'} <= names
                    assert len(names) == len(prom) and all(r['entity'] == namespace+'-a' for r in prom)
                    expected = len(prom)
                else:
                    assert len(prom) == expected, (phase, len(prom), expected)
                if stable_window:
                    expected_groups = (2 if phase == 'disjoint-requests-same-pod' else
                                       sum(2 if delay else int(fail) for delay, fail in settings))
                    if window_merge and not sample_time_negative and phase not in ('disjoint-requests-same-pod', 'missing-traces'):
                        expected_groups = sum(bool(delay or fail) for delay, fail in settings)
                    assert len(groups) == expected_groups, (phase, len(groups), expected_groups)
                    assert not result.get('trace_compression'), 'aggregate measurements must not authorize exact-Span merging'
                    if sample_time_negative:
                        assert not result.get('window_compression')
                        assert result['window_evidence']['rejected'] == {'window samples are not synchronized': 1}
                        details = result['window_evidence']['rejection_details'][0]
                        assert len(set(details['source_timestamps'].values())) == 2
                    assert all('trace_id' not in r['labels'] and 'span_id' not in r['labels'] for r in prom)
                    if expected:
                        assert result['metric_evidence']['ready_groups'] + result['metric_evidence'].get('composite_groups',0) == expected_groups
                    if phase == 'disjoint-requests-same-pod':
                        for group in groups:
                            names = {raw[i]['name'] for i in group['alert_indices']}
                            assert names == {'ProbeError'} or all(n.startswith('ProbeLatency') for n in names)
                    if phase == 'missing-traces':
                        trace_data = result.get('context_evidence', {}).get('traces', {})
                        cutoff = result['collection_started_at']-result['time_window']
                        assert not any(s['end'] >= cutoff for s in trace_data.get('observed_spans', []))
                        assert not any(e['at'] >= cutoff for e in trace_data.get('edges', []))
                saved = get('http://127.0.0.1:18086/api/alerts/compression/'+result['scan_id'])
                assert saved['raw_alerts']==raw and saved['groups']==groups
                item = dict(raw=len(raw), prometheus_raw=len(prom), groups=len(groups),
                            compression_ratio=result['compression_ratio'], scan_seconds=elapsed,
                            trace_compression=result.get('trace_compression'),
                            threshold_compression=result.get('threshold_compression'),
                            window_compression=result.get('window_compression'),
                            window_evidence={k:v for k,v in result.get('window_evidence', {}).items() if k!='proofs'},
                            metric_evidence={k:v for k,v in result.get('metric_evidence', {}).items() if k!='groups'},
                            semantic=result.get('semantic'), raw_preserved=True,
                            independent_entities_preserved=True)
                entry['scans'].append(item)
                print(json.dumps(dict(phase=phase, sample=sample, **item)), flush=True)
        finally:
            stop.set()
            for thread in workers:
                thread.join(timeout=250)
            assert not any(thread.is_alive() for thread in workers)
            duration=time.monotonic()-started
            latencies=sorted(r['elapsed_ms'] for r in observations)
            entry.update(requests=len(observations), unique_traces=len({r['trace_id'] for r in observations}),
                         exported_traces=sum(r['trace_exported'] for r in observations),
                         duration_seconds=duration, achieved_rps=len(observations)/duration,
                         request_p95_ms=latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else None,
                         request_errors=errors)
            report['stages'].append(entry)
            (out/f'{phase}-requests.json').write_text(json.dumps(observations))
        assert not errors, errors[:3]
        if phase == 'disjoint-requests-same-pod':
            calls = [r for r in observations if r['port']==18101]
            assert any(r['duration_ms']>=1000 and r['status']==200 for r in calls)
            assert any(r['duration_ms']<100 and r['status']==503 for r in calls)
            assert not any(r['duration_ms']>=100 and r['status']>=500 for r in calls)
        if phase == 'missing-traces':
            assert observations and not any(r['trace_exported'] for r in observations)
        if phase == 'burst-load':
            baseline = next(s['achieved_rps'] for s in report['stages'] if s['phase']=='healthy-load')
            assert entry['achieved_rps'] > baseline*1.5, 'traffic did not actually increase sufficiently'
    report['preservation_checks_passed'] = True
    report['compression_acceptance'] = 'measured, no global target claimed'


if __name__ == '__main__':
    if os.environ.get('AGENTICSRE_RUN_SUSTAINED_EXPERIMENT') != '1':
        raise SystemExit('explicit sustained experiment opt-in required')
    main(scenario=sustained)
