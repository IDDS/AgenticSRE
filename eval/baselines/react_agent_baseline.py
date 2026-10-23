"""
ReAct Agent Baseline for Fault Diagnosis Comparison
Simulates Hermes Agent style: single Agent with ReAct loop (Think→Act→Observe).
No hypothesis management, no multi-agent, no cross-signal correlation, no memory.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


REACT_SYSTEM_PROMPT = """You are an expert SRE investigating a Kubernetes cluster incident.

You have access to the following tools:
{tool_descriptions}

Use the ReAct pattern to investigate:
1. THINK: Analyze what you know and decide what to investigate next
2. ACT: Call a tool to gather evidence
3. OBSERVE: Analyze the tool output

After gathering sufficient evidence, provide your final diagnosis.

IMPORTANT:
- Always start by checking K8s events and pod status
- Then check metrics if resource issues are suspected
- Then check logs for error patterns
- Be thorough but efficient — don't repeat the same queries

When you have enough evidence, output your final diagnosis with:
FINAL_DIAGNOSIS:
```json
{{
    "root_cause": "specific root cause",
    "confidence": 0.85,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{
        "metrics": "findings",
        "logs": "findings",
        "events": "findings",
        "traces": "findings"
    }},
    "reasoning_chain": "step by step reasoning",
    "remediation_suggestion": "fix"
}}
```"""

REACT_STEP_PROMPT = """Incident: {incident}
Namespace: {namespace}

Investigation so far:
{history}

Think about what you know and what you should investigate next.
Either:
1. Call a tool by responding with:
   TOOL_CALL: {{"tool": "tool_name", "args": {{"key": "value"}}}}
2. Or provide your final diagnosis with:
   FINAL_DIAGNOSIS:
   ```json
   {{ ... }}
   ```

