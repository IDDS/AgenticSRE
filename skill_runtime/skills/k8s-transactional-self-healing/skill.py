from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List


_SAFE_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
_SAFE_TARGET = re.compile(
    r"^(?:deployment|service|pod|node|gpu|volume)/[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$"
)
_OPERATIONS = {
    "transactional_self_healing",
    "execute_undo",
    "agenticsre.k8s.remediation.plan_undo",
}
_CLEANUP_OPERATIONS = {
    "agenticsre.k8s.fault.cleanup",
    "agenticsre.precision.cleanup",
}
_EXECUTE_OPERATION = "agenticsre.k8s.remediation.execute_undo"
_VERIFY_OPERATION = "agenticsre.k8s.recovery.verify"
_AUTHORIZATION_BASIS = "approved_fault_transaction_undo"
_FORBIDDEN_KEYS = {"command", "commands", "shell", "script", "kubectl"}
_ALLOWED_STRATEGIES = {
    "restore_replica_count",
    "restore_deployment_image",
    "restore_container_command",
    "restore_resource_spec",
    "remove_network_fault",
    "remove_fault_helper",
    "execute_transaction_undo",
}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") if isinstance(context.get("payload"), dict) else {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower() in _FORBIDDEN_KEYS or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _undo(data: Dict[str, Any]) -> Dict[str, Any]:
    value = data.get("permitted_undo_action")
    return value if isinstance(value, dict) else {}


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


