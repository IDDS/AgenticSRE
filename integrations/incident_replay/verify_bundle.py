"""Independently verify a frozen replay bundle and recompute top-1 scores."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _service(name: Any) -> str:
    return re.sub(r'-[a-z0-9]{8,10}-[a-z0-9]{5}$', '', name) if isinstance(name, str) else ''


def verify_bundle(directory: Path) -> dict[str, Any]:
    """Fail closed on integrity errors; score every scheduled case once per run.

    Labels are used only after the historical model outputs have been frozen.
    This verifies files and scoring, not the original provider or dataset truth.
    """
    root = directory.resolve(strict=True)
    metadata = _json(root / 'metadata.json')
    entries = metadata.get('files') if isinstance(metadata, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError('missing_file_manifest')
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('invalid_file_manifest')
        name = entry['path']
        if name in declared:
            raise ValueError('duplicate_file_manifest_path')
        declared.add(name)
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('file_outside_bundle')
        if path.stat().st_size != entry.get('bytes') or _sha(path) != entry.get('sha256'):
            raise ValueError('file_integrity_mismatch')

    def load(name: str) -> Any:
        if name not in declared:
            raise ValueError('required_file_not_declared')
        return _json(root / name)

    output: dict[str, Any] = {'integrity': 'verified', 'batches': [],
                              'limitations': ['verifies frozen files and scoring, not original data collection',
                                              'provider model implementation and full original environment not frozen']}
    for date in ('2022-08-23', '2022-08-22'):
        truth = load(f'{date}/truth/{date}-fault_list.json')
        if not isinstance(truth, dict) or not all(isinstance(group, list) for group in truth.values()):
            raise ValueError('invalid_truth')
        labels = [item for group in truth.values() for item in group if isinstance(item, dict)]
        runs = []
        predictions: dict[str, dict[int, str]] = {}
        preparations: dict[str, dict[int, dict[str, Any]]] = {}
        for label in ('first', 'repeat'):
            progress = load(f'{date}/{label}-progress.json')
            indices = progress.get('indices')
            results = progress.get('results')
            preparation = progress.get('preparation')
            if (not progress.get('finished_utc') or not isinstance(indices, list) or not indices
                    or any(type(i) is not int for i in indices) or len(set(indices)) != len(indices)
                    or not isinstance(results, list) or not isinstance(preparation, list)):
                raise ValueError('invalid_progress')
            by_index = {x.get('index'): x for x in results if isinstance(x, dict)}
            prepared_by_index = {x.get('index'): x for x in preparation if isinstance(x, dict)}
            if len(by_index) != len(results) or set(by_index) != set(indices):
                raise ValueError('result_index_mismatch')
            if len(prepared_by_index) != len(preparation) or set(prepared_by_index) != set(indices):
                raise ValueError('preparation_index_mismatch')
            preparations[label] = prepared_by_index
            for code_name, software_key in (('runner.py', 'runner'), ('trace_evidence.py', 'trace_evidence'),
                                             ('compact.py', 'compact'), ('reranking_evidence.py', 'reranking_evidence')):
                code_path = f'frozen-code/{code_name}'
                if code_path not in declared or _sha(root / code_path) != progress.get('software_sha256', {}).get(software_key):
                    raise ValueError('software_hash_mismatch')
            driver_name = f'frozen-code/{label}-{date}.py'
            if driver_name not in declared or _sha(root / driver_name) != progress.get('software_sha256', {}).get('driver'):
                raise ValueError('driver_hash_mismatch')
            cases = []
            predictions[label] = {}
            for index in indices:
                entry = by_index[index]
                case = {'index': index, 'status': entry.get('status'), 'pod_match': False, 'service_match': False}
                if entry.get('status') != 'completed':
                    cases.append(case)
                    continue
                prepared = prepared_by_index[index]
                if prepared.get('status') != 'prepared':
                    raise ValueError('completed_case_not_prepared')
                prefix = 'repeat-' if label == 'repeat' else ''
                for stem, key in (('observations', 'observation_sha256'), ('trace-summary', 'trace_summary_sha256'),
                                  ('trace-extra', 'trace_extra_sha256')):
                    filename = f'{date}/{prefix}{stem}-{index}.json'
                    if filename not in declared or _sha(root / filename) != prepared.get(key):
                        raise ValueError('prepared_input_hash_mismatch')
                extra = load(f'{date}/{prefix}trace-extra-{index}.json')
                if not isinstance(extra, dict) or extra.get('source_sha256') != prepared.get('trace_summary_sha256'):
                    raise ValueError('trace_provenance_mismatch')
                manifest = load(f'{date}/manifest-{index}.json')
                timestamp = datetime.fromisoformat(manifest['selected_time_utc'])
                if timestamp.tzinfo is None:
                    raise ValueError('event_timezone_missing')
                stamp = int(timestamp.timestamp())
                selected = [x for x in labels if str(x.get('inject_timestamp')) == str(stamp)]
                if len(selected) != 1 or not isinstance(selected[0].get('inject_pod'), str):
                    raise ValueError('ambiguous_target')
                text_time = datetime.strptime(selected[0]['inject_time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if int(text_time.timestamp()) != stamp or _sha(root / f'{date}/truth/{date}-fault_list.json') != entry.get('truth_sha256'):
                    raise ValueError('truth_integrity_mismatch')
                target = selected[0]['inject_pod']
                result_name = f'{date}/{"repeat-" if label == "repeat" else ""}result-{index}.json'
                if result_name not in declared or _sha(root / result_name) != entry.get('result_sha256'):
                    raise ValueError('result_hash_mismatch')
                artifact = load(result_name)
                if not isinstance(artifact, dict) or artifact.get('observation_sha256') != prepared.get('observation_sha256'):
                    raise ValueError('result_observation_mismatch')
                result = artifact.get('result') if isinstance(artifact, dict) else None
                affected = result.get('affected_services') if isinstance(result, dict) else None
                if (not isinstance(result, dict) or result.get('status') != 'completed' or result.get('error')
                        or not isinstance(affected, list) or not affected or not isinstance(affected[0], str)):
                    case['status'] = 'invalid_result'
                    cases.append(case)
                    continue
                prediction = affected[0]
                predictions[label][index] = prediction
                coverage = result.get('evidence_coverage')
                truncated = coverage.get('final_context_truncated_agents') if isinstance(coverage, dict) else None
                if not isinstance(truncated, list) or any(not isinstance(item, str) for item in truncated):
                    truncated = None
                case.update(expected_pod=target, predicted_first=prediction, pod_match=prediction == target,
                            service_match=bool(prediction) and _service(prediction) == _service(target),
                            final_context_truncated_agents=truncated,
                            omitted_comparable_groups=entry.get('omitted_comparable_groups'))
                cases.append(case)
            runs.append({'run': label, 'scheduled': len(indices), 'pod_hits': sum(x['pod_match'] for x in cases),
                         'service_hits': sum(x['service_match'] for x in cases),
                         'pod_hits_without_final_context_truncation': sum(
                             x['pod_match'] and x.get('final_context_truncated_agents') == [] for x in cases),
                         'cases': cases})
        if set(predictions['first']) != set(predictions['repeat']):
            comparable = set(predictions['first']) & set(predictions['repeat'])
        else:
            comparable = set(predictions['first'])
        input_keys = ('observation_sha256', 'trace_summary_sha256', 'trace_extra_sha256')
        same_inputs = {i for i in comparable if all(preparations['first'][i].get(k) == preparations['repeat'][i].get(k)
                                                     for k in input_keys)}
        output['batches'].append({'date': date, 'runs': runs, 'comparable_predictions': len(comparable),
                                  'same_first_prediction': sum(predictions['first'][i] == predictions['repeat'][i]
                                                               for i in comparable),
                                  'same_input_hashes': len(same_inputs),
                                  'same_prediction_and_input': sum(predictions['first'][i] == predictions['repeat'][i]
                                                                   for i in same_inputs)})
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_bundle(args.directory), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
