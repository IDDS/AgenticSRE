"""Conservative advisory audit of explicit CPU claims in replay results."""
from __future__ import annotations

import re
from typing import Any

from .claims import assess_cpu_claim

_PATTERNS = {
    'increase': re.compile(r'cpu(?:(?!导致|造成|引起|使得|caus|leading to)[^。；;，,\n]){0,32}?(?:升高|上升|增加|增长|increas(?:e|ed|ing))', re.I),
    'saturation': re.compile(r'cpu[^。；;，,\n]{0,24}?(?:饱和|saturat(?:ed|ion))', re.I),
    'high': re.compile(r'\b(?:elevated|high)\s+cpu\b|\bcpu\b[^。；;，,\n]{0,24}?\b(?:elevated|high)\b', re.I),
}
_NEGATION = re.compile(r'(?:无|没有|未|不|缺少|不能|not|no|without)[^。;；\n]{0,14}$', re.I)


def audit_primary_cpu_claims(result: dict[str, Any], observations: dict[str, Any]) -> dict[str, Any]:
    """Audit limited explicit claims without treating the audit as action authorization.

    Only the first affected service is considered and a unique telemetry identity
    must resolve. The regex covers common positive Chinese/English phrasing, not
    arbitrary natural language; unsupported phrasing may remain undetected.
    """
    text = result.get('root_cause') if isinstance(result, dict) else None
    affected = result.get('affected_services') if isinstance(result, dict) else None
    pods = observations.get('pods') if isinstance(observations, dict) else None
    output: dict[str, Any] = {'status': 'not_checked', 'findings': [], 'automatic_actions_allowed': False,
                              'limitations': ['partial phrase coverage', 'primary affected object only',
                                              'cannot establish causal relationships or authorize actions']}
    if not isinstance(text, str) or not isinstance(affected, list) or not affected or not isinstance(pods, list):
        return output
    claims = []
    for name, pattern in _PATTERNS.items():
        for match in pattern.finditer(text):
            if not _NEGATION.search(text[max(0, match.start() - 20):match.start()]):
                claims.append(name)
                break
    if not claims:
        return output
    target = affected[0]
    if not isinstance(target, str) or not target:
        output.update(status='requires_review', findings=[{'claim': name, 'status': 'insufficient', 'reason': 'invalid_target'} for name in claims])
        return output
    matches = [row for row in pods if isinstance(row, dict) and isinstance(row.get('pod'), str)
               and (row['pod'] == target or re.fullmatch(re.escape(target) + r'-[a-z0-9]{8,10}-[a-z0-9]{5}', row['pod']))]
    if len(matches) != 1:
        output.update(status='requires_review', findings=[{'claim': name, 'status': 'insufficient', 'reason': 'target_identity_ambiguous_or_missing'} for name in claims])
        return output
    metric = matches[0].get('metrics') or {}
    before = (metric.get('before') or {}).get('CpuUsageRate(%)')
    after = (metric.get('after') or {}).get('CpuUsageRate(%)')
    output['findings'] = [
        {'claim': name, 'status': 'insufficient', 'reason': 'high_cpu_threshold_not_defined'}
        if name == 'high' else {'claim': name, **assess_cpu_claim(claim=name, before=before, after=after)}
        for name in claims
    ]
    output['status'] = 'requires_review' if any(x['status'] != 'supported' for x in output['findings']) else 'checked'
    return output
