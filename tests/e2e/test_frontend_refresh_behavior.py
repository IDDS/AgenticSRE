#!/usr/bin/env python3
"""Static regressions for SPA refresh behavior on form-heavy pages."""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_JS = ROOT / "web_app" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "web_app" / "templates" / "index.html"
STYLE_CSS = ROOT / "web_app" / "static" / "css" / "style.css"


def test_config_center_is_not_auto_refreshed() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    assert "function shouldAutoRefreshView(viewId)" in js
    assert "AUTO_REFRESH_VIEWS" in js
    assert "STATIC_REFRESH_VIEWS" in js
    assert "'configcenter'" in js
    assert "refreshCurrentView({ auto: true })" in js
    assert "document.hidden" in js
    assert "return AUTO_REFRESH_VIEWS.has(viewId)" in js
    for static_view in [
        "configcenter",
        "knowledge",
        "healcenter",
        "skillops",
        "reports",
        "faultlab",
        "llmfaultlab",
        "evolution",
    ]:
        assert f"'{static_view}'" in js
    for live_view in ["overview", "metrics", "alerts", "daemon", "aidashboard"]:
        assert f"'{live_view}'" in js


def test_navigation_and_dashboard_ui_are_reduced_noise() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert 'class="nav-advanced-group"' in html
    assert '<details>' in html
    assert "ops-quickbar" in html
    assert "ops-workbench-note" not in html
    assert 'id="sidebar-backdrop"' in html
    assert "data-view-link" not in html
    assert "高级工具" in html
    assert "updateAIDashboardTimeInputs" in js
    assert "style=\"display:none\"" in html
    assert "去分析分支" in js
    assert "去自愈分支" in js
    assert "仅展示前" in js
    assert ".nav-advanced-group" in css
    assert ".ops-quickbar" in css
    assert ".sidebar-backdrop" in css
    assert ".stat-grid-compact" in css
    assert ".aishperf-toolbar" in css


def test_general_alert_agent_skill_keeps_cluster_provenance() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'id="alert-cluster-filter"' in html
    assert "populateGeneralClusterDropdown('alert-cluster-filter')" in js
    assert "params.set('cluster_id', clusterId)" in js
    assert "cluster_id: a?.cluster_id || document.getElementById('alert-cluster-filter')?.value || ''" in js
    assert "cluster_id: options.skillWorkflow?.payload?.cluster_id || options.clusterId || ''" in js
    assert "fault_run_id: result?.fault_run_id || ''" in js


