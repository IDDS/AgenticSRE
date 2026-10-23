"""Promote only the fully tested image; preserve configuration and support rollback."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib.request import urlopen

ROOT = Path('/home/ubuntu/AgenticSRE-releases/release-20261022')
OLD = Path('/home/ubuntu/AgenticSRE-releases/pure-agent-release-20260924-r4')
PREVIOUS = 'sha256:b72b480ca40294ff089272519a546f7af2291640312398aca1d0f59130fca80d'
CANDIDATE = 'sha256:5879e64ef6c43e0397302d803e0f2f5f99825346abc6ef0bbfef10fb85dfd1b0'
PROFILE = 'all-MiniLM-L6-v2'
SERVICES = ['agenticsre-skill-runtime', 'agenticsre']
COMPOSE = ['docker','compose','-p','agenticsre','--project-directory',str(OLD),
           '-f',str(OLD/'docker-compose.production.yaml'),'-f',str(OLD/'docker-compose.skill-production.yaml')]
EXPECTED = json.loads((ROOT/'source-sha256.json').read_text())


def save(name, data):
    (ROOT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2))


def get(port, path):
    with urlopen(f'http://127.0.0.1:{port}{path}',timeout=30) as response:
        return json.load(response)


def inspect(names):
    return {x['Name'].lstrip('/'):x for x in json.loads(subprocess.check_output(['docker','inspect',*names]))}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def fingerprint(item):
    env = dict(value.split('=', 1) for value in item['Config']['Env'])
    # Preserve the effective production model instead of inheriting the
    # research image's hybrid default. Other environment values must match.
    assert env.get('AGENTICSRE_ALERT_EMBEDDING_MODEL', PROFILE) == PROFILE
    env['AGENTICSRE_ALERT_EMBEDDING_MODEL'] = PROFILE
    return digest({'env':sorted(env.items()),'cmd':item['Config']['Cmd'],
        'entrypoint':item['Config']['Entrypoint'],'network':item['HostConfig']['NetworkMode'],
        'restart':item['HostConfig']['RestartPolicy'],
        'healthcheck':item['Config'].get('Healthcheck'),
        'ports':item['HostConfig'].get('PortBindings'),
        'mounts':sorted((m['Source'],m['Destination'],m['RW']) for m in item['Mounts'])})


def config_hashes(items):
    paths=set()
    for item in items.values():
        for m in item['Mounts']:
            source=Path(m['Source'])
            if m['Destination'].startswith('/app/configs/'):
                paths.update(p for p in source.rglob('*') if p.is_file()) if source.is_dir() else paths.add(source)
            if m['Destination']=='/app/data':
                paths.update(source/name for name in ('platform_config.json','cluster_profiles.json'))
    result={}
    for path in sorted(paths):
        if not path.is_file(): continue
        try: value=hashlib.sha256(path.read_bytes()).hexdigest()
        except PermissionError: value=subprocess.check_output(['sudo','-n','sha256sum',str(path)],text=True).split()[0]
        result[str(path)]=value
    return result


def snapshot():
    rules=get(8080,'/api/knowledge/rules')['rules']
    for rule in rules: rule.pop('revision',None)
    recipes=get(8080,'/api/heal/recipes')['recipes']
    lifecycle=get(8080,'/api/alerts/lifecycle')
    return {'rules_count':len(rules),'rules_sha256':digest(sorted(rules,key=lambda x:x['rule_id'])),
            'recipes_count':len(recipes),'recipes_sha256':digest(recipes),
            'faults_count':get(8080,'/api/knowledge/stats')['faults_count'],
            'lifecycle_sha256':digest(lifecycle),'history_files':history_files(),'heal_sha256':digest(get(8080,'/api/heal/runs?limit=200')['runs'])}


def sources(target, image=False):
    code='import hashlib,json,sys;from pathlib import Path;paths=json.load(sys.stdin);print(json.dumps({p:hashlib.sha256((Path("/app")/p).read_bytes()).hexdigest() for p in paths}))'
    cmd=(['docker','run','--rm','-i','--network','none','--entrypoint','python',target,'-c',code] if image
         else ['docker','exec','-i',target,'python','-c',code])
    actual=json.loads(subprocess.check_output(cmd,input=json.dumps(list(EXPECTED)).encode()))
    assert actual==EXPECTED, 'source hashes differ'


def wait_healthy(name):
    for _ in range(40):
        if inspect([name])[name]['State'].get('Health',{}).get('Status')=='healthy': return
        time.sleep(3)
    raise RuntimeError(name+' health timeout')



def history_files():
    # Hash only persisted history/scan content, never publish content or env.
    code = """import json,hashlib;from pathlib import Path
