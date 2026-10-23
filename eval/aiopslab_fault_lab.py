#!/usr/bin/env python3
"""
Controlled fault injection runner for AIOpsLab / K8s validation.

Examples:
    python -m eval.aiopslab_fault_lab list
    python -m eval.aiopslab_fault_lab inject --scenario sn-cpu-stress --dry-run
    python -m eval.aiopslab_fault_lab inject --scenario sn-cpu-stress --background-load
    python -m eval.aiopslab_fault_lab cleanup --scenario sn-cpu-stress
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).parent.parent
SCENARIOS_FILE = Path(__file__).parent / "fault_scenarios.yaml"

logger = logging.getLogger(__name__)


def load_scenarios() -> Dict[str, Any]:
    return yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8")) or {}


def scenario_by_id(scenario_id: str) -> Dict[str, Any]:
    data = load_scenarios()
    for scenario in data.get("scenarios", []):
        if scenario.get("id") == scenario_id:
            return scenario
    raise SystemExit(f"Scenario not found: {scenario_id}")


def run_command(cmd: str, timeout: int = 120) -> Dict[str, Any]:
    logger.info("Exec: %s", cmd)
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {
            "command": cmd,
            "returncode": result.returncode,
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip()[-4000:],
            "stderr": result.stderr.strip()[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"command": cmd, "returncode": 124, "ok": False, "stdout": "", "stderr": "timeout"}


def background_load_command(namespace: str, duration_s: int = 180) -> str:
    job = f"aiopslab-bg-load-{int(time.time())}"
    return (
        f"kubectl -n {namespace} create job {job} "
        "--image=alpine:3.18 --image-pull-policy=IfNotPresent -- /bin/sh -c "
        f"'for i in $(seq 1 {duration_s}); do "
        "wget -q -O /dev/null --timeout=2 "
        "http://nginx-thrift:8080/wrk2-api/home-timeline/read?start=0\\&stop=10 || true; "
        "wget -q -O /dev/null --timeout=2 "
        "http://nginx-thrift:8080/wrk2-api/user-timeline/read?user_id=1\\&start=0\\&stop=10 || true; "
        "sleep 1; done'"
    )


def build_commands(scenario: Dict[str, Any], action: str, background_load: bool = False) -> List[str]:
    inject = scenario.get("inject", {})
    if action == "cleanup":
        return list(inject.get("cleanup", []))

    commands = list(inject.get("commands", []))
    if background_load:
        namespace = inject.get("namespace", "test-social-network")
        commands.insert(0, background_load_command(namespace))
    return commands


def cmd_list(_: argparse.Namespace):
    data = load_scenarios()
    rows = []
    for scenario in data.get("scenarios", []):
        rows.append({
            "id": scenario.get("id"),
            "category": scenario.get("category"),
            "fault_type": scenario.get("fault_type"),
            "target": scenario.get("target_service"),
            "namespace": scenario.get("inject", {}).get("namespace"),
        })
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_run(args: argparse.Namespace):
    scenario = scenario_by_id(args.scenario)
    commands = build_commands(scenario, args.action, background_load=args.background_load)
    payload = {
        "action": args.action,
        "scenario_id": scenario.get("id"),
        "name": scenario.get("name"),
        "dry_run": args.dry_run,
        "commands": commands,
        "results": [],
    }

    if not args.dry_run:
        payload["results"] = [run_command(cmd, timeout=args.timeout) for cmd in commands]

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if payload["results"] and not all(r.get("ok") for r in payload["results"]):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="AIOpsLab/K8s controlled fault injection")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="List available scenarios").set_defaults(func=cmd_list)

    for action in ("inject", "cleanup"):
        p = sub.add_parser(action, help=f"{action} a scenario")
        p.add_argument("--scenario", required=True)
        p.add_argument("--dry-run", action="store_true", help="Print commands without executing")
        p.add_argument("--timeout", type=int, default=120)
        if action == "inject":
            p.add_argument("--background-load", action="store_true", help="Start a short in-cluster background load job")
        else:
            p.set_defaults(background_load=False)
        p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
