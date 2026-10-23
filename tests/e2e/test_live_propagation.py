"""Opt-in remote Kubernetes HTTP fault -> OTLP -> Jaeger evidence acquisition."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def main():
    if os.environ.get('AGENTICSRE_RUN_LIVE_PROPAGATION') != '1':
        print('SKIP: explicit opt-in required')
        return
    root=Path(os.environ.get('AGENTICSRE_PROPAGATION_OUTPUT','/tmp/agenticsre-propagation'))
    root.mkdir(parents=True,exist_ok=True)
    ns='agenticsre-propagation-'+uuid.uuid4().hex[:8]
    label='agenticsre-propagation-probe'
    forwards=[]
    report={'namespace':ns,'status':'running','phases':[]}
    def kubectl(*args,input=None):
        return subprocess.run(['kubectl',*args],input=input,text=True,capture_output=True,check=True,timeout=150).stdout
    def call(base,path,payload=None):
        req=Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        try: response=urlopen(req,timeout=20)
        except HTTPError as exc: response=exc
        with response: return response.code,json.load(response)
    def forward(namespace,service,local,remote):
        log=(root/f'forward-{local}.log').open('w')
        p=subprocess.Popen(['kubectl','-n',namespace,'port-forward','svc/'+service,f'{local}:{remote}','--address=127.0.0.1'],stdout=log,stderr=log)
        forwards.append((p,log))
        for _ in range(60):
            if p.poll() is not None: raise RuntimeError('port-forward exited')
            try:
                with urlopen(f'http://127.0.0.1:{local}/',timeout=1) as response: response.read()
                return
            except HTTPError: return
            except URLError: time.sleep(.25)
        raise TimeoutError('port-forward not ready')
    try:
        kubectl('create','namespace',ns)
        kubectl('label','namespace',ns,'app.kubernetes.io/managed-by='+label)
        image='lsy-1:5001/library/python:3.14.2-alpine'
        pods=json.loads(kubectl('get','pods','-A','-o','json'))['items']
        node=next(p['spec']['nodeName'] for p in pods if p['status']['phase']=='Running' and any(c['image']==image for c in p['spec']['containers']))
        app=Path(__file__).with_name('propagation_probe_app.py').read_text()
        items=[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'probe-code','namespace':ns},'data':{'app.py':app}}]
        roles = ('dependency', 'middle', 'caller') if os.environ.get('AGENTICSRE_PROPAGATION_MULTIHOP') == '1' else ('dependency', 'caller')
        for index, role in enumerate(roles):
            name=ns+'-'+role
            env=[{'name':'ROLE','value':role},{'name':'SERVICE_NAME','value':name},
                 {'name':'SPLIT_SPANS','value':os.environ.get('AGENTICSRE_PROPAGATION_SPLIT_SPANS','0')}]
            env += [{'name':key,'valueFrom':{'fieldRef':{'fieldPath':field}}} for key,field in [('POD_NAME','metadata.name'),('POD_NAMESPACE','metadata.namespace'),('POD_UID','metadata.uid')]]
            if index: env.append({'name':'DOWNSTREAM','value':f'http://{ns}-{roles[index-1]}:8000'})
            items.append({'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':ns,'labels':{'app':name}},
                'spec':{'nodeName':node,'restartPolicy':'Never','containers':[{'name':'probe','image':image,'imagePullPolicy':'IfNotPresent','command':['python','-u','/probe/app.py'],'env':env,
                'resources':{'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}},
                'readinessProbe':{'httpGet':{'path':'/health','port':8000},'periodSeconds':1},
                'volumeMounts':[{'name':'code','mountPath':'/probe','readOnly':True}]}],
                'volumes':[{'name':'code','configMap':{'name':'probe-code'}}]}})
            items.append({'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':ns},'spec':{'selector':{'app':name},'ports':[{'port':8000,'targetPort':8000}]}})
        kubectl('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':items}))
        kubectl('-n',ns,'wait','--for=condition=Ready','pod','--all','--timeout=120s')
        observed=json.loads(kubectl('-n',ns,'get','pods','-o','json'))['items']
        inventory={'pods':[{'namespace':ns,'name':p['metadata']['name'],'uid':p['metadata']['uid'],'labels':p['metadata']['labels']} for p in observed],
                   'services':[{'namespace':ns,'name':ns+'-'+role,'selector':{'app':ns+'-'+role}} for role in roles]}
        (root/'inventory.json').write_text(json.dumps(inventory,indent=2))
        forward(ns,ns+'-caller',18087,8000)
        forward('train-ticket','jaeger',17686,16686)
        jaeger_url = None
        for prefix in ('', '/jaeger/ui'):
            try:
                status, services = call('http://127.0.0.1:17686'+prefix, '/api/services')
                if status == 200 and isinstance(services.get('data'), list):
                    jaeger_url = 'http://127.0.0.1:17686'+prefix
                    break
            except (ValueError, URLError):
                continue
        assert jaeger_url, 'Jaeger query API unavailable'
        expected_spans = len(roles) + (len(roles)-1 if os.environ.get('AGENTICSRE_PROPAGATION_SPLIT_SPANS') == '1' else 0)
        inventory['expected_spans'] = expected_spans
        (root/'inventory.json').write_text(json.dumps(inventory,indent=2))
        for phase,failed in [('healthy',False),('dependency_failed',True),('recovered',False)]:
            assert call('http://127.0.0.1:18087','/fault',{'failed':failed})[0]==200
            code,observation=call('http://127.0.0.1:18087','/probe')
            assert code==(503 if failed else 200),observation
            deadline=time.time()+30
            while time.time()<deadline:
                _,trace=call(jaeger_url,'/api/traces/'+observation['trace_id'])
                if trace.get('data') and len(trace['data'][0].get('spans',[]))>=expected_spans: break
                time.sleep(.5)
            else: raise AssertionError('trace not ingested')
            (root/(phase+'.trace.json')).write_text(json.dumps(trace,indent=2))
            (root/(phase+'.observation.json')).write_text(json.dumps(observation,indent=2))
            report['phases'].append({'phase':phase,'http_status':code,'trace_id':observation['trace_id'],'spans':len(trace['data'][0]['spans'])})
        if os.environ.get('AGENTICSRE_PROPAGATION_STATISTICS') == '1':
            observations=[]
            statistics_trace=None
            for failed in (False, False, True, False, True, True, False, True, False, False, True, False):
                assert call('http://127.0.0.1:18087','/fault',{'failed':failed})[0]==200
                code,observation=call('http://127.0.0.1:18087','/probe')
                assert code==(503 if failed else 200)
                observations.append(observation)
                if failed and statistics_trace is None:
                    deadline=time.time()+30
                    while time.time()<deadline:
                        _,candidate=call(jaeger_url,'/api/traces/'+observation['trace_id'])
                        if candidate.get('data') and len(candidate['data'][0].get('spans',[]))>=expected_spans:
                            statistics_trace=candidate
                            break
                        time.sleep(.5)
                    assert statistics_trace, 'statistics trace not ingested'
                time.sleep(.75)
            (root/'statistics-observations.json').write_text(json.dumps({'observations':observations,'trace':statistics_trace},indent=2))
        subprocess.run(['docker','run','--rm','--network','host','-e','PYTHONPATH=/app',
            '-v',str(root)+':/audit',
            '-v',str(Path(__file__).resolve().parent.parent.parent/'eval/alert_propagation_acceptance.py')+':/tmp/acceptance.py:ro',
            '--entrypoint','python',os.environ.get('AGENTICSRE_PROPAGATION_IMAGE', 'agenticsre:propagation-20260916'),'/tmp/acceptance.py','--evidence','/audit','--jaeger-url',jaeger_url],check=True)
        if os.environ.get('AGENTICSRE_LOG_SAMPLE_CONTAINER'):
            assert call('http://127.0.0.1:18087','/fault',{'failed':True})[0]==200
            code, current = call('http://127.0.0.1:18087','/probe')
            assert code == 503
            (root/'log-sampling-observation.json').write_text(json.dumps(current, indent=2))
            # Readiness access lines must follow the structured request record.
            time.sleep(3)
            subprocess.run(['docker','exec',os.environ['AGENTICSRE_LOG_SAMPLE_CONTAINER'],
                'python','/app/eval/live_log_sampling_acceptance.py','--evidence','/audit'],check=True)
        if os.environ.get('AGENTICSRE_ROOT_CANDIDATE_CONTAINER'):
            assert os.environ.get('AGENTICSRE_PROPAGATION_STATISTICS') == '1'
            assert call('http://127.0.0.1:18087','/fault',{'failed':True})[0]==200
            subprocess.run(['docker','exec',os.environ['AGENTICSRE_ROOT_CANDIDATE_CONTAINER'],
                'python','/app/eval/alert_root_candidate_acceptance.py','--evidence',
                os.environ['AGENTICSRE_ROOT_CANDIDATE_EVIDENCE'],'--jaeger-url',jaeger_url],check=True)
            assert call('http://127.0.0.1:18087','/fault',{'failed':False})[0]==200
        report['status']='passed'
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__)
        raise
    finally:
        for p,log in forwards:
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill();p.wait()
            log.close()
        try:
            meta=json.loads(kubectl('get','namespace',ns,'-o','json'))['metadata']
            assert ns.startswith('agenticsre-propagation-') and meta['labels'].get('app.kubernetes.io/managed-by')==label
            kubectl('delete','namespace',ns,'--wait=true','--timeout=120s')
            report['cleanup_verified']=subprocess.run(['kubectl','get','namespace',ns],capture_output=True).returncode!=0
        finally:
            (root/'live-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__': main()
