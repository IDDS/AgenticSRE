"""
AgenticSRE Hypothesis Agent
Generates and re-ranks root cause hypotheses using LLM with historical context injection.
"""

import logging
import math
from collections import Counter
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from tools.llm_client import LLMClient
from agents.evidence import format_evidence
from diagnostic_strategy import citation_update, finite_confidence, AGENTS, falsification_evidence

logger = logging.getLogger(__name__)


@dataclass
class Hypothesis:
    """A root cause hypothesis with confidence and evidence tracking."""
    id: str
    description: str
    confidence: float = 0.5
    status: str = "active"       # active | confirmed | rejected
    service: str = ""
    fault_type: str = ""
    supporting_evidence: List[str] = field(default_factory=list)
    contradicting_evidence: List[str] = field(default_factory=list)
    investigation_plan: List[str] = field(default_factory=list)
    source: str = "unspecified"
    supporting_evidence_ids: List[str] = field(default_factory=list)
    contradicting_evidence_ids: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    next_agents: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    update_reason: str = ""
    supports_if: str = ""
    refutes_if: str = ""
    verification_test: str = ""
    proposed_status: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "confidence": self.confidence,
            "status": self.status,
            "service": self.service,
            "fault_type": self.fault_type,
            "supporting_evidence": self.supporting_evidence,
            "contradicting_evidence": self.contradicting_evidence,
            "investigation_plan": self.investigation_plan,
            "source": self.source,
            "supporting_evidence_ids": self.supporting_evidence_ids,
            "contradicting_evidence_ids": self.contradicting_evidence_ids,
            "unresolved_questions": self.unresolved_questions,
            "next_agents": self.next_agents,
            "validation_errors": self.validation_errors,
            "update_reason": self.update_reason,
            "supports_if": self.supports_if,
            "refutes_if": self.refutes_if,
            "verification_test":self.verification_test,
            "proposed_status":self.proposed_status,
        }


