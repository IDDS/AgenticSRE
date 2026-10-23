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

ROOT = Path('/home/ubuntu/AgenticSRE-releases/alert-grounding-release-20261016')
OLD = Path('/home/ubuntu/AgenticSRE-releases/pure-agent-release-20260924-r4')
PREVIOUS = 'sha256:cefa5ebf8ae6df50143a5fd10bf5aa1329d74ad22b2e5291e9a644b1165dd3a1'
CANDIDATE = 'sha256:806c44ada1056c6c2f3eab21666a1ebf5bf5a72c42bcf92acc2f5c8ef181ea91'
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
            'lifecycle_sha256':digest(lifecycle),'heal_sha256':digest(get(8080,'/api/heal/runs?limit=200')['runs'])}


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


def main():
    started=time.monotonic()
    validation = json.loads((ROOT/'validation-result.json').read_text())
    assert validation['passed'] and validation['image'] == CANDIDATE
    assert validation['embedding_profile'] == PROFILE
    assert validation['manifest_sha256'] == hashlib.sha256((ROOT/'source-sha256.json').read_bytes()).hexdigest()
    suites = ET.parse(ROOT/'full-production-profile.xml').getroot().iter('testsuite')
    counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
    for suite in suites:
        for key in counts: counts[key] += int(suite.get(key, 0))
    assert counts == dict(tests=1387, failures=0, errors=0, skipped=0), counts
    browser=json.loads((ROOT/'browser-results.json').read_text())
    assert browser.get('status') == 'passed' and not browser['page_errors']
    candidate=subprocess.check_output(['docker','image','inspect','agenticsre:alert-grounding-20261016-r4','--format','{{.Id}}'],text=True).strip()
    assert candidate == CANDIDATE, 'candidate tag no longer matches tested image'
    sources(candidate,image=True)
    names=subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    before=inspect(names)
    assert all(before[n]['Image']==PREVIOUS for n in SERVICES)
    assert not any(x['status'] in ('queued','starting','running') for x in get(8080,'/api/rca/history?limit=100000')['runs'])
    assert not get(8080,'/api/daemon/status')['running'], 'automatic daemon active'
    jobs=Path('/home/ubuntu/agenticsre/data/alert_compression/jobs/jobs.sqlite3')
    if jobs.exists():
        with sqlite3.connect(f'file:{jobs}?mode=ro',uri=True) as db:
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]==0, 'active alert jobs'
    for port in (8080,8090): assert get(port,'/api/health')['status']=='ok'
    data_before=snapshot()
    config_before=config_hashes({n:before[n] for n in SERVICES})
    save('before.json',{'containers':{n:{'id':v['Id'],'image':v['Image'],'fingerprint':fingerprint(v)} for n,v in before.items()},
        'data':data_before,'config_hashes':config_before,'candidate':candidate})
    overrides=[]
    for label,image in [('candidate',candidate),('rollback',PREVIOUS)]:
        path=ROOT/(label+'-override.yaml')
        profile = f'    environment:\n      AGENTICSRE_ALERT_EMBEDDING_MODEL: {PROFILE}\n'
        path.write_text('services:\n'+''.join(f'  {n}:\n    image: {image}\n{profile}' for n in SERVICES))
        subprocess.run([*COMPOSE,'-f',str(path),'config','--quiet'],stdout=subprocess.DEVNULL,check=True)
        overrides.append(path)
    if '--preflight' in sys.argv:
        print(json.dumps({'preflight':True,'candidate':candidate,'data':data_before})); return
    identity=json.loads((ROOT/'release-identity.json').read_text())
    assert re.fullmatch(r'[0-9a-f]{40}', identity['commit'])
    assert identity['image'] == candidate
    try:
        for name in SERVICES:
            subprocess.run([*COMPOSE,'-f',str(overrides[0]),'up','-d','--no-deps','--no-build','--pull','never',name],check=True)
            wait_healthy(name); sources(name)
            item=inspect([name])[name]
            assert item['Image']==candidate and fingerprint(item)==fingerprint(before[name])
            print(name+' healthy; source and runtime configuration verified',flush=True)
        assert snapshot()==data_before, 'persistent content changed'
        after=inspect(names)
        assert config_hashes({n:after[n] for n in SERVICES})==config_before
        for name in names:
            if name not in SERVICES: assert after[name]['Id']==before[name]['Id'] and after[name]['Image']==before[name]['Image']
        capabilities=get(8080,'/api/hermes/capabilities')
        assert capabilities['status']=='ready' and capabilities['skill_runtime']['skill_count']==62
        recovered=get(8080,'/api/heal/runs?filter=recovered&limit=10')
        assert recovered['filtered_total']==recovered['summary']['verified_recovered']
        assert recovered['runs'] and all(r['verification']['recovered'] is True for r in recovered['runs'])
        assert get(8090,'/api/health')['status']=='ok'
        report={'deployed':True,'verified':True,'rolled_back':False,'image':candidate,'previous_image':PREVIOUS,
                'commit':identity['commit'],'embedding_profile':PROFILE,
                'source_manifest_sha256':digest(EXPECTED),'files_verified':len(EXPECTED),
                'configuration_preserved':True,'data_preserved':True,'other_containers_unchanged':True,
                'hermes_ready':True,'recovered_count':recovered['filtered_total'],
                'elapsed_s':round(time.monotonic()-started,3)}
        save('deployment-result.json',report); print(json.dumps(report),flush=True)
    except Exception as exc:
        subprocess.run([*COMPOSE,'-f',str(overrides[1]),'up','-d','--no-deps','--no-build','--pull','never',*SERVICES],check=True)
        for name in SERVICES:
            wait_healthy(name)
            item = inspect([name])[name]
            assert item['Image']==PREVIOUS and fingerprint(item)==fingerprint(before[name])
        save('deployment-result.json',{'deployed':False,'rolled_back':True,'error_type':type(exc).__name__})
        raise


if __name__=='__main__': main()
