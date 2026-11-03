"""Check modality-specific clipping and fail-closed typed CPU assessment."""
from .claims import assess_cpu_claim
from .windows import build_windows
from .audit import audit_primary_cpu_claims


def main():
    windows = build_windows(event_time=1000, available_since={'metric': 800, 'log': 975, 'trace': None})
    assert windows['metric']['before'] == [880, 1000]
    assert windows['log']['before'] == [975, 1000]
    assert windows['trace']['before'] is None
    assert windows['log']['baseline_complete'] is False
    assert assess_cpu_claim(claim='increase', before=None, after={'n': 5, 'mean': 44.7})['status'] == 'insufficient'
    assert assess_cpu_claim(claim='saturation', before=None, after={'n': 5, 'mean': 44.7})['reason'] == 'verified_saturation_threshold_missing'
    assert assess_cpu_claim(claim='increase', before={'n': 3, 'mean': 65}, after={'n': 3, 'mean': 45})['status'] == 'contradicted'
    assert assess_cpu_claim(claim='increase', before={'n': 2, 'mean': 20}, after={'n': 2, 'mean': 80})['status'] == 'insufficient'
    assert assess_cpu_claim(claim='saturation', before=None, after={'n': 3, 'mean': 90}, saturation_threshold=80)['status'] == 'supported'
    obs = {'pods': [{'pod': 'svc-1234567890-abcde', 'metrics': {'before': {}, 'after': {'CpuUsageRate(%)': {'n': 2, 'mean': 44.7}}}}]}
    audit = audit_primary_cpu_claims({'root_cause': 'svc CPU 饱和导致延迟增加', 'affected_services': ['svc']}, obs)
    assert audit['status'] == 'requires_review' and not audit['automatic_actions_allowed']
    assert len(audit['findings']) == 1 and audit['findings'][0]['status'] == 'insufficient'
    negative = audit_primary_cpu_claims({'root_cause': '没有证据支持 CPU 饱和', 'affected_services': ['svc']}, obs)
    assert negative['status'] == 'not_checked'
    mixed = audit_primary_cpu_claims({'root_cause': 'svc CPU 饱和，导致其他服务请求延迟上升', 'affected_services': ['svc']}, obs)
    assert [item['claim'] for item in mixed['findings']] == ['saturation']
    increase = audit_primary_cpu_claims({'root_cause': 'svc 的 CPU 使用率和 P95 延迟显著升高', 'affected_services': ['svc']}, obs)
    assert [item['claim'] for item in increase['findings']] == ['increase']
    elevated = audit_primary_cpu_claims({'root_cause': 'elevated CPU in svc', 'affected_services': ['svc']}, obs)
    assert elevated['status'] == 'requires_review' and elevated['findings'][0]['reason'] == 'high_cpu_threshold_not_defined'
    negative_high = audit_primary_cpu_claims({'root_cause': 'no elevated CPU in svc', 'affected_services': ['svc']}, obs)
    assert negative_high['status'] == 'not_checked' and not negative_high['automatic_actions_allowed']
    print('incident replay windows and typed claim guard: PASS')


if __name__ == '__main__':
    main()
