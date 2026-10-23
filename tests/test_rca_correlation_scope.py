"""Exercise the engine handoff, including the real correlation matrix builder."""
import asyncio
import json

import pytest


@pytest.mark.parametrize('multiple_objects', [False, True])
def test_lifecycle_correlation_keeps_query_scope_and_full_review_audit(monkeypatch, tmp_path, multiple_objects):
    import orchestrator.rca_engine as engine
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient

    cfg = AppConfig()
    cfg.llm.api_key = 'offline-regression-placeholder'
    cfg.memory.enabled = cfg.memory.auto_learn = cfg.evolution.enabled = False
    cfg.pipeline.enable_collaboration_optimization = cfg.pipeline.enable_recovery = False
    cfg.pipeline.max_evidence_iterations = 1
    monkeypatch.chdir(tmp_path)
    facts = [{'source': 'kubectl get pods -o json', 'signal_type': 'pod_waiting_state',
              'service': name, 'observation': f'Pod/{name} state is ImagePullBackOff',
              'raw_ref': {'namespace': 'shop', 'name': name, 'reason': 'ImagePullBackOff'}}
             for name in ('target', 'peer')]

    summary_modes = []

    class Collector:
        def __init__(self, *args):
            pass

        async def analyze(self, *args, **kwargs):
            if hasattr(self, 'summarize'):
                summary_modes.append(self.summarize)
            return {'evidence_items': facts, 'summary': 'namespace audit',
                    'problem_pods': [{'namespace': 'shop', 'name': n} for n in ('target', 'peer')],
                    'anomaly_details': {'cpu:peer': {'anomalies': [1] * 20}}}

    report_prompts = []

    def chat(self, messages, **kwargs):
        kwargs['_receipt']['completion_status'] = 'complete'
        if '最终 RCA 报告' in messages[0]['content']:
            report_prompts.append(messages[-1]['content'])
        if 'independent RCA evidence reviewer' in messages[0]['content']:
            return json.dumps(dict(judge_level=0, coherence_score=.9, evidence_score=.9,
                specificity_score=.9, actionability_score=.9, supported_evidence_ids=['E1'],
                unsupported_claims=[], contradictions=[]))
        return json.dumps({'hypotheses': [{'id': 'H1', 'confidence': .95}],
            'root_cause': 'Pod/target observed ImagePullBackOff', 'confidence': .9,
            'root_entity': {'namespace': 'shop', 'kind': 'Pod', 'name': 'target'},
            'fault_type': 'image_pull_error', 'reasoning_chain': 'observed image pull failure'})

    monkeypatch.setattr(LLMClient, 'chat', chat)
    for name in ('MetricAgent', 'LogAgent', 'TraceAgent', 'EventAgent', 'LLMInferenceAgent'):
        monkeypatch.setattr(engine, name, Collector)
    captured = []
    original = engine.CorrelationAgent.correlate

    def correlate(self, evidence):
        result = original(self, evidence)
        captured.append((evidence, result))
        return result

    monkeypatch.setattr(engine.CorrelationAgent, 'correlate', correlate)
    query = 'ImagePullBackOff: Pod/target' + (' and Pod/peer' if multiple_objects else '')
    result = asyncio.run(engine.run_rca(query, 'shop', config=cfg, registry=ToolRegistry()))
    assert result['status'] == 'completed' and captured
    evidence, correlation = captured[0]
    ranked = {row['service'] for row in correlation['anomaly_matrix']['ranked_services']}
    assert ('peer' in ranked) is multiple_objects
    assert any(item['service'] == 'target' for value in evidence.values()
               if isinstance(value, dict) for item in value.get('evidence_items', []))
    # Narrowing a reasoning input must not erase independent observations from review.
    assert any(fact['service'] == 'peer' for fact in result['judge']['review_evidence'])
    assert report_prompts and ('Pod/peer' in report_prompts[0]) is multiple_objects
    assert summary_modes and all(mode is multiple_objects for mode in summary_modes)
