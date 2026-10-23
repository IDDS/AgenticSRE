#!/usr/bin/env python3
"""Regression tests for K8s faultlab command generation."""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_app.app as app_module  # noqa: E402
from web_app.app import _prepare_faultlab_commands  # noqa: E402


def _scenario(scenario_id: str) -> dict:
    data = yaml.safe_load((ROOT / "eval" / "fault_scenarios.yaml").read_text(encoding="utf-8"))
    for scenario in data["scenarios"]:
        if scenario["id"] == scenario_id:
            return scenario
    raise AssertionError(f"scenario not found: {scenario_id}")


def test_background_load_cleanup_uses_label_selector() -> None:
    scenario = _scenario("sn-cpu-stress")
    cleanup = _prepare_faultlab_commands(
        scenario,
        cluster_id=None,
        namespace_override=None,
        commands_key="cleanup",
        background_load=False,
    )
    assert cleanup[0] == (
        "kubectl -n test-social-network delete job "
        "-l app=agenticsre-bg-load --ignore-not-found=true"
    )


def test_memory_pressure_has_no_conflicting_low_memory_limit(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_find_target_node", lambda *_args, **_kwargs: "worker-a")
    commands = _prepare_faultlab_commands(
        _scenario("sn-mem-pressure"),
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
        resolve_target_node=True,
    )
    command_text = "\n".join(commands)
    assert "--limits='memory=64Mi'" not in command_text
    assert '"emptyDir":{"medium":"Memory","sizeLimit":"128Mi"}' in command_text
    assert '"nodeName":"worker-a"' in command_text


def test_db_slow_does_not_depend_on_mongodb_dd() -> None:
    commands = _prepare_faultlab_commands(
        _scenario("sn-db-slow"),
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
    )
    command_text = "\n".join(commands)
    assert "scale deploy/post-storage-mongodb --replicas=0" in command_text
    assert "exec deploy/post-storage-mongodb" not in command_text
    assert "dd if=/dev/zero" not in command_text


def test_network_partition_does_not_install_iptables_in_service_container() -> None:
    commands = _prepare_faultlab_commands(
        _scenario("sn-network-partition"),
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
    )
    command_text = "\n".join(commands)
    assert "apt-get" not in command_text
    assert "iptables" not in command_text
    assert "scale deploy/url-shorten-service --replicas=0" in command_text


def test_entry_replica_exhaustion_restores_original_replica_count() -> None:
    commands = _prepare_faultlab_commands(
        _scenario("sn-network-delay"),
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
    )
    cleanup = _prepare_faultlab_commands(
        _scenario("sn-network-delay"),
        cluster_id=None,
        namespace_override=None,
        commands_key="cleanup",
        background_load=False,
    )
    command_text = "\n".join(commands + cleanup)
    assert "agenticsre.io/original-replicas" in command_text
    assert "scale deploy/nginx-thrift --replicas=0" in command_text
    assert '--replicas="${replicas:-1}"' in command_text
    assert "tc qdisc" not in command_text


def test_staged_write_cascade_uses_control_plane_for_database_failure() -> None:
    commands = _prepare_faultlab_commands(
        _scenario("sn-staged-write-cascade"),
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
    )
    cleanup = _prepare_faultlab_commands(
        _scenario("sn-staged-write-cascade"),
        cluster_id=None,
        namespace_override=None,
        commands_key="cleanup",
        background_load=False,
    )
    command_text = "\n".join(commands + cleanup)
    assert "scale deploy/post-storage-mongodb --replicas=0" in command_text
    assert "scale deploy/post-storage-mongodb --replicas=1" in command_text
    assert "exec deploy/post-storage-mongodb" not in command_text
    assert "dd if=/dev/zero" not in command_text


def test_fault_scenarios_avoid_bash_and_service_container_network_tools() -> None:
    data = yaml.safe_load((ROOT / "eval" / "fault_scenarios.yaml").read_text(encoding="utf-8"))
    command_text = "\n".join(
        command
        for scenario in data["scenarios"]
        for section in ("commands", "cleanup")
        for command in scenario.get("inject", {}).get(section, [])
    )
    assert "bash -c" not in command_text
    assert "pgrep" not in command_text
    assert "apt-get" not in command_text
    assert "iptables" not in command_text


def test_eval_tasks_avoid_service_container_network_tool_installation() -> None:
    data = yaml.safe_load((ROOT / "eval" / "eval_tasks.yaml").read_text(encoding="utf-8"))
    command_text = "\n".join(
        command
        for task in data["tasks"]
        for section in ("commands", "cleanup")
        for command in task.get("inject", {}).get(section, [])
    )
    assert "bash -c" not in command_text
    assert "apt-get" not in command_text
    assert "iptables" not in command_text
    assert "tc qdisc" not in command_text
