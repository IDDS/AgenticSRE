import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

from tools.llm_client import DeadlineLLMClient, LLMClient
from orchestrator.rca_engine import _apply_model_completion_gate


def client(response):
    value = object.__new__(DeadlineLLMClient)
    value.model, value.temperature, value.max_tokens = 'fixture', 0, 100
    value.deadline, value.call_timeout_s, value.timeout_count = time.monotonic()+10, 1, 0
    value.incomplete_responses = []
    value.client = NS(chat=NS(completions=NS(create=lambda **kwargs: response)))
    return value


def response(reason='stop', text='{"root_cause":"observation"}', **kwargs):
    return NS(choices=[NS(finish_reason=reason, message=NS(content=text,
        refusal=kwargs.get('refusal'), tool_calls=kwargs.get('tool_calls')))],
        usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15))


@pytest.mark.parametrize('result,status', [
    (response('length'), 'truncated'), (response('content_filter'), 'filtered'),
    (response(refusal='refused'), 'refused'), (response(tool_calls=[{}]), 'tool_call'),
    (response(None), 'incomplete'), (response(text=''), 'empty'),
])
def test_plausible_partial_json_cannot_be_consumed_as_complete(result, status):
    model = client(result)
    receipt = {}
    with pytest.raises(ValueError, match='not complete'):
        model.chat([], _receipt=receipt)
    assert model.incomplete_responses == [status]
    assert receipt['usage']['prompt_tokens'] == 10


def test_complete_response_remains_usable_with_receipt():
    model = client(response())
    assert model.json_chat([], with_receipt=True).data['root_cause'] == 'observation'
    assert model.incomplete_responses == []


@pytest.mark.parametrize('statuses,timeouts', [(['truncated'], 0), ([], 1), (['refused'], 2)])
def test_later_passing_judge_does_not_erase_incomplete_execution(statuses, timeouts):
    report = {'root_cause': 'observed symptoms', 'confidence': .99}
    judge = {'needs_review': False, 'judge_level': 0, 'combined_score': .95, 'review_blockers': []}
    final, quality, blocked = _apply_model_completion_gate(report, judge,
        NS(incomplete_responses=statuses, timeout_count=timeouts))
    assert blocked and quality['needs_review'] and quality['combined_score'] == 0
    assert final['root_cause'] == report['root_cause'] and final['confidence'] == .65
    assert report['confidence'] == .99 and judge['needs_review'] is False
    assert 'incomplete_model_execution' in quality['review_blockers']


def test_complete_execution_keeps_existing_quality_decision():
    report, judge = {'confidence': .9}, {'needs_review': False}
    final, quality, blocked = _apply_model_completion_gate(report, judge,
        NS(incomplete_responses=[], timeout_count=0))
    assert final is report and quality is judge and not blocked


@pytest.mark.parametrize('truncated,queue_failure', [(True, False), (False, False), (False, True)])
@pytest.mark.parametrize('query', ['Investigate pod-a HTTP errors', 'Pod/pod-a Pending; investigate HTTP errors'])
def test_real_rca_review_blocks_learning_and_recovery_even_if_queue_fails(monkeypatch, tmp_path, truncated, queue_failure, query):
    import orchestrator.rca_engine as engine
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    cfg = AppConfig()
    cfg.llm.api_key = 'offline-regression-placeholder'
    cfg.memory.enabled = cfg.memory.auto_learn = True
    cfg.evolution.enabled = False
    cfg.memory.db_path = str(tmp_path/'memory')
    cfg.pipeline.enable_collaboration_optimization = cfg.pipeline.enable_graph_rca = False
    cfg.pipeline.enable_recovery = True
    cfg.pipeline.max_evidence_iterations = 1
    cfg.remediation.confidence_threshold = .1
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('configs.config_loader.get_config', lambda: cfg)

    def chat(self, messages, **kwargs):
        system = messages[0]['content']
        kwargs['_receipt']['completion_status'] = 'complete'
        if 'cross-signal correlation' in system and truncated:
            kwargs['_receipt']['completion_status'] = 'truncated'
            return 'invented partial correlation'
        if 'independent RCA evidence reviewer' in system:
            return json.dumps(dict(judge_level=0, coherence_score=.99, evidence_score=.99,
                specificity_score=.99, actionability_score=.99, supported_evidence_ids=['E1'],
                unsupported_claims=[] if truncated else ['unsupported CPU conclusion'], contradictions=[]))
        return json.dumps({'root_cause': 'ns/pod-a observed HTTP errors; mechanism unknown',
            'confidence': .99, 'fault_type': 'application', 'reasoning_chain': 'Observed HTTP errors',
            'hypotheses': [{'id': 'H1', 'description': 'observed HTTP errors', 'confidence': .99}],
            'plan': [{'agent': 'metric_agent', 'priority': 'high'}]})

    class Collector:
        def __init__(self, *args):
            pass

        async def analyze(self, *args, **kwargs):
            return {'summary': 'observed HTTP errors', 'evidence_items': [{'source': 'prometheus',
                'service': 'pod-a', 'signal_type': 'error', 'observation': 'error ratio=1'}]}

    def forbidden(*args, **kwargs):
        raise AssertionError('incomplete model execution must not learn or recover')

    class Store:
        def __init__(self, *args):
            pass

        def get_historical_context(self, *args):
            return {'rules': [], 'faults': []}

        def resolution_stats(self, **kwargs):
            return {}

    class Learner:
        def __init__(self, *args):
            pass

        learn_from_trace = forbidden
        store_fault_context = forbidden

    monkeypatch.setattr(LLMClient, 'chat', chat)
    for name in ('MetricAgent', 'LogAgent', 'TraceAgent', 'EventAgent', 'LLMInferenceAgent'):
        monkeypatch.setattr(engine, name, Collector)
    monkeypatch.setattr(engine, 'RemediationAgent', forbidden)
    monkeypatch.setattr(engine, 'FaultContextStore', Store)
    monkeypatch.setattr(engine, 'ContextLearner', Learner)
    if queue_failure:
        def unavailable(*args, **kwargs):
            raise OSError('review store unavailable')
        monkeypatch.setattr(engine.HumanReviewStore, 'create_review', unavailable)
    result = asyncio.run(engine.run_rca(query, 'ns',
        config=cfg, registry=ToolRegistry()))
    assert result['status'] == 'completed'
    assert result['judge']['needs_review']
    assert bool(result['human_review']) is not queue_failure
    assert result['result']['automation_gate']['allowed'] is False
    assert result['result']['diagnosis_status'] == 'requires_review'
    if truncated:
        assert result['result']['model_budget']['incomplete_responses'] == ['truncated']
        assert result['result']['model_response_validation']['status'] == 'requires_review'
    else:
        assert result['result']['model_budget']['incomplete_responses'] == []
        assert 'unsupported_claims' in result['result']['automation_gate']['blockers']
