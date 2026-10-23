from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml


def default_aishperf_root() -> Path:
    configured = os.environ.get("AISHPERF_OPENNESS_DIR") or os.environ.get("AISH_PERF_OPENNESS_DIR")
    if configured:
        return Path(configured).expanduser()
    candidates = [
        Path("/opt/aishperf_openness"),
        Path("/home/ubuntu/aishperf_openness"),
        Path("/private/tmp/aishperf_openness"),
        Path.home() / "aishperf_openness",
    ]
    for candidate in candidates:
        if (candidate / "entities" / "aiops-chaos" / "new_config.yaml").exists():
            return candidate
    return candidates[0]


BUILTIN_CASES_FILE = Path(__file__).resolve().parents[2] / "configs" / "aishperf_builtin_cases.yaml"


DOMAIN_KEYWORDS = {
    "gpu": (" gpu ", "nvidia-smi", "xid", "gpu-", "high-temp", "ecc", "row", "cuda", "nvswitch"),
    "rdma": ("rdma", "roce", "nccl", "mlx5", "gid", "ib "),
    "network": (" eth ", " tc ", "dns", "mtu", "ip-slow", "link-down", "ddos", "bond"),
    "storage": ("gpfs", "nfs", "disk", "raid", "nvme", "storage", "pvc"),
    "kubernetes": ("kubectl", "workload k8s", "pod", "deployment", "service", "node-unscheduable", "empty-endpoints"),
    "process": ("process", "pkill", "kill", "zombie", "sshd", "systemd"),
    "cpu_memory": ("cpu", "memory", "hugepage", "powersave", "offline"),
    "security": ("hack", "ssh", "mining", "pty", "shell"),
    "ascend": ("ascend", "npu-smi", "hccl"),
    "vendor_gpu": ("biren", "metax", "mthreads", "tianshu", "iluvatar", "musa"),
}


@dataclass(frozen=True)
class AISHPerfCase:
    id: str
    name: str
    inject: List[str]
    recover: List[str]
    domains: List[str]
    risk: str
    source: str = "aishperf.aiops-chaos"

    def to_dict(self, *, include_commands: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "domains": self.domains,
            "risk": self.risk,
            "inject_steps": len(self.inject),
            "recover_steps": len(self.recover),
            "summary": self.summary(),
        }
        if include_commands:
            payload["inject"] = self.inject
            payload["recover"] = self.recover
        return payload

    def summary(self) -> str:
        domain_text = "、".join(self.domains) if self.domains else "通用"
        return f"AISHPerf {domain_text} 故障场景：{self.name}"


