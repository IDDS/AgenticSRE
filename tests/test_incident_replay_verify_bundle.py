import hashlib
import json

import pytest

from integrations.incident_replay.verify_bundle import verify_bundle


def _write(root, name, data, entries):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data).encode() if not isinstance(data, bytes) else data
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    entries.append({'path': name, 'bytes': len(raw), 'sha256': digest})
    return digest


def _bundle(root, truncated_repeat=False):
    entries = []
    code_hashes = {}
    for name, key in [('runner.py', 'runner'), ('trace_evidence.py', 'trace_evidence'),
                      ('compact.py', 'compact'), ('reranking_evidence.py', 'reranking_evidence')]:
        code_hashes[key] = _write(root, 'frozen-code/' + name, name.encode(), entries)
    for date, stamp in [('2022-08-22', 1661126400), ('2022-08-23', 1661212800)]:
        target = 'svc-1234567890-abcde'
        truth = {'events': [{'inject_timestamp': stamp, 'inject_time': date + ' 00:00:00', 'inject_pod': target}]}
        truth_hash = _write(root, f'{date}/truth/{date}-fault_list.json', truth, entries)
        _write(root, f'{date}/manifest-1.json', {'selected_time_utc': date + 'T00:00:00+00:00'}, entries)
        for label, prefix in [('first', ''), ('repeat', 'repeat-')]:
            driver_hash = _write(root, f'frozen-code/{label}-{date}.py', (date + label).encode(), entries)
            prepared = {'index': 1, 'status': 'prepared'}
            for stem, key in [('observations', 'observation_sha256'), ('trace-summary', 'trace_summary_sha256')]:
                prepared[key] = _write(root, f'{date}/{prefix}{stem}-1.json', {'stem': stem}, entries)
            prepared['trace_extra_sha256'] = _write(root, f'{date}/{prefix}trace-extra-1.json',
                                                   {'source_sha256': prepared['trace_summary_sha256']}, entries)
            artifact = {'observation_sha256': prepared['observation_sha256'],
                        'result': {'status': 'completed', 'affected_services': [target],
                                   'evidence_coverage': {'final_context_truncated_agents':
                                                         ['trace_agent'] if truncated_repeat and label == 'repeat' else []}}}
            result_hash = _write(root, f'{date}/{prefix}result-1.json', artifact, entries)
            progress = {'finished_utc': date, 'indices': [1], 'preparation': [prepared],
                        'results': [{'index': 1, 'status': 'completed', 'result_sha256': result_hash,
                                     'truth_sha256': truth_hash}], 'software_sha256': {**code_hashes, 'driver': driver_hash}}
            _write(root, f'{date}/{label}-progress.json', progress, entries)
    (root / 'metadata.json').write_text(json.dumps({'files': entries}))


def test_recomputes_predictions_and_detects_tampering(tmp_path):
    _bundle(tmp_path)
    result = verify_bundle(tmp_path)
    assert result['integrity'] == 'verified'
    assert [batch['same_first_prediction'] for batch in result['batches']] == [1, 1]
    assert all(run['pod_hits'] == 1 and run['scheduled'] == 1 for batch in result['batches'] for run in batch['runs'])
    assert all(run['pod_hits_without_final_context_truncation'] == 1 for batch in result['batches'] for run in batch['runs'])
    (tmp_path / '2022-08-22/result-1.json').write_text('{}')
    with pytest.raises(ValueError, match='file_integrity_mismatch'):
        verify_bundle(tmp_path)


def test_rejects_manifest_paths_outside_bundle(tmp_path):
    _bundle(tmp_path)
    metadata = json.loads((tmp_path / 'metadata.json').read_text())
    outside = tmp_path.parent / 'outside-evidence.json'
    outside.write_text('{}')
    metadata['files'].append({'path': '../outside-evidence.json', 'bytes': 2,
                              'sha256': hashlib.sha256(b'{}').hexdigest()})
    (tmp_path / 'metadata.json').write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='file_outside_bundle'):
        verify_bundle(tmp_path)


def test_failed_case_stays_in_denominator(tmp_path):
    _bundle(tmp_path)
    path = tmp_path / '2022-08-22/first-progress.json'
    progress = json.loads(path.read_text())
    progress['results'][0]['status'] = 'timeout'
    raw = json.dumps(progress).encode()
    path.write_bytes(raw)
    metadata_path = tmp_path / 'metadata.json'
    metadata = json.loads(metadata_path.read_text())
    entry = next(x for x in metadata['files'] if x['path'] == '2022-08-22/first-progress.json')
    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    metadata_path.write_text(json.dumps(metadata))
    batch = next(x for x in verify_bundle(tmp_path)['batches'] if x['date'] == '2022-08-22')
    first = next(x for x in batch['runs'] if x['run'] == 'first')
    assert first['scheduled'] == 1 and first['pod_hits'] == 0 and first['cases'][0]['status'] == 'timeout'
    assert batch['comparable_predictions'] == 0


def test_truncated_hit_is_not_counted_as_complete(tmp_path):
    _bundle(tmp_path, truncated_repeat=True)
    batch = verify_bundle(tmp_path)['batches'][0]
    repeat = next(x for x in batch['runs'] if x['run'] == 'repeat')
    assert repeat['pod_hits'] == 1 and repeat['pod_hits_without_final_context_truncation'] == 0
