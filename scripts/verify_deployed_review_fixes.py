#!/usr/bin/env python3
"""Read-only validation of deployed source hashes, health and sibling isolation.

Run from the checkout being deployed. Never reads container environment/secrets.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("agenticsre", "agenticsre-skill-runtime")


def remote(host, arguments):
    completed = subprocess.run(
        ["ssh", host, shlex.join(arguments)], capture_output=True, text=True, timeout=60,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "remote command failed")
    return completed.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="snail")
    parser.add_argument("--file", action="append", default=[], help="Relative source path to compare in BOTH containers")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Previous output; compare agenticops container IDs/start times")
    args = parser.parse_args()
    paths = args.file or [
        "web_app/app.py", "orchestrator/skill_agent.py",
        "skill_runtime/tool_context.py", "skill_runtime/skills/tool_k8s_readiness/skill.py",
    ]
    for name in paths:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not (ROOT / path).is_file():
            parser.error(f"invalid relative source file: {name}")
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
    inventory = remote(args.host, ["docker", "ps", "-a", "--format", "{{.Names}}"])
    siblings = sorted(name for name in inventory.splitlines() if "agenticops" in name)
    # Health is absent on some sibling containers; use structured inspect template.
    format_string = '{"name":{{json .Name}},"id":{{json .Id}},"started_at":{{json .State.StartedAt}},"status":{{json .State.Status}},"image":{{json .Image}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
    states = [json.loads(line) for line in remote(
        args.host, ["docker", "inspect", "--format", format_string, *TARGETS, *siblings],
    ).splitlines()]
    script = (
        "import hashlib,json,pathlib; "
        f"names={paths!r}; "
        "print(json.dumps({n:hashlib.sha256(pathlib.Path('/app',n).read_bytes()).hexdigest() "
        "if pathlib.Path('/app',n).is_file() else None for n in names}))"
    )
    actual = {container: json.loads(remote(args.host, ["docker", "exec", container, "python", "-c", script])) for container in TARGETS}
    mismatches = [{"container": container, "path": name} for container, hashes in actual.items() for name in paths if hashes.get(name) != expected[name]]
    health = {}
    for container, port in ((TARGETS[0], 8080), (TARGETS[1], 8090)):
        script = f"import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:{port}/api/health',timeout=5))))"
        health[container] = json.loads(remote(args.host, ["docker", "exec", container, "python", "-c", script]))
    sibling_state = {item["name"]: {key: item[key] for key in ("id", "started_at", "status")} for item in states if "agenticops" in item["name"]}
    sibling_unchanged = None
    if args.baseline:
        sibling_unchanged = json.loads(args.baseline.read_text())["siblings"] == sibling_state
    healthy = all(value.get("status") == "ok" for value in health.values())
    success = not mismatches and healthy and sibling_unchanged is not False
    result = {"success": success, "host": args.host, "expected_hashes": expected, "actual_hashes": actual, "mismatches": mismatches, "health": health, "containers": states, "siblings": sibling_state, "siblings_unchanged": sibling_unchanged}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"success": success, "mismatches": mismatches, "healthy": healthy, "siblings_unchanged": sibling_unchanged}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
