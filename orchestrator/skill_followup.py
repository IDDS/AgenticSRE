"""One bounded, identity-fenced read-only follow-up for unresolved exclusions."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import json
import shlex
import time

from skill_runtime.service_evidence import projection_command


def collect_followup(*, namespace, cluster_id, root_entity, service_scope, hypotheses, run_read,
                     per_read_timeout_s=5, total_budget_s=15):
    pending = [h for h in hypotheses if h.get('status') == 'active'
        and h.get('proposed_status') == 'rejected' and 'unverified_rejection' in (h.get('validation_errors') or [])]
    record = {'schema': 'skill-followup/v1', 'attempted': False, 'candidate_ids': [h.get('id') for h in pending],
              'commands': [], 'stop_reason': 'no_unverified_rejection', 'execution_authorized': False}
    if not pending:
        return record, []
    candidates = service_scope.get('candidate_workloads') if isinstance(service_scope, dict) else None
    if (not isinstance(root_entity, dict) or root_entity.get('kind') != 'Deployment'
            or root_entity.get('cluster_id') != cluster_id or root_entity.get('namespace') != namespace
            or not isinstance(root_entity.get('uid'), str) or not root_entity['uid']
            or not isinstance(root_entity.get('resource_version'), str) or not root_entity['resource_version']
            or not isinstance(service_scope, dict) or service_scope.get('status') != 'collected'
            or not isinstance(candidates, list) or candidates != [root_entity.get('name')]):
        record['stop_reason'] = 'target_identity_or_scope_unresolved'
        return record, []
    try:
        identity_command = projection_command('Deployment', namespace, root_entity['name'])
        event_command = shlex.join(['get', 'events', '-n', namespace,
            '--field-selector=involvedObject.uid=' + root_entity['uid'], '--sort-by=.lastTimestamp'])
    except (ValueError, TypeError, KeyError):
        record['stop_reason'] = 'invalid_target_identity'
        return record, []
    # UID is a selector value, never shell syntax or an arbitrary model command.
    if not all(c.isalnum() or c in '-.' for c in root_entity['uid']) or len(root_entity['uid']) > 128:
        record['stop_reason'] = 'invalid_target_identity'
        return record, []
    record['attempted'] = True
    deadline = time.monotonic() + total_budget_s
    reads = []
    for command in (identity_command, event_command, identity_command):
        remaining = min(per_read_timeout_s, deadline - time.monotonic())
        if remaining <= 0:
            record['stop_reason'] = 'followup_budget_exhausted'
            break
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(run_read, command)
        try:
            result = future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            record['stop_reason'] = 'followup_read_timeout'
            break
        except Exception:
            record['stop_reason'] = 'followup_read_failed'
            break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not isinstance(result, dict):
            record['stop_reason'] = 'followup_read_failed'
            break
        result = {**result, 'timestamp': datetime.now(timezone.utc).isoformat(),
                  'cluster_id': cluster_id, 'namespace': namespace}
        reads.append(result)
        record['commands'].append({'command': command, 'success': result.get('success') is True,
                                   'executed': result.get('executed') is True})
        if result.get('success') is not True or result.get('executed') is not True or result.get('outcome_unknown'):
            record['stop_reason'] = 'followup_read_failed'
            break
        if command == identity_command:
            try:
                item = json.loads(result.get('data') or '')
                metadata = item['metadata']
                unchanged = (item.get('kind') == 'Deployment' and metadata['namespace'] == namespace
                    and metadata['name'] == root_entity['name'] and metadata['uid'] == root_entity['uid']
                    and metadata['resourceVersion'] == root_entity['resource_version'])
            except (ValueError, TypeError, KeyError):
                unchanged = False
            if not unchanged:
                record['stop_reason'] = 'target_changed_or_unverifiable'
                break
    else:
        record['stop_reason'] = 'followup_collected_review_required'
    return record, reads
