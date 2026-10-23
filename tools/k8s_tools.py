"""
AgenticSRE Kubernetes Tools
Provides kubectl wrapper and K8s SDK operations with safety controls.
"""

import json
import logging
import shlex
from skill_runtime.kubectl_safety import readonly_kubectl_args
import subprocess
from typing import Any, Dict, List, Optional

from tools.base_tool import SRETool, ToolResult

logger = logging.getLogger(__name__)


class KubectlTool(SRETool):
    """Safe kubectl command execution with read/write safety controls."""

    name = "kubectl"
    description = "Execute kubectl commands against the K8s cluster"

    # Read-only commands that are always safe
    SAFE_COMMANDS = {
        "get", "describe", "logs", "top", "explain",
        "api-resources", "api-versions", "cluster-info",
        "config view", "version", "events",
    }

    # Commands that require explicit write permission
    WRITE_COMMANDS = {
        "apply", "create", "delete", "patch", "replace",
        "scale", "rollout", "cordon", "uncordon", "drain",
        "taint", "label", "annotate", "edit", "exec",
    }

    def __init__(self, kubeconfig: str = "", context: str = "", namespace: str = "default",
                 allow_write: bool = False, use_dry_run: bool = True,
                 forbidden_commands: Optional[List[str]] = None,
                 ssh_jump_host: str = "", target_host: str = "",
                 use_ssh: bool = False):
        self.kubeconfig = kubeconfig
        self.context = context
        self.namespace = namespace
        self.allow_write = allow_write
        self.use_dry_run = use_dry_run
        self.forbidden = forbidden_commands or []
        self.ssh_jump_host = ssh_jump_host
        self.target_host = target_host
        self.use_ssh = use_ssh

    def _execute(self, command: str, namespace: str = "", timeout: int = 30) -> ToolResult:
        ns = namespace or self.namespace
        
        # Safety check
        cmd_parts = command.strip().split()
        if not cmd_parts:
            return ToolResult(success=False, error="Empty command")

        verb = cmd_parts[0].lower()
        
        # Check forbidden commands
        for forbidden in self.forbidden:
            if forbidden.lower() in command.lower():
                return ToolResult(success=False, error=f"Forbidden command: {forbidden}")

        # Write safety gate
        if verb in self.WRITE_COMMANDS and not self.allow_write:
            return ToolResult(success=False, error=f"Write command '{verb}' blocked. Set allow_write=True.")

        try:
            if not self.allow_write:
                args = readonly_kubectl_args(command, ns)
            else:
                args = shlex.split(command)
                if ns and not any(a in {"-n", "--namespace", "-A", "--all-namespaces"} or a.startswith("--namespace=") for a in args):
                    args.extend(["-n", ns])
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if verb in self.WRITE_COMMANDS and self.use_dry_run and not any(a.startswith("--dry-run") for a in args):
            args.append("--dry-run=client")
        if verb == "get" and not any(a in {"-o", "--output"} or a.startswith(("--output=", "-o=")) for a in args):
            args.extend(["-o", "wide"])
        full_cmd = ["kubectl"]
        if self.kubeconfig:
            full_cmd.append(f"--kubeconfig={self.kubeconfig}")
        if self.context:
            full_cmd.append(f"--context={self.context}")
        full_cmd.extend(args)
        if self.use_ssh and self.ssh_jump_host and self.target_host:
            full_cmd = ["ssh", "-J", self.ssh_jump_host, self.target_host, shlex.join(full_cmd)]

        try:
            result = subprocess.run(
                full_cmd, shell=False, capture_output=True, text=True,
                timeout=timeout
            )
            if result.returncode == 0:
                return ToolResult(success=True, data=result.stdout.strip())
            else:
                return ToolResult(success=False, error=result.stderr.strip(), data=result.stdout.strip())
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f"Command timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "kubectl command (without 'kubectl' prefix)"},
                "namespace": {"type": "string", "description": "Target namespace (optional)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            "required": ["command"]
        }

    def health_check(self) -> bool:
        result = self._execute(command="version --client")
        return result.success


class K8sResourceTool(SRETool):
    """High-level K8s resource operations using kubectl."""

    name = "k8s_resources"
    description = "Query and manage Kubernetes resources (pods, services, deployments, etc.)"

    def __init__(self, kubectl: KubectlTool):
        self.kubectl = kubectl

    def _execute(self, action: str = "list", resource_type: str = "pods",
                 name: str = "", namespace: str = "", labels: str = "",
                 output: str = "json") -> ToolResult:
        
        if action == "list":
            cmd = f"get {resource_type}"
            if labels:
                cmd += f" -l {labels}"
            cmd += f" -o {output}"
        elif action == "describe":
            cmd = f"describe {resource_type}"
            if name:
                cmd += f" {name}"
        elif action == "logs":
            if not name:
                return ToolResult(success=False, error="Pod name required for logs")
            cmd = f"logs {name} --tail=200"
        elif action == "events":
            cmd = "get events --sort-by='.lastTimestamp'"
        elif action == "top":
            cmd = f"top {resource_type}"
            if name:
                cmd += f" {name}"
        else:
            return ToolResult(success=False, error=f"Unknown action: {action}")

        return self.kubectl.execute(command=cmd, namespace=namespace)

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "describe", "logs", "events", "top"]},
                "resource_type": {"type": "string", "default": "pods"},
                "name": {"type": "string", "description": "Resource name"},
                "namespace": {"type": "string"},
                "labels": {"type": "string", "description": "Label selector"},
                "output": {"type": "string", "default": "json"},
            },
            "required": ["action"]
        }