def test_skill_ops_is_not_auto_refreshed_and_has_closed_loop_ui() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'data-view="skillops"' in html
    assert 'id="view-skillops"' in html
    assert 'id="skill-result-summary"' in html
    assert 'id="skill-run-list"' in html
    assert 'id="skill-flow-strip"' in html
    assert 'id="skill-catalog-grid"' in html
    assert 'id="skill-catalog-search"' in html
    assert 'id="skill-detail-modal"' in html
    assert 'id="skill-detail-modal-body"' in html
    assert 'class="skillops-workbench"' in html
    assert 'id="skillops-live-summary"' in html
    assert "Skill 运维工作台" in html
    assert "事件接入" in html
    assert "Skill 检测" in html
    assert "诊断融合" in html
    assert "独立调试" in html
    assert "默认纯 Skill 链路" in html
    assert 'id="skill-legacy-fallback" type="checkbox">' in html
    assert 'data-skill-tab="run"' in html
    assert 'id="skillops-tab-catalog"' in html
    assert 'id="skill-catalog-purpose"' in html
    assert "skillops" in js
    assert "/api/skill-workflows/skills" in js
    assert "/api/skill-workflows/runs" in js
    assert "runSkillWorkflow" in js
    assert "openSkillHealPlan" in js
    assert "loadSkillWorkflowRuns" in js
    assert "renderSkillFlowStrip" in js
    assert "renderSkillCatalog" in js
    assert "renderSkillOpsLiveSummary" in js
    assert "renderSkillWorkflowJourney" in js
    assert "skillPurposeKey" in js
    assert "skillCatalogIcon" in js
    assert "openSkillDetailModal" in js
    assert "renderSkillDetailModalBody" in js
    assert "skillDetailCache" in js
    assert "skillRunDetailCache" in js
    assert "source_status" in js
    assert "renderSkillSourcePanel" in js
    assert "renderSimpleMarkdown" in js
    assert "skill.py" in js
    assert "skill.yaml" in js
    assert "switchSkillOpsTab" in js
    assert "checked === true" in js
    assert "Skill 调用失败" in js
    assert "skill_runtime_workflow" not in js
    assert "个 Skill 可调用" in js
    assert "toolset.skill_tool_count" in js
    assert ".skillops-workbench" in css
    assert ".skillops-route-line" in css
    assert ".skillops-live-card" in css
    assert ".skillops-mode-note" in css
    assert ".skill-journey" in css
    assert ".skill-result-layout" in css
    assert ".skill-run-item" in css
    assert ".skill-flow-step" in css
    assert ".skill-raw-fold" in css
    assert ".skill-catalog-card" in css
    assert ".skill-catalog-more" in css
    assert ".skill-detail-modal" in css
    assert ".skill-markdown-view" in css
    assert ".skill-source-view" in css
    assert ".skillops-tab" in css


def test_config_center_dirty_state_prevents_navigation_reload() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    assert "state.configCenterDirty = true" in js
    assert "state.loadedViews.configcenter && state.configCenterDirty" in js
    assert "已跳过自动刷新，避免覆盖当前输入" in js
    assert "state.loadedViews.configcenter && options.navigation" in js


def test_config_center_refresh_button_is_explicit_force_refresh() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'onclick="loadConfigCenter({force:true})"' in html


def test_config_center_has_status_strip_and_collapsed_docs() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'id="configcenter-status-strip"' in html
    assert "function renderConfigCenterStatusStrip(data)" in js
    assert 'id="configcenter-cluster-summary"' in html
    assert 'onclick="openFaultTargetManager()"' in html
    assert "kubectl" in html
    assert "tgt-k8s-kubectl" in js
    assert "function renderConfigCenterClusterSummary()" in js
    assert "await loadFaultTargets()" in js
    assert ".config-cluster-panel" in css
    assert '<details class="config-docs">' in html
    assert ".config-status-strip" in css


def test_llm_fault_lab_has_grouped_action_panel() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'class="fault-action-panel"' in html
    assert 'class="fault-action-group"' in html
    assert "fault-action-title" in html
    assert ".fault-action-panel" in css


def test_fault_lab_targets_are_discovered_selectors() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert '<select id="faultlab-ns-override"' in html
    assert '<select id="llmfaultlab-model"' in html
    assert '<select id="llmfaultlab-namespace"' in html
    assert '<select id="llmfaultlab-deployment"' in html
    assert '<select id="llmfaultlab-service"' in html
    assert 'id="llmfaultlab-active-run"' in html
    assert 'id="llmfaultlab-recover-selected"' in html
    assert "function replaceSelectOptions" in js
    assert "function populateFaultLabNamespaceOptions" in js
    assert "function populateLLMFaultTargetSelectors" in js
    assert "function loadActiveLLMFaultRuns" in js
    assert "function applySelectedLLMFaultRun" in js
    assert "async function cleanupSelectedLLMFaultRun" in js
    assert "/api/fault-runs?active_only=true" in js
    assert "body.fault_run_id = faultRun.id" in js
    assert "`/api/llm-faultlab/cleanup/${s.id}`" in js
    assert "timeoutMs: 180000" in js
    assert "function llmFaultModelForDeployment" in js
    assert "function onLLMFaultNamespaceChange" in js
    assert "function onLLMFaultDeploymentChange" in js
    assert "record.app === target" in js
    assert "function isAIServingPod" in js
    assert "const displayServices =" in js
    assert "/api/cluster/topology?cluster_id=" in js
    assert "let _llmFaultTopologyRequestSeq = 0" in js
    assert "requestSeq !== _llmFaultTopologyRequestSeq" in js
    assert "cachedClusterId === String(clusterId)" in js
    assert "const k8sTopologyData = targetTopology" in js
    assert "const dashboardPromise = api(" in js
    assert "const initialData = cachedData || k8sTopologyData" in js
    assert "基础拓扑已加载" in js
    assert "const freshData = await dashboardPromise" in js
    assert "使用最近拓扑" in js
    assert "K8s 拓扑" in js
    assert "body.deployment = document.getElementById('llmfaultlab-deployment')?.value || 'vllm-server'" not in js
    assert ".fault-field .select-sm:disabled" in css