def _split_commands(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items: List[str] = []
        for item in raw:
            items.extend(_split_commands(item))
        return [item for item in items if item]
    text = str(raw).strip()
    if not text:
        return []
    # AISHPerf config uses YAML list items as ordered shell snippets. Keep each
    # list item intact when possible, only split simple pipe-separated scripts
    # for display and risk classification.
    parts = [part.strip() for part in re.split(r"\n\s*\|\s*|\s+\|\s+", text) if part.strip()]
    return parts or [text]


def _classify_domains(name: str, commands: Iterable[str]) -> List[str]:
    haystack = f" {name} " + " ".join(commands)
    haystack = haystack.lower()
    domains = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            domains.append(domain)
    return domains or ["generic"]


def _risk(domains: Iterable[str], commands: Iterable[str]) -> str:
    domain_set = set(domains)
    joined = " ".join(commands).lower()
    if domain_set.intersection({"gpu", "rdma", "storage", "security", "ascend", "vendor_gpu"}) or "--host-ns" in joined:
        return "critical"
    if domain_set.intersection({"network", "kubernetes", "process"}) or "kubectl delete" in joined or "systemd" in joined:
        return "high"
    if domain_set.intersection({"cpu_memory"}):
        return "medium"
    return "low"


class AISHPerfCatalog:
    """Read-only AISHPerf aiops-chaos catalog adapter.

    The adapter intentionally does not execute AISHPerf commands. It imports
    scenario metadata so AgenticSRE can map, review, and dry-run external cases
    before a dedicated executor is installed and approved.
    """

    def __init__(self, root: Optional[str | Path] = None):
        self.root = Path(root).expanduser() if root else default_aishperf_root()

    @property
    def config_path(self) -> Path:
        return self.root / "entities" / "aiops-chaos" / "new_config.yaml"

    @property
    def eval_root(self) -> Path:
        return self.root / "suites" / "aiops-eval"

    def status(self) -> Dict[str, Any]:
        available = self.config_path.exists()
        builtin_available = BUILTIN_CASES_FILE.exists()
        return {
            "available": available or builtin_available,
            "external_available": available,
            "builtin_available": builtin_available,
            "mode": "external" if available else ("builtin_fallback" if builtin_available else "unavailable"),
            "root": str(self.root),
            "config_path": str(self.config_path),
            "builtin_config_path": str(BUILTIN_CASES_FILE),
            "aiops_eval_available": self.eval_root.exists(),
            "source_url": "https://gitee.com/aishperf-caict/aishperf_openness",
        }

    def _load_config(self) -> Dict[str, Any]:
        path = self.config_path if self.config_path.exists() else BUILTIN_CASES_FILE
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def load_cases(self) -> List[AISHPerfCase]:
        data = self._load_config()
        if not data:
            return []
        cases = ((data.get("emulator") or {}).get("cases") or data.get("cases") or [])
        parsed: List[AISHPerfCase] = []
        for item in cases:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            inject = _split_commands(item.get("inject"))
            recover = _split_commands(item.get("recover"))
            domains = _classify_domains(name, [*inject, *recover])
            parsed.append(AISHPerfCase(
                id=f"aishperf-{name}",
                name=name,
                inject=inject,
                recover=recover,
                domains=domains,
                risk=_risk(domains, [*inject, *recover]),
            ))
        return parsed

    def list_cases(self, *, domain: str = "", query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        q = query.lower().strip()
        selected = []
        for case in self.load_cases():
            if domain and domain not in case.domains:
                continue
            haystack = f"{case.name} {' '.join(case.domains)} {' '.join(case.inject[:3])}".lower()
            if q and q not in haystack:
                continue
            selected.append(case.to_dict())
            if len(selected) >= limit:
                break
        return selected

    def find_case(self, case_id: str) -> Optional[AISHPerfCase]:
        normalized = case_id.removeprefix("aishperf-")
        for case in self.load_cases():
            if case.id == case_id or case.name == normalized:
                return case
        return None

    def build_dry_run_plan(self, case_id: str, *, node: str = "${NODE}", namespace: str = "${NAMESPACE}") -> Dict[str, Any]:
        case = self.find_case(case_id)
        if not case:
            raise KeyError(case_id)
        return {
            "status": "dry_run",
            "executor": "aishperf.aemu",
            "case": case.to_dict(include_commands=True),
            "requires": [
                "已部署 AISHPerf aiops-chaos 的 aemu 服务",
                "目标节点在 aemu emulator.nodes 白名单中",
                "执行前完成 AgenticSRE 风险门控、审批和回滚检查",
            ],
            "aemu_http": {
                "inject": f"POST $AEMU_URL/api/fault/{case.name}",
                "recover": f"DELETE $AEMU_URL/api/fault/{case.name}",
                "body": {"node": node, "namespace": namespace},
            },
            "local_submit": {
                "inject": f"aemu submit --node {node} -c config.yaml --fault {case.name} --inject",
                "recover": f"aemu submit --node {node} -c config.yaml --fault {case.name} --recover",
            },
            "safety": {
                "risk": case.risk,
                "auto_execute": False,
                "reason": "AISHPerf 场景包含 host namespace、GPU/RDMA/存储/安全类高风险命令，默认只允许 dry-run 和人工审批。",
            },
        }

    def aiops_eval_capability(self) -> Dict[str, Any]:
        metrics = self.eval_root / "metrics" / "spec.json"
        rules = self.eval_root / "rules"
        scenarios = self.eval_root / "scenarios"
        return {
            "available": self.eval_root.exists(),
            "rules": sorted(p.stem for p in rules.glob("*.json")) if rules.exists() else [],
            "scenarios": sorted(p.stem for p in scenarios.glob("*.json")) if scenarios.exists() else [],
            "metrics_spec": str(metrics) if metrics.exists() else "",
            "integration_target": "AgenticSRE 可复用其 fault_injection_success_rate、recovery_success_rate、diagnosis_pass_rate、avg_turns、avg_latency_s 等评测指标。",
        }
