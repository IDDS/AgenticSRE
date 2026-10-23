"""Opt-in remote actual HTTP/Prometheus/Jaeger request-identity acceptance.

Per-request metric labels and explicit rule field annotations are additional
instrumentation; this does not claim the uninstrumented threshold test passes.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
import yaml


def main(scenario=None, metric_mode='request', window_proofs=False, start_app=None):
    if metric_mode not in ('request', 'stable_window'):
        raise ValueError('unsupported metric mode')
    if os.environ.get('AGENTICSRE_RUN_TRACE_ALERT_EXPERIMENT') != '1':
        raise SystemExit('explicit opt-in required')
    root=Path(os.environ.get('AGENTICSRE_EXPERIMENT_ROOT','/home/ubuntu/AgenticSRE-releases/tracespan-20260917'))
    token=uuid.uuid4().hex[:8];ns='agenticsre-tracespan-'+token
    out=root/token;out.mkdir()
    image=os.environ.get('AGENTICSRE_EXPERIMENT_IMAGE','agenticsre:tracespan-20260917');owner='tracespan-'+token
    forwards=[];containers=[];namespace_created=False
    report=dict(status='running',namespace=ns,natural_production_incidents=False,stages=[])
    def run(*args,input=None):
        r=subprocess.run(args,input=input,text=True,capture_output=True,timeout=150)
        if r.returncode:raise RuntimeError(str(args[:3])+' failed: '+r.stderr[:400])
        return r.stdout
    def kubectl(*args,input=None):return run('kubectl',*args,input=input)
    def get(url):
        try:r=urlopen(url,timeout=240)
        except HTTPError as exc:r=exc
        with r:
            data=json.load(r)
            if r.code not in (200,503):raise RuntimeError('unexpected HTTP status '+str(r.code))
            return data
    def forward(namespace,service,port,remote):
        log=(out/f'forward-{port}.log').open('w')
        p=subprocess.Popen(['kubectl','-n',namespace,'port-forward','svc/'+service,f'{port}:{remote}','--address=127.0.0.1'],stdout=log,stderr=log)
        forwards.append((p,log))
        for _ in range(60):
            if p.poll() is not None:raise RuntimeError('port-forward stopped')
            try:
                with urlopen(f'http://127.0.0.1:{port}/health',timeout=1):pass
                return
            except HTTPError:return
            except URLError:time.sleep(.25)
        raise RuntimeError('forward not ready')
    def probe(port,delay=0,fail=False,update='both',emit_trace=True):
        observed=get(f'http://127.0.0.1:{port}/probe?'+urlencode(dict(delay_ms=delay,fail=int(fail),update=update,emit_trace=int(emit_trace))))
        report.setdefault('requests',[]).append(dict(port=port,**observed))
        assert observed['status']==(503 if fail else 200)
        return observed
    def firing():
        return get('http://127.0.0.1:18100/api/v1/query?'+urlencode({'query':'ALERTS{alertstate="firing"}'}))['data']['result']
    def scan(name,raw_count,group_count):
        for _ in range(20):
            active=firing()
            if len(active)==raw_count:break
            time.sleep(1)
        assert len(active)==raw_count
        result=get('http://127.0.0.1:18086/api/alerts/scan?'+urlencode(dict(cluster_id='default',namespace=ns)))
        (out/(name+'-scan.json')).write_text(json.dumps(result,ensure_ascii=False))
        (out/(name+'-firing.json')).write_text(json.dumps(active))
        assert not result.get('degraded')
        assert len(result['raw_alerts'])==raw_count
        assert sorted(i for g in result['groups'] for i in g['alert_indices'])==list(range(raw_count))
        assert sorted((a['metric']['alertname'],a['metric']['trace_id'],a['metric']['span_id']) for a in active)==sorted((r['name'],r['labels']['trace_id'],r['labels']['span_id']) for r in result['raw_alerts'])
        assert len(result['groups'])==group_count,(name,len(result['groups']),result.get('trace_compression'))
        for g in result['groups']:
            members=[result['raw_alerts'][i] for i in g['alert_indices']]
            assert len({r['entity'] for r in members})==1
            assert len({(r['labels']['trace_id'],r['labels']['span_id']) for r in members})==1
            assert not any(r['severity']=='critical' for r in members) or g['severity']=='critical'
        saved=get('http://127.0.0.1:18086/api/alerts/compression/'+result['scan_id'])
        assert saved['groups']==result['groups'] and saved['raw_alerts']==result['raw_alerts']
        entry=dict(phase=name,raw=raw_count,groups=len(result['groups']),ratio=result['compression_ratio'],
            trace_compression=result.get('trace_compression'),observed_spans=len(result.get('context_evidence',{}).get('traces',{}).get('observed_spans',[])),
            semantic=result.get('semantic'),raw_preserved=True,no_cross_request_merge=True)
        report['stages'].append(entry);print(json.dumps(entry),flush=True)
        return result
    try:
        run('docker','image','inspect',image)
        before=get('http://127.0.0.1:8080/api/alerts/lifecycle')
        kubectl('create','namespace',ns);namespace_created=True
        kubectl('label','namespace',ns,'app.kubernetes.io/managed-by='+owner)
        cached='lsy-1:5001/library/python:3.14.2-alpine'
        pods=json.loads(kubectl('get','pods','-A','-o','json'))['items']
        node=next(p['spec']['nodeName'] for p in pods if p['status']['phase']=='Running' and any(c['image']==cached for c in p['spec']['containers']))
        items=[dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='probe-code',namespace=ns),data={'app.py':(root/'trace_metric_probe.py').read_text()})]
        for role in ('a','b','c'):
            name=ns+'-'+role
            env=[dict(name=k,valueFrom={'fieldRef':{'fieldPath':v}}) for k,v in [('POD_NAMESPACE','metadata.namespace'),('POD_NAME','metadata.name'),('POD_UID','metadata.uid')]]
            env.append(dict(name='PROBE_METRIC_MODE',value=metric_mode))
            if os.environ.get('AGENTICSRE_PROBE_METRICS_DELAY_MS'):
                env.append(dict(name='PROBE_METRICS_DELAY_MS',value=os.environ['AGENTICSRE_PROBE_METRICS_DELAY_MS']))
            if os.environ.get('AGENTICSRE_PROBE_SAMPLE_LAG_MS'):
                env.append(dict(name='PROBE_SAMPLE_LAG_MS',value=os.environ['AGENTICSRE_PROBE_SAMPLE_LAG_MS']))
            if os.environ.get('AGENTICSRE_SHARED_TRACE_SERVICE') == '1':
                env.append(dict(name='PROBE_SERVICE_NAME',value=ns+'-shared-service'))
            items.append(dict(apiVersion='v1',kind='Pod',metadata=dict(name=name,namespace=ns,labels={'app':name}),spec=dict(
                nodeName=node,restartPolicy='Never',containers=[dict(name='probe',image=cached,imagePullPolicy='IfNotPresent',
                command=['python','-u','/probe/app.py'],env=env,resources={'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}},
                readinessProbe={'httpGet':{'path':'/health','port':8000},'periodSeconds':1,'initialDelaySeconds':10},volumeMounts=[dict(name='code',mountPath='/probe',readOnly=True)])],
                volumes=[dict(name='code',configMap={'name':'probe-code'})])))
            items.append(dict(apiVersion='v1',kind='Service',metadata=dict(name=name,namespace=ns),spec=dict(selector={'app':name},ports=[dict(port=8000,targetPort=8000)])))
        kubectl('apply','-f','-',input=json.dumps(dict(apiVersion='v1',kind='List',items=items)))
        actual_probe = json.loads(kubectl('-n',ns,'get','configmap','probe-code','-o','json'))['data']['app.py']
        assert actual_probe == (root/'trace_metric_probe.py').read_text()
        (out/'probe-source.py').write_text(actual_probe)
        kubectl('-n',ns,'wait','--for=condition=Ready','pod','--all','--timeout=120s')
        (out/'inventory.json').write_text(kubectl('-n',ns,'get','pods','-o','json'))
        for port,role in zip((18101,18102,18103),('a','b','c')):forward(ns,ns+'-'+role,port,8000)
        forward('train-ticket','jaeger',17686,16686)
        jaeger=''
        for prefix in ('','/jaeger/ui'):
            try:
                if isinstance(get('http://127.0.0.1:17686'+prefix+'/api/services').get('data'),list):jaeger='http://127.0.0.1:17686'+prefix;break
            except Exception:pass
        assert jaeger
        rules=[dict(alert='ProbeLatency'+str(t),expr=f'agenticsre_probe_duration_ms > {t}',labels={'severity':s},annotations={'agenticsre_trace_field':'duration_ms'}) for t,s in ((100,'warning'),(200,'warning'),(300,'critical'))]
        rules += [dict(alert='ProbeError',expr='agenticsre_probe_http_status_code{instance!="127.0.0.1:18103"} > 499',labels={'severity':'warning'},annotations={'agenticsre_trace_field':'http_status_code'}),
                  dict(alert='ProbeIndependentCritical',expr='agenticsre_probe_http_status_code{instance="127.0.0.1:18103"} > 499',labels={'severity':'critical'},annotations={'agenticsre_trace_field':'http_status_code'})]
        if metric_mode == 'stable_window':
            for rule in rules:
                rule['expr'] = rule['expr'].replace('agenticsre_probe_duration_ms', 'agenticsre_probe_window_duration_ms').replace(
                    'agenticsre_probe_http_status_code', 'agenticsre_probe_window_error_ratio').replace('> 499', '> 0.5')
                rule.pop('annotations', None)  # A window aggregate is never an exact Span measurement.
                if window_proofs:
                    rule['annotations'] = dict(agenticsre_window_contract='pod_request_window_v1',
                        agenticsre_window_field='mean_duration_ms' if rule['alert'].startswith('ProbeLatency') else 'error_ratio',
                        agenticsre_window_seconds='10',agenticsre_window_count_metric='agenticsre_probe_window_requests',
                        agenticsre_window_end_metric='agenticsre_probe_window_end_seconds',
                        agenticsre_window_operation='measured GET /probe')
                    if os.environ.get('AGENTICSRE_SHARED_TRACE_SERVICE') == '1':
                        rule['annotations']['agenticsre_window_service'] = ns+'-shared-service'
        (out/'rules.yaml').write_text(yaml.safe_dump({'groups':[{'name':'trace-span-test','interval':'1s','rules':rules}]}))
        (out/'prometheus.yaml').write_text(yaml.safe_dump({'global':{'scrape_interval':'1s','evaluation_interval':'1s'},'rule_files':['/probe/rules.yaml'],
            'scrape_configs':[{'job_name':'trace-span-test','static_configs':[{'targets':[f'127.0.0.1:{p}' for p in (18101,18102,18103)]}]}]}))
        name='agenticsre-tracespan-prom-'+token
        run('docker','run','-d','--pull','never','--name',name,'--label','agenticsre.experiment='+owner,'--network','host',
            '-v',str(out)+':/probe:ro','-v','/home/ubuntu/AgenticSRE-releases/livemetrics-20260916/prometheus-probe:/prom:ro',
            '--entrypoint','/prom',image,'--config.file=/probe/prometheus.yaml','--web.listen-address=127.0.0.1:18100','--storage.tsdb.retention.time=1h');containers.append(name)
        if start_app is not None:
            name='agenticsre-tracespan-app-'+token
            containers.append(name)
            start_app(out=out,image=image,name=name,owner=owner,jaeger=jaeger)
        else:
            start_legacy_app(out,image,owner,token,jaeger,run,containers)
        for _ in range(60):
            try:
                if get('http://127.0.0.1:18086/api/health')['status']=='ok':break
            except Exception:pass
            time.sleep(1)
        else:raise AssertionError('app not ready')
        if scenario is not None:
            scenario(out=out, report=report, probe=probe, get=get, namespace=ns)
        else:
            for port in (18101,18102,18103):probe(port)
            scan('healthy',0,0)
            probe(18101,350,True);probe(18102,350,True);probe(18103,0,True)
            time.sleep(27)
            positive=scan('same-request',9,3)
            assert len([g for g in positive['groups'] if g.get('event_merge')])==2
            for port in (18101,18102,18103):probe(port)
            scan('recovered',0,0)
            probe(18101,350,True,'duration_ms');probe(18101,0,True,'http_status_code')
            time.sleep(27)
            negative=scan('different-requests-same-pod',4,2)
            assert not negative.get('trace_compression')
            for port in (18101,18102,18103):probe(port)
            scan('recovered-final',0,0)
        report.update(status='passed',production_lifecycle_unchanged=before==get('http://127.0.0.1:8080/api/alerts/lifecycle'))
        assert report['production_lifecycle_unchanged']
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__,detail=str(exc)[:500]);raise
    finally:
        for name in reversed(containers):
            owned=subprocess.run(['docker','inspect',name,'--format','{{index .Config.Labels "agenticsre.experiment"}}'],capture_output=True,text=True)
            if owned.returncode==0 and owned.stdout.strip()==owner:run('docker','rm','-f',name)
        for p,log in forwards:
            p.terminate()
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
            log.close()
        if namespace_created:
            meta=json.loads(kubectl('get','namespace',ns,'-o','json'))['metadata']
            assert meta['labels'].get('app.kubernetes.io/managed-by')==owner
            kubectl('delete','namespace',ns,'--wait=true','--timeout=120s')
            report['namespace_removed']=subprocess.run(['kubectl','get','namespace',ns],capture_output=True).returncode!=0
        for relative in ('config.yaml','clusters.yaml','compose.yaml','data/platform_config.json'):(out/relative).unlink(missing_ok=True)
        report['containers_cleaned']=True;report['temporary_config_copies_removed']=True
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'output':str(out),'report':report},ensure_ascii=False),flush=True)


def start_legacy_app(out,image,owner,token,jaeger,run,containers):
    """Historical runner; new acceptance uses a credential-free bootstrap callback."""
    service=yaml.safe_load(Path('/home/ubuntu/AgenticSRE-releases/mergereview-20260917/docker-compose.production.yaml').read_text())['services']['agenticsre']
    service.pop('depends_on',None);service.pop('build',None)
    service.update(image=image,container_name='agenticsre-tracespan-app-'+token,labels={'agenticsre.experiment':owner},
        command=['python','-m','uvicorn','web_app.app:app','--host','127.0.0.1','--port','18086'])
    service['healthcheck']['test']=['CMD','curl','-f','http://127.0.0.1:18086/api/health']
    for d in ('data','logs'):(out/d).mkdir()
    platform=json.loads(run('sudo','cat','/home/ubuntu/agenticsre/data/platform_config.json'))
    platform.setdefault('observability',{}).update(prometheus_url='http://127.0.0.1:18100',jaeger_url=jaeger)
    p=out/'data/platform_config.json';p.write_text(json.dumps(platform));p.chmod(0o600)
    volumes=[]
    for volume in service['volumes']:
        src,dst,*mode=volume.split(':')
        if dst in ('/app/data','/app/logs'):src=str(out/dst.rsplit('/',1)[1])
        if dst in ('/app/configs/config.yaml','/app/configs/clusters.yaml'):
            cfg=yaml.safe_load(Path(src).read_text())
            def update(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k=='prometheus_url':value[k]='http://127.0.0.1:18100'
                        elif k=='jaeger_url':value[k]=jaeger
                        else:update(v)
                elif isinstance(value,list):
                    for v in value:update(v)
            update(cfg)
            if dst.endswith('/config.yaml'):cfg.setdefault('alert',{})['time_window']=30
            p=out/Path(dst).name;p.write_text(yaml.safe_dump(cfg));p.chmod(0o600);src=str(p)
        volumes.append(':'.join([src,dst]+mode))
    service['volumes']=volumes
    assert not any(v.startswith('/home/ubuntu/agenticsre/data:') for v in volumes)
    p=out/'compose.yaml';p.write_text(yaml.safe_dump({'services':{'agenticsre':service}}));p.chmod(0o600)
    containers.append(service['container_name'])
    run('docker','compose','-p',owner,'-f',str(p),'up','-d','--no-build','--pull','never')


if __name__=='__main__':main()
