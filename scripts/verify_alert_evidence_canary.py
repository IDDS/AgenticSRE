"""Two read-only post-deploy observations on snail; records no raw logs/config."""
from datetime import datetime, timezone
import json
import subprocess
import time

import deploy_alert_evidence_release as deploy


def main():
    release = deploy.release
    before = json.loads((deploy.ROOT/'before.json').read_text())
    observations = []
    for index in range(2):
        if index: time.sleep(30)
        current = release.inspect(list(before['containers']))
        checks = {}
        for name in release.SERVICES:
            item = current[name]
            raw = subprocess.check_output(['docker','logs','--since',item['State']['StartedAt'],name],
                                          stderr=subprocess.STDOUT)
            checks[name] = dict(healthy=item['State'].get('Health',{}).get('Status') == 'healthy',
                expected_image=item['Image'] == deploy.CANDIDATE,restarts=item['RestartCount'],
                config_preserved=release.fingerprint(item) == before['containers'][name]['fingerprint'],
                python_tracebacks=raw.count(b'Traceback (most recent call last)'))
            assert checks[name]['healthy'] and checks[name]['expected_image'] and checks[name]['config_preserved']
            assert checks[name]['restarts'] == checks[name]['python_tracebacks'] == 0
        assert release.snapshot() == before['data']
        assert release.config_hashes({name:current[name] for name in release.SERVICES}) == before['config_hashes']
        assert all(current[name]['Id'] == old['id'] and current[name]['Image'] == old['image']
                   for name,old in before['containers'].items() if name not in release.SERVICES)
        for port in (8080,8090): assert release.get(port,'/api/health')['status'] == 'ok'
        observation = dict(passed=True,observed_at=datetime.now(timezone.utc).isoformat(),services=checks,
            data_preserved=True,configuration_preserved=True,other_containers_unchanged=True)
        observations.append(observation)
        release.save('canary.json',dict(observations=observations,passed=len(observations) == 2))
        print(json.dumps(observation),flush=True)


if __name__ == '__main__': main()
