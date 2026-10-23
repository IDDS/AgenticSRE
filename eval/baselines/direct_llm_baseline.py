"""
Direct LLM Baseline for Fault Diagnosis Comparison
Single-shot LLM call with all observability data packed into the prompt.
Supports DeepSeek V3 and Claude Opus 4.6 via OpenAI-compatible API.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


DIAGNOSIS_PROMPT = """You are an expert SRE performing root cause analysis on a Kubernetes cluster.

Given the following observability data snapshot from the cluster, diagnose the root cause of the incident.

Incident Description:
{incident}

{observability_data}

Analyze ALL the data above carefully and provide your diagnosis in JSON format:
{{
    "root_cause": "specific root cause explanation — be precise about what service/component is affected and why",
    "confidence": 0.85,
    "fault_type": "resource_exhaustion | application_crash | configuration_error | network_issue | service_disruption | infrastructure | dependency_failure | security_incident",
    "affected_services": ["service1", "service2"],
    "evidence_summary": {{
        "metrics": "key metric findings that support your diagnosis",
        "logs": "key log findings",
        "events": "key K8s event findings",
        "traces": "key trace findings"
    }},
    "reasoning_chain": "step-by-step reasoning from evidence to conclusion",
    "remediation_suggestion": "specific recommended fix",
    "timeline": [
        {{"time": "relative time", "event": "what happened"}}
    ]
}}"""


class DirectLLMBaseline:
    """
    Direct LLM call baseline — no Agent, no tools, no iteration.

    Packs all observability data into a single prompt and makes one LLM call.
    Measures: latency, token usage, and diagnostic quality.
    """

    def __init__(self, model: str, base_url: str, api_key: str,
                 name: str = "", temperature: float = 0.1,
                 max_tokens: int = 4096, timeout: int = 120):
        self.model = model
        self.name = name or model
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def diagnose(self, incident_query: str,
                       observability_snapshot: Dict) -> Dict[str, Any]:
        """
        Run single-shot diagnosis.

        Args:
            incident_query: Description of the incident.
            observability_snapshot: Output from ObservabilityCollector.collect().

        Returns:
            Standardized diagnosis result with metrics.
        """
        text_data = observability_snapshot.get("text_snapshot", "")

        # Truncate if too long for context window
        max_context = 30000
        if len(text_data) > max_context:
            text_data = text_data[:max_context] + "\n... [truncated]"

        prompt = DIAGNOSIS_PROMPT.format(
            incident=incident_query,
            observability_data=text_data,
        )

        start = time.time()
        input_tokens = 0
        output_tokens = 0

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system",
                     "content": "You are an expert SRE root cause analyst. Respond with valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            latency = time.time() - start
            content = resp.choices[0].message.content or ""

            if resp.usage:
                input_tokens = resp.usage.prompt_tokens
                output_tokens = resp.usage.completion_tokens

            # Parse JSON from response
            diagnosis = self._parse_json(content)

            return {
                "method": self.name,
                "status": "completed",
                "diagnosis": diagnosis,
                "raw_response": content[:2000],
                "metrics": {
                    "latency_s": round(latency, 2),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "llm_calls": 1,
                    "tool_calls": 0,
                },
            }

        except Exception as e:
            latency = time.time() - start
            logger.error(f"Direct LLM diagnosis failed: {e}")
            return {
                "method": self.name,
                "status": "error",
                "error": str(e),
                "diagnosis": {},
                "metrics": {
                    "latency_s": round(latency, 2),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "llm_calls": 1,
                    "tool_calls": 0,
                },
            }

    def _parse_json(self, text: str) -> Dict:
        """Extract JSON from LLM response, handling markdown code blocks."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            inside = False
            for line in lines:
                if line.strip().startswith("```") and not inside:
                    inside = True
                    continue
                elif line.strip() == "```" and inside:
                    break
                elif inside:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_response": text, "parse_error": True}
