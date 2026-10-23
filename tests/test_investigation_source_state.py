"""A failed collector must not disappear when another source runs; execute remotely."""
import asyncio
import copy
import json
import os
from pathlib import Path

import pytest

from diagnostic_strategy import InvestigationController


def candidate(**changes):
    return {'id': 'H1', 'description': '依赖连接失败待验证', 'confidence': .6,
            'status': 'active', 'supporting_evidence_ids': [], 'contradicting_evidence_ids': [],
            'validation_errors': [], 'next_agents': [], **changes}


def decide(control, iteration, selected, results, *, hints=(), catalog=None, budget=60):
    catalog = catalog or []
    return control.decide(iteration=iteration, before=[], after=[candidate(next_agents=list(hints))],
        catalog=catalog, novelty=control.observe(catalog), selected_agents=selected,
        results=results, plan={'plan': []}, budget_remaining=budget)


def controller(agents=('event_agent', 'metric_agent', 'log_agent', 'trace_agent'), limit=5):
    return InvestigationController(agents, max_iterations=limit, confidence_threshold=.85)


def test_failed_source_remains_retryable_after_other_collectors_run():
    control = controller()
    first = decide(control, 1, ['event_agent'], {'event_agent': {'error': True}})
    assert first['decision']['next_agents'] == ['metric_agent', 'log_agent',][:2]
    second = decide(control, 2, ['metric_agent', 'log_agent'],
                    {'metric_agent': {}, 'log_agent': {}}, hints=['event_agent'])
    assert second['decision']['next_agents'] == ['event_agent', 'trace_agent']
    assert second['source_states']['event_agent']['status'] == 'failed'
    assert second['source_states']['event_agent']['last_iteration'] == 1
    assert second['decision']['next_agent_reasons']['event_agent'] == 'hypothesis_requested_failed_source_retry'


def test_unrequested_failure_is_not_forgotten_after_all_other_sources_attempted():
    control = controller()
    decide(control, 1, ['event_agent'], {'event_agent': {'error': 'PRIVATE_ERROR'}})
    decide(control, 2, ['metric_agent', 'log_agent'], {'metric_agent': {}, 'log_agent': {}})
    third = decide(control, 3, ['trace_agent'], {'trace_agent': {}})
    assert third['decision']['next_agents'] == ['event_agent']
    assert third['decision']['next_agent_reasons']['event_agent'] == 'bounded_failed_source_retry'
    assert 'PRIVATE_ERROR' not in json.dumps(control.rounds)


def test_second_failed_attempt_exhausts_retry_budget_and_does_not_spin():
    control = controller(agents=('event_agent',))
    first = decide(control, 1, ['event_agent'], {'event_agent': {'error': True}}, hints=['event_agent'])
    second = decide(control, 2, ['event_agent'], {'event_agent': {'error': True}}, hints=['event_agent'])
    assert first['decision']['next_agents'] == ['event_agent']
    assert second['decision']['action'] == 'stop' and second['decision']['next_agents'] == []
    assert second['source_states']['event_agent']['attempts'] == 2
    assert second['source_states']['event_agent']['retry_eligible'] is False
    assert second['decision']['next_agent_reasons'] == {}


@pytest.mark.parametrize('result', [None, 'invalid', {'success': False}, {'error': 'timeout'},
    {'collection_errors': ['partial']}, {'outcome_unknown': True}])
def test_missing_failed_partial_or_unknown_result_is_a_retryable_gap(result):
    control = controller(agents=('event_agent',))
    record = decide(control, 1, ['event_agent'], {'event_agent': result})
    assert record['source_states']['event_agent']['status'] == 'failed'
    assert record['collection_failures'] == ['event_agent']
    assert record['decision']['next_agents'] == ['event_agent']
    assert record['semantics']['failure_is_counterevidence'] is False


@pytest.mark.parametrize('catalog,status', [([], 'no_citable_observations'),
    ([{'id': 'obs-1', 'agent': 'event_agent', 'semantic_fingerprint': 'state'}], 'observed')])
