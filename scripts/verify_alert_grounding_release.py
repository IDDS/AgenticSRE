"""On snail, check deployed partial-result APIs using a zero-model-budget import.

Creates one labelled synthetic import, retained as release evidence. It neither
collects live faults nor runs diagnosis, healing, or external model requests.
"""
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path('/home/ubuntu/AgenticSRE-releases/alert-grounding-release-20261016')
BASE = 'http://127.0.0.1:8080'


def request(path, body=None):
    value = json.dumps(body).encode() if body is not None else None
    req = Request(BASE+path, data=value, headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=30) as response:
        return response.status, json.load(response)


def main():
    marker = ROOT/'production-smoke-job.json'
    if marker.exists():
        identity = json.loads(marker.read_text())
    else:
        clusters = request('/api/clusters')[1]['general_clusters']
        assert clusters, 'no general-compute cluster for smoke test'
        cluster = clusters[0]['id']
        rows = [dict(name='ReleaseGroundingSmoke', source='synthetic-release-verification',
            message='synthetic connection refused; release verification only',
            timestamp=time.time(), cluster_id=cluster, namespace='release-verification',
            pod=f'grounding-smoke-{i}', severity='warning') for i in range(6)]
        status, job = request('/api/alerts/compression-jobs', dict(cluster_id=cluster,
            namespace='release-verification', alerts=rows, semantic=True,
            review_budget={'max_model_calls':0}))
        assert status == 202
        identity = dict(job_id=job['job_id'], cluster_id=cluster)
        marker.write_text(json.dumps(identity, indent=2))
    started = time.monotonic()
    while time.monotonic()-started < 180:
        job = request('/api/alerts/compression-jobs/'+identity['job_id'])[1]
        if job['status'] not in ('queued','running'):
            break
        time.sleep(2)
    assert job['status'] == 'partial', job['status']
    assert job['model_calls'] == 0 and job['can_resume'] is False
    result = request('/api/alerts/compression/'+job['scan_id'])[1]
    semantic = result['semantic']
    assert result['retained_alerts'] == len(result['raw_alerts']) == 6
    assert len(result['groups']) == 6
    assert semantic['deferred_pairs'] == semantic['candidate_pairs'] > 0
    assert semantic['completed_pairs'] == 0 and semantic['failed_pairs'] == 0
    assert result['retrieval']['encoder'] == 'all-MiniLM-L6-v2'
    assert result['retrieval']['status'] == 'ok'
    page = request('/api/alerts/compression/'+job['scan_id']+'/page?section=review_pairs&limit=200')[1]
    assert len(page['items']) == semantic['candidate_pairs']
    assert all(item['review_status'] == 'deferred' for item in page['items'])
    try:
        request('/api/alerts/compression-jobs/'+identity['job_id']+'/resume', {})
    except HTTPError as exc:
        assert exc.code == 409
    else:
        raise AssertionError('exhausted job resumed')
    report = dict(passed=True, **identity, scan_id=job['scan_id'], revision=result['revision'],
        retained_alerts=6, groups=6, deferred_pairs=semantic['deferred_pairs'], model_calls=0,
        embedding_profile=result['retrieval']['encoder'], review_page_verified=True,
        exhausted_resume_http_status=409, elapsed_s=round(time.monotonic()-started,3),
        scope='live API synthetic import; no RCA, healing or external model call')
    (ROOT/'production-api.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
