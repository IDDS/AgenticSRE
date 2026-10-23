"""On snail, verify production original reads with labelled synthetic imports.

No RCA, recovery or external model calls. Imports remain as release evidence.
"""
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError

import verify_alert_grounding_release as prior

ROOT = Path('/home/ubuntu/AgenticSRE-releases/alert-evidence-release-20261017')
prior.ROOT = ROOT


def main():
    prior.main()
    report = json.loads((ROOT/'production-api.json').read_text())
    marker = ROOT/'evidence-smoke-scan.json'
    rare = 'Synthetic readiness detail. '*20+'CreateContainerConfigError: configmap "release-fixture" not found'
    if marker.exists():
        scan = prior.request('/api/alerts/compression/'+json.loads(marker.read_text())['scan_id'])[1]
    else:
        rows = [dict(name='ReleaseEvidenceReadSmoke',source='synthetic-release-verification',
            message=rare if i == 50 else 'Observed Pod is not ready',timestamp=time.time()+i*.001,
            cluster_id=report['cluster_id'],namespace='release-verification',pod='evidence-read-fixture',
            severity='warning') for i in range(101)]
        scan = prior.request('/api/alerts/compress',dict(alerts=rows,cluster_id=report['cluster_id'],
            namespace='release-verification',semantic=False))[1]
        marker.write_text(json.dumps(dict(scan_id=scan['scan_id']),indent=2))
    assert len(scan['raw_alerts']) == 101
    if len(scan['groups']) > 1:
        scan = prior.request('/api/alerts/compression/'+scan['scan_id']+'/edit',dict(revision=scan['revision'],
            action='merge',group_ids=[group['group_id'] for group in scan['groups']],
            reason='synthetic release read verification; not semantic-merge accuracy'))[1]
    assert len(scan['groups']) == 1
    group = scan['groups'][0]
    path = f"/api/alerts/compression/{scan['scan_id']}/groups/{group['group_id']}/evidence"
    url = path+f"?revision={scan['revision']}"
    offset,records,snapshots,pages = 0,[],set(),[]
    while offset is not None:
        status,page = prior.request(url+f'&offset={offset}&limit=17')
        assert status == 200
        pages.append(page)
        records.extend(page['records'])
        snapshots.add(page['ref']['snapshot_sha256'])
        offset = page['next_offset']
    assert records == scan['raw_alerts'] and len(snapshots) == 1
    assert records[50]['message'] == rare
    for bad_url,expected in [(path,422),(path+f"?revision={scan['revision']+1}",409),
                             (url+'&limit=101',422),(url+'&offset=102',409)]:
        try: prior.request(bad_url)
        except HTTPError as exc: assert exc.code == expected
        else: raise AssertionError('invalid original evidence read accepted')
    # Read the deployed store through its diagnosis hydration path without
    # invoking Pipeline or the model. The earlier campaign covers actual RCA.
    code = '''import json,sys
from agents.alert_compression import CompressionStore
from agents.diagnosis_grounding import claim_context
ref=json.load(sys.stdin)
dossier=CompressionStore('/app/data/alert_compression').dossier(**ref,for_diagnosis=True)
context=json.loads(claim_context({'agent_skill_context':{'payload':dossier}}))
print(json.dumps({'dossier':dossier,'context':context},ensure_ascii=False))'''
    ref = dict(scan_id=scan['scan_id'],group_id=group['group_id'],revision=scan['revision'])
    loaded = json.loads(subprocess.check_output(['docker','exec','-i','agenticsre','python','-c',code],
        input=json.dumps(ref).encode()))
    dossier,context = loaded['dossier'],loaded['context']
    assert dossier['raw_evidence']['snapshot_sha256'] == next(iter(snapshots))
    rare_id = records[50]['alert_id']
    assert any(row['alert_id'] == rare_id and row['message'] == rare for row in dossier['alert_samples'])
    assert any(row['id'] == rare_id and rare in row['observation'] for row in context['observations'])
    shown = [row for row in context['observations'] if row['signal_type'] == 'alert_observed']
    coverage = dict(dossier['raw_evidence'],prompt_alerts=len(shown),
                    prompt_excerpted_messages=sum(row['excerpt'] for row in shown))
    report.update(evidence_read=dict(passed=True,**ref,records=len(records),pages=len(pages),
        original_text_equal=True,rare_message_loaded=True,rare_message_in_catalog=True,
        invalid_reads_rejected=True,snapshot_sha256=next(iter(snapshots))),coverage=coverage)
    (ROOT/'production-evidence-pages.json').write_text(json.dumps(pages,ensure_ascii=False,indent=2))
    (ROOT/'production-dossier.json').write_text(json.dumps(loaded,ensure_ascii=False,indent=2))
    (ROOT/'production-api.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report),flush=True)


if __name__ == '__main__': main()
