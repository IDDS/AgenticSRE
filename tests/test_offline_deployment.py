from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_persistent_data import validate_tree  # noqa: E402


def test_persistent_data_validator_accepts_valid_sqlite_and_json(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "memory" / "chroma.sqlite3"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("create table probe (id integer primary key, value text)")
        connection.execute("insert into probe(value) values ('ok')")
    (tmp_path / "history.json").write_text('[{"status": "ok"}]', encoding="utf-8")

    result = validate_tree(tmp_path)

    assert result == {
        "status": "ok",
        "root": str(tmp_path),
        "sqlite_files": 1,
        "json_files": 1,
        "errors": [],
    }


def test_persistent_data_validator_rejects_corrupt_state(tmp_path: Path) -> None:
    (tmp_path / "broken.sqlite3").write_bytes(b"not sqlite")
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")

    result = validate_tree(tmp_path)

    assert result["status"] == "error"
    assert result["sqlite_files"] == 1
    assert result["json_files"] == 1
    assert len(result["errors"]) == 2


def test_offline_installer_help_and_shell_syntax() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(ROOT / "install-offline.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        ["bash", str(ROOT / "install-offline.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--data-check" in help_result.stdout
    assert "--backup [FILE]" in help_result.stdout
    assert "--restore FILE" in help_result.stdout

    installer = (ROOT / "install-offline.sh").read_text(encoding="utf-8")
    assert '"${ACTION}" =~ ^(install|verify)$' in installer
    forbidden_install_commands = (
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "apk",
        "pip",
        "curl",
        "wget",
        "docker pull",
        "docker build",
    )
    executable_lines = [
        line.strip()
        for line in installer.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for command in forbidden_install_commands:
        assert not any(line == command or line.startswith(f"{command} ") for line in executable_lines)

    compose = (ROOT / "docker-compose.offline.yaml").read_text(encoding="utf-8")
    assert compose.count("pull_policy: never") == 2
    assert compose.count('AGENTICSRE_OFFLINE_MODE: "true"') == 2

    oneclick = ROOT / "offline-oneclick-installer.sh"
    oneclick_text = oneclick.read_text(encoding="utf-8")
    oneclick_syntax = subprocess.run(
        ["bash", "-n", str(oneclick)], capture_output=True, text=True, check=False
    )
    assert oneclick_syntax.returncode == 0, oneclick_syntax.stderr
    assert "__AGENTICSRE_OFFLINE_PAYLOAD_BELOW__" in oneclick_text
    assert "tail -n +" in oneclick_text


def test_oneclick_stub_can_extract_and_forward_help(tmp_path: Path) -> None:
    oneclick = tmp_path / "agenticsre.run"
    stub = (ROOT / "offline-oneclick-installer.sh").read_bytes()
    payload = tmp_path / "payload.tar.gz"
    payload_root = tmp_path / "payload-root" / "agenticsre-offline-test"
    payload_root.mkdir(parents=True)
    shutil.copy2(ROOT / ".env.offline.example", payload_root / ".env.offline.example")
    subprocess.run(
        ["tar", "-czf", str(payload), "-C", str(payload_root.parent), payload_root.name],
        check=True,
    )
    oneclick.write_bytes(stub + payload.read_bytes())
    oneclick.chmod(0o755)
    result = subprocess.run(
        ["bash", str(oneclick), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "install-offline.sh is missing" in result.stderr


@pytest.mark.skipif(
    shutil.which("sha256sum") is None and shutil.which("shasum") is None,
    reason="checksum utility unavailable",
)
def test_offline_bundle_verify_only_is_network_and_docker_independent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True)
    (bundle / "runtime").mkdir()
    shutil.copy2(ROOT / "install-offline.sh", bundle / "install-offline.sh")
    shutil.copy2(ROOT / "docker-compose.offline.yaml", bundle / "docker-compose.offline.yaml")
    (bundle / "images" / "agenticsre-image.tar.gz").write_bytes(b"fake-image")
    (bundle / "runtime" / "docker-29.7.2.tgz").write_bytes(b"fake-docker")
    (bundle / "runtime" / "docker-compose-linux-amd64").write_bytes(b"fake-compose")
    (bundle / "BUNDLE.env").write_text(
        "\n".join(
            [
                "BUNDLE_VERSION=test-1",
                "DEPLOYMENT_MODE=strict-offline",
                "REQUIRES_INTERNET=0",
                "CONTAINS_APPLICATION_IMAGE=1",
                "CONTAINS_DOCKER_RUNTIME=1",
                "CONTAINS_COMPOSE=1",
                "CONTAINS_EMBEDDING_MODEL=1",
                "TARGET_OS=linux",
                "TARGET_ARCH=amd64",
                "IMAGE_REF=agenticsre-offline:test-1-amd64",
                "DOCKER_VERSION=29.7.2",
                "COMPOSE_VERSION=v5.5.0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    checksummed = [
        "BUNDLE.env",
        "docker-compose.offline.yaml",
        "images/agenticsre-image.tar.gz",
        "install-offline.sh",
        "runtime/docker-29.7.2.tgz",
        "runtime/docker-compose-linux-amd64",
    ]
    lines = []
    for relative in checksummed:
        digest = hashlib.sha256((bundle / relative).read_bytes()).hexdigest()
        lines.append(f"{digest}  ./{relative}\n")
    (bundle / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    env = dict(os.environ)
    env.update({"AGENTICSRE_TEST_UNAME_S": "Linux", "AGENTICSRE_TEST_UNAME_M": "x86_64"})
    result = subprocess.run(
        ["bash", str(bundle / "install-offline.sh"), "--verify-only"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "严格离线包预检通过" in result.stdout


def test_dashboard_health_exposes_initialized_persistence(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    import web_app.app as app_module

    monkeypatch.setitem(
        app_module._state,
        "persistence_status",
        {"status": "ok", "backend": "chromadb", "path": "./data/memory"},
    )
    response = TestClient(app_module.app).get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["persistence"]["backend"] == "chromadb"


def test_dashboard_and_skill_runtime_use_distinct_workflow_indexes() -> None:
    import skill_runtime.app as runtime_app
    import web_app.app as dashboard_app

    assert runtime_app.SKILL_WORKFLOW_RUNS_FILE != dashboard_app._SKILL_WORKFLOW_RUNS_FILE
    assert "skill_runtime" in runtime_app.SKILL_WORKFLOW_RUNS_FILE.parts