def test_general_fault_lab_uses_tabbed_topology_workbench() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'data-faultlab-tab="single"' in html
    assert 'id="faultlab-tab-sequence"' in html
    assert 'data-faultlab-tab="precision"' in html
    assert 'id="faultlab-tab-precision"' in html
    assert 'id="faultlab-tab-catalog"' in html
    assert 'id="faultlab-topology"' in html
    assert 'id="faultlab-skill-result"' in html
    assert 'id="k8s-fault-exp-summary"' in html
    assert "可用性冒烟" in html
    assert "综合回归" in html
    assert "Agent+Skill 诊断" in html
    assert "Agent+Skill 闭环" in html
    assert "function switchFaultLabTab(tab)" in js
    assert "function loadFaultLabTopology()" in js
    assert "function applyK8sFaultExperimentPreset" in js
    assert "function renderK8sFaultExperimentSummary" in js
    assert "function faultLabSkillWorkflowBody" in js
    assert "function runFaultLabSkillWorkflow" in js
    assert "function renderFaultLabSkillResult" in js
    assert "normalizeK8sSkillFaultType" in js
    assert "openFaultLabTargetPicker" in js
    assert "cluster/topology?cluster_id=" in js
    assert "'faultlab'" in js
    assert ".general-fault-topology-canvas" in css
    assert ".fault-exp-summary" in css
    assert ".fault-exp-step-flow" in css


def test_llm_fault_lab_sequence_has_practical_presets() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    assert 'id="llmfaultlab-tab-sequence"' in html
    assert 'id="fault-exp-summary"' in html
    assert "推理冒烟" in html
    assert "性能退化" in html
    assert "GPU 压力" in html
    assert "function applyLLMFaultExperimentPreset" in js
    assert "function renderFaultExperimentSummary" in js
    assert "function clearFaultExperimentSteps" in js