def test_successful_retry_replaces_failure_without_claiming_empty_is_refutation(catalog, status):
    control = controller(agents=('event_agent',))
    decide(control, 1, ['event_agent'], {'event_agent': {'error': True}})
    final = decide(control, 2, ['event_agent'], {'event_agent': {}}, catalog=catalog)
    assert final['source_states']['event_agent']['status'] == status
    assert final['source_states']['event_agent']['attempts'] == 2
    assert control.feedback()['unresolved_collection_failures'] == []


def test_unselected_result_cannot_invent_an_attempt_or_retry():
    control = controller()
    record = decide(control, 1, ['metric_agent'], {'metric_agent': {}, 'event_agent': {'error': True}})
    assert record['source_states']['event_agent']['status'] == 'untried'
    assert record['source_states']['event_agent']['attempts'] == 0
    assert record['decision']['next_agent_reasons']['event_agent'] == 'untried_source'


def test_source_state_and_feedback_are_detached_snapshots():
    control = controller()
    first = decide(control, 1, ['event_agent'], {'event_agent': {'error': True}})
    frozen = copy.deepcopy(first)
    feedback = control.feedback()
    feedback['source_states']['event_agent']['status'] = 'forged'
    decide(control, 2, ['event_agent'], {'event_agent': {}})
    assert first == frozen and control.rounds[0] == frozen
    assert control.rounds[1]['source_states']['event_agent']['status'] == 'no_citable_observations'


@pytest.mark.parametrize('iteration,budget,reason', [(5, 60, 'iteration_limit_reached'),
                                                    (1, 0, 'model_budget_exhausted')])
def test_retry_does_not_bypass_iteration_or_model_budget(iteration, budget, reason):
    control = controller(agents=('event_agent',))
    record = decide(control, iteration, ['event_agent'], {'event_agent': {'error': True}}, budget=budget)
    assert record['source_states']['event_agent']['retry_eligible'] is True
    assert record['decision']['reason'] == reason and record['decision']['next_agents'] == []
    assert record['decision']['next_agent_reasons'] == {}


def test_no_citable_data_is_not_automatically_retried_as_a_transient_failure():
    control = controller(agents=('log_agent',))
    record = decide(control, 1, ['log_agent'], {'log_agent': {'summary': 'empty logs'}})
    assert record['source_states']['log_agent']['status'] == 'no_citable_observations'
    assert record['decision']['action'] == 'stop'
    assert record['decision']['next_agents'] == []


def test_skill_single_pass_records_actual_source_without_enabling_retry():
    control = controller(agents=(), limit=1)
    record = decide(control, 1, ['skill_agent'], {'skill_agent': {}},
        catalog=[{'id': 'obs-skill', 'agent': 'skill_agent', 'semantic_fingerprint': 'skill'}])
    assert record['source_states']['skill_agent']['status'] == 'observed'
    assert record['decision']['next_agents'] == []
    assert record['decision']['execution_authorized'] is False


