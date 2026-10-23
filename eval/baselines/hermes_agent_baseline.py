"""
Hermes Agent Baseline for Fault Diagnosis Comparison
Directly invokes NousResearch's Hermes Agent framework (AIAgent) with
terminal tools for kubectl/curl access to the K8s cluster.

This is NOT a simulation — it runs the real Hermes Agent loop with its
full tool-calling capabilities (terminal, file, web, delegation).
"""

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Path to the cloned hermes-agent repo
HERMES_REPO = os.environ.get(
    "HERMES_AGENT_PATH",
    str(Path(__file__).parent.parent.parent / "hermes-agent"),
)

# SSH config for remote cluster access (reused from AgenticSRE config)
_SSH_PREFIX = ""


def _ensure_hermes_importable():
    """Add hermes-agent repo to sys.path so we can import AIAgent."""
    repo = Path(HERMES_REPO)
    if not repo.exists():
        # Try /tmp fallback (where we cloned it)
        repo = Path("/tmp/hermes-agent")
    if not repo.exists():
        raise ImportError(
            f"Hermes Agent repo not found at {HERMES_REPO} or /tmp/hermes-agent. "
            "Clone it first: git clone https://github.com/NousResearch/hermes-agent.git"
        )
    repo_str = str(repo)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


SRE_SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) performing root cause analysis
on a Kubernetes cluster. You have access to terminal tools to run commands.

INVESTIGATION APPROACH:
1. Start by checking pod status and K8s events for obvious issues
2. Check resource metrics (CPU, memory) if resource problems suspected
3. Search logs for error patterns
4. Check service endpoints and network connectivity
5. Synthesize findings into a root cause diagnosis

AVAILABLE COMMANDS (use the terminal tool):
- kubectl get pods -n {namespace}
- kubectl describe pod <name> -n {namespace}
- kubectl get events --sort-by='.lastTimestamp' -n {namespace}
- kubectl top pods -n {namespace}
- kubectl top nodes
- kubectl logs <pod-name> -n {namespace} --tail=50
- curl -s 'http://localhost:9090/api/v1/query?query=<promql>'  (Prometheus)
- curl -s 'http://localhost:9200/<index>/_search' -d '<json>'  (Elasticsearch)

{ssh_note}

After your investigation, provide a FINAL DIAGNOSIS in this exact JSON format:
```json
{{
    "root_cause": "specific root cause explanation",
    "confidence": 0.85,
    "fault_type": "resource_exhaustion|application_crash|configuration_error|network_issue|service_disruption|infrastructure|dependency_failure",
    "affected_services": ["service1"],
    "evidence_summary": {{
        "metrics": "what metrics showed",
        "logs": "what logs showed",
        "events": "what K8s events showed",
        "traces": "what traces showed"
    }},
    "reasoning_chain": "step-by-step reasoning from evidence to conclusion",
    "remediation_suggestion": "specific recommended fix"
}}
```

