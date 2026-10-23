from __future__ import annotations

import re
from typing import Any, Dict, List

from skill_runtime.precision_injection import PrecisionInjectionError, build_precision_contract


_SAFE_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
_SAFE_TARGET = re.compile(
    r"^(?:deployment|service|pod|node|process|gpu|volume)/[A-Za-z0-9][A-Za-z0-9._:@-]{0,191}$"
)
_OPERATIONS = {
    "fault_injection",
    "inject",
    "chaos",
    "chaos_experiment",
    "agenticsre.k8s.fault.inject",
}
_FORBIDDEN_KEYS = {"command", "commands", "shell", "script", "kubectl"}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") if isinstance(context.get("payload"), dict) else {}


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _contains_executable_input(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower() in _FORBIDDEN_KEYS or _contains_executable_input(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_executable_input(item) for item in value)
    return False


def _targets(data: Dict[str, Any]) -> List[str]:
    raw = data.get("target_entity_ids")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def _target_scopes(data: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = data.get("target_scopes")
    if not isinstance(raw, list):
        return []
    scopes: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        scope = {
            "namespace": str(item.get("namespace") or "").strip(),
            "target_entity_id": str(item.get("target_entity_id") or "").strip(),
        }
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def _precision_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data.get("precision"), dict):
        return {}
    return build_precision_contract(
        {"precision": data.get("precision")},
        target_entity_ids=_targets(data),
        fault_type=str(data.get("fault_type") or "FaultExperiment"),
        duration_s=_integer(data.get("duration_s"), 30),
        blast_radius_budget=data.get("blast_radius_budget") or {},
    )


def _validate(data: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    scenario_id = str(data.get("scenario_id") or "").strip()
    namespace = str(data.get("namespace") or "").strip()
    targets = _targets(data)
    target_scopes = _target_scopes(data)
    duration_s = _integer(data.get("duration_s"), 30)
    budget = data.get("blast_radius_budget") if isinstance(data.get("blast_radius_budget"), dict) else {}
    max_entities = _integer(budget.get("max_affected_entities"), 1)
    max_duration_s = _integer(budget.get("max_duration_s"), 120)

    if not scenario_id or not _SAFE_NAME.fullmatch(scenario_id):
        blockers.append("scenario_id 必须是已登记的安全场景 ID")
    if data.get("registered_scenario") is not True:
        blockers.append("故障注入只能引用服务端已登记场景")
    if not namespace or not _SAFE_NAME.fullmatch(namespace):
        blockers.append("namespace 必须明确且符合 Kubernetes 命名规则")
    if not targets or any(not _SAFE_TARGET.fullmatch(item) for item in targets):
        blockers.append("必须提供受限的 Kubernetes 目标实体")
    if not target_scopes or any(
        not _SAFE_NAME.fullmatch(item["namespace"])
        or not _SAFE_TARGET.fullmatch(item["target_entity_id"])
        for item in target_scopes
    ):
        blockers.append("每个目标必须绑定明确且安全的 Namespace 作用域")
    if list(dict.fromkeys(item["target_entity_id"] for item in target_scopes)) != targets:
        blockers.append("目标作用域与目标实体列表不一致")
    if max_entities < 1 or len(target_scopes) > max_entities:
        blockers.append(f"目标作用域数 {len(target_scopes)} 超过爆炸半径上限 {max_entities}")
    if duration_s < 1 or duration_s > max_duration_s:
        blockers.append(f"duration_s={duration_s} 超出 1..{max_duration_s} 秒范围")
    if data.get("auto_cleanup", True) is not True:
        blockers.append("故障注入必须启用 auto_cleanup")
    if data.get("approval_required", True) is not True:
        blockers.append("真实故障注入不得关闭审批门禁")
    if _contains_executable_input(data):
        blockers.append("注入契约不得包含 command/shell/script/kubectl")
    try:
        _precision_contract(data)
    except PrecisionInjectionError as exc:
        blockers.append(f"细粒度注入合同未通过预检: {exc}")
    return list(dict.fromkeys(blockers))


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    operation = str(data.get("operation") or data.get("intent") or "").strip().lower()
    text = " ".join(str(data.get(key) or "") for key in ("message", "query", "operation", "intent")).lower()
    matched = operation in _OPERATIONS or any(
        token in text for token in ("fault injection", "chaos experiment", "故障注入", "混沌实验", "故障演练")
    )
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.99 if matched else 0.0,
        "summary": "匹配 AgenticSRE Kubernetes 受控故障注入" if matched else "当前请求不是故障注入任务",
        "evidence": ["请求显式包含故障注入意图"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    blockers = _validate(data)
    targets = _targets(data)
    target_scopes = _target_scopes(data)
    if blockers:
        return {
            "status": "blocked",
            "confidence": 0.0,
            "summary": "故障注入契约未通过安全预检",
            "evidence": blockers,
            "fault_type": str(data.get("fault_type") or "FaultExperiment"),
            "root_cause": "实验计划缺少可验证的目标、时间、审批或回滚约束",
            "root_cause_component": targets[0] if targets else "",
            "blockers": blockers,
        }
    budget = data.get("blast_radius_budget") or {}
    return {
        "status": "ok",
        "confidence": 0.96,
        "summary": "已完成登记场景、目标、持续时间、爆炸半径、审批和自动清理预检。",
        "evidence": [
            f"场景 ID: {data.get('scenario_id')}",
            f"目标作用域数: {len(target_scopes)} / {budget.get('max_affected_entities', 1)}",
            f"持续时间: {_integer(data.get('duration_s'), 30)} 秒",
            "真实执行必须审批且绑定精确清理契约",
        ],
        "fault_type": str(data.get("fault_type") or "FaultExperiment"),
        "root_cause": "主动故障实验计划不向后续被动诊断泄露注入真值",
        "root_cause_component": targets[0],
        "blockers": [],
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    blockers = _validate(data)
    if blockers:
        return {
            "status": "blocked",
            "confidence": 0.0,
            "summary": "安全门禁已阻止生成注入操作",
            "evidence": blockers,
            "suggestions": [],
            "executed": False,
        }
    targets = _targets(data)
    target_scopes = _target_scopes(data)
    scenario_id = str(data.get("scenario_id"))
    namespace = str(data.get("namespace"))
    common = {
        "scenario_id": scenario_id,
        "cluster_id": str(data.get("cluster_id") or ""),
        "namespace": namespace,
        "target_entity_ids": targets,
        "target_scopes": target_scopes,
        "fault_type": str(data.get("fault_type") or "FaultExperiment"),
        "duration_s": _integer(data.get("duration_s"), 30),
        "blast_radius_budget": dict(data.get("blast_radius_budget") or {}),
        "abort_conditions": list(data.get("abort_conditions") or []),
        "auto_cleanup": True,
    }
    precision = _precision_contract(data)
    if precision:
        common["precision"] = precision
    rollback = {
        "operation": "agenticsre.k8s.fault.cleanup",
        "parameters": {
            "scenario_id": scenario_id,
            "cluster_id": common["cluster_id"],
            "namespace": namespace,
            "target_entity_ids": targets,
            "target_scopes": target_scopes,
        },
    }
    action = {
        "step": f"经审批后执行受控故障场景 {scenario_id}",
        "operation": "agenticsre.k8s.fault.inject",
        "parameters": common,
        "target_entity_id": targets[0],
        "risk": str(data.get("risk") or "high"),
        "requires_approval": True,
        "rollback": rollback,
        "verification": {
            "operation": "agenticsre.k8s.recovery.verify",
            "parameters": {
                "scenario_id": scenario_id,
                "cluster_id": common["cluster_id"],
                "namespace": namespace,
                "target_scopes": target_scopes,
                "compare_with_baseline": True,
                "required_precision_evidence": (precision.get("evidence") or {}).get("required_fields", []),
            },
        },
        "real_execution_ready": not precision or (
            (precision.get("execution") or {}).get("mode")
            == "approval_gated_transactional_adapter"
        ),
        "executable": True,
    }
    return {
        "status": "ok",
        "confidence": 0.96,
        "summary": "已生成强类型注入、清理和恢复验证契约；Skill 本身不执行命令。",
        "evidence": ["执行器按 scenario_id 重新解析服务端登记场景，不接受 Agent 任意 shell"],
        "suggestions": [action],
        "rollback": rollback,
        "executed": False,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    scenario_id = str(data.get("scenario_id") or "")
    namespace = str(data.get("namespace") or "")
    blocked = bool(_validate(data))
    precision = {}
    if not blocked:
        precision = _precision_contract(data)
    precision_checks = [
        f"agenticsre.precision.evidence:{field}"
        for field in (precision.get("evidence") or {}).get("required_fields", [])
    ]
    return {
        "status": "blocked" if blocked else "ok",
        "confidence": 0.0 if blocked else 0.92,
        "summary": "注入后隔离真值诊断，清理后对比故障前检查点。",
        "evidence": [] if blocked else ["验证涵盖故障效果、清理、工作负载和端点恢复"],
        "checks": [] if blocked else [
            f"agenticsre.k8s.fault.effect:{scenario_id}",
            f"agenticsre.k8s.fault.cleanup:{scenario_id}",
            f"agenticsre.k8s.deployment.ready:{namespace}",
            f"agenticsre.k8s.endpoint.restored:{namespace}",
            "agenticsre.k8s.baseline.compare",
            *precision_checks,
        ],
    }
