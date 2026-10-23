"""Evidence-bound Job diagnostics; no recovery mutations."""
import re


def _target(payload):
    job, namespace = payload.get('job'), payload.get('namespace')
    valid = lambda value: isinstance(value, str) and bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', value))
    return (job, namespace) if valid(job) and valid(namespace) else ('', '')


def detect(context):
    payload = context.get('payload') or {}
    job, _ = _target(payload)
    matched = bool(job) and payload.get('fault_type', '') in {'', 'job_failed', 'job_failure_investigation'}
    return {'status': 'matched' if matched else 'skipped', 'confidence': 0,
            'summary': 'Job 目标待核查' if matched else '不属于明确的 Job 调查', 'evidence': []}


def diagnose(context):
    payload = context.get('payload') or {}
    job, namespace = _target(payload)
    evidence = payload.get('agent_evidence') or {}
    event = evidence.get('event_agent') if isinstance(evidence, dict) else {}
    event = event if isinstance(event, dict) else {}
    facts = []
    for item in event.get('evidence_items') or []:
        if not isinstance(item, dict) or item.get('signal_type') != 'container_terminated':
            continue
        raw = item.get('raw_ref')
        if not isinstance(raw, dict) or not job:
            continue
        if (raw.get('namespace') == namespace and raw.get('job_name') == job and raw.get('job_uid')
                and raw.get('name') and raw.get('container') and type(raw.get('exit_code')) is int
                and raw['exit_code'] != 0):
            facts.append(raw)
    descriptions = [f"Pod/{f['name']} 容器/{f['container']} 退出码 {f['exit_code']}（{f.get('reason', '')}），控制器 Job/{job} UID={f['job_uid']}" for f in facts]
    root = ('；'.join(descriptions) + '。已证实 Job 所属容器异常退出；Job 条件及更上游业务原因需结合 Job 状态、命令和日志继续核查。'
            if facts else '缺少匹配 Job 归属的当前非零退出证据，暂不能确认任务失败原因。')
    return {'status': 'ok', 'confidence': 0.7 if facts else 0,
            'summary': root, 'root_cause': root, 'fault_type': 'job_failed' if facts else 'unknown',
            'root_cause_component': job, 'evidence': descriptions, 'observations': facts}


def verify(context):
    job, namespace = _target(context.get('payload') or {})
    checks = [f'kubectl get job/{job} -n {namespace} -o json',
              f'kubectl get pods -n {namespace} -l batch.kubernetes.io/job-name={job} -o json'] if job else []
    return {'status': 'ok' if checks else 'skipped', 'confidence': 0,
            'summary': '仅生成只读核查计划；尚未验证业务恢复。', 'evidence': [], 'checks': checks,
            'executed': False, 'recovery_verified': False}