root=Path('/app/data');paths=[root/'rca_history.json',*(root/'alert_compression').rglob('scan-*.json')]
print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}))"""
    return json.loads(subprocess.check_output(['docker','exec','agenticsre','python','-c',code]))


def quiet_run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Compose may include interpolated credentials in errors. Report only code.
        raise RuntimeError('deployment command failed with exit code '+str(result.returncode))


def evidence():
    validation = Path('/home/ubuntu/AgenticSRE-releases/alert-edit-resume-20261022')
    proof = json.loads((validation/'test-verification.json').read_text())
    assert proof['status'] == 'passed' and proof['source_verified']
    assert all(stage['exit_code'] == 0 for stage in proof['stages'])
    assert '1867 passed, 7 warnings' in (validation/'full.log').read_text()
    for name in ('browser.json','restart.json'):
        assert json.loads((validation/name).read_text())['passed']
    frozen = json.loads((validation/'identity.json').read_text())
    assert frozen['image'] == CANDIDATE and frozen['matches_local'] and frozen['matches_test_source']
    identity = json.loads((ROOT/'release-identity.json').read_text())
    assert re.fullmatch(r'[0-9a-f]{40}', identity['commit'])
    assert identity['image'] == CANDIDATE
    assert identity['manifest_sha256'] == hashlib.sha256((ROOT/'source-sha256.json').read_bytes()).hexdigest()
    assert len(EXPECTED) == 209
    return identity


def main():
    started = time.monotonic()
    identity = evidence()
    candidate = subprocess.check_output(['docker','image','inspect',
        'agenticsre:alert-edit-resume-20261022','--format','{{.Id}}'],text=True).strip()
    assert candidate == CANDIDATE
    sources(candidate, image=True)
    names = subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    before = inspect(names)
    assert all(before[name]['Image'] == PREVIOUS for name in SERVICES)
    assert not any(row['status'] in ('queued','starting','running')
        for row in get(8080,'/api/rca/history?limit=100')['runs'])
    assert not get(8080,'/api/daemon/status')['running']
    code = """import json,sqlite3;from pathlib import Path
p=Path('/app/data/alert_compression/jobs/jobs.sqlite3')
with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as db:
 print(json.dumps(db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]))"""
    assert json.loads(subprocess.check_output(['docker','exec','agenticsre','python','-c',code])) == 0
    for port in (8080,8090): assert get(port,'/api/health')['status'] == 'ok'
    # Retain the complete current Compose chain; append only pinned images.
    labels = before['agenticsre']['Config']['Labels']
    assert labels['com.docker.compose.project'] == 'agenticsre'
    compose = ['docker','compose','-p','agenticsre','--project-directory',
        labels['com.docker.compose.project.working_dir']]
    for path in labels['com.docker.compose.project.config_files'].split(','):
        assert Path(path).is_file()
        compose.extend(['-f',path])
    for name, port in (('agenticsre',8080),('agenticsre-skill-runtime',8090)):
        assert before[name]['State']['Health']['Status'] == 'healthy'
        assert before[name]['Config']['Healthcheck']['Test'] == ['CMD','curl','-f',f'http://localhost:{port}/api/health']
    data_before = snapshot()
    config_before = config_hashes({name:before[name] for name in SERVICES})
    save('before.json', {'containers':{name:{'id':item['Id'],'image':item['Image'],
        'fingerprint':fingerprint(item)} for name,item in before.items()},
        'data':data_before,'config_hashes':config_before,'candidate':candidate})
    overrides=[]
    for label,image in [('candidate',candidate),('rollback',PREVIOUS)]:
        path=ROOT/(label+'-override.yaml')
        path.write_text('services:\n'+''.join(f'  {name}:\n    image: {image}\n'
            f'    environment:\n      AGENTICSRE_ALERT_EMBEDDING_MODEL: {PROFILE}\n' for name in SERVICES))
        quiet_run([*compose,'-f',str(path),'config','--quiet'])
        overrides.append(path)
    save('preflight.json',dict(passed=True,image=candidate,commit=identity['commit'],
        tests_passed=1867,source_files=len(EXPECTED),data=data_before))
    if '--preflight' in sys.argv:
        print(json.dumps(dict(preflight=True,image=candidate,commit=identity['commit'],files_verified=len(EXPECTED))))
        return
    try:
        for name in SERVICES:
            quiet_run([*compose,'-f',str(overrides[0]),'up','-d','--no-deps','--no-build','--pull','never',name])
            wait_healthy(name)
            sources(name)
            item=inspect([name])[name]
            assert item['Image']==candidate and fingerprint(item)==fingerprint(before[name]), 'runtime fingerprint changed'
            print(name+' healthy; all source, configuration and healthcheck hashes verified',flush=True)
        assert snapshot()==data_before, 'persistent content changed'
        after=inspect(names)
        assert config_hashes({name:after[name] for name in SERVICES})==config_before
        assert all(after[name]['Id']==before[name]['Id'] and after[name]['Image']==before[name]['Image']
            for name in names if name not in SERVICES)
        caps=get(8080,'/api/hermes/capabilities')
        assert caps['status']=='ready' and caps['skill_runtime']['skill_count']==62
        recovered=get(8080,'/api/heal/runs?filter=recovered&limit=10')
        assert recovered['filtered_total']==recovered['summary']['verified_recovered']
        assert recovered['runs'] and all(row['verification']['recovered'] is True for row in recovered['runs'])
        for port in (8080,8090): assert get(port,'/api/health')['status']=='ok'
        report=dict(deployed=True,verified=True,rolled_back=False,image=candidate,previous_image=PREVIOUS,
            commit=identity['commit'],embedding_profile=PROFILE,files_verified=len(EXPECTED),
            configuration_preserved=True,data_preserved=True,healthcheck_preserved=True,
            other_containers_unchanged=True,hermes_ready=True,recovered_count=recovered['filtered_total'],
            elapsed_s=round(time.monotonic()-started,3))
        save('deployment-result.json',report)
        print(json.dumps(report),flush=True)
    except Exception as exc:
        quiet_run([*compose,'-f',str(overrides[1]),'up','-d','--no-deps','--no-build','--pull','never',*SERVICES])
        for name in SERVICES:
            wait_healthy(name)
            item=inspect([name])[name]
            assert item['Image']==PREVIOUS and fingerprint(item)==fingerprint(before[name])
        save('deployment-result.json',dict(deployed=False,rolled_back=True,error_type=type(exc).__name__))
        raise


if __name__ == '__main__':
    main()
