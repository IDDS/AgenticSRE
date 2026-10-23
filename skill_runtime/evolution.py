"""Candidate skill generation from incidents and execution traces.

This module implements a conservative first step toward skill evolution. It
creates reviewable skill drafts, but never writes or enables new skills by
itself. A later LLM-backed generator can plug into the same contract.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml


def _slug(value: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip().lower())
    raw = re.sub(r"_+", "_", raw).strip("_.-")
    return raw or "unknown_fault"


def _domain(payload: Dict[str, Any]) -> str:
    text = " ".join(str(payload.get(k, "")) for k in ("domain", "fault_type", "message", "scenario")).lower()
    if "vllm" in text or "llm" in text or "gpu" in text:
        return "llm"
    if "k8s" in text or "pod" in text or "deployment" in text or "service" in text:
        return "k8s"
    return "generic"


def _risk(payload: Dict[str, Any]) -> str:
    text = " ".join(str(payload.get(k, "")) for k in ("fault_type", "message", "remediation")).lower()
    if any(token in text for token in ("node", "delete", "drain", "gpu", "vllm")):
        return "high"
    if any(token in text for token in ("restart", "rollout", "scale", "crash")):
        return "medium"
    return "low"


@dataclass
class SkillDraft:
    skill_id: str
    directory: str
    manifest: Dict[str, Any]
    skill_py: str
    tests: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    safety_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "directory": self.directory,
            "manifest": self.manifest,
            "manifest_yaml": yaml.safe_dump(self.manifest, sort_keys=False, allow_unicode=True),
            "skill_py": self.skill_py,
            "tests": self.tests,
            "provenance": self.provenance,
            "safety_notes": self.safety_notes,
        }


@dataclass
class VerificationFinding:
    severity: str
    code: str
    message: str
    path: str = ""
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "suggestion": self.suggestion,
        }


@dataclass
class SkillDraftVerification:
    status: str
    publish_ready: bool
    review_ready: bool
    findings: List[VerificationFinding] = field(default_factory=list)
    gates: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return {
            "status": self.status,
            "publish_ready": self.publish_ready,
            "review_ready": self.review_ready,
            "summary": _verification_summary(self.status, counts),
            "counts": counts,
            "findings": [finding.to_dict() for finding in self.findings],
            "gates": self.gates,
        }


def _verification_summary(status: str, counts: Dict[str, int]) -> str:
    if status == "blocked":
        return f"草案未通过发布门禁：{counts.get('error', 0)} 个错误，{counts.get('warning', 0)} 个警告。"
    if status == "review_required":
        return f"草案可进入人工评审：{counts.get('warning', 0)} 个警告，仍禁止自动发布。"
    return "草案通过静态检查，但生产发布仍需真实 dry-run/前向验证记录。"


class SkillDraftVerifier:
    """Static verifier for generated or imported skill drafts.

    The verifier is intentionally conservative. It cannot prove production
    safety; it only decides whether a draft is structurally reviewable and
    whether it must be blocked before human review.
    """

    _dangerous_code_tokens = (
        "os.system",
        "subprocess.",
        "eval(",
        "exec(",
        "pickle.",
        "shutil.rmtree",
        "socket.",
        "requests.",
        "urllib.request",
    )
    _dangerous_command_tokens = (
        "rm -rf",
        "mkfs",
        "fdisk",
        "dd if=",
        "shutdown",
        "reboot",
        "halt",
        "kill -9",
        "pkill",
        "wipefs",
        "format",
    )
    _secret_patterns = (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"(?i)(password|passwd|secret|token|access[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{6,}"),
    )

    def verify_draft(self, draft: Dict[str, Any] | SkillDraft) -> SkillDraftVerification:
        data = draft.to_dict() if isinstance(draft, SkillDraft) else dict(draft or {})
        manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else {}
        skill_py = str(data.get("skill_py") or "")
        tests = data.get("tests") if isinstance(data.get("tests"), list) else []
        safety_notes = data.get("safety_notes") if isinstance(data.get("safety_notes"), list) else []
        findings: List[VerificationFinding] = []

        self._check_manifest(manifest, findings)
        self._check_skill_py(skill_py, findings)
        self._check_tests(tests, findings)
        self._check_safety_notes(safety_notes, findings)

        error_count = sum(1 for finding in findings if finding.severity == "error")
        warning_count = sum(1 for finding in findings if finding.severity == "warning")
        review_ready = error_count == 0
        publish_ready = False
        status = "blocked" if error_count else "review_required"
        gates = {
            "static_structure": error_count == 0,
            "review_required": True,
            "auto_enable_allowed": False,
            "requires_forward_test": True,
            "requires_dry_run_or_real_experiment": True,
            "requires_human_approval": True,
            "warning_count": warning_count,
        }
        return SkillDraftVerification(
            status=status,
            publish_ready=publish_ready,
            review_ready=review_ready,
            findings=findings,
            gates=gates,
        )

    def _finding(
        self,
        findings: List[VerificationFinding],
        severity: str,
        code: str,
        message: str,
        *,
        path: str = "",
        suggestion: str = "",
    ) -> None:
        findings.append(
            VerificationFinding(
                severity=severity,
                code=code,
                message=message,
                path=path,
                suggestion=suggestion,
            )
        )

    def _check_manifest(self, manifest: Dict[str, Any], findings: List[VerificationFinding]) -> None:
        required_fields = ("id", "name", "domain", "stages", "risk", "permissions", "policy", "confidence")
        for field_name in required_fields:
            if not manifest.get(field_name):
                self._finding(findings, "error", "missing_manifest_field", f"manifest 缺少字段 {field_name}", path=f"manifest.{field_name}")

        stages = manifest.get("stages") if isinstance(manifest.get("stages"), list) else []
        missing_stages = [stage for stage in ("detect", "diagnose", "heal", "verify") if stage not in stages]
        if missing_stages:
            self._finding(
                findings,
                "error",
                "missing_lifecycle_stage",
                f"skill 生命周期不完整，缺少 {', '.join(missing_stages)}",
                path="manifest.stages",
                suggestion="补齐 detect/diagnose/heal/verify 四阶段，或明确拆成 tool skill。",
            )

        policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
        if policy.get("requires_approval") is not True:
            self._finding(
                findings,
                "error",
                "approval_not_required",
                "草案必须 requires_approval=true。",
                path="manifest.policy.requires_approval",
            )
        if policy.get("safe_auto_execute") is True:
            self._finding(
                findings,
                "error",
                "draft_auto_execute_enabled",
                "草案禁止 safe_auto_execute=true。",
                path="manifest.policy.safe_auto_execute",
            )

        permissions = [str(item) for item in (manifest.get("permissions") or [])]
        risk = str(manifest.get("risk") or "low")
        if any(token in permission for permission in permissions for token in (".write", ".admin", "delete", "root")):
            self._finding(
                findings,
                "warning",
                "write_permission_requires_gate",
                "草案包含写/管理权限，必须保留审批、回滚和验证门禁。",
                path="manifest.permissions",
            )
        if risk == "high":
            confidence = manifest.get("confidence") if isinstance(manifest.get("confidence"), dict) else {}
            auto_threshold = float(confidence.get("min_confidence_for_auto_heal") or 0)
            if auto_threshold < 0.9:
                self._finding(
                    findings,
                    "warning",
                    "high_risk_auto_threshold_low",
                    "高风险 skill 的全自动阈值不应低于 0.9。",
                    path="manifest.confidence.min_confidence_for_auto_heal",
                )

    def _check_skill_py(self, skill_py: str, findings: List[VerificationFinding]) -> None:
        if not skill_py.strip():
            self._finding(findings, "error", "missing_skill_py", "草案缺少 skill.py 内容。", path="skill_py")
            return
        try:
            tree = ast.parse(skill_py)
            compile(tree, "<skill-draft>", "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            self._finding(findings, "error", "invalid_python", f"skill.py 无法编译: {exc}", path="skill_py")
            return
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        for function_name in ("detect", "diagnose", "heal", "verify"):
            if function_name not in functions:
                self._finding(
                    findings,
                    "error",
                    "missing_stage_function",
                    f"skill.py 缺少 {function_name} 函数。",
                    path="skill_py",
                )
        lowered = skill_py.lower()
        for token in self._dangerous_code_tokens:
            if token.lower() in lowered:
                self._finding(
                    findings,
                    "error",
                    "dangerous_code_token",
                    f"skill.py 含高风险代码调用 {token}。",
                    path="skill_py",
                    suggestion="把外部执行移动到受控 runner，并使用 dry-run/审批门禁。",
                )
        for token in self._dangerous_command_tokens:
            if token in lowered:
                self._finding(
                    findings,
                    "error",
                    "dangerous_command_token",
                    f"skill.py 含危险命令片段 {token}。",
                    path="skill_py",
                )
        for pattern in self._secret_patterns:
            if pattern.search(skill_py):
                self._finding(
                    findings,
                    "error",
                    "embedded_secret",
                    "skill.py 疑似包含密钥、密码或 token。",
                    path="skill_py",
                    suggestion="密钥必须来自运行时环境或外部凭据管理。",
                )

    def _check_tests(self, tests: List[Any], findings: List[VerificationFinding]) -> None:
        joined = " ".join(str(item).lower() for item in tests)
        if len(tests) < 4:
            self._finding(findings, "warning", "insufficient_tests", "草案测试项不足。", path="tests")
        for required in ("detect", "diagnose", "heal", "verify"):
            if required not in joined:
                self._finding(
                    findings,
                    "warning",
                    "missing_test_area",
                    f"测试计划缺少 {required} 覆盖。",
                    path="tests",
                )
        if "dry-run" not in joined and "dry run" not in joined:
            self._finding(findings, "warning", "missing_dry_run_test", "测试计划缺少 dry-run 验证。", path="tests")
        if "前向" not in joined and "held-out" not in joined and "forward" not in joined:
            self._finding(findings, "warning", "missing_forward_test", "测试计划缺少独立样本前向测试。", path="tests")

    def _check_safety_notes(self, safety_notes: List[Any], findings: List[VerificationFinding]) -> None:
        joined = " ".join(str(item).lower() for item in safety_notes)
        if "safe_auto_execute=false" not in joined and "safe_auto_execute" not in joined:
            self._finding(findings, "warning", "missing_auto_execute_note", "安全说明应明确禁止自动启用。", path="safety_notes")
        if "人工" not in joined and "human" not in joined and "review" not in joined:
            self._finding(findings, "warning", "missing_review_note", "安全说明应明确人工评审要求。", path="safety_notes")

    def release_gate(self, draft: Dict[str, Any] | SkillDraft, evidence: Dict[str, Any]) -> Dict[str, Any]:
        verification = self.verify_draft(draft).to_dict()
        findings: List[VerificationFinding] = []
        evidence = dict(evidence or {})
        draft_data = draft.to_dict() if isinstance(draft, SkillDraft) else dict(draft or {})
        draft_sha256 = hashlib.sha256(json.dumps(
            draft_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        checks = {
            "draft_content_bound": evidence.get("draft_sha256") == draft_sha256,
            "static_verification": verification.get("review_ready") is True,
            "unit_tests_passed": evidence.get("unit_tests_passed") is True,
            "dry_run_passed": evidence.get("dry_run_passed") is True,
            "forward_tests_passed": evidence.get("forward_tests_passed") is True,
            "rollback_verified": evidence.get("rollback_verified") is True,
            "human_approved": evidence.get("human_approved") is True,
            "publish_target_set": bool(str(evidence.get("publish_target") or "").strip()),
        }
        for key, passed in checks.items():
            if passed:
                continue
            self._finding(
                findings,
                "error",
                f"release_gate_{key}_missing",
                _release_gate_message(key),
                path=f"evidence.{key}",
            )

        command = ""
        if all(checks.values()):
            draft_data = draft.to_dict() if isinstance(draft, SkillDraft) else dict(draft or {})
            directory = str(draft_data.get("directory") or draft_data.get("skill_id") or "candidate").replace(".", "_")
            command = f"reviewed manual publish to skill_runtime/skills/{directory}"

        status = "publish_ready" if all(checks.values()) else "blocked"
        return {
            "status": status,
            "draft_sha256": draft_sha256,
            "publish_ready": status == "publish_ready",
            "checks": checks,
            "verification": verification,
            "findings": [finding.to_dict() for finding in findings],
            "publish_plan": {
                "mode": "manual_reviewed_copy",
                "command": command,
                "auto_enable": False,
                "notes": [
                    "发布计划不会自动写入 skills 目录。",
                    "发布前应保留评审记录、测试输出、dry-run 输出和回滚验证记录。",
                    "发布后需重新执行 /api/skills/reload 并做独立前向验证。",
                ],
            },
        }


def _release_gate_message(key: str) -> str:
    messages = {
        "draft_content_bound": "测试和审批证据必须绑定当前草案 draft_sha256；内容修改后需重新验证和审批。",
        "static_verification": "草案静态校验未通过 review_ready。",
        "unit_tests_passed": "缺少单元/接口测试通过证据。",
        "dry_run_passed": "缺少 dry-run 验证通过证据。",
        "forward_tests_passed": "缺少独立样本前向测试通过证据。",
        "rollback_verified": "缺少回滚或恢复验证证据。",
        "human_approved": "缺少人工审批证据。",
        "publish_target_set": "缺少发布目标说明。",
    }
    return messages.get(key, f"缺少发布门禁证据: {key}")


class SkillDraftGenerator:
    def draft(self, payload: Dict[str, Any]) -> SkillDraft:
        incident = payload.get("incident") if isinstance(payload.get("incident"), dict) else payload
        fault_type = str(incident.get("fault_type") or incident.get("name") or "unknown_fault")
        domain = _domain(incident)
        risk = _risk(incident)
        skill_id = f"{domain}.{_slug(fault_type)}"
        directory = skill_id.replace(".", "_")
        evidence = incident.get("evidence") if isinstance(incident.get("evidence"), list) else []
        min_sources = max(1, min(3, len(evidence) or 2))
        manifest = {
            "id": skill_id,
            "version": "0.1.0-draft",
            "name": str(incident.get("title") or fault_type),
            "domain": domain,
            "description": "候选 skill，由故障样本和执行轨迹生成；遵循精简、渐进披露、验证先行原则，需人工评审后启用。",
            "stages": ["detect", "diagnose", "heal", "verify"],
            "risk": risk,
            "permissions": ["k8s.read"] if domain == "k8s" else ["host.read"],
            "tags": [domain, _slug(fault_type)],
            "policy": {
                "requires_approval": True,
                "safe_auto_execute": False,
            },
            "confidence": {
                "min_evidence_sources": min_sources,
                "min_confidence_for_heal": 0.85,
                "min_confidence_for_auto_heal": 0.9,
            },
        }
        tokens = [fault_type.lower(), str(incident.get("message") or "").lower()]
        token_expr = ", ".join(repr(t) for t in tokens if t)
        skill_py = f'''from __future__ import annotations


def detect(context):
    payload = context.get("payload") or {{}}
    text = " ".join(str(payload.get(k, "")) for k in ("fault_type", "message", "root_cause", "scenario")).lower()
    matched = any(token and token in text for token in [{token_expr}])
    return {{
        "status": "matched" if matched else "skipped",
        "confidence": 0.75 if matched else 0.0,
        "summary": "候选 skill 命中故障信号" if matched else "候选 skill 未命中",
        "evidence": ["候选规则来自故障复盘样本"] if matched else [],
    }}


def diagnose(context):
    payload = context.get("payload") or {{}}
    return {{
        "status": "ok",
        "confidence": 0.75,
        "summary": payload.get("root_cause") or "候选 skill 需要补充根因逻辑",
        "evidence": (context.get("detect") or {{}}).get("evidence", []),
        "fault_type": payload.get("fault_type") or "{fault_type}",
        "root_cause": payload.get("root_cause") or "draft root cause",
    }}


def heal(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "候选 skill 默认不生成真实自愈动作，需人工补充并评审。",
        "evidence": ["draft skill requires human review"],
        "suggestions": [],
    }}


def verify(context):
    return {{
        "status": "ok",
        "confidence": 0.5,
        "summary": "候选 skill 需要补充验证计划。",
        "evidence": ["draft verification placeholder"],
        "checks": [],
    }}
'''
        return SkillDraft(
            skill_id=skill_id,
            directory=directory,
            manifest=manifest,
            skill_py=skill_py,
            tests=[
                "补充 detect 命中/不命中测试",
                "补充 diagnose 证据链和置信度测试",
                "补充 heal dry-run、审批和回滚测试",
                "补充 verify 恢复判定测试",
                "用独立样本做前向测试，避免只对生成样本过拟合",
            ],
            provenance={
                "source": payload.get("source") or "incident_payload",
                "trace_count": len(payload.get("traces") or []),
                "feedback_count": len(payload.get("feedback") or []),
            },
            safety_notes=[
                "草案默认 requires_approval=true 且 safe_auto_execute=false。",
                "草案不会自动写入 skills 目录，也不会自动启用。",
                "必须通过单元测试、dry-run 和人工评审后才能发布。",
                "SKILL.md/说明应保持精简，详细知识放入 references 或脚本资源。",
            ],
        )
