"""Real Pipeline reasoning on frozen observations, not live-tool RCA acceptance.

Only domain collectors are replaced. Hypothesis, planning, reranking,
correlation, report, review and closure use the actual application code.
Run exclusively on the remote experiment host.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from unittest.mock import patch

from eval.alert_diagnosis_replay import encode, save, sha

# Fixed before any RCA output: six fault phases, both disjoint groups, and
# the prior intake-reader citation failure. This is a development selection.
CASE_IDS = ('case-00', 'case-03', 'case-08', 'case-09', 'case-12', 'case-20', 'case-23', 'case-28')
LIMITS = dict(max_calls=16, max_tokens=2048, total_s=180, call_s=45,
              temperature=0, thinking=False, max_retries=0)


def frozen_domains(scan, group):
    """Project captured observations; never infer absent tool responses."""
    namespace, entity = group['namespace'], group['entity']
    ref = {'scan_id': scan['scan_id'], 'revision': scan['revision'],
           'namespace': namespace, 'group_id': group['group_id'], 'as_of': scan['created_at']}
    metrics = []
    for index in group['alert_indices']:
        row = scan['raw_alerts'][index]
        metrics.append({'source': row['source'], 'service': entity,
            'signal_type': 'frozen_alert_observed', 'timestamp': str(row['timestamp']),
            'observation': encode({k: row[k] for k in ('name', 'severity', 'message', 'alert_id')}),
            'raw_ref': dict(ref, alert_id=row['alert_id'])})
    measured = scan.get('metric_evidence', {}).get('groups', {}).get(group['group_id'], {})
    measured = measured.get('measurements', []) if measured.get('status') == 'composite' else [measured]
    for item in measured:
        points = item.get('points', [])
        if not points:
            continue
        metrics.append({'source': 'prometheus', 'service': entity,
            'signal_type': 'frozen_metric_observed', 'timestamp': str(points[-1][0]),
            'observation': encode({'query': item.get('query'), 'evidence_id': item.get('evidence_id'),
                'samples': len(points), 'first': points[0], 'last': points[-1],
                'minimum': min(p[1] for p in points), 'maximum': max(p[1] for p in points)}),
            'raw_ref': dict(ref, evidence_id=item.get('evidence_id'), points=points)})
    spans, seen = [], set()
    entity_ref = '|'.join((group['cluster_id'], namespace, group['entity_kind'], entity))
    for proof in scan.get('window_evidence', {}).get('proofs', []):
        for span in proof.get('spans', []):
            identity = span.get('trace_id'), span.get('span_id')
            if span.get('entity_ref') != entity_ref or identity in seen:
                continue
            seen.add(identity)
            spans.append({'source': 'jaeger', 'service': entity,
                'signal_type': 'frozen_trace_span_observed', 'timestamp': str(span.get('start')),
                'observation': encode(span), 'raw_ref': dict(ref, **dict(zip(('trace_id', 'span_id'), identity)))})
    result = {}
    for name, items in (('metric_agent', metrics), ('trace_agent', spans[:30]),
                        ('log_agent', []), ('event_agent', []), ('llm_inference_agent', [])):
        result[name] = {'agent': name, 'evidence_items': items, 'error': not bool(items),
            'summary': ('冻结采集时点的原始观测；不能确认应用内部根因，不代表当前集群状态。' if items else
                        '该冻结快照未保存此类取证结果；不可用不表示正常。'),
            'source': 'frozen_scan_replay', 'as_of': scan['created_at'],
            'truncated': name == 'trace_agent' and len(spans) > 30}
    return result


def prepare(source, prior, output):
    from agents.alert_compression import CompressionStore
    output.mkdir(exist_ok=False)
    cases = {c['id']: c for c in json.loads((prior/'dossier/cases.json').read_text())}
    for case_id in CASE_IDS:
        case = cases[case_id]
        raw = (source/f"{case['phase']}-{case['sample']}-scan.json").read_bytes()
        assert sha(raw) == case['scan_sha256']
        scan = json.loads(raw)
        group = next(g for g in scan['groups'] if g['group_id'] == case['group_id'])
        with tempfile.TemporaryDirectory() as directory:
            store = CompressionStore(directory)
            save(Path(directory)/(scan['scan_id']+'.json'), scan)
            with patch('agents.alert_semantics.time.time', return_value=scan['created_at']):
                dossier = store.dossier(scan['scan_id'], group['group_id'], scan['revision'])
        query = (f"请调查 {dossier['namespace']} 中 {dossier['entity_kind']}/{dossier['entity']} "
                 f"的告警事件：{dossier['alert_samples'][0]['name']}。根据证据包及实时只读观测验证根因。")
        save(output/(case_id+'.json'), {'query': query, 'namespace': group['namespace'], 'as_of': scan['created_at'],
             'dossier': dossier, 'domains': frozen_domains(scan, group)})
        # Scoring labels are written separately and never loaded by run_one.
        save(output/(case_id+'-label.json'), {'case': case, 'entity': group['entity'],
             'namespace': group['namespace'], 'alert_ids': group['alert_ids'],
             'mechanism_truth': None, 'mechanism_evidence': 'not_collected'})


class FrozenAgent:
    def __init__(self, name, domains, tracker):
        self.name, self.domains, self.tracker = name, domains, tracker

    async def analyze(self, query, namespace='', **kwargs):
        self.tracker.append({'agent': self.name, 'query': query, 'namespace': namespace, 'kwargs': kwargs})
        return copy.deepcopy(self.domains[self.name])


def counting_llm_class(path):
    from tools.llm_client import DeadlineLLMClient

    class CountingLLM(DeadlineLLMClient):
        def __init__(self, config, **kwargs):
            super().__init__(config, total_s=LIMITS['total_s'], call_s=LIMITS['call_s'])
            self.records, self.lock = [], threading.Lock()

        def chat(self, messages, temperature=None, max_tokens=None, model=None, **kwargs):
            with self.lock:
                if len(self.records) >= LIMITS['max_calls']:
                    self.deadline = 0
                    raise TimeoutError('frozen replay call budget exhausted')
                row = {'call': len(self.records)+1, 'messages': copy.deepcopy(messages), 'provider': {}}
                self.records.append(row)
                save(path, self.records)
            kwargs.update(thinking=False, max_retries=0, request_timeout_s=LIMITS['call_s'])
            receipt = kwargs.get('_receipt', {})
            kwargs['_receipt'] = receipt
            start = time.monotonic()
            try:
                answer = super().chat(messages, temperature=0,
                    max_tokens=min(max_tokens or LIMITS['max_tokens'], LIMITS['max_tokens']), model=model, **kwargs)
                row['answer'] = answer
                return answer
            except Exception as exc:
                row['error_type'] = type(exc).__name__
                raise
            finally:
                row['seconds'] = round(time.monotonic()-start, 3)
                row['provider'] = copy.deepcopy(receipt)
                with self.lock:
                    save(path, self.records)

    return CountingLLM


async def run_one(packet_path, output, llm_cfg):
    from configs.config_loader import AppConfig
    import orchestrator.rca_engine as engine
    import orchestrator.pipeline as pipeline_module
    from tools.base_tool import ToolRegistry
    logging.disable(logging.CRITICAL)
    packet = json.loads(packet_path.read_text())
    output.mkdir(exist_ok=False)
    cfg = AppConfig()
    cfg.llm = llm_cfg
    cfg.llm.temperature, cfg.llm.max_tokens = 0, LIMITS['max_tokens']
    cfg.memory.enabled = cfg.memory.auto_learn = False
    cfg.memory.db_path = str(output/'memory')
    cfg.evolution.enabled = cfg.evolution.auto_record = False
    cfg.pipeline.enable_collaboration_optimization = False
    cfg.pipeline.enable_graph_rca = False
    cfg.pipeline.max_evidence_iterations = 2
    cfg.pipeline.enable_recovery = cfg.runtime.enable_self_healing = False
    registry = ToolRegistry()
    tracker, events = [], []
    previous_dir = Path.cwd()
    os.chdir(output)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch('configs.config_loader.get_config', return_value=cfg))
            stack.enter_context(patch.object(pipeline_module, 'build_tool_registry', return_value=registry))
            stack.enter_context(patch.object(engine, 'DeadlineLLMClient', counting_llm_class(output/'calls.json')))
            for cls, name in (('MetricAgent', 'metric_agent'), ('LogAgent', 'log_agent'),
                              ('TraceAgent', 'trace_agent'), ('EventAgent', 'event_agent'),
                              ('LLMInferenceAgent', 'llm_inference_agent')):
                stack.enter_context(patch.object(engine, cls,
                    side_effect=lambda *a, name=name, **k: FrozenAgent(name, packet['domains'], tracker)))
            # Scope replay time to handoff freshness, preserving real latency clocks.
            import agents.alert_root_candidates as roots
            handoff = roots.diagnosis_handoff
            stack.enter_context(patch.object(roots, 'diagnosis_handoff',
                side_effect=lambda value, as_of=None: handoff(value, as_of=packet['as_of'])))
            try:
                import agents.alert_dossier_prompt as prompt
            except ModuleNotFoundError:
                prompt = None  # Historical baseline predates the bounded handoff.
            if prompt:
                initial = prompt.initial_incident
                stack.enter_context(patch.object(prompt, 'initial_incident',
                    side_effect=lambda query, value: initial(query, value, as_of=packet['as_of'])))
            pipeline = pipeline_module.Pipeline(cfg)
            result = await pipeline.run(packet['query'], packet['namespace'],
                initial_evidence={'payload': packet['dossier']}, log_callback=events.append)
            save(output/'result.json', result.to_dict())
            save(output/'events.json', events)
            save(output/'collector-calls.json', tracker)
    finally:
        os.chdir(previous_dir)


def inspect_result(result, calls, label):
    """Mechanical checks only; does not score unknown causal mechanisms."""
    rca = result.get('result', {})
    final = rca.get('result', {})
    text = encode(final)
    refs = sorted(set(re.findall(r'(?<![A-Za-z0-9_-])a-[0-9a-f]{24}(?![A-Za-z0-9_-])', text)))
    entity = final.get('root_entity')
    usage = [c.get('provider', {}).get('usage', {}) for c in calls]
    complete = all(c.get('provider', {}).get('completion_status') == 'complete' for c in calls) and bool(calls)
    known = bool(calls) and all(type(u.get(k)) is int for u in usage for k in ('prompt_tokens', 'completion_tokens'))
    return {'pipeline_completed': result.get('status') == rca.get('status') == 'completed',
        'provider_calls': len(calls), 'provider_complete': complete,
        'input_tokens': sum(u['prompt_tokens'] for u in usage) if known else None,
        'output_tokens': sum(u['completion_tokens'] for u in usage) if known else None,
        'duration_s': result.get('duration_s'), 'literal_alert_citations': refs,
        'unknown_literal_alert_citations': sorted(set(refs)-set(label['alert_ids'])),
        'root_entity_matches_target': isinstance(entity, dict) and entity.get('namespace') == label['namespace']
            and entity.get('name') == label['entity'],
        'root_entity': entity, 'root_cause': final.get('root_cause'),
        'judge_needs_review': rca.get('judge', {}).get('needs_review'),
        'judge_blockers': rca.get('judge', {}).get('review_blockers'),
        'human_review_created': bool(rca.get('human_review')),
        'confidence': final.get('confidence'), 'mechanism_accuracy': None}