class K8sHealthTool(SRETool):
    """Comprehensive K8s cluster health checker."""

    name = "k8s_health"
    description = "Run comprehensive health checks on K8s cluster components"

    def __init__(self, kubectl: KubectlTool):
        self.kubectl = kubectl

    def _execute(self, component: str = "all") -> ToolResult:
        checks = {}
        
        if component in ("all", "nodes"):
            checks["nodes"] = self._check_nodes()
        if component in ("all", "pods"):
            checks["pods"] = self._check_pods()
        if component in ("all", "services"):
            checks["services"] = self._check_services()
        if component in ("all", "events"):
            checks["events"] = self._check_events()
        if component in ("all", "resources"):
            checks["resources"] = self._check_resources()

        # Overall health
        unhealthy = [k for k, v in checks.items() if v.get("status") == "unhealthy"]
        overall = "unhealthy" if unhealthy else "healthy"
        
        return ToolResult(
            success=True,
            data={
                "overall_status": overall,
                "unhealthy_components": unhealthy,
                "checks": checks
            }
        )

    def _check_nodes(self) -> Dict:
        result = self.kubectl.execute(command="get nodes -o json")
        if not result.success:
            return {"status": "unknown", "error": result.error}
        try:
            nodes = json.loads(result.data)
            node_list = []
            unhealthy = 0
            for node in nodes.get("items", []):
                name = node["metadata"]["name"]
                conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
                ready = conditions.get("Ready", "Unknown")
                if ready != "True":
                    unhealthy += 1
                node_list.append({"name": name, "ready": ready, "conditions": conditions})
            return {
                "status": "unhealthy" if unhealthy > 0 else "healthy",
                "total": len(node_list),
                "unhealthy_count": unhealthy,
                "nodes": node_list
            }
        except Exception as e:
            return {"status": "unknown", "error": str(e)}

    def _check_pods(self) -> Dict:
        result = self.kubectl.execute(command="get pods --all-namespaces -o json")
        if not result.success:
            return {"status": "unknown", "error": result.error}
        try:
            pods = json.loads(result.data)
            problem_pods = []
            total = 0
            for pod in pods.get("items", []):
                total += 1
                phase = pod.get("status", {}).get("phase", "Unknown")
                name = pod["metadata"]["name"]
                ns = pod["metadata"]["namespace"]
                
                # Check for problem states
                container_statuses = pod.get("status", {}).get("containerStatuses", [])
                for cs in container_statuses:
                    waiting = cs.get("state", {}).get("waiting", {})
                    reason = waiting.get("reason", "")
                    if reason in ("CrashLoopBackOff", "ImagePullBackOff", "OOMKilled", "Error"):
                        problem_pods.append({
                            "name": name, "namespace": ns,
                            "phase": phase, "reason": reason,
                            "restart_count": cs.get("restartCount", 0)
                        })
                
                if phase not in ("Running", "Succeeded"):
                    if not any(p["name"] == name for p in problem_pods):
                        problem_pods.append({"name": name, "namespace": ns, "phase": phase})

            return {
                "status": "unhealthy" if problem_pods else "healthy",
                "total_pods": total,
                "problem_pods": problem_pods[:20],  # Limit for context
            }
        except Exception as e:
            return {"status": "unknown", "error": str(e)}

    def _check_services(self) -> Dict:
        result = self.kubectl.execute(command="get svc --all-namespaces -o json")
        if not result.success:
            return {"status": "unknown", "error": result.error}
        try:
            data = json.loads(result.data)
            return {"status": "healthy", "total": len(data.get("items", []))}
        except Exception:
            return {"status": "unknown"}

    def _check_events(self) -> Dict:
        result = self.kubectl.execute(
            command="get events --all-namespaces --sort-by='.lastTimestamp' --field-selector type=Warning"
        )
        if not result.success:
            return {"status": "unknown", "error": result.error}
        warnings = [line for line in (result.data or "").split("\n") if line.strip()]
        return {
            "status": "unhealthy" if len(warnings) > 5 else "healthy",
            "warning_count": max(len(warnings) - 1, 0),  # subtract header
            "recent_warnings": warnings[:10]
        }

    def _check_resources(self) -> Dict:
        result = self.kubectl.execute(command="top nodes")
        if not result.success:
            return {"status": "unknown", "note": "metrics-server may not be installed"}
        return {"status": "healthy", "data": result.data}
