"""Remote controlled Kubernetes failures -> import API -> live-tool/model RCA.

These are development cases, not natural incidents or independent accuracy
acceptance. Frozen labels are never mounted into the diagnosis containers.
"""
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASELINE='sha256:806c44ada1056c6c2f3eab21666a1ebf5bf5a72c42bcf92acc2f5c8ef181ea91'


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


def run(*args,input=None):
    result=subprocess.run(args,input=input,text=True,capture_output=True,timeout=150)
    if result.returncode:raise RuntimeError('remote command failed: '+str(args[:3]))
    return result.stdout


def request(port,path,body=None):
    req=Request(f'http://127.0.0.1:{port}'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=60) as response:return json.load(response)


def collect(container,namespace):
    code='''import json,sys
from agents.event_agent import EventAgent
from tools.k8s_tools import KubectlTool,K8sResourceTool
from tools.base_tool import ToolRegistry
r=ToolRegistry.isolated()
k=KubectlTool(kubeconfig="/run/acceptance-kube/config",namespace=sys.argv[1],allow_write=False,use_dry_run=False)
r.register(k);r.register(K8sResourceTool(k))
a=EventAgent(None,r)
print(json.dumps(a._build_current_state_evidence_items({},a._fetch_problem_pods(sys.argv[1]))))
'''
    return json.loads(run('docker','exec',container,'python','-c',code,namespace))


def await_run(port,path,timeout=270):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=request(port,path)
        if value['status'] not in ('queued','starting','running'):return value
        time.sleep(1)
    raise TimeoutError('operation did not reach a terminal status')


def compression_load(port,out):
    def one(index):
        ns='load-'+str(index)
        rows=[dict(name='ControlledLoad',source='synthetic-load',message='controlled latency observation',
            timestamp=time.time(),cluster_id='default',namespace=ns,pod='object-'+str(i),
            severity='critical' if i==0 else 'warning') for i in range(8)]
        started=time.monotonic()
        job=request(port,'/api/alerts/compression-jobs',dict(alerts=rows,cluster_id='default',namespace=ns,semantic=False))
        admitted=time.monotonic()-started
        result=await_run(port,'/api/alerts/compression-jobs/'+job['job_id'],120)
        scan=request(port,'/api/alerts/compression/'+result['scan_id'])
        indexes=[i for group in scan['groups'] for i in group['alert_indices']]
        checks=dict(completed=result['status']=='completed',model_calls_zero=result['model_calls']==0,
            partition=sorted(indexes)==list(range(8)),namespace=all(r['namespace']==ns for r in scan['raw_alerts']),
            groups=len(scan['groups'])==8,critical=any(g['severity']=='critical' for g in scan['groups']))
        return dict(job_id=job['job_id'],checks=checks,passed=all(checks.values()),
            admission_s=admitted,completion_s=time.monotonic()-started)
    phases=[]
    for concurrency in (1,4,8):
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            rows=list(pool.map(one,[concurrency*100+i for i in range(8)]))
        values=sorted(row['completion_s'] for row in rows)
        phases.append(dict(concurrency=concurrency,samples=8,passed=all(r['passed'] for r in rows),
            p50_s=statistics.median(values),p95_s=values[math.ceil(.95*len(values))-1],runs=rows))
        save(out/'load.json',dict(phases=phases,scope='controlled functional concurrency; customer SLO not supplied'))
    return all(p['passed'] for p in phases)


