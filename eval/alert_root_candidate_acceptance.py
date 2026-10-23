"""Opt-in remote HTTP -> Jaeger -> candidate -> dossier -> real RCA experiment.

Injection labels are evaluated after diagnosis and never included in its prompt.
This is controlled HTTP evidence, not Prometheus firing alerts or production accuracy.
"""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import threading
import time
from urllib.request import urlopen
from urllib.error import HTTPError

from agents.alert_compression import compress_alerts, propose_relations, CompressionStore
from agents.alert_context import collect_traces, recall_context, trace_evidence
from agents.alert_semantics import build_scag
from agents.alert_root_candidates import diagnosis_handoff


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--jaeger-url',required=True)
    args=parser.parse_args();root=args.evidence
    inventory=json.loads((root/'inventory.json').read_text())
    captured=json.loads((root/'statistics-observations.json').read_text())
    rows=[]
    for observation in captured['observations']:
        leaf=observation
        while leaf.get('dependency'):leaf=leaf['dependency']
        for sample in (leaf,observation):
            rows.append(dict(name='HTTPFailureIndicator',source='http_probe',cluster_id='default',
                namespace=inventory['pods'][0]['namespace'],pod=sample['pod'],timestamp=sample['end_ns']/1e9,
                metric_name='http_request_failed',metric_value=int(sample['code']>=500),unit='boolean',
                trace_id=sample['trace_id'],span_id=sample['span_id']))
    result=compress_alerts(rows,cluster_id='default')
    result.update(origin='live_probe',cluster_id='default',collection_started_at=time.time(),
                  collection={'http_probe':'ok','jaeger':'ok'})
    result['context_evidence']={'traces':collect_traces(args.jaeger_url,result,inventory)}
    assert result['context_evidence']['traces']['status']=='mapped'
    recall_context(result)
    from web_app.app import _apply_platform_config, _cluster_scoped_config
    from tools.llm_client import LLMClient
    _apply_platform_config();cfg=copy.deepcopy(_cluster_scoped_config('default'))
    asyncio.run(propose_relations(result,LLMClient(cfg.llm),timeout_s=90))
    assert result['semantic']['status']=='ok' and result['semantic']['model_calls']>0
    build_scag(result)
    (root/'root-candidate-scan.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    assert result['root_candidates']['status']=='candidates_ready',result['root_candidates']
    # Labels belong to the evaluator, not candidate generation or diagnosis.
    expected=leaf['pod']
    assert result['root_candidates']['candidates'][0]['entity_ref'].endswith('|'+expected)
    assert not result['root_candidates']['causal_confirmed']
    assert [r['raw'] for r in result['raw_alerts']]==rows
    (root/'root-candidate-scan.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    negatives=[]
    for phase in ('healthy','recovered'):
        negative=copy.deepcopy(result)
        negative['context_evidence']['traces']=trace_evidence(json.loads((root/(phase+'.trace.json')).read_text()),
            'default',inventory,'live_jaeger_probe')
        build_scag(negative)
        assert not negative['root_candidates']['candidates']
        negatives.append(dict(phase=phase,status=negative['root_candidates']['status']))
    store=CompressionStore(root/'compression-store');saved=store.create(result)
    caller_group=next(g['group_id'] for g in saved['groups'] if g['entity']==observation['pod'])
    dossier=store.dossier(saved['scan_id'],caller_group,1)
    assert expected in diagnosis_handoff(dossier)
    (root/'diagnosis-dossier.json').write_text(json.dumps(dossier,ensure_ascii=False,indent=2))
    cfg.runtime.enable_self_healing=False
    cfg.memory.enabled=False
    cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=False
    cfg.observability.jaeger_url=args.jaeger_url
    cfg.pipeline.rca_llm_budget_s=1200
    cfg.pipeline.max_evidence_iterations=3
    from orchestrator.pipeline import Pipeline
    stopped=threading.Event()
    try:
        current_response = urlopen('http://127.0.0.1:18087/probe', timeout=15)
    except HTTPError as exc:
        current_response = exc
    with current_response:
        current_leaf = json.load(current_response)
    while current_leaf.get('dependency'):
        current_leaf = current_leaf['dependency']
    (root/'independent-runtime-observation.json').write_text(json.dumps(current_leaf, indent=2))
    def load():
        while not stopped.is_set():
            try:
                try:response=urlopen('http://127.0.0.1:18087/probe',timeout=15)
                except HTTPError as exc:response=exc
                with response:response.read()
            except Exception:pass
            stopped.wait(1)
    worker=threading.Thread(target=load,daemon=True);worker.start()
    events=[]
    def record(event):
        events.append(event)
        with (root/'rca-progress.jsonl').open('a') as stream:
            stream.write(json.dumps(event,ensure_ascii=False,default=str)+'\n')
    try:
        pipeline=Pipeline(cfg)
        outcome=asyncio.run(pipeline.run(
            '请调查当前 HTTP 503 的依赖传播，明确区分观测、根因假设和未证实机制。',
            namespace=inventory['pods'][0]['namespace'],initial_evidence={'payload':dossier},
            log_callback=record))
    finally:
        stopped.set();worker.join(timeout=20)
    output=outcome.to_dict()
    (root/'rca-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str))
    (root/'rca-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2,default=str))
    assert output['status']=='completed',output.get('error')
    diagnosis=output.get('result',{})
    final=diagnosis.get('result',{})
    items=[item for event in events if isinstance(event,dict) and event.get('event')=='structured_evidence'
           for item in event.get('items',[])]
    log_rows = []
    for item in items:
        if item.get('source') != 'kubectl' or item.get('service') != expected:
            continue
        for line in item.get('observation', '').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                log_rows.append(row)
    change_id = current_leaf.get('state', {}).get('change_id')
    state_transition = bool(change_id) and any(
        row.get('event') == 'state_transition' and row.get('pod') == expected
        and row.get('change_id') == change_id and row.get('field') == 'FAILED'
        and row.get('before') is False and row.get('after') is True for row in log_rows)
    state_observed = bool(change_id) and any(
        row.get('pod') == expected and row.get('code') == 503
        and row.get('state', {}).get('FAILED') is True
        and row.get('state', {}).get('change_id') == change_id for row in log_rows)
    gates=dict(pipeline_completed=output['status']=='completed',
        trace_observations=any(i.get('source')=='jaeger' for i in items),
        target_logs=any(i.get('source')=='kubectl' and i.get('service')==expected
                        and i.get('signal_type')=='pod_logs_observed' for i in items),
        component_identified=expected in final.get('root_cause',''),
        exact_root_entity=final.get('root_entity') == {
            'namespace': inventory['pods'][0]['namespace'], 'kind': 'Pod', 'name': expected},
        root_entity_observed=final.get('root_entity_validation', {}).get('observed') is True,
        state_transition_observed=state_transition,
        runtime_state_observed=state_observed,
        budget_available=not final.get('model_budget',{}).get('exhausted',True),
        judge_accepted=diagnosis.get('judge',{}).get('needs_review') is False)
    report=dict(passed=all(gates.values()),acceptance_gates=gates,expected_component=expected,top_candidate=result['root_candidates']['candidates'][0]['entity_ref'],
        model=result['semantic'],candidate_causal_confirmed=False,negative_cases=negatives,
        diagnosis_status=output['status'],raw_preserved=True,
        scope='real HTTP observations and Jaeger collection with real model and Pipeline; not Prometheus alert intake or calibrated root accuracy')
    (root/'root-acceptance-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    assert report['passed'], report['acceptance_gates']


if __name__=='__main__':main()