def test_engine_retries_requested_failed_source_after_intervening_successes(monkeypatch, tmp_path):
    import orchestrator.rca_engine as engine
    from agents.hypothesis_agent import Hypothesis
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient

    cfg = AppConfig()
    cfg.llm.api_key = 'offline-regression-placeholder'
    cfg.memory.enabled = cfg.memory.auto_learn = cfg.evolution.enabled = False
    cfg.pipeline.enable_collaboration_optimization = cfg.pipeline.enable_correlation = False
    cfg.pipeline.enable_graph_rca = cfg.pipeline.enable_recovery = False
    cfg.pipeline.max_evidence_iterations = 4
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('configs.config_loader.get_config', lambda: cfg)
    actual, attempts, plans = [], {}, []

    def generate(self, *args, **kwargs):
        self.last_generation_method = 'controlled_regression_fixture'
        return [Hypothesis('H1', 'checkout 依赖连接失败待验证', .6),
                Hypothesis('H2', 'checkout 应用配置不匹配待验证', .5)]

    def plan(self, hypotheses, incident, iteration):
        plans.append(incident)
        return {'plan': [{'agent': 'event_agent'}]}

    def collector(name):
        class Collector:
            def __init__(self, *args):
                pass
            async def analyze(self, *args, **kwargs):
                actual.append(name)
                attempts[name] = attempts.get(name, 0) + 1
                if name == 'event_agent' and attempts[name] == 1:
                    return {'error': True, 'summary': 'controlled temporary collector failure'}
                return {'summary': 'controlled observation', 'evidence_items': [{
                    'source': name, 'signal_type': 'application_error', 'observation': name + ' checkout error',
                    'raw_ref': {'kind': 'Service', 'name': 'checkout', 'namespace': 'shop', 'uid': 'fixture-svc'}}]}
        return Collector

    def chat(self, messages, **options):
        options['_receipt']['completion_status'] = 'complete'
        user = messages[-1]['content']
        if '"current_hypotheses"' in user:
            data = json.loads(user)
            refs = [row['id'] for row in data['observations']]
            recovered = any(row['agent'] == 'event_agent' for row in data['observations'])
            return json.dumps({'hypotheses': [{'id': hid, 'confidence': .95 if recovered and hid == 'H1' else .6,
                'status': 'confirmed' if recovered and hid == 'H1' else 'active',
                'supporting_evidence_ids': refs[:2], 'contradicting_evidence_ids': [],
                'reasoning': 'controlled fixture assessment', 'next_agents': ['event_agent'],
                'unresolved_questions': [] if recovered else ['需要事件来源恢复后补证']} for hid in ('H1', 'H2')]})
        return json.dumps({'root_cause': 'controlled fixture: checkout 机制待核验', 'confidence': .6,
                           'fault_type': 'unknown', 'reasoning_chain': 'regression fixture, not cluster truth'})

    monkeypatch.setattr(engine.HypothesisAgent, 'generate', generate)
    monkeypatch.setattr(engine.PlanningAgent, 'generate_plan', plan)
    monkeypatch.setattr(LLMClient, 'chat', chat)
    for cls, name in [('EventAgent', 'event_agent'), ('MetricAgent', 'metric_agent'),
                      ('LogAgent', 'log_agent'), ('TraceAgent', 'trace_agent'), ('LLMInferenceAgent', 'llm_inference_agent')]:
        monkeypatch.setattr(engine, cls, collector(name))
    result = asyncio.run(engine.run_rca('Investigate checkout errors', 'shop', config=cfg, registry=ToolRegistry()))
    assert result['status'] == 'completed'
    strategy = result['diagnostic_strategy']
    rounds = strategy['rounds']
    assert len(rounds) == 3
    assert rounds[0]['selected_agents'] == ['event_agent']
    assert set(rounds[1]['selected_agents']) == {'metric_agent', 'log_agent'}
    assert rounds[1]['source_states']['event_agent']['status'] == 'failed'
    assert rounds[1]['decision']['next_agents'] == ['event_agent', 'trace_agent']
    assert set(rounds[2]['selected_agents']) == {'event_agent', 'trace_agent'}
    assert rounds[2]['source_states']['event_agent']['status'] == 'observed'
    assert rounds[2]['source_states']['event_agent']['attempts'] == 2
    assert rounds[2]['decision']['reason'] == 'cited_hypothesis_ready_for_independent_review'
    assert 'unresolved_collection_failures' in plans[2] and 'event_agent' in plans[2]
    assert all(row['decision']['execution_authorized'] is False for row in rounds)
    artifact = os.environ.get('AGENTICSRE_RETRY_TEST_ARTIFACT')
    if artifact:
        Path(artifact).write_text(json.dumps({'scenario_type': 'controlled_regression_fixture',
            'not_cluster_truth': True, 'actual_investigation_calls': actual[:5], 'strategy': strategy},
            ensure_ascii=False, indent=2))
