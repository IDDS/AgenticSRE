"""Opt-in controlled batch acceptance against an isolated remote API instance."""
import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eval.alert_compression_acceptance import score_partition


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    last_records = []
    for name in ('burst_10000', 'independent_10000'):
        records = json.loads((args.fixtures / f'{name}.inputs.json').read_text())
        for record in records:
            record['cluster_id'] = 'default'
            record['namespace'] = 'compression-acceptance'
        labels = json.loads((args.fixtures / f'{name}.labels.json').read_text())
        payload = {'alerts': records, 'cluster_id': 'default', 'namespace': 'compression-acceptance', 'semantic': False}
        request = Request(args.base_url.rstrip('/') + '/api/alerts/compress', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        started = time.perf_counter()
        with urlopen(request, timeout=180) as response:
            raw = response.read()
        elapsed = time.perf_counter() - started
        result = json.loads(raw)
        report = score_partition(records, labels, result)
        report.update(case=name, elapsed_s=round(elapsed, 3), response_bytes=len(raw), scan_id=result['scan_id'],
                      retrieval=result.get('retrieval'), semantic=result.get('semantic'), scag_edges=len(result.get('scag', {}).get('edges', [])))
        assert report['passed'] and report['missed_compatible_pairs'] == 0, report
        assert result['omitted_alerts'] == 0 and not result.get('lifecycle'), result.get('lifecycle')
        assert result['semantic']['model_calls'] == 0
        assert result['retrieval']['indexed_groups'] == len(result['groups'])
        assert result['retrieval']['truncated'] is False
        assert result['retrieval']['embedded_groups'] == len(result['groups'])
        assert result['retrieval']['queried_groups'] == len(result['groups'])
        assert len(result.get('candidate_pairs', [])) == result['retrieval']['candidate_pairs_total']
        assert result['retrieval']['candidate_pairs_truncated'] is False
        assert not any(edge.get('causal_confirmed') for edge in result.get('scag', {}).get('edges', []))
        (args.output / f'{name}.response.json').write_bytes(raw)
        reports.append(report)
        last_records = records
        print(json.dumps(report, ensure_ascii=False), flush=True)
    rejected = False
    request = Request(args.base_url.rstrip('/') + '/api/alerts/compress', data=json.dumps({'alerts': last_records + [last_records[0]], 'cluster_id': 'default'}).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as error:
        rejected = error.code == 400 and '10000' in error.read().decode()
    assert rejected, '10001 records must fail clearly, not silently truncate'
    (args.output / 'report.json').write_text(json.dumps({'passed': True, 'over_limit_rejected': rejected, 'cases': reports}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