def main():
    if os.environ.get('AGENTICSRE_RUN_ONLINE_ACCEPTANCE')!='1':raise SystemExit('explicit isolated opt-in required')
    root=Path(os.environ['AGENTICSRE_EXPERIMENT_ROOT'])
    candidate=(root/'candidate-image.txt').read_text().strip()
    assert candidate.startswith('sha256:')
    token=uuid.uuid4().hex[:8];owner='alert-online-'+token
    out=root/token;out.mkdir()
    (out/'sources').mkdir()
    for filename in ('alert_online_app.py','alert_online_acceptance.py'):
        shutil.copyfile(root/filename,out/'sources'/filename)
    cases=[dict(id='case-a',kind='app_config',reason='CreateContainerConfigError'),
           dict(id='case-b',kind='image_name',reason='InvalidImageName'),
           dict(id='case-c',kind='init_config',reason='CreateContainerConfigError')]
    for case in cases:case.update(namespace=owner+'-'+case['id'][-1],pod='workload',config='cfg-'+token)
    healthy=owner+'-control'
    arms=os.environ.get('AGENTICSRE_ACCEPTANCE_ARMS','baseline,candidate').split(',')
    assert arms in (['baseline','candidate'],['candidate'])
    contract=dict(scope='controlled development failures; not natural-incident accuracy',natural_incidents=0,
        images={'baseline':BASELINE,'candidate':candidate},cases=cases,healthy_namespace=healthy,
        order=[[arm for arm in order if arm in arms] for order in
            (['baseline','candidate'],['candidate','baseline'],['baseline','candidate'])],
        limits=dict(max_calls_per_rca=16,max_output_tokens=2048,total_s=180,call_s=45),
        source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (root/'alert_online_app.py',root/'alert_online_acceptance.py')},
        checks=['current reason/message and exact object retained in actual collector',
            'healthy control has no problem evidence','literal alert citations',
            'typed mechanism claim and target; unsupported output remains reviewable',
            'provider receipts and live tool calls','scope/revision rejection','controlled concurrency without data loss'])
    save(out/'contract.json',contract)
    (out/'contract.sha256').write_text(hashlib.sha256((out/'contract.json').read_bytes()).hexdigest()+'\n')
    names=[];namespaces=[]
    report=dict(scope=contract['scope'],cases=[],status='running',formal_verdict='not_accepted')
    save(out/'report.json',report)
    print(json.dumps({'campaign':token,'phase':'frozen','cases':len(cases)}),flush=True)
    before=request(8080,'/api/alerts/lifecycle')
    production=json.loads(run('docker','inspect','agenticsre'))[0]
    assert production['Image']==BASELINE
    mounts={m['Destination']:m['Source'] for m in production['Mounts']}
    env=dict(os.environ)
    env_names=[]
    for item in production['Config']['Env']:
        key,_,value=item.partition('=')
        if key.startswith(('LLM_','OPENAI_','DEEPSEEK_')) or key in ('HTTP_PROXY','HTTPS_PROXY','NO_PROXY'):
            env[key]=value;env_names.extend(['-e',key])
    sockets=[]
    try:
        for port in (18088,18089):
            sock=socket.socket();sockets.append(sock);sock.bind(('127.0.0.1',port))
    finally:
        for sock in sockets:sock.close()
    try:
        cached='lsy-1:5001/library/python:3.14.2-alpine'
        pods=json.loads(run('kubectl','get','pods','-A','-o','json'))['items']
        node=next(p['spec']['nodeName'] for p in pods if p['status']['phase']=='Running'
            and any(c['image']==cached for c in p['spec']['containers']))
        for case in cases+[dict(namespace=healthy,kind='healthy',pod='workload')]:
            ns=case['namespace']
            manifest=dict(apiVersion='v1',kind='Namespace',metadata=dict(name=ns,labels={'agenticsre-experiment':owner}))
            run('kubectl','create','-f','-',input=json.dumps(manifest));namespaces.append(ns)
            container=dict(name='app',image=cached,imagePullPolicy='IfNotPresent',command=['python','-c','import time; time.sleep(3600)'],
                resources={'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'100m','memory':'64Mi'}})
            spec=dict(nodeName=node,restartPolicy='Never',containers=[container])
            if case['kind']=='image_name':container['image']='invalid::tag'
            elif case['kind'] in ('app_config','init_config'):
                target=container
                if case['kind']=='init_config':
                    target=dict(container,name='bootstrap',command=['python','-c','print("initialized")'])
                    spec['initContainers']=[target]
                target['env']=[dict(name='MODE',valueFrom={'configMapKeyRef':{'name':case['config'],'key':'mode'}})]
            run('kubectl','apply','-f','-',input=json.dumps(dict(apiVersion='v1',kind='Pod',
                metadata=dict(name=case['pod'],namespace=ns,labels={'agenticsre-experiment':owner}),spec=spec)))
        for case in cases:
            for _ in range(90):
                pod=json.loads(run('kubectl','get','pod',case['pod'],'-n',case['namespace'],'-o','json'))
                states=pod['status'].get('containerStatuses',[])+pod['status'].get('initContainerStatuses',[])
                if any(c.get('state',{}).get('waiting',{}).get('reason')==case['reason'] for c in states):break
                time.sleep(2)
            else:raise AssertionError('expected controlled state unavailable')
            save(out/(case['id']+'-pod.json'),pod)
        run('kubectl','wait','-n',healthy,'--for=condition=Ready','pod/workload','--timeout=120s')
        apps={}
        for arm,port in [('baseline',18088),('candidate',18089)]:
            if arm not in arms:continue
            folder=out/arm;folder.mkdir()
            for name in ('data','logs','audit'):(folder/name).mkdir()
            image=contract['images'][arm]
            assert run('docker','image','inspect',image,'--format','{{.Id}}').strip()==image
            name=owner+'-'+arm;names.append(name)
            command=['docker','run','-d','--pull','never','--name',name,'--label','agenticsre.experiment='+owner,
                '--network','host','--cpus','2','--memory','4g','--no-healthcheck',
                '-e','PYTHONPATH=/app','-e','AGENTICSRE_ONLINE_ACCEPTANCE=1','-e','AGENTICSRE_AUTH_ENABLED=0',
                '-e','AGENTICSRE_ALERT_EMBEDDING_MODEL=all-MiniLM-L6-v2','-e','AGENTICSRE_ACCEPTANCE_PORT='+str(port),*env_names,
                '-v',str(out/'sources/alert_online_app.py')+':/acceptance/app.py:ro',
                '-v','/home/ubuntu/.kube:/run/acceptance-kube:ro',
                '-v',mounts['/app/configs/config.yaml']+':/runtime/config.yaml:ro',
                '-v',mounts['/app/data']+'/platform_config.json:/runtime/platform.json:ro',
                '-v',str(folder/'data')+':/app/data','-v',str(folder/'logs')+':/app/logs',
                '-v',str(folder/'audit')+':/audit','--entrypoint','python',image,'/acceptance/app.py']
            subprocess.run(command,env=env,check=True,stdout=subprocess.DEVNULL,timeout=90)
            for _ in range(60):
                try:
                    if request(port,'/api/health')['status']=='ok':break
                except Exception:time.sleep(1)
            else:raise RuntimeError('isolated app not ready')
            apps[arm]=(name,port,folder)
            rows=collect(name,healthy);save(folder/'healthy-evidence.json',rows)
            assert rows==[]
        for case,order in zip(cases,contract['order']):
            for arm in order:
                name,port,folder=apps[arm]
                evidence=collect(name,case['namespace']);save(folder/(case['id']+'-collector.json'),evidence)
                pod=json.loads((out/(case['id']+'-pod.json')).read_text())
                assert not any(c.get('ready') for c in pod['status'].get('containerStatuses',[]))
                rows=[dict(name='PodNotReady',source='controlled-live-pod-status',message='Observed Pod is not ready',
                    timestamp=time.time(),cluster_id='default',namespace=case['namespace'],pod=case['pod'],severity='warning',
                    observed_uid=pod['metadata']['uid'],observed_resource_version=pod['metadata']['resourceVersion'])]
                scan=request(port,'/api/alerts/compress',dict(alerts=rows,cluster_id='default',namespace=case['namespace'],semantic=False))
                save(folder/(case['id']+'-scan.json'),scan)
                assert len(scan['groups'])==1
                ref=dict(scan_id=scan['scan_id'],group_id=scan['groups'][0]['group_id'],revision=scan['revision'])
                for invalid in (dict(alert_group=ref,namespace='other'),dict(alert_group=dict(ref,revision=ref['revision']+1))):
                    try:request(port,'/api/rca/run',invalid)
                    except HTTPError as exc:assert exc.code==409
                    else:raise AssertionError('scope/revision mismatch accepted')
                started=time.monotonic()
                run_id=request(port,'/api/rca/run',dict(alert_group=ref))['run_id']
                result=await_run(port,'/api/rca/'+run_id)
                save(folder/(case['id']+'-rca.json'),result)
                final=(result.get('result') or {}).get('result',{}).get('result',{})
                target=final.get('root_entity') or {}
                checks=dict(completed=result['status']=='completed',
                    literal_citations=final.get('citation_validation',{}).get('status')=='passed',
                    live_message_in_catalog=any(case['reason'] in row['observation'] and
                        ('Reported message:' in row['observation']) for row in final.get('evidence_references',[])),
                    supported_target=final.get('diagnosis_status')=='supported_hypothesis' and
                        target.get('namespace')==case['namespace'] and target.get('name')==case['pod'],
                    review_blocks_automation=final.get('diagnosis_status')!='requires_review' or final.get('automation_gate',{}).get('allowed') is False)
                entry=dict(case=case['id'],arm=arm,run_id=run_id,port=port,seconds=round(time.monotonic()-started,3),
                    diagnosis_status=final.get('diagnosis_status'),checks=checks)
                report['cases'].append(entry);save(out/'report.json',report)
                print(json.dumps(entry),flush=True)
        report['load_passed']=compression_load(apps['candidate'][1],out)
        report['production_lifecycle_unchanged']=before==request(8080,'/api/alerts/lifecycle')
        assert report['production_lifecycle_unchanged']
        report['status']='completed'
    except BaseException as exc:
        report.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        cleanup=[]
        for name in names:
            item=json.loads(run('docker','inspect',name))[0]
            assert item['Config']['Labels'].get('agenticsre.experiment')==owner
            run('docker','stop','-t','5',name);run('docker','rm',name)
            cleanup.append(name)
        for ns in namespaces:
            item=json.loads(run('kubectl','get','namespace',ns,'-o','json'))
            assert item['metadata']['labels'].get('agenticsre-experiment')==owner
            run('kubectl','delete','namespace',ns,'--wait=true','--timeout=120s')
            cleanup.append(ns)
        report['cleanup']=cleanup
        save(out/'report.json',report)
    print(json.dumps({'campaign':token,'status':report['status'],'formal_verdict':'not_accepted'}),flush=True)


if __name__=='__main__':main()
