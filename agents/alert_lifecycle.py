"""Durable observation lifecycle; disappearance is not proof of business recovery."""
from __future__ import annotations

import copy
import json
import math
import sqlite3
from pathlib import Path

from agents.alert_compression import digest


def incident_id(row):
    labels = dict(row['labels'])
    for key in ('severity', 'alertstate', '__name__'):
        labels.pop(key, None)
    if row['source'] == 'prometheus' and row['entity_kind'] == 'job':
        # Scraper identity may rotate without creating a new failed Job.
        for key in ('pod', 'instance', 'service', 'endpoint', 'container', 'job'):
            labels.pop(key, None)
    raw = row['raw'].get('raw') or {}
    event_uid = raw.get('metadata', {}).get('uid') if isinstance(raw, dict) else None
    identity = [row['cluster_id'], row['namespace'], row['source'], row['name'],
                row['entity_kind'], row['entity'], labels]
    from agents.alert_identity import target_identity
    target=target_identity(row)
    if target['entity_uid']:
        identity.append(target['entity_uid'])
    if row['source'] == 'k8s_event':
        identity = [row['cluster_id'], row['namespace'], 'k8s_event', event_uid] if event_uid else identity + [row['message']]
    return 'incident-' + digest(identity)


class AlertLifecycle:
    def __init__(self, path, clear_after_s=30):
        self.path = Path(path)
        self.clear_after_s = clear_after_s

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute('CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, cluster TEXT, namespace TEXT, source TEXT, body TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS watermarks (cluster TEXT, namespace TEXT, source TEXT, at REAL, PRIMARY KEY(cluster,namespace,source))')
        conn.execute('CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, body TEXT)')
        return conn

    @staticmethod
    def view(state):
        keys = ('incident_id', 'name', 'namespace', 'source', 'entity_kind', 'entity', 'status', 'episode',
                'first_observed_at', 'episode_started_at', 'last_observed_at', 'resolved_at', 'observations',
                'source_last_at', 'resolution_basis', 'business_recovery_verified', 'last_scan_id')
        return {key: state.get(key) for key in keys}

    def observe(self, scan):
        scope, cluster = scan.get('scan_scope', ''), scan.get('cluster_id')
        at = scan.get('collection_started_at')
        valid = (scan.get('origin') == 'live_scan' and cluster and not scan.get('degraded')
                 and scan.get('collection') == {'prometheus': 'ok', 'kubectl': 'ok'}
                 and scan.get('revision') == 1 and not scan.get('history')
                 and isinstance(at, (int, float)) and math.isfinite(at) and at > 0)
        if not valid:
            scan['lifecycle'] = {'status': 'ineligible', 'reason': 'complete, ordered live snapshots required'}
            return scan
        rows = scan['raw_alerts']
        if any(r['cluster_id'] != cluster or scope and r['namespace'] != scope for r in rows):
            raise ValueError('lifecycle observation scope mismatch')
        # Import, edit, failure and historical bootstrap never create recovery events.
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT body FROM observations WHERE id=?', (scan['scan_id'],)).fetchone()
            if previous:
                projection = json.loads(previous[0])
            else:
                records = [json.loads(r[0]) for r in conn.execute(
                    'SELECT body FROM incidents WHERE cluster=?' + (' AND namespace=?' if scope else ''),
                    (cluster, scope) if scope else (cluster,))]
                states = {s['incident_id']: s for s in records}
                namespaces = {scope} if scope else {r['namespace'] for r in rows} | {s['namespace'] for s in records}
                accepted = set()
                for namespace in namespaces:
                    for source in ('prometheus', 'k8s_event'):
                        old = conn.execute('SELECT at FROM watermarks WHERE cluster=? AND namespace=? AND source=?',
                                           (cluster, namespace, source)).fetchone()
                        if old and at <= old[0]:
                            continue
                        conn.execute('INSERT OR REPLACE INTO watermarks VALUES (?,?,?,?)', (cluster, namespace, source, at))
                        accepted.add((namespace, source))
                seen, transitions = set(), []
                row_ids = {}
                for row in rows:
                    if (row['namespace'], row['source']) not in accepted:
                        continue
                    key = incident_id(row)
                    row_ids[row['alert_id']] = key
                    if key in seen:
                        continue
                    seen.add(key)
                    state = states.get(key)
                    action = None
                    if state is None:
                        state = dict(incident_id=key, cluster_id=cluster, namespace=row['namespace'], source=row['source'],
                            entity_kind=row['entity_kind'], entity=row['entity'], name=row['name'], episode=1,
                            first_observed_at=at, episode_started_at=at, observations=0, history=[])
                        states[key] = state
                        action = 'opened' if row['source'] == 'prometheus' else 'event_observed'
                    elif state['status'] in ('resolved', 'expired'):
                        state['episode'] += 1
                        state['episode_started_at'] = at
                        action = 'reopened' if row['source'] == 'prometheus' else 'event_reobserved'
                    state.update(status='active' if row['source'] == 'prometheus' else 'observed',
                        last_observed_at=at, source_last_at=row['timestamp'] if row['source'] == 'k8s_event' else None,
                        observations=state['observations']+1, missing_since=None, missing_snapshots=0,
                        resolved_at=None, resolution_basis=None, business_recovery_verified=False,
                        last_scan_id=scan['scan_id'])
                    if action:
                        transition = dict(incident_id=key, action=action, at=at, episode=state['episode'], scan_id=scan['scan_id'])
                        state['history'].append(transition)
                        transitions.append(transition)
                for key, state in states.items():
                    if (state['namespace'], state['source']) not in accepted:
                        continue
                    if key not in seen and state['status'] not in ('resolved', 'expired'):
                        state['missing_since'] = state.get('missing_since') or at
                        state['missing_snapshots'] = state.get('missing_snapshots', 0)+1
                        state['status'] = 'pending_resolution' if state['source'] == 'prometheus' else 'event_missing'
                        if state['missing_snapshots'] >= 2 and at-state['missing_since'] >= self.clear_after_s:
                            state['status'] = 'resolved' if state['source'] == 'prometheus' else 'expired'
                            state['resolved_at'] = at if state['source'] == 'prometheus' else None
                            state['resolution_basis'] = 'absent_from_complete_firing_snapshots' if state['source'] == 'prometheus' else 'event_absent_not_recovery'
                            transition = dict(incident_id=key, action=state['status'], at=at, episode=state['episode'], scan_id=scan['scan_id'])
                            state['history'].append(transition)
                            transitions.append(transition)
                        state['last_scan_id'] = scan['scan_id']
                    conn.execute('INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?)',
                                 (key, cluster, state['namespace'], state['source'], json.dumps(state)))
                projection = {'summary': {'status': 'ok' if accepted or not namespaces else 'stale',
                    'transitions': transitions, 'clear_after_s': self.clear_after_s,
                    'timestamp_meaning': 'observed_by_scan_not_true_trigger_time'},
                    'groups': {g['group_id']: [self.view(states[key]) for key in dict.fromkeys(
                        row_ids[a] for a in g['alert_ids'] if a in row_ids)] for g in scan['groups']}}
                conn.execute('INSERT INTO observations VALUES (?,?)', (scan['scan_id'], json.dumps(projection)))
            scan['lifecycle'] = copy.deepcopy(projection['summary'])
            for group in scan['groups']:
                entries = projection['groups'].get(group['group_id'], [])
                group['lifecycle'] = entries[:10]
                group['lifecycle_total'] = len(entries)
        return scan

    def list(self, cluster, namespace='', limit=200):
        with self._connect() as conn:
            rows = conn.execute('SELECT body FROM incidents WHERE cluster=?' + (' AND namespace=?' if namespace else '') +
                                ' ORDER BY rowid DESC LIMIT ?', (cluster, namespace, limit) if namespace else (cluster, limit)).fetchall()
        return [dict(self.view(s), history=s['history'][-20:]) for s in (json.loads(r[0]) for r in rows)]
