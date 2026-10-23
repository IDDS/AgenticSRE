"""Command-line entry point for vLLM fault injection."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any

from vllm_fault_injector.host_faults import build_host_fault_command
from vllm_fault_injector.injector import FaultInjector


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def _run(args: argparse.Namespace) -> int:
    action = "cleanup" if args.command == "recover" else args.command

    if args.target == "host":
        built = build_host_fault_command(
            args.fault_type,
            action,
            dry_run=False,
            interface=args.interface,
            endpoint=args.endpoint,
            model=args.model,
            model_dir=args.model_dir,
        )
        if not built.supported:
            _print_json({"status": "unsupported", **asdict(built)})
            return 2
        if args.dry_run:
            _print_json({"status": "dry_run", **asdict(built)})
            return 0

        proc = await asyncio.create_subprocess_shell(
            built.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=args.timeout)
        _print_json({
            "status": "executed" if proc.returncode == 0 else "failed",
            **asdict(built),
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        })
        return proc.returncode

    injector = FaultInjector(
        kubectl_cmd=args.kubectl_cmd,
        namespace=args.namespace,
        deployment=args.deployment,
        dry_run=args.dry_run,
        prometheus_url=args.prometheus_url,
    )
    if action == "cleanup":
        result = await injector.recover(args.layer)
    elif action == "experiment":
        result = await injector.run_experiment(
            args.fault_type,
            baseline_samples=args.baseline_samples,
            fault_samples=args.fault_samples,
            sample_interval=args.sample_interval,
        )
    else:
        result = await injector.inject(args.fault_type)
    _print_json({
        "status": "dry_run" if args.dry_run else "executed",
        "target": args.target,
        "action": action,
        "result": result,
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="vLLM fault injector")
    parser.add_argument("command", choices=["inject", "recover", "experiment"])
    parser.add_argument("fault_type")
    parser.add_argument("--target", choices=["k8s", "host"], default="host")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)

    parser.add_argument("--namespace", default="default")
    parser.add_argument("--deployment", default="vllm-server")
    parser.add_argument("--kubectl-cmd", default="kubectl")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--layer", default="all")
    parser.add_argument("--baseline-samples", type=int, default=2)
    parser.add_argument("--fault-samples", type=int, default=3)
    parser.add_argument("--sample-interval", type=float, default=3.0)

    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/v1/completions")
    parser.add_argument("--model", default="vllm")
    parser.add_argument("--model-dir", default="")

    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
