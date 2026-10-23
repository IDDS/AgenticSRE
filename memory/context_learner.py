"""
AgenticSRE Context Learner
Automatic rule extraction from RCA reasoning traces — WeRCA-style continuous learning.
"""

import logging
from typing import Any, Dict, List, Optional

from tools.llm_client import LLMClient
from memory.fault_context_store import FaultContextStore

logger = logging.getLogger(__name__)


class ContextLearner:
    """
    Extracts "If condition → then conclusion" diagnostic rules from RCA
    reasoning traces. Supports both auto-learn (no ground truth) and
    supervised (with ground truth) modes.
    
    SOW: "利用专家反馈、系统反馈、历史轨迹等重要信息，构建智能体上下文，实现多智能体运维能力的持续演化"
    """

    EXTRACT_PROMPT = """You are an expert SRE knowledge engineer.
Given the following RCA reasoning trace, extract reusable diagnostic rules.

Each rule should follow the pattern: "If <observable condition>, then <likely conclusion>"

RCA Reasoning Trace:
{reasoning_trace}

Root Cause Found: {root_cause}
Confidence: {confidence}

Extract 1-3 high-quality rules. Be specific about observable conditions.

Respond in JSON:
{{
    "rules": [
        {{
            "condition": "specific observable condition (metrics/logs/events pattern)",
            "conclusion": "likely root cause or diagnosis",
            "fault_type": "category of fault",
            "confidence": 0.8
        }}
    ]
}}"""

    SUPERVISED_PROMPT = """You are an expert SRE knowledge engineer.
Compare the agent's diagnosis with the ground truth and extract learning rules.

Agent Diagnosis:
{agent_diagnosis}

Ground Truth:
{ground_truth}

Was the diagnosis correct? Extract rules that capture:
- If correct: The successful diagnostic pattern
- If incorrect: What the agent should have looked for instead

Respond in JSON:
{{
    "correct": true,
    "rules": [
        {{
            "condition": "observable condition",
            "conclusion": "correct conclusion",
            "rule_type": "positive|negative",
            "lesson": "what to learn from this case",
            "confidence": 0.9
        }}
    ]
}}"""

    def __init__(self, llm: LLMClient, store: FaultContextStore, config=None):
        self.llm = llm
        self.store = store
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.auto_learn = cfg.memory.auto_learn

    def learn_from_trace(self, reasoning_trace: str, root_cause: str,
                          confidence: float, judge_level: int = 0, *, memory_scope=None,
                          source_incident_id: str = '') -> Dict:
        """
        Auto-learn mode: extract rules from an RCA reasoning trace.
        Blocked if judge_level >= 3 (low quality reasoning).
        """
        if not self.auto_learn:
            return {"status": "disabled", "rules_added": 0}
        
        try:
            numeric_judge_level = int(judge_level)
        except Exception:
            numeric_judge_level = 3

        if numeric_judge_level >= 3:
            logger.info("Auto-learning blocked: judge_level >= 3 (low quality)")
            return {"status": "blocked_by_judge", "rules_added": 0}

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE knowledge extraction expert."},
                {"role": "user", "content": self.EXTRACT_PROMPT.format(
                    reasoning_trace=reasoning_trace[:4000],
                    root_cause=root_cause[:500],
                    confidence=confidence,
                )}
            ])
            from memory.rule_lifecycle import valid_rules
            if not isinstance(result, dict) or not valid_rules(result.get('rules', [])):
                return {'status': 'invalid_rules', 'rules_added': 0, 'rules_activated': 0}
            rule_ids = []
            receipts = []
            for rule in result.get("rules", []):
                receipt = self.store.add_rule({
                    "condition": rule.get("condition", ""),
                    "conclusion": rule.get("conclusion", ""),
                    "fault_type": rule.get("fault_type", ""),
                    "namespace": "",
                    "confidence": rule.get("confidence", 0.5),
                    "source": "auto_learn",
                    "from_trace": reasoning_trace[:200],
                    "source_incident_id": source_incident_id,
                    **(memory_scope.source_fields() if memory_scope is not None else {}),
                }, return_receipt=True)
                if not isinstance(receipt, dict) or type(receipt.get('created')) is not bool:
                    raise ValueError('rule_write_receipt_required')
                if receipt['created'] and receipt.get('stored_status') != 'candidate':
                    raise ValueError('new_rule_must_remain_candidate')
                receipts.append(receipt)
                rule_ids.append(receipt['rule_id'])

            new_ids = [r['rule_id'] for r in receipts if r['created']]
            return {"status": "success", "rules_added": len(new_ids), 'rule_ids': rule_ids,
                    'candidate_rule_ids': new_ids, 'rule_results': receipts,
                    'rule_status': 'candidate' if new_ids else 'no_new_rules', 'rules_activated': 0}

        except Exception as e:
            logger.error('Auto-learning failed (%s)', type(e).__name__)
            return {"status": "error", "error": 'auto_learning_failed', 'rules_added': None,
                    'rules_activated': 0}

    def learn_supervised(self, agent_diagnosis: str, ground_truth: str, *, memory_scope=None,
                         source_incident_id: str = '', source_feedback_id: str = '') -> Dict:
        """
        Supervised learning: compare agent diagnosis with ground truth
        and extract positive/negative rules.
        """
        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE knowledge extraction expert."},
                {"role": "user", "content": self.SUPERVISED_PROMPT.format(
                    agent_diagnosis=agent_diagnosis[:3000],
                    ground_truth=ground_truth[:1000],
                )}
            ])

            # JSON strings such as "false" are truthy in Python. Validate the
            # complete verdict before writing even the first derived rule.
            correct = result.get("correct") if isinstance(result, dict) else None
            if type(correct) is not bool:
                return {"status": "invalid_correctness", "correct": None, "rules_added": 0}
            rules = result.get("rules", [])
            from memory.rule_lifecycle import valid_rules
            if not valid_rules(rules):
                return {"status": "invalid_rules", "correct": None, "rules_added": 0}
            rule_ids = []
            receipts = []
            
            for rule in rules:
                receipt = self.store.add_rule({
                    "condition": rule.get("condition", ""),
                    "conclusion": rule.get("conclusion", ""),
                    "rule_type": rule.get("rule_type", "positive" if correct else "negative"),
                    "lesson": rule.get("lesson", ""),
                    "confidence": rule.get("confidence", 0.5),
                    "source": "supervised",
                    "source_incident_id": source_incident_id,
                    "source_feedback_id": source_feedback_id,
                    **(memory_scope.source_fields() if memory_scope is not None else {}),
                }, return_receipt=True)
                if not isinstance(receipt, dict) or type(receipt.get('created')) is not bool:
                    raise ValueError('rule_write_receipt_required')
                if receipt['created'] and receipt.get('stored_status') != 'candidate':
                    raise ValueError('new_rule_must_remain_candidate')
                receipts.append(receipt)
                rule_ids.append(receipt['rule_id'])

            new_ids = [r['rule_id'] for r in receipts if r['created']]
            return {
                "status": "success",
                "correct": correct,
                "rules_added": len(new_ids),
                "rule_ids": rule_ids,
                "correct_source": "model_comparison_to_expert_text",
                'rule_status': 'candidate' if new_ids else 'no_new_rules', 'rules_activated': 0,
                'candidate_rule_ids': new_ids, 'rule_results': receipts,
            }

        except Exception as e:
            logger.error("Supervised learning failed (%s)", type(e).__name__)
            return {"status": "error", "error": "supervised_learning_failed", "correct": None}

    def store_fault_context(self, incident_query: str, rca_result: Dict,
                             evidence: Dict, hypotheses: List[Dict], *, memory_scope=None,
                             source_incident_id: str = '') -> str:
        """Store a complete fault context for future reference."""
        fault = {
            "description": incident_query[:500],
            "root_cause": rca_result.get("root_cause", ""),
            "confidence": rca_result.get("confidence", 0),
            "fault_type": rca_result.get("fault_type", ""),
            "source_incident_id": source_incident_id,
            'memory_status': 'candidate',
            **(memory_scope.source_fields() if memory_scope is not None else {}),
            "hypotheses": [h.get("description", "") for h in hypotheses[:5]],
            "evidence_summary": {
                agent: str(result.get("summary", ""))[:300]
                for agent, result in evidence.items()
            },
        }
        return self.store.add_fault(fault)
