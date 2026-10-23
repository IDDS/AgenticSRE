"""Remote-only opt-in real Prometheus rules -> live scan -> metric evidence acceptance."""
import json,os,subprocess,time,uuid
from pathlib import Path
from urllib.request import urlopen
import yaml

ROOT=Path('/home/ubuntu/AgenticSRE-releases/livemetrics-20260916')

def main():
    if os.environ.get('AGENTICSRE_RUN_LIVE_METRICS')!='1':raise SystemExit('explicit opt-in required')
    namespace='agenticsre-metrics-'+uuid.uuid4().hex[:8]
    out=ROOT/os.environ.get('AGENTICSRE_METRIC_OUTPUT','live-results');out.mkdir(exist_ok=True)
    containers=['agenticsre-metric-probe-'+namespace[-8:],'agenticsre-metric-prometheus-'+namespace[-8:]]
    created=[]
    def run(*args):
        process=subprocess.run(args,capture_output=True,text=True)
        if process.returncode:
            raise RuntimeError('command failed: '+str(args[:3])+' '+process.stderr[:500])
        return process.stdout
    def get(url):
        with urlopen(url,timeout=180) as r:return json.load(r)
    def request(port,path):
        with urlopen(f'http://127.0.0.1:{port}'+path,timeout=5) as r:return r.read()
    original=(ROOT/'docker-compose.canary.yaml').read_text()
    report={'status':'running','namespace':namespace}
    try:
        for name in containers:
            assert subprocess.run(['docker','inspect',name],capture_output=True).returncode!=0,'probe name already in use'
        prom={'global':{'scrape_interval':'1s','evaluation_interval':'1s'},'rule_files':['/etc/prometheus/probe-rules.yaml'],
              'scrape_configs':[{'job_name':'live-metric-probe','static_configs':[{'targets':['127.0.0.1:18101','127.0.0.1:18102']}]}]}
        rules={'groups':[{'name':'probe','interval':'1s','rules':[{'alert':'ProbeRequestsHigh','expr':'agenticsre_probe_requests_total > 2','labels':{'severity':'warning'}}]}]}
        (ROOT/'probe-prometheus.yaml').write_text(yaml.safe_dump(prom));(ROOT/'probe-rules.yaml').write_text(yaml.safe_dump(rules))
        run('docker','run','-d','--name',containers[0],'--label','agenticsre.experiment=live-metrics','--network','host',
            '-e','PROBE_NAMESPACE='+namespace,'-v',str(ROOT/'live_metric_exporter.py')+':/probe.py:ro','python:3.12-slim','python','/probe.py')
        created.append(containers[0])
        run('docker','run','-d','--name',containers[1],'--label','agenticsre.experiment=live-metrics','--network','host',
            '-v',str(ROOT/'probe-prometheus.yaml')+':/etc/prometheus/prometheus.yml:ro',
            '-v',str(ROOT/'probe-rules.yaml')+':/etc/prometheus/probe-rules.yaml:ro',
            '-v',str(ROOT/'prometheus-probe')+':/probe-prometheus:ro','--entrypoint','/probe-prometheus','agenticsre:livemetrics-20260916',
            '--config.file=/etc/prometheus/prometheus.yml','--web.listen-address=127.0.0.1:18100','--storage.tsdb.retention.time=1h')
        created.append(containers[1])
        data=yaml.safe_load(original)
        for key,service in data['services'].items():
            service.setdefault('environment',{})['LLM_API_KEY']=''
            volumes=[]
            for volume in service['volumes']:
                src,dst,*mode=volume.split(':')
                if dst in ('/app/configs/config.yaml','/app/configs/clusters.yaml'):
                    cfg=yaml.safe_load(Path(src).read_text())
                    def update(value):
                        if isinstance(value,dict):
                            for k,v in value.items():
                                if k=='prometheus_url':value[k]='http://127.0.0.1:18100'
                                else:update(v)
                        elif isinstance(value,list):
                            for v in value:update(v)
                    update(cfg)
                    if dst.endswith('/config.yaml'):
                        cfg.setdefault('alert',{})['time_window']=30
                        cfg.setdefault('llm',{})['api_key']=''
                        cfg.setdefault('observability',{})['prometheus_url']='http://127.0.0.1:18100'
                    path=ROOT/('probe-'+Path(dst).name);path.write_text(yaml.safe_dump(cfg));path.chmod(0o600)
                    src=str(path)
                volumes.append(':'.join([src,dst]+mode))
            service['volumes']=volumes
        platform=ROOT/'canary-data/platform_config.json'
        backup=run('sudo','cat',str(platform))
        p=json.loads(backup);p.setdefault('observability',{})['prometheus_url']='http://127.0.0.1:18100';p.pop('llm',None)
        temp=ROOT/'probe-platform.json';temp.write_text(json.dumps(p));temp.chmod(0o600)
        run('sudo','cp',str(temp),str(platform))
        (ROOT/'docker-compose.canary.yaml').write_text(yaml.safe_dump(data))
        run('docker','compose','-p','agenticsre-livemetrics-canary','-f',str(ROOT/'docker-compose.canary.yaml'),'up','-d','--no-build','--pull','never')
        for _ in range(60):
            try:
                if get('http://127.0.0.1:18086/api/health')['status']=='ok':break
            except Exception:pass
            time.sleep(1)
        else:raise AssertionError('canary not ready')
        before=get('http://127.0.0.1:18100/api/v1/query?query=ALERTS')
        assert not before['data']['result'],before
        for _ in range(45):
            for port in (18101,18102):request(port,'/probe')
            time.sleep(1)
        firing=get('http://127.0.0.1:18100/api/v1/query?query=ALERTS%7Balertstate%3D%22firing%22%7D')
        assert len(firing['data']['result'])==2,firing
        (out/'firing-alerts.json').write_text(json.dumps(firing,indent=2))
        scan=get('http://127.0.0.1:18086/api/alerts/scan?cluster_id=default&namespace='+namespace)
        (out/'scan.json').write_text(json.dumps(scan,indent=2))
        assert scan['origin']=='live_scan' and len(scan['raw_alerts'])==2,scan.get('collection')
        evidence=scan['metric_evidence'];assert evidence['ready_groups']==2,evidence
        assert all(v['query_basis']=='simple_comparison_operand' for v in evidence['groups'].values())
        assert all(min(float(v[1]) for v in e['raw_values'])>=0 for e in evidence['groups'].values())
        edges=scan['scag']['edges'];assert len(edges)==1,edges
        assert edges[0]['checks']['statistics']['status']=='pass',edges
        assert not edges[0]['causal_confirmed']
        saved=get('http://127.0.0.1:18086/api/alerts/compression/'+scan['scan_id'])
        assert saved['metric_evidence']==evidence and saved['raw_alerts']==scan['raw_alerts']
        for port in (18101,18102):request(port,'/reset')
        time.sleep(3)
        assert not get('http://127.0.0.1:18100/api/v1/query?query=ALERTS')['data']['result']
        report.update(status='passed',firing_alerts=2,raw_alerts_preserved=2,metric_groups=2,
            samples=[len(e['points']) for e in evidence['groups'].values()],statistics=edges[0]['checks']['statistics']['status'],
            pearson=edges[0]['checks']['statistics']['pearson'],scan_id=scan['scan_id'],rule_cleared_after_counter_reset=True)
    except Exception as exc:
        logs=subprocess.run(['docker','logs','--tail','150','agenticsre-livemetrics-canary'],capture_output=True,text=True)
        (out/'canary-error.log').write_text(logs.stdout+logs.stderr)
        report.update(status='failed',error=type(exc).__name__);raise
    finally:
        (ROOT/'docker-compose.canary.yaml').write_text(original)
        run('sudo','cp','/home/ubuntu/agenticsre/data/platform_config.json',str(ROOT/'canary-data/platform_config.json'))
        run('docker','compose','-p','agenticsre-livemetrics-canary','-f',str(ROOT/'docker-compose.canary.yaml'),'up','-d','--no-build','--pull','never')
        removed=[]
        for name in created:
            owner=subprocess.run(['docker','inspect',name,'--format','{{index .Config.Labels "agenticsre.experiment"}}'],capture_output=True,text=True)
            if owner.returncode==0 and owner.stdout.strip()=='live-metrics':
                run('docker','rm','-f',name);removed.append(name)
        report['owned_containers_removed']=removed
        (out/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)

if __name__=='__main__':main()
