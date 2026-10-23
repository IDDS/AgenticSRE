"""Opt-in isolated Web API with real collectors and model/tool receipts.

Run on the remote experiment host. Only the model configuration is read from
read-only production mounts; no production data directory is mounted.
"""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid


def save(path, value):
    raw=json.dumps(value,ensure_ascii=False,indent=2,default=str)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(raw)
    temporary.replace(path)


def model_config():
    import yaml
    from configs.config_loader import LLMConfig, _walk_and_substitute
    values=_walk_and_substitute(yaml.safe_load(Path('/runtime/config.yaml').read_text())).get('llm',{})
    override=json.loads(Path('/runtime/platform.json').read_text()).get('llm',{})
    cfg=LLMConfig(**{k:v for k,v in values.items() if k in LLMConfig.__dataclass_fields__})
    for key in ('model','base_url','api_key'):
        if override.get(key):setattr(cfg,key,override[key])
    if not cfg.api_key:raise ValueError('missing model configuration')
    cfg.temperature,cfg.max_tokens=0,2048
    return cfg


def install_receipts(root):
    from tools.llm_client import LLMClient
    from tools.base_tool import SRETool
    from orchestrator.pipeline import Pipeline
    calls,tools,runs=[],[],[]
    lock=threading.RLock()
    active=None
    original_chat,original_tool,original_run=LLMClient.chat,SRETool.execute,Pipeline.run

    async def run(self,*args,**kwargs):
        nonlocal active
        with lock:
            if active is not None:raise RuntimeError('acceptance diagnoses run sequentially')
            active=uuid.uuid4().hex
            identity=active
            record=dict(id=identity,started_at=time.time(),namespace=kwargs.get('namespace') or (args[1] if len(args)>1 else ''))
            runs.append(record);save(root/'runs.json',runs)
        try:
            return await original_run(self,*args,**kwargs)
        finally:
            with lock:
                record['finished_at']=time.time();active=None;save(root/'runs.json',runs)

    def chat(self,messages,temperature=None,max_tokens=None,model=None,**kwargs):
        with lock:
            if active is None:raise RuntimeError('model use outside an active acceptance diagnosis')
            if sum(row['run']==active for row in calls)>=16:
                raise TimeoutError('frozen acceptance model call budget exhausted')
            row=dict(id=len(calls)+1,run=active,messages=copy.deepcopy(messages),provider={},started_at=time.time())
            calls.append(row);save(root/'calls.json',calls)
        receipt=kwargs.setdefault('_receipt',{})
        kwargs.update(thinking=False,max_retries=0,request_timeout_s=45)
        try:
            value=original_chat(self,messages,temperature=0,max_tokens=min(max_tokens or 2048,2048),model=model,**kwargs)
            row['answer']=value
            return value
        except Exception as exc:
            row['error_type']=type(exc).__name__
            raise
        finally:
            with lock:
                row['provider']=copy.deepcopy(receipt);row['finished_at']=time.time()
                save(root/'calls.json',calls)

    def execute(self,**kwargs):
        result=original_tool(self,**kwargs)
        with lock:
            tools.append(dict(run=active,tool=self.name,arguments=kwargs,success=result.success,
                duration_ms=result.duration_ms,data_sha256=hashlib.sha256(str(result.data).encode()).hexdigest()))
            save(root/'tools.json',tools)
        return result

    Pipeline.run,LLMClient.chat,SRETool.execute=run,chat,execute


def main():
    if os.environ.get('AGENTICSRE_ONLINE_ACCEPTANCE')!='1':
        raise SystemExit('explicit isolated acceptance opt-in required')
    import configs.config_loader as loader
    cfg=loader.AppConfig();cfg.llm=model_config()
    cfg.kubernetes.kubeconfig='/run/acceptance-kube/config'
    cfg.kubernetes.use_ssh=cfg.kubernetes.use_dry_run=False
    cfg.observability.prometheus_url='http://127.0.0.1:19'
    cfg.observability.jaeger_url=cfg.observability.elasticsearch_url='http://127.0.0.1:19'
    cfg.memory.enabled=cfg.memory.auto_learn=False
    cfg.evolution.enabled=cfg.evolution.auto_record=False
    cfg.runtime.enable_self_healing=cfg.pipeline.enable_recovery=cfg.remediation.enabled=False
    cfg.pipeline.enable_graph_rca=cfg.pipeline.enable_collaboration_optimization=False
    cfg.pipeline.rca_llm_budget_s=180;cfg.pipeline.rca_llm_call_timeout_s=45
    cfg.pipeline.max_evidence_iterations=2
    cfg.fault_targets.k8s_clusters=[dict(id='default',name='Isolated acceptance',kind='general',
        kubeconfig=cfg.kubernetes.kubeconfig,prometheus_url=cfg.observability.prometheus_url,
        jaeger_url=cfg.observability.jaeger_url)]
    loader._config=cfg
    install_receipts(Path('/audit'))
    logging.disable(logging.CRITICAL)
    import web_app.app as web
    web._state['config']=cfg
    import uvicorn
    uvicorn.run(web.app,host='127.0.0.1',port=int(os.environ['AGENTICSRE_ACCEPTANCE_PORT']),log_level='critical')


if __name__=='__main__':main()
