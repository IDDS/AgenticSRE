"""Durable feedback receipts and content identities; no model or backend access."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


class FeedbackConflict(ValueError):
    """A replay attempts to change an already consumed request."""


class FeedbackStorageError(RuntimeError):
    """Stored state is unavailable; never replace it with an empty ledger."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def diagnosis_revision(result):
    # Exclude completion telemetry added after HITL creation; retain the
    # diagnosis, its rationale, target identity and explicitly reported scope.
    fields = ('root_cause', 'reasoning_chain', 'fault_type', 'root_entity',
              'incident_target', 'namespace', 'cluster_id', 'supporting_evidence_ids')
    return digest({key: result.get(key) for key in fields})


@contextmanager
def file_lock(path):
    with Path(path).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_list(path):
    if not Path(path).exists():
        return []
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise FeedbackStorageError('feedback_storage_unavailable') from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise FeedbackStorageError('feedback_storage_invalid')
    return value


def atomic_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
