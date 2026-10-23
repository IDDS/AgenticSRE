"""Remote-only exploratory probes; no production writes or external LLM calls.

Separate established contracts from proposed acceptance criteria. A boundary
probe is not evidence that the current grouping contract promises incident
segmentation or accepts native Alertmanager payloads.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_jobs import AlertJobs
from agents.alert_semantics import local_embeddings, recall_candidates


def record(i=0, **changes):
    return dict(dict(name='ApplicationLog', source='logs', timestamp=1700000000+i,
                     cluster_id='qa', namespace='shop', pod='api', severity='warning',
                     message='upstream response duration_ms=20', state='firing'), **changes)


def dossier_for(records):
    result = compress_alerts(records)
    with tempfile.TemporaryDirectory() as directory:
        store = CompressionStore(directory)
        snapshot = store.create(result)
        dossier = store.dossier(snapshot['scan_id'], snapshot['groups'][0]['group_id'], 1)
    return result, dossier


def boundary_probes():
    probes = []
    rows = [record(i, message=f'upstream response duration_ms={9000 if i == 7 else 20}') for i in range(20)]
    result, dossier = dossier_for(rows)
    probes.append(dict(name='interior_log_latency_peak', criterion='proposed_diagnostic_evidence_retention',
        groups=len(result['groups']), raw_preserved=[r['raw'] for r in result['raw_alerts']] == rows,
        samples=[s['message'] for s in dossier['alert_samples']],
        peak_in_dossier=any('9000' in s['message'] for s in dossier['alert_samples']),
        symbolic=dossier.get('symbolic'), explanation='Same template and severity, but the slowest request is an interior record.'))

    metric_rows = [record(i, source='metrics', message='', metric_name='latency', metric_value=9000 if i == 7 else 20,
                          unit='ms') for i in range(20)]
    result, dossier = dossier_for(metric_rows)
    probes.append(dict(name='structured_metric_peak_control', criterion='existing_contract',
        passed=result['groups'][0]['metric_summary']['max_alert_id'] in {s['alert_id'] for s in dossier['alert_samples']},
        summary=result['groups'][0]['metric_summary']))

    rows = [record(i, state=state) for i, state in enumerate(['firing', 'resolved', 'firing'])]
    result = compress_alerts(rows)
    probes.append(dict(name='resolved_then_refired_in_one_import', criterion='proposed_incident_episode_segmentation',
        groups=len(result['groups']), memberships=[g['alert_indices'] for g in result['groups']],
        firing_episodes_separate=not any(0 in g['alert_indices'] and 2 in g['alert_indices'] for g in result['groups']),
        lifecycle_module_invoked=False, raw_preserved=[r['raw'] for r in result['raw_alerts']] == rows))

    row = record(labels={'severity':'critical', 'pod':'api'})
    del row['severity']
    result = compress_alerts([row])
    probes.append(dict(name='label_only_severity_import', criterion='input_normalization_boundary',
        supplied_label_severity='critical', normalized_severity=result['raw_alerts'][0]['severity'],
        group_severity=result['groups'][0]['severity'],
        explanation='Direct import only; live Prometheus adapter explicitly promotes labels.severity.'))

    rows = [record(i, severity='critical' if i % 2 else 'warning') for i in range(50)]
    result, dossier = dossier_for(rows)
    probes.append(dict(name='many_severity_changes_are_explicitly_truncated', criterion='existing_contract',
        passed=dossier['severity_changes_total'] == 50 and len(dossier['severity_changes']) == 20,
        total=dossier['severity_changes_total'], shown=len(dossier['severity_changes']),
        critical_group=result['groups'][0]['severity'] == 'critical'))

    rows = [record(i, labels={'container':container, 'tenant':tenant})
            for container in ['app','sidecar'] for tenant in ['tenant-a','tenant-b'] for i in range(10)]
    result = compress_alerts(rows)
    probes.append(dict(name='container_and_tenant_isolation', criterion='existing_contract',
        passed=len(result['groups']) == 4, inputs=len(rows), groups=len(result['groups'])))
    return probes


def retrieval_probe():
    # Labels identify semantic similarity for retrieval, not a shared root cause.
    pairs = [
        ('english_connectivity', 'The database connection was refused', 'Unable to connect to database server'),
        ('chinese_connectivity', '数据库连接被拒绝', '无法连接到数据库服务器'),
        ('bilingual_connectivity', 'The database connection was refused', '数据库拒绝客户端连接'),
        ('english_memory', 'Container terminated because it ran out of memory', 'The process was killed after exceeding the memory limit'),
        ('chinese_memory', '容器因为内存不足被终止', '进程超过内存限制后被系统杀死'),
        ('bilingual_image', 'Failed to pull image: tag does not exist', '指定版本的容器镜像在仓库中不存在'),
        ('chinese_storage', '磁盘没有剩余空间导致写入失败', '存储容量耗尽，无法保存文件'),
        ('bilingual_readiness', 'The service has no healthy backend instances', '服务没有就绪的后端实例'),
    ]
    texts = [text for _, left, right in pairs for text in (left, right)]
    texts += ['Daily backup completed successfully', 'User password changed successfully',
              '每日备份已成功完成', '用户成功修改了密码', 'TLS certificate expired', '证书有效期已经结束']
    rows = [record(i, pod=f'object-{i}', message=text) for i,text in enumerate(texts)]
    started = time.monotonic()
    result = recall_candidates(compress_alerts(rows))
    found = {frozenset((p['left'], p['right'])) for p in result['candidate_pairs']}
    wide = recall_candidates(copy.deepcopy(result), top_n=len(rows)-1)
    wide_found = {frozenset((p['left'], p['right'])) for p in wide['candidate_pairs']}
    import numpy as np
    vectors = np.asarray(local_embeddings(texts), dtype=float)
    vectors /= np.linalg.norm(vectors, axis=1)[:,None]
    groups = result['groups']
    checks = [dict(name=name, left=left, right=right,
                   cosine=round(float(vectors[2*i] @ vectors[2*i+1]),6),
                   recalled=frozenset((groups[2*i]['group_id'], groups[2*i+1]['group_id'])) in found,
                   recalled_with_top_n_21=frozenset((groups[2*i]['group_id'], groups[2*i+1]['group_id'])) in wide_found)
              for i,(name,left,right) in enumerate(pairs)]
    return dict(criterion='semantic_candidate_recall_only_not_causal_accuracy',
                encoder=result['retrieval'], pairs=checks, hits=sum(c['recalled'] for c in checks),
                labelled_positive_pairs=len(checks), candidate_pairs=len(found),
                widened_top_n_hits=sum(c['recalled_with_top_n_21'] for c in checks),
                widened_candidate_pairs=len(wide_found),
                seconds=round(time.monotonic()-started,3),
                provenance='small hand-authored multilingual challenge set; other pairs are unlabelled')


def prepared_fixture():
    result = compress_alerts([record(i, pod=f'job-object-{i}', message='connection refused') for i in range(46)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


def worker(root, phase):
    store = AlertJobs(root/'jobs')
    class ControlledModel:
        calls = 0
        async def async_json_chat(self, messages, **kwargs):
            self.calls += 1
            pairs = json.loads(messages[-1]['content'])
            with (root/'attempts.jsonl').open('a') as stream:
                stream.write(json.dumps({'phase':phase,'pairs':[p['right'] for p in pairs]})+'\n')
                stream.flush()
            if phase == 'before_crash' and self.calls == 2:
                (root/'inflight.marker').write_text('second batch entered')
                await asyncio.Event().wait()
            return {'relations':[]}
    model = ControlledModel()
    async def prepare(_):
        return prepared_fixture()
    asyncio.run(store.run(prepare, lambda _: (model,60), CompressionStore(root/'scans')))


def wait_until(check, timeout=60):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if check():
            return
        time.sleep(.05)
    raise TimeoutError('isolated checkpoint probe deadline')


def crash_probe(root):
    root.mkdir(parents=True,exist_ok=True)
    store = AlertJobs(root/'jobs')
    key = store.create({'semantic':True})['job_id']
    command = [sys.executable,str(Path(__file__).resolve()),'--output',str(root),'--worker']
    child = subprocess.Popen(command+['before_crash'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda:(root/'inflight.marker').exists())
        before = store.get(key)
        child.kill()  # Only this test-owned child; emulate hard process death mid-model-call.
        child.wait(timeout=10)
        child = subprocess.Popen(command+['after_restart'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_until(lambda:store.get(key)['status'] == 'completed')
        after = store.get(key)
        saved = CompressionStore(root/'scans').get(after['scan_id'])
        attempts = [json.loads(line) for line in (root/'attempts.jsonl').read_text().splitlines()]
        first = set(attempts[0]['pairs'])
        retried = {p for a in attempts if a['phase']=='after_restart' for p in a['pairs']}
        preserved = saved['raw_alerts'] == prepared_fixture()['raw_alerts']
        return dict(criterion='actual_worker_process_crash_with_controlled_model_not_remote_llm',
            before=before, after=after, attempted_model_entries=len(attempts),
            recorded_model_calls=after['model_calls'],
            completed_first_batch_not_repeated=not bool(first & retried),
            raw_preserved=preserved, retry_batch_sizes=[len(a['pairs']) for a in attempts if a['phase']=='after_restart'],
            recovery_passed=after['completed_pairs']==45 and not bool(first & retried) and preserved,
            call_accounting_complete=after['model_calls']==len(attempts))
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',choices=['before_crash','after_restart'])
    parser.add_argument('--retrieval-only',action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.worker:
        return worker(args.output,args.worker)
    if args.retrieval_only:
        report = retrieval_probe()
        (args.output/'retrieval-ablation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        return print(json.dumps(report,ensure_ascii=False,indent=2))
    report = {'boundaries':boundary_probes()}
    path = args.output/'exploration-report.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    report['retrieval'] = retrieval_probe()
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    report['hard_crash'] = crash_probe(args.output/'hard-crash')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
