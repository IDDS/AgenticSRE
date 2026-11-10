import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/validate_platform_native_faults.sh'


@pytest.mark.parametrize('state', ['exists', 'create_fails', 'apply_fails'])
def test_cleanup_only_after_namespace_created(tmp_path, state):
    commands = tmp_path / 'commands'
    kubectl = tmp_path / 'kubectl'
    kubectl.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$COMMAND_LOG"
case "$1 $2" in
  'get namespace') [ "$TEST_STATE" = exists ]; exit $? ;;
  'create namespace') [ "$TEST_STATE" != create_fails ]; exit $? ;;
  'apply -f') exit 1 ;;
  'delete '*) exit 0 ;;
esac
exit 1
''')
    kubectl.chmod(0o755)
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\nprintf "mock-container\\n"\n')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f'{tmp_path}:' + os.environ['PATH'],
               COMMAND_LOG=str(commands), TEST_STATE=state)
    result = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    calls = commands.read_text().splitlines()
    deletes = [line for line in calls if line.startswith('delete ')]
    if state in ('exists', 'create_fails'):
        assert deletes == []
    else:
        assert any(line.startswith('delete namespace ') for line in deletes)