def test_alert_center_has_analysis_and_healing_branches() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "runAgentAnalysisFromAlert" in js
    assert "runSkillAnalysisFromAlert" in js
    assert "runAgentHealingFromAlert" in js
    assert "runSkillHealingFromAlert" in js
    assert "启动智能体分析" in js
    assert "Agent+Skill 分析" in js
    assert "生成智能体自愈" in js
    assert "启动 Agent+Skill 自愈" in js
    assert "在下方选择多智能体或 Agent+Skill 分析链路" in js
    assert "启动 Agent+Skill 分析" in js
    assert "启动 Agent+Skill 自愈" in js
    assert "alert-pagination" in html
    assert "renderAlertPagination" in js
    assert "setAlertPageSize" in js
    assert "renderAgentSkillCompactProgress" in js
    assert "agent-skill-simple-progress" in js
    assert "打开 RCA 工作流" in js
    assert "renderAgentSkillBridgeEvent" in js
    assert "renderSkillWorkflowRunsDetail" in js
    assert "renderFaultLabSkillResult" in js
    assert "renderLLMFaultSkillClosure" in js
    assert "renderAlertSkillExecutionProcess(result, stagesFilter" in js
    assert "renderAlertSkillVisualSummary" in js
    assert "renderAlertSkillWaitingCard" in js
    assert "Agent 已启动，正在等待 Skill 工具调用事件" in js
    assert "技术细节" in js
    assert "skill-stage-rail" in js
    assert "_alertSkillAgentRunCache" in js
    assert "_alertSkillAgentStatusCache" in js
    assert "_alertSkillAgentPollers" in js
    assert "startAlertSkillAgentPolling" in js
    assert "skillResult || ['completed', 'failed'].includes(status.status)" not in js
    assert "latestAgentSkillEvent(status) || attempts" not in js
    assert "['completed', 'failed', 'cancelled', 'expired'].includes(status.status)" in js
    assert "refreshAlertSkillAgentRun" in js
    assert "refreshAlertSkillAgentRun(index, { poll: true, stage, silent: true, runId:" in js
    assert "skillAgentRun: runRecord" in js
    assert "skill_workflow: options.skillWorkflow" in js
    assert "skillWorkflow: body" in js
    assert "renderAlertSkillAnalysisCard(skillResult, index, active, { agentRun: skillAgentRun" in js
    assert "Agent 尚未调用 Skill Runtime" not in js
    assert "invocation_mode: 'agent_skill'" in js
    assert "return runAgentWorkflowFromAlert(index)" in js
    assert "return runSkillWorkflowFromAlert(index, ['detect', 'diagnose'], 'analysis')" in js
    assert "runAlertAnalysisCompare" in js
    assert "runAlertHealingCompare" in js
    assert "分析分支" in js
    assert "自愈分支" in js
    assert ".alert-action-menu" in css
    assert ".alert-branch-card" in css
    assert ".skill-visual-summary" in css
    assert ".skill-stage-pill" in css
    assert ".agent-skill-simple-progress" in css


def test_reports_and_rca_skill_workflow_are_clickable_and_visible() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "rca-skill-runtime" in html
    assert "rca-skill-runtime-list" in html
    assert "addRCASkillRuntimeEvent" in js
    assert "case 'skill_runtime'" in js
    assert "generateReportFromItem" in js
    assert "data-report-run-id" in js
    assert "分析中，完成后可生成" in js
    assert "运行中的 Agent+Skill 分析不会生成最终故障报告" in js
    assert ".alert-skill-progress" in css
    assert ".alert-skill-process-empty" in css
    assert ".agent-skill-bridge" in css


def test_ai_dashboard_exposes_teller_fine_grained_trace_tab() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'data-ai-dashboard-tab="trace"' in html
    assert 'id="ai-dashboard-trace-summary"' in html
    assert 'id="ai-dashboard-top-kernels"' in html
    assert 'id="ai-dashboard-top-nvtx"' in html
    assert 'id="ai-dashboard-top-runtime"' in html
    assert 'id="ai-dashboard-key-durations"' in html
    assert 'id="ai-dashboard-event-type-stats"' in html
    assert 'id="ai-dashboard-trace-events"' in html
    assert 'id="ai-dashboard-trace-profiler"' in html
    assert 'id="ai-dashboard-trace-collect-status"' in html
    assert 'id="ai-dashboard-request-context"' in html
    assert 'id="ai-dashboard-request-detail"' in html
    assert 'id="ai-dashboard-teller-plan"' in html
    assert "请求级上下文" in html
    assert "端到端请求 Trace" in html
    assert "TELLER 接入计划" in html
    assert "fine_grained_trace" in js
    assert "renderAIDashboardFineGrainedTrace" in js
    assert "renderAIDashboardRequestContext" in js
    assert "renderAIDashboardRequestDetail" in js
    assert "renderAIDashboardE2ETrace" in js
    assert "aiTraceE2ERequest" in js
    assert "selectAIDashboardTraceRequest" in js
    assert "aiTraceRequestDiagnosis" in js
    assert "renderAIDashboardTellerPlan" in js
    assert "请求执行路径" in js
    assert "关键路径时间条" in js
    assert "端到端链路视图" in js
    assert "HTTP 入站" in js


