"""Read-only incident projection and conservative freshness checks for review observations."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from contextlib import closing

from agents.alert_lifecycle import incident_id


MAX_CONTEXT_AGE_S = 300  # Observation policy; does not enable a cache.


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def stamp(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value > 0 else None


class ReviewContextGuard:
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def binding(result):
        fields = ('raw_alerts','groups','cluster_id','scan_scope','origin','collection','degraded',
                  'collection_started_at','time_window','context_evidence','metric_evidence')
        return digest({k:result.get(k) for k in fields})

    @staticmethod
    def eligible(result, source):
        return (source == 'live_scan' and result.get('origin') == 'live_scan'
                and result.get('collection') == {'prometheus':'ok','kubectl':'ok'}
                and result.get('cluster_id') and not result.get('degraded') and not result.get('history'))

    def projection(self, result):
        """One SQLite read transaction; never creates a database or advances watermarks."""
        rows, cluster, scope = result['raw_alerts'], result['cluster_id'], result.get('scan_scope','')
        at = stamp(result.get('collection_started_at'))
        if at is None or not rows:
            return dict(status='blocked',reason='missing_observation_time_or_members')
        if any(r['cluster_id'] != cluster or scope and r['namespace'] != scope
               or r['source'] not in ('prometheus','k8s_event') for r in rows):
            return dict(status='blocked',reason='observation_scope_mismatch')
        ids = {incident_id(row) for row in rows}
        states, versions, watermarks, members = {}, {}, {}, []
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
            try:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                ordered = sorted(ids)
                for start in range(0,len(ordered),400):
                    chunk = ordered[start:start+400]
                    for key, body in db.execute('SELECT id,body FROM incidents WHERE id IN ('+','.join('?'*len(chunk))+')',chunk):
                        states[key] = json.loads(body)
                for ns,source in sorted({(r['namespace'],r['source']) for r in rows}):
                    mark = db.execute('SELECT at FROM watermarks WHERE cluster=? AND namespace=? AND source=?',
                                      (cluster,ns,source)).fetchone()
                    if mark is None or stamp(mark[0]) is None or at <= mark[0]:
                        return dict(status='blocked',reason='unordered_observation')
                    watermarks[(ns,source)] = mark[0]
                for row in rows:
                    key = incident_id(row)
                    state = states.get(key)
                    if state is None:
                        return dict(status='blocked',reason='new_episode_unknown')
                    if (state.get('cluster_id') != cluster or state.get('namespace') != row['namespace']
                            or state.get('source') != row['source'] or state.get('incident_id') != key):
                        return dict(status='blocked',reason='incident_scope_mismatch')
                    if state.get('status') in ('resolved','expired'):
                        return dict(status='blocked',reason='episode_reopening')
                    expected = 'active' if row['source'] == 'prometheus' else 'observed'
                    if state.get('status') != expected:
                        return dict(status='blocked',reason='episode_unstable')
                    if (type(state.get('episode')) is not int or state['episode'] < 1
                            or stamp(state.get('episode_started_at')) is None
                            or stamp(state.get('last_observed_at')) is None
                            or not state['episode_started_at'] <= state['last_observed_at'] <= watermarks[(row['namespace'],row['source'])]):
                        return dict(status='blocked',reason='invalid_incident_version')
                    if key not in versions:
                        versions[key] = digest(state)
                    members.append(dict(alert_id=row['alert_id'],incident_id=key,episode=state['episode'],
                                        episode_started_at=state['episode_started_at'],status=state['status']))
                return dict(status='verified',episode_sha256=digest(members),
                    state_sha256=digest([versions,sorted((ns,src,at) for (ns,src),at in watermarks.items())]),
                    members=members)
            finally:
                db.rollback()

    def capture(self, result, source):
        if not self.eligible(result,source):
            return dict(status='blocked',reason='not_complete_live_observation')
        try:
            return dict(self.projection(result),schema_version=1,captured_at=time.time(),
                        binding_sha256=self.binding(result))
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
            return dict(status='blocked',reason='lifecycle_unavailable')

    def assess(self, result, batch, source, binding=None):
        if not self.eligible(result,source):
            return dict(status='blocked',reason='not_complete_live_observation')
        saved = result.get('review_context',{})
        if saved.get('status') != 'verified' or saved.get('schema_version') != 1:
            return dict(status='blocked',reason=saved.get('reason','missing_pre_review_projection'))
        try:
            if stamp(saved.get('captured_at')) is None or time.time() < saved['captured_at']:
                return dict(status='blocked',reason='future_or_missing_projection_time')
            if saved.get('binding_sha256') != (binding if binding is not None else self.binding(result)):
                return dict(status='blocked',reason='prepared_evidence_changed')
            current = self.projection(result)
            if current['status'] != 'verified':
                return {k:v for k,v in current.items() if k != 'members'}
            if current['state_sha256'] != saved.get('state_sha256'):
                return dict(status='blocked',reason='lifecycle_version_changed')
            freshness = self.freshness(result,batch)
            return dict(freshness,episode_sha256=current['episode_sha256'],state_sha256=current['state_sha256'])
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError):
            return dict(status='blocked',reason='review_context_unavailable')

    @staticmethod
    def freshness(result, batch):
        now, started = time.time(), stamp(result.get('collection_started_at'))
        window = stamp(result.get('time_window'))
        if started is None or window is None:
            return dict(status='blocked',reason='missing_evidence_time')
        window = min(window,MAX_CONTEXT_AGE_S)
        times = [started]
        groups = {g['group_id']:g for g in result['groups']}
        keys = set(p[side] for p in batch for side in ('left','right'))
        for key in keys:
            for i in groups[key]['alert_indices']:
                row = result['raw_alerts'][i]
                if row['source'] == 'prometheus':
                    # Collector wall time is not proof of a fresh Prometheus sample.
                    raw = row['raw'].get('raw')
                    value = raw.get('value') if isinstance(raw,dict) else None
                    times.append(value[0] if isinstance(value,list) and len(value)==2 else None)
                else:
                    times.append(row.get('timestamp'))
        def metric_times(entry):
            if entry.get('status') == 'composite':
                for measurement in entry.get('measurements',[]): metric_times(measurement)
            elif entry.get('status') == 'ready':
                last, end = entry.get('summary',{}).get('last'), entry.get('end')
                times.extend([end,last[0] if isinstance(last,list) and len(last)==2 else None])
                if isinstance(last,list) and last and stamp(last[0]) and stamp(end) and last[0] > end:
                    times.append(None)
        traces = result.get('context_evidence',{}).get('traces',{})
        edges = traces.get('edges',[])+traces.get('internal_span_edges',[])
        for pair in batch:
            for metric in pair.get('metric_context',{}).get('groups',{}).values(): metric_times(metric)
            context = pair.get('observed_context',{})
            refs = context.get('trace_refs',[])
            paths = context.get('propagation_candidates',[])
            if (refs or paths) and (traces.get('truncated') or traces.get('status') != 'mapped'):
                return dict(status='blocked',reason='trace_time_unverified')
            for ref in refs:
                times.append(ref.get('at') if str(ref.get('provenance','')).startswith('live_jaeger') else None)
            for path in paths:
                if not path.get('span_links'):
                    times.append(None)
                for link in path.get('span_links',[]):
                    matches = [e for e in edges if e.get('trace_id') == path.get('trace_id')
                        and e.get('span_id') == link.get('span_id') and e.get('parent_span_id') == link.get('parent_span_id')]
                    times.append(matches[0].get('at') if len(matches)==1 and str(matches[0].get('provenance','')).startswith('live_jaeger') else None)
            changes = context.get('change_refs',[])
            if changes and result.get('context_evidence',{}).get('rollouts',{}).get('truncated'):
                return dict(status='blocked',reason='change_time_unverified')
            for ref in changes:
                times.append(ref.get('at') if str(ref.get('provenance','')).startswith('live_kubectl') else None)
        if any(stamp(t) is None for t in times):
            return dict(status='blocked',reason='missing_evidence_time')
        until = min(times)+window
        if max(times) > now:
            return dict(status='blocked',reason='future_evidence_time')
        if now >= until:
            return dict(status='blocked',reason='evidence_expired',valid_until=until)
        return dict(status='verified',valid_until=until,checked_at=now,window_seconds=window)
