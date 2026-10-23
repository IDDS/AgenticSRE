"""
Human-in-the-loop review queue for RCA quality gates and remediation approvals.

The queue is intentionally file-backed so it works in the cluster deployment
without adding another service dependency.
"""

from __future__ import annotations

import json
import time
import uuid
import copy

from memory.feedback_contract import FeedbackConflict, atomic_json, diagnosis_revision, file_lock, read_list
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_REVIEW_DIR = "./data/hitl"


class HumanReviewStore:
    """Persistent HITL review queue."""

    def __init__(self, review_dir: Optional[str] = None):
        self._dir = Path(review_dir or _DEFAULT_REVIEW_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._review_file = self._dir / "reviews.json"
        self._reviews: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        self._reviews = read_list(self._review_file)

    def _save(self):
        atomic_json(self._review_file, self._reviews)

    def create_review(
        self,
        incident_id: str,
        reason: str,
        rca_result: Dict[str, Any],
        judge: Optional[Dict[str, Any]] = None,
        priority: str = "medium",
        source: str = "quality_gate",
    ) -> Dict[str, Any]:
        """Create or return an open review for the same incident/source."""
        with file_lock(self._dir / 'reviews.lock'):
            self._load()
            for review in self._reviews:
                if (
                    review.get("incident_id") == incident_id
                    and review.get("source") == source
                    and review.get("status") == "pending"
                ):
                    return review

            review = {
                "review_id": f"hitl-{uuid.uuid4().hex[:8]}",
                "incident_id": incident_id,
                "source": source,
                "reason": reason,
                "priority": priority,
                "status": "pending",
                "created_at": time.time(),
                "updated_at": time.time(),
                "rca_result": copy.deepcopy(rca_result),
                "diagnosis_revision": diagnosis_revision(rca_result),
                "judge": judge or {},
                "decision": "",
                "reviewer": "",
                "expert_diagnosis": "",
                "comment": "",
                "learning_result": {},
            }
            self._reviews.append(review)
            self._save()
            return review

    def list_reviews(self, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """List reviews, newest first."""
        with file_lock(self._dir / 'reviews.lock'):
            self._load()
            items = self._reviews
            if status:
                items = [r for r in items if r.get('status') == status]
            return copy.deepcopy(sorted(items, key=lambda r: r.get('created_at', 0), reverse=True)[:limit])

    def decide_review(
        self,
        review_id: str,
        decision: str,
        reviewer: str = "",
        expert_diagnosis: str = "",
        comment: str = "",
        context_learner=None,
        feedback_store=None,
        trace_store=None,
        fault_store=None,
    ) -> Dict[str, Any]:
        """Record a human decision and optionally trigger supervised learning."""
        if any(not isinstance(value, str) for value in (decision, reviewer, expert_diagnosis, comment)):
            raise ValueError('review_fields_must_be_text')
        decision = decision.strip().lower()
        if decision not in {"approve", "reject", "needs_more_evidence"}:
            raise ValueError("decision must be approve, reject, or needs_more_evidence")

        with file_lock(self._dir / 'reviews.lock'):
            self._load()
            for review in self._reviews:
                if review.get('review_id') != review_id:
                    continue
                if review.get('status') == 'closed':
                    if (review.get('decision') != decision or review.get('expert_diagnosis', '') != expert_diagnosis):
                        raise FeedbackConflict('closed_review_cannot_be_rewritten')
                    return copy.deepcopy(review)
                review.update(decision=decision, reviewer=reviewer, expert_diagnosis=expert_diagnosis,
                    comment=comment, updated_at=time.time(),
                    status='pending' if decision == 'needs_more_evidence' else 'closed')
                review['learning_result'] = {}
                eligible = decision in {'approve', 'reject'} and bool(expert_diagnosis.strip()) and context_learner is not None
                if eligible:
                    review['learning_result'] = {'learning_status': 'outcome_unknown'}
                self._save()  # A closed decision is consumed before learning.
                selected = copy.deepcopy(review)
                break
            else:
                raise KeyError('review not found')
        if not eligible:
            return selected
        if feedback_store is None:
            learning = {'learning_status': 'blocked_no_feedback_store'}
        else:
            learning = feedback_store.submit_feedback(
                incident_id=selected.get('incident_id', review_id), expert_diagnosis=expert_diagnosis,
                comment=comment, context_learner=context_learner, trace_store=trace_store,
                fault_store=fault_store, event_id=review_id, event_scope='hitl',
                expected_diagnosis_revision=selected.get('diagnosis_revision') or diagnosis_revision(selected.get('rca_result', {})))
        with file_lock(self._dir / 'reviews.lock'):
            self._load()
            for current in self._reviews:
                if current.get('review_id') == review_id:
                    current['learning_result'] = learning
                    self._save()
                    return copy.deepcopy(current)
            raise FeedbackConflict('review_claim_missing_after_learning')

    def stats(self) -> Dict[str, Any]:
        with file_lock(self._dir / 'reviews.lock'):
            self._load()
        total = len(self._reviews)
        pending = sum(1 for r in self._reviews if r.get("status") == "pending")
        closed = sum(1 for r in self._reviews if r.get("status") == "closed")
        learned = sum(1 for r in self._reviews if
            r.get('learning_result', {}).get('learning_status', r.get('learning_result', {}).get('status')) == 'success')
        return {
            "total": total,
            "pending": pending,
            "closed": closed,
            "learned": learned,
        }
