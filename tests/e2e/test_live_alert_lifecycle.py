"""Opt-in remote Job failure -> scan -> RCA -> recovery -> recurrence acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def diagnosis_acceptance(run):
    envelope = (run.get('result') or {}).get('result') or {}
    diagnosis = envelope.get('result') or {}
    root = str(diagnosis.get('root_cause') or '').lower()
    # Mentioning "exit code unavailable" is not a successful diagnosis.
    observed_exit = any(item.get('signal_type') == 'container_terminated' and item.get('raw_ref', {}).get('exit_code') == 7
        for event in run.get('events', []) if isinstance(event, dict)
        for item in event.get('items', []) if isinstance(item, dict))
    job_skill = any(event.get('event') == 'skill_runtime' and event.get('primary_skill_id') == 'k8s.job_failed'
        and (event.get('result') or {}).get('diagnosis', {}).get('fault_type') == 'job_failed'
        and '7' in str((event.get('result') or {}).get('diagnosis', {}).get('root_cause', ''))
        for event in run.get('events', []) if isinstance(event, dict))
    return (run.get('status') == 'completed' and observed_exit and job_skill and '7' in root and ('exit' in root or '退出' in root)
            and diagnosis.get('fault_type') in {'application_crash', 'configuration_error', 'job_failed', 'probe_failure', 'container_terminated'})


def main():
    if os.environ.get('AGENTICSRE_RUN_LIVE_DIAGNOSIS') != '1':
        print('SKIP: explicit remote live validation opt-in required')
        return
    os.environ['AGENTICSRE_LIVE_DIAG_NS'] = 'agenticsre-diag-e2e-lifecycle-' + uuid.uuid4().hex[:6]
    spec = importlib.util.spec_from_file_location('helpers', Path(__file__).with_name('test_live_diagnosis_fault_injection.py'))
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.assert_safe_identifiers()
    output = Path(os.environ.get('AGENTICSRE_LIFECYCLE_REPORT', '/tmp/agenticsre-lifecycle-live.json'))
    report = {'status': 'running', 'namespace': h.NAMESPACE, 'run_id': h.RUN_ID,
              'fault': 'owned Job exits with code 7', 'recovery': 'replace owned Job with successful exit 0',
              'rule_note': 'test-only rule uses real kube_job_failed metric, for=0s, interval=15s'}
    job_name = 'agenticsre-diag-job'
    def save(stage):
        report['stage'] = stage
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'stage': stage, 'namespace': h.NAMESPACE}, ensure_ascii=False), flush=True)
    def apply(obj):
        subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True,
                       capture_output=True, check=True, timeout=60)
    def create_job(code):
        apply({'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': job_name, 'namespace': h.NAMESPACE},
            'spec': {'backoffLimit': 0, 'template': {'spec': {'restartPolicy': 'Never', 'containers': [
                {'name': 'probe', 'image': h.IMAGE, 'command': ['/bin/sh', '-c', f'echo lifecycle-probe; exit {code}']}]}}}})
        condition = 'failed' if code else 'complete'
        h.kubectl('-n', h.NAMESPACE, 'wait', f'--for=condition={condition}', f'job/{job_name}', '--timeout=180s', timeout=190)
        pods = json.loads(h.kubectl('-n', h.NAMESPACE, 'get', 'pods', '-l', f'job-name={job_name}', '-o', 'json').stdout)
        codes = [c.get('state', {}).get('terminated', {}).get('exitCode') for p in pods['items'] for c in p['status'].get('containerStatuses', [])]
        assert code in codes, codes
        return {'condition': condition, 'exit_codes': codes}
    def replace_job(code):
        assert h.namespace_labels().get('agenticsre.io/run-id') == h.RUN_ID
        h.kubectl('-n', h.NAMESPACE, 'delete', 'job', job_name, '--wait=true', '--timeout=60s')
        return create_job(code)
    def scan():
        data = h.api('GET', '/api/alerts/scan?namespace='+h.NAMESPACE, timeout=180)
        assert data['lifecycle']['status'] == 'ok', data.get('lifecycle')
        assert data['omitted_alerts'] == 0
        return data
    def target(data):
        return next((g for g in data['groups'] if g['representative'] == 'AgenticSRELifecycleProbe'), None)
    def wait_firing(episode):
        deadline = time.time()+300
        while time.time() < deadline:
            data = scan()
            group = target(data)
            if group:
                state = group['lifecycle'][0]
                assert state['episode'] == episode and state['status'] == 'active', state
                return data, group, state
            time.sleep(8)
        raise TimeoutError('test rule did not produce a firing alert')
    def wait_resolved(incident):
        deadline = time.time()+300
        while time.time() < deadline:
            scan()
            data = h.api('GET', '/api/alerts/lifecycle?namespace='+h.NAMESPACE)
            state = next(s for s in data['incidents'] if s['incident_id'] == incident)
            if state['status'] == 'resolved':
                return state
            time.sleep(12)
        raise TimeoutError('alert did not resolve after healthy Job and complete scans')
    created = False
    try:
        h.setup_namespace()
        created = True
        apply({'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'PrometheusRule',
            'metadata': {'name': 'lifecycle-probe', 'namespace': h.NAMESPACE, 'labels': {'release': 'kube-prom'}},
            'spec': {'groups': [{'name': 'agenticsre-lifecycle', 'interval': '15s', 'rules': [{
                'alert': 'AgenticSRELifecycleProbe', 'expr': f'kube_job_failed{{namespace="{h.NAMESPACE}",job_name="{job_name}",condition="true"}} > 0',
                'for': '0s', 'labels': {'severity': 'warning'}, 'annotations': {'summary': 'Job failed'}}]}]}})
        report['fault_observed'] = create_job(7)
        save('real_job_failed')
        data, group, state = wait_firing(1)
        report['first'] = state
        duplicate = scan()
        repeated = target(duplicate)['lifecycle'][0]
        assert repeated['incident_id'] == state['incident_id'] and repeated['episode'] == 1
        assert repeated['observations'] > state['observations']
        report['repeated'] = repeated
        body = {'alert_group': {'scan_id': data['scan_id'], 'group_id': group['group_id'], 'revision': data['revision']},
                'namespace': h.NAMESPACE, 'dedup_key': h.RUN_ID}
        h.assert_no_truth_leak(body)
        report['rca_request'] = body
        started = h.api('POST', '/api/rca/run', body)
        report['rca_run_id'] = started['run_id']
        save('diagnosis_running')
        try:
            run = h.poll_rca(started['run_id'], timeout=1000)
            report['diagnosis'] = h.diagnosis_view(run, skill=False)
            report['diagnosis_run'] = run
            report['diagnosis_correct'] = diagnosis_acceptance(run)
        except Exception as exc:
            report['diagnosis_error'] = str(exc)
            report['diagnosis_correct'] = False
        save('diagnosis_finished')
        report['recovery_observed'] = replace_job(0)
        save('job_completed')
        report['resolved'] = wait_resolved(state['incident_id'])
        assert report['resolved']['episode'] == 1
        save('alert_resolved')
        report['recurrence_observed'] = replace_job(7)
        _, _, reopened = wait_firing(2)
        assert reopened['incident_id'] == state['incident_id']
        report['reopened'] = reopened
        save('recurrence_verified')
        report['final_recovery_observed'] = replace_job(0)
        report['final_resolved'] = wait_resolved(state['incident_id'])
        assert report['final_resolved']['episode'] == 2
        report['lifecycle_passed'] = True
        report['status'] = 'passed' if report['diagnosis_correct'] else 'diagnosis_failed'
        save('completed')
    except Exception as exc:
        report.update(status='failed', error=str(exc))
        save('failed')
        raise
    finally:
        if created:
            h.cleanup_namespace()
            report['cleanup_verified'] = not h.namespace_exists()
            save('cleaned')
    assert report['status'] == 'passed', report.get('diagnosis') or report.get('diagnosis_error')


if __name__ == '__main__':
    main()
