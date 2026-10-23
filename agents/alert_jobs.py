"""Durable, single-worker alert jobs with per-batch checkpoints."""
import asyncio
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from agents.alert_compression import CompressionStore, propose_relations
from agents.alert_semantics import build_scag
from agents.alert_review import limits, new_budget, reserve, budget_view, resume_summary, record_provider
from agents import alert_review_observations as review_observations


class JobReviewBudget:
    def __init__(self, store, key, value):
        self.store, self.key = store, key
        with store.connect() as db:
            state = new_budget(value)
            legacy = db.execute('SELECT answer,calls FROM batches WHERE job=?', (key,)).fetchall()
            if legacy:
                # Old checkpoints lack pre-call reservations. Never reset unknown spend to zero.
                state.update(model_calls=sum(r['calls'] for r in legacy),
                             input_bytes=state['limits']['max_input_bytes'], legacy_usage_unknown=True)
            db.execute('INSERT OR IGNORE INTO review_budgets VALUES (?,?)',
                       (key, json.dumps(state)))

    def reserve(self, input_bytes, output_tokens):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = json.loads(db.execute('SELECT state FROM review_budgets WHERE job=?', (self.key,)).fetchone()[0])
            answer = reserve(state, input_bytes, output_tokens)
            db.execute('UPDATE review_budgets SET state=? WHERE job=?', (json.dumps(state), self.key))
        return answer

    def snapshot(self):
        with self.store.connect() as db:
            return budget_view(json.loads(db.execute('SELECT state FROM review_budgets WHERE job=?', (self.key,)).fetchone()[0]))

    def record_provider(self, attempt, metadata):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = json.loads(db.execute('SELECT state FROM review_budgets WHERE job=?', (self.key,)).fetchone()[0])
            record_provider(state, attempt, metadata)
            db.execute('UPDATE review_budgets SET state=? WHERE job=?', (json.dumps(state), self.key))


