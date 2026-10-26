"""Identity-bound scale experiment controls for remote, non-production acceptance.

No command runs on import. Snapshots contain hashes, not workload configuration
or credentials. The watchdog is independent of the diagnosis process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any

FENCE_ANNOTATION = 'agenticsre.io/live-recovery-fence'


def spec_fingerprint(resource: dict[str, Any]) -> str:
    spec = dict(resource['spec'])
    spec.pop('replicas', None)
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(resource: dict[str, Any], *, namespace: str, deployment: str) -> dict[str, Any]:
    metadata = resource['metadata']
    replicas = resource['spec'].get('replicas', 1)
    if (resource.get('kind') != 'Deployment' or metadata.get('namespace') != namespace
            or metadata.get('name') != deployment or not metadata.get('uid')
            or not metadata.get('resourceVersion') or metadata.get('deletionTimestamp')):
        raise ValueError('deployment_identity_incomplete')
    if namespace.startswith('kube-') or namespace in {'default', 'production', 'prod'}:
        raise ValueError('dedicated_testbed_required')
    if FENCE_ANNOTATION in (metadata.get('annotations') or {}):
        raise ValueError('previous_recovery_fence_requires_cleanup')
    if type(replicas) is not int or replicas != 1:
        raise ValueError('single_replica_testbed_required')
    status = resource.get('status') or {}
    if status.get('readyReplicas') != replicas or status.get('observedGeneration') != metadata.get('generation'):
        raise ValueError('stable_healthy_deployment_required')
    return {'namespace': namespace, 'deployment': deployment, 'uid': metadata['uid'],
            'original_replicas': replicas, 'spec_sha256': spec_fingerprint(resource),
            'fence_token': uuid.uuid4().hex}


def scale_patch(saved: dict[str, Any], current: dict[str, Any], *, restore: bool,
                fence_unknown: bool = False) -> list[dict[str, Any]] | None:
    metadata = current['metadata']
    if (current.get('kind') != 'Deployment' or metadata.get('namespace') != saved['namespace']
            or metadata.get('name') != saved['deployment'] or metadata.get('uid') != saved['uid']
            or not metadata.get('resourceVersion') or metadata.get('deletionTimestamp')):
        raise ValueError('deployment_identity_changed')
    if spec_fingerprint(current) != saved['spec_sha256']:
        raise ValueError('concurrent_deployment_edit')
    replicas = current['spec'].get('replicas', 1)
    if type(replicas) is not int:
        raise ValueError('invalid_replica_count')
    original = saved['original_replicas']
    if type(original) is not int or original != 1:
        raise ValueError('invalid_saved_replica_count')
    annotations = dict(metadata.get('annotations') or {})
    if not restore and FENCE_ANNOTATION in annotations:
        raise ValueError('experiment_already_fenced')
    if not restore and 'injection_deadline_unix' in saved:
        deadline = saved['injection_deadline_unix']
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or time.time() >= deadline:
            raise ValueError('injection_deadline_passed')
    if restore and replicas == original:
        if not fence_unknown or annotations.get(FENCE_ANNOTATION) == saved['fence_token']:
            return None
        if FENCE_ANNOTATION in annotations:
            raise ValueError('concurrent_recovery_fence')
        # An unacknowledged injection can still be in flight. A real metadata
        # update invalidates its old resourceVersion; a replica-count no-op
        # cannot supply that barrier. Never dispatch after this fence exists.
        annotations[FENCE_ANNOTATION] = saved['fence_token']
        return [{'op': 'test', 'path': '/metadata/uid', 'value': saved['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': metadata['resourceVersion']},
                {'op': 'test', 'path': '/spec/replicas', 'value': original},
                {'op': 'add', 'path': '/metadata/annotations', 'value': annotations}]
    expected, desired = (0, original) if restore else (original, 0)
    if replicas != expected:
        raise ValueError('concurrent_scale_change')
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': saved['uid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': metadata['resourceVersion']},
            {'op': 'test', 'path': '/spec/replicas', 'value': expected},
            {'op': 'replace', 'path': '/spec/replicas', 'value': desired}]
    if restore and fence_unknown:
        # Also fence an observed zero. Otherwise the suspended original caller
        # could reread this restored one and dispatch another zero after the
        # independent watchdog has finished.
        if FENCE_ANNOTATION in annotations and annotations[FENCE_ANNOTATION] != saved['fence_token']:
            raise ValueError('concurrent_recovery_fence')
        annotations[FENCE_ANNOTATION] = saved['fence_token']
        patch.append({'op': 'add', 'path': '/metadata/annotations', 'value': annotations})
    return patch


def read_deployment(saved: dict[str, Any]) -> dict[str, Any]:
    process = subprocess.run(['kubectl', '-n', saved['namespace'], 'get', 'deployment',
                              saved['deployment'], '-o', 'json'], capture_output=True, text=True, timeout=15)
    if process.returncode:
        raise RuntimeError('deployment_read_failed')
    return json.loads(process.stdout)


def scale(saved: dict[str, Any], *, restore: bool, fence_unknown: bool = False) -> str:
    # Setting an absolute replica count is convergent. Every bounded attempt
    # repeats identity/spec/replica checks; an observed desired state stops it.
    for _ in range(3):
        patch = scale_patch(saved, read_deployment(saved), restore=restore, fence_unknown=fence_unknown)
        if patch is None:
            return 'already_restored'
        try:
            process = subprocess.run(['kubectl', '-n', saved['namespace'], 'patch', 'deployment',
                saved['deployment'], '--type=json', '-p', json.dumps(patch)],
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            process = None  # Lost acknowledgement: inspect before any retry.
        fencing = patch[-1]['path'] == '/metadata/annotations'
        if process is not None and not process.returncode:
            return 'fenced_restored' if fencing else 'restored' if restore else 'injected'
        # Do not interpret an arbitrary failure as "not executed". A subsequent
        # invocation, including the watchdog, must inspect the live object first.
        latest = read_deployment(saved)
        expected = saved['original_replicas'] if restore else 0
        if (latest['metadata'].get('uid') == saved['uid']
                and spec_fingerprint(latest) == saved['spec_sha256']
                and latest['spec'].get('replicas') == expected
                and (not fencing or latest['metadata'].get('annotations', {}).get(FENCE_ANNOTATION) == saved['fence_token'])):
            return 'observed_restored' if restore else 'observed_injected'
        if latest['metadata'].get('resourceVersion') == patch[1]['value']:
            raise RuntimeError('scale_outcome_unknown' if process is None else 'scale_patch_failed')
    raise RuntimeError('scale_patch_conflict_budget')


def release_fence(saved: dict[str, Any]) -> bool:
    """Remove only this experiment's barrier, after its watchdog has exited."""
    current = read_deployment(saved)
    # Reuse all identity/spec/replica guards before touching metadata.
    if scale_patch(saved, current, restore=True) is not None:
        raise ValueError('fence_release_requires_original_replica_count')
    annotations = current['metadata'].get('annotations') or {}
    if FENCE_ANNOTATION not in annotations:
        return True
    if annotations[FENCE_ANNOTATION] != saved['fence_token']:
        raise ValueError('concurrent_recovery_fence')
    pointer = '/metadata/annotations/' + FENCE_ANNOTATION.replace('~', '~0').replace('/', '~1')
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': saved['uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
             {'op': 'test', 'path': pointer, 'value': saved['fence_token']}, {'op': 'remove', 'path': pointer}]
    try:
        subprocess.run(['kubectl', '-n', saved['namespace'], 'patch', 'deployment', saved['deployment'],
                        '--type=json', '-p', json.dumps(patch)], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        pass
    latest = read_deployment(saved)
    return (latest['metadata'].get('uid') == saved['uid'] and
            FENCE_ANNOTATION not in (latest['metadata'].get('annotations') or {}))


def watchdog(directory: Path) -> None:
    contract = json.loads((directory / 'contract.json').read_text())
    (directory / 'watchdog-ready.json').write_text(json.dumps({'ready': True, 'deadline': contract['deadline_unix']}))
    while time.time() < contract['deadline_unix']:
        if (directory / 'cleanup-complete.json').exists():
            return
        time.sleep(.25)
    result: dict[str, Any] = {'attempted': True, 'at_unix': time.time(), 'attempts': 0}
    try:
        receipt = json.loads((directory / 'injection-confirmed.json').read_text())
        confirmed = (receipt.get('uid') == contract['target']['uid']
                     and receipt.get('outcome') in {'injected', 'observed_injected'})
    except (OSError, ValueError, KeyError):
        confirmed = False
    retry_deadline = time.monotonic() + 60
    while time.monotonic() < retry_deadline:
        result['attempts'] += 1
        try:
            result['outcome'] = scale(contract['target'], restore=True, fence_unknown=not confirmed)
            break
        except json.JSONDecodeError as exc:
            result.update(outcome='restore_not_confirmed', error_type=type(exc).__name__)
        except ValueError as exc:
            # Identity/spec/concurrent scale changes require operator review.
            # Never turn a safety refusal into an unbounded recovery loop.
            result.update(outcome='restore_refused', error_type=type(exc).__name__)
            break
        except Exception as exc:
            result.update(outcome='restore_not_confirmed', error_type=type(exc).__name__)
        time.sleep(1)
    (directory / 'watchdog-result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watchdog', type=Path, required=True)
    watchdog(parser.parse_args().watchdog)
