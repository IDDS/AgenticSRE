#!/usr/bin/env python3
"""
AgenticSRE Comparison Runner
Runs all 4 methods (AgenticSRE / DeepSeek V3 / Claude Opus / Hermes ReAct)
against the same fault scenarios and produces comparison reports.

Usage:
    python -m eval.comparison_runner                       # All tasks
    python -m eval.comparison_runner --task cpu-stress-001  # Single task
    python -m eval.comparison_runner --category resource    # By category
    python -m eval.comparison_runner --methods agenticsre,deepseek_v3  # Subset
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from configs.config_loader import get_config
from tools import build_tool_registry, LLMClient
from eval.observability_collector import ObservabilityCollector
from eval.baselines.direct_llm_baseline import DirectLLMBaseline
from eval.baselines.hermes_agent_baseline import HermesAgentBaseline

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
TASKS_FILE = EVAL_DIR / "eval_tasks.yaml"
SN_TASKS_FILE = EVAL_DIR / "fault_scenarios.yaml"  # Social Network scenarios


# ═══════════════════════════════════════════
#  Explainability & Remediation Judge
# ═══════════════════════════════════════════

JUDGE_PROMPT = """You are an expert evaluator of SRE root cause analysis quality.

Evaluate the following RCA output on two dimensions:

1. **Explainability** (0-10): How well does the reasoning chain explain the diagnosis?
   - 10: Clear causal chain, specific evidence cited, each step logically follows
   - 5: Some reasoning but vague, missing key evidence links
   - 0: No reasoning, just a conclusion

2. **Remediation Quality** (0-10): How actionable is the fix suggestion?
   - 10: Specific kubectl/config commands, addresses root cause, safe to execute
   - 5: General direction but not specific enough to execute
   - 0: No suggestion or completely wrong

RCA Output:
{rca_output}

Expected Root Cause Keywords: {expected_keywords}
Expected Fault Type: {expected_fault_type}

