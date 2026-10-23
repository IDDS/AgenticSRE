"""Revision-bound original alert reads and bounded, deterministic RCA loading."""
import copy
import hashlib
import json


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def group_snapshot(data, group_id, revision):
    if type(revision) is not int or revision != data['revision']:
        raise ValueError('stale revision; reload the event groups')
    group = next((g for g in data['groups'] if g['group_id'] == group_id), None)
    if group is None:
        raise ValueError('unknown group')
    indexes = group['alert_indices']
    rows = data['raw_alerts']
    if (not indexes or any(type(i) is not int or i < 0 or i >= len(rows) for i in indexes)
            or len(set(indexes)) != len(indexes)):
        raise ValueError('invalid stored group membership')
    members = [rows[i] for i in indexes]
    if ([r['alert_id'] for r in members] != group['alert_ids']
            or len(members) != group['alert_count']
            or len({r['alert_id'] for r in members}) != len(members)
            or any((r['cluster_id'], r['namespace']) != (group['cluster_id'], group['namespace']) for r in members)):
        raise ValueError('inconsistent stored group membership or scope')
    ref = dict(scan_id=data['scan_id'], group_id=group_id, revision=revision)
    digest = hashlib.sha256(encoded(dict(ref=ref, records=members)).encode()).hexdigest()
    return group, members, dict(ref, snapshot_sha256=digest)


def evidence_page(data, group_id, revision, *, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('offset must be nonnegative; limit must be between 1 and 100')
    group, members, ref = group_snapshot(data, group_id, revision)
    if offset > len(members):
        raise ValueError('offset exceeds group membership')
    end = min(offset + limit, len(members))
    return dict(ref=ref, cluster_id=group['cluster_id'], namespace=group['namespace'],
                total=len(members), offset=offset, next_offset=end if end < len(members) else None,
                records=copy.deepcopy(members[offset:end]))


def hydrate_dossier(dossier, data, *, max_chars=12000, max_records=32):
    """Read one immutable store snapshot; keep omissions separate from fidelity.

    Diversity buckets use the existing symbolic parser, never model-generated
    labels. Extrema and severity are prioritized; no claim of optimal selection.
    The cap is serialized characters, not provider tokens or storage read bytes.
    """
    if type(max_chars) is not int or not 1000 <= max_chars <= 32000:
        raise ValueError('invalid evidence character budget')
    if type(max_records) is not int or not 1 <= max_records <= 100:
        raise ValueError('invalid evidence record budget')
    from agents.alert_semantics import symbolic
    group, members, ref = group_snapshot(data, dossier['group_id'], dossier['revision'])
    if any(dossier[k] != group[k] for k in ('cluster_id', 'namespace')) or dossier['scan_id'] != data['scan_id']:
        raise ValueError('dossier scope differs from stored snapshot')
    ranks = {'critical': 4, 'high': 3, 'warning': 2, 'info': 1}
    extrema = set()
    for stats in [dossier.get('metric_summary', {}), *dossier.get('log_numeric_summary', {}).values()]:
        extrema.update(stats[k] for k in ('min_alert_id', 'max_alert_id') if k in stats)
    buckets = {}
    for row in members:
        key = tuple(row[k] for k in ('source', 'name', 'entity_kind', 'entity', 'state', 'severity')) + (symbolic(row)['key'],)
        buckets.setdefault(key, []).append(row)
    # One representative per distinct state/template/object precedes repeats.
    representatives = [values[0] for values in sorted(buckets.values(),
        key=lambda values: (-ranks.get(values[0]['severity'], 0), len(values)))]
    protected = [row for row in members if row['alert_id'] in extrema]
    by_id = {row['alert_id']: row for row in members}
    prior = [by_id[s['alert_id']] for s in dossier['alert_samples'] if s['alert_id'] in by_id]
    # A budget is a ceiling, not a target: repeated notifications must not fill
    # spare context after their template/state, endpoints and extrema are kept.
    ordered = protected + representatives + prior
    selected, seen = [], set()
    for row in ordered:
        aid = row['alert_id']
        if aid in seen:
            continue
        seen.add(aid)
        sample = {k: row[k] for k in ('alert_id', 'name', 'source', 'severity', 'timestamp',
                                      'cluster_id', 'namespace', 'entity_kind', 'entity', 'state')}
        from agents.alert_identity import target_identity
        sample.update(target_identity(row))
        sample['message'] = row['message'][:4000]
        sample['message_truncated'] = len(row['message']) > 4000
        sample['message_chars'] = len(row['message'])
        sample['message_sha256'] = hashlib.sha256(row['message'].encode()).hexdigest()
        if len(selected) < max_records and len(encoded(selected + [sample])) <= max_chars:
            selected.append(sample)
    result = copy.deepcopy(dossier)
    result['alert_samples'] = selected
    result['raw_alert_ids'] = [row['alert_id'] for row in members]
    result['raw_alert_ids_total'] = len(members)
    result['raw_evidence'] = dict(ref, total_alerts=len(members), loaded_alerts=len(selected),
        entity_count=(len({(r['cluster_id'], r['namespace'], r['entity_kind'], r['entity']) for r in members})
                      if all(r['entity_kind'] and r['entity'] for r in members) else None),
        omitted_alerts=len(members)-len(selected), excerpted_messages=sum(s['message_truncated'] for s in selected),
        selected_chars=len(encoded(selected)), max_chars=max_chars, max_records=max_records,
        selection='extrema_then_severity_and_rare_symbolic_state_then_prior_samples',
        evidence_url=f"/api/alerts/compression/{ref['scan_id']}/groups/{ref['group_id']}/evidence?revision={ref['revision']}",
        interpretation='loaded observations only; omitted originals remain revision-bound and retrievable')
    return result