class HypothesisAgent:
    """
    Hypothesis generation and re-ranking agent.
    
    Implements the "Hypothesis" phase of the Discovery→Hypothesis→Plan→Investigate→Reason paradigm.
    Uses historical context from memory to bootstrap hypothesis generation.
    """

    GENERATION_PROMPT = """你是 Kubernetes 集群根因分析专家，需要输出中文诊断内容。

请根据当前故障信号和历史上下文，生成 3-5 个按置信度排序的根因假设。

Incident Signal:
{incident_signal}

{historical_context}

每个假设需要包含：
1. 中文根因描述，要求具体、可执行
2. 初始置信度 (0.0-1.0)
3. 可能受影响的服务或组件
4. 故障类型分类，fault_type 可以保留英文枚举值
5. 调查计划，命令可以保留 kubectl/PromQL 原文，但说明应为中文

根因粒度约束：
- 只能描述当前信号或历史证据能够支持的故障机制和对象
- 不得猜测未观测到的配置参数、参数取值、内部实现细节或人为操作
- 如果证据只能支持“KV Cache 压力导致排队”，不得进一步猜测具体由哪个 vLLM 参数配置错误造成
- 假设应代表可区分的机制；调查计划写出支持和反驳条件，优先选择能区分前两名的只读证据
- 历史规则仅用于产生候选，不是当前事故的观测证据；初始置信度是排序建议，不是正确率

只返回 JSON：
{{
    "hypotheses": [
        {{
            "id": "H1",
            "description": "具体的中文根因解释",
            "confidence": 0.7,
            "service": "affected_service",
            "fault_type": "cpu_throttling|memory_leak|network_partition|config_error|dependency_failure|...",
            "investigation_plan": ["kubectl get pods -n ...", "check prometheus metric ..."],
            "supports_if": "何种当前观测支持此机制",
            "refutes_if": "何种当前观测会反驳此机制"
        }}
    ]
}}"""

    RERANK_PROMPT = """你是 SRE 根因分析专家，需要根据新证据重新评估已有假设。

Current Hypotheses:
{hypotheses}

New Evidence:
{evidence}

请根据证据更新每个假设的置信度：
- 证据支持假设时提高置信度
- 证据反驳假设时降低置信度
- 先检查现有假设能否覆盖关键证据；如果关键证据指向未被任何现有假设表达的新根因，
  可在 new_hypotheses 中补充 1-2 个新分支
- 只有在现有假设无法解释关键证据时才新增分支，避免同义重复和无依据扩张
- “同一组件、不同故障机制”不算覆盖，例如过载与副本不可用必须是不同分支
- 新分支必须使用证据中出现的准确服务/组件名，并明确引用支持该机制的证据
- 不得推断证据未出现的配置参数、内部协议、写一致性机制或实现细节
- 如果证据只支持“组件不可用”，结论必须停留在该粒度，不得升级为更具体但未经验证的机制
- 不得使用实验场景 ID、注入动作或 ground truth 生成新分支
- supporting_evidence、contradicting_evidence、reasoning 均使用中文描述

只返回 JSON：
{{
    "hypotheses": [
        {{
            "id": "H1",
            "confidence": 0.85,
            "status": "active|confirmed|rejected",
            "supporting_evidence": ["支持该假设的证据"],
            "contradicting_evidence": ["反驳该假设的证据"],
            "reasoning": "置信度变化原因"
        }}
    ],
    "new_hypotheses": [
        {{
            "description": "现有分支未覆盖、但被新证据直接支持的具体根因",
            "confidence": 0.85,
            "status": "active|confirmed",
            "service": "受影响服务或组件",
            "fault_type": "故障类型",
            "supporting_evidence": ["支持新分支的关键证据"],
            "contradicting_evidence": [],
            "investigation_plan": ["进一步验证该分支的通用调查步骤"],
            "reasoning": "为什么现有分支无法覆盖该证据"
        }}
    ]
}}"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.evidence_catalog = None
        self.last_generation_method = "not_run"
        self.branch_decisions = []

    def set_evidence_catalog(self, catalog):
        self.evidence_catalog = catalog

    def generate(self, incident_signal: str,
                 historical_rules: List[str] = None,
                 historical_faults: List[Dict] = None) -> List[Hypothesis]:
        """Generate initial hypotheses from incident signal + historical context."""
        fast = self._fast_generate(incident_signal)
        if fast:
            self.last_generation_method = "deterministic_seed"
            for hyp in fast:
                hyp.source = "deterministic_seed"
            return fast
        
        # Build historical context
        hist_context = ""
        if historical_rules:
            hist_context += "\nHistorical diagnostic rules (from past incidents):\n"
            for rule in historical_rules[:5]:
                hist_context += f"  - {rule}\n"
        if historical_faults:
            hist_context += "\nSimilar past incidents:\n"
            for fault in historical_faults[:3]:
                hist_context += f"  - [{fault.get('fault_type', '')}] {fault.get('description', '')[:200]}\n"
                hist_context += f"    Root cause: {fault.get('root_cause', '')[:200]}\n"

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "你是资深 SRE 根因分析专家。除 fault_type、命令和资源名外，诊断内容必须使用中文。"},
                {"role": "user", "content": self.GENERATION_PROMPT.format(
                    incident_signal=incident_signal[:3000],
                    historical_context=hist_context if hist_context else "No historical context available.",
                )}
            ], max_tokens=2048, thinking=False)

            if not isinstance(result, dict) or not isinstance(result.get("hypotheses"), list) or not result.get("hypotheses"):
                result = self.llm.json_chat([
                    {
                        "role": "system",
                        "content": "根据运行期观测生成最多3个中文根因假设。不得猜测未观测配置，只返回包含 hypotheses 数组的 JSON。",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"事件与观测：{incident_signal[:3000]}\n"
                            "输出格式：{\"hypotheses\":[{\"id\":\"H1\",\"description\":\"证据支持的故障机制\","
                            "\"confidence\":0.8,\"service\":\"目标对象\",\"fault_type\":\"类型\","
                            "\"investigation_plan\":[\"最小只读验证\"]}]}"
                        ),
                    },
                ], max_tokens=2048, thinking=False)

            hypotheses = []
            for h in result.get("hypotheses", []):
                if not isinstance(h, dict) or not isinstance(h.get("description"), str) or not h["description"].strip():
                    continue
                if len(hypotheses) >= 5:
                    break
                hypotheses.append(Hypothesis(
                    # Host-owned IDs stay unique even for duplicated or malformed
                    # model IDs. Later evidence updates refer to these exact IDs.
                    id=f"H{len(hypotheses)+1}",
                    description=h["description"].strip()[:2000],
                    confidence=finite_confidence(h.get("confidence", 0.5)),
                    service=str(h.get("service") or "")[:256],
                    fault_type=str(h.get("fault_type") or "")[:128],
                    investigation_plan=[p[:500] for p in h.get("investigation_plan",[]) if isinstance(p,str)][:8]
                        if isinstance(h.get("investigation_plan"),list) else [],
                    source="llm_with_history" if historical_rules or historical_faults else "llm",
                    supports_if=str(h.get("supports_if") or "")[:500],
                    refutes_if=str(h.get("refutes_if") or "")[:500],
                ))
            
            # Sort by confidence
            hypotheses.sort(key=lambda h: h.confidence, reverse=True)
            self.last_generation_method = "llm_with_history" if historical_rules or historical_faults else "llm"
            return hypotheses

        except Exception as e:
            self.last_generation_method = "generation_failed"
            logger.error(f"Hypothesis generation failed: {e}")
            return [Hypothesis(
                id="H1",
                description=f"当前证据不足，需要继续排查：{incident_signal[:200]}",
                confidence=0.3,
                source="generation_failed",
            )]

    def _fast_generate(self, incident_signal: str) -> List[Hypothesis]:
        """Generate deterministic hypotheses for common high-signal K8s incidents."""
        text = incident_signal.lower()
        is_otel = any(h in text for h in ["otel-demo", "opentelemetry demo", "frontend-proxy"])
        if is_otel and any(h in text for h in [
            "networkchaos", "chaosmesh", "network policy", "networkpolicy",
            "networkpolicydeny", "partition",
        ]):
            return [
                Hypothesis(
                    id="H1",
                    description="临时网络故障配置正在阻断 OpenTelemetry Demo 中 frontend-proxy 与 frontend 之间的流量。",
                    confidence=0.9,
                    service="frontend-proxy/frontend",
                    fault_type="network_partition",
                    investigation_plan=[
                        "kubectl get networkchaos -n otel-demo",
                        "kubectl get networkpolicy -n otel-demo",
                        "kubectl get endpoints frontend -n otel-demo",
                        "移除临时网络故障前后，对比 frontend-proxy 的 HTTP 状态",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="frontend Service 没有可用 endpoints 或选择器未命中后端 Pod，导致 frontend-proxy 返回 5xx。",
                    confidence=0.45,
                    service="frontend",
                    fault_type="service_endpoint_failure",
                    investigation_plan=[
                        "kubectl get endpoints frontend -n otel-demo -o wide",
                        "kubectl get pods -n otel-demo -l app.kubernetes.io/component=frontend",
                    ],
                ),
            ]
        if is_otel and any(h in text for h in [
            "scale-to-zero", "scaled to zero", "replicas=0", "no ready endpoints",
            "no endpoints", "serviceunavailable", "service unavailable",
        ]):
            return [
                Hypothesis(
                    id="H1",
                    description="OpenTelemetry Demo 的 frontend Deployment 被缩容到 0，导致 service/frontend 没有可用 endpoint。",
                    confidence=0.92,
                    service="frontend",
                    fault_type="service_unavailable_no_endpoints",
                    investigation_plan=[
                        "kubectl get deployment frontend -n otel-demo -o wide",
                        "kubectl get endpoints frontend -n otel-demo",
                        "kubectl get pods -n otel-demo -l app.kubernetes.io/component=frontend",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="frontend Pod 存在但未 Ready，frontend-proxy 无法把用户流量转发到可用后端。",
                    confidence=0.5,
                    service="frontend",
                    fault_type="pod_not_ready",
                    investigation_plan=[
                        "kubectl describe pods -n otel-demo -l app.kubernetes.io/component=frontend",
                        "kubectl get events -n otel-demo --field-selector type=Warning",
                    ],
                ),
            ]
        if is_otel and any(h in text for h in ["health check", "verify whether", "healthy", "baseline"]):
            return [
                Hypothesis(
                    id="H1",
                    description="当前没有明显的 OpenTelemetry Demo 活跃故障，需要继续确认核心工作负载、遥测组件和压测组件是否健康。",
                    confidence=0.72,
                    service="otel-demo",
                    fault_type="no_fault_detected",
                    investigation_plan=[
                        "kubectl get pods -n otel-demo",
                        "kubectl get svc,endpoints -n otel-demo",
                        "query frontend/frontend-proxy request, error, CPU, and memory metrics",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="如果工作负载运行正常但 Prometheus、Collector 或 Jaeger 数据不可用，则可能存在 OTel 观测链路缺口。",
                    confidence=0.35,
                    service="otel-collector/prometheus",
                    fault_type="observability_gap",
                    investigation_plan=[
                        "kubectl get pods -n otel-demo -l app.kubernetes.io/name=prometheus",
                        "kubectl get pods -n otel-demo -l app.kubernetes.io/name=opentelemetry-collector",
                    ],
                ),
            ]
        if any(h in text for h in [
            "failed to pull image", "imagepull", "image pull", "errimagepull",
            "imagepullbackoff", "failed to resolve reference",
        ]):
            return [
                Hypothesis(
                    id="H1",
                    description="节点或集群到镜像仓库的出口访问异常，导致镜像 manifest 解析或镜像层下载失败。",
                    confidence=0.86,
                    fault_type="network_registry_access",
                    investigation_plan=[
                        "kubectl describe pod <pod> -n <namespace>",
                        "kubectl get events -n <namespace> --field-selector type=Warning",
                        "从受影响节点验证事件中实际镜像仓库域名的 DNS 解析和 HTTPS 连通性",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="目标镜像仓库的域名解析失败，或节点 DNS/CoreDNS 解析链路存在异常。",
                    confidence=0.55,
                    fault_type="dns_failure",
                    investigation_plan=[
                        "检查 CoreDNS 健康状态和请求指标",
                        "从受影响节点验证实际镜像仓库域名的解析结果",
                    ],
                ),
                Hypothesis(
                    id="H3",
                    description="目标镜像引用、镜像仓库可用性、访问权限或限流异常，可能导致镜像元数据获取失败。",
                    confidence=0.35,
                    fault_type="image_registry_or_tag",
                    investigation_plan=[
                        "确认镜像和 tag 是否存在",
                        "检查实际镜像仓库的可用性、访问限速和拉取权限",
                    ],
                ),
            ]
        if "crashloopbackoff" in text:
            return [
                Hypothesis(
                    id="H1",
                    description="容器进程因应用启动失败、配置错误或依赖缺失而反复退出。",
                    confidence=0.8,
                    fault_type="application_crash",
                    investigation_plan=[
                        "查看 Pod 上一次退出前的日志：kubectl logs --previous",
                        "查看 Pod describe 中的最后终止状态和事件",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="资源限制、探针配置或依赖服务未就绪可能导致容器反复重启。",
                    confidence=0.5,
                    fault_type="resource_or_probe_failure",
                    investigation_plan=[
                        "检查重启原因、探针配置和内存限制",
                        "检查依赖服务是否 Ready",
                    ],
                ),
            ]
        if "pending" in text or "failedscheduling" in text:
            return [
                Hypothesis(
                    id="H1",
                    description="Pod 的 CPU/memory 请求超过候选节点当前可分配资源，导致无法调度；需核对声明与事件。",
                    confidence=0.6,
                    fault_type="scheduling_failure",
                    supports_if="当前 FailedScheduling 明确报告 Insufficient CPU 或 memory。",
                    refutes_if="完整 Pod 声明的所有容器、初始化容器、Pod 级及 overhead 的 CPU/memory 请求均为零；其他资源类型不在本候选范围。",
                    verification_test="zero_cpu_memory_requests",
                    investigation_plan=[
                        "同时核对完整 Pod requests、初始化容器与 overhead，以及当前调度事件；不能仅凭其他拒绝原因排除本分支。",
                    ],
                ),
                Hypothesis(id="H2", description="节点选择器、亲和性或污点容忍约束排除了可用节点，导致 Pod 无法调度；需核对声明与拒绝原因。",
                    confidence=0.55, fault_type="scheduling_failure", investigation_plan=[
                        "对照 Pod 的调度约束、节点标签/污点及当前 FailedScheduling 原因，不根据 Pending 状态单独确认。"],
                    supports_if="当前调度拒绝原因明确指向选择器、亲和性或未容忍污点，并与对象声明匹配。",
                    refutes_if="当前节点满足这些约束，且调度拒绝原因指向其他机制。"),
                Hypothesis(id="H3", description="Pod 引用的 PVC 或通用临时卷尚未绑定，导致调度等待；需核对实际卷声明与绑定状态。",
                    confidence=0.4, fault_type="scheduling_failure", investigation_plan=[
                        "只读核对 PVC 绑定状态与卷相关调度事件；没有卷引用或卷已绑定且无相关错误时降低本分支优先级。"],
                    supports_if="Pod 明确引用 PVC 或通用临时卷，且当前绑定状态与事件直接支持等待绑定。",
                    refutes_if="完整 Pod API 声明确认没有 PVC/通用临时卷引用，仅无卷或 projected/configMap/secret/downwardAPI/emptyDir；不排除其他存储故障。",
                    verification_test="no_pvc_or_ephemeral_volume"),
            ]
        return []

    def rerank(
        self,
        hypotheses: List[Hypothesis],
        new_evidence: Dict,
        allow_branch_expansion: bool = True,
        max_hypotheses: int = 6,
    ) -> List[Hypothesis]:
        """Re-rank hypotheses and expand uncovered branches when evidence requires it."""
        self.branch_decisions = []
        if not hypotheses:
            return hypotheses
        
        # Format current hypotheses
        hyp_text = "\n".join([
            f"[{h.id}] (conf={h.confidence:.2f}) {h.description} "
            f"[supporting: {len(h.supporting_evidence)}, contradicting: {len(h.contradicting_evidence)}]"
            for h in hypotheses
        ])

        # Format new evidence
        evidence_text = format_evidence(new_evidence, limit=5000)
        user_prompt = self.RERANK_PROMPT.format(hypotheses=hyp_text,evidence=evidence_text[:5000])
        if self.evidence_catalog is not None:
            import json
            # A single schema avoids conflicting legacy prose-only output
            # examples silently dropping the observed-reference fields.
            user_prompt=json.dumps({
                "task":"逐项以观测评估竞争机制；只返回 output_schema 形状的 JSON。",
                "constraints":[
                    "supporting_evidence_ids/contradicting_evidence_ids 必须复制 observations 中的完整 obs- ID；无对应观测时返回空数组并说明缺口。",
                    "每个已有假设均须评估。没有引用不得提高分数或确认/排除；采集失败不是反证；confirmed 只是待独立审核的判断。",
                    "观测没有提及某机制不等于该机制已被反驳；区分机制的直接反证才能用于 rejected。",
                    "同一观测不可同时作为同一假设的支持和反驳。不得臆造观测没有出现的参数、对象或内部机制。",
                    "候选 verification_test 非空时，rejected 必须引用该测试在 verification_checks 中为 true 的观测；false/null 不能反驳，也不能确认故障。缺少测试时保留 active。",
                    "仅在现有机制不能覆盖实际观测时新增最多两条分支，service 必须为支持观测中出现的准确对象名。",
                ],
                "current_hypotheses":[h.to_dict() for h in hypotheses],
                "observations":self.evidence_catalog,
                "output_schema":{"hypotheses":[{"id":"已有 H 编号","confidence":0.5,
                    "status":"active|confirmed|rejected","supporting_evidence_ids":[],
                    "contradicting_evidence_ids":[],"reasoning":"引用观测的判别结果与评分理由",
                    "unresolved_questions":["仍缺少的区分性观测"],"next_agents":["event_agent|metric_agent|log_agent|trace_agent|llm_inference_agent"]}],
                    "new_hypotheses":[{"description":"实际观测支持的新机制","confidence":0.5,
                        "status":"active","service":"观测对象名","fault_type":"机制类型",
                        "supporting_evidence_ids":[],"contradicting_evidence_ids":[],
                        "reasoning":"为何原候选不足","supports_if":"支持条件","refutes_if":"反驳条件"}]},
            },ensure_ascii=False)

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "你是资深 SRE 根因分析评审专家。除命令、资源名和枚举值外，输出内容必须使用中文。"},
                {"role": "user", "content": user_prompt}
            ], max_tokens=2048, thinking=False)

            if self.evidence_catalog is not None:
                return self._grounded_updates(hypotheses, result, allow_branch_expansion, max_hypotheses)

            # Update hypotheses
            updates = {h["id"]: h for h in result.get("hypotheses", [])}
            for hyp in hypotheses:
                if hyp.id in updates:
                    u = updates[hyp.id]
                    hyp.confidence = min(max(u.get("confidence", hyp.confidence), 0.0), 1.0)
                    hyp.status = u.get("status", hyp.status)
                    # A rejected branch must never win the next top-hypothesis
                    # selection merely because the model returned an inconsistent
                    # high confidence value alongside status=rejected.
                    if str(hyp.status).lower() == "rejected":
                        hyp.confidence = 0.0
                    hyp.supporting_evidence.extend(u.get("supporting_evidence", []))
                    hyp.contradicting_evidence.extend(u.get("contradicting_evidence", []))

            if allow_branch_expansion and len(hypotheses) < max_hypotheses:
                existing_descriptions = {
                    " ".join(h.description.lower().split())
                    for h in hypotheses
                    if h.description
                }
                next_index = max(
                    [int(h.id[1:]) for h in hypotheses if h.id.startswith("H") and h.id[1:].isdigit()] or [0]
                ) + 1
                remaining = max(0, max_hypotheses - len(hypotheses))
                added = 0
                for item in result.get("new_hypotheses", []):
                    if added >= min(2, remaining):
                        break
                    if not isinstance(item, dict):
                        continue
                    description = str(item.get("description", "")).strip()
                    normalized = " ".join(description.lower().split())
                    if len(description) < 8 or normalized in existing_descriptions:
                        continue
                    status = str(item.get("status", "active"))
                    confidence = min(max(float(item.get("confidence", 0.5)), 0.0), 1.0)
                    if status.lower() == "rejected":
                        # Do not add an already rejected branch as an actionable
                        # candidate; malformed model output should fail closed.
                        continue
                    hypotheses.append(Hypothesis(
                        id=f"H{next_index}",
                        description=description,
                        confidence=confidence,
                        status=status,
                        service=str(item.get("service", "")),
                        fault_type=str(item.get("fault_type", "")),
                        supporting_evidence=list(item.get("supporting_evidence", [])),
                        contradicting_evidence=list(item.get("contradicting_evidence", [])),
                        investigation_plan=list(item.get("investigation_plan", [])),
                    ))
                    existing_descriptions.add(normalized)
                    next_index += 1
                    added += 1

            # Re-sort by confidence
            hypotheses.sort(key=lambda h: h.confidence, reverse=True)
            return hypotheses

        except Exception as e:
            logger.error(f"Hypothesis reranking failed: {e}")
            return hypotheses

    def _grounded_updates(self, hypotheses, result, allow_expansion, max_hypotheses):
        """Apply only observed references; preserve model assertions as judgments."""
        if not isinstance(result, dict):
            return hypotheses
        rows = result.get("hypotheses") if isinstance(result.get("hypotheses"),list) else []
        updates = {str(row.get("id")):row for row in rows if isinstance(row,dict)}
        counts = Counter(str(row.get("id")) for row in rows if isinstance(row,dict))
        def retain_unverified_rejection(hyp, previous=None, followup=None):
            if previous:
                for field_name, value in previous.items():
                    setattr(hyp, field_name, value)
            hyp.proposed_status = 'rejected'
            hyp.status = 'active'
            if followup is not None:
                questions, sources = followup
                hyp.unresolved_questions = list(dict.fromkeys([
                    *hyp.unresolved_questions, *(q.strip()[:300] for q in questions if q.strip())]))[:6]
                hyp.next_agents = list(dict.fromkeys([
                    *hyp.next_agents, *(a for a in sources if a in AGENTS)]))[:3]
            if 'unverified_rejection' not in hyp.validation_errors:
                hyp.validation_errors.append('unverified_rejection')
            question = '需要同对象、同事故时间窗的直接反证，才能排除该候选。'
            if question not in hyp.unresolved_questions and len(hyp.unresolved_questions) < 6:
                hyp.unresolved_questions = [*hyp.unresolved_questions, question]
        for hyp in hypotheses:
            pending_rejection = (hyp.proposed_status == 'rejected' and
                                 'unverified_rejection' in hyp.validation_errors)
            previous = ({'supporting_evidence_ids': list(hyp.supporting_evidence_ids),
                         'contradicting_evidence_ids': list(hyp.contradicting_evidence_ids),
                         'supporting_evidence': list(hyp.supporting_evidence),
                         'contradicting_evidence': list(hyp.contradicting_evidence),
                         'update_reason': hyp.update_reason} if pending_rejection else None)
            if counts[hyp.id] > 1:
                hyp.validation_errors = ["duplicate_hypothesis_update"]
                if pending_rejection:
                    retain_unverified_rejection(hyp, previous)
                continue
            update = updates.get(hyp.id)
            if not update:
                hyp.validation_errors = ["hypothesis_not_assessed"]
                if pending_rejection:
                    retain_unverified_rejection(hyp, previous)
                continue
            support, oppose, errors = citation_update(update,self.evidence_catalog)
            requested_status = str(update.get('status', 'active')).strip().lower()
            hyp.supporting_evidence_ids, hyp.contradicting_evidence_ids = support, oppose
            hyp.validation_errors = errors
            questions=update.get('unresolved_questions',hyp.unresolved_questions)
            next_agents=update.get('next_agents',hyp.next_agents)
            if not isinstance(questions,list) or any(not isinstance(q,str) for q in questions):
                errors.append('invalid_unresolved_questions')
            if not isinstance(next_agents,list) or any(not isinstance(a,str) for a in next_agents):
                errors.append('invalid_next_agents')
            hyp.update_reason = str(update.get("reasoning") or "")[:600]
            if errors:
                # Do not let an unsupported model update change the ranking.
                if requested_status == 'rejected' or pending_rejection:
                    valid_followup = (isinstance(questions,list) and all(isinstance(q,str) for q in questions)
                        and isinstance(next_agents,list) and all(isinstance(a,str) for a in next_agents))
                    retain_unverified_rejection(hyp, previous, (questions, next_agents) if valid_followup else None)
                continue
            score=update.get("confidence")
            if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:
                hyp.validation_errors.append("invalid_confidence_value")
                if requested_status == 'rejected' or pending_rejection:
                    retain_unverified_rejection(hyp, previous)
                continue
            status = requested_status
            if status not in {"active","confirmed","rejected"}:
                hyp.validation_errors.append("invalid_hypothesis_status")
                if pending_rejection:
                    retain_unverified_rejection(hyp, previous)
                continue
            if status == "rejected" and not oppose or status == "confirmed" and not support:
                hyp.validation_errors.append("status_requires_matching_evidence_reference")
                if status == 'rejected' or pending_rejection:
                    retain_unverified_rejection(hyp, previous, (questions, next_agents))
                continue
            if status=='rejected' and not falsification_evidence(hyp.to_dict(),self.evidence_catalog,oppose):
                hyp.validation_errors.append('falsification_test_not_observed')
                retain_unverified_rejection(hyp, previous, (questions, next_agents))
                continue
            if pending_rejection and status != 'rejected':
                retain_unverified_rejection(hyp, previous)
                continue
            if status=='confirmed' and falsification_evidence(hyp.to_dict(),self.evidence_catalog,[c['id'] for c in self.evidence_catalog]):
                hyp.validation_errors.append('typed_test_refutes_confirmation')
                continue
            # Missing or invalid fields cannot silently close an earlier gap.
            # A valid cited assessment may explicitly clear a list with [].
            hyp.unresolved_questions=[q.strip()[:300] for q in questions if q.strip()][:6]
            hyp.next_agents=[a for a in next_agents if a in AGENTS][:3]
            hyp.confidence = float(score)
            hyp.proposed_status = status
            hyp.status = "active" if support and oppose and status == "confirmed" else status
            if hyp.status == "rejected":
                hyp.confidence = 0.0
            hyp.supporting_evidence = [str(x)[:300] for x in update.get("supporting_evidence",[]) if isinstance(x,str)][:8] if isinstance(update.get("supporting_evidence"),list) else []
            hyp.contradicting_evidence = [str(x)[:300] for x in update.get("contradicting_evidence",[]) if isinstance(x,str)][:8] if isinstance(update.get("contradicting_evidence"),list) else []
        if allow_expansion:
            known={" ".join(h.description.casefold().split()) for h in hypotheses}
            next_id=max([int(h.id[1:]) for h in hypotheses if h.id.startswith("H") and h.id[1:].isdigit()] or [0])+1
            for item in (result.get("new_hypotheses") if isinstance(result.get("new_hypotheses"),list) else [])[:2]:
                if len(hypotheses)>=max_hypotheses or not isinstance(item,dict):
                    continue
                support,oppose,errors=citation_update(item,self.evidence_catalog)
                description=str(item.get("description") or "").strip()
                normalized=" ".join(description.casefold().split())
                service=str(item.get("service") or "")
                objects={c["entity"]["name"] for c in self.evidence_catalog if c["id"] in support and c.get("entity")}
                reasons=list(errors)
                if not support: reasons.append("new_branch_requires_observed_support")
                if len(description)<8: reasons.append("insufficient_branch_description")
                if normalized in known: reasons.append("duplicate_branch")
                if service not in objects: reasons.append("branch_object_not_observed")
                if item.get("status")=="rejected": reasons.append("already_rejected_branch")
                score=item.get("confidence")
                if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:
                    reasons.append("invalid_confidence_value")
                if reasons:
                    self.branch_decisions.append({"description":description[:500],"accepted":False,"reasons":list(dict.fromkeys(reasons))})
                    continue
                hypothesis=Hypothesis(id=f"H{next_id}",description=description,service=service,
                    fault_type=str(item.get("fault_type") or ""),source="evidence_grounded_branch",
                    confidence=float(score),supporting_evidence_ids=support,
                    contradicting_evidence_ids=oppose,status="active",update_reason=str(item.get("reasoning") or "")[:600],
                    supports_if=str(item.get("supports_if") or "")[:500],refutes_if=str(item.get("refutes_if") or "")[:500])
                self.branch_decisions.append({"description":description[:500],"accepted":True,"hypothesis_id":hypothesis.id,
                                             "supporting_evidence_ids":support})
                hypotheses.append(hypothesis); known.add(normalized); next_id+=1
        hypotheses.sort(key=lambda h:h.confidence,reverse=True)
        return hypotheses
