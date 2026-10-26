"""
AgenticSRE Fault Context Store
Persistent storage for diagnostic rules and fault contexts.
Supports ChromaDB (primary) and JSON fallback.
WeRCA-style continuous fault context learning.
"""

import json
import hashlib
import logging
import time
import os
import tempfile
import fcntl
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from remediation_history import build_recovery_scope
from recovery_verification import valid_verification_fingerprint, VERIFICATION_CONTRACT_VERSION
from memory.feedback_contract import atomic_json, file_lock, read_list

logger = logging.getLogger(__name__)


class FaultContextStore:
    """
    Persistent storage for:
    - Diagnostic rules: "If condition, then conclusion" patterns
    - Fault contexts: Historical fault records with full evidence
    
    Primary: ChromaDB with cosine similarity
    Fallback: JSON files with simple TF-IDF matching
    """

    supports_memory_scope = True
    MAX_SCOPE_CANDIDATES = 4096

    def __init__(self, config=None):
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.mem_cfg = cfg.memory
        self.db_path = Path(self.mem_cfg.db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        
        self._backend = None
        self._init_backend()

    def _init_backend(self):
        """Initialize ChromaDB or fallback to JSON."""
        if self.mem_cfg.backend == "chromadb":
            try:
                import chromadb
                self._client = chromadb.PersistentClient(path=str(self.db_path / "chromadb"))
                self._rules_col = self._client.get_or_create_collection(
                    name=self.mem_cfg.rules_collection,
                    metadata={"hnsw:space": "cosine"},
                )
                self._faults_col = self._client.get_or_create_collection(
                    name=self.mem_cfg.faults_collection,
                    metadata={"hnsw:space": "cosine"},
                )
                self._backend = "chromadb"
                logger.info("FaultContextStore: using ChromaDB backend")
            except Exception as e:
                logger.warning(f"ChromaDB init failed: {e}, falling back to JSON")
                self._backend = "json"
        else:
            self._backend = "json"
        
        if self._backend == "json":
            self._rules_file = self.db_path / "rules.json"
            self._faults_file = self.db_path / "faults.json"
            self._load_json()

    def _load_json(self):
        """Load JSON fallback data."""
        self._rules_data = []
        self._faults_data = []
        if self._rules_file.exists():
            try:
                self._rules_data = json.loads(self._rules_file.read_text())
            except Exception:
                pass
        if self._faults_file.exists():
            try:
                self._faults_data = json.loads(self._faults_file.read_text())
            except Exception:
                pass

    def _save_json(self):
        """Save JSON fallback data."""
        self._rules_file.write_text(json.dumps(self._rules_data, indent=2, ensure_ascii=False))
        self._faults_file.write_text(json.dumps(self._faults_data, indent=2, ensure_ascii=False))

    # ── Rule Operations ──

    @staticmethod
    def rule_revision(rule: Dict) -> str:
        fields = ("name", "condition", "conclusion", "fault_type", "namespace", "confidence", "status",
                  "cluster_id", "scope_kind", "scope_reviewed_by", "scope_reviewed_at", "memory_status",
                  "scope_review_revision",
                  "valid_from", "valid_to", "review_expires_at", "workload_version", "entity_uid",
                  "revision_seq", "rule_type", "lesson", "source", "source_incident_id", "source_feedback_id")
        fields += ('activation_content_revision',)
        content = {k: str(rule.get(k, "")) for k in fields}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def update_rule(self, rule_id: str, patch: Dict, revision: str) -> Dict:
        """Preserve identity/learning metadata; a failed write never deletes a rule."""
        from memory.rule_lifecycle import EDITABLE, MAX_EVENTS, history as rule_history, snapshot, reset_votes
        if set(patch) - EDITABLE - {'status'} or ('status' in patch and patch['status'] != 'inactive'):
            raise ValueError('rule_field_requires_lifecycle_review')
        with (self.db_path / "rules-edit.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._backend == "json":
                rules = read_list(self._rules_file)
                old = next((r for r in rules if r.get("rule_id") == rule_id), None)
            else:
                result = self._rules_col.get(ids=[rule_id], include=["metadatas"])
                old = dict(result["metadatas"][0]) if result.get("ids") else None
            if old is None:
                raise KeyError(rule_id)
            if revision != self.rule_revision(old):
                raise ValueError("规则已变化，请刷新后重新编辑")
            if all(str(old.get(k, '')) == str(v) for k, v in patch.items()):
                return old
            events = rule_history(old)
            if len(events) >= MAX_EVENTS:
                raise ValueError('rule_history_retention_limit')
            history = json.loads(old.get("edit_history", "[]"))
            history = (history + [{"at": time.time(), "before": {k: old.get(k) for k in patch}}])[-20:]
            updated = {**old, **patch, "rule_id": rule_id, "updated_at": time.time(),
                       "edit_history": json.dumps(history, ensure_ascii=False)}
            updated['status'] = 'inactive' if patch.get('status') == 'inactive' else 'candidate'
            updated['memory_status'] = updated['status']
            updated['revision_seq'] = int(old.get('revision_seq', 0)) + 1
            reset_votes(updated, old)
            events.append(dict(operation='edit', at=updated['updated_at'], before_revision=revision,
                revision=self.rule_revision(updated), status=updated['status'], before=snapshot(old), after=snapshot(updated),
                independent_benefit_verified=False))
            updated['lifecycle_history'] = json.dumps(events, ensure_ascii=False, allow_nan=False)
            if self._backend == "json":
                candidate = [updated if r.get("rule_id") == rule_id else r for r in rules]
                name = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.db_path, delete=False) as tmp:
                        name = tmp.name
                        json.dump(candidate, tmp, ensure_ascii=False, allow_nan=False)
                        tmp.flush()
                        os.fsync(tmp.fileno())
                    os.replace(name, self._rules_file)
                finally:
                    if name and os.path.exists(name):
                        os.unlink(name)
                self._rules_data = candidate
            else:
                self._rules_col.update(ids=[rule_id],
                    documents=[f"{updated.get('condition', '')} -> {updated.get('conclusion', '')}"],
                    metadatas=[{k: str(v) for k, v in updated.items() if isinstance(v, (str, int, float, bool))}])
            return updated

    def _read_rule_locked(self, rule_id):
        if self._backend == 'json':
            rows = read_list(self._rules_file)
            rule = next((row for row in rows if row.get('rule_id') == rule_id), None)
        else:
            rows = None
            value = self._rules_col.get(ids=[rule_id], include=['metadatas'])
            rule = dict(value['metadatas'][0] or {}) if value.get('ids') else None
        if rule is None:
            raise KeyError(rule_id)
        return rule, rows

    def decide_rule(self, rule_id, *, revision, decision, reviewer, reason, event_id,
                    identity_source='operator_label'):
        from memory.rule_lifecycle import (decision_request, history, snapshot, MAX_EVENTS, valid_rules,
            effective_status, content_revision, reset_votes)
        from memory.memory_scope import MemoryScope, admission_reason
        request, request_hash = decision_request(revision, decision, reviewer, reason, event_id, identity_source)
        with file_lock(self.db_path / 'rules-edit.lock'):
            old, rows = self._read_rule_locked(rule_id)
            events = history(old)
            prior = next((event for event in events if event.get('event_id') == event_id), None)
            def response(event, current, replayed):
                return dict(rule_id=rule_id, status=event['status'], revision=event['revision'],
                    current_status=effective_status(current), current_revision=self.rule_revision(current),
                    event_id=event_id, replayed=replayed, independent_benefit_verified=False,
                    review_basis='manual_evidence_review')
            if prior:
                if prior.get('request_sha256') != request_hash:
                    raise ValueError('rule_event_content_conflict')
                return response(prior, old, True)
            if revision != self.rule_revision(old):
                raise ValueError('rule_revision_conflict')
            if len(events) >= MAX_EVENTS and decision != 'deactivate':
                raise ValueError('rule_history_retention_limit')
            state = effective_status(old)
            if decision == 'deactivate' and state == 'source_blocked' and old.get('status') == 'active':
                state = 'active'
            allowed = {'approve': {'candidate'}, 'reject': {'candidate'},
                       'deactivate': {'active'}, 'resubmit': {'rejected', 'inactive'}}
            if state not in allowed[decision]:
                raise ValueError('invalid_rule_state_transition')
            target = {'approve': 'active', 'reject': 'rejected', 'deactivate': 'inactive', 'resubmit': 'candidate'}[decision]
            if decision == 'approve':
                from memory.feedback_lifecycle import rule_block_reason
                if rule_block_reason(old):
                    raise ValueError('rule_source_feedback_blocked')
                source_scope = MemoryScope(cluster_id=old.get('cluster_id', ''), namespace=old.get('namespace', ''),
                    workload_version=old.get('workload_version', ''), entity_uid=old.get('entity_uid', ''))
                if not source_scope.complete or old.get('scope_kind') == 'global':
                    raise ValueError('local_rule_scope_required')
                if not valid_rules([{**old, 'confidence': float(old.get('confidence', .5))}]):
                    raise ValueError('invalid_rule_content')
                proposed = {**old, 'status': 'active', 'memory_status': 'active',
                            'activation_content_revision': content_revision(old)}
                if admission_reason(proposed, source_scope, 'rules') != 'admitted':
                    raise ValueError('rule_not_currently_applicable')
            updated = {**old, 'status': target, 'memory_status': target, 'updated_at': time.time(),
                       'revision_seq': int(old.get('revision_seq', 0)) + 1}
            if decision == 'approve':
                updated['activation_content_revision'] = content_revision(updated)
                reset_votes(updated, old)
            event = {**request, 'operation': decision, 'event_id': event_id, 'request_sha256': request_hash,
                'at': updated['updated_at'], 'before_revision': revision, 'revision': self.rule_revision(updated), 'status': target,
                'before': snapshot(old), 'after': snapshot(updated), 'independent_benefit_verified': False}
            events.append(event)
            updated['lifecycle_history'] = json.dumps(events, ensure_ascii=False, allow_nan=False)
            if self._backend == 'json':
                atomic_json(self._rules_file, [updated if r.get('rule_id') == rule_id else r for r in rows])
                self._rules_data = [updated if r.get('rule_id') == rule_id else r for r in rows]
            else:
                self._rules_col.update(ids=[rule_id],
                    documents=[f"{updated.get('condition', '')} -> {updated.get('conclusion', '')}"],
                    metadatas=[{k: str(v) for k, v in updated.items() if isinstance(v, (str, int, float, bool))}])
            return response(event, updated, False)

    def rule_history(self, rule_id, *, offset=0, limit=50):
        from memory.rule_lifecycle import history, effective_status
        with file_lock(self.db_path / 'rules-edit.lock'):
            rule, _ = self._read_rule_locked(rule_id)
            events = history(rule)
            return dict(rule_id=rule_id, revision=self.rule_revision(rule), status=effective_status(rule),
                stored_status=rule.get('status', 'active'),
                events=events[max(0, offset):max(0, offset)+max(1, min(100, limit))], total=len(events))

    def add_rule(self, rule: Dict, *, return_receipt=False):
        """Add a diagnostic rule. Returns rule_id."""
        rule = dict(rule)
        rule_id = f"rule-{hashlib.md5(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:10]}"
        rule["rule_id"] = rule_id
        rule["timestamp"] = time.time()
        rule.setdefault("created_at", rule["timestamp"])
        rule.setdefault("updated_at", rule["timestamp"])
        rule.setdefault("source", "unknown")
        rule.setdefault("status", "active")
        if rule.get('source') in ('auto_learn', 'supervised'):
            rule['status'] = rule['memory_status'] = 'candidate'
        rule.setdefault('revision_seq', 0)
        rule.setdefault("usage_count", 0)
        rule.setdefault("last_used", 0)
        rule.setdefault("quality_score", float(rule.get("confidence") if rule.get("confidence") is not None else .5))
        rule.setdefault("positive_votes", 0)
        rule.setdefault("negative_votes", 0)
        
        with file_lock(self.db_path / 'rules-edit.lock'):
            if self._backend == 'chromadb':
                # Re-extraction is not an edit. Preserve existing votes and any
                # human-edited content under the original stable identifier.
                existing = self._rules_col.get(ids=[rule_id], include=['metadatas'])
                created = not existing.get('ids')
                current = rule if created else dict(existing['metadatas'][0] or {})
                if created:
                    text = f"{rule.get('condition', '')} -> {rule.get('conclusion', '')}"
                    self._rules_col.upsert(ids=[rule_id], documents=[text],
                        metadatas=[{k: str(v) for k, v in rule.items() if isinstance(v, (str, int, float, bool))}])
            else:
                records = read_list(self._rules_file)
                current = next((r for r in records if r.get('rule_id') == rule_id), None)
                created = current is None
                if created:
                    current = rule
                    records.append(rule)
                    atomic_json(self._rules_file, records)
                self._rules_data = records
            from memory.rule_lifecycle import effective_status
            receipt = dict(rule_id=rule_id, created=created, stored_status=current.get('status', 'active'),
                           effective_status=effective_status(current),
                           revision=self.rule_revision(current))

        logger.info(f"Added rule: {rule_id}")
        return receipt if return_receipt else rule_id

    def _scoped_query(self, query, n, memory_scope, kind):
        """Choose eligible identities before semantic top-k, with a bounded scan.

        Chroma metadata predicates narrow the scan, then exact temporal and
        review checks precede the embedding query. Oversized scopes fail closed.
        """
        from memory.memory_scope import filter_memory
        if not memory_scope.complete or n <= 0:
            return []
        if self._backend == 'json':
            path = self._rules_file if kind == 'rules' else self._faults_file
            candidates, _ = filter_memory(read_list(path), memory_scope, kind)
            words = set(query.lower().split())
            scored = []
            for row in candidates:
                text = (f"{row.get('condition', '')} {row.get('conclusion', '')}" if kind == 'rules'
                        else f"{row.get('description', '')} {row.get('root_cause', '')}")
                overlap = len(words & set(text.lower().split()))
                if overlap:
                    scored.append((overlap, row))
            scored.sort(key=lambda item: item[0], reverse=True)
            return [row for _, row in scored[:n]]
        collection = self._rules_col if kind == 'rules' else self._faults_col
        if collection.count() == 0:
            return []
        local = {'$and': [{'cluster_id': memory_scope.cluster_id}, {'namespace': memory_scope.namespace}]}
        where = {'$or': [local, {'scope_kind': 'global'}]} if kind == 'rules' else local
        result = collection.get(where=where, include=['metadatas'], limit=self.MAX_SCOPE_CANDIDATES + 1)
        if len(result['ids']) > self.MAX_SCOPE_CANDIDATES:
            raise ValueError('memory_scope_candidate_limit')
        identity = 'rule_id' if kind == 'rules' else 'fault_id'
        candidates = [{**(meta or {}), identity: key} for key, meta in zip(result['ids'], result['metadatas'])
                      if (meta or {}).get(identity) == key]
        candidates, _ = filter_memory(candidates, memory_scope, kind)
        if not candidates:
            return []  # No eligible memory means no embedding/model download.
        selected = collection.query(query_texts=[query], n_results=min(n, len(candidates)),
            where={'$and': [where, {identity: {'$in': [row[identity] for row in candidates]}}]})
        rows = []
        for i, key in enumerate(selected.get('ids', [[]])[0]):
            meta = selected['metadatas'][0][i] or {}
            if meta.get(identity) != key:
                continue
            row = {**meta, 'text': selected['documents'][0][i]}
            if kind == 'rules':
                row['similarity'] = 1 - selected['distances'][0][i]
            rows.append(row)
        # A concurrent edit may change applicability between get and query.
        return filter_memory(rows, memory_scope, kind)[0]

    def query_similar_rules(self, query: str, n: int = 5, *, _raise_errors=False, memory_scope=None) -> List[Dict]:
        """Find rules similar to the query."""
        from memory.rule_lifecycle import is_active
        if memory_scope is not None:
            try:
                return self._scoped_query(query, n, memory_scope, 'rules')
            except Exception:
                if _raise_errors:
                    raise
                logger.warning('Scoped rule retrieval unavailable')
                return []
        if self._backend == "chromadb":
            try:
                # Empty memory cannot yield a match. Avoid embedding the query
                # (and a cold model download) before any rules exist.
                if self._rules_col.count() == 0:
                    return []
                results = self._rules_col.query(query_texts=[query], n_results=min(n, 10))
                rules = []
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    meta = results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {}
                    if not is_active(meta):
                        continue
                    distance = results.get("distances", [[]])[0][i] if results.get("distances") else 0
                    rules.append({**meta, "text": doc, "similarity": 1 - distance})
                return rules
            except Exception as e:
                if _raise_errors:
                    raise
                logger.error(f"ChromaDB query failed: {e}")
                return []
        else:
            # Simple keyword matching fallback
            query_lower = query.lower()
            scored = []
            for rule in read_list(self._rules_file):
                if not is_active(rule):
                    continue
                text = f"{rule.get('condition', '')} {rule.get('conclusion', '')}".lower()
                # Simple overlap score
                query_words = set(query_lower.split())
                text_words = set(text.split())
                overlap = len(query_words & text_words)
                if overlap > 0:
                    scored.append((rule, overlap / max(len(query_words), 1)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [r for r, _ in scored[:n]]

    def record_rule_usage(self, rule_ids: List[str], positive: Optional[bool] = None,
                          *, expected_revisions=None, feedback_id=None) -> Dict:
        """Record exposure or explicit usefulness on the original content version.

        Uses the editor's lock so validation and update cannot race an edit.
        Positive/negative feedback requires an immutable revision, never an ID
        inferred from a whole-incident model verdict.
        """
        from memory.rule_lifecycle import is_active
        if positive is not None and type(positive) is not bool:
            raise ValueError('rule_feedback_must_be_boolean')
        if not rule_ids:
            return {'updated': 0, 'skipped': 0}
        wanted = list(dict.fromkeys(rule_ids))
        if positive is not None and expected_revisions is None:
            return {'updated': 0, 'skipped': len(wanted), 'reason': 'rule_revision_required'}
        with file_lock(self.db_path / 'rules-edit.lock'):
            if self._backend == 'json':
                records = read_list(self._rules_file)
                selected = [(r['rule_id'], r, None) for r in records if r.get('rule_id') in wanted]
            else:
                result = self._rules_col.get(ids=wanted, include=['metadatas', 'documents'])
                selected = [(key, dict(result['metadatas'][i] or {}), result['documents'][i])
                            for i, key in enumerate(result.get('ids', []))]
            now, updated = time.time(), 0
            for key, rule, document in selected:
                if (expected_revisions is not None and expected_revisions.get(key) != self.rule_revision(rule)):
                    continue
                if not is_active(rule):
                    continue
                if positive is not None and feedback_id:
                    from memory.feedback_lifecycle import ledger_snapshot
                    origin = next((e for e in ledger_snapshot() if e.get('feedback_id') == feedback_id), None)
                    if origin is None or origin.get('feedback_state', 'active') != 'active':
                        continue
                    vote_ids = json.loads(rule.get('feedback_vote_ids', '[]'))
                    if feedback_id in vote_ids:
                        continue
                    vote_ids.append(feedback_id)
                    rule['feedback_vote_ids'] = json.dumps(vote_ids)
                # A correctness vote is not another observation of rule use.
                if positive is None:
                    rule['usage_count'] = int(float(rule.get('usage_count', 0) or 0)) + 1
                    rule['last_used'] = now
                rule['updated_at'] = now
                pos = int(float(rule.get('positive_votes', 0) or 0)) + int(positive is True)
                neg = int(float(rule.get('negative_votes', 0) or 0)) + int(positive is False)
                rule.update(positive_votes=pos, negative_votes=neg)
                if pos + neg:
                    base = float(rule.get('confidence') if rule.get('confidence') is not None else .5)
                    rule['quality_score'] = round(0.6 * base + 0.4 * pos / (pos + neg), 3)
                if self._backend != 'json':
                    self._rules_col.upsert(ids=[key], documents=[document],
                        metadatas=[{k: str(v) for k, v in rule.items()}])
                updated += 1
            if self._backend == 'json':
                if updated:
                    atomic_json(self._rules_file, records)
                self._rules_data = records
            return {'updated': updated, 'skipped': len(wanted)-updated,
                    'reason': 'versioned_rule_feedback' if positive is not None else 'context_exposure'}

    def governance_report(self, stale_after_days: int = 30) -> Dict:
        """Summarize memory health, stale rules, and potential conflicts."""
        rules = self.list_rules(limit=1000)
        faults = self.list_faults(limit=1000)
        now = time.time()
        stale_cutoff = now - stale_after_days * 86400

        stale = [
            r for r in rules
            if float(r.get("last_used", 0) or 0) < stale_cutoff
            and float(r.get("timestamp", now) or now) < stale_cutoff
        ]
        low_quality = [
            r for r in rules
            if float(r.get("quality_score", r.get("confidence", 1)) or 0) < 0.45
        ]

        buckets: Dict[str, List[Dict]] = {}
        for rule in rules:
            key = f"{rule.get('namespace', 'general')}::{rule.get('fault_type', '')}::{rule.get('condition', '')[:60].lower()}"
            buckets.setdefault(key, []).append(rule)
        conflicts = []
        for items in buckets.values():
            conclusions = {str(r.get("conclusion", "")).strip().lower() for r in items if r.get("conclusion")}
            if len(items) > 1 and len(conclusions) > 1:
                conflicts.append({
                    "condition": items[0].get("condition", ""),
                    "rule_ids": [r.get("rule_id") for r in items],
                    "conclusions": list(conclusions)[:5],
                })

        from memory.rule_lifecycle import is_active, effective_status, content_revision
        active = sum(1 for r in rules if is_active(r))
        reviewed = sum(1 for r in rules if is_active(r) and r.get('activation_content_revision') == content_revision(r))
        quality_values = [float(r.get("quality_score", r.get("confidence", 0)) or 0) for r in rules if is_active(r)]
        avg_quality = sum(quality_values) / max(len(quality_values), 1)

        return {
            "backend": self._backend,
            "rules_count": len(rules),
            "faults_count": len(faults),
            "active_rules": active,
            "reviewed_rules": reviewed,
            'candidate_rules': sum(effective_status(r) == 'candidate' for r in rules),
            'rejected_rules': sum(effective_status(r) == 'rejected' for r in rules),
            'inactive_rules': sum(effective_status(r) == 'inactive' for r in rules),
            'source_blocked_rules': sum(effective_status(r) == 'source_blocked' for r in rules),
            'quality_score_population': 'currently_consumable_rules',
            'independent_benefit_verified': False,
            "avg_quality_score": round(avg_quality, 3),
            "stale_rules": len(stale),
            "low_quality_rules": len(low_quality),
            "conflicts": conflicts[:20],
            "health_score": round(max(0.0, min(1.0, 0.45 + 0.25 * avg_quality + 0.2 * (reviewed / max(len(rules), 1)) - 0.05 * len(conflicts))), 3),
        }

    def list_rules(self, limit: int = 500, *, raise_errors=False) -> List[Dict]:
        """Return stored rules across backends."""
        if self._backend == "json":
            self._rules_data = read_list(self._rules_file)
            return list(self._rules_data[-limit:])
        try:
            result = self._rules_col.get(limit=limit)
            rules = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                rules.append({**meta, "rule_id": doc_id, "text": doc})
            return rules
        except Exception as e:
            if raise_errors:
                raise
            logger.warning("list_rules failed: %s", e)
            return []

    def list_faults(self, limit: int = 500) -> List[Dict]:
        """Return stored fault contexts across backends."""
        if self._backend == "json":
            return list(self._faults_data[-limit:])
        try:
            result = self._faults_col.get(limit=limit)
            faults = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                faults.append({**meta, "fault_id": doc_id, "text": doc})
            return faults
        except Exception as e:
            logger.warning("list_faults failed: %s", e)
            return []

    # ── Fault Context Operations ──

    def add_fault(self, fault: Dict) -> str:
        """Add a historical fault context. Returns fault_id."""
        fault = dict(fault)
        resolution = fault.get("resolution")
        if isinstance(resolution, str):
            resolution = {"status": resolution}
        resolution = dict(resolution) if isinstance(resolution, dict) else {}
        commands = resolution.get("commands") or fault.get("commands") or []
        if isinstance(commands, str):
            commands = [commands]
        target = fault.get("target") if isinstance(fault.get("target"), dict) else {
            key: fault.get(key)
            for key in ("deployment", "service", "pod", "node", "network_policy", "network_chaos")
            if fault.get(key)
        }
        if commands and not resolution.get("action_fingerprint"):
            scope = build_recovery_scope(
                commands=commands,
                cluster_id=fault.get("cluster_id") or resolution.get("cluster_id"),
                namespace=fault.get("namespace") or resolution.get("namespace"),
                target=target,
                environment=fault.get("environment") or resolution.get("environment"),
                workload_version=(
                    fault.get("workload_version")
                    or fault.get("application_version")
                    or resolution.get("workload_version")
                ),
            )
            resolution.update({
                "action_fingerprint": scope["action_fingerprint"],
                "environment_fingerprint": scope["environment_fingerprint"],
                "scope_version": scope["scope_version"],
            })
        if resolution:
            fault["resolution"] = resolution
            fault["resolution_status"] = str(resolution.get("status") or resolution.get("outcome") or "")
            fault["resolution_action"] = str(
                resolution.get("trigger") or resolution.get("action") or resolution.get("type") or ""
            )
            fault["action_fingerprint"] = str(resolution.get("action_fingerprint") or "")
            fault["environment_fingerprint"] = str(resolution.get("environment_fingerprint") or "")
            fault["recovery_scope_version"] = str(resolution.get("scope_version") or "")
            for source, destination in [('verification_fingerprint', 'verification_fingerprint'),
                                        ('verification_contract_version', 'verification_contract_version'),
                                        ('verification_level', 'verification_level'),
                                        ('verification_status', 'recovery_verification_status')]:
                if source in resolution:
                    fault[destination] = str(resolution[source] or '')
        fault_id = f"fault-{hashlib.md5(json.dumps(fault, sort_keys=True, default=str).encode()).hexdigest()[:10]}"
        fault["fault_id"] = fault_id
        fault["timestamp"] = time.time()
        
        if self._backend == "chromadb":
            text = f"{fault.get('description', '')} {fault.get('root_cause', '')} {fault.get('fault_type', '')}"
            self._faults_col.upsert(
                ids=[fault_id],
                documents=[text],
                metadatas=[{k: str(v)[:500] for k, v in fault.items() 
                           if isinstance(v, (str, int, float, bool))}],
            )
        else:
            with file_lock(self.db_path / 'faults-write.lock'):
                records = read_list(self._faults_file)
                if not any(f.get('fault_id') == fault_id for f in records):
                    records.append(fault)
                    atomic_json(self._faults_file, records)
                self._faults_data = records
        
        logger.info(f"Added fault context: {fault_id}")
        return fault_id

    def delete_fault(self, fault_id: str) -> None:
        """Delete only the selected fault without rewriting cached rules."""
        if self._backend == 'chromadb':
            # A backend failure must reach the API instead of reporting success.
            self._faults_col.delete(ids=[fault_id])
            return
        with file_lock(self.db_path / 'faults-write.lock'):
            records = read_list(self._faults_file)
            retained = [record for record in records if record.get('fault_id') != fault_id]
            atomic_json(self._faults_file, retained)
            self._faults_data = retained

    def query_similar_faults(self, query: str, n: int = 5, *, _raise_errors=False, memory_scope=None) -> List[Dict]:
        """Find fault contexts similar to the query."""
        if memory_scope is not None:
            try:
                return self._scoped_query(query, n, memory_scope, 'faults')
            except Exception:
                if _raise_errors:
                    raise
                logger.warning('Scoped fault retrieval unavailable')
                return []
        if self._backend == "chromadb":
            try:
                if self._faults_col.count() == 0:
                    return []
                results = self._faults_col.query(query_texts=[query], n_results=min(n, 10))
                faults = []
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    meta = results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {}
                    faults.append({**meta, "text": doc})
                return faults
            except Exception as e:
                if _raise_errors:
                    raise
                logger.error(f"ChromaDB fault query failed: {e}")
                return []
        else:
            query_lower = query.lower()
            scored = []
            for fault in self._faults_data:
                text = f"{fault.get('description', '')} {fault.get('root_cause', '')}".lower()
                query_words = set(query_lower.split())
                text_words = set(text.split())
                overlap = len(query_words & text_words)
                if overlap > 0:
                    scored.append((fault, overlap / max(len(query_words), 1)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [r for r, _ in scored[:n]]

    def resolution_stats(
        self,
        *,
        fault_type: str = "",
        error_pattern: str = "",
        action_fingerprint: str = "",
        environment_fingerprint: str = "",
        verification_fingerprint: str = "",
        require_verification_contract: bool = False,
        min_samples: int = 14,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """
        Aggregate historical recovery outcomes for a fault type or error pattern.

        This statistic is reserved for recovery policy and automation gates.
        It must not be used as a proxy for historical diagnosis correctness.
        """
        fault_key = str(fault_type or "").strip().lower()
        pattern_key = str(error_pattern or "").strip().lower()
        action_key = str(action_fingerprint or "").strip()
        environment_key = str(environment_fingerprint or "").strip()
        verification_key = str(verification_fingerprint or '').strip()
        verification_required = bool(require_verification_contract or verification_key)
        verification_complete = valid_verification_fingerprint(verification_key)
        scope_required = bool(action_key or environment_key or verification_required)
        faults = self.list_faults(limit=limit)
        base_matched: List[Dict[str, Any]] = []
        matched: List[Dict[str, Any]] = []
        verification_missing = verification_mismatch = verification_unverified = verification_conflict = 0
        for fault in faults:
            ft = str(fault.get("fault_type") or fault.get("error_category") or "").strip().lower()
            pat = str(fault.get("error_pattern") or fault.get("pattern") or "").strip().lower()
            base_match = False
            if pattern_key and pat == pattern_key:
                base_match = True
            elif fault_key and ft == fault_key:
                base_match = True
            if not base_match:
                continue
            base_matched.append(fault)
            resolution = fault.get("resolution") if isinstance(fault.get("resolution"), dict) else {}
            record_action = str(
                fault.get("action_fingerprint") or resolution.get("action_fingerprint") or ""
            ).strip()
            record_environment = str(
                fault.get("environment_fingerprint") or resolution.get("environment_fingerprint") or ""
            ).strip()
            if action_key and record_action != action_key:
                continue
            if environment_key and record_environment != environment_key:
                continue
            if verification_required:
                fields = [('verification_fingerprint', 'verification_fingerprint'),
                          ('verification_contract_version', 'verification_contract_version'),
                          ('verification_level', 'verification_level'),
                          ('verification_status', 'recovery_verification_status')]
                if any(nested in resolution and flat in fault and resolution[nested] != fault[flat]
                       for nested, flat in fields):
                    verification_conflict += 1
                    continue
                record_verification = fault.get('verification_fingerprint') or resolution.get('verification_fingerprint')
                if not record_verification:
                    verification_missing += 1
                    continue
                if not verification_complete or record_verification != verification_key:
                    verification_mismatch += 1
                    continue
                version = fault.get('verification_contract_version') or resolution.get('verification_contract_version')
                level = fault.get('verification_level') or resolution.get('verification_level')
                verified = fault.get('recovery_verification_status') or resolution.get('verification_status')
                if (version != VERIFICATION_CONTRACT_VERSION or level not in {'infrastructure', 'http_headers', 'json_assertions'}
                        or verified not in {'verified', 'failed'}):
                    verification_unverified += 1
                    continue
            matched.append(fault)

        success = 0
        failed = 0
        auto = 0
        button = 0
        manual = 0
        code_fix = 0
        unclassified = 0
        for fault in matched:
            resolution = fault.get("resolution")
            if isinstance(resolution, str):
                resolution = {"status": resolution}
            resolution = resolution if isinstance(resolution, dict) else {}
            status = str(
                resolution.get("status")
                or resolution.get("outcome")
                or fault.get("resolution_status")
                or fault.get("status")
                or ""
            ).lower()
            action = str(
                resolution.get("trigger")
                or resolution.get("action")
                or resolution.get("type")
                or fault.get("resolution_action")
                or ""
            ).lower()
            code_deployed = bool(
                resolution.get("code_deployed")
                or resolution.get("code_fix")
                or fault.get("code_deployed")
                or fault.get("code_fix")
            )

            if status in {"success", "succeeded", "resolved", "recovered", "ok", "true"}:
                success += 1
            elif status in {"failed", "failure", "unresolved", "error", "false"}:
                failed += 1
            else:
                unclassified += 1
            if action == "auto":
                auto += 1
            elif action in {"button", "approval", "rerun_with_approval"}:
                button += 1
            elif action == "manual":
                manual += 1
            if code_deployed or action == "fix_code":
                code_fix += 1

        sample_count = success + failed
        success_rate = success / sample_count if sample_count else None
        legacy_unscoped_count = 0
        for fault in base_matched:
            resolution = fault.get("resolution") if isinstance(fault.get("resolution"), dict) else {}
            status = str(
                resolution.get("status")
                or resolution.get("outcome")
                or fault.get("resolution_status")
                or ""
            ).lower()
            if status not in {
                "success", "succeeded", "resolved", "recovered", "ok", "true",
                "failed", "failure", "unresolved", "error", "false",
            }:
                continue
            record_action = fault.get("action_fingerprint") or resolution.get("action_fingerprint")
            record_environment = fault.get("environment_fingerprint") or resolution.get("environment_fingerprint")
            if not record_action or not record_environment:
                legacy_unscoped_count += 1
        return {
            "history_kind": "recovery_outcome",
            "fault_type": fault_type,
            "error_pattern": error_pattern,
            "scope_required": scope_required,
            "scope_complete": not scope_required or bool(action_key and environment_key and
                (not verification_required or verification_complete)),
            "scope_version": 'recovery_scope_v2' if verification_required else ('recovery_scope_v1' if scope_required else 'legacy_fault_scope'),
            "action_fingerprint": action_key or None,
            "environment_fingerprint": environment_key or None,
            'verification_required': verification_required,
            'verification_scope_complete': not verification_required or verification_complete,
            'verification_fingerprint': verification_key or None,
            'verification_missing_count': verification_missing,
            'verification_mismatch_count': verification_mismatch,
            'verification_unverified_count': verification_unverified,
            'verification_conflict_count': verification_conflict,
            "sample_count": sample_count,
            "success_count": success,
            "failure_count": failed,
            "success_rate": round(success_rate, 3) if success_rate is not None else None,
            "matched_record_count": len(matched),
            "unclassified_outcome_count": unclassified,
            "legacy_unscoped_count": legacy_unscoped_count,
            "auto_count": auto,
            "button_count": button,
            "manual_count": manual,
            "code_fix_count": code_fix,
            "minimum_samples": max(1, int(min_samples or 14)),
            "sufficient_samples": sample_count >= max(1, int(min_samples or 14)),
            "source": "fault_context_store",
        }

    def _history_query_rules(self, query, n=5, memory_scope=None):
        return self.query_similar_rules(query, n=n, _raise_errors=True, memory_scope=memory_scope)

    def _history_query_faults(self, query, n=5, memory_scope=None):
        return self.query_similar_faults(query, n=n, _raise_errors=True, memory_scope=memory_scope)

    def get_historical_context(self, incident_query: str, *, timeout_s=None, memory_scope=None) -> Dict:
        """Retrieve a bounded snapshot, with explicit availability metadata."""
        from memory.history_retrieval import retrieve_history
        return retrieve_history(lambda: self, incident_query, memory_scope=memory_scope,
            n_rules=self.mem_cfg.max_similar_results,
            n_faults=self.mem_cfg.max_similar_results,
            timeout_s=getattr(self.mem_cfg, 'retrieval_timeout_s', 5.0) if timeout_s is None else timeout_s).context

    def stats(self) -> Dict:
        """Get store statistics."""
        if self._backend == "chromadb":
            governance = self.governance_report()
            return {
                "backend": "chromadb",
                "rules_count": self._rules_col.count(),
                "faults_count": self._faults_col.count(),
                "health_score": governance.get("health_score", 0),
                "avg_quality_score": governance.get("avg_quality_score", 0),
                "stale_rules": governance.get("stale_rules", 0),
                "low_quality_rules": governance.get("low_quality_rules", 0),
                "conflicts": len(governance.get("conflicts", [])),
            }
        else:
            governance = self.governance_report()
            return {
                "backend": "json",
                "rules_count": len(self._rules_data),
                "faults_count": len(self._faults_data),
                "health_score": governance.get("health_score", 0),
                "avg_quality_score": governance.get("avg_quality_score", 0),
                "stale_rules": governance.get("stale_rules", 0),
                "low_quality_rules": governance.get("low_quality_rules", 0),
                "conflicts": len(governance.get("conflicts", [])),
            }