Respond in JSON:
{{
    "explainability_score": 7,
    "explainability_reason": "brief explanation",
    "remediation_score": 6,
    "remediation_reason": "brief explanation",
    "root_cause_match": true,
    "semantic_accuracy": 0.85
}}"""


# ═══════════════════════════════════════════
#  Comparison Runner
# ═══════════════════════════════════════════

class ComparisonRunner:
    """Orchestrates fault injection → data collection → 4-method diagnosis → evaluation."""

    def __init__(self, config=None, methods: List[str] = None, scenario_file: str = None):
        self.cfg = config or get_config()
        self.tasks_data = self._load_tasks(scenario_file)
        self.scoring = self.tasks_data.get("scoring", {})
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Build shared tool registry
        self.registry = build_tool_registry(self.cfg)
        self.llm = LLMClient(self.cfg.llm)
        self.collector = ObservabilityCollector(self.registry)

        # Initialize methods
        self.all_methods = self._init_methods()
        if methods:
            self.methods = {k: v for k, v in self.all_methods.items() if k in methods}
        else:
            self.methods = self.all_methods

    def _load_tasks(self, scenario_file: str = None) -> Dict:
        # Support both eval_tasks.yaml (key: tasks) and fault_scenarios.yaml (key: scenarios)
        if scenario_file:
            path = Path(scenario_file)
            if not path.is_absolute():
                path = EVAL_DIR / scenario_file
        else:
            path = TASKS_FILE
        with open(path) as f:
            data = yaml.safe_load(f)
        # Normalize: fault_scenarios.yaml uses "scenarios" key
        if "scenarios" in data and "tasks" not in data:
            data["tasks"] = data.pop("scenarios")
        return data

    def _init_methods(self) -> Dict[str, Any]:
        """Initialize all comparison methods."""
        methods = {}

        # 1. AgenticSRE (uses run_rca directly)
        methods["agenticsre"] = "pipeline"  # marker — handled specially

        # 2. DeepSeek V3 Direct (with full observability data)
        ds_key = os.environ.get("DEEPSEEK_API_KEY", self.cfg.llm.api_key)
        if ds_key:
            methods["deepseek_v3_direct"] = DirectLLMBaseline(
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                api_key=ds_key,
                name="deepseek_v3_direct",
            )

        # 3. DeepSeek V3 Blind (no observability data — only incident description)
        if ds_key:
            methods["deepseek_v3_blind"] = DirectLLMBaseline(
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                api_key=ds_key,
                name="deepseek_v3_blind",
            )

        # 4. Claude Opus 4.6 Direct
        claude_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if claude_key:
            methods["claude_opus_direct"] = DirectLLMBaseline(
                model="claude-opus-4-6-20250115",
                base_url="https://api.anthropic.com/v1",
                api_key=claude_key,
                name="claude_opus_direct",
                max_tokens=4096,
            )

        # 5. Hermes Agent (real NousResearch framework)
        ds_key_for_hermes = os.environ.get("DEEPSEEK_API_KEY", self.cfg.llm.api_key)
        methods["hermes_agent"] = HermesAgentBaseline(
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=ds_key_for_hermes,
            max_iterations=30,
            toolsets=["terminal", "file"],
            ssh_jump_host=self.cfg.kubernetes.ssh_jump_host,
            ssh_target=self.cfg.kubernetes.target_host,
            use_ssh=self.cfg.kubernetes.use_ssh,
        )

        return methods

    # ── Fault Injection ──

    def _run_commands(self, commands: List[str], method: str = "kubectl") -> bool:
        for cmd in commands:
            try:
                if method == "ssh" and self.cfg.kubernetes.use_ssh:
                    full = f"ssh -J {self.cfg.kubernetes.ssh_jump_host} {self.cfg.kubernetes.ssh_target} '{cmd}'"
                else:
                    full = cmd
                logger.info(f"  Exec: {full}")
                subprocess.run(full, shell=True, capture_output=True, text=True, timeout=60)
            except Exception as e:
                logger.error(f"  Command failed: {e}")
                return False
        return True

    # ── Run Single Method ──

    async def _run_agenticsre(self, task: Dict, namespace: str) -> Dict:
        """Run the full AgenticSRE pipeline."""
        from orchestrator.rca_engine import run_rca

        start = time.time()
        try:
            result = await run_rca(
                incident_query=task["description"],
                namespace=namespace,
                config=self.cfg,
                registry=self.registry,
            )
            latency = time.time() - start

            diagnosis = result.get("result", {})
            # Estimate tokens (AgenticSRE doesn't track per-call tokens natively)
            return {
                "method": "agenticsre",
                "status": result.get("status", "unknown"),
                "diagnosis": diagnosis,
                "steps": len(result.get("phases", [])),
                "iterations": result.get("iterations", []),
                "judge": result.get("judge", {}),
                "metrics": {
                    "latency_s": round(latency, 2),
                    "input_tokens": 0,  # not tracked per-call
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "llm_calls": len(result.get("phases", [])),
                    "tool_calls": sum(
                        len(it.get("evidence_agents", []))
                        for it in result.get("iterations", [])
                    ),
                },
            }
        except Exception as e:
            return {
                "method": "agenticsre",
                "status": "error",
                "error": str(e),
                "diagnosis": {},
                "metrics": {"latency_s": round(time.time() - start, 2)},
            }

    async def _run_hermes(self, agent: HermesAgentBaseline,
                          task: Dict, namespace: str) -> Dict:
        """Run the real Hermes Agent baseline."""
        return await agent.diagnose(task["description"], namespace)

    async def _run_direct_llm(self, baseline: DirectLLMBaseline,
                               task: Dict, snapshot: Dict) -> Dict:
        """Run a direct LLM baseline."""
        return await baseline.diagnose(task["description"], snapshot)

    # ── Evaluation ──

    def _evaluate_result(self, task: Dict, method_result: Dict, judge_result: Dict = None) -> Dict:
        """Score a method's diagnosis against expected outcomes.
        Uses both keyword matching AND LLM semantic matching for accuracy."""
        expected = task.get("expected", {})
        validation = task.get("validation", {})
        diagnosis = method_result.get("diagnosis", {})
        metrics = method_result.get("metrics", {})

        # Root cause keyword match
        root_cause_text = (diagnosis.get("root_cause", "") or "").lower()
        # Also check reasoning_chain and evidence for keywords
        reasoning_text = (diagnosis.get("reasoning_chain", "") or "").lower()
        evidence_text = json.dumps(diagnosis.get("evidence_summary", {}), default=str).lower()
        full_text = f"{root_cause_text} {reasoning_text} {evidence_text}"

        keywords = expected.get("root_cause_contains", [])
        keyword_match = any(kw.lower() in full_text for kw in keywords)

        # LLM semantic match (from judge_result)
        llm_match = False
        semantic_accuracy = 0.5
        if judge_result:
            llm_match = judge_result.get("root_cause_match_llm", False)
            semantic_accuracy = judge_result.get("semantic_accuracy", 0.5)

        # Combined accuracy: keyword OR LLM match
        is_accurate = keyword_match or llm_match

        # Fault type match
        expected_type = expected.get("fault_type", "").lower()
        actual_type = (diagnosis.get("fault_type", "") or "").lower()
        type_match = expected_type in actual_type if expected_type else False

        # Confidence
        confidence = diagnosis.get("confidence", 0)
        if isinstance(confidence, str):
            try:
                confidence = float(confidence)
            except ValueError:
                confidence = 0

        # Latency score (1.0 if within limit, decreasing after)
        max_time = validation.get("max_detection_time_s", 120)
        latency = metrics.get("latency_s", 999)
        latency_score = max(0, 1.0 - max(0, latency - max_time) / max_time)

        # Token efficiency (lower is better, normalized)
        total_tokens = metrics.get("total_tokens", 0)
        token_score = 1.0 / (1.0 + total_tokens / 10000) if total_tokens > 0 else 0.5

        # Has remediation
        has_remediation = bool(diagnosis.get("remediation_suggestion"))

        # Compute weighted score (use semantic_accuracy instead of binary match)
        w = self.scoring
        accuracy_score = max(semantic_accuracy, 1.0 if is_accurate else 0.0)
        score = (
            w.get("root_cause_match_weight", 0.3) * accuracy_score +
            w.get("confidence_weight", 0.3) * min(1.0, confidence) +
            w.get("detection_time_weight", 0.2) * latency_score +
            w.get("remediation_quality_weight", 0.2) * (1.0 if has_remediation else 0.0)
        )

        return {
            "keyword_match": keyword_match,
            "llm_match": llm_match,
            "is_accurate": is_accurate,
            "semantic_accuracy": round(semantic_accuracy, 3),
            "fault_type_match": type_match,
            "confidence": round(confidence, 3),
            "latency_s": round(latency, 2),
            "latency_score": round(latency_score, 3),
            "token_score": round(token_score, 3),
            "total_tokens": total_tokens,
            "has_remediation": has_remediation,
            "score": round(score, 3),
            "llm_calls": metrics.get("llm_calls", 0),
            "tool_calls": metrics.get("tool_calls", 0),
        }

    def _judge_quality(self, task: Dict, diagnosis: Dict) -> Dict:
        """Use LLM to judge explainability and remediation quality."""
        expected = task.get("expected", {})
        rca_text = json.dumps(diagnosis, indent=2, ensure_ascii=False, default=str)[:4000]

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an expert RCA quality evaluator."},
                {"role": "user", "content": JUDGE_PROMPT.format(
                    rca_output=rca_text,
                    expected_keywords=expected.get("root_cause_contains", []),
                    expected_fault_type=expected.get("fault_type", ""),
                )},
            ])
            return {
                "explainability": result.get("explainability_score", 5) / 10.0,
                "remediation_quality": result.get("remediation_score", 5) / 10.0,
                "semantic_accuracy": result.get("semantic_accuracy", 0.5),
                "root_cause_match_llm": result.get("root_cause_match", False),
            }
        except Exception as e:
            logger.warning(f"Quality judge failed: {e}")
            return {"explainability": 0.5, "remediation_quality": 0.5,
                    "semantic_accuracy": 0.5}

    # ── Run Single Task ──

    async def run_task(self, task: Dict) -> Dict:
        """Run all methods against a single fault scenario."""
        task_id = task["id"]
        inject = task.get("inject", {})
        method = inject.get("method", "kubectl")
        namespace = inject.get("namespace", "")

        logger.info(f"\n{'='*60}")
        logger.info(f"Task: {task_id} — {task['name']}")

        try:
            # 1. Inject fault
            logger.info("  Injecting fault...")
            self._run_commands(inject.get("commands", []), method)

            # 2. Wait for propagation
            logger.info("  Waiting for fault to propagate...")
            await asyncio.sleep(15)

            # 3. Collect observability snapshot (shared by baselines)
            logger.info("  Collecting observability data...")
            self.collector.namespace = namespace
            snapshot = self.collector.collect(task["description"])
            logger.info(f"  Data collected in {snapshot['collection_time_s']}s")

            # 4. Run each method
            results = {}
            for method_name, method_impl in self.methods.items():
                logger.info(f"  Running {method_name}...")
                try:
                    if method_name == "agenticsre":
                        raw = await self._run_agenticsre(task, namespace)
                    elif method_name == "deepseek_v3_blind" and isinstance(method_impl, DirectLLMBaseline):
                        # Blind mode: only incident description, no observability data
                        blind_snapshot = {"text_snapshot": "(No observability data available. Diagnose based on the incident description only.)"}
                        raw = await self._run_direct_llm(method_impl, task, blind_snapshot)
                    elif isinstance(method_impl, DirectLLMBaseline):
                        raw = await self._run_direct_llm(method_impl, task, snapshot)
                    elif isinstance(method_impl, HermesAgentBaseline):
                        raw = await self._run_hermes(method_impl, task, namespace)
                    else:
                        continue

                    # Judge quality first (need semantic_accuracy for scoring)
                    judge_result = self._judge_quality(task, raw.get("diagnosis", {}))
                    # Evaluate with judge result for semantic matching
                    eval_result = self._evaluate_result(task, raw, judge_result)

                    results[method_name] = {
                        **eval_result,
                        **judge_result,
                        "status": raw.get("status", "unknown"),
                        "diagnosis_summary": (raw.get("diagnosis", {}).get("root_cause", ""))[:300],
                        "raw_diagnosis": raw.get("diagnosis", {}),
                    }

                    logger.info(
                        f"    {method_name}: score={eval_result['score']:.3f} "
                        f"accurate={eval_result['is_accurate']} "
                        f"(kw={eval_result['keyword_match']} llm={eval_result.get('llm_match',False)}) "
                        f"latency={eval_result['latency_s']}s "
                        f"tokens={eval_result['total_tokens']}"
                    )

                except Exception as e:
                    logger.error(f"    {method_name} FAILED: {e}")
                    results[method_name] = {
                        "status": "error", "error": str(e), "score": 0,
                    }

            return {
                "task_id": task_id,
                "task_name": task["name"],
                "category": task.get("category", ""),
                "fault_type": task.get("fault_type", ""),
                "results": results,
            }

        finally:
            # Cleanup
            logger.info("  Cleaning up...")
            self._run_commands(inject.get("cleanup", []), method)
            await asyncio.sleep(5)

    # ── Run All Tasks ──

    async def run_all(self, task_filter: str = None,
                      category_filter: str = None) -> Dict:
        """Run comparison across all matching tasks."""
        tasks = self.tasks_data.get("tasks", [])
        if task_filter:
            tasks = [t for t in tasks if t["id"] == task_filter]
        if category_filter:
            tasks = [t for t in tasks if t.get("category") == category_filter]

        logger.info(f"Running {len(tasks)} tasks with {len(self.methods)} methods")
        logger.info(f"Methods: {list(self.methods.keys())}")

        all_results = []
        for task in tasks:
            result = await self.run_task(task)
            all_results.append(result)

        report = self._generate_report(all_results)

        # Save JSON
        json_path = RESULTS_DIR / f"comparison_{int(time.time())}.json"
        with open(json_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)

        # Save Markdown
        md_path = RESULTS_DIR / f"comparison_{int(time.time())}.md"
        md_content = self._generate_markdown(report)
        with open(md_path, "w") as f:
            f.write(md_content)

        logger.info(f"\nReports saved:")
        logger.info(f"  JSON: {json_path}")
        logger.info(f"  Markdown: {md_path}")

        return report

    # ── Report Generation ──

    def _generate_report(self, all_results: List[Dict]) -> Dict:
        """Generate comparison summary from all task results."""
        method_stats = {}
        for method_name in self.methods:
            scores = []
            latencies = []
            tokens = []
            explain = []
            remed = []
            matches = 0
            total = 0

            for task_result in all_results:
                r = task_result.get("results", {}).get(method_name, {})
                if "score" in r:
                    scores.append(r["score"])
                    latencies.append(r.get("latency_s", 0))
                    tokens.append(r.get("total_tokens", 0))
                    explain.append(r.get("explainability", 0.5))
                    remed.append(r.get("remediation_quality", 0.5))
                    if r.get("is_accurate", r.get("keyword_match")):
                        matches += 1
                    total += 1

            n = max(len(scores), 1)
            method_stats[method_name] = {
                "avg_score": round(sum(scores) / n, 3),
                "avg_latency_s": round(sum(latencies) / n, 2),
                "avg_tokens": round(sum(tokens) / n),
                "avg_explainability": round(sum(explain) / n, 3),
                "avg_remediation": round(sum(remed) / n, 3),
                "accuracy": round(matches / max(total, 1), 3),
                "tasks_run": total,
                "tasks_matched": matches,
            }

        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_tasks": len(all_results),
            "methods_compared": list(self.methods.keys()),
            "method_summary": method_stats,
            "per_task_results": all_results,
        }

    def _generate_markdown(self, report: Dict) -> str:
        """Generate Markdown comparison report."""
        lines = [
            "# AgenticSRE 故障诊断方法对比评测报告",
            "",
            f"**评测时间**: {report['timestamp']}",
            f"**故障场景数**: {report['total_tasks']}",
            f"**对比方法**: {', '.join(report['methods_compared'])}",
            "",
            "---",
            "",
            "## 总分对比",
            "",
            "| 方法 | 综合评分 | 准确率 | 平均延迟(s) | 平均Token | 可解释性 | 修复质量 |",
            "|------|---------|--------|------------|----------|---------|---------|",
        ]

        for method, stats in report.get("method_summary", {}).items():
            lines.append(
                f"| {method} | {stats['avg_score']:.3f} | "
                f"{stats['accuracy']:.1%} | {stats['avg_latency_s']:.1f} | "
                f"{stats['avg_tokens']} | {stats['avg_explainability']:.2f} | "
                f"{stats['avg_remediation']:.2f} |"
            )

        lines.extend(["", "---", "", "## 雷达图数据（用于绘图）", "", "```json"])
        radar_data = {}
        for method, stats in report.get("method_summary", {}).items():
            radar_data[method] = {
                "准确率": stats["accuracy"],
                "效率(延迟)": max(0, 1.0 - stats["avg_latency_s"] / 120),
                "Token效率": 1.0 / (1.0 + stats["avg_tokens"] / 10000),
                "可解释性": stats["avg_explainability"],
                "修复质量": stats["avg_remediation"],
            }
        lines.append(json.dumps(radar_data, indent=2, ensure_ascii=False))
        lines.extend(["```", ""])

        # Per-task details
        lines.extend(["---", "", "## 各场景详细对比", ""])
        for task_result in report.get("per_task_results", []):
            tid = task_result["task_id"]
            tname = task_result["task_name"]
            lines.append(f"### {tid}: {tname}")
            lines.append(f"**类别**: {task_result.get('category', '')} | "
                         f"**故障类型**: {task_result.get('fault_type', '')}")
            lines.append("")
            lines.append("| 方法 | 评分 | 根因匹配 | 置信度 | 延迟(s) | Token | 诊断摘要 |")
            lines.append("|------|-----|---------|--------|--------|-------|---------|")
            for method, r in task_result.get("results", {}).items():
                match_icon = "Y" if r.get("is_accurate", r.get("keyword_match")) else "N"
                lines.append(
                    f"| {method} | {r.get('score', 0):.3f} | {match_icon} | "
                    f"{r.get('confidence', 0):.2f} | {r.get('latency_s', 0):.1f} | "
                    f"{r.get('total_tokens', 0)} | {r.get('diagnosis_summary', '')[:80]} |"
                )
            lines.append("")

        # Summary
        lines.extend(["---", "", "## 结论", ""])
        best_method = max(
            report.get("method_summary", {}).items(),
            key=lambda x: x[1]["avg_score"],
            default=("N/A", {}),
        )
        lines.append(f"综合评分最高: **{best_method[0]}** ({best_method[1].get('avg_score', 0):.3f})")

        return "\n".join(lines)


# ═══════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AgenticSRE Comparison Runner")
    parser.add_argument("--task", help="Run specific task by ID")
    parser.add_argument("--category", help="Run tasks by category")
    parser.add_argument("--methods", help="Comma-separated method names to run")
    parser.add_argument("--scenarios", help="Scenario YAML file (default: eval_tasks.yaml, use 'sn' for fault_scenarios.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    methods = args.methods.split(",") if args.methods else None
    scenario_file = None
    if args.scenarios:
        scenario_file = "fault_scenarios.yaml" if args.scenarios == "sn" else args.scenarios
    runner = ComparisonRunner(methods=methods, scenario_file=scenario_file)
    asyncio.run(runner.run_all(task_filter=args.task, category_filter=args.category))


if __name__ == "__main__":
    main()
