"""Execute compound commands against a stub, never a real Kubernetes cluster."""
import shlex

import pytest

import web_app.app as dashboard


@pytest.mark.parametrize('command,count', [
    ('printf manifest | kubectl create -f -', 1),
    ('true && kubectl get pods && kubectl get nodes', 2),
    ('true; kubectl get pods; kubectl get nodes', 2),
    ('(kubectl get pods)', 1),
])
def test_compound_commands_keep_selected_cluster(monkeypatch, tmp_path, command, count):
    kubectl = tmp_path / 'kubectl'
    kubectl.write_text('#!/bin/sh\nprintf "%s\\n" "$KUBECONFIG"\n')
    kubectl.chmod(0o755)
    selected = str(tmp_path / "selected cluster's config.yaml")
    monkeypatch.setenv('KUBECONFIG', str(tmp_path / 'default-cluster.yaml'))
    monkeypatch.setattr(dashboard, '_find_k8s_cluster', lambda _: {
        'id': 'selected', 'kubectl_path': str(kubectl),
        'kubeconfig': selected, 'context': 'shared-context-name',
    })
    monkeypatch.setattr(dashboard, '_get_config', lambda: type('Config', (), {
        'kubernetes': type('Kubernetes', (), {'use_ssh': False})(),
    })())

    result = dashboard._shell_sync(command, timeout=5, cluster_id='selected')

    assert result['ok'], result['stderr']
    assert result['stdout'].splitlines() == [selected] * count


def test_cluster_scope_keeps_stdin_and_failure_status(monkeypatch, tmp_path):
    kubectl = tmp_path / 'kubectl'
    kubectl.write_text('#!/bin/sh\ncat\nexit 19\n')
    kubectl.chmod(0o755)
    monkeypatch.setattr(dashboard, '_find_k8s_cluster', lambda _: {
        'id': 'selected', 'kubectl_path': str(kubectl),
        'kubeconfig': str(tmp_path / 'selected.yaml'),
    })
    monkeypatch.setattr(dashboard, '_get_config', lambda: type('Config', (), {
        'kubernetes': type('Kubernetes', (), {'use_ssh': False})(),
    })())
    payload = '{"message": "quotes: \' and $() remain literal"}'

    result = dashboard._shell_sync(
        f'printf %s {shlex.quote(payload)} | kubectl create -f -',
        timeout=5, cluster_id='selected',
    )

    assert not result['ok']
    assert result['returncode'] == 19
    assert result['stdout'] == payload