What is your next step?"""


# Tool name → (method_name, arg_mapping) for each supported tool
TOOL_DISPATCH = {
    "kubectl": lambda reg, args: reg.execute(
        "kubectl",
        command=args.get("command", "get pods"),
        namespace=args.get("namespace", ""),
    ),
    "prometheus": lambda reg, args: reg.execute(
        "prometheus",
        query=args.get("query", ""),
        query_type=args.get("query_type", "instant"),
    ),
    "elasticsearch": lambda reg, args: reg.execute(
        "elasticsearch",
        query=args.get("query", "error"),
        level=args.get("level", "error"),
        time_range=args.get("time_range", "30m"),
        namespace=args.get("namespace", ""),
    ),
    "jaeger": lambda reg, args: reg.execute(
        "jaeger",
        service=args.get("service", ""),
        limit=args.get("limit", 10),
    ),
    "k8s_health": lambda reg, args: reg.execute(
        "k8s_health",
        component=args.get("component", "all"),
    ),
    "k8s_resource": lambda reg, args: reg.execute(
        "k8s_resource",
        resource_type=args.get("resource_type", "pods"),
        namespace=args.get("namespace", ""),
    ),
    "anomaly_detection": lambda reg, args: reg.execute(
        "anomaly_detection",
        data=args.get("data", []),
        method=args.get("method", "zscore"),
    ),
}


class ReActAgentBaseline:
    """
    Single-agent ReAct baseline — simulates Hermes Agent style diagnosis.

    Differences from AgenticSRE:
    - Single agent (no multi-agent collaboration)
    - No hypothesis generation/ranking
    - No cross-signal correlation engine
    - No persistent memory or learning
    - No quality judge
    - Sequential tool calls (no parallel domain agents)
    """

    def __init__(self, llm: LLMClient, registry: ToolRegistry,
                 max_steps: int = 15):
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps

    async def diagnose(self, incident_query: str,
                       namespace: str = "") -> Dict[str, Any]:
        """
        Run ReAct diagnosis loop.

        Returns standardized result with metrics.
        """
        start = time.time()
        total_input_tokens = 0
        total_output_tokens = 0
        tool_calls = 0
        llm_calls = 0
        history: List[str] = []

        # Build tool descriptions for system prompt
        tool_descs = self._build_tool_descriptions()
        system = REACT_SYSTEM_PROMPT.format(tool_descriptions=tool_descs)

        for step in range(self.max_steps):
            # Build step prompt
            history_text = "\n".join(history) if history else "(no investigation yet)"
            user_prompt = REACT_STEP_PROMPT.format(
                incident=incident_query,
                namespace=namespace or "default",
                history=history_text[-6000:],  # truncate history
            )

            # LLM call
            try:
                resp = self.llm.client.chat.completions.create(
                    model=self.llm.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                    max_tokens=2048,
                )
                llm_calls += 1
                content = resp.choices[0].message.content or ""

                if resp.usage:
                    total_input_tokens += resp.usage.prompt_tokens
                    total_output_tokens += resp.usage.completion_tokens

            except Exception as e:
                logger.error(f"ReAct LLM call failed at step {step}: {e}")
                history.append(f"[Step {step+1}] ERROR: LLM call failed: {e}")
                break

            # Check for final diagnosis
            if "FINAL_DIAGNOSIS:" in content:
                diagnosis = self._extract_diagnosis(content)
                latency = time.time() - start
                return {
                    "method": "hermes_react",
                    "status": "completed",
                    "diagnosis": diagnosis,
                    "steps": step + 1,
                    "history": history,
                    "metrics": {
                        "latency_s": round(latency, 2),
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "total_tokens": total_input_tokens + total_output_tokens,
                        "llm_calls": llm_calls,
                        "tool_calls": tool_calls,
                    },
                }

            # Check for tool call
            if "TOOL_CALL:" in content:
                tool_info = self._extract_tool_call(content)
                if tool_info:
                    tool_name = tool_info.get("tool", "")
                    tool_args = tool_info.get("args", {})

                    # Add namespace to args if not specified
                    if namespace and "namespace" not in tool_args:
                        tool_args["namespace"] = namespace

                    # Execute tool
                    tool_result = self._execute_tool(tool_name, tool_args)
                    tool_calls += 1

                    # Record in history
                    thinking = content.split("TOOL_CALL:")[0].strip()
                    result_text = str(tool_result.data)[:1500] if tool_result.success else f"ERROR: {tool_result.error}"
                    history.append(
                        f"[Step {step+1}] THINK: {thinking[:300]}\n"
                        f"  ACT: {tool_name}({json.dumps(tool_args)[:200]})\n"
                        f"  OBSERVE: {result_text[:500]}"
                    )
                else:
                    history.append(f"[Step {step+1}] Invalid tool call format: {content[:200]}")
            else:
                # Model is thinking without acting — record it
                history.append(f"[Step {step+1}] THINK: {content[:500]}")

        # Max steps reached — force a conclusion
        latency = time.time() - start
        diagnosis = self._force_conclusion(incident_query, history)
        llm_calls += 1

        return {
            "method": "hermes_react",
            "status": "max_steps_reached",
            "diagnosis": diagnosis,
            "steps": self.max_steps,
            "history": history,
            "metrics": {
                "latency_s": round(latency, 2),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "llm_calls": llm_calls,
                "tool_calls": tool_calls,
            },
        }

    def _build_tool_descriptions(self) -> str:
        """Build tool description text for the system prompt."""
        tools = self.registry.list_tools()
        lines = []
        for t in tools:
            name = t.get("name", "")
            desc = t.get("description", "")
            if name in TOOL_DISPATCH:
                lines.append(f"- {name}: {desc}")
        return "\n".join(lines) if lines else "(no tools available)"

    def _execute_tool(self, tool_name: str, args: Dict) -> ToolResult:
        """Execute a tool by name with given args."""
        dispatch_fn = TOOL_DISPATCH.get(tool_name)
        if dispatch_fn is None:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")
        try:
            return dispatch_fn(self.registry, args)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _extract_tool_call(self, content: str) -> Optional[Dict]:
        """Extract tool call JSON from LLM response."""
        try:
            idx = content.index("TOOL_CALL:")
            json_str = content[idx + len("TOOL_CALL:"):].strip()
            # Find JSON object
            start = json_str.index("{")
            depth = 0
            for i, ch in enumerate(json_str[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(json_str[start:i+1])
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse tool call: {e}")
        return None

    def _extract_diagnosis(self, content: str) -> Dict:
        """Extract final diagnosis JSON from LLM response."""
        try:
            idx = content.index("FINAL_DIAGNOSIS:")
            rest = content[idx + len("FINAL_DIAGNOSIS:"):]
            # Handle markdown code blocks
            if "```" in rest:
                parts = rest.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:]
                    part = part.strip()
                    if part.startswith("{"):
                        return json.loads(part)
            # Try direct JSON parse
            start = rest.index("{")
            depth = 0
            for i, ch in enumerate(rest[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(rest[start:i+1])
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse diagnosis: {e}")
        return {"raw_response": content[:1000], "parse_error": True}

    def _force_conclusion(self, incident: str, history: List[str]) -> Dict:
        """Force a conclusion when max steps is reached."""
        history_text = "\n".join(history[-5:])
        try:
            result = self.llm.json_chat([
                {"role": "system",
                 "content": "You are an SRE. Based on the investigation history, provide your best diagnosis."},
                {"role": "user",
                 "content": (
                     f"Incident: {incident}\n\n"
                     f"Investigation history:\n{history_text[:4000]}\n\n"
                     "Provide your diagnosis in JSON with: root_cause, confidence, "
                     "fault_type, affected_services, reasoning_chain, remediation_suggestion"
                 )},
            ])
            return result
        except Exception as e:
            return {"root_cause": "Investigation inconclusive", "confidence": 0.2,
                    "error": str(e)}
