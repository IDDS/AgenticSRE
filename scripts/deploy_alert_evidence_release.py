"""Promote the tested evidence-reader image on snail with preserved state.

Reuses the previous release's configuration, source and data checks. Run with
--preflight before promotion; no builds or dependency installation are needed.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import deploy_alert_grounding_release as release

ROOT = Path('/home/ubuntu/AgenticSRE-releases/alert-evidence-release-20261017')
VALIDATION = Path('/home/ubuntu/AgenticSRE-releases/alert-evidence-read-20261016')
PREVIOUS = 'sha256:806c44ada1056c6c2f3eab21666a1ebf5bf5a72c42bcf92acc2f5c8ef181ea91'
CANDIDATE = 'sha256:b72b480ca40294ff089272519a546f7af2291640312398aca1d0f59130fca80d'
TAG = 'agenticsre:alert-evidence-read-20261016-r6'
release.ROOT = ROOT
release.EXPECTED = json.loads((VALIDATION/'r6/source-sha256.json').read_text())


def main():
    started = time.monotonic()
    ROOT.mkdir(parents=True, exist_ok=True)
    identity = json.loads((ROOT/'release-identity.json').read_text())
    assert re.fullmatch(r'[0-9a-f]{40}', identity['commit'])
    assert identity['image'] == CANDIDATE
    assert identity['manifest_sha256'] == hashlib.sha256((VALIDATION/'r6/source-sha256.json').read_bytes()).hexdigest()
    counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
    for suite in ET.parse(VALIDATION/'r6/full.xml').getroot().iter('testsuite'):
        for key in counts: counts[key] += int(suite.get(key, 0))
    assert counts == dict(tests=1438, failures=0, errors=0, skipped=0), counts
    assert len(release.EXPECTED) == 396
    summary = json.loads((VALIDATION/'summary-r6.json').read_text())
    assert len(summary['cases']) == 3
    assert all(all(case['checks'].values()) and case['actual_context_contains_rare']
               and case['reviewer_received_resolved_selections'] for case in summary['cases'])
    assert summary['browser']['passed'] and summary['browser']['original_link_passed']
    assert all(phase['passed'] for phase in summary['load']['phases'])
    assert summary['resources_cleaned'] and summary['production_lifecycle_unchanged']
    candidate = subprocess.check_output(['docker','image','inspect',TAG,'--format','{{.Id}}'],text=True).strip()
    assert candidate == CANDIDATE
    release.sources(candidate, image=True)
    names = subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    before = release.inspect(names)
    assert all(before[name]['Image'] == PREVIOUS for name in release.SERVICES)
    assert not any(row['status'] in ('queued','starting','running')
                   for row in release.get(8080,'/api/rca/history?limit=100000')['runs'])
    assert not release.get(8080,'/api/daemon/status')['running']
    jobs = Path('/home/ubuntu/agenticsre/data/alert_compression/jobs/jobs.sqlite3')
    if jobs.exists():
        with sqlite3.connect(f'file:{jobs}?mode=ro', uri=True) as db:
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0
    for port in (8080,8090): assert release.get(port,'/api/health')['status'] == 'ok'
    data_before = release.snapshot()
    config_before = release.config_hashes({name:before[name] for name in release.SERVICES})
    release.save('before.json', {'containers':{name:{'id':item['Id'],'image':item['Image'],
        'fingerprint':release.fingerprint(item)} for name,item in before.items()},
        'data':data_before,'config_hashes':config_before,'candidate':candidate})
    overrides = []
    for label,image in [('candidate',candidate),('rollback',PREVIOUS)]:
        path = ROOT/(label+'-override.yaml')
        path.write_text('services:\n'+''.join(f'  {name}:\n    image: {image}\n'
            f'    environment:\n      AGENTICSRE_ALERT_EMBEDDING_MODEL: {release.PROFILE}\n'
            for name in release.SERVICES))
        subprocess.run([*release.COMPOSE,'-f',str(path),'config','--quiet'],stdout=subprocess.DEVNULL,check=True)
        overrides.append(path)
    release.save('preflight.json', dict(passed=True,image=candidate,commit=identity['commit'],
        tests=counts,source_files=len(release.EXPECTED),data=data_before))
    if '--preflight' in sys.argv:
        print(json.dumps(dict(preflight=True,image=candidate,data=data_before))); return
    try:
        for name in release.SERVICES:
            subprocess.run([*release.COMPOSE,'-f',str(overrides[0]),'up','-d',
                            '--no-deps','--no-build','--pull','never',name],check=True)
            release.wait_healthy(name)
            release.sources(name)
            item = release.inspect([name])[name]
            assert item['Image'] == candidate and release.fingerprint(item) == release.fingerprint(before[name])
            print(name+' healthy; source and runtime configuration verified',flush=True)
        assert release.snapshot() == data_before
        after = release.inspect(names)
        assert release.config_hashes({name:after[name] for name in release.SERVICES}) == config_before
        assert all(after[name]['Id'] == before[name]['Id'] and after[name]['Image'] == before[name]['Image']
                   for name in names if name not in release.SERVICES)
        caps = release.get(8080,'/api/hermes/capabilities')
        assert caps['status'] == 'ready' and caps['skill_runtime']['skill_count'] == 62
        recovered = release.get(8080,'/api/heal/runs?filter=recovered&limit=10')
        assert recovered['filtered_total'] == recovered['summary']['verified_recovered']
        assert recovered['runs'] and all(row['verification']['recovered'] is True for row in recovered['runs'])
        for port in (8080,8090): assert release.get(port,'/api/health')['status'] == 'ok'
        report = dict(deployed=True,verified=True,rolled_back=False,image=candidate,previous_image=PREVIOUS,
            commit=identity['commit'],embedding_profile=release.PROFILE,files_verified=len(release.EXPECTED),
            configuration_preserved=True,data_preserved=True,other_containers_unchanged=True,
            hermes_ready=True,recovered_count=recovered['filtered_total'],elapsed_s=round(time.monotonic()-started,3))
        release.save('deployment-result.json',report)
        print(json.dumps(report),flush=True)
    except Exception as exc:
        subprocess.run([*release.COMPOSE,'-f',str(overrides[1]),'up','-d',
                        '--no-deps','--no-build','--pull','never',*release.SERVICES],check=True)
        for name in release.SERVICES:
            release.wait_healthy(name)
            item = release.inspect([name])[name]
            assert item['Image'] == PREVIOUS and release.fingerprint(item) == release.fingerprint(before[name])
        release.save('deployment-result.json',dict(deployed=False,rolled_back=True,error_type=type(exc).__name__))
        raise


if __name__ == '__main__': main()