def test_ai_dashboard_exposes_node_resource_metrics_and_pod_pagination() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "节点基础资源" in html
    for canvas_id in (
        "chart-ai-node-cpu",
        "chart-ai-node-memory",
        "chart-ai-node-rootfs",
        "chart-ai-node-disk-read",
        "chart-ai-node-disk-write",
        "chart-ai-node-net-rx",
        "chart-ai-node-net-tx",
    ):
        assert f'id="{canvas_id}"' in html
    for metric_key in (
        "node_cpu_usage",
        "node_memory_usage",
        "node_rootfs_usage",
        "node_disk_read",
        "node_disk_write",
        "node_network_receive",
        "node_network_transmit",
    ):
        assert metric_key in js
    assert "setAIResourcePage" in js
    assert "_aiResourcePage" in js
    assert "第 <strong>" in js
    assert ".ai-resource-pagination" in css
    assert "cfg-ai-th-nodecpu" in html
    assert "cfg-ai-th-nodemem" in html
    assert "cfg-ai-th-diskread" in html
    assert "cfg-ai-th-netrx" in html
    assert "detection.thresholds.ai" in html
    assert 'data-ai-metric-tab="llm"' in html
    assert 'data-ai-metric-tab="node"' in html
    assert 'data-ai-metric-tab="gpu"' in html
    assert 'data-ai-metric-tab="gpu_device"' in html
    assert 'id="ai-metric-tab-node"' in html
    assert "switchAIMetricTab" in js
    assert "resizeAIMetricCharts" in js
    assert ".ai-metric-tabs" in css
    assert ".ai-metric-tab-panel.active" in css
    assert "node_cpu_usage" in js
    assert "node_network_transmit" in js
    assert ".config-mini-grid" in css
    assert "vLLM 调度" in js
    assert "Prefill" in js
    assert "Decode" in js
    assert "missing_layers" in js
    assert "coverage" in js
    assert "critical_path" in js
    assert "TELLER 执行树已解析" in js
    assert "refreshAIEvents({ silent: true })" in js
    assert "loadAIDashboardTellerPlan" in js
    assert "/teller/integration-plan" in js
    assert "renderAIDashboardTraceProfiler" in js
    assert "display_start_ns" in js
    assert "display_time_range_ns" in js
    assert "renderAIDashboardKeyDurations" in js
    assert "renderAIDashboardEventTypeStats" in js
    assert ".ai-request-detail" in css
    assert ".ai-request-path-timeline" in css
    assert ".ai-e2e-chain" in css
    assert ".ai-e2e-node" in css
    assert "collectAIDashboardTellerTrace" in js
    assert "teller_trace_dir" in js
    assert "tgt-k8s-teller" in js
    assert ".ai-trace-metrics" in css
    assert ".ai-trace-lane-events" in css
    assert ".ai-trace-type-bars" in css
    assert ".ai-trace-runtime-card" in css
    assert ".ai-trace-lane-track" in css
    assert ".ai-trace-control-card" in css
    assert ".ai-request-list" in css
    assert ".ai-request-path" in css
    assert ".ai-trace-hierarchy" in css
    assert ".ai-teller-plan-fold" in css


def test_aishperf_catalog_uses_adminlte_style_layout() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert 'id="aishperf-summary"' in html
    assert 'id="aishperf-grid" class="aishperf-grid"' in html
    assert "aishperf-info-box" in js
    assert "aishperf-status-strip" in js
    assert "场景 Catalog" in js
    assert ".aishperf-info-box" in css
    assert ".aishperf-status-strip" in css
    assert ".aishperf-catalog" in css


