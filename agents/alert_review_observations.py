"""Observe exact batch repetition without reusing answers or changing review policy."""
from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid


OBSERVATION_WINDOW_S = 300  # Measurement horizon, not a cache TTL.
SCHEMA_VERSION = 3


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def policy_fingerprint():
    root = Path(__file__).resolve().parent.parent
    names = ('agents/alert_review.py', 'agents/alert_review_observations.py',
             'agents/alert_review_context.py', 'agents/alert_lifecycle.py',
             'agents/alert_compression.py', 'agents/alert_context.py',
             'agents/alert_numeric.py', 'agents/alert_model_evidence.py',
             'agents/alert_semantics.py', 'tools/llm_client.py', 'tools/llm_receipt.py')
    return fingerprint({name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names})


def create_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS review_observations (
        id TEXT PRIMARY KEY, job TEXT NOT NULL, offset INTEGER NOT NULL, pairs INTEGER NOT NULL,
        started REAL NOT NULL, finished REAL, outcome TEXT NOT NULL, match_key TEXT,
        identity TEXT NOT NULL, prior_outcome TEXT, prior_age_s REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS review_observations_match ON review_observations(match_key,started)')
    db.execute('CREATE INDEX IF NOT EXISTS review_observations_job ON review_observations(job)')


def summary(db, job):
    rows = db.execute('SELECT pairs,outcome,prior_outcome,identity FROM review_observations WHERE job=?', (job,)).fetchall()
    outcomes, blockers = Counter(), Counter()
    repeated = Counter()
    context_verified = provider_verified = 0
    for row in rows:
        outcomes[row['outcome']] += 1
        identity = json.loads(row['identity'])
        blockers.update(identity['reuse_blockers'])
        context_verified += (row['outcome'] in ('relations','empty')
                             and identity.get('completion_context',{}).get('status') == 'verified')
        provider_verified += (row['outcome'] in ('relations','empty')
                              and identity.get('provider',{}).get('completion_status') == 'complete')
        if row['prior_outcome'] and row['outcome'] != 'invalidated':
            repeated[row['prior_outcome']] += 1
            repeated['pairs'] += row['pairs']
    return dict(mode='observe_only', window_seconds=OBSERVATION_WINDOW_S,
                observed_attempts=len(rows), outcomes=dict(outcomes),
                prior_nonempty_requests=repeated['relations'], prior_empty_requests=repeated['empty'],
                repeated_pairs=repeated['pairs'], reused_pairs=0, reuse_allowed=False,
                context_verified_reviews=context_verified,
                provider_verified_reviews=provider_verified,
                blocker_counts=dict(blockers))


class ReviewObserver:
    def __init__(self, store, job, result, model, *, guard=None, source=None):
        self.store, self.job, self.result, self.model = store, job, result, model
        self.groups = {g['group_id']: g for g in result['groups']}
        self.evidence = {}
        self.guard, self.source, self.binding, self.contexts = guard, source, None, {}

    def group_fingerprint(self, key):
        if key not in self.evidence:
            group = self.groups[key]
            # Include every member's full raw data, even if the prompt has only samples.
            self.evidence[key] = fingerprint(dict(group=group,
                members=[self.result['raw_alerts'][i] for i in group['alert_indices']]))
        return self.evidence[key]

    def begin(self, offset, batch, request, output_tokens):
        model = getattr(self.model, 'model', None)
        provider = getattr(getattr(self.model, 'client', None), 'base_url', None)
        temperature = getattr(self.model, 'temperature', None)
        temperature_valid = type(temperature) in (int, float) and math.isfinite(temperature)
        model_valid = isinstance(model, str) and bool(model)
        provider = str(provider) if provider is not None else ''
        keys = list(dict.fromkeys(p[side] for p in batch for side in ('left', 'right')))
        scope_valid = all(all(self.groups[key].get(field) for field in ('cluster_id','entity_kind','entity')) for key in keys)
        comparable = model_valid and bool(provider) and temperature_valid and scope_valid
        # Operator assertion only; aliases are not inferred to be immutable revisions.
        revision = os.environ.get('AGENTICSRE_ALERT_REVIEW_MODEL_REVISION', '')
        blockers = ['reuse_disabled', 'episode_unverified', 'access_scope_unversioned',
                    'provider_completion_metadata_unavailable', 'provider_usage_unavailable',
                    'evidence_freshness_unverified']
        if not revision:
            blockers.append('model_revision_missing')
        if not comparable:
            blockers.append('identity_incomplete')
        context = dict(status='blocked',reason='missing_trusted_guard')
        if self.guard is not None:
            if self.binding is None:
                self.binding = self.guard.binding(self.result)
            context = self.guard.assess(self.result,batch,self.source,self.binding)
            if context['status'] == 'verified':
                blockers.remove('episode_unverified')
                blockers.remove('evidence_freshness_unverified')
            else:
                blockers.append('context_'+context['reason'])
        identity = dict(schema_version=SCHEMA_VERSION, policy_sha256=policy_fingerprint(),
            adapter_sha256=fingerprint([type(self.model).__module__, type(self.model).__qualname__]),
            model_sha256=fingerprint(model) if model_valid else None,
            provider_sha256=fingerprint(provider) if provider else None,
            declared_revision_sha256=fingerprint(revision) if revision else None,
            temperature=temperature if temperature_valid else None, output_tokens=output_tokens, thinking=False,
            prompt_sha256=fingerprint(request[0]), messages_sha256=fingerprint(request),
            evidence_sha256=fingerprint([self.group_fingerprint(key) for key in keys]),
            origin_sha256=fingerprint(self.result.get('origin', 'unspecified')),
            episode_sha256=context.get('episode_sha256'),reuse_blockers=blockers)
        # Snapshot versions and check times audit one attempt, not cross-scan identity.
        match_key = fingerprint(identity) if comparable and (self.guard is None or context['status']=='verified') else None
        identity['review_context'] = context
        now, receipt = time.time(), uuid.uuid4().hex
        with self.store.connect(timeout=.1) as db:
            prior = db.execute('''SELECT outcome,started FROM review_observations
                WHERE match_key=? AND job<>? AND outcome IN ('relations','empty')
                AND started>=? AND started<=? AND finished<=?
                ORDER BY started DESC LIMIT 1''',
                (match_key, self.job, now-OBSERVATION_WINDOW_S, now, now)).fetchone() if match_key else None
            db.execute('INSERT INTO review_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (receipt,self.job,offset,len(batch),now,None,'incomplete',match_key,json.dumps(identity),
                 prior['outcome'] if prior else None, now-prior['started'] if prior else None))
        if self.guard is not None:
            self.contexts[receipt] = (batch,identity)
        return receipt

    def finish(self, receipt, answer, rejected):
        outcome = ('failed' if answer['semantic']['status'] != 'ok' else 'rejected' if rejected
                   else 'relations' if answer['relations'] else 'empty')
        identity = None
        invalidated = False
        if receipt in self.contexts:
            batch, identity = self.contexts.pop(receipt)
            current = self.guard.assess(self.result,batch,self.source,self.binding)
            identity['completion_context'] = current
            before = identity['review_context']
            invalidated = (before['status']=='verified' and (current['status']!='verified'
                           or before['state_sha256'] != current.get('state_sha256')))
            if invalidated:
                identity['reuse_blockers'].append('review_context_changed')
                if outcome in ('relations','empty'):
                    outcome = 'invalidated'
        with self.store.connect(timeout=.1) as db:
            row = db.execute("SELECT identity FROM review_observations WHERE id=? AND job=? AND outcome='incomplete'",
                             (receipt,self.job)).fetchone()
            if row is None:
                return
            identity = identity if identity is not None else json.loads(row['identity'])
            provider = answer['semantic'].get('provider')
            if provider and provider.get('source') == 'provider_response':
                identity['provider'] = provider
                identity['reuse_blockers'].remove('provider_completion_metadata_unavailable')
                if provider.get('completion_status') != 'complete':
                    identity['reuse_blockers'].append('provider_completion_incomplete')
                usage = provider.get('usage',{})
                if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens','completion_tokens')):
                    identity['reuse_blockers'].remove('provider_usage_unavailable')
            db.execute("UPDATE review_observations SET finished=?,outcome=? WHERE id=? AND job=? AND outcome='incomplete'",
                       (time.time(),outcome,receipt,self.job))
            db.execute('UPDATE review_observations SET identity=?,match_key=CASE WHEN ? THEN NULL ELSE match_key END WHERE id=? AND job=?',
                       (json.dumps(identity),invalidated,receipt,self.job))
