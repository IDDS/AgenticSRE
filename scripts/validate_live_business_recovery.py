"""Remote Train Ticket acceptance: actual business traffic, blind diagnosis, guarded recovery.

Default is read-only. An actual fault additionally requires --execute and
AGENTICSRE_RUN_LIVE_RECOVERY=1. Never run against a production testbed.
The diagnosis service must be an isolated instance with learning/automatic
actions disabled and a server-held JSON business recovery profile.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.live_recovery_control import read_deployment, release_fence, scale, snapshot

NAMESPACE = 'train-ticket'
SERVICE = 'ts-station-service'
BUSINESS_PATH = '/api/v1/stationservice/stations'
CONTROL_PATH = '/api/v1/routeservice/routes'
FORBIDDEN = {'fault_type', 'root_cause', 'diagnosis', 'initial_evidence', 'candidate_faults',
             'candidate_skills', 'experiment_fault_type', 'scenario_id', 'confidence', 'commands'}


def no_truth(value):
    if isinstance(value, dict):
        if FORBIDDEN.intersection(value):
            raise ValueError('diagnosis_request_contains_answer')
        for item in value.values(): no_truth(item)
    elif isinstance(value, list):
        for item in value: no_truth(item)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(out, name, value):
    temporary = out / (name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(out / name)


def request(base, path, body=None, *, timeout=15, trace=None):
    headers = {'Content-Type': 'application/json'}
    if trace: headers['traceparent'] = '00-' + trace + '-' + uuid.uuid4().hex[:16] + '-01'
    req = urllib.request.Request(base + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024: raise ValueError('receipt_size_limit')
        return json.loads(data)


def get_resource(kind, name=None):
    args = ['kubectl', '-n', NAMESPACE, 'get', kind] + ([name] if name else []) + ['-o', 'json']
    value = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if value.returncode: raise RuntimeError('testbed_read_failed')
    return json.loads(value.stdout)


def business_sample(base, phase, index):
    value = {'at': now(), 'phase': phase, 'index': index, 'trace_id': uuid.uuid4().hex,
             'business_success': False, 'control_success': False}
    started = time.monotonic()
    try:
        data = request(base, BUSINESS_PATH, timeout=3, trace=value['trace_id'])
        value.update(http_status=200, business_success=isinstance(data, dict) and
                     type(data.get('status')) is int and data['status'] == 1 and
                     isinstance(data.get('data'), list) and bool(data['data']))
    except Exception as exc:
        value.update(error_type=type(exc).__name__, http_status=getattr(exc, 'code', None))
    value['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    try:
        data = request(base, CONTROL_PATH, timeout=3)
        value['control_success'] = isinstance(data, dict) and data.get('status') == 1 and bool(data.get('data'))
    except Exception as exc:
        value['control_error_type'] = type(exc).__name__
    return value


def metrics(samples):
    values = sorted(s['elapsed_ms'] for s in samples)
    return {'samples': len(samples), 'successes': sum(s['business_success'] for s in samples),
            'controls_healthy': bool(samples) and all(s['control_success'] for s in samples),
            'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None}


def diagnose(api, samples, out, label, budget=165):
    observed = metrics(samples)
    query = (f'请只读调查集群 default、命名空间 {NAMESPACE} 的 Service/{SERVICE}。'
        f'实际业务入口为 ts-ui-dashboard 的 GET {BUSINESS_PATH}。'
        f'观测窗口 {samples[0]["at"]} 至 {samples[-1]["at"]}，'
        f'{observed["samples"]} 笔中 {observed["successes"]} 笔满足业务响应条件，'
        f'HTTP 状态为 {sorted({str(s.get("http_status")) for s in samples})}。'
        f'同入口 GET {CONTROL_PATH} 对照是否全部成功：{observed["controls_healthy"]}。'
        '实际请求 trace ID：' + ','.join(s['trace_id'] for s in samples[-2:]) + '。'
        '请沿实际 selector、端点、Pod 和所属工作负载采集证据，判断该接口是否异常及具体原因。'
        '不得用旁路对象代替目标；证据不足时明确保留未知。只读调查，不执行处置。')
    records = []
    for mode in ('multi', 'skill'):
        body = {'query': query, 'namespace': NAMESPACE, 'cluster_id': 'default',
                'dedup_key': uuid.uuid4().hex}
        if mode == 'skill':
            body['skill_workflow'] = {'domain': 'k8s', 'stages': ['detect', 'diagnose'], 'dry_run': True,
                'analysis_phases': ['context', 'hypothesis', 'plan', 'investigation', 'reasoning'],
                'payload': {'source': 'read_only_business_observation', 'domain': 'k8s', 'cluster_id': 'default',
                            'cluster_kind': 'general', 'namespace': NAMESPACE, 'service': SERVICE}}
        no_truth(body)
        save(out, label + '-' + mode + '-request.json', body)
        record = {'mode': mode, 'requested_at': now(), 'started_monotonic': time.monotonic()}
        try:
            record.update(request(api, '/api/rca/run', body))
        except Exception as exc:
            record.update(status='request_outcome_unknown', error_type=type(exc).__name__)
        records.append(record)
    deadline = time.monotonic() + budget
    pending = [r for r in records if r.get('run_id')]
    while pending and time.monotonic() < deadline:
        for record in list(pending):
            raw = request(api, '/api/rca/' + record['run_id'])
            record.update(status=raw.get('status'), duration_s=round(time.monotonic() - record['started_monotonic'], 3))
            if record['status'] in {'completed', 'failed'}:
                save(out, label + '-' + record['mode'] + '-run.json', raw)
                model = request(api, '/api/rca/' + record['run_id'] + '/ontology')
                save(out, label + '-' + record['mode'] + '-ontology.json', model)
                record['ontology'] = model
                record['completed_at'] = now()
                pending.remove(record)
        save(out, label + '-diagnoses.json', records)
        if pending: time.sleep(2)
    return records


def try_approved_recovery(api, records, contract, out):
    """Use only the existing ontology gate; never inject a diagnosis or lower thresholds."""
    attempts = []
    for record in records:
        model = record.get('ontology') or {}
        context = model.get('action_context') or {}
        target = context.get('target') or {}
        saved = contract['target']
        if (not context.get('recorded_conditions_met') or target.get('kind') != 'Deployment'
                or target.get('uid') != saved['uid'] or target.get('name') != saved['deployment']
                or target.get('namespace') != NAMESPACE or target.get('cluster_id') != 'default'):
            attempts.append({'mode': record['mode'], 'executed': False, 'reason': 'ontology_gate_not_ready',
                             'blockers': context.get('blockers') or []})
            continue
        body = {'source_run_id': record['run_id'], 'ontology_revision': model['revision'],
            'cluster_id': 'default', 'namespace': NAMESPACE, 'service': SERVICE,
            'commands': [f'kubectl scale deployment/{target["name"]} -n {NAMESPACE} --replicas=1'],
            'dry_run': True, 'source': 'live_business_acceptance_operator_selected_action'}
        try:
            preview = request(api, '/api/heal/execute', body)
            save(out, 'heal-preview.json', preview)
            body.update(dry_run=False, approved=True, ontology_proposal_id=preview['id'])
            save(out, 'heal-request.json', body)
            run = request(api, '/api/heal/execute', body, timeout=90)
            save(out, 'heal-run.json', run)
            attempts.append({'mode': record['mode'], 'executed': run.get('action_success'),
                             'run_id': run.get('id'), 'status': run.get('status')})
            break  # Never repeat an action after either a success or an unknown outcome.
        except Exception as exc:
            attempts.append({'mode': record['mode'], 'executed': None,
                             'reason': 'action_request_failed_or_unknown', 'error_type': type(exc).__name__,
                             'http_status': getattr(exc, 'code', None)})
            break
    save(out, 'action-attempts.json', attempts)
    return attempts


def run(args):
    if args.execute and os.environ.get('AGENTICSRE_RUN_LIVE_RECOVERY') != '1':
        raise RuntimeError('explicit_live_test_opt_in_required')
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    observed = get_resource('deployment', SERVICE)
    saved = snapshot(observed, namespace=NAMESPACE, deployment=SERVICE)
    if any((h.get('spec') or {}).get('scaleTargetRef', {}).get('name') == SERVICE
           for h in get_resource('hpa').get('items', [])):
        raise RuntimeError('autoscaled_target_refused')
    service = get_resource('service', SERVICE)
    selector = service['spec'].get('selector')
    if not selector: raise RuntimeError('service_selector_required')
    matching = [p for p in get_resource('pods').get('items', []) if
                all(p['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())]
    if len(matching) != 1: raise RuntimeError('exact_single_ready_backend_required')
    pod = matching[0]
    if (pod['metadata'].get('deletionTimestamp') or not any(c.get('type') == 'Ready' and c.get('status') == 'True'
            for c in pod.get('status', {}).get('conditions', []))):
        raise RuntimeError('ready_backend_required')
    owner = next((o for o in pod['metadata'].get('ownerReferences', []) if o.get('controller')), {})
    if owner.get('kind') != 'ReplicaSet': raise RuntimeError('deployment_ownership_not_proven')
    replica_set = get_resource('replicaset', owner['name'])
    if (replica_set['metadata']['uid'] != owner['uid'] or not any(
            o.get('controller') and o.get('kind') == 'Deployment' and o.get('uid') == saved['uid']
            for o in replica_set['metadata'].get('ownerReferences', []))):
        raise RuntimeError('service_backend_owned_by_other_deployment')
    endpoints = get_resource('endpoints', SERVICE)
    endpoint_uids = {a.get('targetRef', {}).get('uid') for subset in endpoints.get('subsets', [])
                     for a in subset.get('addresses', [])}
    if endpoint_uids != {pod['metadata']['uid']}: raise RuntimeError('ready_endpoint_identity_mismatch')
    if get_resource('networkchaos').get('items'): raise RuntimeError('concurrent_chaos_experiment')
    forward_log = (out / 'forward.log').open('w')
    forward = subprocess.Popen(['kubectl', '-n', NAMESPACE, 'port-forward', 'service/ts-ui-dashboard',
        '18226:8080', '--address=127.0.0.1'], stdout=forward_log, stderr=forward_log)
    contract = {'created_at': now(), 'target': saved, 'service_uid': service['metadata']['uid'],
                'baseline_pod_uid': pod['metadata']['uid'],
                'duration_limit_s': 240, 'truth_in_diagnosis_request': False,
                'business_warmup_budget_s': 120,
                'recovery_acceptance': 'after first valid business response, three consecutive windows of ten successes with healthy controls',
                'action_selection': 'operator_selected_with_server_held_ontology_gate'}
    samples, diagnoses, action_attempts = [], [], []
    error, armed, restored = None, False, False
    def collect(phase, count):
        values = []
        for index in range(count):
            value = business_sample(args.business, phase, index)
            samples.append(value); values.append(value)
            save(out, 'business-samples.json', samples)
            if not value['control_success']: raise RuntimeError('independent_business_control_failed')
            time.sleep(.25)
        return values
    try:
        for _ in range(100):
            if forward.poll() is not None: raise RuntimeError('owned_port_forward_failed')
            if 'Forwarding from 127.0.0.1:18226' in (out / 'forward.log').read_text(): break
            time.sleep(.1)
        else: raise RuntimeError('owned_port_forward_not_ready')
        baseline = collect('baseline', 20)
        if metrics(baseline)['successes'] != 20: raise RuntimeError('healthy_business_baseline_required')
        save(out, 'baseline.json', metrics(baseline))
        if not args.execute:
            return {'status': 'preflight_passed', 'fault_injected': False}
        if args.healthy_diagnosis:
            diagnose(args.api, baseline, out, 'healthy')
        live_service = get_resource('service', SERVICE)
        if (live_service['metadata']['uid'] != service['metadata']['uid']
                or live_service['spec'].get('selector') != selector):
            raise RuntimeError('service_identity_changed')
        if any((h.get('spec') or {}).get('scaleTargetRef', {}).get('name') == SERVICE
               for h in get_resource('hpa').get('items', [])):
            raise RuntimeError('concurrent_autoscaling_enabled')
        contract['deadline_unix'] = time.time() + contract['duration_limit_s']
        saved['injection_deadline_unix'] = contract['deadline_unix']
        save(out, 'contract.json', contract)
        log = (out / 'watchdog.log').open('w')
        watcher = subprocess.Popen([sys.executable, str(Path(__file__).with_name('live_recovery_control.py')),
            '--watchdog', str(out)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        log.close()
        for _ in range(30):
            if watcher.poll() is not None: raise RuntimeError('independent_watchdog_failed')
            if (out / 'watchdog-ready.json').exists(): break
            time.sleep(.1)
        else: raise RuntimeError('independent_watchdog_not_ready')
        # Mark armed before dispatch: response loss must still run restoration.
        armed = True
        contract['injection_at'] = now()
        save(out, 'contract.json', contract)
        contract['injection_outcome'] = scale(saved, restore=False)
        save(out, 'contract.json', contract)
        save(out, 'injection-confirmed.json', {'uid': saved['uid'], 'outcome': contract['injection_outcome']})
        for _ in range(40):
            endpoints = get_resource('endpoints', SERVICE)
            if not any(s.get('addresses') for s in endpoints.get('subsets', [])): break
            time.sleep(.5)
        else: raise RuntimeError('fault_endpoints_not_empty')
        fault = collect('fault', 5)
        if metrics(fault)['successes'] != 0: raise RuntimeError('actual_business_failure_required')
        diagnoses = diagnose(args.api, fault, out, 'fault')
        if time.time() < contract['deadline_unix'] - 35:
            action_attempts = try_approved_recovery(args.api, diagnoses, contract, out)
        # Experiment fallback is separate from self-healing and never counts as
        # autonomous recovery, even when the subsequent business window passes.
        fallback = scale(saved, restore=True)
        save(out, 'fallback-restoration.json', {'outcome': fallback, 'at': now()})
        restored = True
    except Exception as exc:
        error = {'error_type': type(exc).__name__, 'http_status': getattr(exc, 'code', None)}
        if isinstance(exc, (ValueError, RuntimeError)):
            error['reason'] = str(exc)
    finally:
        if armed and not restored:
            try:
                save(out, 'fallback-restoration.json', {'outcome': scale(saved, restore=True,
                    fence_unknown=not bool(contract.get('injection_outcome'))), 'at': now()})
                restored = True
            except Exception as exc:
                save(out, 'restoration-error.json', {'error_type': type(exc).__name__})
        if armed and restored:
            try:
                ready_deadline = time.monotonic() + 120
                while time.monotonic() < ready_deadline:
                    current = read_deployment(saved)
                    if current.get('status', {}).get('readyReplicas') == 1: break
                    time.sleep(1)
                # A recreated Java process can be Pod Ready before its HTTP
                # server/data initialization is usable. Retain every warmup
                # sample; never relabel the original failed recovery windows.
                warmup_started = time.monotonic()
                warmup = []
                while time.monotonic() - warmup_started < contract['business_warmup_budget_s']:
                    value = collect('recovery_warmup', 1)[0]
                    warmup.append(value)
                    if value['business_success']:
                        save(out, 'business-readiness.json', {'first_valid_at': value['at'],
                            'warmup_samples': len(warmup), 'warmup_failures': sum(not r['business_success'] for r in warmup),
                            'observed_warmup_s': round(time.monotonic()-warmup_started, 3)})
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('business_readiness_budget_exhausted')
                recovery = collect('recovery', 30)
                current = read_deployment(saved)
                windows = [metrics(recovery[i:i+10]) for i in range(0, 30, 10)]
                recovered = (current['metadata']['uid'] == saved['uid'] and
                    current['spec']['replicas'] == 1 and current.get('status', {}).get('readyReplicas') == 1 and
                    all(w['successes'] == 10 and w['controls_healthy'] for w in windows))
                save(out, 'recovery-windows.json', windows)
                if recovered:
                    save(out, 'cleanup-complete.json', {'restored': True, 'at': now()})
                    try:
                        watcher.wait(timeout=3)
                        save(out, 'fence-cleanup.json', {'released': release_fence(saved)})
                    except subprocess.TimeoutExpired:
                        save(out, 'fence-cleanup.json', {'released': False, 'reason': 'watchdog_still_finishing'})
            except Exception as exc:
                error = error or {'error_type': type(exc).__name__}
        forward.terminate()
        try: forward.wait(timeout=5)
        except subprocess.TimeoutExpired: forward.kill(); forward.wait()
        forward_log.close()
        report = {'finished_at': now(), 'watchdog_armed': armed,
            'fault_dispatched': True if contract.get('injection_outcome') else None if armed else False,
            'injection_outcome': contract.get('injection_outcome', 'unknown' if armed else 'not_attempted'),
            'restored': restored,
            'business_recovery_confirmed': (out / 'cleanup-complete.json').exists(),
            'baseline': metrics([s for s in samples if s['phase'] == 'baseline']),
            'fault': metrics([s for s in samples if s['phase'] == 'fault']),
            'diagnosis_runs': [{k: v for k, v in r.items() if k not in {'ontology', 'started_monotonic'}} for r in diagnoses],
            'action_attempts': action_attempts, 'error': error, 'accuracy_requires_independent_review': True,
            'production_replaced': False, 'model_accuracy_claimed': False}
        save(out, 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--api', default='http://127.0.0.1:18112')
    parser.add_argument('--business', default='http://127.0.0.1:18226')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--healthy-diagnosis', action='store_true')
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False))