class AlertJobs:
    def __init__(self, directory, *, output=None):
        self.directory = Path(directory)
        self.output = output
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, created REAL, updated REAL, total INTEGER, error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS batches (job TEXT, offset INTEGER, count INTEGER, status TEXT, answer TEXT, calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job, offset))')
            db.execute('CREATE TABLE IF NOT EXISTS review_budgets (job TEXT PRIMARY KEY, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS published (job TEXT PRIMARY KEY, scan_id TEXT NOT NULL, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS review_outcomes (job TEXT PRIMARY KEY, state TEXT NOT NULL)')
            review_observations.create_schema(db)

    @contextmanager
    def connect(self, *, timeout=30):
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def validate(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'job-[0-9a-f]{32}', key):
            raise ValueError('invalid job id')
        return key

    def create(self, payload, *, key=None):
        limits(payload.get('review_budget'))
        key = self.validate(key) if key is not None else 'job-' + uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload FROM jobs WHERE id=?', (key,)).fetchone()
            if existing:
                if json.loads(existing['payload']) != payload:
                    raise ValueError('job id already belongs to a different request')
            else:
                db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (key, json.dumps(payload), 'queued', now, now, None, None))
        return self.get(key)

    def get(self, key):
        self.validate(key)
        with self.connect() as db:
            row = db.execute('SELECT id,status,created,updated,total,error FROM jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise FileNotFoundError(key)
            out = dict(row)
            batches = db.execute('SELECT offset,count,status,calls FROM batches WHERE job=? ORDER BY offset', (key,)).fetchall()
            budget = db.execute('SELECT state FROM review_budgets WHERE job=?', (key,)).fetchone()
            published = db.execute('SELECT scan_id,revision FROM published WHERE job=?', (key,)).fetchone()
            outcome = db.execute('SELECT state FROM review_outcomes WHERE job=?', (key,)).fetchone()
            try:
                observation = review_observations.summary(db, key)
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                observation = dict(mode='observe_only', status='unavailable', observed_attempts=0,
                                   reuse_allowed=False, reused_pairs=0)
        if observation['observed_attempts'] or observation.get('status') == 'unavailable':
            out['review_observation'] = observation
        out['model_calls'] = sum(b['calls'] for b in batches)
        out['completed_pairs'] = sum(b['count'] for b in batches if b['status'] == 'ok')
        out['failed_pairs'] = sum(b['count'] for b in batches if b['status'] != 'ok')
        out['completed_batches'] = sum(b['status'] == 'ok' for b in batches)
        out['failed_batches'] = sum(b['status'] != 'ok' for b in batches)
        out['job_id'] = out.pop('id')
        out['candidate_pairs'] = out.pop('total')
        if budget:
            out['budget'] = budget_view(json.loads(budget[0]))
            out['model_calls'] = out['budget']['model_calls']
            out['can_resume'] = not out['budget']['exhausted_reason']
            if out['status'] in ('partial', 'failed', 'cancelled'):
                state = json.loads(outcome[0]) if outcome else None
                if state is None and published and not out['budget']['exhausted_reason']:
                    state = self.legacy_review_outcome(key, out['budget'])
                if state is None:
                    # A cancelled in-flight job has not finished assessing its
                    # pending requests. Do not rebuild them on the cancel path.
                    count = max(0, (out['candidate_pairs'] or 0)-out['completed_pairs'])
                    state = {'batches': [{'status':'deferred','pairs':count}] if count else []}
                out.update(resume_summary(state, out['budget']))
                if (out['status'] in ('failed', 'cancelled') and out['candidate_pairs'] is not None
                        and out['completed_pairs'] == out['candidate_pairs']):
                    # Completed model work may still need a retry of graph /
                    # lifecycle / file publication; this consumes no new call.
                    out.update(can_resume=True, retryable_pairs=0, resume_blocked_reason=None,
                               resume_action='publish_result')
        out['unreviewed_pairs'] = max(0, (out['candidate_pairs'] or 0)-out['completed_pairs']-out['failed_pairs'])
        if published or out['status'] == 'completed':
            out['scan_id'] = published['scan_id'] if published else 'scan-' + key[4:]
            if published:
                out['result_revision'] = published['revision']
            out['result_url'] = '/api/alerts/compression/' + out['scan_id'] + '/summary'
        if published and self.output is not None:
            # The atomic scan file is the authority. No second "edited" flag
            # can be lost in a crash between a manual edit and a SQL update.
            out['review_revision'] = published['revision']
            try:
                current = self.output.get(published['scan_id'])
                revision = current['revision']
                if (current.get('scan_id') != published['scan_id']
                        or type(revision) is not int or revision < 1):
                    raise ValueError('invalid published result reference')
                edited = bool(current.get('history') or current.get('semantic', {}).get('status') == 'invalidated_by_edit')
                out['result_state'] = 'edited' if edited else 'unchanged' if revision == published['revision'] else 'revision_changed'
                out['result_revision'] = revision
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                out['result_state'] = 'unavailable'
            out['review_applies_to_result'] = out['result_state'] == 'unchanged'
            if not out['review_applies_to_result']:
                reason = {'edited':'result_edited', 'revision_changed':'result_revision_changed',
                          'unavailable':'result_unavailable'}[out['result_state']]
                out.update(can_resume=False, retryable_pairs=0, resume_blocked_reason=reason)
                out.pop('resume_action', None)
        return out

    @contextmanager
    def publication_lock(self, key):
        self.validate(key)
        if self.output is None:
            yield
            return
        self.output.directory.mkdir(parents=True, exist_ok=True)
        path = self.output._path('scan-' + key[4:])
        with open(str(path)+'.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,payload FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', updated=?, error=NULL WHERE id=?", (time.time(), row['id']))
            return row['id'], json.loads(row['payload'])

    def state(self, key, status, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE id=? AND status='running'", (status, error, time.time(), key))

    def control(self, key, action):
        if action == 'resume':
            # Same file-lock -> SQL-lock order as publication. An edit cannot
            # commit between capability assessment and queueing this retry.
            with self.publication_lock(key):
                return self._control(key, action)
        return self._control(key, action)

    def _control(self, key, action):
        info = self.get(key)
        if action == 'resume' and info.get('can_resume') is False:
            reason = info.get('resume_blocked_reason') or info.get('budget', {}).get('exhausted_reason')
            detail = ('review cannot resume' if str(reason).startswith('result_')
                      else 'review budget exhausted or pending evidence cannot fit')
            raise ValueError(f'{detail} ({reason}); retained evidence remains available')
        with self.connect() as db:
            if action == 'resume':
                changed = db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status IN ('partial','failed','cancelled')", (time.time(), key)).rowcount
            elif action == 'cancel':
                changed = db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('queued','running')", (time.time(), key)).rowcount
            else:
                raise ValueError('unknown action')
            if not changed:
                raise ValueError('job state does not permit this action')
        return self.get(key)

    def prepared(self, key):
        path = self.directory / (self.validate(key) + '.json')
        return json.loads(path.read_text()) if path.exists() else None

    def prepare(self, key, result):
        CompressionStore(self.directory)._write(self.directory / (self.validate(key) + '.json'), result)
        self.set_total(key, len(result.get('candidate_pairs', [])))

    def set_total(self, key, total):
        with self.connect() as db:
            db.execute('UPDATE jobs SET total=?,updated=? WHERE id=?', (total, time.time(), key))

    def checkpoints(self, key):
        with self.connect() as db:
            rows = db.execute('SELECT offset,count,answer,calls FROM batches WHERE job=? ORDER BY offset', (key,)).fetchall()
            return {row['offset']: dict(json.loads(row['answer']), count=row['count'], attempted_calls=row['calls']) for row in rows}

    def checkpoint(self, key, offset, count, result):
        answer = {'semantic': result['semantic'], 'relations': result['relations']}
        with self.connect() as db:
            db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?) ON CONFLICT(job,offset) DO UPDATE SET count=excluded.count,status=excluded.status,answer=excluded.answer,calls=batches.calls+excluded.calls',
                       (key, offset, count, 'ok' if result['semantic']['status'] == 'ok' else 'failed', json.dumps(answer), result['semantic']['model_calls']))
            db.execute('UPDATE jobs SET updated=? WHERE id=?', (time.time(), key))

    def save_review_outcome(self, key, semantic, *, if_absent=False):
        # No raw messages or model answers are needed for the progress API.
        state = {'batches': [{k: b[k] for k in ('status','pairs','input_bytes') if k in b}
                             for b in semantic.get('batches', [])]}
        with self.connect() as db:
            if if_absent:
                # A concurrent worker may have just saved a newer assessment.
                db.execute('INSERT OR IGNORE INTO review_outcomes VALUES (?,?)', (key, json.dumps(state)))
                state = json.loads(db.execute('SELECT state FROM review_outcomes WHERE job=?', (key,)).fetchone()[0])
            else:
                db.execute('INSERT OR REPLACE INTO review_outcomes VALUES (?,?)', (key, json.dumps(state)))
        return state

    def legacy_review_outcome(self, key, budget):
        """Assess old terminal jobs once; do not rebuild prompts on every poll."""
        from agents.alert_review import batches, messages, message_bytes
        prepared = self.prepared(key)
        if prepared is None:
            return {'batches': [{'status':'deferred','pairs':1}]}
        checkpoints = self.checkpoints(key)
        cap = min(budget['limits']['max_batch_input_bytes'], budget['limits']['max_input_bytes']-budget['input_bytes'])
        pending = []
        for offset, batch in batches(prepared, cap, checkpoints):
            if not batch:  # Already successful checkpoint.
                continue
            pending.append(dict(status='failed' if offset in checkpoints else 'deferred',
                                pairs=len(batch), input_bytes=message_bytes(messages(batch))))
        return self.save_review_outcome(key, {'batches':pending}, if_absent=True)

    async def process(self, key, payload, prepare, model, output, finalize=None, *, review_guard=None):
        if self.output is not None and self.output.directory.resolve() != output.directory.resolve():
            raise ValueError('review job output store mismatch')
        self.output = output
        async def running():
            info = await asyncio.to_thread(self.get, key)
            if info.get('result_state') in ('edited', 'revision_changed', 'unavailable'):
                await asyncio.to_thread(self.state, key, 'cancelled', info['resume_blocked_reason'])
                return False
            return info['status'] == 'running'
        try:
            existing = await asyncio.to_thread(output.get, 'scan-'+key[4:])
        except FileNotFoundError:
            existing = None
        if existing and existing.get('history'):
            raise ValueError('result was edited; refusing to overwrite its evidence')
        result = await asyncio.to_thread(self.prepared, key)
        if result is None:
            result = await prepare(payload)
            if review_guard is not None:
                result['review_context'] = await asyncio.to_thread(review_guard.capture, result, payload.get('source'))
            await asyncio.to_thread(self.prepare, key, result)
        pairs = result.get('candidate_pairs', [])
        # Recover a crash between the atomic prepared-file write and SQL update.
        await asyncio.to_thread(self.set_total, key, len(pairs))
        if payload.get('semantic') is True and pairs:
            llm, timeout = model(payload)
            budget = JobReviewBudget(self, key, payload.get('review_budget'))
            checkpoints = await asyncio.to_thread(self.checkpoints, key)
            async def save_batch(offset, count, part):
                await asyncio.to_thread(self.checkpoint, key, offset, count, part)
            observer = review_observations.ReviewObserver(self, key, result, llm,
                guard=review_guard, source=payload.get('source'))
            await propose_relations(result, llm, timeout_s=timeout, budget=budget,
                                    checkpoints=checkpoints, checkpoint=save_batch, should_continue=running,
                                    observer=observer)
            await asyncio.to_thread(self.save_review_outcome, key, result['semantic'])
            info = await asyncio.to_thread(self.get, key)
            if 'review_observation' in info:
                result['semantic']['review_observation'] = info['review_observation']
        elif payload.get('semantic') is True:
            result['semantic'] = {'status': 'no_candidates', 'model_calls': 0}
        if not await running():
            return
        await asyncio.to_thread(build_scag, result, result.get('job_topology'))
        result.pop('job_topology', None)
        info = await asyncio.to_thread(self.get, key)
        scan_id = 'scan-'+key[4:]
        try:
            previous = await asyncio.to_thread(output.get, scan_id)
        except FileNotFoundError:
            previous = None
        if previous and previous.get('history'):
            raise ValueError('result was edited; refusing to overwrite its evidence')
        revision = previous['revision']+1 if previous else 1
        result.update(scan_id=scan_id, revision=revision, created_at=info['created'], history=[], semantic_job_id=key)
        if previous and 'lifecycle' in previous:
            result['lifecycle'] = previous['lifecycle']
            by_id = {g['group_id']: g for g in previous['groups']}
            for group in result['groups']:
                for field in ('lifecycle', 'lifecycle_total'):
                    if field in by_id.get(group['group_id'], {}):
                        group[field] = by_id[group['group_id']][field]
        if finalize is not None and previous is None:
            await finalize(payload, result)
        if not await running():
            return
        await asyncio.to_thread(self.publish, key, output, result, previous['revision'] if previous else None)
        status = result.get('semantic', {}).get('status')
        await asyncio.to_thread(self.state, key, 'partial' if status in ('partial', 'failed', 'unavailable') else 'completed')

    def publish(self, key, output, result, expected_revision):
        from agents.alert_integrity import partition_receipt
        result['integrity']=partition_receipt(result)
        output.directory.mkdir(parents=True, exist_ok=True)
        path = output._path(result['scan_id'])
        with open(str(path)+'.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = output.get(result['scan_id']) if path.exists() else None
            if (current and current.get('history')) or (current['revision'] if current else None) != expected_revision:
                raise ValueError('result revision changed; refusing to overwrite evidence')
            output._write(path, result)
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO published VALUES (?,?,?)', (key, result['scan_id'], result['revision']))

    async def run(self, prepare, model, output, finalize=None, *, review_guard=None):
        # A filesystem lock spans the worker lifetime, including multiple app
        # processes sharing this data directory. OS releases it after a crash.
        lock = (self.directory / 'worker.lock').open('a')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            while True:
                job = await asyncio.to_thread(self.claim)
                if job is None:
                    await asyncio.sleep(.5)
                    continue
                key, payload = job
                try:
                    await self.process(key, payload, prepare, model, output, finalize, review_guard=review_guard)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await asyncio.to_thread(self.state, key, 'failed', type(exc).__name__)
        finally:
            lock.close()


def scan_summary(result):
    fields = ('scan_id', 'revision', 'created_at', 'cluster_id', 'total_alerts', 'compressed_groups',
              'compression_ratio', 'omitted_alerts', 'retained_alerts', 'semantic_job_id', 'semantic', 'retrieval','integrity')
    summary = {key: result[key] for key in fields if key in result}
    # Progress detail is available in jobs; the summary must stay small.
    if 'semantic' in summary:
        summary['semantic'] = {key: value for key, value in summary['semantic'].items() if key != 'batches'}
    summary['counts'] = {key: len(result.get(key, [])) for key in ('groups', 'raw_alerts', 'candidate_pairs', 'relations')}
    summary['counts']['scag_edges'] = len(result.get('scag', {}).get('edges', []))
    return summary


def scan_page(result, section, offset, limit):
    if section not in ('groups', 'raw_alerts', 'candidate_pairs', 'relations', 'scag_edges', 'review_pairs'):
        raise ValueError('invalid section')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('invalid pagination')
    if section == 'review_pairs':
        from agents.alert_review import review_page
        values = result.get('candidate_pairs', [])
        items = review_page(result, offset, limit)
    else:
        values = result.get('scag', {}).get('edges', []) if section == 'scag_edges' else result.get(section, [])
        items = values[offset:offset+limit]
    return {'scan_id': result['scan_id'], 'revision': result['revision'], 'section': section,
            'offset': offset, 'limit': limit, 'total': len(values), 'items': items,
            'next_offset': offset+limit if offset+limit < len(values) else None}
