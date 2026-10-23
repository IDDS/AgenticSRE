"""Capture actual agent prompt boundaries, then compare visible fact delivery.

The real-provider task is a reader of the hypothesis incident segment, not a
full RCA run. Labels are frozen original group members, never model answers.
"""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from agents.alert_compression import CompressionStore
from agents.alert_root_candidates import diagnosis_handoff
from agents.hypothesis_agent import HypothesisAgent
from agents.planning_agent import PlanningAgent
from eval.alert_diagnosis_replay import PHASES, PROMPT, encode, save, sha, symptom


class Capture:
    def __init__(self):
        self.calls = []

    def json_chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {'hypotheses': [{'id': 'H1', 'description': '待验证', 'confidence': .3}], 'plan': []}


def prepare(source, output, arm):
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    for phase in PHASES:
        for sample in range(2):
            raw = (source/f'{phase}-{sample}-scan.json').read_bytes()
            scan = json.loads(raw)
            with tempfile.TemporaryDirectory() as directory:
                store = CompressionStore(directory)
                save(Path(directory)/(scan['scan_id']+'.json'), scan)
                for group in scan['groups']:
                    with patch('agents.alert_semantics.time.time', return_value=scan['created_at']):
                        dossier = store.dossier(scan['scan_id'], group['group_id'], scan['revision'])
                        query = (f"请调查 {dossier['namespace']} 中 {dossier['entity_kind']}/{dossier['entity']} "
                                 f"的告警事件：{dossier['alert_samples'][0]['name']}。根据证据包及实时只读观测验证根因。")
                        if arm == 'raw':  # Existing reader API's raw lane = pre-fix delivery here.
                            incident = (diagnosis_handoff(dossier)+query+
                                '\n\n当前事件结构化观测上下文（仅含运行期证据，不含注入真值）：\n'+
                                json.dumps(dossier, ensure_ascii=False, default=str)[:8000])
                        else:
                            from agents.alert_dossier_prompt import initial_incident
                            incident = initial_incident(query, dossier, as_of=scan['created_at'])
                    model = Capture()
                    HypothesisAgent(model).generate(incident)
                    PlanningAgent(model, SimpleNamespace(list_tools=lambda: [])).generate_plan([], incident)
                    assert len(model.calls) == 2, 'fixture unexpectedly used a hard-coded fast path'
                    assert incident[:3000] in model.calls[0][1]['content']
                    assert incident[:2000] in model.calls[1][1]['content']
                    if arm == 'dossier':
                        assert len(incident) <= 2000
                        visible = json.loads(incident)
                        delivered_alerts = {a['alert_id'] for a in visible['observations']}
                        original_rows = [scan['raw_alerts'][i] for i in group['alert_indices']]
                        # The fixture has one live row per type; all source IDs
                        # must reach both agents, not only the old samples.
                        assert delivered_alerts == {a['alert_id'] for a in original_rows}
                        measured = scan['metric_evidence']['groups'][group['group_id']]
                        measured = measured['measurements'] if measured.get('status') == 'composite' else [measured]
                        delivered = {m['evidence_id']: m['summary'] for m in visible['measurements']}
                        assert set(delivered) == {m['evidence_id'] for m in measured}
                        for measurement in measured:
                            points = measurement['points']
                            summary = delivered[measurement['evidence_id']]
                            assert summary['samples'] == len(points)
                            assert summary['minimum'] == min(p[1] for p in points)
                            assert summary['maximum'] == max(p[1] for p in points)
                            assert summary['first'] == points[0] and summary['last'] == points[-1]
                    rows = [scan['raw_alerts'][i] for i in group['alert_indices']]
                    expected = {group['entity']: {'symptoms': sorted({symptom(a['name']) for a in rows}),
                                                   'severity': group['severity']}}
                    labels = {'expected': expected, 'support': {a['alert_id']: {
                        'entity': a['entity'], 'kind': symptom(a['name'])} for a in rows}}
                    case_id = f'case-{len(cases):02d}'
                    messages = [{'role': 'system', 'content': PROMPT+
                        '\n仅核对该组当前告警及其对象；关联候选和过去生命周期记录不是该组当前告警。输入可能是被既有预算截断的片段，不要补造缺失信息。'},
                        {'role': 'user', 'content': incident[:3000]}]
                    save(output/f'{case_id}-{arm}.json', messages)
                    save(output/f'{case_id}-labels.json', labels)
                    save(output/f'{case_id}-agent-prompts.json', model.calls)
                    cases.append({'id': case_id, 'phase': phase, 'sample': sample,
                        'scan_sha256': sha(raw), 'scan_id': scan['scan_id'], 'group_id': group['group_id'],
                        'incident_chars': len(incident), 'hypothesis_visible_chars': len(incident[:3000]),
                        'planner_visible_chars': len(incident[:2000]),
                        'dossier_chars': len(json.dumps(dossier, ensure_ascii=False)),
                        'observations_in_context': len(json.loads(incident)['observations']) if arm == 'dossier' else None,
                        'omitted': json.loads(incident).get('omitted') if arm == 'dossier' else None})
    save(output/'cases.json', cases)
    print(encode({'arm': arm, 'groups': len(cases), 'source_scans': 16,
                  'hypothesis_visible_chars': sum(c['hypothesis_visible_chars'] for c in cases)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arm', choices=['raw', 'dossier'], required=True)
    args = parser.parse_args()
    prepare(args.source, args.output, args.arm)
