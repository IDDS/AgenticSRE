"""
AgenticSRE Expert Feedback Store
Persists expert feedback and activates supervised learning via ContextLearner.learn_supervised().
SOW: "利用专家反馈...构建智能体上下文，实现多智能体运维能力的持续演化"
"""

from __future__ import annotations

import json
import logging
import os
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from confidence_statistics import clopper_pearson_lower_bound
from memory.feedback_contract import (FeedbackConflict, atomic_json, diagnosis_revision, digest, file_lock, read_list)
from memory.feedback_lifecycle import feedback_directory, feedback_revision

logger = logging.getLogger(__name__)

class ExpertFeedbackStore:
    """
    Stores expert assertions and model-comparison receipts. Replays never
    repeat learning; rule usefulness requires explicit versioned feedback.

    Usage:
        store = ExpertFeedbackStore()
        store.submit_feedback("rca-001", "OOMKill root cause", context_learner=learner)
    """

    def __init__(self, feedback_dir: Optional[str] = None):
        self._dir = Path(feedback_dir) if feedback_dir is not None else feedback_directory()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._feedback_file = self._dir / "feedback_entries.json"
        self._entries: List[Dict] = []
        self._load()

    def _load(self):
        self._entries = read_list(self._feedback_file)

    def _save(self):
        atomic_json(self._feedback_file, self._entries)

    @staticmethod
    def _response(entry, replayed=False):
        keys = ('feedback_id', 'incident_id', 'learning_status', 'rules_generated',
                'rules_voted', 'correct', 'correct_source', 'diagnosis_revision',
                'trace_snapshot_sha256', 'rule_vote_results')
        keys += ('rule_status', 'rules_activated', 'candidate_rule_ids', 'rule_results')
        result = {key: entry[key] for key in keys if key in entry}
        result.update(feedback_state=entry.get('feedback_state', 'active'),
                      feedback_revision=feedback_revision(entry))
        result['replayed'] = replayed
        if replayed and entry['learning_status'] == 'processing':
            # A durable claim may outlive its process. Never repeat unknown writes.
            result['learning_status'] = 'outcome_unknown'
        return result

    def submit_feedback(self, incident_id, expert_diagnosis, comment='',
                        context_learner=None, trace_store=None, fault_store=None,
                        *, event_id=None, expected_diagnosis_revision=None,
                        rule_feedback=None, event_scope='expert'):
        """Consume one durable event; incident labels never imply rule credit.

        Locks serialize processes sharing this directory. The processing receipt
        is durable before any model or knowledge writes. A crash leaves unknown
        effects and its event cannot automatically execute again.
        """
        for value, maximum in ((incident_id, 200), (expert_diagnosis, 8000), (comment, 4000)):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError('invalid_feedback_text')
        incident_id, expert_diagnosis = incident_id.strip(), expert_diagnosis.strip()
        if not incident_id or not expert_diagnosis:
            raise ValueError('incident_and_expert_diagnosis_required')
        if event_id is not None and (not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', event_id)):
            raise ValueError('invalid_feedback_event_id')
        if expected_diagnosis_revision is not None and (not isinstance(expected_diagnosis_revision, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_diagnosis_revision)):
            raise ValueError('invalid_diagnosis_revision')
        judgments = [] if rule_feedback is None else rule_feedback
        if not isinstance(judgments, list) or len(judgments) > 5:
            raise ValueError('invalid_rule_feedback')
        ids = set()
        for judgment in judgments:
            if (not isinstance(judgment, dict) or set(judgment) != {'rule_id', 'revision', 'useful'}
                    or not isinstance(judgment['rule_id'], str) or not judgment['rule_id']
                    or not isinstance(judgment['revision'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', judgment['revision'])
                    or type(judgment['useful']) is not bool or judgment['rule_id'] in ids):
                raise ValueError('invalid_rule_feedback')
            ids.add(judgment['rule_id'])
        request = {'incident_id': incident_id, 'expert_diagnosis': expert_diagnosis,
                   'expected_revision': expected_diagnosis_revision, 'rule_feedback': judgments}
        request_hash = digest(request)
        # Explicit event IDs are immutable. Legacy clients get a stable retry key.
        key = 'fb-' + digest({'scope': event_scope, 'event': event_id if event_id is not None else request_hash})[:32]
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
            prior = next((entry for entry in self._entries if entry.get('feedback_id') == key), None)
            if prior:
                if prior.get('request_sha256') != request_hash:
                    raise FeedbackConflict('feedback_event_already_consumed_with_different_content')
                return self._response(prior, replayed=True)
            entry = dict(feedback_id=key, incident_id=incident_id, expert_diagnosis=expert_diagnosis,
                         comment=comment, timestamp=time.time(), request_sha256=request_hash,
                         learning_status='blocked_no_trace', rules_generated=0, rules_voted=0,
                         correct=None, correct_source='model_comparison_to_expert_text', trace_matched=False,
                         rule_vote_results=[], feedback_state='active', feedback_seq=0, feedback_history=[])
            trace_result = None
            if trace_store is not None:
                try:
                    trace = trace_store.get_trace_by_id(incident_id)
                    candidate = trace.result if trace is not None else None
                    if isinstance(candidate, dict) and (candidate.get('root_cause') or candidate.get('reasoning_chain')):
                        trace_result = json.loads(json.dumps(candidate, allow_nan=False))
                except Exception:
                    entry['learning_status'] = 'blocked_trace_unavailable'
            if trace_result is not None:
                entry.update(trace_matched=True, diagnosis_revision=diagnosis_revision(trace_result),
                    trace_snapshot_sha256=digest(trace_result),
                    agent_root_cause=str(trace_result.get('root_cause') or ''),
                    fault_type=str(trace_result.get('fault_type') or ''),
                    error_pattern=str(trace_result.get('error_pattern') or ''),
                    cluster_id=str(trace_result.get('cluster_id') or ''),
                    namespace=str(trace_result.get('namespace') or ''),
                    workload_version=str(trace_result.get('workload_version') or ''),
                    entity_uid=str(trace_result.get('entity_uid') or ''))
                refs = {ref['rule_id']: ref['revision'] for ref in trace_result.get('recalled_rule_refs', [])
                        if isinstance(ref, dict) and isinstance(ref.get('rule_id'), str)
                        and isinstance(ref.get('revision'), str)}
                if any(refs.get(j['rule_id']) != j['revision'] for j in judgments):
                    raise FeedbackConflict('rule_feedback_not_bound_to_original_diagnosis')
                if expected_diagnosis_revision and expected_diagnosis_revision != entry['diagnosis_revision']:
                    entry['learning_status'] = 'blocked_stale_diagnosis'
                else:
                    entry['learning_status'] = 'processing' if context_learner is not None else 'no_learner'
            self._entries.append(entry)
            self._save()  # Claim first; no effects have occurred if this write fails.
            if entry['learning_status'] != 'processing':
                return self._response(entry)
        try:
            original = 'Root cause: ' + entry['agent_root_cause'] + '\nReasoning: ' + str(trace_result.get('reasoning_chain') or '')[:1000]
            from memory.memory_scope import MemoryScope
            source_scope = MemoryScope(cluster_id=entry.get('cluster_id', ''), namespace=entry.get('namespace', ''),
                workload_version=entry.get('workload_version', ''), entity_uid=entry.get('entity_uid', ''))
            learned = context_learner.learn_supervised(agent_diagnosis=original, ground_truth=expert_diagnosis,
                memory_scope=source_scope, source_incident_id=incident_id, source_feedback_id=key)
            if not isinstance(learned, dict):
                learned = {}
            valid = learned.get('status') == 'success' and type(learned.get('correct')) is bool
            entry['learning_status'] = 'success' if valid else ('invalid_correctness' if learned.get('status') == 'success' else learned.get('status', 'outcome_unknown'))
            if entry['learning_status'] == 'error':
                entry['learning_status'] = 'outcome_unknown'
            entry['correct'] = learned.get('correct') if valid else None
            entry['rules_generated'] = int(learned.get('rules_added', 0)) if valid else 0
            entry['generated_rule_ids'] = learned.get('rule_ids', []) if valid else []
            if valid:
                entry['rule_status'] = learned.get('rule_status', 'unknown')
                entry['rules_activated'] = learned.get('rules_activated')
                entry['candidate_rule_ids'] = learned.get('candidate_rule_ids', [])
                entry['rule_results'] = learned.get('rule_results', [])
            if entry['learning_status'] == 'outcome_unknown':
                entry['rules_generated'] = None
                entry['rules_voted'] = None
            if valid and fault_store is not None:
                for judgment in judgments:
                    voted = fault_store.record_rule_usage([judgment['rule_id']], positive=judgment['useful'],
                        expected_revisions={judgment['rule_id']: judgment['revision']}, feedback_id=key)
                    entry['rules_voted'] += voted.get('updated', 0)
                    entry['rule_vote_results'].append(dict(rule_id=judgment['rule_id'],
                        revision=judgment['revision'], useful=judgment['useful'], **voted))
        except Exception as exc:
            entry['learning_status'] = 'outcome_unknown'
            entry['correct'] = None
            entry['rules_generated'] = None
            entry['rules_voted'] = None
            entry['error_code'] = type(exc).__name__
            logger.warning('Feedback effects incomplete (%s)', type(exc).__name__)
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
            index = next((i for i, current in enumerate(self._entries) if current.get('feedback_id') == key), None)
            if index is None or self._entries[index].get('request_sha256') != request_hash:
                raise FeedbackConflict('feedback_claim_changed_after_effects')
            # A completed label was not available when its earlier claim was
            # created. Freeze knowledge availability at finalization time.
            entry['recorded_at'] = time.time()
            current = self._entries[index]
            for field in ('feedback_state', 'feedback_seq', 'feedback_history', 'retracted_at'):
                if field in current:
                    entry[field] = current[field]
            self._entries[index] = entry
            self._save()
        return self._response(entry)

    def retract_feedback(self, feedback_id, *, revision, reviewer, reason, event_id,
                         identity_source='operator_label'):
        """Atomically revoke future use while preserving claims and late receipts."""
        if self._dir.resolve() != feedback_directory().resolve():
            raise ValueError('feedback_policy_directory_mismatch')
        from memory.rule_lifecycle import decision_request
        request, request_hash = decision_request(revision, 'deactivate', reviewer, reason, event_id, identity_source)
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
            entry = next((row for row in self._entries if row.get('feedback_id') == feedback_id), None)
            if entry is None:
                raise KeyError(feedback_id)
            events = entry.get('feedback_history', [])
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                from memory.feedback_contract import FeedbackStorageError
                raise FeedbackStorageError('feedback_history_invalid')
            prior = next((event for event in events if event.get('event_id') == event_id), None)
            if prior:
                if prior.get('request_sha256') != request_hash:
                    raise FeedbackConflict('feedback_retraction_event_conflict')
                return {**self._response(entry, replayed=True), 'retraction': prior}
            if revision != feedback_revision(entry):
                raise FeedbackConflict('feedback_revision_conflict')
            if entry.get('feedback_state', 'active') != 'active':
                raise FeedbackConflict('feedback_already_retracted')
            before = feedback_revision(entry)
            entry.update(feedback_state='retracted', feedback_seq=int(entry.get('feedback_seq', 0)) + 1,
                         retracted_at=time.time())
            event = {**request, 'operation': 'retract', 'event_id': event_id, 'request_sha256': request_hash,
                     'at': entry['retracted_at'], 'before_revision': before, 'revision': feedback_revision(entry)}
            entry['feedback_history'] = [*events, event]
            self._save()  # This same authority gates JSON and Chroma consumption.
            return {**self._response(entry), 'retraction': event}


    def get_recent_feedback(self, n=10, *, for_context=False, memory_scope=None):
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
            entries = self._entries
            if for_context:
                # Reuse reviewed derived rules. A successful comparison is not
                # approval to inject the original free-text opinion as memory.
                entries = []
            if memory_scope is not None:
                from memory.memory_scope import filter_memory
                entries = filter_memory(entries, memory_scope, 'feedback')[0]
            count = max(0, int(n))
            return [{**entry, 'feedback_state': entry.get('feedback_state', 'active'),
                     'feedback_revision': feedback_revision(entry)}
                    for entry in reversed(entries[-count:])] if count else []

    def diagnosis_stats(
        self,
        *,
        fault_type: str = "",
        error_pattern: str = "",
        cluster_id: str = "",
        limit: int = 1000,
        confidence_level: float = 0.95,
        memory_scope=None,
    ) -> Dict[str, Any]:
        """Aggregate model comparisons once per incident, with source disclosed.

        Only feedback entries with an explicit boolean ``correct`` label count.
        Recovery success or failure is intentionally not read here.
        """
        fault_key = str(fault_type or "").strip().lower()
        pattern_key = str(error_pattern or "").strip().lower()
        cluster_key = str(cluster_id or "").strip().lower()
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
            entries = list(self._entries)
        if memory_scope is not None:
            from memory.memory_scope import filter_memory
            entries = filter_memory(entries, memory_scope, 'feedback')[0]
        entries = entries[-max(1, int(limit)):]
        # Repeated opinions/revisions of one incident are not independent trials.
        latest = {}
        for entry in entries:
            # A newer withdrawn or pending assertion is a barrier, not a reason
            # to silently resurrect an older label for the same incident.
            if entry.get('feedback_state', 'active') != 'active' or entry.get('learning_status') == 'processing':
                latest[entry.get('incident_id') or entry.get('feedback_id')] = None
            elif type(entry.get('correct')) is bool and entry.get('learning_status') == 'success':
                latest[entry.get('incident_id') or entry.get('feedback_id')] = entry
        matched: List[Dict[str, Any]] = []
        for entry in latest.values():
            if entry is None or not isinstance(entry.get("correct"), bool):
                continue
            entry_fault = str(entry.get("fault_type") or "").strip().lower()
            entry_pattern = str(entry.get("error_pattern") or "").strip().lower()
            entry_cluster = str(entry.get("cluster_id") or "").strip().lower()
            if cluster_key and entry_cluster != cluster_key:
                continue
            if pattern_key and entry_pattern == pattern_key:
                matched.append(entry)
            elif fault_key and entry_fault == fault_key:
                matched.append(entry)

        sample_count = len(matched)
        correct_count = sum(1 for entry in matched if entry.get("correct") is True)
        accuracy = correct_count / sample_count if sample_count else None
        lower_bound = (
            clopper_pearson_lower_bound(
                correct_count,
                sample_count,
                confidence_level=confidence_level,
            )
            if sample_count
            else None
        )
        return {
            "history_kind": "diagnosis_correctness",
            "label_basis": "model_comparison_to_expert_text",
            "independent_truth_verified": False,
            "sample_unit": "unique_incident",
            "fault_type": fault_type,
            "error_pattern": error_pattern,
            "cluster_id": cluster_id,
            "diagnosis_sample_count": sample_count,
            "diagnosis_correct_count": correct_count,
            "diagnosis_incorrect_count": sample_count - correct_count,
            "diagnosis_accuracy": round(accuracy, 3) if accuracy is not None else None,
            "diagnosis_accuracy_lower_bound": round(lower_bound, 3) if lower_bound is not None else None,
            "confidence_level": confidence_level,
        }

    def get_feedback_stats(self) -> Dict:
        """Aggregate feedback statistics."""
        with file_lock(self._dir / 'feedback.lock'):
            self._load()
        total = len(self._entries)
        with_rules = sum(1 for e in self._entries if (e.get("rules_generated") or 0) > 0)
        total_rules = sum(e.get("rules_generated") or 0 for e in self._entries)
        return {
            "total": total,
            "retracted": sum(e.get('feedback_state') == 'retracted' for e in self._entries),
            "active": sum(e.get('feedback_state', 'active') == 'active' for e in self._entries),
            "with_rules": with_rules,
            "total_rules_generated": total_rules,
            "counts_basis": "historical_generation_receipts_not_active_knowledge",
            "success_rate": with_rules / max(total, 1),
        }