Be thorough and systematic. Use kubectl and other tools to gather real evidence."""


class HermesAgentBaseline:
    """
    Real Hermes Agent integration — runs NousResearch's AIAgent with terminal
    tools to investigate K8s incidents.

    Key characteristics (from Hermes Agent framework):
    - Single-agent ReAct loop with function calling
    - Terminal tool for running shell commands (kubectl, curl, etc.)
    - File tools for reading files
    - Web tools for HTTP requests
    - Delegate tool for spawning subagents
    - Memory system (MEMORY.md / USER.md)
    - Skills system (procedural memory)
    - Up to 90 iterations per conversation

    Differences from AgenticSRE:
    - Single general-purpose agent (not domain-specialized multi-agent)
    - No structured hypothesis generation/ranking/reranking
    - No cross-signal correlation engine (Metric × Log × Trace × Event)
    - No graph-based RCA localization
    - No RCA quality judge
    - No WeRCA-style continuous rule learning
    - No anomaly detection toolkit
    - No parallel domain agent execution
    """

    def __init__(self, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com/v1",
                 api_key: str = "",
                 max_iterations: int = 30,
                 toolsets: List[str] = None,
                 ssh_jump_host: str = "",
                 ssh_target: str = "",
                 use_ssh: bool = False):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.max_iterations = max_iterations
        self.toolsets = toolsets or ["terminal", "file"]
        self.ssh_jump_host = ssh_jump_host
        self.ssh_target = ssh_target
        self.use_ssh = use_ssh

        global _SSH_PREFIX
        if use_ssh and ssh_jump_host and ssh_target:
            _SSH_PREFIX = f"ssh -J {ssh_jump_host} {ssh_target}"

    async def diagnose(self, incident_query: str,
                       namespace: str = "") -> Dict[str, Any]:
        """
        Run real Hermes Agent for fault diagnosis.

        Returns standardized result dict with metrics.
        """
        start = time.time()

        # Build the SRE prompt
        ssh_note = ""
        if self.use_ssh and _SSH_PREFIX:
            ssh_note = (
                f"NOTE: The K8s cluster is accessed via SSH. Prefix all kubectl commands with:\n"
                f"  {_SSH_PREFIX}\n"
                f"Example: {_SSH_PREFIX} kubectl get pods -n {namespace or 'default'}"
            )

        system_prompt = SRE_SYSTEM_PROMPT.format(
            namespace=namespace or "default",
            ssh_note=ssh_note,
        )

        user_message = (
            f"INCIDENT: {incident_query}\n"
            f"NAMESPACE: {namespace or 'default'}\n\n"
            "Please investigate this incident using the available tools and provide "
            "your root cause analysis with evidence."
        )

        # Try real Hermes Agent first, fall back to subprocess
        result = await self._run_hermes_agent(system_prompt, user_message)

        latency = time.time() - start
        result["metrics"]["latency_s"] = round(latency, 2)
        return result

    async def _run_hermes_agent(self, system_prompt: str,
                                 user_message: str) -> Dict[str, Any]:
        """Run the real Hermes Agent via its Python API."""
        try:
            _ensure_hermes_importable()
            from run_agent import AIAgent
        except ImportError as e:
            logger.warning(f"Cannot import Hermes Agent: {e}, falling back to subprocess")
            return await self._run_hermes_subprocess(system_prompt, user_message)

        try:
            agent = AIAgent(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                max_iterations=self.max_iterations,
                enabled_toolsets=self.toolsets,
                save_trajectories=False,
                quiet_mode=True,
                ephemeral_system_prompt=system_prompt,
                skip_context_files=True,
                skip_memory=True,
            )

            result = agent.run_conversation(user_message)

            # Extract metrics
            final_response = result.get("final_response", "")
            messages = result.get("messages", [])
            api_calls = result.get("api_calls", 0)
            completed = result.get("completed", False)

            # Count tool calls from messages
            tool_call_count = 0
            tool_trace = []
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    for tc in (msg.get("tool_calls") or []):
                        fn = tc.get("function", {})
                        tool_call_count += 1
                        tool_trace.append({
                            "tool": fn.get("name", ""),
                            "args_preview": fn.get("arguments", "")[:100],
                        })

            # Extract token counts
            input_tokens = getattr(agent, "session_prompt_tokens", 0)
            output_tokens = getattr(agent, "session_completion_tokens", 0)

            # Parse diagnosis from the final response
            diagnosis = self._extract_diagnosis(final_response)

            return {
                "method": "hermes_agent",
                "status": "completed" if completed else "max_iterations",
                "diagnosis": diagnosis,
                "raw_response": final_response[:3000],
                "steps": api_calls,
                "tool_trace": tool_trace,
                "metrics": {
                    "latency_s": 0,  # filled by caller
                    "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
                    "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
                    "total_tokens": (input_tokens or 0) + (output_tokens or 0),
                    "llm_calls": api_calls,
                    "tool_calls": tool_call_count,
                },
            }

        except Exception as e:
            logger.error(f"Hermes Agent execution failed: {e}", exc_info=True)
            return {
                "method": "hermes_agent",
                "status": "error",
                "error": str(e),
                "diagnosis": {},
                "metrics": {
                    "latency_s": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                },
            }

    async def _run_hermes_subprocess(self, system_prompt: str,
                                      user_message: str) -> Dict[str, Any]:
        """
        Fallback: Run Hermes Agent as a subprocess if direct import fails.
        Uses the hermes CLI or python run_agent.py.
        """
        import asyncio
        import subprocess

        hermes_path = Path(HERMES_REPO)
        if not hermes_path.exists():
            hermes_path = Path("/tmp/hermes-agent")

        # Write a temporary script that runs the agent programmatically
        escaped_system = system_prompt.replace('"', '\\"').replace('\n', '\\n')
        escaped_user = user_message.replace('"', '\\"').replace('\n', '\\n')
        toolsets_json = json.dumps(self.toolsets)

        script = (
            f'import sys, json, os\n'
            f'sys.path.insert(0, "{hermes_path}")\n'
            f'os.environ.setdefault("OPENAI_API_KEY", "{self.api_key}")\n'
            f'\n'
            f'from run_agent import AIAgent\n'
            f'\n'
            f'agent = AIAgent(\n'
            f'    base_url="{self.base_url}",\n'
            f'    api_key="{self.api_key}",\n'
            f'    model="{self.model}",\n'
            f'    max_iterations={self.max_iterations},\n'
            f'    enabled_toolsets={toolsets_json},\n'
            f'    save_trajectories=False,\n'
            f'    quiet_mode=True,\n'
            f'    ephemeral_system_prompt="{escaped_system}",\n'
            f'    skip_context_files=True,\n'
            f'    skip_memory=True,\n'
            f')\n'
            f'\n'
            f'result = agent.run_conversation("{escaped_user}")\n'
            f'output = {{\n'
            f'    "final_response": result.get("final_response", ""),\n'
            f'    "api_calls": result.get("api_calls", 0),\n'
            f'    "completed": result.get("completed", False),\n'
            f'    "input_tokens": getattr(agent, "session_prompt_tokens", 0),\n'
            f'    "output_tokens": getattr(agent, "session_completion_tokens", 0),\n'
            f'}}\n'
            f'print("HERMES_RESULT:" + json.dumps(output, ensure_ascii=False, default=str))\n'
        )
        script_path = Path(f"/tmp/hermes_run_{uuid.uuid4().hex[:8]}.py")
        script_path.write_text(script)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(hermes_path),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300
            )
            stdout_text = stdout.decode("utf-8", errors="replace")

            # Parse result
            if "HERMES_RESULT:" in stdout_text:
                json_str = stdout_text.split("HERMES_RESULT:")[1].strip().split("\n")[0]
                data = json.loads(json_str)
                diagnosis = self._extract_diagnosis(data.get("final_response", ""))
                return {
                    "method": "hermes_agent",
                    "status": "completed" if data.get("completed") else "max_iterations",
                    "diagnosis": diagnosis,
                    "raw_response": data.get("final_response", "")[:3000],
                    "steps": data.get("api_calls", 0),
                    "metrics": {
                        "latency_s": 0,
                        "input_tokens": data.get("input_tokens", 0),
                        "output_tokens": data.get("output_tokens", 0),
                        "total_tokens": data.get("input_tokens", 0) + data.get("output_tokens", 0),
                        "llm_calls": data.get("api_calls", 0),
                        "tool_calls": 0,
                    },
                }
            else:
                return {
                    "method": "hermes_agent",
                    "status": "error",
                    "error": f"No result marker in output. stderr: {stderr.decode()[:500]}",
                    "diagnosis": {},
                    "metrics": {"latency_s": 0, "total_tokens": 0, "llm_calls": 0, "tool_calls": 0},
                }
        except asyncio.TimeoutError:
            return {
                "method": "hermes_agent",
                "status": "timeout",
                "error": "Hermes Agent subprocess timed out after 300s",
                "diagnosis": {},
                "metrics": {"latency_s": 300, "total_tokens": 0, "llm_calls": 0, "tool_calls": 0},
            }
        finally:
            script_path.unlink(missing_ok=True)

    def _extract_diagnosis(self, text: str) -> Dict:
        """Extract JSON diagnosis from Hermes Agent's final response."""
        if not text:
            return {"root_cause": "No response", "confidence": 0}

        # Try to find JSON block in markdown
        if "```json" in text:
            parts = text.split("```json")
            for part in parts[1:]:
                json_str = part.split("```")[0].strip()
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    continue

        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("{"):
                    try:
                        return json.loads(part)
                    except json.JSONDecodeError:
                        continue

        # Try to find raw JSON
        for i, ch in enumerate(text):
            if ch == "{":
                depth = 0
                for j in range(i, len(text)):
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[i:j+1])
                            except json.JSONDecodeError:
                                break
                break

        # Fallback: wrap the response as root_cause
        return {
            "root_cause": text[:500],
            "confidence": 0.3,
            "fault_type": "unknown",
            "reasoning_chain": text[:1000],
            "parse_error": True,
        }