def test_ai_event_center_is_separate_tabbed_page() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert 'data-view="aievents"' in html
    assert 'id="view-aievents"' in html
    assert 'id="ai-events-cluster"' in html
    assert 'data-ai-events-tab="events"' in html
    assert 'data-ai-events-tab="experiments"' in html
    assert 'data-ai-events-tab="rules"' in html
    assert 'data-ai-events-tab="history"' in html
    assert "智算事件中心" in html
    assert "loadAIEvents" in js
    assert "refreshAIEvents" in js
    assert "switchAIEventsTab" in js
    assert "showAIEventDisposition" in js
    assert "runAIEventAgentAnalysis" in js
    assert "runAIEventAgentHealing" in js
    assert "runAIEventSkill" in js
    assert "refreshAIEventAgentRun" in js
    assert "startAIEventAgentPolling" in js
    assert "_aiEventActionInFlight" in js
    assert "aiEventActionPending" in js
    assert "启动中..." in js
    assert "status: 'expired'" in js
    assert "suppressErrorLog: true" in js
    assert "AI_ANALYSIS_PHASES" in js
    assert "renderAIAnalysisPhaseConfig('multi_agent')" in js
    assert "renderAIAnalysisPhaseConfig('agent_skill')" in js
    assert "analysis_phases: aiAnalysisPhases('multi_agent')" in js
    assert "analysis_phases: aiAnalysisPhases('agent_skill')" in js
    assert "智算事件分析与自愈" in js
    assert "选择“多智能体”或“Agent+Skill”分析链路" in js
    assert "启动多智能体分析" in js
    assert "启动 Agent+Skill 分析" in js
    assert "生成多智能体自愈" in js
    assert "启动 Agent+Skill 自愈" in js
    assert "renderAgentSkillCompactProgress(status" in js
    assert "openAIDashboardForCluster" in js
    assert "没有业务请求时" in js
    assert "查看智算观测" in js
    assert "/api/ai-events/refresh" in js
    assert "/api/ai-events/${encodeURIComponent(eventId)}/agent" in js
    assert "/api/ai-events/${encodeURIComponent(eventId)}/heal" in js
    assert "limit: '50'" in js
    assert "智算事件加载失败：" in js
    assert "重试" in js
    assert "e.status !== 'resolved'" in js
    assert "K8s Event" in js
    assert "k8s_event_sources" in js
    assert "K8s 已连接无 Warning" in js
    assert "K8s 未配置" in js
    assert "saveAIEventThresholds" in js
    assert "data-ai-threshold-key" in js
    assert "ai-rule-confirmation-points" in js
    assert "/api/platform/config" in js
    assert ".ai-threshold-editor" in css
    assert ".ai-threshold-grid" in css
    assert "K8s 对象" in js
    assert "Skill 工具已运行" in js
    assert "switchView('aievents')" in html
    assert ".ai-event-center-card" in css
    assert ".ai-events-tabs" in css
    assert ".ai-event-card" in css
    assert ".ai-event-detail" in css
    assert ".ai-event-command-list" in css
    assert ".fault-observe-callout" in css


def test_ai_event_rca_navigation_closes_modal_and_isolates_run_mode() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert "async function openRCAHistoryRun(runId, mode = '')" in js
    assert "closeAlertDispositionModal();" in js
    assert "replayRCAWorkflowRun(run, mode);" in js
    assert "mode === 'multi_agent' && evt?.event === 'skill_runtime'" in js
    assert "'data-mode': 'multi_agent'" in js
    assert "'data-mode': 'agent_skill'" in js


def test_unconfigured_host_templates_do_not_block_target_save() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert "const configuredHosts = (_faultTargets.llm_hosts || []).filter" in js
    assert "主机 id/名称 不能为空" in js
    assert "主机 id/名称/host 不能为空" not in js
    assert "个主机档案待配置地址" in js
