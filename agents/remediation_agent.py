"""
AgenticSRE Remediation Agent
Self-healing agent with plan→execute→verify→rollback cycle.
"""

import logging
import copy
import uuid
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class RemediationAgent:
    """
    Self-healing remediation agent.
    
    Flow: LLM generates NL plan → converts to kubectl commands → 
    executes with recorded undo actions → verifies cluster health.
    """

    PLAN_PROMPT = """You are an expert SRE responsible for remediating Kubernetes issues.
Given the root cause analysis result, generate a safe remediation plan.

RCA Result:
{rca_result}

Cluster Context:
{cluster_context}

Safety constraints:
- NEVER delete namespaces, nodes, or persistent volumes
- ALWAYS prefer scaling over deletion
- Use rolling restart over hard restart
- Ensure rollback commands exist for every action

Generate a remediation plan:
{{
    "actions": [
        {{
            "description": "what this action does",
            "command": "kubectl command to execute",
            "rollback_command": "kubectl command to undo this",
            "risk_level": "low|medium|high",
            "verification": "how to verify the action succeeded"
        }}
    ],
    "estimated_recovery_time": "2m",
    "requires_approval": true
}}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        self.execution = None
        self.namespace = ""
        
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.confidence_threshold = cfg.remediation.confidence_threshold
        self.require_approval = cfg.remediation.require_approval
        self.enabled = cfg.remediation.enabled

    async def remediate(self, rca_result: Dict, confidence: float = 0.0,
                         approved: bool = False) -> Dict:
        """Execute remediation based on RCA results."""
        
        if not self.enabled:
            return {"status": "disabled", "message": "Self-healing is disabled in configuration"}

        if confidence < self.confidence_threshold:
            return {
                "status": "skipped",
                "message": f"Confidence {confidence:.2f} below threshold {self.confidence_threshold}",
            }

        if self.require_approval and not approved:
            plan = self._generate_plan(rca_result)
            return {
                "status": "pending_approval",
                "plan": plan,
                "message": "Remediation plan requires approval before execution",
            }

        return self.execute_plan(self._generate_plan(rca_result))

    def execute_plan(self, plan: Dict, checkpoint=None) -> Dict:
        """Execute exactly this plan, checkpointing intent before each write.

        A failed tool call may already have changed the target. Never retry it
        or automatically undo earlier steps without reconciling that outcome.
        """
        specs = plan.get("actions") if isinstance(plan, dict) else None
        if (not isinstance(specs, list) or not specs or any(
            not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("command", "rollback_command")
            ) for item in specs
        )):
            return {"status": "failed", "error": "invalid_or_empty_plan",
                    "actions": [], "requires_manual_review": True, "rollback_available": False}
        record = {"status": "running", "actions": [
            {"action_id": f"rem-{uuid.uuid4().hex[:8]}",
             "description": item.get("description", ""),
             "command": item["command"], "rollback_command": item["rollback_command"],
             "status": "pending"} for item in copy.deepcopy(specs)
        ], "requires_manual_review": False, "rollback_available": False}
        self.execution = record
        save = checkpoint or (lambda value: None)
        save(record)
        for action in record["actions"]:
            action["status"] = "executing"
            save(record)
            try:
                result = self._execute_command(action["command"])
            except Exception as exc:
                result = ToolResult(success=False, error=str(exc))
            action.update(status="executed" if result.success else "failed",
                          result=str(result.data)[:500] if result.success else result.error,
                          outcome_unknown=not result.success)
            record["rollback_available"] = any(a["status"] == "executed" for a in record["actions"])
            if not result.success:
                record.update(status="failed", requires_manual_review=True)
            save(record)
            if not result.success:
                return record
        try:
            verification = self._verify_health()
        except Exception as exc:
            verification = {"status": "error", "error": str(exc)}
        state = verification.get("overall_status", verification.get("status")) if isinstance(verification, dict) else None
        checks = verification.get("checks", {}) if isinstance(verification, dict) else {}
        known_healthy = state in ("healthy", "ok") and isinstance(checks, dict) and all(
            isinstance(item, dict) and item.get("status") in ("healthy", "ok")
            for item in checks.values())
        record.update(verification=verification,
                      status="executed" if known_healthy else "verification_failed",
                      requires_manual_review=not known_healthy)
        save(record)
        return record

    def rollback(self, execution=None, checkpoint=None) -> Dict:
        """Undo recorded, confirmed writes once in reverse order.

        Unknown execution/undo outcomes require operator reconciliation. An
        empty or legacy record cannot establish that anything was rolled back.
        """
        record = execution if execution is not None else self.execution
        if not record or not record.get("actions"):
            return {"status": "blocked", "error": "no_recorded_actions",
                    "requires_manual_review": True, "actions": []}
        actions = record["actions"]
        if any(a.get("outcome_unknown") or a.get("status") in (
            "executing", "rolling_back", "rollback_failed", "failed") for a in actions):
            return {"status": "blocked", "error": "action_outcome_unknown",
                    "requires_manual_review": True, "actions": copy.deepcopy(actions)}
        eligible = [a for a in reversed(actions) if a.get("status") == "executed"]
        if any(not a.get("rollback_command") for a in eligible):
            return {"status": "blocked", "error": "missing_recorded_rollback",
                    "requires_manual_review": True, "actions": copy.deepcopy(actions)}
        if not eligible:
            done = any(a.get("status") == "rolled_back" for a in actions)
            return {"status": "rolled_back" if done else "blocked", "requires_manual_review": not done,
                    "actions": copy.deepcopy(actions)}
        save = checkpoint or (lambda value: None)
        for action in eligible:
            action["status"] = "rolling_back"
            record["status"] = "rolling_back"
            save(record)
            try:
                result = self._execute_command(action["rollback_command"])
            except Exception as exc:
                result = ToolResult(success=False, error=str(exc))
            action.update(status="rolled_back" if result.success else "rollback_failed",
                          rollback_result=str(result.data)[:500] if result.success else result.error,
                          outcome_unknown=not result.success)
            if not result.success:
                record.update(status="rollback_failed", requires_manual_review=True)
                save(record)
                return {"status": "failed", "requires_manual_review": True,
                        "actions": copy.deepcopy(actions)}
            save(record)
        record.update(status="rolled_back", rollback_available=False)
        save(record)
        return {"status": "rolled_back", "requires_manual_review": False,
                "actions": copy.deepcopy(actions),
                "scope": "recorded undo commands succeeded; business recovery not certified"}

    def _generate_plan(self, rca_result: Dict) -> Dict:
        """Generate remediation plan via LLM."""
        # Get cluster context
        cluster_context = ""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health:
            result = k8s_health.execute(component="pods")
            if result.success:
                cluster_context = str(result.data)[:2000]

        try:
            return self.llm.json_chat([
                {"role": "system", "content": "You are an expert SRE remediation planner."},
                {"role": "user", "content": self.PLAN_PROMPT.format(
                    rca_result=str(rca_result)[:3000],
                    cluster_context=cluster_context,
                )}
            ])
        except Exception as e:
            logger.error(f"Plan generation failed: {e}")
            return {"actions": [], "error": str(e)}

    def _execute_command(self, command: str) -> ToolResult:
        """Execute a kubectl command."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return ToolResult(success=False, error="kubectl tool not available")
        # Strip 'kubectl' prefix if present
        cmd = command.strip()
        if cmd.startswith("kubectl "):
            cmd = cmd[8:]
        return kubectl.execute(command=cmd, **({"namespace": self.namespace} if self.namespace else {}))

    def _verify_health(self) -> Dict:
        """Verify cluster health after remediation."""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return {"status": "unknown", "note": "health check tool not available"}
        result = k8s_health.execute(component="all")
        return result.data if result.success else {"status": "error", "error": result.error}