def _validate(data: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    diagnosis = data.get("diagnosis") if isinstance(data.get("diagnosis"), dict) else {}
    checkpoint = data.get("checkpoint") if isinstance(data.get("checkpoint"), dict) else {}
    undo = _undo(data)
    parameters = undo.get("parameters") if isinstance(undo.get("parameters"), dict) else {}
    scenario_id = str(data.get("scenario_id") or "").strip()
    cluster_id = str(data.get("cluster_id") or "").strip()
    namespace = str(data.get("namespace") or "").strip()
    targets = _targets(data)
    target_scopes = _target_scopes(data)
    strategy = data.get("remediation_strategy") if isinstance(data.get("remediation_strategy"), dict) else {}
    strategy_id = str(strategy.get("id") or "").strip()
    undo_targets = [str(item).strip() for item in parameters.get("target_entity_ids") or [] if str(item).strip()]
    undo_scopes = parameters.get("target_scopes") if isinstance(parameters.get("target_scopes"), list) else []
    confidence = _number(diagnosis.get("confidence"), 0.0)
    max_recovery_s = _integer(data.get("max_recovery_s"), 60)

    if not str(data.get("incident_id") or "").strip():
        blockers.append("incident_id 不能为空")
    if not scenario_id or not _SAFE_NAME.fullmatch(scenario_id):
        blockers.append("scenario_id 必须是已登记的安全场景 ID")
    if data.get("registered_scenario") is not True:
        blockers.append("事务式自愈只能使用已登记故障交易")
    if cluster_id and not _SAFE_NAME.fullmatch(cluster_id):
        blockers.append("cluster_id 不符合安全命名规则")
    if not namespace or not _SAFE_NAME.fullmatch(namespace):
        blockers.append("namespace 必须明确且符合 Kubernetes 命名规则")
    if not targets or any(not _SAFE_TARGET.fullmatch(item) for item in targets):
        blockers.append("必须提供受限的 Kubernetes 目标实体")
    if not target_scopes or any(
        not _SAFE_NAME.fullmatch(item["namespace"])
        or not _SAFE_TARGET.fullmatch(item["target_entity_id"])
        for item in target_scopes
    ):
        blockers.append("每个自愈目标必须绑定明确且安全的 Namespace 作用域")
    if list(dict.fromkeys(item["target_entity_id"] for item in target_scopes)) != targets:
        blockers.append("自愈目标作用域与目标实体列表不一致")
    if not diagnosis.get("fault_type") or not diagnosis.get("root_cause"):
        blockers.append("必须先有故障类型和根因诊断")
    if confidence < 0.8:
        blockers.append(f"诊断置信度 {confidence:.0%} 低于自愈规划阈值 80%")
    if strategy_id not in _ALLOWED_STRATEGIES:
        blockers.append("恢复策略不在强类型允许列表中")
    if str(strategy.get("execution_mode") or "") != "exact_approved_transaction_undo":
        blockers.append("恢复策略必须绑定精确已审批事务 Undo")
    if not checkpoint or not isinstance(checkpoint.get("summary"), dict):
        blockers.append("缺少可用于无回归验证的故障前检查点")
    if str(undo.get("operation") or "") not in _CLEANUP_OPERATIONS:
        blockers.append("只允许选择注入交易携带的强类型 cleanup/Undo")
    for key, expected in (("scenario_id", scenario_id), ("cluster_id", cluster_id), ("namespace", namespace)):
        if str(parameters.get(key) or "") != expected:
            blockers.append(f"Undo {key} 与诊断事件作用域不一致")
    if undo_targets != targets:
        blockers.append("Undo 目标与受控事件目标不一致")
    if undo_scopes != target_scopes:
        blockers.append("Undo 目标 Namespace 作用域与受控事件不一致")
    if str(data.get("authorization_basis") or "") != _AUTHORIZATION_BASIS:
        blockers.append("恢复缺少原故障交易的审批依据")
    if max_recovery_s < 5 or max_recovery_s > 120:
        blockers.append("max_recovery_s 必须限制在 5..120 秒")
    if _contains_forbidden_key(data):
        blockers.append("自愈契约不得包含 command/shell/script/kubectl")
    return list(dict.fromkeys(blockers))


def _checkpoint_fingerprint(data: Dict[str, Any]) -> str:
    checkpoint = data.get("checkpoint") if isinstance(data.get("checkpoint"), dict) else {}
    raw = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    operation = str(data.get("operation") or data.get("intent") or "").strip().lower()
    text = " ".join(str(data.get(key) or "") for key in ("message", "intent", "operation")).lower()
    matched = operation in _OPERATIONS or any(
        token in text for token in ("transactional self healing", "execute undo", "事务式自愈", "执行 undo")
    )
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.99 if matched else 0.0,
        "summary": "匹配 AgenticSRE Kubernetes 事务式自愈" if matched else "当前请求不是受控 Undo 自愈任务",
        "evidence": ["请求显式要求执行原故障交易 Undo"] if matched else [],
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    diagnosis = data.get("diagnosis") if isinstance(data.get("diagnosis"), dict) else {}
    blockers = _validate(data)
    if blockers:
        return {
            "status": "blocked",
            "confidence": 0.0,
            "summary": "事务式自愈契约未通过安全预检",
            "evidence": blockers,
            "fault_type": str(diagnosis.get("fault_type") or ""),
            "root_cause": str(diagnosis.get("root_cause") or ""),
            "root_cause_component": str(diagnosis.get("root_cause_component") or ""),
            "blockers": blockers,
        }
    return {
        "status": "ok",
        "confidence": min(0.99, max(0.9, _number(diagnosis.get("confidence"), 0.9))),
        "summary": "诊断、作用域、已审批 Undo、检查点和恢复时间窗均已通过预检。",
        "evidence": [
            f"诊断置信度: {_number(diagnosis.get('confidence')):.0%}",
            f"恢复策略: {(data.get('remediation_strategy') or {}).get('id')}",
            f"检查点: {_checkpoint_fingerprint(data)}",
        ],
        "fault_type": str(diagnosis.get("fault_type") or ""),
        "root_cause": str(diagnosis.get("root_cause") or ""),
        "root_cause_component": str(diagnosis.get("root_cause_component") or ""),
        "blockers": [],
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    blockers = _validate(data)
    if blockers:
        return {
            "status": "blocked",
            "confidence": 0.0,
            "summary": "安全门禁已阻止生成事务式自愈操作",
            "evidence": blockers,
            "suggestions": [],
            "executed": False,
        }
    undo = _undo(data)
    targets = _targets(data)
    target_scopes = _target_scopes(data)
    strategy = dict(data.get("remediation_strategy") or {})
    fingerprint = _checkpoint_fingerprint(data)
    action = {
        "step": f"执行 {strategy.get('name') or strategy.get('id')}，且不扩展原故障交易作用域",
        "operation": _EXECUTE_OPERATION,
        "parameters": {
            "incident_id": str(data.get("incident_id") or ""),
            "scenario_id": str(data.get("scenario_id") or ""),
            "cluster_id": str(data.get("cluster_id") or ""),
            "namespace": str(data.get("namespace") or ""),
            "target_entity_ids": targets,
            "target_scopes": target_scopes,
            "undo_action": undo,
            "checkpoint_fingerprint": fingerprint,
            "max_recovery_s": _integer(data.get("max_recovery_s"), 60),
        },
        "transaction_role": "mitigation_agent",
        "remediation_strategy": strategy,
        "authorization_basis": _AUTHORIZATION_BASIS,
        "risk": "high",
        "requires_approval": False,
        "verification": {
            "operation": _VERIFY_OPERATION,
            "parameters": {
                "scenario_id": str(data.get("scenario_id") or ""),
                "cluster_id": str(data.get("cluster_id") or ""),
                "namespace": str(data.get("namespace") or ""),
                "target_scopes": target_scopes,
                "checkpoint_fingerprint": fingerprint,
                "compare_with_baseline": True,
            },
        },
        "executable": True,
    }
    return {
        "status": "ok",
        "confidence": 0.98,
        "summary": "已生成绑定原交易 Undo 的强类型自愈契约；Skill 本身不执行命令。",
        "evidence": ["执行器重新解析已登记 scenario_id 的 cleanup，不接受任意 shell"],
        "suggestions": [action],
        "executed": False,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    data = _payload(context)
    blockers = _validate(data)
    namespace = str(data.get("namespace") or "")
    fingerprint = _checkpoint_fingerprint(data) if isinstance(data.get("checkpoint"), dict) else ""
    return {
        "status": "blocked" if blockers else "ok",
        "confidence": 0.0 if blockers else 0.96,
        "summary": "Undo 后必须对比检查点并验证无回归。",
        "evidence": blockers or [f"检查点指纹: {fingerprint}"],
        "checks": [] if blockers else [
            f"agenticsre.k8s.deployment.ready:{namespace}",
            f"agenticsre.k8s.endpoint.restored:{namespace}",
            f"agenticsre.k8s.target_pods.healthy:{namespace}",
            f"agenticsre.k8s.helper_pods.cleared:{namespace}",
            f"agenticsre.k8s.baseline.compare:{fingerprint}",
        ],
    }
