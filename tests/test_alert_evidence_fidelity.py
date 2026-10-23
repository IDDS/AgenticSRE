"""Regressions for observed import severity and compact-dossier evidence loss."""
import copy

import pytest

from agents.alert_compression import CompressionStore, compress_alerts


def alert(i=0, **changes):
    return dict(dict(name='ApplicationLog', source='logs', timestamp=1700000000+i,
                     cluster_id='qa', namespace='shop', pod='api',
                     severity='warning', message='upstream response duration_ms=20'), **changes)


def save(records, tmp_path):
    result = compress_alerts(records)
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    group = scan['groups'][0]
    return store, scan, group


@pytest.mark.parametrize('top', [None, '', '  '])
def test_label_severity_is_used_when_explicit_severity_is_absent(top):
    row = alert(severity=top, labels={'severity':' CRITICAL '})
    original = copy.deepcopy(row)
    result = compress_alerts([row])
    assert result['raw_alerts'][0]['severity'] == 'critical'
    assert result['groups'][0]['severity'] == 'critical'
    assert result['raw_alerts'][0]['raw'] == original and row == original


def test_explicit_severity_has_precedence_and_existing_unknown_levels_survive():
    for top, expected in [(' INFO ', 'info'), ('vendor-level', 'vendor-level')]:
        result = compress_alerts([alert(severity=top, labels={'severity':'critical'})])
        assert result['groups'][0]['severity'] == expected
    assert compress_alerts([alert(severity=None)])['groups'][0]['severity'] == 'warning'


@pytest.mark.parametrize('field,channel', [
    ('duration_ms', 'message.duration_ms'), ('latency_ms', 'message.latency_ms'),
    ('after', 'message.after_ms'), ('waiting', 'message.waiting_ms'),
])
def test_interior_log_peak_is_in_compact_and_full_dossiers(field, channel, tmp_path):
    def text(value):
        return f'upstream {field} {value} ms' if field in ('after','waiting') else f'upstream {field}={value}'
    rows = [alert(i, message=text(9000 if i == 7 else 20)) for i in range(20)]
    store, scan, group = save(rows, tmp_path)
    assert len(scan['groups']) == 1
    for compact in (True, False):
        dossier = store.dossier(scan['scan_id'], group['group_id'], 1, compact=compact)
        numeric = dossier['log_numeric_summary'][channel]
        assert numeric['unit'] == 'ms' and numeric['max'] == 9000 and numeric['min'] == 20
        assert numeric['observations'] == 20
        peak = next(s for s in dossier['alert_samples'] if s['alert_id'] == numeric['max_alert_id'])
        assert '9000' in peak['message']
        assert peak['log_numeric_observations'][channel]['max'] == 9000
    assert store.get(scan['scan_id']) == scan
    assert [r['raw'] for r in scan['raw_alerts']] == rows


def test_label_latency_is_preserved_with_provenance_and_does_not_override_message(tmp_path):
    rows = [alert(i, labels={'latency_ms':str(9000 if i == 7 else 20)}) for i in range(20)]
    store, scan, group = save(rows, tmp_path)
    dossier = store.dossier(scan['scan_id'], group['group_id'], 1)
    summary = dossier['log_numeric_summary']
    assert summary['labels.latency_ms']['max'] == 9000
    assert summary['message.duration_ms']['max'] == 20
    assert summary['labels.latency_ms']['max_alert_id'] in {s['alert_id'] for s in dossier['alert_samples']}


def test_peak_after_message_truncation_is_still_explicit_and_traceable(tmp_path):
    rows = [alert(i, message='prefix '+('x'*600)+f' duration_ms={9000 if i == 7 else 20}') for i in range(20)]
    store, scan, group = save(rows, tmp_path)
    dossier = store.dossier(scan['scan_id'], group['group_id'], 1)
    numeric = dossier['log_numeric_summary']['message.duration_ms']
    peak = next(s for s in dossier['alert_samples'] if s['alert_id'] == numeric['max_alert_id'])
    assert numeric['max'] == 9000 and peak['message_truncated']
    assert len(peak['message']) == 500
    assert peak['log_numeric_observations']['message.duration_ms']['max'] == 9000


@pytest.mark.parametrize('text', ['duration_ms=NaN', 'latency_ms=inf', 'duration_ms=-10',
    'duration_ms=1e999', 'duration_ms=9000ns', 'duration_ms=9.1.2', 'duration_ms=20%', 'duration_ms=10/20',
    'request_id=9000 trace_id=9999 status=503 version=2'])
def test_invalid_values_units_and_identifiers_do_not_become_millisecond_evidence(text, tmp_path):
    store, scan, group = save([alert(message=text)], tmp_path)
    dossier = store.dossier(scan['scan_id'], group['group_id'], 1)
    assert not dossier.get('log_numeric_summary')


def test_zero_scientific_notation_and_repeated_field_values_remain_explicit(tmp_path):
    rows = [alert(0, message='duration_ms=0 duration_ms=2.5e1'),
            alert(1, message='duration_ms=1.2 duration_ms=9e3')]
    store, scan, group = save(rows, tmp_path)
    dossier = store.dossier(scan['scan_id'], group['group_id'], 1)
    numeric = dossier['log_numeric_summary']['message.duration_ms']
    assert numeric['min'] == 0 and numeric['max'] == 9000 and numeric['observations'] == 4
    assert len(dossier['alert_samples']) == 2


def test_manually_split_groups_do_not_inherit_another_groups_peak(tmp_path):
    rows = [alert(i, message=f'duration_ms={9000 if i == 7 else 20}') for i in range(20)]
    store, scan, group = save(rows, tmp_path)
    peak_id = scan['raw_alerts'][7]['alert_id']
    edited = store.edit(scan['scan_id'], revision=1, action='split', group_ids=[group['group_id']],
                        alert_ids=[peak_id], reason='isolate slow request for review')
    for part in edited['groups']:
        dossier = store.dossier(scan['scan_id'], part['group_id'], 2)
        expected = 9000 if peak_id in part['alert_ids'] else 20
        assert dossier['log_numeric_summary']['message.duration_ms']['max'] == expected
    with pytest.raises(ValueError, match='stale'):
        store.dossier(scan['scan_id'], group['group_id'], 1)
