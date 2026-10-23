"""Opt-in remote experiment: real HTTP -> metric thresholds -> firing -> scan.

Never rewrites production rules, data, credentials or grouping expectations.
"""
import json, os, subprocess, threading, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
import yaml

ROOT=Path(os.environ.get('AGENTICSRE_THRESHOLD_ROOT','/home/ubuntu/AgenticSRE-releases/thresholds-20260917'))
IMAGE=os.environ.get('AGENTICSRE_THRESHOLD_IMAGE','agenticsre:metricui-20260917')


def main():
    if os.environ.get('AGENTICSRE_RUN_THRESHOLD_EXPERIMENT')!='1':raise SystemExit('explicit opt-in required')
    token=uuid.uuid4().hex[:8];namespace='agenticsre-threshold-'+token
    out=ROOT/token;out.mkdir(parents=True)
    label='thresholds-'+token;created=[];stop=threading.Event();profiles={p:(0,False) for p in (18101,18102,18103)};traffic=[]
    report={'status':'running','namespace':namespace,'natural_production_incidents':False,'stages':[]}
    def run(*args):
        r=subprocess.run(args,capture_output=True,text=True)
        if r.returncode:raise RuntimeError('command failed: '+str(args[:3])+' '+r.stderr[:300])
        return r.stdout
    def get(url,timeout=240):
        with urlopen(url,timeout=timeout) as r:return json.load(r)
    def prom(query):return get('http://127.0.0.1:18100/api/v1/query?'+urlencode({'query':query}),15)['data']['result']
    def firing():return prom('ALERTS{alertstate="firing"}')
    def wait_firing(count):
        end=time.monotonic()+25
        while time.monotonic()<end:
            rows=firing()
            if len(rows)==count:return rows
            time.sleep(1)
        raise AssertionError(f'expected {count} real firing alerts, observed {len(rows)}')
    def drive(port):
        while not stop.is_set():
            delay,fail=profiles[port];started=time.time()
            try:
                try:
                    with urlopen(f'http://127.0.0.1:{port}/probe?delay_ms={delay}&fail={int(fail)}',timeout=3) as response:status=response.status;response.read()
                except HTTPError as response:status=response.code;response.read()
                traffic.append(dict(at=started,port=port,status=status,requested_delay_ms=delay))
            except Exception:pass
            stop.wait(.6)
    def scan(stage,count):
        rows=wait_firing(count)
        result=get('http://127.0.0.1:18086/api/alerts/scan?'+urlencode({'cluster_id':'default','namespace':namespace}))
        (out/(stage+'-scan.json')).write_text(json.dumps(result,ensure_ascii=False))
        (out/(stage+'-firing.json')).write_text(json.dumps(rows))
        assert not result.get('degraded'),result.get('collection')
        assert len(result['raw_alerts'])==count
        indices=[i for g in result['groups'] for i in g['alert_indices']]
        assert sorted(indices)==list(range(count))
        expected=sorted((r['metric']['alertname'],r['metric']['instance'],r['metric']['severity']) for r in rows)
        actual=sorted((r['name'],r['labels']['instance'],r['severity']) for r in result['raw_alerts'])
        assert expected==actual
        critical={i for i,r in enumerate(result['raw_alerts']) if r['severity']=='critical'}
        assert critical <= {i for g in result['groups'] if g['severity']=='critical' for i in g['alert_indices']}
        assert all(len({result['raw_alerts'][i]['labels']['instance'] for i in g['alert_indices']})==1 for g in result['groups'])
        saved=get('http://127.0.0.1:18086/api/alerts/compression/'+result['scan_id'])
        assert saved['raw_alerts']==result['raw_alerts'] and saved['groups']==result['groups']
        summary=dict(stage=stage,firing=count,groups=len(result['groups']),compression_ratio=result['compression_ratio'],
            raw_preserved=True,critical_retained=len(critical),cross_object_merges=0,
            semantic=result.get('semantic'),metric_ready=result.get('metric_evidence',{}).get('ready_groups',0),
            relations=len(result.get('relations',[])),scan_id=result['scan_id'])
        report['stages'].append(summary);print(json.dumps(summary),flush=True)
        if os.environ.get('AGENTICSRE_REVIEW_ACCEPTANCE') == '1':
            suggestions = result.get('merge_review', {}).get('suggestions', [])
            summary['review_suggestions'] = len(suggestions)
            if suggestions:
                # Explicit API workflow test on the disposable instance, not autonomous compression.
                payload = dict(revision=result['revision'], action='merge', group_ids=suggestions[0]['group_ids'],
                               reason='isolated acceptance test of explicit review workflow')
                request = Request('http://127.0.0.1:18086/api/alerts/compression/'+result['scan_id']+'/edit',
                                  data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
                with urlopen(request, timeout=30) as response: edited = json.load(response)
                assert edited['raw_alerts'] == result['raw_alerts']
                assert len(edited['groups']) == len(result['groups'])-1
                assert edited['merge_review']['status'] == 'invalidated_by_edit'
                assert not edited['merge_review']['suggestions']
                assert edited['revision'] == result['revision']+1
                (out/(stage+'-review-edit.json')).write_text(json.dumps(edited,ensure_ascii=False))
                summary['explicit_review_workflow_passed'] = True
        return result
    try:
        production_before=get('http://127.0.0.1:8080/api/alerts/lifecycle')
        # Expectations fixed before any observation; two degraded services plus independent failure.
        (out/'plan.json').write_text(json.dumps(dict(thresholds_ms=[100,200,300],time_window=30,
            maximum_stage_raw_alerts=9,desired_event_groups=3,desired_pair_recall=.95,
            required_raw_retention=1,required_critical_retention=1,allowed_cross_object_merges=0)))
        rules=[{'alert':name,'expr':f'agenticsre_probe_duration_ms > {threshold}','labels':{'severity':severity}}
               for threshold,name,severity in [(100,'ProbeLatencyElevated','warning'),(200,'ProbeLatencyHigh','warning'),(300,'ProbeLatencyCritical','critical')]]
        rules += [{'alert':'ProbeRequestFailed','expr':'agenticsre_probe_last_request_failed{instance!="127.0.0.1:18103"} > 0','labels':{'severity':'warning'}},
                  {'alert':'ProbeIndependentCritical','expr':'agenticsre_probe_last_request_failed{instance="127.0.0.1:18103"} > 0','labels':{'severity':'critical'}}]
        (out/'rules.yaml').write_text(yaml.safe_dump({'groups':[{'name':'threshold-test','interval':'1s','rules':rules}]}))
        (out/'prometheus.yaml').write_text(yaml.safe_dump({'global':{'scrape_interval':'1s','evaluation_interval':'1s'},
            'rule_files':['/probe/rules.yaml'],'scrape_configs':[{'job_name':'threshold-test','static_configs':[{'targets':[f'127.0.0.1:{p}' for p in profiles]}]}]}))
        for suffix,args in [('exporter',['-e','PROBE_NAMESPACE='+namespace,'-v',str(ROOT/'threshold_probe_exporter.py')+':/probe.py:ro','python:3.12-slim','python','/probe.py']),
            ('prometheus',['-v',str(out)+':/probe:ro','-v','/home/ubuntu/AgenticSRE-releases/livemetrics-20260916/prometheus-probe:/probe-prometheus:ro','--entrypoint','/probe-prometheus',IMAGE,'--config.file=/probe/prometheus.yaml','--web.listen-address=127.0.0.1:18100','--storage.tsdb.retention.time=1h'])]:
            name='agenticsre-threshold-'+suffix+'-'+token
            run('docker','run','-d','--name',name,'--label','agenticsre.experiment='+label,'--network','host',*args);created.append(name)
        original=yaml.safe_load(Path('/home/ubuntu/AgenticSRE-releases/metricui-20260917/docker-compose.production.yaml').read_text())
        service=original['services']['agenticsre'];service.pop('depends_on',None);service.pop('build',None)
        service['image']=IMAGE;service['container_name']='agenticsre-threshold-app-'+token
        service['labels']={'agenticsre.experiment':label}
        service['command']=['python','-m','uvicorn','web_app.app:app','--host','127.0.0.1','--port','18086']
        service['healthcheck']['test']=['CMD','curl','-f','http://127.0.0.1:18086/api/health']
        (out/'data').mkdir();(out/'logs').mkdir()
        platform=json.loads(run('sudo','cat','/home/ubuntu/agenticsre/data/platform_config.json'))
        platform.setdefault('observability',{})['prometheus_url']='http://127.0.0.1:18100'
        f=out/'data/platform_config.json';f.write_text(json.dumps(platform));f.chmod(0o600)
        volumes=[]
        for volume in service['volumes']:
            src,dst,*mode=volume.split(':')
            if dst in ('/app/data','/app/logs'):src=str(out/dst.rsplit('/',1)[1])
            if dst in ('/app/configs/config.yaml','/app/configs/clusters.yaml'):
                cfg=yaml.safe_load(Path(src).read_text())
                def update(v):
                    if isinstance(v,dict):
                        for k,x in v.items():
                            if k=='prometheus_url':v[k]='http://127.0.0.1:18100'
                            else:update(x)
                    elif isinstance(v,list):
                        for x in v:update(x)
                update(cfg)
                if dst.endswith('/config.yaml'):cfg.setdefault('alert',{})['time_window']=30
                f=out/Path(dst).name;f.write_text(yaml.safe_dump(cfg));f.chmod(0o600);src=str(f)
            volumes.append(':'.join([src,dst]+mode))
        assert not any(v.startswith('/home/ubuntu/agenticsre/data:') or v.startswith('/home/ubuntu/agenticsre/logs:') for v in volumes)
        service['volumes']=volumes
        compose=out/'compose.yaml';compose.write_text(yaml.safe_dump({'services':{'agenticsre':service}}));compose.chmod(0o600)
        created.append(service['container_name'])
        run('docker','compose','-p','threshold-'+token,'-f',str(compose),'up','-d','--no-build','--pull','never')
        for _ in range(60):
            try:
                if get('http://127.0.0.1:18086/api/health',3)['status']=='ok':break
            except Exception:pass
            time.sleep(1)
        else:raise AssertionError('isolated app not ready')
        wait_firing(0)
        for port in profiles:threading.Thread(target=drive,args=(port,),daemon=True).start()
        profiles[18101]=(150,False);scan('one-threshold',1)
        profiles[18101]=(250,False);scan('two-thresholds',2)
        profiles[18101]=(350,True);profiles[18102]=(350,True);profiles[18103]=(0,True)
        time.sleep(35)
        peak=scan('peak-independent-objects',9)
        assert peak['metric_evidence'].get('source_ready_groups',peak['metric_evidence']['ready_groups'])==9
        merged=[g for g in peak['groups'] if g.get('threshold_merge')]
        report['nested_threshold_target']=dict(expected_groups=5,actual_groups=len(peak['groups']),
            passed=len(peak['groups'])==5 and len(merged)==2 and all(g['alert_count']==3 and g['severity']=='critical' for g in merged))
        from math import comb
        expected_pairs=2*comb(4,2)
        found=sum(comb(len(g['alert_indices']),2) for g in peak['groups'])
        report['event_compression_target']=dict(expected_groups=3,actual_groups=len(peak['groups']),pair_recall=found/expected_pairs,
            passed=len(peak['groups'])==3 and found/expected_pairs>=.95,
            interpretation='Grouping goal only; proposed semantic relations are not compressed events.')
        for port in profiles:profiles[port]=(0,False)
        scan('recovered-first',0);time.sleep(35);scan('recovered-confirm',0)
        time.sleep(35);profiles[18101]=(350,True);time.sleep(5)
        recurrent=scan('new-episode',4)
        before_ids={s['incident_id']:s['episode'] for g in peak['groups'] for s in g.get('lifecycle',[])}
        recurring=[s for g in recurrent['groups'] for s in g.get('lifecycle',[]) if s['incident_id'] in before_ids]
        report['new_episode_separated']=len(recurring)==4 and all(s['episode']>before_ids[s['incident_id']] for s in recurring)
        if os.environ.get('AGENTICSRE_REVIEW_NEGATIVE_ACCEPTANCE') == '1':
            # Independent controls on the same object: latency and errors alternate.
            # Finish with both active so the scan sees the same four alert rules,
            # while measured histories disagree within its 30-second window.
            for _ in range(6):
                profiles[18101]=(350,False);time.sleep(3)
                profiles[18101]=(0,True);time.sleep(3)
            profiles[18101]=(350,True);time.sleep(3)
            independent=scan('same-object-independent-controls',4)
            assert not independent.get('merge_review', {}).get('suggestions')
            statistics=[e['checks']['statistics'] for e in independent['scag']['edges']]
            assert any(s.get('status')=='fail' for s in statistics),statistics
            report['same_object_negative']=dict(passed=True,groups=len(independent['groups']),
                suggestions=0,statistics=[{k:s[k] for k in ('status','reason','pearson') if k in s} for s in statistics])
        for port in profiles:profiles[port]=(0,False)
        wait_firing(0)
        report['production_lifecycle_unchanged']=production_before==get('http://127.0.0.1:8080/api/alerts/lifecycle')
        report['model_validation_complete']=all(s.get('semantic',{}).get('coverage_complete') for s in report['stages'] if s['firing']>1)
        report['status']='completed'
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__,detail=str(exc)[:500]);raise
    finally:
        stop.set();(out/'traffic.json').write_text(json.dumps(traffic))
        removed=[]
        for name in reversed(created):
            owner=subprocess.run(['docker','inspect',name,'--format','{{index .Config.Labels "agenticsre.experiment"}}'],capture_output=True,text=True)
            if owner.returncode==0 and owner.stdout.strip()==label:
                run('docker','rm','-f',name);removed.append(name)
        report['owned_containers_removed']=removed
        for relative in ('config.yaml','clusters.yaml','compose.yaml','data/platform_config.json'):
            (out/relative).unlink(missing_ok=True)
        report['temporary_config_copies_removed']=True
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'output':str(out),'report':report},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
