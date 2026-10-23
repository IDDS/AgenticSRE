"""Feedback withdrawal policy shared by writers and every rule consumer.

The feedback ledger is the authority: withdrawal never depends on a second
write to JSON rules or Chroma. Only future consumption is affected.
"""
import os
import json
import threading
from pathlib import Path

from memory.feedback_contract import FeedbackStorageError, digest, read_list

_cache_lock = threading.Lock()
_cache = {}


def feedback_directory():
    return Path(os.environ.get('AGENTICSRE_FEEDBACK_DIR', './data/expert_feedback')).expanduser()


def feedback_revision(entry):
    return digest({key: entry.get(key, default) for key, default in (
        ('feedback_id', ''), ('request_sha256', ''), ('feedback_state', 'active'), ('feedback_seq', 0))})


def ledger_snapshot():
    path = feedback_directory() / 'feedback_entries.json'
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise FeedbackStorageError('feedback_policy_unavailable') from exc
    stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    # Atomic replacement changes the inode. Never cache a failed read.
    with _cache_lock:
        cached = _cache.get(str(path.absolute()))
        if cached is not None and cached[0] == stamp:
            return cached[1]
        entries = tuple(read_list(path))
        ids = [entry.get('feedback_id') for entry in entries if entry.get('feedback_id')]
        if len(ids) != len(set(ids)):
            raise FeedbackStorageError('duplicate_feedback_identity')
        _cache[str(path.absolute())] = (stamp, entries)
        return entries


def rule_block_reason(rule):
    """Conservatively quarantine affected votes; do not invent counter rollbacks."""
    entries = ledger_snapshot()
    by_id = {entry.get('feedback_id'): entry for entry in entries}
    source = rule.get('source_feedback_id')
    if source:
        origin = by_id.get(source)
        if origin is None:
            return 'source_feedback_missing'
        if origin.get('feedback_state', 'active') != 'active':
            return 'source_feedback_retracted'
        if origin.get('learning_status') != 'success':
            return 'source_feedback_unsettled'
    try:
        votes = json.loads(rule.get('feedback_vote_ids', '[]'))
    except (ValueError, TypeError) as exc:
        raise FeedbackStorageError('rule_feedback_dependencies_invalid') from exc
    if not isinstance(votes, list) or any(not isinstance(key, str) for key in votes):
        raise FeedbackStorageError('rule_feedback_dependencies_invalid')
    if any(key not in by_id or by_id[key].get('feedback_state', 'active') != 'active' for key in votes):
        return 'rule_vote_feedback_retracted_or_missing'
    if any(by_id[key].get('learning_status') != 'success' for key in votes):
        return 'rule_vote_feedback_unsettled'
    # Legacy recorded votes lacked an attribution field on the rule. Bind them
    # to their original revision; an edit/reset must not inherit this evidence.
    from memory.fault_context_store import FaultContextStore
    revision = FaultContextStore.rule_revision(rule)
    for entry in entries:
        if entry.get('feedback_state', 'active') == 'active':
            continue
        for vote in entry.get('rule_vote_results', []):
            if (vote.get('rule_id') == rule.get('rule_id') and vote.get('revision') == revision
                    and vote.get('updated', 0)):
                return 'rule_vote_feedback_retracted_or_missing'
    return ''
