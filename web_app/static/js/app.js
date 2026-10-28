/* ═════════════════════════════════════════════
   AgenticSRE Dashboard — Main SPA Logic
   Blue-White Theme with Chart.js Metrics
   ═════════════════════════════════════════════ */

// ── State ──
const state = {
    currentView: 'overview',
    refreshTimer: null,
    refreshInterval: 10,
    loadedViews: {},
    configCenterDirty: false,
    sseConnections: {},
    rcaRunId: null,
    daemonLogSSE: null,
    detectionSSE: null,
    podChart: null,
    metricRange: '15m',
    charts: {},  // chart instances keyed by canvas id
    lastSkillWorkflow: null,
    skillCatalogLoaded: false,
    skillCatalog: [],
    skillDetailCache: {},
    skillRunDetailCache: {},
    skillOpsTab: 'run',
    skillCatalogPurpose: '',
    aiTraceSelectedRequestId: '',
    aiTraceLastTrace: null,
    aiDashboardData: null,
    aiTraceFilter: 'all',
    aiTraceQuery: '',
};

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function compactEvidenceText(value, maxLength = 180) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    if (/kubectl .*执行失败/i.test(text) || /connection refused/i.test(text) || /couldn't get current server API group list/i.test(text)) {
        const command = (text.match(/kubectl\s+[^执]+/) || ['kubectl'])[0].trim();
        const reason = /connection refused/i.test(text)
            ? 'K8s API 连接被拒绝，未能采集实时集群证据'
            : 'K8s 证据采集失败';
        return `${command} 执行失败：${reason}`;
    }
    return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function renderEvidenceList(items = [], options = {}) {
    const maxItems = options.maxItems || 4;
    const evidence = Array.isArray(items) ? items.filter(Boolean) : [];
    if (!evidence.length) return '';
    const visible = evidence.slice(0, maxItems);
    const list = visible.map(x => `<li>${escapeZh(compactEvidenceText(x))}</li>`).join('');
    const raw = evidence.some(x => String(x || '').length > 220 || /\n/.test(String(x || '')));
    return `<div class="compact-evidence-list">
        <strong>${escapeHtml(options.title || '关键证据')}</strong>
        <ul>${list}</ul>
        ${raw ? `<details class="skill-raw-fold"><summary>查看原始证据</summary><pre>${escapeHtml(evidence.join('\n\n'))}</pre></details>` : ''}
    </div>`;
}

// Chart color palette (blue theme)
const CHART_COLORS = [
    '#1e6fd9', '#0ea5e9', '#10b981', '#8b5cf6',
    '#f59e0b', '#ef4444', '#06b6d4', '#ec4899',
    '#14b8a6', '#6366f1',
];

const RISK_LABELS = {
    low: '低风险',
    medium: '中风险',
    high: '高风险',
    critical: '严重风险',
};

const SOURCE_LABELS = {
    kb: '知识库',
    dynamic: '动态生成',
    deterministic: '内置规则',
    diagnosis_dynamic: '诊断生成',
    llm_faultlab: '智算实验',
    ai_dashboard: '智算指标',
    k8s_event: 'K8s 事件',
    fault_experiment: '故障实验',
};

const DIAGNOSIS_BLOCKER_LABELS = {
    missing_diagnosis: '缺少诊断结论',
    low_confidence: '诊断置信度低于阈值',
    rule_min_confidence: '诊断置信度低于规则阈值',
    missing_root_cause: '缺少根因结论',
    missing_fault_type: '缺少故障类型',
    confidence_below_threshold: '诊断置信度低于配置阈值',
};

const AUTO_EXECUTION_BLOCKER_LABELS = {
    confidence_below_auto_threshold: '置信度未达到全自动阈值',
    insufficient_history: '历史恢复样本不足',
    history_outcome_counts_missing: '历史恢复记录缺少成功/失败计数',
    history_success_lower_bound_low: '历史恢复成功率置信下界不足',
    recent_code_change: '存在近期代码或发布变更',
    recipe_not_marked_safe: '规则未标记可全自动执行',
    manual_recovery_only: '该故障仅允许人工恢复',
    rollback_not_available: '中高风险动作缺少回滚操作',
    action_risk_too_high: '动作风险超过自动执行上限',
    no_executable_actions: '没有可执行自愈动作',
};

const LLM_FAULT_TYPE_LABELS = {
    process_crash: 'vLLM 进程崩溃',
    model_load_failure: '模型加载失败',
    kv_cache_exhaustion: 'KV Cache 耗尽',
    process_hang: 'vLLM 进程挂起',
    model_permission_error: '模型权限异常',
    cuda_error: 'CUDA 设备错误',
    nccl_failure: 'NCCL 通信失败',
    endpoint_timeout: '推理端点异常',
    grpc_failure: 'gRPC 端口阻断',
    tp_communication_loss: '张量并行通信丢失',
    http_503: 'HTTP 推理端口不可用',
    gpu_oom: 'GPU 显存 OOM',
    cpu_stress: 'CPU 压力',
    memory_pressure: '系统内存压力',
    prefill_decode_misclassification: 'Prefill/Decode 调度误判',
    attention_backend_mismatch: 'Attention 后端不匹配',
    state_contamination: 'KV Cache 状态污染',
    cupti_trace_overhead: 'CUPTI 追踪开销',
    request_queue_overflow: '请求队列积压',
    continuous_batch_disruption: '连续批处理扰动',
    pcie_throttle: 'PCIe 带宽受限',
    gpu_clock_throttle: 'GPU 频率降速',
    bandwidth_throttle: '网络带宽受限',
    disk_pressure: '磁盘压力',
    fd_exhaustion: '文件句柄耗尽',
    packet_loss: '网络丢包',
    rdma_failure: 'RDMA 通信故障',
    tokenizer_error: 'Tokenizer 异常',
    weight_corruption: '模型权重损坏',
    cuda_ctx_corruption: 'CUDA 上下文异常',
    unknown: '未知智算故障',
};

const LLM_LAYER_LABELS = {
    software: '软件层',
    driver: '驱动层',
    network: '网络层',
    os: '操作系统层',
};

const SEVERITY_LABELS = {
    warning: '警告',
    critical: '严重',
    info: '提示',
};

const K8S_FAULT_LABELS = {
    ServiceUnavailable: '服务不可用',
    ResourceExhausted: '资源耗尽',
    HighLatency: '高延迟',
    OOMKilled: '容器 OOM',
    PodCrashLoop: 'Pod 反复重启',
};

const DIAGNOSIS_MODE_LABELS = {
    llm_faultlab: '智算故障实验诊断',
    rca_result: 'RCA 诊断结果',
    alert_only: '告警文本降级',
};

const STATUS_LABELS = {
    completed: '已完成',
    running: '运行中',
    failed: '失败',
    ok: '正常',
    success: '成功',
    error: '错误',
    no_match: '未匹配',
    missing: '缺失',
    unavailable: '不可用',
    unknown: '未知',
    ready: '就绪',
    skipped: '已跳过',
    matched: '已匹配',
    diagnosed: '已诊断',
    active: '活跃',
    confirmed: '已确认',
    rejected: '已排除',
    pending: '待处理',
};

const FAULT_TYPE_LABELS = {
    ResourceExhausted: '资源耗尽',
    ImagePullError: '镜像拉取失败',
    SchedulingFailed: '调度失败',
    ServiceUnavailable: '服务不可用',
    DNSResolution: 'DNS 解析异常',
    LLMRuntime: 'LLM 运行时异常',
    HighLatency: '高延迟',
    Unknown: '未知故障',
    GenericFault: '通用故障',
    network_partition: '网络分区或阻断',
    service_endpoint_failure: '服务端点异常',
    service_unavailable_no_endpoints: '服务无可用端点',
    pod_not_ready: 'Pod 未就绪',
    no_fault_detected: '未发现活跃故障',
    observability_gap: '观测链路缺口',
    network_registry_access: '镜像仓库网络访问异常',
    image_registry_or_tag: '镜像仓库或 tag 异常',
    application_crash: '应用进程崩溃',
    resource_or_probe_failure: '资源限制或探针异常',
    scheduling_failure: '调度失败',
    process_crash: '进程崩溃',
    vllm_process_missing: 'vLLM 进程缺失',
};

const RCA_AGENT_LABELS = {
    metric_agent: '指标智能体',
    log_agent: '日志智能体',
    trace_agent: '链路智能体',
    event_agent: '事件智能体',
    llm_inference_agent: '智算推理智能体',
    structured_evidence: '结构化证据',
    correlation: '跨信号关联',
    graph_rca: '图根因定位',
    remediation: '自愈智能体',
    skill_agent: 'Agent+Skill 智能体',
};

const RCA_QUALITY_LABELS = {
    0: '优秀',
    1: '良好',
    2: '待复核',
    3: '待复核',
    gold: '优秀',
    silver: '合格',
    bronze: '待复核',
};

const TEXT_TRANSLATIONS = new Map([
    ['Initializing tools and agents...', '正在初始化工具和智能体...'],
    ['Retrieving historical context...', '正在检索历史上下文...'],
    ['Generating root cause hypotheses...', '正在生成根因假设...'],
    ['Running cross-signal correlation...', '正在执行跨信号关联分析...'],
    ['Running graph-based RCA localization...', '正在执行图根因定位...'],
    ['Generating final RCA report...', '正在生成最终根因报告...'],
    ['Running collaboration critic reflection...', '正在执行协作反思评估...'],
    ['Running quality assessment...', '正在执行质量评估...'],
    ['Running diagnosis closure loop...', '正在执行诊断闭环复核...'],
    ['Auto-learning from this incident...', '正在从本次故障中自动学习...'],
    ['Skipping auto-learning for no-fault RCA result', '未发现活跃故障，跳过自动学习'],
    ['Initiating self-healing...', '正在启动自愈流程...'],
    ['LLM report generation failed', '大模型报告生成失败'],
    ['using fallback', '使用降级报告'],
    ['based on hypothesis analysis', '基于假设分析生成降级结论'],
    ['Investigate the reported incident manually', '请基于已采集证据进行人工复核和处置'],
    ['Root Cause', '根因'],
    ['Confidence', '置信度'],
    ['Analysis failed', '分析失败，未能生成根因建议'],
    ['Pipeline failed', '分析流水线失败'],
    ['Unknown evidence agent', '未知证据智能体'],
    ['evidence timeout', '证据采集超时'],
    ['Fast evidence plan', '快速证据采集计划'],
    ['Running evidence agents', '正在运行证据智能体'],
    ['Evidence collection', '证据采集'],
    ['Re-ranking hypotheses', '正在重排根因假设'],
    ['Top hypothesis', '首位假设'],
    ['Top confidence', '最高置信度'],
    ['High confidence reached', '已达到高置信度'],
    ['stopping iterations', '停止继续迭代'],
    ['Top suspect', '首要怀疑对象'],
    ['Graph RCA top', '图根因定位首位对象'],
    ['Collaboration strategy', '协作策略'],
    ['Critic quality', '反思质量分'],
    ['Refined confidence', '修正后置信度'],
    ['Judge level', '质量等级'],
    ['Flagged for review', '已标记需要复核'],
    ['low quality reasoning', '推理质量不足'],
    ['Historical resolution stats failed', '历史恢复统计失败'],
    ['Calibrated confidence', '校准后置信度'],
    ['Closure reason', '闭环原因'],
    ['Target agents', '目标智能体'],
    ['Closure judge score', '闭环复评分'],
    ['Human review queued', '已提交人工复核'],
    ['Rules added', '新增规则数'],
    ['Rule usage bumped', '规则使用次数已更新'],
    ['Remediation status', '自愈状态'],
    ['Remediation plan generated, waiting for approval', '已生成自愈方案，等待审批'],
    ['Found', '找到'],
    ['similar rules', '条相似规则'],
    ['similar faults', '条相似故障'],
    ['root cause requires more evidence', '当前证据不足，需要补充更多观测后确认根因'],
    ['insufficient evidence before RCA', 'RCA 前证据不足，需要先采集多源证据'],
    ['generic remediation required', '需要生成通用受控恢复方案'],
    ['post-healing verification required', '自愈后需要执行恢复验证'],
    ['risk gate passed for dry-run or low-risk actions', 'Dry-run 或低风险动作已通过风险门禁'],
    ['image pull failed', '镜像拉取失败'],
    ['cluster dns resolution degraded', '集群 DNS 解析能力下降'],
    ['traffic denied by policy or chaos rule', '流量被 NetworkPolicy 或 Chaos 规则阻断'],
    ['node condition NotReady', '节点处于 NotReady 状态'],
    ['vLLM process crash', 'vLLM 进程崩溃'],
    ['memory or GPU memory exhaustion', '内存或 GPU 显存资源耗尽'],
    ['image registry, tag, or pull secret failure', '镜像仓库、镜像 tag 或拉取凭据异常'],
    ['service endpoint or backend readiness failure', 'Service endpoint 缺失或后端就绪状态异常'],
    ['DNS resolution failure', 'DNS 解析异常'],
    ['LLM runtime, GPU, or inference endpoint failure', 'LLM 运行时、GPU 或推理端点异常'],
    ['latency or timeout regression', '请求时延或超时异常升高'],
    ['specific root cause explanation', '具体根因说明'],
    ['recommended fix', '建议修复动作'],
    ['recurrence prevention', '复发预防措施'],
    ['healthy', '健康'],
    ['unhealthy', '不健康'],
    ['manual', '手动'],
    ['auto_learn', '自动学习'],
    ['supervised', '监督学习'],
    ['no_learner', '未触发学习'],
    ['success', '成功'],
    ['failed', '失败'],
    ['unknown', '未知'],
]);

const LLM_SYMPTOM_LABELS = {
    'Pod restart detected': '检测到 Pod 重启',
    'Service unavailable (503)': '服务不可用或返回 503',
    'Request timeout spike': '请求超时突增',
    'CrashLoopBackOff': 'Pod 进入 CrashLoopBackOff',
    'Model not found error in logs': '日志出现模型不存在错误',
    'Zero throughput': '吞吐降为 0',
    'Increased latency': '请求时延升高',
    'Request rejection': '请求被拒绝',
    'GPU memory spike': 'GPU 显存突增',
    'Health check timeout': '健康检查超时',
    'Requests hang without immediate pod restart': '请求挂起但 Pod 未立即重启',
    'GPU utilization may remain stale while throughput drops to zero': '吞吐降为 0，GPU 利用率可能停留在旧值',
    'Permission denied while loading model asset': '加载模型资产时权限被拒绝',
    'CrashLoopBackOff after restart': '重启后进入 CrashLoopBackOff',
    'Model load failure in logs': '日志出现模型加载失败',
    'CUDA error in logs': '日志出现 CUDA 错误',
    'GPU utilization drop to 0': 'GPU 利用率降为 0',
    'All requests fail': '所有请求失败',
    'NCCL timeout in logs': '日志出现 NCCL 超时',
    'Tensor parallel stall': '张量并行停滞',
    'Throughput collapse': '吞吐骤降',
    'P99 latency spike': 'P99 时延突增',
    'Client timeout errors': '客户端超时错误',
    'Request queue buildup': '请求队列堆积',
    'Connection refused errors': '连接被拒绝',
    'Service health check failure': '服务健康检查失败',
    'NCCL/TP timeout': 'NCCL/张量并行超时',
    'Partial GPU utilization': '部分 GPU 利用率异常',
    'Request failures': '请求失败',
    'Connection refused or HTTP 503': '连接被拒绝或 HTTP 503',
    'Readiness probe failure': '就绪探针失败',
    'Zero successful requests': '成功请求数为 0',
    'CUDA OOM error': 'CUDA OOM 错误',
    'Process killed by OOM killer': '进程被 OOM Killer 终止',
    'Pod restart': 'Pod 重启',
    'Increased preprocessing latency': '预处理时延升高',
    'CPU utilization spike': 'CPU 利用率突增',
    'Request queue growth': '请求队列增长',
    'System memory high': '系统内存占用高',
    'Swap usage increase': 'Swap 使用量升高',
    'Attention kernel errors in CUDA logs': 'CUDA 日志出现 Attention Kernel 错误',
    'Throughput degradation': '吞吐下降',
    'Increased error rate on inference requests': '推理请求错误率升高',
    'NaN in model output tokens': '模型输出 token 出现 NaN',
    'Output quality degradation': '输出质量下降',
    'CUDA computation errors': 'CUDA 计算错误',
    '5-15% increase in kernel launch latency': 'Kernel 启动时延升高 5-15%',
    'TTFT and TPOT degradation': 'TTFT 和 TPOT 恶化',
    'Throughput reduction under profiling load': '追踪负载下吞吐下降',
};

const LLM_DESCRIPTION_LABELS = {
    process_crash: '终止 vLLM 主进程，用于验证服务重启和可用性恢复。',
    model_load_failure: '破坏模型加载路径，用于验证重启后的模型加载失败。',
    kv_cache_exhaustion: '填满 KV Cache，用于触发推理内存压力。',
    process_hang: '暂停 vLLM 服务进程，模拟事件循环或 CUDA 调用卡住。',
    model_permission_error: '移除模型资产读权限，模拟 NFS 或 hostPath 权限回退。',
    cuda_error: '设置非法 CUDA 设备，触发运行时错误。',
    nccl_failure: '阻断 NCCL 端口，扰动多 GPU 通信。',
    endpoint_timeout: '临时移除推理 Service 的就绪后端，模拟连接超时、拒绝和请求成功率下降。',
    grpc_failure: '阻断 gRPC 服务端口。',
    tp_communication_loss: '阻断张量并行跨节点通信。',
    http_503: '拒绝 OpenAI 兼容 HTTP 推理端口，模拟接口不可用或 503。',
    gpu_oom: '消耗 GPU 显存，触发 OOM。',
    cpu_stress: '制造 CPU 压力，影响预处理吞吐。',
    memory_pressure: '消耗系统内存，制造主机内存压力。',
    prefill_decode_misclassification: '降低 GPU 内存和并发序列数，触发调度阶段误判。',
    attention_backend_mismatch: '强制不兼容的 Attention 后端，验证模型架构兼容性问题。',
    state_contamination: '向 GPU 内存注入异常值，模拟跨层状态污染。',
    cupti_trace_overhead: '启用 CUPTI 活动追踪，注入性能观测开销。',
};

const AUTO_REFRESH_VIEWS = new Set([
    'overview',
    'metrics',
    'alerts',
    'daemon',
    'aidashboard',
]);

const STATIC_REFRESH_VIEWS = new Set([
    'configcenter',
    'knowledge',
    'healcenter',
    'skillops',
    'reports',
    'faultlab',
    'llmfaultlab',
    'loadgen',
    'evolution',
]);

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initAIEventActionDelegates();
    initConfigCenterDirtyTracking();
    initRefresh();
    loadAuthState();
    loadOverview();
    healthCheck();
    setInterval(healthCheck, 30000);
});

function initAIEventActionDelegates() {
    document.addEventListener('click', event => {
        const button = event.target.closest('[data-ai-event-action]');
        if (!button) return;
        const action = button.dataset.aiEventAction || '';
        const eventId = button.dataset.aiEventId || '';
        const stage = button.dataset.stage || 'details';
        const mode = button.dataset.mode || '';
        const runId = button.dataset.runId || '';
        const status = button.dataset.status || '';
        if (!eventId && !['open-rca'].includes(action)) return;
        event.preventDefault();
        if (button.disabled) return;
        if (action === 'open' || action === 'switch') {
            showAIEventDisposition(eventId, stage);
        } else if (action === 'status') {
            updateAIEventStatus(eventId, status || 'resolved');
        } else if (action === 'run-agent-analysis') {
            runAIEventAgentAnalysis(eventId);
        } else if (action === 'run-agent-healing') {
            runAIEventAgentHealing(eventId);
        } else if (action === 'run-skill') {
            runAIEventSkill(eventId, mode || 'diagnose');
        } else if (action === 'refresh-run') {
            refreshAIEventAgentRun(eventId, runId);
        } else if (action === 'open-rca') {
            openRCAHistoryRun(runId, mode);
        }
    });
}

// ─────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────

function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            switchView(view);
            setMobileSidebar(false);
        });
    });

    document.getElementById('toggle-sidebar')?.addEventListener('click', () => {
        const sidebar = document.getElementById('sidebar');
        if (!sidebar) return;
        if (window.matchMedia('(max-width: 768px)').matches) {
            setMobileSidebar(!sidebar.classList.contains('open'));
            return;
        }
        sidebar.classList.toggle('collapsed');
        document.body.classList.toggle('sidebar-collapsed');
    });

    document.getElementById('sidebar-backdrop')?.addEventListener('click', () => setMobileSidebar(false));
    window.addEventListener('resize', () => {
        if (!window.matchMedia('(max-width: 768px)').matches) setMobileSidebar(false);
    });
}

function setMobileSidebar(open) {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (!sidebar || !backdrop) return;
    sidebar.classList.toggle('open', Boolean(open));
    backdrop.classList.toggle('open', Boolean(open));
    document.body.classList.toggle('mobile-sidebar-open', Boolean(open));
}

function switchView(viewId) {
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    const activeNav = document.querySelector(`.nav-item[data-view="${viewId}"]`);
    activeNav?.classList.add('active');
    if (activeNav) activeNav.closest('details')?.setAttribute('open', '');

    document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
    document.getElementById(`view-${viewId}`)?.classList.add('active');

    const titles = {
        overview: '运维概览', metrics: '通算指标', logs: '日志查询',
        alerts: '通算告警', rca: '根因分析', traces: '链路追踪',
        daemon: '守护进程', knowledge: '故障知识库', evolution: '演化闭环',
        configcenter: '配置中心', healcenter: '自愈中心', skillops: 'Skill 调试',
        faultlab: '通算故障实验', llmfaultlab: '智算故障实验', loadgen: '负载生成', aidashboard: '智算观测', aievents: '智算事件', reports: '故障报告',
        events: '事件追踪', hermes: '智能助手'
    };
    const subtitles = {
        overview: '集群健康、工作负载拓扑和近期运维信号',
        metrics: '通算集群 Prometheus 基础设施、网络、容器和业务指标',
        logs: '按命名空间检索日志，为诊断采集证据',
        alerts: '告警聚合、诊断和自愈处置',
        rca: '基于证据、反思和闭环的多智能体根因分析',
        traces: '分布式链路和服务级执行路径',
        daemon: '后台检测守护进程和实时运维信号',
        knowledge: '故障知识、专家反馈和记忆治理',
        evolution: '系统能力、人工评审队列和智能体演化遥测',
        configcenter: '大模型、异常检测和自愈策略运行时配置',
        healcenter: '自愈知识库、建议、Dry-run 执行和回滚记录',
        skillops: 'Skill 匹配、执行结果和运行审计',
        aidashboard: '面向多智算集群的 vLLM、GPU、拓扑和业务 SLO 观测',
        aievents: '智算故障检测、诊断和自愈处置',
        faultlab: '面向 K8s/AIOpsLab 的通算可控故障实验',
        llmfaultlab: '智算推理故障的注入、诊断与恢复验证',
        loadgen: '独立启动、观测和停止通算或智算负载任务',
        reports: '历史 RCA 报告和可导出诊断摘要',
        events: 'Kubernetes 事件流和集群状态变化',
        hermes: '面向排障和命令执行的交互式运维助手'
    };
    document.getElementById('view-title').textContent = titles[viewId] || viewId;
    const subtitle = document.getElementById('view-subtitle');
    if (subtitle) subtitle.textContent = subtitles[viewId] || '';

    state.currentView = viewId;
    refreshCurrentView({ navigation: true });
}

function toggleOverviewDetails(details) {
    if (!details?.open) return;
    setTimeout(() => state.podChart?.resize?.(), 30);
}

function refreshCurrentView(options = {}) {
    if (options.auto && !shouldAutoRefreshView(state.currentView)) {
        return;
    }
    const loaders = {
        overview: loadOverview,
        metrics: loadMetrics,
        logs: () => loadNamespaces('log-ns'),
        alerts: () => { loadAlertList(); loadDetectionConfig(); },
        rca: () => loadRCAHistory({reset: options.navigation !== true}),
        traces: loadTracesView,
        daemon: loadDaemonStatus,
        configcenter: () => loadConfigCenter({ force: options.force === true, navigation: options.navigation === true }),
        healcenter: loadHealCenter,
        skillops: loadSkillOps,
        knowledge: loadKnowledge,
        evolution: loadEvolution,
        aidashboard: loadAIDashboard,
        aievents: loadAIEvents,
        faultlab: loadFaultLab,
        llmfaultlab: loadLLMFaultLab,
        loadgen: loadLoadGenerator,
        reports: loadReportList,
        events: () => { Promise.all([loadNamespaces('event-ns'), loadEvents()]); },
        hermes: loadHermesView,
    };
    (loaders[state.currentView] || (() => {}))();
}

function shouldAutoRefreshView(viewId) {
    if (STATIC_REFRESH_VIEWS.has(viewId)) return false;
    return AUTO_REFRESH_VIEWS.has(viewId);
}

// ── Auto-Refresh ──

function initRefresh() {
    const refreshIfVisible = () => {
        if (document.hidden) return;
        refreshCurrentView({ auto: true });
    };
    document.getElementById('refresh-interval').addEventListener('change', (e) => {
        state.refreshInterval = parseInt(e.target.value);
        clearInterval(state.refreshTimer);
        if (state.refreshInterval > 0) {
            state.refreshTimer = setInterval(refreshIfVisible, state.refreshInterval * 1000);
        }
    });
    state.refreshTimer = setInterval(refreshIfVisible, 10000);
}

// ─────────────────────────────────────────
// API Helpers
// ─────────────────────────────────────────

async function api(path, options = {}) {
    let timer = null;
    try {
        const timeoutMs = options.timeoutMs || 30000;
        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        if (controller) timer = setTimeout(() => controller.abort(), timeoutMs);
        const fetchOptions = { ...options };
        delete fetchOptions.timeoutMs;
        delete fetchOptions.suppressErrorLog;
        if (controller && !fetchOptions.signal) fetchOptions.signal = controller.signal;
        const res = await fetch(path, fetchOptions);
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text || res.statusText }; }
        if (!res.ok) {
            const msg = typeof data?.detail === 'string'
                ? data.detail
                : data?.detail?.message || data?.message || data?.error || `HTTP ${res.status}`;
            const err = new Error(msg);
            err.status = res.status;
            err.data = data;
            throw err;
        }
        return data;
    } catch (e) {
        const aborted = e?.name === 'AbortError' || /aborted|aborterror/i.test(String(e?.message || e || ''));
        if (!options.suppressErrorLog && !aborted) console.error(`API error [${path}]:`, e);
        if (options.throwOnError) throw e;
        return null;
    } finally {
        if (timer) clearTimeout(timer);
    }
}

async function loadAuthState() {
    const data = await api('/api/auth/me');
    const chip = document.getElementById('auth-chip');
    const username = document.getElementById('auth-username');
    const logoutBtn = document.getElementById('logout-btn');
    if (!data?.auth_enabled) {
        chip?.classList.remove('active');
        if (logoutBtn) logoutBtn.style.display = 'none';
        return;
    }
    const activeUser = data.username || 'admin';
    if (username) username.textContent = activeUser;
    const sidebarUser = document.getElementById('sidebar-username');
    if (sidebarUser) sidebarUser.textContent = activeUser;
    chip?.classList.add('active');
    if (logoutBtn) logoutBtn.style.display = 'inline-flex';
}

async function logout() {
    await api('/api/auth/logout', { method: 'POST' });
    window.location.href = '/login';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeHtmlAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function shortText(text, maxLength = 24) {
    const value = String(text ?? '');
    if (value.length <= maxLength) return value;
    return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

function formatTime(ts) {
    if (!ts) return '-';
    try { return new Date(ts).toLocaleString('zh-CN'); } catch { return ts; }
}

function setVal(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
}

function badgeClass(phase) {
    const map = { Running: 'success', Succeeded: 'success', Pending: 'warning', Failed: 'danger', Unknown: 'gray' };
    return map[phase] || 'gray';
}

// ─────────────────────────────────────────
// Overview
// ─────────────────────────────────────────

async function loadOverview() {
    const [data, nodesData, events] = await Promise.all([
        api('/api/cluster/overview'),
        api('/api/cluster/nodes'),
        api('/api/cluster/events?limit=5'),
    ]);

    if (data) {
        animateValue('stat-nodes', data.nodes || 0);
        animateValue('stat-pods', data.pods_total || 0);
        animateValue('stat-ns', data.namespaces || 0);
    }

    if (nodesData?.nodes) {
        document.getElementById('node-grid').innerHTML = nodesData.nodes.map(n => `
            <div class="node-card">
                <div class="node-name">${escapeHtml(n.name)}</div>
                <div class="node-meta">
                    <span class="badge badge-${n.ready === 'True' ? 'success' : 'danger'}">${n.ready === 'True' ? 'Ready' : 'NotReady'}</span>
                    ${n.roles.map(r => `<span class="badge badge-info">${r}</span>`).join(' ')}
                </div>
                <div class="node-meta" style="margin-top:4px">CPU: ${n.cpu} | Mem: ${n.memory} | ${n.version}</div>
            </div>
        `).join('');
    }

    if (events?.events) {
        const warnings = events.events.filter(e => e.type === 'Warning');
        document.getElementById('alert-preview').innerHTML = warnings.length
            ? warnings.map(e => `
                <div class="signal-item">
                    <span><span class="badge badge-warning">${e.reason}</span> ${escapeHtml(e.message?.substring(0, 100) || '')}</span>
                    <span class="text-muted">${e.object}</span>
                </div>
            `).join('')
            : '<p class="text-muted" style="padding:12px">暂无告警</p>';
    }

    // Load topology + pod chart
    loadTopology();
    if (data?.pod_phases) renderPodChart(data.pod_phases);
}

function animateValue(elemId, target) {
    const el = document.getElementById(elemId);
    if (!el) return;
    const current = parseInt(el.textContent) || 0;
    if (current === target) { el.textContent = target; return; }
    const diff = target - current;
    const steps = Math.min(Math.abs(diff), 20);
    const stepVal = diff / steps;
    let i = 0;
    const timer = setInterval(() => {
        i++;
        if (i >= steps) { el.textContent = target; clearInterval(timer); }
        else { el.textContent = Math.round(current + stepVal * i); }
    }, 30);
}

function renderPodChart(phases) {
    const canvas = document.getElementById('pod-chart');
    const ctx = canvas.getContext('2d');

    if (state.podChart) { state.podChart.destroy(); }

    const entries = Object.entries(phases);
    const colors = {
        Running: '#10b981', Succeeded: '#0ea5e9',
        Pending: '#f59e0b', Failed: '#ef4444', Unknown: '#a0aec0'
    };

    state.podChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: entries.map(([k]) => k),
            datasets: [{
                data: entries.map(([, v]) => v),
                backgroundColor: entries.map(([k]) => colors[k] || '#a0aec0'),
                borderWidth: 2,
                borderColor: '#ffffff',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '60%',
            plugins: {
                legend: { position: 'bottom', labels: { padding: 16, font: { size: 12 } } },
            },
            animation: { animateRotate: true, duration: 800 },
        }
    });
}

// ─────────────────────────────────────────
// Operational Topology
// ─────────────────────────────────────────

const _topo = { topoData: null, nsFilter: '', view: 'nodes' };

const NS_COLORS = ['#60a5fa','#34d399','#a78bfa','#fbbf24','#f472b6','#22d3ee','#fb923c','#818cf8','#4ade80','#e879f9','#38bdf8','#facc15','#c084fc','#2dd4bf','#f87171'];

function _nsColor(ns, nsList) {
    const idx = nsList.indexOf(ns);
    return NS_COLORS[idx % NS_COLORS.length];
}

async function loadTopology() {
    const data = await api('/api/cluster/topology');
    if (!data) return;
    _topo.topoData = data;

    const s = data.summary || {};
    animateValue('stat-nodes', s.total_nodes || 0);
    animateValue('stat-deploys', s.total_deployments || 0);
    animateValue('stat-services', s.total_services || 0);
    animateValue('stat-pods', s.total_pods || 0);
    animateValue('stat-ns', data.namespaces?.length || 0);
    animateValue('stat-faulty', (s.faulty_pods || 0) + (s.faulty_deployments || 0));

    const sel = document.getElementById('topo-ns-filter');
    if (sel && data.namespaces) {
        const cur = sel.value;
        sel.innerHTML = '<option value="">所有命名空间</option>' +
            data.namespaces.map(ns => `<option value="${ns}" ${ns===cur?'selected':''}>${ns}</option>`).join('');
    }

    renderTopology(data);
}

function filterTopologyNs() {
    _topo.nsFilter = document.getElementById('topo-ns-filter')?.value || '';
    if (_topo.topoData) renderTopology(_topo.topoData);
}

function switchTopologyView(view) {
    _topo.view = view || 'nodes';
    document.querySelectorAll('[data-topo-view]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.topoView === _topo.view);
    });
    document.querySelectorAll('.topo-view-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === (_topo.view === 'dependency' ? 'topo-dependency-map' : 'topo-node-map'));
    });
    if (_topo.topoData) renderTopology(_topo.topoData);
}

function renderTopology(data) {
    const nsFilter = _topo.nsFilter;
    const pods = nsFilter ? data.pods.filter(p => p.namespace === nsFilter) : data.pods;
    const deployments = nsFilter ? data.deployments.filter(d => d.namespace === nsFilter) : data.deployments;
    const services = nsFilter ? data.services.filter(s => s.namespace === nsFilter) : data.services;
    const nodes = data.nodes || [];

    renderTopologyHealth(data, pods, deployments, services);
    renderTopologyNodes(nodes, pods, deployments, services);
    renderTopologyDependencyMap(nodes, pods, deployments, services);
    renderTopologyFaults(pods, deployments);
    renderTopologyNamespaces(data.namespaces || [], pods, deployments);
}

function renderTopologyHealth(data, pods, deployments, services) {
    const strip = document.getElementById('topo-health-strip');
    if (!strip) return;
    const faultyPods = pods.filter(p => p.is_faulty).length;
    const faultyDeploys = deployments.filter(d => d.is_faulty).length;
    const readyPods = pods.filter(p => p.phase === 'Running' && !p.is_faulty).length;
    strip.innerHTML = [
        ['Pods', `${readyPods}/${pods.length}`, faultyPods ? 'warning' : 'success'],
        ['Deployments', `${deployments.filter(d => !d.is_faulty).length}/${deployments.length}`, faultyDeploys ? 'warning' : 'success'],
        ['Services', String(services.length), 'info'],
        ['Namespaces', String(nsFilterOrAll(data.namespaces?.length || 0)), 'info'],
    ].map(([label, value, kind]) => `
        <div class="topo-health ${kind}">
            <span>${label}</span>
            <strong>${value}</strong>
        </div>
    `).join('');
}

function nsFilterOrAll(total) {
    return _topo.nsFilter ? '1' : total;
}

function renderTopologyNodes(nodes, pods, deployments, services) {
    const container = document.getElementById('topo-node-map');
    if (!container) return;

    const podsByNode = {};
    pods.forEach(p => {
        const key = p.node || 'unscheduled';
        if (!podsByNode[key]) podsByNode[key] = [];
        podsByNode[key].push(p);
    });

    container.innerHTML = nodes.map(node => {
        const nodePods = podsByNode[node.name] || [];
        const faulty = nodePods.filter(p => p.is_faulty);
        const namespaces = [...new Set(nodePods.map(p => p.namespace))].sort();
        const topNamespaces = namespaces.slice(0, 5).map(ns => `<span>${escapeHtml(ns)}</span>`).join('');
        const podDots = nodePods.slice(0, 80).map(p => `
            <button class="pod-dot ${p.is_faulty ? 'faulty' : p.phase === 'Running' ? 'running' : 'pending'}"
                    title="${escapeHtml(p.namespace + '/' + p.name)} | ${escapeHtml(p.phase)} | Ready ${escapeHtml(p.ready)} | Restarts ${p.restarts}">
            </button>
        `).join('');
        const morePods = nodePods.length > 80 ? `<span class="topo-more">+${nodePods.length - 80}</span>` : '';
        const role = node.roles?.includes('control-plane') ? 'control-plane' : 'worker';

        return `<section class="topo-node-card ${node.ready ? '' : 'faulty'}">
            <div class="topo-node-head">
                <div>
                    <h4>${escapeHtml(node.name)}</h4>
                    <span>${escapeHtml(role)} · CPU ${escapeHtml(node.cpu || '-')} · Mem ${escapeHtml(node.memory || '-')}</span>
                </div>
                <span class="badge badge-${node.ready ? 'success' : 'danger'}">${node.ready ? 'Ready' : 'NotReady'}</span>
            </div>
            <div class="topo-node-metrics">
                <div><strong>${nodePods.length}</strong><span>Pod</span></div>
                <div><strong>${faulty.length}</strong><span>异常</span></div>
                <div><strong>${namespaces.length}</strong><span>NS</span></div>
            </div>
            <div class="topo-namespace-tags">${topNamespaces || '<span>无工作负载</span>'}</div>
            <div class="topo-pod-dots">${podDots}${morePods}</div>
        </section>`;
    }).join('');
}

function renderTopologyDependencyMap(nodes, pods, deployments, services) {
    const container = document.getElementById('topo-dependency-map');
    if (!container) return;
    const namespaces = [...new Set(pods.map(p => p.namespace))].sort().slice(0, 6);
    const visibleServices = services.slice(0, 8);
    const visibleDeployments = deployments.slice(0, 8);
    const podSet = new Map();
    const matchesSelector = (selector, pod) => {
        const labels = pod.labels || {};
        return selector && Object.keys(selector).length && Object.entries(selector).every(([k, v]) => labels[k] === v);
    };
    visibleServices.forEach(svc => pods.filter(p => p.namespace === svc.namespace && matchesSelector(svc.selector, p)).slice(0, 4).forEach(p => podSet.set(`${p.namespace}/${p.name}`, p)));
    visibleDeployments.forEach(dep => pods.filter(p => p.namespace === dep.namespace && matchesSelector(dep.selector, p)).slice(0, 4).forEach(p => podSet.set(`${p.namespace}/${p.name}`, p)));
    pods.filter(p => p.is_faulty).slice(0, 10).forEach(p => podSet.set(`${p.namespace}/${p.name}`, p));
    if (!podSet.size) pods.slice(0, 12).forEach(p => podSet.set(`${p.namespace}/${p.name}`, p));
    const visiblePods = [...podSet.values()].slice(0, 18);
    const visibleNodes = nodes.slice(0, 8);

    const graphNodes = [];
    const graphEdges = [];
    const addNode = (id, kind, name, meta, x, y, status = '', faulty = false) => graphNodes.push({ id, kind, name, meta, x, y, status, faulty });
    const addEdge = (from, to, label = '') => {
        if (graphNodes.some(n => n.id === from) && graphNodes.some(n => n.id === to)) graphEdges.push({ from, to, label });
    };
    const maxRows = Math.max(namespaces.length, visibleServices.length, visibleDeployments.length, visiblePods.length, visibleNodes.length, 1);
    const baseHeight = Math.max(860, maxRows * 88 + 120);
    const rowY = (index, total, height, gap) => {
        const span = total * height + Math.max(0, total - 1) * gap;
        const top = Math.max(72, (baseHeight - span) / 2);
        return Math.round(top + index * (height + gap));
    };
    namespaces.forEach((ns, i) => {
        const nsPods = pods.filter(p => p.namespace === ns);
        const nsFaults = nsPods.filter(p => p.is_faulty).length;
        addNode(`ns:${ns}`, 'namespace', ns, `${nsPods.length} Pod`, 36, rowY(i, namespaces.length, 72, 38), nsFaults ? `${nsFaults} 异常` : 'OK', nsFaults > 0);
    });
    visibleServices.forEach((svc, i) => addNode(`svc:${svc.namespace}/${svc.name}`, 'service', svc.name, `${svc.namespace} · ${svc.type || '-'} · ${(svc.ports || []).join(',') || '-'}`, 292, rowY(i, visibleServices.length, 72, 26)));
    visibleDeployments.forEach((dep, i) => addNode(`dep:${dep.namespace}/${dep.name}`, 'deployment', dep.name, `${dep.namespace} · ${dep.available}/${dep.replicas} available`, 548, rowY(i, visibleDeployments.length, 72, 26), dep.is_faulty ? '异常' : 'OK', dep.is_faulty));
    visiblePods.forEach((pod, i) => addNode(`pod:${pod.namespace}/${pod.name}`, 'pod', pod.name, `${pod.namespace} · ${pod.ready} · ${pod.restarts} restarts`, 804, rowY(i, visiblePods.length, 72, 16), pod.phase || '-', pod.is_faulty));
    visibleNodes.forEach((node, i) => addNode(`node:${node.name}`, 'node', node.name, `${(node.roles || []).join(',') || 'worker'} · CPU ${node.cpu || '-'}`, 1060, rowY(i, visibleNodes.length, 72, 28), node.ready ? 'Ready' : 'NotReady', !node.ready));

    visibleServices.forEach(svc => {
        addEdge(`ns:${svc.namespace}`, `svc:${svc.namespace}/${svc.name}`, '包含');
        pods.filter(p => p.namespace === svc.namespace && matchesSelector(svc.selector, p)).slice(0, 4).forEach(p => addEdge(`svc:${svc.namespace}/${svc.name}`, `pod:${p.namespace}/${p.name}`, 'selector'));
    });
    visibleDeployments.forEach(dep => {
        addEdge(`ns:${dep.namespace}`, `dep:${dep.namespace}/${dep.name}`, '包含');
        pods.filter(p => p.namespace === dep.namespace && matchesSelector(dep.selector, p)).slice(0, 4).forEach(p => addEdge(`dep:${dep.namespace}/${dep.name}`, `pod:${p.namespace}/${p.name}`, '控制'));
    });
    visiblePods.forEach(p => addEdge(`pod:${p.namespace}/${p.name}`, `node:${p.node}`, '调度'));

    const byId = Object.fromEntries(graphNodes.map(n => [n.id, n]));
    const safeId = text => String(text).replace(/[^A-Za-z0-9_-]/g, '_');
    const clipText = (text, max = 20) => {
        const value = String(text || '-');
        return value.length > max ? `${value.slice(0, max - 1)}…` : value;
    };
    const svgHeight = Math.max(baseHeight, Math.max(0, ...graphNodes.map(n => n.y)) + 120);
    const pathFor = e => {
        const a = byId[e.from];
        const b = byId[e.to];
        const x1 = a.x + 178;
        const y1 = a.y + 34;
        const x2 = b.x;
        const y2 = b.y + 34;
        const mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    };
    container.innerHTML = `
        <div class="topo-dependency-canvas">
            <svg viewBox="0 0 1260 ${svgHeight}" role="img" aria-label="集群服务依赖拓扑图">
                <defs>
                    <marker id="topoArrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L0,6 L8,3 z"></path>
                    </marker>
                </defs>
                <g class="topo-dep-lanes">
                    <text x="36" y="34">Namespace</text>
                    <text x="292" y="34">Service</text>
                    <text x="548" y="34">Deployment</text>
                    <text x="804" y="34">Pod</text>
                    <text x="1060" y="34">Node</text>
                </g>
                ${graphEdges.map(e => `<path id="topo-edge-${safeId(e.from + '-' + e.to)}" class="topo-dep-edge" d="${pathFor(e)}"></path>`).join('')}
                ${graphNodes.map(n => `
                    <g class="topo-dep-node ${escapeHtml(n.kind)} ${n.faulty ? 'faulty' : ''}" transform="translate(${n.x}, ${n.y})">
                        <rect width="178" height="72" rx="5"></rect>
                        <text x="12" y="18" class="kind">${escapeHtml(n.kind)}</text>
                        <text x="12" y="39" class="name">${escapeHtml(clipText(n.name, 18))}</text>
                        <text x="12" y="57" class="meta">${escapeHtml(clipText(n.meta, 26))}</text>
                        ${n.status ? `<text x="166" y="18" class="status" text-anchor="end">${escapeHtml(n.status)}</text>` : ''}
                    </g>
                `).join('')}
            </svg>
        </div>
        <div class="topo-dependency-note">当前展示过滤范围内最多 6 个命名空间、8 个 Service、8 个 Deployment、18 个关键 Pod 和 8 个节点；异常实体优先展示。</div>
    `;
}

function renderTopologyFaults(pods, deployments) {
    const el = document.getElementById('topo-fault-list');
    if (!el) return;
    const podFaults = pods.filter(p => p.is_faulty).slice(0, 12).map(p => ({
        type: 'Pod',
        name: p.name,
        namespace: p.namespace,
        detail: p.fault_reason || p.phase,
    }));
    const depFaults = deployments.filter(d => d.is_faulty).slice(0, 8).map(d => ({
        type: 'Deployment',
        name: d.name,
        namespace: d.namespace,
        detail: `${d.available}/${d.replicas} available`,
    }));
    const items = [...podFaults, ...depFaults];
    if (!items.length) {
        el.innerHTML = '<p class="text-muted">当前过滤范围内无异常实体</p>';
        return;
    }
    el.innerHTML = items.map(item => `
        <div class="topo-fault-item">
            <span class="badge badge-danger">${item.type}</span>
            <div>
                <strong>${escapeHtml(item.name)}</strong>
                <p>${escapeHtml(item.namespace)} · ${escapeHtml(item.detail)}</p>
            </div>
        </div>
    `).join('');
}

function renderTopologyNamespaces(namespaces, pods, deployments) {
    const el = document.getElementById('topo-namespace-list');
    if (!el) return;
    const rows = namespaces.map(ns => {
        const nsPods = (_topo.topoData?.pods || []).filter(p => p.namespace === ns);
        const nsDeps = (_topo.topoData?.deployments || []).filter(d => d.namespace === ns);
        const faults = nsPods.filter(p => p.is_faulty).length + nsDeps.filter(d => d.is_faulty).length;
        return { ns, pods: nsPods.length, deps: nsDeps.length, faults };
    }).sort((a, b) => b.faults - a.faults || b.pods - a.pods).slice(0, 14);

    el.innerHTML = rows.map(row => `
        <button class="topo-ns-row ${_topo.nsFilter === row.ns ? 'active' : ''}" onclick="selectTopologyNamespace('${escapeHtml(row.ns)}')">
            <span>${escapeHtml(row.ns)}</span>
            <small>${row.pods} pod · ${row.deps} deploy${row.faults ? ` · ${row.faults} 异常` : ''}</small>
        </button>
    `).join('');
}

function selectTopologyNamespace(ns) {
    const sel = document.getElementById('topo-ns-filter');
    if (sel) sel.value = ns;
    _topo.nsFilter = ns;
    if (_topo.topoData) renderTopology(_topo.topoData);
}

// ─────────────────────────────────────────
// Metrics (Charts + Instant Values)
// ─────────────────────────────────────────

function setMetricRange(range, btn) {
    state.metricRange = range;
    document.querySelectorAll('#metrics-time-range .time-range-btn').forEach(b => b.classList.remove('active'));
    const activeBtn = btn || document.querySelector(`#metrics-time-range .time-range-btn[data-range="${range}"]`);
    if (activeBtn) activeBtn.classList.add('active');
    loadMetricCharts();
}

async function loadMetrics() {
    const ns = document.getElementById('metrics-ns')?.value || '';
    loadNamespaces('metrics-ns');

    // Load both charts and instant values in parallel
    await Promise.all([
        loadMetricCharts(),
        loadInstantMetrics(ns),
    ]);
}

async function loadInstantMetrics(ns) {
    const [metrics, podData] = await Promise.all([
        api(`/api/prometheus/metrics_summary?namespace=${ns || ''}`),
        api(`/api/cluster/pods?namespace=${ns || ''}`),
    ]);

    if (metrics) {
        renderMetricBars('metrics-node-cpu', metrics.node_cpu, '%');
        renderMetricBars('metrics-node-memory', metrics.node_memory, '%');
        renderMetricBars('metrics-node-disk', metrics.node_disk, '%');
        renderContainerTop('metrics-cpu-top', metrics.container_cpu_top, '%');
        renderContainerTop('metrics-mem-top', metrics.container_mem_top, 'MB');
    }

    if (podData?.pods) {
        const tbody = document.querySelector('#pod-table tbody');
        tbody.innerHTML = podData.pods.map(p => `
            <tr>
                <td>${escapeHtml(p.name)}</td>
                <td>${escapeHtml(p.namespace)}</td>
                <td><span class="badge badge-${badgeClass(p.phase)}">${p.phase}</span></td>
                <td>${p.ready}</td>
                <td class="${p.restarts > 5 ? 'text-danger' : ''}">${p.restarts}</td>
                <td>${escapeHtml(p.node || '')}</td>
            </tr>
        `).join('');
    }
}

async function loadMetricCharts() {
    const range = state.metricRange;
    const ns = document.getElementById('metrics-ns')?.value || '';
    const nsParam = ns ? `&namespace=${encodeURIComponent(ns)}` : '';
    const isOtelDemo = ns === 'otel-demo';
    const rateWindow = ['5m', '10m', '15m', '30m'].includes(range) ? '1m' : '5m';
    const otelRateWindow = ['5m', '10m', '15m', '30m'].includes(range) ? '2m' : '5m';

    // ── Node metrics ──
    const [cpuData, memData, diskData] = await Promise.all([
        api(`/api/prometheus/metric_history?metric_name=node_cpu_usage&duration=${range}`),
        api(`/api/prometheus/metric_history?metric_name=node_memory_usage&duration=${range}`),
        api(`/api/prometheus/metric_history?metric_name=node_disk_usage&duration=${range}`),
    ]);
    renderTimeSeriesChart('chart-node-cpu', cpuData, '%');
    renderTimeSeriesChart('chart-node-memory', memData, '%');
    renderTimeSeriesChart('chart-node-disk', diskData, '%');

    // ── Network metrics ──
    const netQueries = {
        'chart-net-receive': `sum by(instance)(rate(node_network_receive_bytes_total{device!="lo"}[${rateWindow}])) / 1024`,
        'chart-net-transmit': `sum by(instance)(rate(node_network_transmit_bytes_total{device!="lo"}[${rateWindow}])) / 1024`,
        'chart-tcp-established': 'node_netstat_Tcp_CurrEstab',
        'chart-tcp-retrans': `sum by(instance)(rate(node_netstat_Tcp_RetransSegs[${rateWindow}]))`,
        'chart-dns-requests': `sum by(pod)(rate(coredns_dns_requests_total[${rateWindow}]))`,
    };
    const netUnits = {
        'chart-net-receive': ' KB/s', 'chart-net-transmit': ' KB/s',
        'chart-tcp-established': '', 'chart-tcp-retrans': '/s', 'chart-dns-requests': '/s',
    };
    const netPromises = Object.entries(netQueries).map(([id, q]) =>
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(q)}&duration=${range}`).then(d => [id, d])
    );
    const netResults = await Promise.all(netPromises);
    for (const [id, data] of netResults) {
        renderTimeSeriesChart(id, data, netUnits[id] || '');
    }

    // ── Container metrics — filtered by namespace ──
    const nsFilter = ns ? `namespace="${ns}"` : '';
    const containerCpuQuery = nsFilter
        ? `sum by(pod, namespace)(rate(container_cpu_usage_seconds_total{${nsFilter}}[${rateWindow}])) * 100`
        : `sum by(pod, namespace)(rate(container_cpu_usage_seconds_total[${rateWindow}])) * 100`;
    const containerMemQuery = nsFilter
        ? `sum by(pod, namespace)(container_memory_working_set_bytes{${nsFilter}}) / 1024 / 1024`
        : `sum by(pod, namespace)(container_memory_working_set_bytes) / 1024 / 1024`;

    const [ccpuData, cmemData] = await Promise.all([
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(containerCpuQuery)}&duration=${range}&max_series=10`),
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(containerMemQuery)}&duration=${range}&max_series=10`),
    ]);
    renderTimeSeriesChart('chart-container-cpu', ccpuData, '%');
    renderTimeSeriesChart('chart-container-mem', cmemData, 'MB');

    // ── Business metrics ──
    const bizSource = isOtelDemo ? 'otel' : 'cluster';
    const bizQueries = isOtelDemo ? {
        'chart-biz-restarts': `sum(rate(app_frontend_requests_total{service_name="frontend"}[${otelRateWindow}]))`,
        'chart-biz-replicas': `histogram_quantile(0.99, sum by(le)(rate(http_server_duration_milliseconds_bucket{service_name="frontend"}[${otelRateWindow}])))`,
        'chart-biz-apiserver': `(sum(rate(app_frontend_requests_total{service_name="frontend",status=~"5.."}[${otelRateWindow}])) or vector(0)) + (max(httpcheck_status{service_name="frontend-proxy",http_status_class="5xx"}) or vector(0))`,
    } : {
        'chart-biz-restarts': ns
            ? `sum(rate(kube_pod_container_status_restarts_total{namespace="${ns}"}[${rateWindow}])) * 300`
            : `sum(rate(kube_pod_container_status_restarts_total[${rateWindow}])) * 300`,
        'chart-biz-replicas': nsFilter
            ? `sum by(deployment)(kube_deployment_status_replicas_available{${nsFilter}})`
            : 'sum by(deployment)(kube_deployment_status_replicas_available)',
        'chart-biz-apiserver': 'sum by(verb)(rate(apiserver_request_total[5m]))',
    };
    const bizUnits = isOtelDemo
        ? { 'chart-biz-restarts': '/s', 'chart-biz-replicas': ' ms', 'chart-biz-apiserver': '/s' }
        : { 'chart-biz-restarts': '/s', 'chart-biz-replicas': '', 'chart-biz-apiserver': '/s' };

    if (isOtelDemo) {
        const titles = {
            'chart-biz-restarts': 'frontend 请求量 (/s)',
            'chart-biz-replicas': 'frontend P99 延迟 (ms)',
            'chart-biz-apiserver': 'frontend/proxy 5xx 信号',
        };
        Object.entries(titles).forEach(([id, text]) => {
            const canvas = document.getElementById(id);
            const header = canvas?.closest('.card')?.querySelector('.chart-header h3');
            if (header) header.textContent = text;
        });
    } else {
        const titles = {
            'chart-biz-restarts': 'Pod 重启次数 (by namespace)',
            'chart-biz-replicas': 'Deployment 可用副本',
            'chart-biz-apiserver': 'API Server 请求速率 (/s)',
        };
        Object.entries(titles).forEach(([id, text]) => {
            const canvas = document.getElementById(id);
            const header = canvas?.closest('.card')?.querySelector('.chart-header h3');
            if (header) header.textContent = text;
        });
    }
    const bizPromises = Object.entries(bizQueries).map(([id, q]) =>
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(q)}&duration=${range}&max_series=10&source=${bizSource}`).then(d => [id, d])
    );
    const bizResults = await Promise.all(bizPromises);
    for (const [id, data] of bizResults) {
        renderTimeSeriesChart(id, data, bizUnits[id] || '');
    }
}

function _fmtTs(ts) {
    const d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

function normalizeSeriesLabel(label) {
    const text = String(label || '').trim();
    if (!text || text === '{}' || text === '__unknown__') return '聚合序列';
    return text;
}

function renderTimeSeriesChart(canvasId, data, unit) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    if (state.charts[canvasId]) {
        state.charts[canvasId].destroy();
        delete state.charts[canvasId];
    }

    // Update anomaly badge on chart header
    const chartCard = canvas.closest('.card');
    if (chartCard) {
        const existing = chartCard.querySelector('.anomaly-badge');
        if (existing) existing.remove();
        if (data?.anomalies?.length > 0) {
            const badge = document.createElement('span');
            badge.className = 'anomaly-badge';
            const methods = [...new Set(data.anomalies.map(a => a.method || 'zscore'))].join('+');
            const critical = data.anomalies.filter(a => (a.severity || '').toLowerCase() === 'critical').length;
            badge.innerHTML = `<span class="anomaly-badge-pill">${data.anomalies.length} 异常点 (${methods})${critical ? ` · ${critical} critical` : ''}</span>`;
            const header = chartCard.querySelector('.chart-header');
            if (header) header.appendChild(badge);
        }
        const existingLegend = chartCard.querySelector('.anomaly-legend');
        if (existingLegend) existingLegend.remove();
        if (data?.anomalies?.length > 0) {
            const legend = document.createElement('div');
            legend.className = 'anomaly-legend';
            const methods = [...new Set(data.anomalies.map(a => a.method || 'zscore'))].slice(0, 3).join(' / ');
            legend.innerHTML = `
                <span><i class="anom-dot warning"></i> warning</span>
                <span><i class="anom-dot critical"></i> critical</span>
                <span>${escapeHtml(methods)} 检测，悬停异常点查看时间、序列和分数</span>
            `;
            canvas.parentElement.insertBefore(legend, canvas);
        }
        // Show detection config info
        const existingInfo = chartCard.querySelector('.detection-info');
        if (existingInfo) existingInfo.remove();
        if (data?.detection) {
            const info = document.createElement('div');
            info.className = 'detection-info';
            info.style.cssText = 'font-size:10px;color:var(--text-muted);margin-top:-8px;margin-bottom:8px;';
            info.textContent = `detection: ${data.detection.methods?.join(', ') || '-'} | z=${data.detection.z_threshold} | ewma_span=${data.detection.ewma_span}`;
            canvas.parentElement.insertBefore(info, canvas);
        }
    }

    if (!data || !data.series || data.series.length === 0) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#a0aec0';
        ctx.font = '13px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('暂无数据', canvas.width / 2, canvas.height / 2);
        return;
    }

    // Use category labels (HH:mm) from the first series timestamps
    const refTimestamps = data.series[0].timestamps;
    const labels = refTimestamps.map(t => _fmtTs(t));

    // Thin out labels to avoid crowding (show ~12 labels max)
    const step = Math.max(1, Math.floor(labels.length / 12));
    const displayLabels = labels.map((l, i) => i % step === 0 ? l : '');

    const datasets = data.series.map((s, i) => ({
        label: normalizeSeriesLabel(s.label),
        data: s.values,
        borderColor: CHART_COLORS[i % CHART_COLORS.length],
        backgroundColor: CHART_COLORS[i % CHART_COLORS.length] + '18',
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: true,
        tension: 0.3,
    }));

    const anomaliesBySeries = {};
    (data.anomalies || []).forEach((a) => {
        const idx = a.index != null ? a.index : -1;
        if (idx < 0 || idx >= refTimestamps.length) return;
        const key = normalizeSeriesLabel(a.series_label);
        if (!anomaliesBySeries[key]) anomaliesBySeries[key] = [];
        anomaliesBySeries[key].push({
            x: labels[idx],
            y: a.value,
            _anomaly: true,
            severity: a.severity || 'warning',
            method: a.method || 'zscore',
            zscore: a.zscore,
            series_label: key,
            timestamp: labels[idx],
        });
    });
    Object.entries(anomaliesBySeries).forEach(([seriesLabel, points]) => {
        const hasCritical = points.some(p => p.severity === 'critical');
        datasets.push({
            label: `异常: ${seriesLabel}`,
            type: 'scatter',
            data: points,
            borderColor: hasCritical ? '#dc2626' : '#d97706',
            backgroundColor: hasCritical ? '#ef4444' : '#f59e0b',
            pointRadius: 5,
            pointHoverRadius: 8,
            pointStyle: 'triangle',
            showLine: false,
            order: -10,
        });
    });

    // Build anomaly annotation points (using data index)
    const anomalyAnnotations = {};
    if (data.anomalies && data.anomalies.length > 0) {
        data.anomalies.forEach((a, i) => {
            const idx = a.index != null ? a.index : -1;
            if (idx >= 0 && idx < refTimestamps.length) {
                const color = a.severity === 'critical' ? '#ef4444' : '#f59e0b';
                anomalyAnnotations[`anomaly_${i}`] = {
                    type: 'point',
                    xValue: idx,
                    yValue: a.value,
                    radius: 6,
                    backgroundColor: color,
                    borderColor: '#ffffff',
                    borderWidth: 2,
                    label: {
                        content: `${a.severity} z=${a.zscore}`,
                        display: true,
                        position: 'end',
                        font: { size: 9, weight: 'bold' },
                        color: color,
                        backgroundColor: 'rgba(255,255,255,0.85)',
                        padding: 3,
                    },
                };
            }
        });
    }

    // Threshold lines
    if (data.thresholds) {
        if (data.thresholds.warn != null) {
            anomalyAnnotations['warn_line'] = {
                type: 'line',
                yMin: data.thresholds.warn, yMax: data.thresholds.warn,
                borderColor: '#f59e0b88', borderWidth: 1.5, borderDash: [6, 4],
                label: { content: `Warn ${data.thresholds.warn}`, display: true, position: 'start', font: { size: 10 }, color: '#d97706' },
            };
        }
        if (data.thresholds.crit != null) {
            anomalyAnnotations['crit_line'] = {
                type: 'line',
                yMin: data.thresholds.crit, yMax: data.thresholds.crit,
                borderColor: '#ef444488', borderWidth: 1.5, borderDash: [6, 4],
                label: { content: `Crit ${data.thresholds.crit}`, display: true, position: 'start', font: { size: 10 }, color: '#ef4444' },
            };
        }
    }

    try {
        state.charts[canvasId] = new Chart(canvas, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        grid: { color: 'rgba(226,232,240,0.3)' },
                        ticks: { color: '#a0aec0', font: { size: 10 }, maxRotation: 0, callback: (_v, i) => displayLabels[i] || '' },
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(226,232,240,0.4)' },
                        ticks: { color: '#a0aec0', font: { size: 10 }, callback: (v) => v + unit },
                    },
                },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { padding: 12, font: { size: 11 }, usePointStyle: true, pointStyle: 'circle' },
                    },
                    tooltip: {
                        backgroundColor: '#1a2b42', titleFont: { size: 12 }, bodyFont: { size: 11 },
                        padding: 10, cornerRadius: 6,
                        callbacks: {
                            title: (items) => {
                                const raw = items[0]?.raw;
                                return raw?._anomaly ? `异常时间 ${raw.timestamp}` : (items[0] ? labels[items[0].dataIndex] || '' : '');
                            },
                            label: (ctx) => {
                                const raw = ctx.raw || {};
                                if (raw._anomaly) {
                                    const score = raw.zscore != null ? ` z=${raw.zscore}` : '';
                                    return `${raw.series_label}: ${Number(raw.y || 0).toFixed(2)}${unit} · ${raw.severity} · ${raw.method}${score}`;
                                }
                                return `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(2)}${unit}`;
                            },
                        },
                    },
                    annotation: { annotations: anomalyAnnotations },
                },
                animation: { duration: 600, easing: 'easeOutQuart' },
            }
        });
    } catch(e) {
        console.error(`Chart render error [${canvasId}]:`, e);
    }
}

function renderBusinessChart(canvasId, data, unit) {
    if (!data) {
        renderTimeSeriesChart(canvasId, null, unit);
        return;
    }

    const series = [];
    const results = data.results || [];
    for (const r of results) {
        const metric = r.metric || {};
        const label = metric.service || metric.destination_service_name || Object.values(metric)[0] || 'unknown';
        const values_raw = r.values || [];
        series.push({
            label,
            timestamps: values_raw.map(v => v[0]),
            values: values_raw.map(v => {
                const f = parseFloat(v[1]);
                return isNaN(f) || !isFinite(f) ? 0 : f;
            }),
        });
    }

    renderTimeSeriesChart(canvasId, { series, anomalies: [], thresholds: {} }, unit);
}

function renderMetricBars(containerId, data, unit) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const results = data?.results || [];
    if (!results.length) {
        el.innerHTML = '<p class="text-muted" style="padding:8px">暂无数据</p>';
        return;
    }

    el.innerHTML = results.map(r => {
        const label = r.metric?.instance || r.metric?.node || Object.values(r.metric || {})[0] || 'unknown';
        const shortLabel = label.replace(/:.*$/, '');
        const val = parseFloat(r.value?.[1] || 0).toFixed(1);
        const pct = Math.min(parseFloat(val), 100);
        const color = pct > 90 ? 'var(--danger)' : pct > 75 ? 'var(--warning)' : 'var(--accent)';
        return `
            <div class="metric-bar-item">
                <div class="metric-bar-label">
                    <span>${escapeHtml(shortLabel)}</span>
                    <strong>${val}${unit}</strong>
                </div>
                <div class="metric-bar-track">
                    <div class="metric-bar-fill" style="width:${pct}%;background:${color}"></div>
                </div>
            </div>
        `;
    }).join('');
}

function renderContainerTop(tableId, data, unit) {
    const tbody = document.querySelector(`#${tableId} tbody`);
    if (!tbody) return;

    const results = data?.results || [];
    if (!results.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-muted" style="text-align:center">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = results.map(r => {
        const pod = r.metric?.pod || 'unknown';
        const ns = r.metric?.namespace || '-';
        const val = parseFloat(r.value?.[1] || 0).toFixed(2);
        return `<tr><td>${escapeHtml(pod)}</td><td>${escapeHtml(ns)}</td><td>${val} ${unit}</td></tr>`;
    }).join('');
}

async function runPromQL() {
    const query = document.getElementById('promql-input')?.value?.trim();
    const queryType = document.getElementById('promql-type')?.value || 'instant';
    const resultEl = document.getElementById('promql-result');

    if (!query) { resultEl.textContent = '请输入 PromQL 查询'; return; }

    resultEl.textContent = '查询中...';
    const data = await api(`/api/prometheus/query?query=${encodeURIComponent(query)}&query_type=${queryType}`);

    if (!data || data.error) {
        resultEl.textContent = `错误: ${data?.error || '请求失败'}`;
        return;
    }

    resultEl.textContent = JSON.stringify(data.results || data, null, 2);
}

// ─────────────────────────────────────────
// Logs
// ─────────────────────────────────────────

async function loadNamespaces(selectId) {
    const data = await api('/api/cluster/namespaces');
    if (!data?.namespaces) return;

    const sel = document.getElementById(selectId);
    const current = sel.value;
    const firstOpt = sel.querySelector('option')?.textContent || '选择命名空间';
    sel.innerHTML = `<option value="">${firstOpt}</option>` +
        data.namespaces.map(ns => `<option value="${ns}" ${ns === current ? 'selected' : ''}>${ns}</option>`).join('');
}

async function loadPodsByNs() {
    const ns = document.getElementById('log-ns').value;
    if (!ns) return;

    const data = await api(`/api/cluster/pods?namespace=${ns}`);
    if (!data?.pods) return;

    const sel = document.getElementById('log-pod');
    sel.innerHTML = '<option value="">选择Pod</option>' +
        data.pods.map(p => `<option value="${p.name}">${p.name} (${p.phase})</option>`).join('');
}

async function fetchLogs() {
    const ns = document.getElementById('log-ns').value;
    const pod = document.getElementById('log-pod').value;
    const lines = document.getElementById('log-lines').value || 200;

    if (!ns || !pod) { alert('请选择命名空间和Pod'); return; }

    const viewer = document.getElementById('log-content');
    viewer.textContent = '加载中...';
    const data = await api(`/api/logs/${ns}/${pod}?lines=${lines}`);
    viewer.textContent = data?.logs || '无日志内容';
}

// ─────────────────────────────────────────
// Alerts
// ─────────────────────────────────────────

let _alertData = [];
let _filteredAlerts = [];
let _alertPage = 1;
let _alertPageSize = 10;
let _activeAlertHealIndex = null;
let _activeAlertDispositionKey = '';
let _lastAlertHealResult = null;
let _activeAlertHealHtml = '';
let _currentHealPlan = null;
let _currentHealPlanSourcePayload = null;
let _lastHealResult = null;
let _lastHealRuns = [];
let _activeRCAAlertKey = null;
const _alertDiagnosisCache = new Map();
const _alertSkillWorkflowCache = new Map();
const _alertAgentRunCache = new Map();
const _alertAgentStatusCache = new Map();
const _alertSkillAgentRunCache = new Map();
const _alertSkillAgentStatusCache = new Map();
const _alertSkillAgentPollers = new Map();
const _alertAgentPollers = new Map();
const _alertHealCapabilityCache = new Map();
const _alertDispositionState = new Map();
const ALERT_DISPOSITION_STORAGE_KEY = 'agenticsre.alertDisposition.v2';

const SOURCE_CONFIG = {
    k8s_event:   { label: 'K8s事件',    icon: '&#9783;', cls: 'source-k8s' },
    prometheus:  { label: 'Prometheus', icon: '&#9636;', cls: 'source-prom' },
    pod_health:  { label: 'Pod健康',    icon: '&#9673;', cls: 'source-pod' },
    node_health: { label: '节点健康',   icon: '&#9633;', cls: 'source-node' },
    metric_anomaly: { label: '指标异常', icon: '&#9650;', cls: 'source-metric' },
};

async function loadAlertList() {
    hydrateAlertDispositionState();
    const duration = document.getElementById('alert-window-filter')?.value || '1h';
    if (!(_faultTargets.k8s_clusters || []).length) await loadFaultTargets();
    const clusterId = document.getElementById('alert-cluster-filter')?.value || '';
    restoreAlertScan(clusterId);
    const params = new URLSearchParams();
    if (duration) params.set('duration', duration);
    if (clusterId) params.set('cluster_id', clusterId);
    const data = await api(`/api/alerts/list?${params.toString()}`);
    if (!data) return;
    _alertData = data.alerts || [];
    _alertPage = 1;
    if (data.degraded) {
        showToast(data.warning || '告警采集超时，当前显示降级结果', 'warning');
    }
    refreshOpenAlertDispositionModal();
    renderAlertSourceStats(_alertData, data);
    renderAlertDispositionHistory();
    filterAlerts();
}

function renderAlertSourceStats(alerts, meta = {}) {
    const stats = {};
    alerts.forEach(a => { stats[a.source] = (stats[a.source] || 0) + 1; });

    const container = document.getElementById('alert-source-stats');
    const sourceInfo = {
        k8s_event:   { label: 'K8s事件',    color: 'var(--info)' },
        prometheus:  { label: 'Prometheus', color: 'var(--accent)' },
        pod_health:  { label: 'Pod健康',    color: 'var(--danger)' },
        node_health: { label: '节点健康',   color: 'var(--warning)' },
        metric_anomaly: { label: '指标异常', color: 'var(--success)' },
    };

    const activeTotal = Number(meta.active_total ?? alerts.filter(a => a.active !== false).length);
    const durationLabel = document.getElementById('alert-window-filter')?.selectedOptions?.[0]?.textContent || '当前窗口';
    container.innerHTML = Object.entries(stats).map(([src, count]) => {
        const info = sourceInfo[src] || { label: src, color: 'var(--text-muted)' };
        return `<div class="stat-card" style="border-left:3px solid ${info.color}">
            <div class="stat-label">${info.label}</div>
            <div class="stat-value">${count}</div>
        </div>`;
    }).join('') + `<div class="stat-card accent">
        <div class="stat-label">${escapeHtml(durationLabel)}</div>
        <div class="stat-value">${alerts.length}</div>
        <div class="stat-sub">当前活跃 ${escapeHtml(activeTotal)}</div>
    </div>`;
}

function renderAlertDispositionHistory() {
    const container = document.getElementById('alert-disposition-history');
    if (!container) return;
    const records = [..._alertDispositionState.values()]
        .filter(item => item.key && !isAlertCurrentlyActive(item.key))
        .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
        .slice(0, 5);
    if (!records.length) {
        container.innerHTML = '';
        return;
    }
    container.innerHTML = `<div class="alert-history-strip">
        <div>
            <strong>已消除告警的处理记录</strong>
            <span>告警不再出现在当前列表中，仅保留分析/自愈状态，不再触发新动作。</span>
        </div>
        <div class="alert-history-items">
            ${records.map(item => {
                const a = item.alert || {};
                const stage = item.stage === 'healing' ? '自愈' : '分析';
                return `<button class="btn btn-sm" onclick="openResolvedAlertDisposition('${escapeHtmlAttr(item.key)}')">
                    <span class="badge badge-gray">已消除</span>
                    ${escapeHtml(a.title || a.service || item.key)}
                    <span class="text-muted">${escapeHtml(stage)}</span>
                </button>`;
            }).join('')}
        </div>
    </div>`;
}

function openResolvedAlertDisposition(key) {
    const item = alertDispositionState(key);
    if (!item?.alert) return;
    openAlertDispositionModal(-1, {
        key,
        alert: item.alert,
        stage: item.stage || 'details',
        active: false,
    });
    if (_alertAgentRunCache.get(key)?.run_id) refreshAlertAgentRun(key, { poll: true, silent: true });
    if (_alertSkillAgentRunCache.get(key)?.run_id) refreshAlertSkillAgentRun(key, { poll: true, silent: true });
}

function filterAlerts(source) {
    if (source) {
        document.querySelectorAll('.alert-source-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.alert-source-btn[data-source="${source}"]`)?.classList.add('active');
    }

    const activeSource = document.querySelector('.alert-source-btn.active')?.dataset.source || 'all';
    const severityFilter = document.getElementById('alert-severity-filter').value;
    _alertPageSize = Number(document.getElementById('alert-page-size')?.value || _alertPageSize || 10);

    let filtered = _alertData;
    if (activeSource !== 'all') filtered = filtered.filter(a => a.source === activeSource);
    if (severityFilter !== 'all') filtered = filtered.filter(a => a.severity === severityFilter);

    if (source) _alertPage = 1;
    renderAlertTable(filtered);
}

function renderAlertTable(alerts) {
    _filteredAlerts = alerts;
    const tbody = document.getElementById('alert-table-body');
    const empty = document.getElementById('alert-empty');

    if (!alerts.length) {
        tbody.innerHTML = '';
        empty.style.display = 'block';
        renderAlertPagination(0);
        return;
    }
    empty.style.display = 'none';

    const pageSize = Math.max(5, Math.min(100, Number(_alertPageSize || 10)));
    const totalPages = Math.max(1, Math.ceil(alerts.length / pageSize));
    _alertPage = Math.max(1, Math.min(Number(_alertPage || 1), totalPages));
    const start = (_alertPage - 1) * pageSize;
    const pageAlerts = alerts.slice(start, start + pageSize);
    tbody.innerHTML = pageAlerts.map((a, offset) => {
        const i = start + offset;
        const src = SOURCE_CONFIG[a.source] || { label: a.source, icon: '?', cls: '' };
        const sevClass = a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info';
        const key = alertDiagnosisKey(a);
        const disposition = alertDispositionState(key);
        const inProgress = !!(disposition.stage || disposition.agentRun || disposition.skillResult || disposition.healResult);
        const isActive = _activeAlertDispositionKey === key;
        const historical = a.active === false;
        return `<tr class="${isActive ? 'alert-row-active' : ''}">
            <td><span class="source-badge ${src.cls}">${src.icon} ${src.label}</span></td>
            <td><span class="badge badge-${sevClass}">${a.severity}</span></td>
            <td title="${escapeHtml(a.description || '')}">
                ${escapeHtml(a.title || (a.description || '').substring(0, 80) || '')}
                ${inProgress ? '<span class="badge badge-info" style="margin-left:6px">处理中</span>' : ''}
                ${historical ? '<span class="badge badge-gray" style="margin-left:6px">历史</span>' : '<span class="badge badge-success" style="margin-left:6px">活跃</span>'}
            </td>
            <td>${escapeHtml(a.service || '')}</td>
            <td>${escapeHtml(a.namespace || '')}</td>
            <td>${formatTime(a.timestamp ? a.timestamp * 1000 : null)}</td>
            <td>
                <div class="table-actions alert-branch-actions">
                    <button class="btn btn-sm btn-primary" onclick="showAlertDisposition(${i}, 'analysis')">分析</button>
                    <button class="btn btn-sm btn-secondary" onclick="showAlertDisposition(${i}, 'healing')">自愈</button>
                    <button class="btn btn-sm" onclick="showAlertDisposition(${i}, 'details')">详情</button>
                </div>
            </td>
        </tr>`;
    }).join('');
    renderAlertPagination(alerts.length);
}

function renderAlertPagination(total) {
    const el = document.getElementById('alert-pagination');
    if (!el) return;
    const pageSize = Math.max(5, Math.min(100, Number(_alertPageSize || 10)));
    const totalPages = Math.max(1, Math.ceil(Number(total || 0) / pageSize));
    _alertPage = Math.max(1, Math.min(Number(_alertPage || 1), totalPages));
    const start = total ? ((_alertPage - 1) * pageSize + 1) : 0;
    const end = total ? Math.min(total, _alertPage * pageSize) : 0;
    el.innerHTML = `<div class="alert-pagination-info">
            <strong>${escapeHtml(total)}</strong> 条告警
            <span>当前 ${escapeHtml(start)}-${escapeHtml(end)} / 第 ${escapeHtml(_alertPage)} 页，共 ${escapeHtml(totalPages)} 页</span>
        </div>
        <div class="alert-pagination-actions">
            <select id="alert-page-size" class="select-sm" onchange="setAlertPageSize(this.value)">
                ${[5, 10, 20, 50, 100].map(n => `<option value="${n}" ${n === pageSize ? 'selected' : ''}>每页 ${n}</option>`).join('')}
            </select>
            <button class="btn btn-sm" ${_alertPage <= 1 ? 'disabled' : ''} onclick="setAlertPage(${_alertPage - 1})">上一页</button>
            <button class="btn btn-sm" ${_alertPage >= totalPages ? 'disabled' : ''} onclick="setAlertPage(${_alertPage + 1})">下一页</button>
        </div>`;
}

function setAlertPage(page) {
    _alertPage = Number(page || 1);
    filterAlerts();
}

function setAlertPageSize(size) {
    _alertPageSize = Number(size || 10);
    _alertPage = 1;
    filterAlerts();
}

function buildHealPayloadFromAlert(a) {
    const description = a?.description || a?.message || '';
    const raw = a?.raw_data || {};
    const involved = raw?.involvedObject || {};
    const faultType = inferSkillFaultTypeFromAlert(a);
    return {
        source: 'alert',
        alert: {
            source: a?.source || '',
            severity: a?.severity || '',
            title: a?.title || '',
            description,
            namespace: a?.namespace || '',
            service: a?.service || '',
            labels: a?.labels || {},
            raw_data: {
                reason: raw?.reason || '',
                message: raw?.message || description,
                involvedObject: {
                    kind: involved?.kind || '',
                    name: involved?.name || '',
                    namespace: involved?.namespace || a?.namespace || '',
                },
                metadata: {
                    namespace: raw?.metadata?.namespace || a?.namespace || '',
                },
            },
        },
        namespace: a?.namespace || 'default',
        fault_type: faultType,
        raw_fault_type: raw?.reason || a?.labels?.alertname || '',
        message: `[${(a?.severity || 'warning').toUpperCase()}] ${a?.title || ''} ${description}`,
        object: a?.service || '',
        pod: a?.source === 'pod_health' || /pod/i.test(a?.title || '') ? a?.service || '' : '',
        node: a?.source === 'node_health' ? a?.service || '' : '',
        evidence: [a?.source || '', a?.severity || '', description],
    };
}

function alertDiagnosisKey(a) {
    if (!a) return '';
    const raw = a.raw_data || {};
    const involved = raw.involvedObject || {};
    if (a.fingerprint) return `fp:${a.fingerprint}`;
    if (a.signal_id) return `sig:${a.signal_id}`;
    return [
        a.source || '',
        a.namespace || '',
        a.service || '',
        a.title || '',
        involved.kind || '',
        involved.name || '',
        raw.reason || '',
    ].join('|');
}

function getAlertByKey(key, list = _alertData) {
    return (list || []).find(a => alertDiagnosisKey(a) === key) || null;
}

function activeAlertIndexByKey(key) {
    return (_filteredAlerts || []).findIndex(a => alertDiagnosisKey(a) === key);
}

function isAlertCurrentlyActive(key) {
    const alert = getAlertByKey(key, _alertData);
    return !!alert && alert.active !== false;
}

function loadAlertDispositionState() {
    try {
        const raw = sessionStorage.getItem(ALERT_DISPOSITION_STORAGE_KEY);
        const items = raw ? JSON.parse(raw) : [];
        _alertDispositionState.clear();
        if (Array.isArray(items)) {
            items.forEach(item => {
                if (item?.key) _alertDispositionState.set(item.key, item);
            });
        }
    } catch {
        _alertDispositionState.clear();
    }
}

function persistAlertDispositionState() {
    try {
        const items = [..._alertDispositionState.values()]
            .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
            .slice(0, 30);
        sessionStorage.setItem(ALERT_DISPOSITION_STORAGE_KEY, JSON.stringify(items));
    } catch {}
}

function updateAlertDispositionState(key, patch = {}) {
    if (!key) return null;
    const prev = _alertDispositionState.get(key) || {};
    const next = { ...prev, key, updated_at: Date.now(), ...patch };
    _alertDispositionState.set(key, next);
    persistAlertDispositionState();
    return next;
}

function alertDispositionState(key) {
    return _alertDispositionState.get(key) || {};
}

function hydrateAlertDispositionState() {
    loadAlertDispositionState();
    _alertDispositionState.forEach((item, key) => {
        if (item.skillResult) _alertSkillWorkflowCache.set(key, item.skillResult);
        if (item.healResult) _alertHealCapabilityCache.set(key, item.healResult);
        if (item.agentRun) _alertAgentRunCache.set(key, item.agentRun);
        if (item.skillAgentRun) _alertSkillAgentRunCache.set(key, item.skillAgentRun);
    });
}

function enrichHealPayloadWithDiagnosis(payload, diagnosis, key = '') {
    if (!diagnosis) return { ...payload, diagnosis_mode: 'alert_only' };
    const services = Array.isArray(diagnosis.affected_services) ? diagnosis.affected_services : [];
    const evidence = Object.values(diagnosis.evidence_summary || {}).map(v => String(v || '')).filter(Boolean);
    return {
        ...payload,
        source: 'alert-rca',
        diagnosis_mode: 'rca_result',
        diagnosis_key: key,
        fault_type: diagnosis.fault_type || payload.fault_type || '',
        root_cause: diagnosis.root_cause || payload.root_cause || '',
        root_cause_component: diagnosis.root_cause_component || payload.root_cause_component || services[0] || payload.object || '',
        remediation_hint: diagnosis.remediation_suggestion || payload.remediation_hint || '',
        diagnosis,
        evidence: [...(payload.evidence || []), ...evidence],
        object: payload.object || services[0] || '',
        deployment: payload.deployment || (/deployment|deploy/i.test(services[0] || '') ? services[0] : ''),
        message: [
            payload.message || '',
            diagnosis.fault_type || '',
            diagnosis.root_cause || '',
            diagnosis.remediation_suggestion || '',
            evidence.join(' '),
        ].join(' '),
    };
}

function buildHealPayloadFromAlertWithDiagnosis(a) {
    const base = buildHealPayloadFromAlert(a);
    const key = alertDiagnosisKey(a);
    const diagnosis = key ? _alertDiagnosisCache.get(key) : null;
    return enrichHealPayloadWithDiagnosis(base, diagnosis, key);
}

function alertTextForRouting(a) {
    const raw = a?.raw_data || {};
    const labels = a?.labels || {};
    return [
        a?.source,
        a?.severity,
        a?.title,
        a?.description,
        a?.service,
        a?.namespace,
        raw.reason,
        raw.message,
        raw.alertname,
        labels.alertname,
        labels.reason,
        labels.pod,
        labels.deployment,
        labels.node,
        labels.job,
        labels.instance,
    ].map(x => String(x || '')).join(' ').toLowerCase();
}

function inferSkillDomainFromAlert(a) {
    const text = alertTextForRouting(a);
    if (/(vllm|llm|gpu|cuda|inference|token|ttft|tpot)/.test(text)) return 'llm';
    return 'k8s';
}

function inferSkillFaultTypeFromAlert(a) {
    const text = alertTextForRouting(a);
    if (/(vllm|llm).*(crash|restart|unavailable|503)|process_crash/.test(text)) return 'vllm_process_crash';
    if (/(gpu).*oom|cuda.*out of memory|gpu_oom/.test(text)) return 'gpu_oom';
    if (/(timeout|latency|endpoint|ttft|tpot)/.test(text) && /(vllm|llm|inference)/.test(text)) return 'endpoint_timeout';
    if (/(etcdinsufficientmembers|insufficient members|etcd.*quorum|quorum.*etcd|kube-etcd|etcdserver|etcd.*leader|no leader)/.test(text)) return 'control_plane_unhealthy';
    if (/(kube-apiserver|kubeapiserver|apiserver.*down|apiserver.*unhealthy|control.?plane)/.test(text)) return 'control_plane_unhealthy';
    if (/(imagepull|image pull|errimagepull|pull image|镜像)/.test(text)) return 'image_pull_error';
    if (/(crashloop|crash loop|back-off restarting|restart|pod.*重启)/.test(text)) return 'pod_crashloop';
    if (/(serviceunavailable|targetdown|endpoint|no ready endpoints|503|服务不可用)/.test(text)) return 'service_unavailable';
    if (/(node.*notready|node_not_ready|节点.*不可用|kubelet)/.test(text)) return 'node_not_ready';
    if (/(oomkilled|oom killed|out of memory|内存不足)/.test(text)) return 'oom_killed';
    if (/(unschedulable|failedscheduling|scheduling|调度失败)/.test(text)) return 'scheduling_failed';
    if (/(dns|resolve|解析)/.test(text)) return 'dns_resolution';
    if (/(networkpolicy|network policy|deny|拒绝)/.test(text)) return 'network_policy_deny';
    if (/(configmap|secret|not found|配置缺失)/.test(text)) return 'config_missing';
    if (/(volume|mount|pvc|存储|挂载)/.test(text)) return 'volume_mount_failed';
    return inferSkillDomainFromAlert(a) === 'llm' ? 'endpoint_timeout' : 'service_unavailable';
}

function buildSkillWorkflowBodyFromAlert(a, stages) {
    const payload = buildHealPayloadFromAlertWithDiagnosis(a);
    const domain = inferSkillDomainFromAlert(a);
    const inferredFaultType = inferSkillFaultTypeFromAlert(a);
    const faultType = inferredFaultType || payload.fault_type;
    const raw = a?.raw_data || {};
    const labels = a?.labels || {};
    const rawFaultType = labels.alertname || raw.alertname || a?.title || payload.raw_fault_type || payload.fault_type || '';
    return {
        domain,
        stages: stages || ['detect', 'diagnose', 'heal', 'verify'],
        dry_run: true,
        allow_legacy_fallback: true,
        payload: {
            ...payload,
            source: 'alert_center',
            alert_id: alertDiagnosisKey(a),
            cluster_id: a?.cluster_id || document.getElementById('alert-cluster-filter')?.value || '',
            cluster_kind: a?.cluster_kind || 'general',
            domain,
            fault_type: faultType,
            raw_fault_type: rawFaultType,
            alert_name: rawFaultType,
            namespace: payload.namespace || a?.namespace || 'default',
            deployment: payload.deployment || payload.root_cause_component || '',
            service: payload.service || payload.object || a?.service || '',
            pod: payload.pod || '',
            node: payload.node || '',
            object: payload.object || a?.service || labels.service || labels.job || '',
            labels,
            message: payload.message || `[${(a?.severity || 'warning').toUpperCase()}] ${a?.title || ''} ${a?.description || ''}`,
        },
    };
}

function renderAlertDispositionPanelHtml(index, options = {}) {
    const stateItem = options.key ? alertDispositionState(options.key) : {};
    const a = options.alert || _filteredAlerts[index] || stateItem.alert || null;
    const key = alertDiagnosisKey(a);
    const skillResult = options.skillResult || _alertSkillWorkflowCache.get(key);
    const agentRun = options.agentRun || _alertAgentRunCache.get(key) || null;
    const agentStatus = agentRun?.run_id ? _alertAgentStatusCache.get(agentRun.run_id) : null;
    const skillAgentRun = options.skillAgentRun || _alertSkillAgentRunCache.get(key) || null;
    const skillAgentStatus = skillAgentRun?.run_id ? _alertSkillAgentStatusCache.get(skillAgentRun.run_id) : null;
    const healResult = options.healResult || _alertHealCapabilityCache.get(key) || null;
    const mode = options.mode || 'ready';
    const skillBody = a ? buildSkillWorkflowBodyFromAlert(a, ['detect', 'diagnose']) : {};
    const activeStage = options.stage || stateItem.stage || 'details';
    const active = options.active ?? isAlertCurrentlyActive(key);
    const activeDetails = activeStage === 'details' || activeStage === 'ready';
    const activeAnalysis = activeStage === 'analysis';
    const activeHealing = activeStage === 'healing';
    return `<div class="alert-disposition-workbench">
        <div class="alert-disposition-toolbar">
            <div class="alert-disposition-tabs">
                <button class="btn btn-sm ${activeDetails ? 'btn-primary' : ''}" onclick="switchAlertDispositionStage(${index}, 'details')">详情</button>
                <button class="btn btn-sm ${activeAnalysis ? 'btn-primary' : ''}" onclick="switchAlertDispositionStage(${index}, 'analysis')">分析分支</button>
                <button class="btn btn-sm ${activeHealing ? 'btn-primary' : ''}" onclick="switchAlertDispositionStage(${index}, 'healing')">自愈分支</button>
            </div>
            <div class="alert-disposition-actions">
                ${activeDetails ? `
                    <button class="btn btn-sm btn-primary" ${active ? '' : 'disabled'} onclick="switchAlertDispositionStage(${index}, 'analysis')">去分析分支</button>
                    <button class="btn btn-sm" ${active ? '' : 'disabled'} onclick="switchAlertDispositionStage(${index}, 'healing')">去自愈分支</button>
                ` : activeAnalysis ? `
                    <span class="text-muted">在下方选择多智能体或 Agent+Skill 分析链路</span>
                ` : `
                    <span class="text-muted">在下方选择多智能体或 Agent+Skill 自愈链路</span>
                `}
            </div>
        </div>
        <div class="alert-disposition-status">
            <span class="badge badge-${active ? 'success' : 'gray'}">${active ? '告警活跃' : '告警已消除'}</span>
            <span class="badge badge-info">${escapeHtml(skillBody.domain || '-')}</span>
            <span class="badge badge-info">${escapeHtml(skillBody.payload?.fault_type || '-')}</span>
            <span class="badge badge-warning">Dry-run</span>
            <span class="badge badge-${skillResult ? 'success' : 'gray'}">${skillResult ? 'Skill 工具已运行' : 'Skill 工具未运行'}</span>
            ${agentRun ? `<span class="badge badge-success">Agent ${escapeHtml(statusLabel(agentStatus?.status || agentRun.status || 'running'))}</span>` : ''}
            ${skillAgentRun ? `<span class="badge badge-info">Agent+Skill ${escapeHtml(statusLabel(skillAgentStatus?.status || skillAgentRun.status || 'running'))}</span>` : ''}
            ${healResult ? '<span class="badge badge-success">自愈方案已生成</span>' : ''}
        </div>
        ${active ? '' : '<div class="alert-resolved-note">最新检测未返回该告警，系统不会再触发新的分析或自愈动作。已完成的分析过程仍可查看。</div>'}
        ${renderAlertDetailSummary(a, skillBody)}
        ${activeDetails ? renderAlertDetailsSection(a, { agentRun, agentStatus, skillResult, healResult, skillAgentRun, skillAgentStatus }) : ''}
        ${activeAnalysis ? renderAlertAnalysisSection(index, { agentRun, agentStatus, skillResult, mode, active, skillAgentRun, skillAgentStatus }) : ''}
        ${activeHealing ? renderAlertHealingSection(index, { agentRun, agentStatus, skillResult, healResult, mode, active, skillAgentRun, skillAgentStatus }) : ''}
    </div>`;
}

function renderAlertDetailSummary(a, skillBody = {}) {
    return `<div class="alert-context-strip compact">
        <div><span>告警</span><strong>${escapeHtml(a?.title || a?.description || '-')}</strong></div>
        <div><span>对象</span><strong>${escapeHtml(a?.service || skillBody.payload?.deployment || skillBody.payload?.pod || '-')}</strong></div>
        <div><span>命名空间</span><strong>${escapeHtml(a?.namespace || skillBody.payload?.namespace || '-')}</strong></div>
        <div><span>来源</span><strong>${escapeHtml(sourceLabel(a?.source || '-'))}</strong></div>
    </div>`;
}

function renderAlertDetailsSection(a, { agentRun, agentStatus, skillResult, healResult, skillAgentRun, skillAgentStatus } = {}) {
    const raw = a?.raw_data || {};
    return `<div class="alert-details-grid">
        <div class="alert-detail-card">
            <div class="skill-card-head"><strong>告警详情</strong><span class="badge badge-${a?.severity === 'critical' ? 'danger' : a?.severity === 'warning' ? 'warning' : 'info'}">${escapeHtml(a?.severity || '-')}</span></div>
            <div class="skill-kv"><span>标题</span><strong>${escapeHtml(a?.title || '-')}</strong></div>
            <div class="skill-kv"><span>描述</span><strong>${escapeHtml(a?.description || '-')}</strong></div>
            <div class="skill-kv"><span>时间</span><strong>${escapeHtml(formatTime(a?.timestamp ? a.timestamp * 1000 : null))}</strong></div>
        </div>
        <div class="alert-detail-card">
            <div class="skill-card-head"><strong>处理状态</strong><span class="badge badge-info">当前会话</span></div>
            <div class="skill-kv"><span>智能体分析</span><strong>${escapeHtml(agentRun ? statusLabel(agentStatus?.status || agentRun.status || 'running') : '未运行')}</strong></div>
            <div class="skill-kv"><span>Agent+Skill</span><strong>${escapeHtml(skillAgentRun ? statusLabel(skillAgentStatus?.status || skillAgentRun.status || 'running') : (skillResult ? '已运行' : '未运行'))}</strong></div>
            <div class="skill-kv"><span>自愈方案</span><strong>${escapeHtml(healResult ? '已生成' : '未生成')}</strong></div>
        </div>
        ${Object.keys(raw).length ? `<details class="alert-detail-raw">
            <summary>原始字段</summary>
            <pre>${escapeHtml(JSON.stringify(raw, null, 2))}</pre>
        </details>` : ''}
    </div>`;
}

function renderAlertAnalysisSection(index, { agentRun, agentStatus, skillResult, mode, active = true, skillAgentRun, skillAgentStatus } = {}) {
    return `<div class="alert-section-block">
        <div class="alert-section-title"><strong>分析过程</strong><span>${mode === 'running' ? '运行中' : '根因和证据'}</span></div>
        <div class="alert-compare-grid">
            ${renderAlertAgentAnalysisCard(agentRun, agentStatus, index, active)}
            ${renderAlertSkillAnalysisCard(skillResult, index, active, { agentRun: skillAgentRun, agentStatus: skillAgentStatus })}
        </div>
    </div>`;
}

function renderAlertHealingSection(index, { agentRun, agentStatus, skillResult, healResult, mode, active = true, skillAgentRun, skillAgentStatus } = {}) {
    return `<div class="alert-section-block">
        <div class="alert-section-title"><strong>自愈方案</strong><span>${mode === 'running' ? '生成中' : '门禁、动作和验证'}</span></div>
        <div class="alert-compare-grid">
            ${renderAlertAgentHealingCard(agentRun, agentStatus, healResult, index, active)}
            ${renderAlertSkillHealingCard(skillResult, index, active, { agentRun: skillAgentRun, agentStatus: skillAgentStatus })}
        </div>
        ${healResult ? `<div class="alert-heal-nested">${renderAlertHealPanelHtml(healResult, index)}</div>` : ''}
    </div>`;
}

function renderAlertSkillLoadingCard(title, message) {
    return `<div class="skill-result-card alert-lane-card alert-skill-loading-card">
        <div class="skill-card-head"><strong>${escapeHtml(title)}</strong><span class="badge badge-info">运行中</span></div>
        <div class="skill-kv"><span>调用方式</span><strong>Agent 工具调用</strong></div>
        <div class="alert-skill-progress">
            ${['检测', '诊断', '门禁', '自愈', '验证'].map((name, idx) => `<span class="${idx === 0 ? 'active' : ''}">${escapeHtml(name)}</span>`).join('')}
        </div>
        <p>${escapeZh(message)}</p>
    </div>`;
}

function renderAlertSkillWorkflowHtml(res) {
    const diagnosis = res?.diagnosis || {};
    const plan = res?.execution_plan || {};
    const runtime = res?.skill_runtime || {};
    const migration = res?.migration || {};
    const commands = plan.commands || [];
    const checks = res?.verification_plan?.checks || [];
    return `<div class="alert-disposition-result">
        <div class="skill-status-strip">
            <div><span>主 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
            <div><span>匹配数</span><strong>${escapeHtml(runtime.matched_count ?? '-')}</strong></div>
            <div><span>迁移模式</span><strong>${escapeHtml(skillMigrationModeLabel(migration.mode || '-'))}</strong></div>
            <div><span>置信度</span><strong>${escapeHtml(formatConfidence(diagnosis.confidence))}</strong></div>
        </div>
        <div class="alert-compare-grid">
            <div class="skill-result-card">
                <div class="skill-card-head"><strong>诊断结果</strong><span class="badge badge-info">${escapeHtml(diagnosis.fault_type || '-')}</span></div>
                <p>${escapeZh(diagnosis.root_cause || diagnosis.summary || '暂无诊断结论')}</p>
                <p>组件：${escapeHtml(diagnosis.root_cause_component || '-')}</p>
            </div>
            <div class="skill-result-card">
                <div class="skill-card-head"><strong>自愈计划</strong><span class="badge badge-warning">Dry-run</span></div>
                <p>动作数：${escapeHtml(plan.command_count ?? commands.length ?? 0)}；验证项：${escapeHtml(checks.length)}</p>
                ${commands.slice(0, 3).map(cmd => `<code>${escapeHtml(cmd.command || cmd)}</code>`).join('')}
            </div>
        </div>
        ${renderAlertSkillExecutionProcess(res, ['detect', 'diagnose', 'heal', 'verify'], { open: true })}
        <details class="skill-raw-fold"><summary>原始 Skill 输出</summary><pre>${escapeHtml(JSON.stringify(res, null, 2))}</pre></details>
    </div>`;
}

function renderAlertAgentAnalysisCard(run, status, index, active = true) {
    if (!run) {
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>智能体分析</strong><span class="badge badge-gray">未运行</span></div>
            <p>多智能体 RCA 会展示阶段、假设、证据和最终结论。</p>
            <button class="btn btn-sm btn-primary" ${active ? '' : 'disabled'} onclick="runAgentAnalysisFromAlert(${index})">启动智能体分析</button>
        </div>`;
    }
    const events = status?.events || [];
    const logs = status?.logs || [];
    const result = status?.result || {};
    const phases = events.filter(e => e.event === 'phase_start' || e.event === 'phase_complete');
    const hypotheses = [...events].reverse().find(e => e.event === 'hypotheses')?.items || [];
    const evidence = events.filter(e => e.event === 'evidence');
    const inner = result?.result || result || {};
    const rca = inner.result && typeof inner.result === 'object' ? inner.result : inner;
    const runStatus = status?.status || run.status || 'unknown';
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>智能体分析</strong><span class="badge badge-${runStatus === 'completed' ? 'success' : runStatus === 'failed' ? 'danger' : 'info'}">${escapeHtml(statusLabel(runStatus))}</span></div>
        <div class="skill-kv"><span>Run</span><strong>${escapeHtml(run.run_id || '-')}</strong></div>
        ${phases.length ? `<div class="alert-mini-timeline">${phases.map(e => `<span class="${phaseEventClass(e)}">${escapeHtml(phaseDisplayName(e))}</span>`).join('')}</div>` : '<p class="text-muted">等待阶段事件...</p>'}
        ${hypotheses.length ? `<details class="skill-raw-fold" open><summary>候选假设 ${escapeHtml(hypotheses.length)}</summary><ul>${hypotheses.map(h => `<li>${escapeZh(h.description || h.root_cause || '')} · ${escapeHtml(formatConfidence(h.confidence))}</li>`).join('')}</ul></details>` : ''}
        ${evidence.length ? `<details class="skill-raw-fold" open><summary>证据采集 ${escapeHtml(evidence.length)}</summary><ul>${evidence.map(e => `<li>${escapeHtml(rcaAgentLabel(e.agent || '-'))}：${escapeZh(e.summary || '')}</li>`).join('')}</ul></details>` : ''}
        ${rca.root_cause || rca.error ? `<div class="rca-remediation"><strong>结论：</strong>${escapeZh(rca.root_cause || rca.error || '')}</div>` : ''}
        ${logs.length ? `<details class="skill-raw-fold"><summary>日志摘要 ${escapeHtml(logs.length)}</summary><pre>${escapeHtml(logs.slice(-20).join('\\n'))}</pre></details>` : ''}
        <div class="table-actions">
            <button class="btn btn-sm" onclick="refreshAlertAgentRun(${index})">刷新过程</button>
            <button class="btn btn-sm" onclick="openRCAHistoryRun('${escapeHtmlAttr(run.run_id || '')}', 'multi_agent')">打开 RCA 页面</button>
        </div>
    </div>`;
}

function latestAgentSkillEvent(status = {}) {
    const event = [...(status.events || [])].reverse().find(e => e.event === 'skill_runtime');
    if (event) return event;
    const partial = status.partial_result || {};
    const result = partial.skill_runtime_result || null;
    if (!result || typeof result !== 'object') return null;
    const runtime = result.skill_runtime || {};
    const diagnosis = result.diagnosis || {};
    const plan = result.execution_plan || {};
    const summary = result.agent_tool_invocation?.summary || {};
    return {
        event: 'skill_runtime',
        status: result.status || partial.status || 'completed',
        source: partial.source || 'agent_skill_bridge',
        tool: partial.agent_skill_bridge?.tool || result.agent_tool_invocation?.tool || 'agenticsre_skill_run',
        primary_skill_id: runtime.primary_skill_id || diagnosis.skill_id || '',
        matched_count: runtime.matched_count || 0,
        confidence: diagnosis.confidence,
        command_count: plan.command_count ?? (plan.commands || []).length,
        summary: typeof summary === 'object' ? (summary.summary || '') : '',
        result,
    };
}

function agentSkillResultFromEvent(event = {}, requestedStages = []) {
    if (!event || typeof event !== 'object') return null;
    const result = event.result || null;
    if (!result || typeof result !== 'object') return null;
    return {
        ...result,
        requested_stages: requestedStages.length ? requestedStages : (result.requested_stages || ['detect', 'diagnose', 'heal', 'verify']),
        invocation_mode: 'agent_tool',
    };
}

function startAlertSkillAgentPolling(key, runId, options = {}) {
    return startAlertRunPolling(key, runId, 'agent_skill', options);
}

function startAlertRunPolling(key, runId, mode, options = {}) {
    if (!key || !runId) return;
    const pollers = mode === 'agent_skill' ? _alertSkillAgentPollers : _alertAgentPollers;
    if (pollers.has(runId)) return;
    const runs = mode === 'agent_skill' ? _alertSkillAgentRunCache : _alertAgentRunCache;
    const poller = { timer: null, key, failures: 0 };
    pollers.set(runId, poller);
    const tick = async () => {
        if (runs.get(key)?.run_id !== runId) {
            pollers.delete(runId);
            return;
        }
        const refresh = mode === 'agent_skill' ? refreshAlertSkillAgentRun : refreshAlertAgentRun;
        const status = await refresh(key, { ...options, runId, poll: false, silent: true });
        if (['completed', 'failed', 'cancelled', 'expired'].includes(status?.status)
                || runs.get(key)?.run_id !== runId) {
            pollers.delete(runId);
            return;
        }
        poller.failures = status ? 0 : poller.failures + 1;
        poller.timer = setTimeout(tick, Math.min(30000, 2500 * 2 ** Math.min(poller.failures, 4)));
    };
    tick();
}

function renderAgentWorkflowDetail(status = {}) {
    const events = status?.events || [];
    const phases = events.filter(e => e.event === 'phase_start' || e.event === 'phase_complete');
    const phaseByKey = new Map();
    phases.forEach(e => {
        const key = e.phase || e.name || '';
        if (!key) return;
        const prev = phaseByKey.get(key) || {};
        phaseByKey.set(key, {
            ...prev,
            ...e,
            event: e.event === 'phase_complete' || prev.event === 'phase_complete' ? 'phase_complete' : 'phase_start',
        });
    });
    const uniquePhases = Array.from(phaseByKey.values());
    const hypotheses = [...events].reverse().find(e => e.event === 'hypotheses')?.items || [];
    const llmHypEvent = [...events].reverse().find(e => e.event === 'llm_hypotheses') || {};
    const contextEvent = [...events].reverse().find(e => e.event === 'context') || {};
    const evidence = events.filter(e => e.event === 'evidence');
    const skillEvents = events.filter(e => e.event === 'skill_runtime');
    const logs = status?.logs || [];
    const completed = uniquePhases.filter(e => e.event === 'phase_complete' && e.status !== 'degraded');
    const currentPhase = [...phases].reverse().find(e => e.event === 'phase_start' || e.event === 'phase_complete');
    const skillEvent = skillEvents[skillEvents.length - 1] || {};
    const skillResult = skillEvent.result || {};
    const diagnosis = skillResult.diagnosis || {};
    const runtime = skillResult.skill_runtime || {};
    const primarySkill = skillEvent.primary_skill_id || runtime.primary_skill_id || diagnosis.skill_id || '';
    const topHypotheses = hypotheses.slice(0, 3);
    const topEvidence = evidence.slice(-3);
    const llmHypotheses = Array.isArray(llmHypEvent.items) ? llmHypEvent.items : [];
    const llmCount = hypotheses.filter(h => h.llm_used || h.source === 'llm').length || llmHypotheses.length;
    return `<div class="agent-workflow-detail">
        <div class="agent-workflow-summary">
            <div class="skill-status-strip compact">
                <div><span>状态</span><strong>${escapeHtml(statusLabel(status.status || 'running'))}</strong></div>
                <div><span>当前阶段</span><strong>${escapeHtml(currentPhase ? phaseDisplayName(currentPhase) : '等待事件')}</strong></div>
                <div><span>已完成</span><strong>${escapeHtml(completed.length)}</strong></div>
                <div><span>候选根因</span><strong>${escapeHtml(hypotheses.length)}</strong></div>
                <div><span>LLM生成</span><strong>${escapeHtml(llmHypEvent.success ? `${llmCount} 个` : '未使用')}</strong></div>
                <div><span>证据</span><strong>${escapeHtml(evidence.length)}</strong></div>
                <div><span>Skill 调用</span><strong>${escapeHtml(skillEvents.length ? '已调用' : '等待')}</strong></div>
            </div>
            ${renderAgentSkillHypothesisGeneration(contextEvent, llmHypEvent, hypotheses)}
            ${primarySkill ? `<div class="skill-kv"><span>关键 Skill</span><strong>${escapeHtml(primarySkill)}</strong></div>` : ''}
            ${diagnosis.root_cause || diagnosis.summary ? `<p><strong>结论：</strong>${escapeZh(diagnosis.root_cause || diagnosis.summary)}</p>` : ''}
            ${diagnosis.confidence !== undefined ? `<div class="skill-kv"><span>置信度</span><strong>${escapeHtml(formatConfidence(diagnosis.confidence))}</strong></div>` : ''}
            ${topHypotheses.length ? `<div class="compact-evidence-list"><strong>候选根因</strong><ul>${topHypotheses.map(h => `<li>${escapeZh(h.description || h.root_cause || '')} · ${escapeHtml(formatConfidence(h.confidence))}</li>`).join('')}</ul></div>` : ''}
            ${topEvidence.length ? `<div class="compact-evidence-list"><strong>关键证据</strong><ul>${topEvidence.map(e => `<li>${escapeHtml(rcaAgentLabel(e.agent || '-'))}：${escapeZh(e.summary || '')}</li>`).join('')}</ul></div>` : ''}
            ${!phases.length && !skillEvents.length ? '<p class="text-muted">Agent 已启动，正在等待 Skill 工具调用事件和阶段事件返回。</p>' : ''}
        </div>
        <details class="skill-raw-fold">
            <summary>技术细节</summary>
            ${uniquePhases.length ? `<div class="alert-mini-timeline">${uniquePhases.map(e => `<span class="${phaseEventClass(e)}">${escapeHtml(phaseDisplayName(e))}</span>`).join('')}</div>` : '<p class="text-muted">等待 Agent 阶段事件...</p>'}
            ${skillEvents.length ? skillEvents.map(e => renderAgentSkillBridgeEvent(e)).join('') : '<p class="text-muted">尚未产生 Skill 工具调用事件。</p>'}
            ${logs.length ? `<details class="skill-raw-fold"><summary>Agent 日志 ${escapeHtml(logs.length)}</summary><pre>${escapeHtml(logs.slice(-30).join('\\n'))}</pre></details>` : ''}
        </details>
    </div>`;
}

function renderAgentSkillHypothesisGeneration(contextEvent = {}, llmHypEvent = {}, hypotheses = []) {
    const llmHypotheses = Array.isArray(llmHypEvent.items) ? llmHypEvent.items : [];
    const ranked = Array.isArray(hypotheses) ? hypotheses : [];
    const llmUsed = !!llmHypEvent.success || ranked.some(h => h.llm_used || h.source === 'llm');
    if (!llmUsed && !ranked.length && !Object.keys(contextEvent || {}).length) return '';
    const top = ranked.length ? ranked.slice(0, 3) : llmHypotheses.slice(0, 3);
    const contextBits = [];
    if (contextEvent.rules_count !== undefined) contextBits.push(`历史规则 ${contextEvent.rules_count}`);
    if (contextEvent.faults_count !== undefined) contextBits.push(`相似故障 ${contextEvent.faults_count}`);
    if (contextEvent.recent_runs_count !== undefined) contextBits.push(`近期RCA ${contextEvent.recent_runs_count}`);
    const llmBadge = llmUsed
        ? `<span class="badge badge-success">LLM 已生成</span>`
        : `<span class="badge badge-gray">确定性假设</span>`;
    return `<div class="agent-llm-hypothesis-box">
        <div class="agent-llm-head">
            <strong>假设生成</strong>
            ${llmBadge}
            ${contextBits.length ? `<small>${escapeHtml(contextBits.join(' / '))}</small>` : ''}
        </div>
        <p>Agent 先检索当前告警、历史规则、相似故障和观测证据，再由 LLM 生成候选根因并与规则假设合并排序。</p>
        ${top.length ? `<div class="agent-llm-hyp-list">${top.map((h, i) => `
            <div>
                <span>#${i + 1}</span>
                <strong>${escapeHtml(formatConfidence(h.confidence))}</strong>
                <p>${escapeZh(h.description || h.root_cause || h.summary || '')}</p>
                <small>${escapeHtml(h.source === 'llm' || h.llm_used ? 'LLM生成' : '规则生成')}</small>
            </div>
        `).join('')}</div>` : '<p class="text-muted">暂无候选根因。</p>'}
    </div>`;
}

function renderAgentSkillBridgeEvent(event = {}) {
    const result = event.result || {};
    const runtime = result.skill_runtime || {};
    const diagnosis = result.diagnosis || {};
    const plan = result.execution_plan || {};
    return `<details class="skill-raw-fold agent-skill-bridge">
        <summary>Agent 调用 Skill</summary>
        <div class="skill-status-strip">
            <div><span>主 Skill</span><strong>${escapeHtml(event.primary_skill_id || runtime.primary_skill_id || '-')}</strong></div>
            <div><span>调用方式</span><strong>${escapeHtml(event.source || result.agent_tool_invocation?.mode || '-')}</strong></div>
            <div><span>工具</span><strong>${escapeHtml(event.tool || result.agent_tool_invocation?.tool || '-')}</strong></div>
            <div><span>命中数</span><strong>${escapeHtml(event.matched_count ?? runtime.matched_count ?? 0)}</strong></div>
            <div><span>置信度</span><strong>${escapeHtml(formatConfidence(event.confidence ?? diagnosis.confidence))}</strong></div>
            <div><span>自愈动作</span><strong>${escapeHtml(event.command_count ?? plan.command_count ?? 0)}</strong></div>
        </div>
        <p>${escapeZh(event.summary || diagnosis.root_cause || 'Skill 已作为 Agent 工具调用。')}</p>
        ${renderAlertSkillExecutionProcess(result, ['detect', 'diagnose', 'heal', 'verify'], { open: false, showMissing: true })}
    </details>`;
}

function renderAlertSkillWaitingCard(index, active = true, context = {}, mode = 'analysis') {
    const run = context.agentRun || {};
    const status = context.agentStatus || {};
    const isAgentSkill = run.invocation_mode === 'agent_skill';
    const title = mode === 'healing' ? 'Agent+Skill 自愈' : 'Agent+Skill 分析';
    const waitingText = isAgentSkill
        ? 'Agent 已启动，正在按假设、规划、调查、推理推进分析。'
        : '分析由 Agent 发起，命中后会在这里展示 Skill 调用和推理进度。';
    if (!isAgentSkill) return '';
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>${escapeHtml(title)}</strong><span class="badge badge-info">${escapeHtml(statusLabel(status.status || run.status || 'running'))}</span></div>
        <p>${escapeZh(waitingText)}</p>
        ${renderAgentSkillCompactProgress(status)}
        <div class="table-actions">
            <button class="btn btn-sm" onclick="refreshAlertSkillAgentRun(${index}, {stage: '${mode === 'healing' ? 'healing' : 'analysis'}'})">刷新过程</button>
            <button class="btn btn-sm" onclick="openRCAHistoryRun('${escapeHtmlAttr(run.run_id || '')}', 'agent_skill')">打开 RCA 工作流</button>
        </div>
    </div>`;
}

function renderAlertSkillAnalysisCard(res, index, active = true, context = {}) {
    if (!res) {
        const waiting = renderAlertSkillWaitingCard(index, active, context, 'analysis');
        if (waiting) return waiting;
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>Agent+Skill 分析</strong><span class="badge badge-gray">等待 Agent</span></div>
            <p>Agent 会先形成候选根因，再规划验证、调用 Skill 调查证据并输出推理结论。</p>
            <button class="btn btn-sm btn-primary" ${active ? '' : 'disabled'} onclick="runSkillAnalysisFromAlert(${index})">启动 Agent+Skill 分析</button>
        </div>`;
    }
    const diagnosis = res.diagnosis || {};
    const runtime = res.skill_runtime || {};
    const primaryRun = primarySkillRun(res);
    const evidence = Array.isArray(diagnosis.evidence) ? diagnosis.evidence : [];
    const actionCount = res.execution_plan?.command_count ?? (res.execution_plan?.suggestions || []).length ?? 0;
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>Agent+Skill 分析</strong><span class="badge badge-${diagnosis.confidence >= 0.85 ? 'success' : 'warning'}">${escapeHtml(formatConfidence(diagnosis.confidence))}</span></div>
        <div class="skill-kv"><span>根因</span><strong>${escapeHtml(faultTypeLabel(diagnosis.fault_type || '-'))}</strong></div>
        <div class="skill-kv"><span>关键 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
        <p>${escapeZh(diagnosis.root_cause || diagnosis.summary || '暂无诊断结论')}</p>
        ${renderEvidenceList(evidence, { title: '关键证据', maxItems: 3 })}
        ${actionCount ? `<div class="skill-kv"><span>建议动作</span><strong>${escapeHtml(actionCount)} 个，默认 Dry-run</strong></div>` : ''}
        <details class="skill-raw-fold">
            <summary>查看 Agent+Skill 过程</summary>
            ${renderAgentSkillInvocationFlow(res, { mode: 'analysis' })}
            ${renderAlertSkillExecutionProcess(res, res.requested_stages || [], { open: false, showMissing: false })}
        </details>
    </div>`;
}

function renderAlertAgentHealingCard(run, status, healResult, index, active = true) {
    const available = !!healResult;
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>智能体自愈</strong><span class="badge badge-${available ? 'success' : 'gray'}">${available ? '方案已生成' : '未生成'}</span></div>
        <p>复用智能体诊断结果和自愈知识库，输出可编辑方案、审批和 dry-run。</p>
        ${run ? `<div class="skill-kv"><span>关联 RCA</span><strong>${escapeHtml(run.run_id || '-')} · ${escapeHtml(status?.status || 'running')}</strong></div>` : ''}
        ${healResult ? `<div class="skill-kv"><span>动作数</span><strong>${escapeHtml((healResult.suggestions || []).length)}</strong></div>` : '<p class="text-muted">尚未生成智能体自愈方案。</p>'}
        <div class="table-actions">
            <button class="btn btn-sm" ${active ? '' : 'disabled'} onclick="runAgentHealingFromAlert(${index})">生成智能体自愈</button>
            ${available ? `<button class="btn btn-sm btn-primary" onclick="openHealPlanFromAlert(${index})">查看/编辑方案</button>` : ''}
            ${available ? `<button class="btn btn-sm" onclick="dryRunHealFromAlert(${index})">Dry-run</button>` : ''}
        </div>
    </div>`;
}

function renderAlertSkillHealingCard(res, index, active = true, context = {}) {
    if (!res) {
        const waiting = renderAlertSkillWaitingCard(index, active, context, 'healing');
        if (waiting) return waiting;
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>Skill 自愈工具</strong><span class="badge badge-gray">等待 Agent</span></div>
            <p>自愈闭环由 Agent 发起，Skill 负责输出门禁、恢复动作和验证步骤。</p>
            <button class="btn btn-sm btn-primary" ${active ? '' : 'disabled'} onclick="runSkillHealingFromAlert(${index})">启动 Agent+Skill 自愈</button>
        </div>`;
    }
    const capability = res.capability || {};
    const plan = res.execution_plan || {};
    const suggestions = plan.suggestions || [];
    const checks = res.verification_plan?.checks || [];
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>Agent+Skill 自愈</strong><span class="badge badge-${capability.heal_ready ? 'success' : 'warning'}">${capability.heal_ready ? '门禁通过' : '门禁受限'}</span></div>
        <div class="skill-kv"><span>调用方式</span><strong>Agent 工具调用</strong></div>
        <div class="skill-kv"><span>动作数</span><strong>${escapeHtml(plan.command_count ?? suggestions.length ?? 0)}</strong></div>
        <div class="skill-kv"><span>验证项</span><strong>${escapeHtml(checks.length)}</strong></div>
        ${capability.heal_blocker_messages?.length ? `<div class="skill-note warning">${escapeZh(capability.heal_blocker_messages.join(' / '))}</div>` : ''}
        ${suggestions.slice(0, 3).map(s => `<code>${escapeHtml(s.command || '')}</code>`).join('')}
        ${renderAgentSkillInvocationFlow(res, { mode: 'healing' })}
        ${renderAlertSkillExecutionProcess(res, res.requested_stages || [], { open: true, showMissing: false })}
    </div>`;
}

function renderAlertSkillTraceCompact(runs = [], stagesFilter = []) {
    const lines = [];
    runs.forEach(run => {
        (run.trace || []).forEach(t => {
            if (!stagesFilter.length || stagesFilter.includes(t.stage)) {
                lines.push({ skill: run.skill?.id || '-', ...t });
            }
        });
    });
    if (!lines.length) return '<p class="text-muted">暂无阶段 trace。</p>';
    return `<div class="alert-stage-lines">${lines.slice(0, 10).map(t => `<div>
        <span class="badge badge-${t.status === 'ok' || t.status === 'matched' ? 'success' : t.status === 'error' ? 'danger' : 'gray'}">${escapeHtml(t.stage || '-')}</span>
        <strong>${escapeHtml(t.skill)}</strong>
        <em>${escapeHtml(formatConfidence(t.confidence))} · ${escapeHtml(t.duration_ms ?? 0)}ms</em>
    </div>`).join('')}</div>`;
}

function alertSkillStageLabel(stage) {
    return ({ detect: '检测', diagnose: '诊断', heal: '自愈', verify: '验证', plan: '计划' })[stage] || stage || '-';
}

function skillStageBadgeClass(status) {
    if (['ok', 'matched', 'completed', 'ready'].includes(status)) return 'success';
    if (['error', 'failed', 'blocked'].includes(status)) return 'danger';
    if (['skipped', 'missing', 'no_match'].includes(status)) return 'gray';
    return 'warning';
}

function normalizeSkillStageOutput(stage = {}) {
    const output = stage.output || {};
    if (!output || typeof output !== 'object') return {};
    return output;
}

function renderSkillOutputHighlights(output = {}, stageName = '') {
    const rows = [];
    const add = (label, value) => {
        if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)) return;
        rows.push(`<div class="skill-kv"><span>${escapeHtml(label)}</span><strong>${escapeZh(Array.isArray(value) ? value.join(' / ') : value)}</strong></div>`);
    };
    add('故障类型', output.fault_type);
    add('根因组件', output.root_cause_component);
    add('根因', output.root_cause);
    add('命中规则', output.matched_rule || output.rule);
    add('外部方法', output.external_method?.source || output.method);
    add('评测门禁', output.evaluation_gate?.ready === undefined ? '' : (output.evaluation_gate.ready ? '通过' : '证据不足'));
    const suggestions = output.suggestions || output.actions || output.commands || [];
    const checks = output.checks || output.verification_checks || [];
    if (stageName === 'heal' && suggestions.length) {
        rows.push(`<div class="alert-skill-action-list">${suggestions.slice(0, 4).map((item, idx) => {
            const action = typeof item === 'string' ? { command: item } : (item || {});
            return `<div>
                <span>${idx + 1}</span>
                <div>
                    <strong>${escapeHtml(action.step || action.action || action.name || `动作 ${idx + 1}`)}</strong>
                    ${action.command ? `<code>${escapeHtml(action.command)}</code>` : ''}
                    ${action.risk ? `<small>风险: ${escapeHtml(riskLabel(action.risk))}</small>` : ''}
                </div>
            </div>`;
        }).join('')}</div>`);
    }
    if (stageName === 'verify' && checks.length) {
        rows.push(`<div class="alert-skill-check-list">${checks.slice(0, 5).map(item => `<code>${escapeHtml(item.command || item)}</code>`).join('')}</div>`);
    }
    return rows.join('') || '<p class="text-muted">该阶段没有结构化输出。</p>';
}

function renderAlertSkillStageDetail(stageName, stage = {}, trace = {}) {
    const output = normalizeSkillStageOutput(stage);
    const evidence = stage.evidence || output.evidence || [];
    const status = stage.status || trace.status || 'skipped';
    return `<div class="alert-skill-stage-detail">
        <div class="alert-skill-stage-head">
            <span class="badge badge-${skillStageBadgeClass(status)}">${escapeHtml(alertSkillStageLabel(stageName))}</span>
            <strong>${escapeHtml(statusLabel(status))}</strong>
            <em>${escapeHtml(formatConfidence(stage.confidence ?? trace.confidence ?? 0))} · ${escapeHtml(trace.duration_ms ?? 0)}ms</em>
        </div>
        ${stage.summary ? `<p>${escapeZh(stage.summary)}</p>` : ''}
        ${renderSkillOutputHighlights(output, stageName)}
        ${evidence.length ? `<details class="skill-raw-fold" open><summary>证据 ${evidence.length} 条</summary>${renderEvidenceList(evidence, { title: '摘要', maxItems: 8 })}</details>` : ''}
        ${stage.error ? `<div class="skill-note danger">${escapeHtml(stage.error)}</div>` : ''}
        ${Object.keys(output).length ? `<details class="skill-raw-fold"><summary>阶段输出 JSON</summary><pre>${escapeHtml(JSON.stringify(output, null, 2))}</pre></details>` : ''}
    </div>`;
}

function renderAlertSkillRunDetail(run = {}, stagesFilter = []) {
    const stages = run.stages || {};
    const traces = {};
    (run.trace || []).forEach(item => { traces[item.stage] = item; });
    const showMissing = run.__showMissingStages === true;
    const declaredStages = skillRunDeclaredStages(run);
    const implementedStages = skillRunImplementedStages(run);
    const executedStages = skillRunExecutedStages(run);
    const candidateStages = [
        ...executedStages,
        ...(showMissing ? (implementedStages.length ? implementedStages : declaredStages) : []),
    ].filter((item, idx, arr) => item && arr.indexOf(item) === idx);
    const stageNames = candidateStages
        .filter(name => {
            if (stagesFilter.length && !stagesFilter.includes(name)) return false;
            return showMissing || stages[name] || traces[name];
        });
    const skill = run.skill || {};
    const policy = run.policy || {};
    const healBlockers = policy.heal_blocker_messages || policy.heal_blockers || [];
    const supportsHeal = policy.supports_heal !== false;
    const visibleHealBlockers = supportsHeal ? healBlockers : [];
    if (!stageNames.length) return '';
    return `<div class="alert-skill-run-detail">
        <div class="alert-skill-run-head">
            <div>
                <strong>${escapeHtml(skill.name || skill.id || '-')}</strong>
                <span>${escapeHtml(skill.id || '-')} · ${escapeHtml(skill.domain || '-')} · ${escapeHtml(riskLabel(skill.risk || 'low'))}</span>
                <small>实现能力: ${escapeHtml((implementedStages.length ? implementedStages : declaredStages).map(alertSkillStageLabel).join(' / ') || '-')}；本次有效执行: ${escapeHtml(executedStages.map(alertSkillStageLabel).join(' / ') || '-')}</small>
            </div>
            <div>
                ${supportsHeal
                    ? `<span class="badge badge-${policy.heal_ready ? 'success' : 'warning'}">${policy.heal_ready ? '自愈门禁通过' : '自愈门禁受限'}</span>`
                    : '<span class="badge badge-gray">不提供自愈</span>'}
                ${supportsHeal ? (policy.requires_approval ? '<span class="badge badge-warning">需审批</span>' : '<span class="badge badge-success">低风险</span>') : ''}
            </div>
        </div>
        <div class="alert-skill-stage-grid">
            ${stageNames.map(name => renderAlertSkillStageDetail(name, stages[name] || {}, traces[name] || {})).join('')}
        </div>
        ${visibleHealBlockers.length ? `<div class="skill-note warning">${escapeZh(visibleHealBlockers.join(' / '))}</div>` : ''}
        <details class="skill-raw-fold"><summary>Skill policy / trace</summary><pre>${escapeHtml(JSON.stringify({ policy, trace: run.trace || [] }, null, 2))}</pre></details>
    </div>`;
}

function skillStageStatusFromRun(run = {}, stageName = '') {
    const stage = run.stages?.[stageName] || {};
    const trace = (run.trace || []).find(item => item.stage === stageName) || {};
    return stage.status || trace.status || (stage.summary || stage.output ? 'ok' : 'missing');
}

function skillStageEvidenceCount(run = {}, stageName = '') {
    const stage = run.stages?.[stageName] || {};
    const output = normalizeSkillStageOutput(stage);
    const evidence = stage.evidence || output.evidence || [];
    return Array.isArray(evidence) ? evidence.length : 0;
}

function primarySkillRun(res = {}) {
    const runtime = res.skill_runtime || {};
    const runs = runtime.runs || [];
    const primary = runtime.primary_skill_id || res.diagnosis?.skill_id || '';
    return (primary ? runs.find(run => run.skill?.id === primary) : null) || runs[0] || {};
}

function skillRunDeclaredStages(run = {}) {
    const skillStages = run.skill?.stages;
    const declared = run.declared_stages || skillStages || [];
    return Array.isArray(declared) ? declared.filter(Boolean) : [];
}

function skillRunImplementedStages(run = {}) {
    const implemented = run.implemented_stages || [];
    return Array.isArray(implemented) ? implemented.filter(Boolean) : [];
}

function skillRunExecutedStages(run = {}) {
    const executed = run.executed_stages || Object.keys(run.stages || {});
    return Array.isArray(executed) ? executed.filter(Boolean) : [];
}

function renderAgentSkillFlowStep(index, title, status = 'pending', detail = '') {
    const cls = status === 'done' ? 'done' : status === 'running' ? 'active' : status === 'failed' ? 'failed' : 'pending';
    return `<div class="agent-skill-flow-step ${cls}">
        <span>${escapeHtml(index)}</span>
        <div><strong>${escapeHtml(title)}</strong>${detail ? `<small>${escapeZh(detail)}</small>` : ''}</div>
    </div>`;
}

function renderAgentSkillCompactProgress(status = {}, result = null) {
    const events = status?.events || [];
    const skillEvent = [...events].reverse().find(e => e.event === 'skill_runtime') || {};
    const res = result || skillEvent.result || {};
    const runtime = res.skill_runtime || {};
    const diagnosis = res.diagnosis || {};
    const phase = [...events].reverse().find(e => e.event === 'phase_start' || e.event === 'phase_complete');
    const skillCalled = !!(skillEvent.event || runtime.primary_skill_id || diagnosis.skill_id);
    const completed = status.status === 'completed' || !!(diagnosis.root_cause || diagnosis.summary);
    const phaseKeys = new Set(events.map(e => e.phase).filter(Boolean));
    const steps = [
        { label: '假设', done: phaseKeys.has('agent_skill_hypothesis') || skillCalled || completed, active: !phaseKeys.has('agent_skill_hypothesis') && !skillCalled },
        { label: '规划', done: phaseKeys.has('agent_skill_plan') || skillCalled || completed, active: phaseKeys.has('agent_skill_hypothesis') && !phaseKeys.has('agent_skill_plan') },
        { label: '调查', done: phaseKeys.has('agent_skill_investigation') || skillCalled || completed, active: phaseKeys.has('agent_skill_plan') && !skillCalled },
        { label: '推理', done: completed, active: skillCalled && !completed },
    ];
    return `<div class="agent-skill-simple-progress">
        ${steps.map((step, idx) => `<span class="${step.done ? 'done' : step.active ? 'active' : ''}">${idx + 1}. ${escapeHtml(step.label)}</span>`).join('')}
        <small>${escapeHtml(phase ? phaseDisplayName(phase) : (skillCalled ? 'Skill 已调用' : '等待 Agent 规划'))}</small>
    </div>`;
}

function renderAgentSkillInvocationFlow(res = {}, options = {}) {
    const runtime = res.skill_runtime || {};
    const runs = runtime.runs || [];
    const diagnosis = res.diagnosis || {};
    const plan = res.execution_plan || {};
    const primaryRun = primarySkillRun(res);
    const primary = runtime.primary_skill_id || diagnosis.skill_id || '';
    const matchedCount = runtime.matched_count ?? runs.length ?? 0;
    const executed = skillRunExecutedStages(primaryRun);
    const declared = skillRunDeclaredStages(primaryRun);
    const implemented = skillRunImplementedStages(primaryRun);
    const hasPlan = Number(plan.command_count ?? (plan.suggestions || []).length ?? 0) > 0;
    const steps = [
        renderAgentSkillFlowStep(1, '假设生成', 'done', '根据告警和上下文提出候选根因'),
        renderAgentSkillFlowStep(2, '验证规划', 'done', `规划 Skill 调查路径，命中 ${matchedCount} 个 Skill`),
        renderAgentSkillFlowStep(3, '调查取证', executed.length || primary ? 'done' : 'pending', primary ? `主 Skill ${primary}` : '等待 Skill 命中'),
        renderAgentSkillFlowStep(4, '推理裁决', diagnosis.confidence !== undefined ? 'done' : 'pending', `${diagnosis.root_cause || diagnosis.summary || '等待诊断结论'}${diagnosis.confidence !== undefined ? ` · ${formatConfidence(diagnosis.confidence)}` : ''}`),
    ];
    if (options.mode === 'healing' || hasPlan) {
        steps.push(renderAgentSkillFlowStep(5, '生成受控自愈方案', hasPlan ? 'done' : 'pending', hasPlan ? `动作 ${plan.command_count ?? (plan.suggestions || []).length}` : '未生成恢复动作'));
    }
    return `<div class="agent-skill-call-flow">
        ${steps.join('')}
        <div class="agent-skill-call-meta">
            <span>主 Skill 实现能力：${escapeHtml((implemented.length ? implemented : declared).map(alertSkillStageLabel).join(' / ') || '-')}</span>
            <span>本次请求阶段：${escapeHtml((res.requested_stages || []).map(alertSkillStageLabel).join(' / ') || '-')}</span>
        </div>
    </div>`;
}

function renderAlertSkillVisualSummary(res = {}, requestedStages = []) {
    const runtime = res.skill_runtime || {};
    const runs = runtime.runs || [];
    const primary = runtime.primary_skill_id || res.diagnosis?.skill_id || '';
    const primaryRun = primarySkillRun(res);
    const executedStages = skillRunExecutedStages(primaryRun);
    const stages = executedStages.length ? executedStages : (requestedStages.length ? requestedStages : skillRunDeclaredStages(primaryRun));
    const diagnosis = res.diagnosis || {};
    const plan = res.execution_plan || {};
    const confidence = diagnosis.confidence ?? runtime.confidence ?? 0;
    const evidenceCount = stages.reduce((sum, name) => sum + skillStageEvidenceCount(primaryRun, name), 0);
    return `<div class="skill-visual-summary">
        <div class="skill-visual-head">
            <div>
                <span class="badge badge-info">Agent+Skill</span>
                <strong>${escapeHtml(primaryRun.skill?.name || primaryRun.skill?.id || primary || 'Skill 工作流')}</strong>
                <small>${escapeHtml(primary || runtime.primary_skill_id || '-')}</small>
            </div>
            <div class="skill-visual-metrics">
                <span>命中 ${escapeHtml(runtime.matched_count ?? runs.length ?? 0)}</span>
                <span>置信度 ${escapeHtml(formatConfidence(confidence))}</span>
                <span>证据 ${escapeHtml(evidenceCount)}</span>
                <span>动作 ${escapeHtml(plan.command_count ?? (plan.suggestions || []).length ?? 0)}</span>
            </div>
        </div>
        <div class="skill-stage-rail">
            ${stages.map((name, idx) => {
                const status = skillStageStatusFromRun(primaryRun, name);
                const cls = skillStageBadgeClass(status);
                return `<div class="skill-stage-pill ${cls}">
                    <span>${idx + 1}</span>
                    <strong>${escapeHtml(alertSkillStageLabel(name))}</strong>
                    <small>${escapeHtml(statusLabel(status))}</small>
                </div>`;
            }).join('')}
        </div>
        ${diagnosis.root_cause || diagnosis.summary ? `<div class="skill-visual-cause"><strong>Skill 结论</strong><span>${escapeZh(diagnosis.root_cause || diagnosis.summary || '')}</span></div>` : ''}
    </div>`;
}

function renderAlertSkillExecutionProcess(res = {}, stagesFilter = [], options = {}) {
    const runtime = res.skill_runtime || {};
    const runs = runtime.runs || [];
    const requestedStages = stagesFilter.length ? stagesFilter : (res.requested_stages || []);
    if (!runs.length) return `<div class="alert-skill-process-empty">
        <strong>Skill 执行过程</strong>
        <p>本次请求未匹配到可执行 Skill。请求阶段：${escapeHtml(requestedStages.map(alertSkillStageLabel).join(' / ') || '-')}</p>
    </div>`;
    const primary = runtime.primary_skill_id || res.diagnosis?.skill_id || '';
    const primaryRun = primary ? runs.find(run => run.skill?.id === primary) : null;
    const ordered = [
        ...(primaryRun ? [primaryRun] : []),
        ...runs.filter(run => !primaryRun || run.skill?.id !== primaryRun.skill?.id),
    ].map(run => ({ ...run, __showMissingStages: options.showMissing === true }));
    const openAttr = options.open ? ' open' : '';
    return `${renderAlertSkillVisualSummary(res, requestedStages)}
    <details class="alert-skill-process"${openAttr}>
        <summary>
            <span>Skill 调用过程</span>
            <strong>${escapeHtml(runtime.matched_count ?? runs.length)} 个候选 · 主路径 ${escapeHtml(primary || '-')} · 展示实际有效执行阶段</strong>
        </summary>
        ${ordered.map(run => renderAlertSkillRunDetail(run, requestedStages)).filter(Boolean).join('') || '<p class="text-muted">当前阶段没有可展示的 Skill 过程。</p>'}
    </details>`;
}

function showAlertDisposition(index, stage = 'ready') {
    const a = _filteredAlerts[index];
    if (!a) return;
    const key = alertDiagnosisKey(a);
    _activeAlertHealIndex = index;
    _activeAlertDispositionKey = key;
    _lastAlertHealResult = null;
    updateAlertDispositionState(key, { stage, alert: a, active: true });
    _activeAlertHealHtml = renderAlertDispositionPanelHtml(index, { stage, key, alert: a, active: true });
    openAlertDispositionModal(index, { stage, alert: a, key });
    renderAlertTable(_filteredAlerts);
    const run = _alertAgentRunCache.get(key);
    if (run?.run_id) refreshAlertAgentRun(key, { poll: true, silent: true });
    const skillRun = _alertSkillAgentRunCache.get(key);
    if (skillRun?.run_id) refreshAlertSkillAgentRun(key, { poll: true, silent: true });
}

function openAlertDispositionModal(index, options = {}) {
    const modal = document.getElementById('alert-disposition-modal');
    const body = document.getElementById('alert-disposition-modal-body');
    const title = document.getElementById('alert-disposition-title');
    const subtitle = document.getElementById('alert-disposition-subtitle');
    if (!modal || !body) return;
    const a = options.alert || _filteredAlerts[index] || getAlertByKey(options.key) || alertDispositionState(options.key).alert;
    const key = options.key || alertDiagnosisKey(a);
    if (title) title.textContent = '告警分析与自愈';
    _activeAlertDispositionKey = key;
    _activeAIEventDispositionId = '';
    _activeAlertHealIndex = index >= 0 ? index : activeAlertIndexByKey(key);
    const active = isAlertCurrentlyActive(key);
    body.innerHTML = renderAlertDispositionPanelHtml(_activeAlertHealIndex, {
        ...options,
        key,
        alert: a,
        active,
    });
    if (subtitle) {
        subtitle.textContent = active
            ? '告警仍在当前列表中，可继续分析、自愈或对比'
            : '告警已从当前列表消失，仅展示历史处理状态';
    }
    modal.style.display = 'flex';
}

function closeAlertDispositionModal() {
    const modal = document.getElementById('alert-disposition-modal');
    if (modal) modal.style.display = 'none';
    _activeAlertDispositionKey = '';
    _activeAlertHealIndex = null;
    _activeAIEventDispositionId = '';
    renderAlertTable(_filteredAlerts);
}

function switchAlertDispositionStage(index, stage) {
    const ctx = alertActionContext(index);
    if (!ctx.alert || !ctx.key) return;
    updateAlertDispositionState(ctx.key, { stage, alert: ctx.alert, active: isAlertCurrentlyActive(ctx.key) });
    openAlertDispositionModal(ctx.index, { key: ctx.key, alert: ctx.alert, stage });
}

function alertDispositionPanelElement(index) {
    return document.getElementById('alert-disposition-modal-body') || document.getElementById(`alert-heal-panel-${index}`);
}

function guardActiveAlertAction(index) {
    const ctx = alertActionContext(index);
    if (!ctx.alert || !ctx.key) return null;
    if (!isAlertCurrentlyActive(ctx.key)) {
        showToast('该告警已消除，不再触发新的分析或自愈动作', 'error');
        updateAlertDispositionView(ctx.index, { stage: alertDispositionState(ctx.key).stage || 'analysis' });
        return null;
    }
    return ctx;
}

function refreshOpenAlertDispositionModal() {
    if (!_activeAlertDispositionKey) return;
    const item = alertDispositionState(_activeAlertDispositionKey);
    const index = activeAlertIndexByKey(_activeAlertDispositionKey);
    const alert = getAlertByKey(_activeAlertDispositionKey) || item.alert;
    if (!alert) return;
    openAlertDispositionModal(index, {
        key: _activeAlertDispositionKey,
        alert,
        stage: item.stage || 'details',
    });
}

function alertActionContext(index) {
    const keyFromModal = _activeAlertDispositionKey || '';
    const explicitKey = typeof index === 'string' ? index : '';
    const a = explicitKey
        ? (getAlertByKey(explicitKey) || alertDispositionState(explicitKey).alert)
        : (getAlertByKey(keyFromModal) || alertDispositionState(keyFromModal).alert || _filteredAlerts[index]);
    const key = alertDiagnosisKey(a) || keyFromModal;
    return { alert: a, key: key || explicitKey, index: typeof index === 'number' && index >= 0 ? index : activeAlertIndexByKey(key || explicitKey) };
}

function updateAlertDispositionView(index, options = {}) {
    const ctx = alertActionContext(index);
    if (!ctx.alert || !ctx.key) return ctx;
    if (options.skillResult) _alertSkillWorkflowCache.set(ctx.key, options.skillResult);
    if (options.agentRun) _alertAgentRunCache.set(ctx.key, options.agentRun);
    if (options.skillAgentRun) _alertSkillAgentRunCache.set(ctx.key, options.skillAgentRun);
    if (options.healResult) _alertHealCapabilityCache.set(ctx.key, options.healResult);
    updateAlertDispositionState(ctx.key, {
        stage: options.stage || alertDispositionState(ctx.key).stage || 'details',
        alert: ctx.alert,
        active: isAlertCurrentlyActive(ctx.key),
        skillResult: _alertSkillWorkflowCache.get(ctx.key) || null,
        agentRun: _alertAgentRunCache.get(ctx.key) || null,
        skillAgentRun: _alertSkillAgentRunCache.get(ctx.key) || null,
        healResult: _alertHealCapabilityCache.get(ctx.key) || null,
    });
    if (_activeAlertDispositionKey !== ctx.key) return ctx;
    _activeAlertHealIndex = ctx.index;
    _activeAlertHealHtml = renderAlertDispositionPanelHtml(ctx.index, {
        ...options,
        key: ctx.key,
        alert: ctx.alert,
        active: isAlertCurrentlyActive(ctx.key),
    });
    const panel = alertDispositionPanelElement(ctx.index);
    if (panel) panel.innerHTML = _activeAlertHealHtml;
    else openAlertDispositionModal(ctx.index, { ...options, key: ctx.key, alert: ctx.alert });
    renderAlertTable(_filteredAlerts);
    return { ...ctx, panel };
}

async function runSkillWorkflowFromAlert(index, stages = ['detect', 'diagnose', 'heal', 'verify'], stage = 'healing') {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return;
    const { alert: a, key } = guarded;
    index = key;
    updateAlertDispositionView(index, { mode: 'running', stage });
    const requestedStages = stages || ['detect', 'diagnose', 'heal', 'verify'];
    const body = buildSkillWorkflowBodyFromAlert(a, requestedStages);
    const payload = body.payload || {};
    const stageText = stage === 'analysis' ? 'detect/diagnose' : requestedStages.join('/');
    const query = [
        `[告警处置][${payload.fault_type || a.title || 'unknown'}]`,
        payload.message || a.description || a.title || '',
        `namespace=${payload.namespace || a.namespace || ''}`,
        `service=${payload.service || payload.deployment || a.service || ''}`,
        `source=${a.source || payload.source || ''}`,
        `请由 Agent 按假设、规划、调查、推理流程调用已注册 Skill 执行 ${stageText}，输出中文根因、证据、置信度、自愈建议和验证步骤。`,
    ].filter(Boolean).join('; ');
    const result = await runAgentSkillDiagnosis(query, payload.namespace || a.namespace || '', '', {
        maxPolls: 36,
        skillWorkflow: body,
        onStart: ({ run }) => {
            const runRecord = {
                ...run,
                alert_index: index,
                status: 'running',
                invocation_mode: 'agent_skill',
                requested_stages: requestedStages,
            };
            _alertSkillAgentRunCache.set(key, runRecord);
            updateAlertDispositionView(index, { skillAgentRun: runRecord, mode: 'running', stage });
            startAlertSkillAgentPolling(key, run.run_id, { index, stage, requestedStages });
            refreshAlertSkillAgentRun(index, { poll: true, stage, silent: true, runId: run.run_id });
        },
        onStatus: ({ run, status, skillEvent }) => {
            const runRecord = {
                ...run,
                alert_index: index,
                status: status?.status || 'running',
                invocation_mode: 'agent_skill',
                requested_stages: requestedStages,
            };
            _alertSkillAgentRunCache.set(key, runRecord);
            if (status) _alertSkillAgentStatusCache.set(run.run_id, status);
            const skillResult = agentSkillResultFromEvent(skillEvent || latestAgentSkillEvent(status), requestedStages);
            if (skillResult) _alertSkillWorkflowCache.set(key, skillResult);
            updateAlertDispositionView(index, {
                skillAgentRun: runRecord,
                skillResult: skillResult || _alertSkillWorkflowCache.get(key) || null,
                mode: status?.status === 'running' ? 'running' : 'ready',
                stage,
            });
        },
    });
    if (!result?.run?.run_id) {
        updateAlertDispositionView(index, { mode: 'failed', stage });
        showToast(stage === 'analysis' ? 'Agent+Skill 分析触发失败' : 'Agent+Skill 自愈触发失败', 'error');
        return;
    }
    const runRecord = {
        ...result.run,
        alert_index: index,
        status: result.status?.status || 'running',
        invocation_mode: 'agent_skill',
        requested_stages: requestedStages,
    };
    _alertSkillAgentRunCache.set(key, runRecord);
    if (result.status) _alertSkillAgentStatusCache.set(result.run.run_id, result.status);
    const skillResult = agentSkillResultFromEvent(result.skillEvent || latestAgentSkillEvent(result.status), requestedStages);
    if (skillResult) _alertSkillWorkflowCache.set(key, skillResult);
    updateAlertDispositionView(index, { skillAgentRun: runRecord, skillResult: skillResult || _alertSkillWorkflowCache.get(key) || null, stage });
    startAlertSkillAgentPolling(key, result.run.run_id, { index, stage, requestedStages });
    if (result.status?.status === 'running') refreshAlertSkillAgentRun(index, { poll: true, stage, silent: true, runId: result.run.run_id });
    showToast(stage === 'analysis' ? 'Agent+Skill 分析已触发' : 'Agent+Skill 自愈已触发');
}

function runSkillAnalysisFromAlert(index) {
    return runSkillWorkflowFromAlert(index, ['detect', 'diagnose'], 'analysis');
}

function runSkillHealingFromAlert(index) {
    return runSkillWorkflowFromAlert(index, ['detect', 'diagnose', 'heal', 'verify'], 'healing');
}

async function startAgentRunFromAlert(index) {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return null;
    const a = guarded.alert;
    const query = `[${(a.severity || 'warning').toUpperCase()}] ${a.title || ''} — ${a.description || ''} (service=${a.service || ''}, namespace=${a.namespace || ''})`;
    _activeRCAAlertKey = alertDiagnosisKey(a);
    return api('/api/rca/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            query,
            namespace: a.namespace || '',
            cluster_id: a.cluster_id || document.getElementById('alert-cluster-filter')?.value || '',
        }),
    });
}

async function runAgentWorkflowFromAlert(index) {
    const ctx = guardActiveAlertAction(index);
    if (!ctx) return;
    index = ctx.key;
    const run = await startAgentRunFromAlert(index);
    if (!run?.run_id) {
        showToast('智能体诊断触发失败', 'error');
        return;
    }
    const { key } = alertActionContext(index);
    const runRecord = { ...run, alert_index: index, status: 'running' };
    _alertAgentRunCache.set(key, runRecord);
    updateAlertDispositionView(index, { agentRun: runRecord, stage: 'analysis' });
    refreshAlertAgentRun(index, { poll: true });
    showToast('智能体诊断已启动');
}

function runAgentAnalysisFromAlert(index) {
    return runAgentWorkflowFromAlert(index);
}

async function runAgentSkillDiagnosis(query, namespace = '', renderTarget = '', options = {}) {
    const out = renderTarget ? document.getElementById(renderTarget) : null;
    if (out) out.textContent = '正在启动 Agent+Skill RCA，Agent 将按假设、规划、调查、推理执行...';
    const run = await api('/api/rca/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            query,
            namespace,
            cluster_id: options.skillWorkflow?.payload?.cluster_id || options.clusterId || '',
            ...(options.skillWorkflow ? { skill_workflow: options.skillWorkflow } : {}),
        }),
        timeoutMs: 60000,
    });
    if (!run?.run_id) {
        if (out) out.textContent = 'Agent RCA 启动失败。';
        return null;
    }
    if (typeof options.onStart === 'function') options.onStart({ run });
    const maxPolls = options.maxPolls || 30;
    let status = null;
    for (let i = 0; i < maxPolls; i += 1) {
        await sleep(i === 0 ? 800 : 2500);
        status = await api(`/api/rca/${encodeURIComponent(run.run_id)}?_=${Date.now()}`, { cache: 'no-store' });
        if (!status) continue;
        if (out) out.textContent = `Agent RCA ${status.status || 'running'} · run=${run.run_id}`;
        const skillEvent = [...(status.events || [])].reverse().find(e => e.event === 'skill_runtime');
        if (typeof options.onStatus === 'function') options.onStatus({ run, status, skillEvent });
        if (skillEvent || ['completed', 'failed'].includes(status.status)) {
            return { run, status, skillEvent };
        }
    }
    return { run, status, skillEvent: null };
}

function renderAgentSkillDiagnosisResult(containerId, result, title = 'Agent+Skill 诊断结果') {
    const panel = document.getElementById(containerId);
    if (!panel) return;
    const skillEvent = result?.skillEvent || [...(result?.status?.events || [])].reverse().find(e => e.event === 'skill_runtime');
    const skillResult = skillEvent?.result || {};
    panel.style.display = '';
    panel.innerHTML = `<div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-info">Agent 调用 Skill</span>
                <strong>${escapeHtml(title)}</strong>
                <span class="text-muted">${escapeHtml(result?.run?.run_id || '-')}</span>
            </div>
            <span class="badge badge-${result?.status?.status === 'completed' ? 'success' : 'warning'}">${escapeHtml(statusLabel(result?.status?.status || 'running'))}</span>
        </div>
        ${renderAgentWorkflowDetail(result?.status || {})}
        ${skillEvent && skillResult ? renderAlertSkillExecutionProcess(agentSkillResultFromEvent(skillEvent) || skillResult, ['detect', 'diagnose', 'heal', 'verify'], { open: true, showMissing: true }) : ''}`;
}

async function refreshAlertAgentRun(index, options = {}) {
    return refreshAlertRun(index, options, 'multi_agent');
}

async function refreshAlertSkillAgentRun(index, options = {}) {
    return refreshAlertRun(index, options, 'agent_skill');
}

async function refreshAlertRun(index, options, mode) {
    const { alert: a, key } = alertActionContext(index);
    if (!a) return null;
    const skillMode = mode === 'agent_skill';
    const runs = skillMode ? _alertSkillAgentRunCache : _alertAgentRunCache;
    const statuses = skillMode ? _alertSkillAgentStatusCache : _alertAgentStatusCache;
    const run = runs.get(key);
    if (!run?.run_id) return null;
    if (options.runId && options.runId !== run.run_id) return null;
    let status;
    try {
        status = await api(`/api/rca/${encodeURIComponent(run.run_id)}?_=${Date.now()}`, {
            cache: 'no-store', throwOnError: true, suppressErrorLog: true, timeoutMs: 20000,
        });
    } catch (error) {
        if (error?.status === 404) {
            status = { id: run.run_id, status: 'expired', error: '运行记录已过期或服务已重启' };
        } else {
            if (_activeAlertDispositionKey === key) {
                const panel = document.getElementById('alert-disposition-modal-body');
                if (panel && !panel.querySelector('[data-poll-error]')) {
                    panel.insertAdjacentHTML('afterbegin', '<p data-poll-error class="text-warning">状态同步失败，正在重试；当前显示最近一次结果。</p>');
                }
            }
        }
    }
    if (runs.get(key)?.run_id !== run.run_id) return null;
    if (!status) {
        if (options.poll) startAlertRunPolling(key, run.run_id, mode, options);
        return null;
    }
    // A slower request must not regress a terminal run to an old running snapshot.
    const previous = statuses.get(run.run_id);
    if (['completed', 'failed', 'cancelled', 'expired'].includes(previous?.status)
            && !['completed', 'failed', 'cancelled', 'expired'].includes(status.status)) return previous;
    statuses.set(run.run_id, status);
    const updatedRun = { ...run, status: status.status || run.status };
    runs.set(key, updatedRun);
    const patch = skillMode ? { skillAgentRun: updatedRun } : { agentRun: updatedRun };
    if (skillMode) {
        const skillResult = agentSkillResultFromEvent(latestAgentSkillEvent(status), run.requested_stages || ['detect', 'diagnose']);
        if (skillResult) _alertSkillWorkflowCache.set(key, skillResult);
        patch.skillResult = skillResult || _alertSkillWorkflowCache.get(key) || null;
    }
    updateAlertDispositionState(key, patch);
    if (_activeAlertDispositionKey === key) {
        updateAlertDispositionView(key, { ...patch, stage: options.stage || alertDispositionState(key).stage || 'analysis' });
    }
    if (options.poll && !['completed', 'failed', 'cancelled', 'expired'].includes(status.status)) {
        startAlertRunPolling(key, run.run_id, mode, options);
    }
    if (!options.silent && status.status !== 'running') {
        showToast(status.status === 'completed' ? '分析已完成' : status.error || '分析已结束');
    }
    return status;
}

async function runAlertCompareWorkflow(index) {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return;
    updateAlertDispositionView(index, { mode: 'running', stage: 'analysis' });
    await runSkillWorkflowFromAlert(index, ['detect', 'diagnose'], 'analysis');
}

function runAlertAnalysisCompare(index) {
    return runAlertCompareWorkflow(index);
}

async function showAlertHealCapability(index) {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return;
    const { alert: a, key } = guarded;
    _lastAlertHealResult = null;
    updateAlertDispositionView(index, { mode: 'running', stage: 'healing' });
    const btn = document.getElementById(`alert-heal-btn-${index}`);
    if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
    const result = await api('/api/heal/capability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildHealPayloadFromAlertWithDiagnosis(a)),
        timeoutMs: 8000,
    });
    if (!result) {
        const panel = alertDispositionPanelElement(index);
        if (panel) panel.innerHTML = renderAlertHealError('自愈能力生成超时或失败。已避免无限等待，请稍后重试，或先做 RCA 补齐上下文。');
        if (btn) { btn.disabled = false; btn.textContent = '自愈'; }
        return;
    }
    _lastAlertHealResult = result;
    _alertHealCapabilityCache.set(key, result);
    updateAlertDispositionView(index, { healResult: result, stage: 'healing' });
    if (btn) { btn.disabled = false; btn.textContent = '自愈'; }
}

async function runAgentHealingFromAlert(index) {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return;
    const { alert: a, key } = guarded;
    updateAlertDispositionView(index, { mode: 'running', stage: 'healing' });
    let agentRun = _alertAgentRunCache.get(key);
    if (!agentRun) {
        const run = await startAgentRunFromAlert(index);
        if (run?.run_id) {
            agentRun = { ...run, alert_index: index, status: 'running', invocation_mode: 'multi_agent' };
            _alertAgentRunCache.set(key, agentRun);
            refreshAlertAgentRun(index, { poll: true, stage: 'healing', silent: true });
        }
    }
    const result = await api('/api/heal/capability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildHealPayloadFromAlertWithDiagnosis(a)),
        timeoutMs: 10000,
    });
    if (result) {
        _lastAlertHealResult = result;
        _alertHealCapabilityCache.set(key, result);
    }
    updateAlertDispositionView(index, {
        agentRun,
        healResult: result,
        mode: result || agentRun ? 'ready' : 'failed',
        stage: 'healing',
    });
    showToast(result ? '多智能体自愈方案已生成' : '多智能体自愈方案生成失败', result ? 'success' : 'error');
}

async function runAlertHealingCompare(index) {
    const guarded = guardActiveAlertAction(index);
    if (!guarded) return;
    updateAlertDispositionView(index, { mode: 'running', stage: 'healing' });
    await runSkillWorkflowFromAlert(index, ['detect', 'diagnose', 'heal', 'verify'], 'healing');
}

function renderAlertHealLoading(text = '生成自愈能力...') {
    return `<div class="alert-heal-panel">
        <div class="heal-process">
            ${renderHealProcessSteps('detect')}
        </div>
        <div class="loading">${escapeHtml(text)}</div>
    </div>`;
}

function renderAlertHealError(message) {
    return `<div class="alert-heal-panel">
        ${renderHealProcessSteps('failed')}
        <div class="text-danger" style="padding:10px 0">${escapeHtml(message)}</div>
    </div>`;
}

function renderHealProcessSteps(active) {
    const steps = [
        ['detect', '告警识别', '提取类型和对象'],
        ['kb', '知识库检索', '匹配自愈策略'],
        ['plan', '方案生成', '命令和回滚动作'],
        ['approve', '审批确认', '风险门禁'],
        ['execute', '执行/验证', 'Dry-run 或真实执行'],
    ];
    const order = steps.map(s => s[0]);
    const activeIdx = order.indexOf(active);
    return `<div class="heal-process-steps">${steps.map((s, i) => {
        let cls = 'pending';
        if (active === 'failed') cls = i <= 1 ? 'failed' : 'pending';
        else if (i < activeIdx) cls = 'done';
        else if (i === activeIdx) cls = 'active';
        return `<div class="heal-step ${cls}">
            <span>${i + 1}</span>
            <strong>${escapeHtml(s[1])}</strong>
            <small>${escapeHtml(s[2])}</small>
        </div>`;
    }).join('')}</div>`;
}

function renderAlertHealPanel(result, index) {
    const container = alertDispositionPanelElement(index) || document.getElementById('alert-scan-result');
    _activeAlertHealHtml = renderAlertHealPanelHtml(result, index);
    container.innerHTML = _activeAlertHealHtml;
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderAlertHealPanelHtml(result, index) {
    const summary = result.summary || {};
    const target = result.target || {};
    const capability = result.capability || {};
    const suggestions = result.suggestions || [];
    const blocked = result.blocked_templates || [];
    const available = suggestions.length > 0;
    const a = _filteredAlerts[index];
    const diagnosis = a ? _alertDiagnosisCache.get(alertDiagnosisKey(a)) : null;
    const diagnosisMode = diagnosis ? '已使用分析诊断结果' : '未关联诊断结果';
    return `
        <div class="alert-heal-panel">
            ${renderHealProcessSteps(available ? 'approve' : 'plan')}
            <div class="alert-heal-header">
                <div>
                    <h4>智能体自愈方案</h4>
                    <p class="text-muted">知识库${result.recipe ? '命中' : '未命中'} · ${diagnosisMode} · 故障类型 ${escapeHtml(summary.fault_type || result.fault_type || 'unknown')} · namespace ${escapeHtml(summary.namespace || result.namespace || 'default')}</p>
                </div>
                <div class="table-actions">
                    ${available ? `<button class="btn btn-sm btn-primary" onclick="openHealPlanFromAlert(${index})">查看/编辑方案</button>` : ''}
                    ${available ? `<button class="btn btn-sm" onclick="dryRunHealFromAlert(${index})">快速 Dry-run</button>` : ''}
                    <button class="btn btn-sm" onclick="startRCAFromAlert(${index})">先做 RCA</button>
                    <button class="btn btn-sm" onclick="switchView('healcenter')">自愈中心</button>
                </div>
            </div>
            <div class="alert-heal-meta">
                <span class="badge badge-${available ? 'success' : 'warning'}">${available ? '可生成自愈方案' : '暂无可执行方案'}</span>
                <span class="badge badge-${diagnosis ? 'success' : 'warning'}">${diagnosis ? '诊断驱动' : '告警文本降级'}</span>
                <span class="badge badge-info">目标 pod=${escapeHtml(target.pod || '-')}</span>
                <span class="badge badge-info">deployment=${escapeHtml(target.deployment || '-')}</span>
                <span class="badge badge-info">node=${escapeHtml(target.node || '-')}</span>
                ${capability.supports_rollback ? '<span class="badge badge-success">支持回滚</span>' : ''}
                ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
            </div>
            ${diagnosis ? `<div class="rca-remediation"><strong>诊断依据：</strong>${escapeZh(diagnosis.root_cause || faultTypeLabel(diagnosis.fault_type) || '')}</div>` : '<div class="rca-remediation"><strong>诊断依据：</strong>当前未找到该告警的分析结果，方案基于告警文本和对象生成。建议先点击“分析”。</div>'}
            ${result.recipe?.description ? `<div class="rca-remediation">${escapeHtml(result.recipe.description)}</div>` : ''}
            <div class="heal-stage-groups">
                <div class="heal-stage-card">
                    <strong>1. 只读确认</strong>
                    <span>优先执行 describe/top/logs 等观测动作，降低误修复概率。</span>
                </div>
                <div class="heal-stage-card">
                    <strong>2. 恢复动作</strong>
                    <span>对 rollout restart/undo/cordon 等动作应用审批和高风险门禁。</span>
                </div>
                <div class="heal-stage-card">
                    <strong>3. 验证回滚</strong>
                    <span>记录回滚命令，运行结果可在自愈中心查看。</span>
                </div>
            </div>
            <div class="rem-actions-list">
                ${suggestions.map((s, i) => renderHealSuggestionItem(s, i)).join('')}
                ${blocked.map((s, i) => renderHealBlockedItem(s, i)).join('')}
            </div>
            <pre class="log-viewer" id="alert-heal-output-${index}" style="min-height:90px">${available ? '等待用户确认：点击 Dry-run 预演查看将执行的自愈动作。' : '当前告警缺少可落地目标对象或知识库策略。建议先执行根因分析补齐上下文。'}</pre>
        </div>`;
}

function renderHealSuggestionItem(s, i) {
    const risk = (s.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="rem-action-item">
        <div class="rem-action-header"><span class="rem-action-num">${i + 1}</span><span class="badge badge-${riskClass}">${escapeHtml(riskLabel(s.risk))}</span><span class="badge badge-info">${escapeHtml(sourceLabel(s.source))}</span><span class="rem-action-desc">${escapeHtml(s.step || '')}</span></div>
        <div class="rem-action-cmd"><code>${escapeHtml(s.command || '')}</code></div>
        ${s.selection_reason ? `<div class="rem-action-rollback">匹配分=${escapeHtml(s.selection_score ?? '-')} · ${escapeHtml(s.selection_reason)}</div>` : ''}
        ${s.rollback_command ? `<div class="rem-action-rollback">回滚命令: ${escapeHtml(s.rollback_command)}</div>` : ''}
    </div>`;
}

function renderHealBlockedItem(s, i) {
    return `<div class="rem-action-item blocked">
        <div class="rem-action-header"><span class="rem-action-num">!</span><span class="badge badge-warning">已阻断</span><span class="rem-action-desc">${escapeHtml(s.step || `模板 ${i + 1}`)}</span></div>
        <div class="rem-action-cmd"><code>${escapeHtml(s.command || '')}</code></div>
        <div class="rem-action-rollback">${escapeHtml(s.blocked_reason || '已阻断')}</div>
    </div>`;
}

async function dryRunHealFromAlert(index) {
    const { alert: a, key } = alertActionContext(index);
    if (!a) return;
    const out = document.getElementById(`alert-heal-output-${index}`) || document.getElementById('alert-heal-output');
    if (out) out.textContent = 'Dry-run 执行中...';
    const panel = alertDispositionPanelElement(index);
    if (panel && _lastAlertHealResult) {
        const cloned = panel.querySelector('.heal-process-steps');
        if (cloned) cloned.outerHTML = renderHealProcessSteps('execute');
    }
    const result = await api('/api/heal/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...buildHealPayloadFromAlertWithDiagnosis(a), dry_run: true, source: 'alert-center' }),
    });
    if (!result) {
        if (out) out.textContent = 'Dry-run 失败，请检查后端日志。';
        showToast('告警自愈 dry-run 失败', 'error');
        return;
    }
    if (out) out.textContent = JSON.stringify(result, null, 2);
    updateAlertDispositionState(key, { stage: 'healing', alert: a, active: isAlertCurrentlyActive(key) });
    showToast('告警自愈 dry-run 已完成');
}

function openHealPlanFromAlert(index) {
    const { alert: a, key } = alertActionContext(index);
    const result = _lastAlertHealResult || _alertHealCapabilityCache.get(key);
    if (!a || !result) return;
    openHealPlanModal(result, buildHealPayloadFromAlertWithDiagnosis(a), { source: 'alert-center' });
}

let _alertCompressionScan = null;

function alertLifecycleStatus(status) {
    return ({active:'持续触发', observed:'历史事件', pending_resolution:'等待确认解除',
        resolved:'告警已解除', expired:'历史事件已过期', event_missing:'事件未再次检出'}[status] || '状态未知');
}

async function loadAlertLifecycle() {
    const scan = _alertCompressionScan;
    const target = document.getElementById('alert-lifecycle-history');
    if (!scan || !target) return;
    target.textContent = '加载中';
    const params = new URLSearchParams({cluster_id: scan.cluster_id || '', namespace: scan.scan_scope || ''});
    const data = await api(`/api/alerts/lifecycle?${params}`);
    if (_alertCompressionScan !== scan) return;
    if (!data) { target.textContent = '生命周期历史暂不可用'; return; }
    target.innerHTML = (data.incidents || []).map(s => `<p>${escapeHtml(s.namespace)} / ${escapeHtml(s.entity)} · ${escapeHtml(s.name)} · ${escapeHtml(alertLifecycleStatus(s.status))} · 第 ${Number(s.episode)} 次<br>首次观测：${escapeHtml(new Date(s.first_observed_at * 1000).toLocaleString())}；最近检出：${escapeHtml(new Date(s.last_observed_at * 1000).toLocaleString())}</p>`).join('') || '暂无生命周期记录';
}

let _alertScanJob = null;
let _alertScanCluster = null;
let _alertScanTimer = null;
let _alertScanGeneration = 0;
const alertScanStorageKey = cluster => `agenticsre.alertScan.v1.${cluster}`;

function saveAlertScanJob(job) {
    try { localStorage.setItem(alertScanStorageKey(job.cluster_id), JSON.stringify(job)); } catch (_) { /* storage disabled */ }
}

function restoreAlertScan(cluster) {
    if (_alertScanCluster === cluster) return;
    clearTimeout(_alertScanTimer);
    ++_alertScanGeneration;
    _alertScanCluster = cluster;
    _alertScanJob = null;
    _alertCompressionScan = null;
    try {
        const saved = JSON.parse(localStorage.getItem(alertScanStorageKey(cluster)) || 'null');
        if (saved?.cluster_id === cluster && /^job-[0-9a-f]{32}$/.test(saved.job_id)) _alertScanJob = saved;
    } catch (_) { /* no valid saved job */ }
    const container = document.getElementById('alert-scan-result');
    if (container) container.innerHTML = '';
    if (_alertScanJob) pollAlertScan(_alertScanJob, ++_alertScanGeneration);
}

function alertReviewBlockedText(job) {
    const reason = job.resume_blocked_reason || job.budget?.exhausted_reason;
    if (reason === 'result_edited') return '分组已人工编辑，旧审核不适用于当前版本；请重新扫描，当前原文和调整记录仍可查看。';
    if (reason === 'result_revision_changed') return '结果版本已变化，旧作业不能继续审核当前分组；请重新加载结果。';
    if (reason === 'result_unavailable') return '当前结果暂时无法核验，已停止旧作业重试；请先恢复或重新加载保存的结果。';
    if (reason === 'batch_input_bytes') return '待审证据超过单次审核上限，当前设置下无法继续；完整原文仍可查看。';
    if (reason === 'input_bytes') return '剩余输入额度无法容纳未完成的证据，当前设置下无法继续；完整原文仍可查看。';
    if (reason === 'no_pending_pairs') return '候选关系已审核完成，无需重试。';
    return '本次审核额度或时限已用尽，可查看保留证据，或重新扫描获取最新观测。';
}

function renderAlertScanJob(job, warning = '') {
    const container = document.getElementById('alert-scan-result');
    if (!container) return;
    const labels = {submitting:'正在提交', queued:'排队中', running:'采集与分析中', partial:'部分关系待审',
        failed:'扫描失败', cancelled:'已取消', completed:'扫描完成'};
    const active = ['queued', 'running'].includes(job.status);
    const resultLabel = {edited:'分组已编辑 · 旧审核已失效', unavailable:'结果暂时无法核验', revision_changed:'结果版本已变化'}[job.result_state];
    container.innerHTML = `<div role="status">
        <strong>${escapeHtml(resultLabel || labels[job.status] || '正在查询进度')}</strong>
        <p>任务已保存，刷新页面后可继续查看进度。${job.candidate_pairs != null ? ` ${job.review_applies_to_result === false ? '前版本审核记录：' : ''}已完成 ${Number(job.completed_pairs || 0)} / ${Number(job.candidate_pairs)} 对关系分析。` : ''}</p>
        ${warning ? `<p class="text-warning">${escapeHtml(warning)}</p>` : ''}
        ${job.error ? `<p class="text-danger">${escapeHtml(job.error)}</p>` : ''}
        ${(['partial', 'failed', 'cancelled'].includes(job.status) || job.review_applies_to_result === false) && job.can_resume === false ? `<p class="text-muted">${escapeHtml(alertReviewBlockedText(job))}</p>` : ''}
        ${active ? '<button class="btn btn-sm" onclick="controlAlertScan(\'cancel\')">取消扫描</button>' : ''}
        ${['partial', 'failed', 'cancelled'].includes(job.status) && job.can_resume !== false ? `<button class="btn btn-sm" onclick="controlAlertScan('resume')">${job.resume_action === 'publish_result' ? '重试保存结果' : '重试未完成部分'}</button>` : ''}
    </div>`;
}

async function pollAlertScan(job, generation) {
    if (generation !== _alertScanGeneration || _alertScanJob !== job) return;
    clearTimeout(_alertScanTimer);
    renderAlertScanJob(job);
    let data;
    try {
        data = job.status === 'submitting'
            ? await api('/api/alerts/scan-jobs', {method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({job_id:job.job_id, cluster_id:job.cluster_id}),
                throwOnError:true, suppressErrorLog:true, timeoutMs:10000})
            : await api(`/api/alerts/compression-jobs/${job.job_id}`, {throwOnError:true, suppressErrorLog:true, timeoutMs:10000});
    } catch (error) {
        if (generation !== _alertScanGeneration) return;
        // Retrying a submission with the same id cannot create another scan.
        if (error.status === 404) {
            job.status = 'failed'; job.error = '任务记录已不存在，请重新扫描。';
            saveAlertScanJob(job); renderAlertScanJob(job); return;
        }
    }
    if (generation !== _alertScanGeneration || _alertScanJob !== job) return;
    if (!data) {
        renderAlertScanJob(job, '暂时无法获取进度，将自动重试；这不表示扫描失败。');
    } else {
        Object.assign(job, data);
        saveAlertScanJob(job);
        if (job.status === 'completed' || (job.status === 'partial' && job.result_url)
                || (job.result_state === 'edited' && job.result_url)) {
            const result = await api(`/api/alerts/compression/${job.scan_id}`);
            if (generation !== _alertScanGeneration) return;
            if (result) { renderAlertCompression(result); return; }
            renderAlertScanJob(job, '结果暂时无法读取，将自动重试。');
        } else {
            renderAlertScanJob(job);
            if (['partial','failed','cancelled'].includes(job.status)) return;
        }
    }
    _alertScanTimer = setTimeout(() => pollAlertScan(job, generation), 2000);
}

async function runAlertScan() {
    const cluster = document.getElementById('alert-cluster-filter')?.value || '';
    restoreAlertScan(cluster);
    if (_alertScanJob && ['submitting','queued','running'].includes(_alertScanJob.status)) {
        pollAlertScan(_alertScanJob, ++_alertScanGeneration); return;
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const job = {job_id:'job-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join(''),
                 cluster_id:cluster, status:'submitting'};
    _alertScanJob = job;
    saveAlertScanJob(job);
    pollAlertScan(job, ++_alertScanGeneration);
}

async function controlAlertScan(action) {
    const job = _alertScanJob;
    if (!job) return;
    const generation = ++_alertScanGeneration;
    clearTimeout(_alertScanTimer);
    const result = await api(`/api/alerts/compression-jobs/${job.job_id}/${action}`, {method:'POST'});
    if (generation !== _alertScanGeneration || _alertScanJob !== job) return;
    if (result) Object.assign(job, result);
    saveAlertScanJob(job);
    pollAlertScan(job, generation);
}

function metricEvidenceText(value) {
    return ({ready:'指标证据已取得', partial:'部分分组已取证', unknown:'证据不足', unavailable:'采集不可用',
        unconfigured:'未配置指标后端', skipped:'本次未取证', not_applicable:'不适用业务指标取证',
        invalidated_by_edit:'分组已编辑，请重新扫描',
        'constant heartbeat rule has no workload measurement':'恒定心跳用于检查告警通路，没有业务资源指标。',
        'constant series':'历史数值恒定，无法判断统计相关性。',
        'unsupported rule expression':'规则表达式暂未适配，请配置测量查询或重新审阅规则。',
        'missing or unsupported resource identity':'缺少支持的资源身份，无法唯一关联指标。',
        'measurement series is missing or ambiguous':'指标序列缺失或不唯一。',
        'firing rule is missing or ambiguous':'触发规则缺失或不唯一。',
        'insufficient numeric history coverage':'历史样本数量或时间覆盖不足。',
        simple_comparison_operand:'原规则指标选择器', reviewed_expression:'已审阅的完整规则适配',
        rule_annotation:'规则显式配置的测量查询'}[value] || value || '暂无指标取证记录');
}

function renderGroupMetricEvidence(data, group) {
    if (data.scag?.status === 'invalidated_by_edit') return '<p class="text-muted">指标证据：分组已编辑，请重新扫描。</p>';
    const e = data.metric_evidence?.groups?.[group.group_id];
    if (!e) return '<p class="text-muted">指标证据：本组暂无取证记录。</p>';
    if (e.status === 'composite') return `<details class="metric-evidence"><summary>多指标证据 · ${(e.measurements || []).length} 份独立测量</summary><p>各指标独立保留，不将不同单位拼成一条序列。</p>${(e.measurements || []).map(m => renderGroupMetricEvidence({...data, metric_evidence:{groups:{[group.group_id]:m}}}, group)).join('')}</details>`;
    const samples = Array.isArray(e.points) ? e.points : [];
    const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未知';
    const meanings = {
        'TargetDown-v1':'不可达抓取目标的百分比，保留原规则分组。',
        'KubePodNotReady-v1':'Pending / Unknown 阶段测量值，保留原规则的非 Job owner 关联。',
        'etcdMembersDown-v1':'原规则的复合测量值，包含成员不可达与对端通信失败分支。',
        'etcdInsufficientMembers-v1':'可达成员数减去动态阈值；负值表示不足。'
    };
    const adapter = e.measurement_adapter;
    return `<details class="metric-evidence"><summary>指标证据 · ${escapeHtml(metricEvidenceText(e.status))}${samples.length ? ` · ${samples.length} 个历史点` : ''}</summary>
        ${e.reason || e.error ? `<p>${escapeHtml(metricEvidenceText(e.reason || e.error))}</p>` : ''}
        ${e.status === 'ready' ? '<p>取证成功不等于统计相关，也不代表已确认根因。</p>' : ''}
        ${e.query_basis ? `<p>测量来源：${escapeHtml(metricEvidenceText(e.query_basis))}</p>` : ''}
        ${adapter ? `<p>规则适配：${escapeHtml(adapter.adapter_id)}<br>${escapeHtml(meanings[adapter.adapter_id] || adapter.meaning || '')}</p>` : ''}
        ${e.rule ? `<p>规则：${escapeHtml(e.rule.name || '')}</p><pre class="metric-code">${escapeHtml(e.rule.expression || '')}</pre>` : ''}
        ${e.query ? `<details><summary>查看实际测量查询与序列标签</summary><pre class="metric-code">${escapeHtml(e.query)}</pre><pre class="metric-code">${escapeHtml(JSON.stringify(e.series_labels || {}, null, 2))}</pre></details>` : ''}
        ${Number.isFinite(e.start) ? `<p>窗口：${escapeHtml(time(e.start))} — ${escapeHtml(time(e.end))}<br>步长：${escapeHtml(String(e.step_s))} 秒；时间网格覆盖率：${Number.isFinite(e.coverage) ? (e.coverage * 100).toFixed(0) + '%' : '未知'}</p>` : ''}
        ${e.evidence_id ? `<p class="metric-code">证据 ID：${escapeHtml(e.evidence_id)}</p>` : ''}
        ${samples.length ? `<details><summary>查看历史样本（${samples.length} 点${samples.length > 100 ? '，展示前 100 点' : ''}）</summary><div class="metric-samples"><table><thead><tr><th scope="col">时间</th><th scope="col">测量值</th></tr></thead><tbody>${samples.slice(0,100).map(p => `<tr><td>${escapeHtml(time(p[0]))}</td><td>${escapeHtml(String(p[1]))}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    </details>`;
}

async function loadAlertReviewPairs(offset = 0) {
    const scan = _alertCompressionScan;
    if (!scan) return;
    const page = await api(`/api/alerts/compression/${scan.scan_id}/page?section=review_pairs&offset=${offset}&limit=50`);
    if (_alertCompressionScan !== scan || !page) return;
    const target = document.getElementById('alert-review-pairs');
    if (!target) return;
    if (page.revision !== scan.revision) {
        target.textContent = '结果版本已变化，请刷新扫描结果后查看。'; return;
    }
    const names = new Map((scan.groups || []).map(g => [g.group_id, `${g.entity || ''} / ${g.representative || ''}`]));
    const labels = {ok:'已审核', failed:'审核失败', deferred:'待审核', unreviewed:'未审核'};
    const reasons = {batch_input_bytes:'证据超过单次输入上限', input_bytes:'剩余输入额度不足',
        model_calls:'调用额度用尽', output_tokens:'输出额度不足', deadline:'审核时限已到',
        model_unavailable:'审核模型不可用', interrupted:'审核已中断'};
    Object.assign(reasons,{provider_authentication_failed:'模型认证失败，等待服务修复',
        provider_payment_required:'模型服务返回 402，等待服务修复',provider_permission_denied:'模型访问被拒绝，等待服务修复',
        provider_endpoint_unavailable:'模型入口不可用，等待服务修复'});
    target.innerHTML = `<p>第 ${offset + 1}–${offset + page.items.length} 对，共 ${page.total} 对。已审核不表示已确认共同根因。</p>` +
        page.items.map(p => `<p>${escapeHtml(names.get(p.left) || p.left)} ↔ ${escapeHtml(names.get(p.right) || p.right)}：${escapeHtml(labels[p.review_status] || '未审核')}${reasons[p.review_reason] ? `（${escapeHtml(reasons[p.review_reason])}）` : ''}</p>`).join('') +
        (offset > 0 ? `<button class="btn btn-sm" onclick="loadAlertReviewPairs(${Math.max(0,offset-50)})">上一页</button>` : '') +
        (page.next_offset !== null ? `<button class="btn btn-sm" onclick="loadAlertReviewPairs(${page.next_offset})">下一页</button>` : '');
}

function renderAlertCompression(data) {
    _alertCompressionScan = data;
    const container = document.getElementById('alert-scan-result');
    let html = `<div style="margin-bottom:12px">
        <span class="badge badge-info">原始告警: ${data.total_alerts || 0}</span>
        <span class="badge badge-success">事件组: ${data.compressed_groups || 0}</span>
        <span class="badge badge-warning">条数压缩率: ${((data.compression_ratio || 0) * 100).toFixed(0)}%</span>
        <p>原始告警完整保留；分组与待验证关联不代表已确认根因。</p>
        ${data.integrity?.status === 'passed' ? `<p class="alert-partition-integrity">本版本结构校验：${Number(data.integrity.partition_members)} 条成员完整覆盖原文，遗漏 ${Number(data.integrity.omitted_alerts)} 条。结构校验不代表语义合并或根因正确。</p>` : '<p class="text-muted">此记录未保存完整分区校验凭据，不能倒推已经通过。</p>'}
        ${data.semantic?.status === 'invalidated_by_edit' ? '<p class="text-warning alert-review-invalidated">分组已编辑，旧关系审核已失效；需要重新扫描和审核，原始告警仍可回查。</p>' : ''}
        ${data.semantic?.provider_failure ? `<p class="text-warning alert-provider-failure">模型服务拒绝本次审核（HTTP ${Number(data.semantic.provider_failure.status_code)}），已停止后续批次。失败和待审候选均保留，不代表已判定无关；修复服务后可在剩余预算内手动重试。</p>` : ''}
        ${data.warning ? `<p class="text-danger">${escapeHtml(data.warning)}</p>` : ''}
        ${['failed','partial','unavailable'].includes(data.semantic?.status) ? `<p class="text-warning">关系审核尚未全部完成，事实证据仍可查看和诊断。已审核 ${Number(data.semantic.completed_pairs || 0)}，失败 ${Number(data.semantic.failed_pairs || 0)}，待审 ${Number(data.semantic.deferred_pairs || 0)} 对；待审不表示无关。</p>` : ''}
        ${data.semantic?.status !== 'invalidated_by_edit' && _alertScanJob?.scan_id === data.scan_id && _alertScanJob.status === 'partial' ? (_alertScanJob.can_resume !== false ? '<button class="btn btn-sm" onclick="controlAlertScan(\'resume\')">重试未完成审核</button>' : `<p class="text-muted">${escapeHtml(alertReviewBlockedText(_alertScanJob))}</p>`) : ''}
        ${data.semantic?.candidate_pairs ? '<details><summary>关系审核清单</summary><button class="btn btn-sm" onclick="loadAlertReviewPairs(0)">加载审核状态</button><div id="alert-review-pairs"></div></details>' : ''}
        <input id="compression-edit-reason" placeholder="拆分 / 合并原因（必填）" aria-label="拆分或合并原因">
        <button class="btn btn-sm" onclick="editAlertGroups('merge')">合并选中事件组</button>
    </div>`;
    if (data.retrieval) html += `<p>关联召回：${escapeHtml(data.retrieval.status === 'ok' ? '语义向量 + 关键词' : '关键词降级 / 等待重新扫描')}，覆盖 ${data.retrieval.indexed_groups || 0} / ${data.retrieval.total_groups || 0} 组。</p>`;
    const evidenceStatus = s => ({ready:'已就绪', warming_up:'历史样本不足', ineligible:'本次采集不满足条件', unavailable:'采集不可用', mapped:'已映射', no_mapped_calls:'缺少可映射调用', no_target_services:'未找到告警对应服务', observed:'已取得版本变化', no_comparable_revisions:'缺少可比较版本', inventory_unavailable:'资源清单不可用', invalidated_by_edit:'编辑后待重新扫描'}[s] || s || '未知');
    if (data.automatic_baseline) html += `<p>自动历史基线：${escapeHtml(evidenceStatus(data.automatic_baseline.status))}，${data.automatic_baseline.snapshots || 0} 个独立时间桶；统计完整扫描中的告警数量。</p>`;
    if (data.lifecycle) {
        const changes = data.lifecycle.transitions || [];
        html += `<p>生命周期：${data.lifecycle.status === 'ok' ? '已更新' : '本次未更新，保留已有状态'}；本次新增 ${changes.filter(s => s.action === 'opened').length}，再次触发 ${changes.filter(s => s.action === 'reopened').length}，解除 ${changes.filter(s => s.action === 'resolved').length}。</p>
            <p>首次观测不等于实际首次触发；告警解除表示连续完整扫描中不再触发，业务恢复仍需验证。历史事件过期不代表故障恢复。</p>
            <details><summary>查看生命周期历史</summary><button class="btn btn-sm" onclick="loadAlertLifecycle()">加载最近 200 条记录</button><div id="alert-lifecycle-history"></div></details>`;
    }
    if (data.context_evidence) html += `<p>Tracing：${escapeHtml(evidenceStatus(data.context_evidence.traces?.status))}，${data.context_evidence.traces?.edges?.length || 0} 条已映射调用，${data.context_evidence.traces?.observed_spans?.length || 0} 份已映射请求 Span；发布变更：${escapeHtml(evidenceStatus(data.context_evidence.rollouts?.status))}，${data.context_evidence.rollouts?.changes?.length || 0} 条版本变化。</p>`;
    if (data.metric_evidence) {
        const entries = data.scag?.status === 'invalidated_by_edit' ? [] : (data.groups || []).map(g => data.metric_evidence.groups?.[g.group_id]).filter(Boolean);
        html += `<section class="metric-overview" aria-label="指标取证概览"><strong>实时指标取证</strong><p>${data.scag?.status === 'invalidated_by_edit' ? '分组已编辑，请重新扫描。' : `已取得 ${entries.filter(e => ['ready','composite'].includes(e.status)).length} 组；不适用 ${entries.filter(e => e.status === 'not_applicable').length} 组；待补证 ${entries.filter(e => !['ready','composite','not_applicable'].includes(e.status)).length} 组。`}</p><p class="text-muted">${escapeHtml(metricEvidenceText(data.metric_evidence.status))}。历史数值用于验证关联，不会据此自动抑制原始告警。</p></section>`;
    }
    const roots = data.root_candidates;
    if (roots) {
        const stale = !Number.isFinite(roots.valid_until) || Date.now()/1000 > roots.valid_until;
        html += `<section aria-label="传播起点与根因候选"><h4>传播起点与根因候选</h4>
            <p>排序用于安排调查，不是根因概率；关联证据不能确认因果。</p>
            ${stale ? '<p>证据不可用或已过期，请重新扫描。</p>' : !roots.candidates?.length ? '<p>独立证据不足，暂不推荐根因候选。</p>' : ''}
            ${(stale ? [] : roots.candidates || []).map(c => `<details><summary>候选 ${Number(c.rank)}：${escapeHtml(c.entity_ref)}</summary>
                <p>${c.status === 'possible_downstream_symptom' ? '也存在更下游依赖的证据，可能仍是症状。' : '传播证据检查通过，仍需诊断验证。'}</p>
                <p>${(c.paths || []).map(p => escapeHtml(p.join(' → '))).join('<br>')}</p>
                <p>${(c.next_checks || []).map(escapeHtml).join('<br>')}</p>
                <p>请使用对应事件组的诊断入口继续调查，证据包将携带这些候选。</p>
            </details>`).join('')}</section>`;
    }
    const review = data.merge_review;
    if (review?.status === 'ready' && review.suggestions?.length) {
        const names = Object.fromEntries((data.groups || []).map(g => [g.group_id, `${g.entity}: ${g.representative}`]));
        const expired = !Number.isFinite(review.valid_until) || Date.now() / 1000 > review.valid_until;
        html += `<section aria-label="跨指标归并建议"><h4>跨指标归并建议 · 需人工核对</h4>
            <p>共 ${review.suggestions.length} 对候选${review.suggestions.length > 20 ? '，展示前 20 对' : ''}。建议尚未合并，不计入压缩率；多个建议共享事件组，不表示可以整体合并。</p>
            ${expired ? '<p>证据已过期，请重新扫描。</p>' : ''}
            ${review.suggestions.slice(0,20).map((s, i) => `<details><summary>${escapeHtml(s.group_ids.map(k => names[k] || k).join(' ＋ '))}</summary>
                <p>结构、时间和统计检查通过；${Number(s.alert_count)} 条原始告警。</p>
                <p>${escapeHtml(s.reason || '')}</p><p>${escapeHtml(s.warning || '')}</p>
                <p class="metric-code">指标证据：${escapeHtml((s.metric_evidence_ids || []).join('；'))}</p>
                <button class="btn btn-sm" onclick="selectMergeReview(${i})" ${expired ? 'disabled' : ''}>选择这两个事件组</button>
            </details>`).join('')}</section>`;
    }
    const ranked = (data.groups || []).map((g, gi) => ({g, gi}));
    ranked.sort((a, b) => (data.ranking || []).indexOf(a.g.group_id) - (data.ranking || []).indexOf(b.g.group_id));
    ranked.forEach(({g, gi}) => {
        html += `<div class="alert-group ${g.severity === 'critical' ? 'critical' : ''}">
            <div class="group-title"><input type="checkbox" class="compression-group" value="${gi}" aria-label="选择事件组 ${gi + 1}"> ${escapeHtml(g.representative || '事件组')}</div>
            <p class="text-muted alert-object-identity">对象 UID：${escapeHtml(g.entity_uid || '未采集或涉及多个对象')}。${g.entity_uid ? '来自告警记录，诊断和执行前仍须实时核验。' : '名称相同不能证明是同一资源实例。'}</p>
            <div class="group-meta">${g.alert_count} 条 | ${escapeHtml(g.severity)} | ${escapeHtml(g.namespace)} | ${escapeHtml((g.affected_entities || []).join(', '))}</div>
            ${(g.lifecycle || []).map(s => `<p>${escapeHtml(alertLifecycleStatus(s.status))} · 第 ${Number(s.episode)} 次 · 首次观测 ${escapeHtml(new Date(s.first_observed_at * 1000).toLocaleString())} · 已检出 ${Number(s.observations)} 次</p>`).join('')}
            <p>模板：${escapeHtml(g.symbolic?.template || g.representative || '')}</p>
            ${g.metric_summary ? `<p>指标范围：${escapeHtml(String(g.metric_summary.min))} ～ ${escapeHtml(String(g.metric_summary.max))}，${g.metric_summary.samples} 个样本（峰值保留）</p>` : ''}
            ${g.threshold_merge ? `<p>已归并 ${Number(g.threshold_merge.original_groups)} 组同指标嵌套阈值，最高严重度与全部原文保留。</p><details><summary>查看归并规则依据</summary>${g.threshold_merge.rules.map(r => `<p>${escapeHtml(r.rule.name)}：<code class="metric-code">${escapeHtml(r.rule.expression)}</code></p>`).join('')}</details>` : ''}
            ${g.event_merge?.window ? `<details><summary>请求窗口的跨指标归并依据</summary><p>已用真实 Trace 重算窗口测量值，验证 ${Number(g.event_merge.window.request_count)} 个请求均为慢且失败。表示同一批请求的观测，不是根因确认。</p><p>窗口：${escapeHtml(new Date(g.event_merge.window.start * 1000).toISOString())} — ${escapeHtml(new Date(g.event_merge.window.end * 1000).toISOString())}</p><p>保留 ${Number(g.event_merge.original_groups.length)} 个原分组及全部原文。</p></details>` : g.event_merge?.span ? `<details><summary>同一失败请求的跨指标归并依据</summary><p>指标值与同一真实失败 Span 一致；表示同一请求的观测，不是根因确认。</p><p class="metric-code">Trace：${escapeHtml(g.event_merge.span.trace_id)}<br>Span：${escapeHtml(g.event_merge.span.span_id)}</p><p>保留 ${Number(g.event_merge.original_groups.length)} 个原分组及全部原文。</p></details>` : ''}
            ${renderGroupMetricEvidence(data, g)}
            <p>频率偏移：${['supplied_baseline', 'automatic_snapshot_baseline'].includes(g.frequency?.status) ? escapeHtml(String(g.frequency.deviation_score)) : '缺少历史基线，未判定'}</p>
            <button class="btn btn-sm" onclick="startRCAFromGroup(${gi})">证据包诊断</button>
            <details><summary>展开原始告警 / 选择拆分</summary>
            ${(g.alert_indices || []).map(i => {
                const a = data.raw_alerts[i];
                return `<div class="signal-item"><label><input type="checkbox" class="compression-member-${gi}" value="${i}"> ${escapeHtml(a.name)} | ${escapeHtml(a.severity)} | ${escapeHtml(a.source)} | ${escapeHtml(String(a.timestamp || '时间未知'))}<br>${escapeHtml(a.message)}</label></div>`;
            }).join('')}
            <button class="btn btn-sm" onclick="editAlertGroups('split', ${gi})">拆出选中告警</button>
            </details></div>`;
    });
    if ((data.scag?.edges || []).length) {
        const checkText = {pass: '支持', fail: '不支持', unknown: '证据不足'};
        const names = Object.fromEntries((data.groups || []).map(g => [g.group_id, `${g.entity}: ${g.representative}`]));
        html += '<h4>关联证据检查 · 因果待验证</h4><p>同一时间出现或统计相关不能单独证明根因。</p>';
        html += data.scag.edges.map(r => `<details><summary>${escapeHtml(names[r.left] || r.left)} → ${escapeHtml(names[r.right] || r.right)} · ${escapeHtml(r.type)}</summary>
            <p>结构：${escapeHtml(checkText[r.checks.structure.status])}；时间：${escapeHtml(checkText[r.checks.time.status])}；统计：${escapeHtml(checkText[r.checks.statistics.status])}</p>
            ${r.checks.statistics.reason ? `<p>统计说明：${escapeHtml(metricEvidenceText(r.checks.statistics.reason))}</p>` : ''}
            ${r.checks.statistics.metric_evidence_ids?.some(Boolean) ? `<p class="metric-code">指标证据引用：${escapeHtml(r.checks.statistics.metric_evidence_ids.filter(Boolean).join('；'))}</p>` : ''}
            ${r.checks.context ? `<p>调用 / 变更证据：${escapeHtml(checkText[r.checks.context.status])}，Trace 引用 ${r.checks.context.trace_refs?.length || 0}，变更引用 ${r.checks.context.change_refs?.length || 0}</p>` : ''}
            <p>${escapeHtml(r.reason || '')} · 拓扑来源：${escapeHtml(r.checks.structure.provenance || 'missing')}</p></details>`).join('');
    }
    container.innerHTML = html;
}

function selectMergeReview(index) {
    const scan = _alertCompressionScan;
    const review = scan?.merge_review;
    const suggestion = review?.suggestions?.[index];
    if (!suggestion || review.status !== 'ready' || !Number.isFinite(review.valid_until) || Date.now() / 1000 > review.valid_until) {
        alert('归并建议已失效，请重新扫描。'); return;
    }
    document.querySelectorAll('.compression-group').forEach(el => {
        el.checked = suggestion.group_ids.includes(scan.groups[Number(el.value)]?.group_id);
    });
    document.getElementById('compression-edit-reason')?.focus();
}

async function editAlertGroups(action, index) {
    const scan = _alertCompressionScan;
    if (!scan) return;
    const reason = document.getElementById('compression-edit-reason').value.trim();
    if (!reason) { alert('请填写拆分 / 合并原因'); return; }
    const groupIds = action === 'split' ? [scan.groups[index].group_id] :
        [...document.querySelectorAll('.compression-group:checked')].map(el => scan.groups[Number(el.value)].group_id);
    const alertIds = action === 'split' ? [...document.querySelectorAll(`.compression-member-${index}:checked`)].map(el => scan.raw_alerts[Number(el.value)].alert_id) : [];
    const data = await api(`/api/alerts/compression/${scan.scan_id}/edit`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({revision: scan.revision, action, group_ids: groupIds, alert_ids: alertIds, reason}),
    });
    if (data?.scan_id) renderAlertCompression(data);
}

function startRCAFromGroup(index) {
    const scan = _alertCompressionScan;
    const group = scan?.groups[index];
    if (!group) return;
    switchView('rca');
    document.getElementById('rca-query').value = `${group.representative} (${group.alert_count} 条告警)`;
    document.getElementById('rca-ns').value = group.namespace || '';
    startRCA({scan_id: scan.scan_id, group_id: group.group_id, revision: scan.revision});
}

function toggleDetectionSSE() {
    if (state.detectionSSE) { state.detectionSSE.close(); state.detectionSSE = null; return; }

    const feed = document.getElementById('detection-feed');
    state.detectionSSE = new EventSource('/api/detection/stream');
    state.detectionSSE.onmessage = (e) => {
        try {
            const signal = JSON.parse(e.data);
            const item = document.createElement('div');
            item.className = 'signal-item';
            item.innerHTML = `
                <span><span class="badge badge-${signal.severity === 'critical' ? 'danger' : 'warning'}">${signal.severity || 'info'}</span> ${escapeHtml(signal.description || signal.msg || JSON.stringify(signal).substring(0, 100))}</span>
                <span class="text-muted">${formatTime(signal.timestamp)}</span>
            `;
            feed.prepend(item);
        } catch {}
    };
}

async function clearSignals() {
    await api('/api/detection/signals', { method: 'DELETE' });
    document.getElementById('detection-feed').innerHTML = '';
}

// ─────────────────────────────────────────
// Detection Config Management
// ─────────────────────────────────────────

let _detectionConfig = null;

async function loadDetectionConfig() {
    const data = await api('/api/detection/config');
    if (!data) return;
    _detectionConfig = data;
    renderSourceToggles(data);
    renderCategoryToggles(data);
    renderServiceTags('business-services-tags', data.business_services || [], 'business_services');
    renderServiceTags('db-services-tags', data.db_services || [], 'db_services');
    renderMetricChecksTable(data.metric_checks || []);
    renderCriticalReasons('critical-event-reasons', data.critical_event_reasons || []);
    renderCriticalReasons('critical-pod-reasons', data.critical_pod_reasons || []);
    updateSourceFilterButtons(data.sources_enabled || {});
    const lbEl = document.getElementById('cfg-lookback-m');
    const ztEl = document.getElementById('cfg-z-threshold');
    const esEl = document.getElementById('cfg-ewma-span');
    if (lbEl) lbEl.value = data.default_lookback_m || 30;
    if (ztEl) ztEl.value = data.default_z_threshold || 3.0;
    if (esEl) esEl.value = data.default_ewma_span || 10;
}

function renderSourceToggles(config) {
    const container = document.getElementById('source-toggles');
    const sources = config.sources_enabled || {};
    const labels = { prometheus: 'Prometheus', k8s_event: 'K8s事件', pod_health: 'Pod健康', node_health: '节点健康', metric_anomaly: '指标异常' };
    container.innerHTML = Object.entries(sources).map(([key, enabled]) => `
        <label style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:13px;cursor:pointer">
            <input type="checkbox" data-source="${escapeHtml(key)}" ${enabled ? 'checked' : ''}
                   onchange="toggleSource('${escapeHtml(key)}', this.checked)">
            ${labels[key] || key}
        </label>
    `).join('');
}

function toggleSource(key, enabled) {
    if (_detectionConfig?.sources_enabled) _detectionConfig.sources_enabled[key] = enabled;
}

function renderCategoryToggles(config) {
    const container = document.getElementById('category-toggles');
    if (!container) return;
    const cats = config.categories_enabled || {};
    const labels = { infrastructure: '基础设施', application: '应用', business: '业务工作负载', database: '数据库', k8s_workload: 'K8s工作负载健康' };
    container.innerHTML = Object.entries(cats).map(([key, enabled]) => `
        <label style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:13px;cursor:pointer">
            <input type="checkbox" data-category="${escapeHtml(key)}" ${enabled ? 'checked' : ''}
                   onchange="toggleCategory('${escapeHtml(key)}', this.checked)">
            ${labels[key] || key}
        </label>
    `).join('');
}

function toggleCategory(key, enabled) {
    if (_detectionConfig?.categories_enabled) _detectionConfig.categories_enabled[key] = enabled;
}

function renderServiceTags(containerId, services, configKey) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = services.map((s, i) => `
        <span class="badge badge-info" style="margin:2px;font-size:12px">
            ${escapeHtml(s)}
            <span style="cursor:pointer;margin-left:4px" onclick="removeServiceTag('${containerId}', '${configKey}', ${i})">&times;</span>
        </span>
    `).join('') + `<button class="btn btn-sm" onclick="addServiceTag('${containerId}', '${configKey}')" style="font-size:11px">+ 添加</button>`;
}

function addServiceTag(containerId, configKey) {
    const name = prompt('输入服务名称:');
    if (!name) return;
    if (_detectionConfig) {
        _detectionConfig[configKey] = _detectionConfig[configKey] || [];
        _detectionConfig[configKey].push(name.trim());
        renderServiceTags(containerId, _detectionConfig[configKey], configKey);
    }
}

function removeServiceTag(containerId, configKey, index) {
    if (_detectionConfig?.[configKey]) {
        _detectionConfig[configKey].splice(index, 1);
        renderServiceTags(containerId, _detectionConfig[configKey], configKey);
    }
}

function renderMetricChecksTable(checks) {
    const tbody = document.getElementById('metric-checks-body');
    if (!checks.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center">暂无指标配置</td></tr>';
        return;
    }
    const allMethods = ['threshold', 'zscore', 'ewma', 'spectral_residual', 'pearson_onset', 'rate_change'];
    const defaultMethods = (_detectionConfig?.default_detect_methods) || ['threshold', 'zscore'];
    tbody.innerHTML = checks.map((c, i) => {
        const methods = c.detect_methods || defaultMethods;
        const methodCheckboxes = allMethods.map(m => {
            const checked = methods.includes(m) ? 'checked' : '';
            return `<label style="display:inline-flex;align-items:center;gap:2px;font-size:11px;white-space:nowrap"><input type="checkbox" class="mc-method" data-idx="${i}" data-method="${m}" ${checked}> ${m}</label>`;
        }).join(' ');
        return `<tr>
            <td><input class="input-sm mc-name" value="${escapeHtml(c.name || '')}" style="width:120px" data-idx="${i}"></td>
            <td><select class="select-sm mc-level" data-idx="${i}"><option value="node" ${c.level==='node'?'selected':''}>node</option><option value="container" ${c.level==='container'?'selected':''}>container</option></select></td>
            <td><input type="number" class="input-sm mc-warn" value="${c.warn}" style="width:60px" data-idx="${i}"></td>
            <td><input type="number" class="input-sm mc-crit" value="${c.crit}" style="width:60px" data-idx="${i}"></td>
            <td>${escapeHtml(c.unit || '%')}</td>
            <td style="max-width:280px">${methodCheckboxes}</td>
            <td><button class="btn btn-sm" onclick="editMetricCheck(${i})" title="编辑PromQL">Edit</button> <button class="btn btn-sm btn-danger" onclick="removeMetricCheck(${i})">Del</button></td>
        </tr>`;
    }).join('');
}

function renderCriticalReasons(containerId, reasons) {
    const container = document.getElementById(containerId);
    container.innerHTML = reasons.map((r, i) => `
        <span class="badge badge-danger" style="margin:2px;font-size:12px">${escapeHtml(r)}
            <span style="cursor:pointer;margin-left:4px" onclick="removeCriticalReason('${containerId}', ${i})">&times;</span>
        </span>
    `).join('') + `<button class="btn btn-sm" onclick="addCriticalReason('${containerId}')" style="font-size:11px">+ 添加</button>`;
}

function addCriticalReason(containerId) {
    const reason = prompt('输入原因名称:');
    if (!reason) return;
    const key = containerId === 'critical-event-reasons' ? 'critical_event_reasons' : 'critical_pod_reasons';
    if (_detectionConfig) {
        _detectionConfig[key] = _detectionConfig[key] || [];
        _detectionConfig[key].push(reason.trim());
        renderCriticalReasons(containerId, _detectionConfig[key]);
    }
}

function removeCriticalReason(containerId, index) {
    const key = containerId === 'critical-event-reasons' ? 'critical_event_reasons' : 'critical_pod_reasons';
    if (_detectionConfig?.[key]) { _detectionConfig[key].splice(index, 1); renderCriticalReasons(containerId, _detectionConfig[key]); }
}

function addMetricCheck() {
    const defaultMethods = (_detectionConfig?.default_detect_methods) || ['threshold', 'zscore'];
    const newCheck = { name: 'new_metric', query: '', unit: '%', label_key: 'instance', ns_key: '', level: 'node', warn: 85, crit: 95, detect_methods: [...defaultMethods] };
    if (_detectionConfig) {
        _detectionConfig.metric_checks = _detectionConfig.metric_checks || [];
        _detectionConfig.metric_checks.push(newCheck);
        renderMetricChecksTable(_detectionConfig.metric_checks);
        editMetricCheck(_detectionConfig.metric_checks.length - 1);
    }
}

function editMetricCheck(index) {
    if (!_detectionConfig?.metric_checks) return;
    const check = _detectionConfig.metric_checks[index];
    if (!check) return;
    const query = prompt('PromQL 查询表达式:', check.query || '');
    if (query === null) return;
    check.query = query;
    const labelKey = prompt('标签键 (label_key):', check.label_key || 'instance');
    if (labelKey !== null) check.label_key = labelKey;
    const nsKey = prompt('命名空间键 (ns_key, 可留空):', check.ns_key || '');
    if (nsKey !== null) check.ns_key = nsKey;
    renderMetricChecksTable(_detectionConfig.metric_checks);
}

function removeMetricCheck(index) {
    if (!_detectionConfig?.metric_checks) return;
    _detectionConfig.metric_checks.splice(index, 1);
    renderMetricChecksTable(_detectionConfig.metric_checks);
}

function _collectMetricChecksFromUI() {
    if (!_detectionConfig?.metric_checks) return;
    const checks = _detectionConfig.metric_checks;
    document.querySelectorAll('.mc-name').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].name = el.value; });
    document.querySelectorAll('.mc-level').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].level = el.value; });
    document.querySelectorAll('.mc-warn').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].warn = parseFloat(el.value) || 0; });
    document.querySelectorAll('.mc-crit').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].crit = parseFloat(el.value) || 0; });
    const methodsByIdx = {};
    document.querySelectorAll('.mc-method').forEach(el => { const idx = parseInt(el.dataset.idx); if (!methodsByIdx[idx]) methodsByIdx[idx] = []; if (el.checked) methodsByIdx[idx].push(el.dataset.method); });
    for (const [idx, methods] of Object.entries(methodsByIdx)) { const i = parseInt(idx); if (checks[i]) checks[i].detect_methods = methods; }
}

async function saveDetectionConfig() {
    if (!_detectionConfig) return;
    _collectMetricChecksFromUI();
    const lbEl = document.getElementById('cfg-lookback-m');
    const ztEl = document.getElementById('cfg-z-threshold');
    const esEl = document.getElementById('cfg-ewma-span');
    const payload = {
        sources_enabled: _detectionConfig.sources_enabled,
        metric_checks: _detectionConfig.metric_checks,
        critical_event_reasons: _detectionConfig.critical_event_reasons,
        critical_pod_reasons: _detectionConfig.critical_pod_reasons,
        default_lookback_m: lbEl ? parseInt(lbEl.value) || 30 : 30,
        default_z_threshold: ztEl ? parseFloat(ztEl.value) || 3.0 : 3.0,
        default_ewma_span: esEl ? parseInt(esEl.value) || 10 : 10,
        categories_enabled: _detectionConfig.categories_enabled || {},
        business_services: _detectionConfig.business_services || [],
        db_services: _detectionConfig.db_services || [],
    };
    const statusEl = document.getElementById('detection-save-status');
    statusEl.textContent = '保存中...';
    statusEl.style.color = 'var(--accent)';
    const result = await api('/api/detection/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (result?.status === 'ok') {
        statusEl.innerHTML = '<span style="color:var(--success);font-weight:600">&#10003; 配置已保存并生效</span>';
        showToast('检测配置已保存，指标图表将使用新的异常检测参数');
        updateSourceFilterButtons(_detectionConfig.sources_enabled);
        loadAlertList();
    } else {
        statusEl.innerHTML = '<span style="color:var(--danger);font-weight:600">&#10007; 保存失败</span>';
        showToast('保存失败，请检查参数', 'error');
    }
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
}

function showToast(msg, type) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;top:70px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    const bg = type === 'error' ? 'var(--danger)' : 'var(--accent)';
    toast.style.cssText = `padding:12px 20px;border-radius:8px;background:${bg};color:#fff;font-size:13px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,0.15);opacity:0;transform:translateX(20px);transition:all 0.3s ease;max-width:360px;`;
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; toast.style.transform = 'translateX(0)'; });
    setTimeout(() => {
        toast.style.opacity = '0'; toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function updateSourceFilterButtons(sourcesEnabled) {
    const filterContainer = document.getElementById('alert-source-filters');
    if (!filterContainer) return;
    filterContainer.querySelectorAll('.alert-source-btn[data-source]').forEach(btn => {
        const src = btn.dataset.source;
        if (src === 'all') return;
        btn.style.display = (sourcesEnabled[src] !== false) ? '' : 'none';
    });
}

function startRCAFromAlert(index) {
    const a = _filteredAlerts[index];
    if (!a) return;
    const query = `[${(a.severity || 'warning').toUpperCase()}] ${a.title || ''} — ${a.description || ''} (service=${a.service || ''}, namespace=${a.namespace || ''})`;
    _activeRCAAlertKey = alertDiagnosisKey(a);
    switchView('rca');
    document.getElementById('rca-query').value = query;
    document.getElementById('rca-ns').value = a.namespace || '';
    startRCA();
}

// ─────────────────────────────────────────
// RCA
// ─────────────────────────────────────────

async function startRCA(alertGroup = null) {
    state.rcaHistoryGeneration = (state.rcaHistoryGeneration || 0) + 1;
    if (state.rcaEventSource) state.rcaEventSource.close();
    const query = document.getElementById('rca-query').value.trim();
    const ns = document.getElementById('rca-ns').value.trim();
    if (!query) { alert('请描述故障现象'); return; }

    const data = await api('/api/rca/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, namespace: ns, ...(alertGroup ? {alert_group: alertGroup} : {}) }),
    });
    if (!data?.run_id) { alert('启动失败'); return; }

    state.rcaRunId = data.run_id;
    const progress = document.getElementById('rca-progress');
    progress.style.display = 'block';
    document.getElementById('rca-result-card').style.display = 'none';
    state._pendingCollaboration = null;
    state._pendingCritique = null;
    state._pendingClosure = null;

    const logEl = document.getElementById('rca-log');
    logEl.textContent = '';
    document.getElementById('rca-history-retrieval')?.remove();
    document.getElementById('rca-phases').innerHTML = '';
    document.getElementById('rca-hyp-list').innerHTML = '';
    document.getElementById('rca-hypotheses').style.display = 'none';
    document.getElementById('rca-strategy-live')?.remove();
    document.getElementById('rca-hypothesis-review-live')?.remove();
    document.getElementById('rca-evidence-grid').innerHTML = '';
    document.getElementById('rca-evidence').style.display = 'none';
    const skillRuntimePanel = document.getElementById('rca-skill-runtime');
    const skillRuntimeList = document.getElementById('rca-skill-runtime-list');
    if (skillRuntimePanel) skillRuntimePanel.style.display = 'none';
    if (skillRuntimeList) skillRuntimeList.innerHTML = '';
    document.getElementById('rca-iteration').style.display = 'none';
    document.getElementById('rca-result-content').innerHTML = '';

    const sse = new EventSource(`/api/rca/${data.run_id}/stream`);
    state.rcaEventSource = sse;
    sse.onmessage = (e) => {
        if (state.rcaRunId !== data.run_id) { sse.close(); return; }
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'log') { logEl.textContent += zhLogLine(msg.msg) + '\n'; logEl.scrollTop = logEl.scrollHeight; }
            else if (msg.type === 'event') { handleRCAEvent(msg.data); }
            else if (msg.type === 'done') { sse.close(); renderRCAFinalResult(msg.result); loadRCAHistory({reset:true}); }
        } catch {}
    };
    sse.onerror = () => { sse.close(); };
}

function handleRCAEvent(evt) {
    switch (evt.event) {
        case 'history_retrieval': renderHistoryRetrieval(evt.data); break;
        case 'phase_start': updatePhase(evt.phase, evt.name, 'active'); break;
        case 'phase_complete': updatePhase(evt.phase, evt.name, evt.status === 'degraded' ? 'degraded' : 'done'); break;
        case 'hypotheses': renderHypotheses(evt.items); break;
        case 'hypothesis_generation_record': renderLiveStrategy(evt); break;
        case 'diagnostic_strategy': renderLiveStrategy(evt); break;
        case 'hypothesis_independent_review': renderLiveHypothesisReview(evt); break;
        case 'evidence': addEvidenceCard(evt.agent, evt.summary, evt.success); break;
        case 'skill_runtime': addRCASkillRuntimeEvent(evt); break;
        case 'iteration': updateIteration(evt.current, evt.total); break;
        case 'collaboration_policy': renderCollaborationPolicy(evt.data); break;
        case 'collaboration_critique': renderCollaborationCritique(evt.data); break;
        case 'diagnosis_closure': renderDiagnosisClosure(evt.data); break;
        case 'judge': renderJudge(evt.data); break;
        case 'remediation': renderRemediation(evt.data); break;
        case 'remediation_executed': renderRemediationResult(evt.data); break;
    }
}

function historyRetrievalHtml(meta) {
    const labels = {complete:'检索完成', partial:'部分可用', timed_out:'检索超时', busy:'检索繁忙', unavailable:'检索不可用', blocked_scope:'集群或命名空间缺失、冲突，已阻止历史召回', not_started:'未开始', running:'进行中', disabled:'历史检索已禁用', not_requested:'本次未请求历史检索', not_recorded:'未保存检索状态'};
    const status = meta?.status || 'not_recorded';
    const components = Object.entries(meta?.components || {}).map(([name, item]) =>
        `${name === 'rules' ? '规则' : '相似故障'}：${labels[item.status] || item.status}${item.count !== undefined ? ` (${item.count})` : ''}`).join('；');
    const scope = meta?.scope;
    const scopeText = scope ? `<p>适用范围：${escapeHtml(scope.cluster_id || '未指定集群')} / ${escapeHtml(scope.namespace || '未指定命名空间')}；按本次检索时点检查有效性。</p>` : '';
    return `<strong>历史知识检索 · ${escapeHtml(labels[status] || status)}</strong><p>${escapeHtml(components)}</p>${scopeText}<small>历史仅用于生成候选假设，根因需当前证据核验。${status === 'not_recorded' ? '此记录未保存检索状态，无法倒推。' : ''}</small>`;
}

function renderHistoryRetrieval(meta) {
    let panel = document.getElementById('rca-history-retrieval');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'rca-history-retrieval';
        panel.className = 'card';
        document.getElementById('rca-phases')?.insertAdjacentElement('afterend', panel);
    }
    panel.innerHTML = historyRetrievalHtml(meta);
    panel.dataset.status = meta?.status || 'not_recorded';
}

const PHASE_NAMES = {
    0:'告警压缩',
    1:'上下文检索',
    2:'假设生成',
    3:'证据调查',
    4:'交叉关联',
    5:'图分析',
    6:'报告生成',
    7:'质量评估',
    8:'自动学习',
    9:'自愈修复',
    10:'协作反思',
    11:'诊断闭环',
    agent_skill_context_retrieval:'上下文检索',
    agent_skill_context:'上下文解析',
    agent_skill_hypothesis:'假设生成',
    agent_skill_plan:'验证规划',
    agent_skill_investigation:'调查取证',
    agent_skill_reasoning:'推理裁决',
    agent_evidence:'调查取证',
    skill_runtime:'Skill 工具调用',
    AGENT_SKILL_TOOL_CALL:'智能体调用 Skill',
};

function phaseDisplayName(event = {}) {
    const label = PHASE_NAMES[event.phase] || PHASE_NAMES[event.name] || event.name || event.phase || '-';
    return event.status === 'degraded' ? `${label} · 未完成` : label;
}

function phaseEventClass(event) {
    return event.status === 'degraded' ? 'text-warning' : (event.event === 'phase_complete' ? 'done' : 'active');
}

function updatePhase(num, name, status) {
    const container = document.getElementById('rca-phases');
    let badge = document.getElementById(`rca-phase-${num}`);
    if (!badge) { badge = document.createElement('span'); badge.id = `rca-phase-${num}`; container.appendChild(badge); }
    badge.textContent = phaseDisplayName({phase:num, name, status});
    badge.dataset.status = status;
    badge.className = `phase-badge ${status === 'degraded' ? 'text-warning' : status}`;
}

function updateIteration(current, total) {
    const el = document.getElementById('rca-iteration');
    el.style.display = 'block';
    el.innerHTML = `<span class="iteration-badge">迭代 ${current} / ${total}</span>`;
}

function renderHypotheses(items) {
    document.getElementById('rca-hypotheses').style.display = 'block';
    document.getElementById('rca-hyp-list').innerHTML = items.map((h, i) => {
        const pct = Math.round(h.confidence * 100);
        return `<div class="hyp-item"><span class="hyp-rank">#${i+1}</span><div class="hyp-bar-wrap"><div class="hyp-bar" style="width:${pct}%"></div></div><span class="hyp-conf">${pct}%</span><span class="hyp-desc" title="${escapeZh(h.description)}">${escapeZh(h.description)}</span></div>`;
    }).join('');
}

const STRATEGY_REASONS = {
    model_service_unavailable:'模型服务不可用，保留观测并请求复核',
    cited_hypothesis_ready_for_independent_review:'候选已引用观测，转入独立审核',
    model_budget_exhausted:'模型时间预算已用尽', iteration_limit_reached:'已达调查轮数上限',
    no_new_information_or_untried_sources:'未获得新增差异观测，且无未尝试的信息源',
    switch_information_source:'结合缺口切换调查信息源', replan_with_evidence_gaps:'根据证据缺口重新规划',
    resolve_investigation_gaps:'仍有待判别问题，继续补证',
    unresolved_gaps_no_eligible_sources:'补证缺口仍在，当前没有可用的后续来源',
    pipeline_failed:'诊断中断或失败，保留已记录过程', record_not_available:'旧记录未保存该格式过程',
};

function renderLiveStrategy(event) {
    const parent=document.getElementById('rca-hypotheses');
    if (!parent) return;
    parent.style.display='block';
    let el=document.getElementById('rca-strategy-live');
    if (!el) { el=document.createElement('div');el.id='rca-strategy-live';parent.appendChild(el); }
    if (event.event==='hypothesis_generation_record') {
        el.innerHTML='<p>候选已记录；初始排序和历史经验不是当前事故的证实结论。</p>';
    } else {
        const row=document.createElement('p');
        row.textContent=`第 ${event.iteration} 轮：${STRATEGY_REASONS[event.decision?.reason]||event.decision?.reason||''}；新增差异观测 ${event.novelty?.novel_observation_ids?.length||0} 项。`;
        el.appendChild(row);
    }
}

function renderLiveHypothesisReview(event) {
    const parent=document.getElementById('rca-hypotheses');if(!parent)return;
    parent.style.display='block';
    let el=document.getElementById('rca-hypothesis-review-live');
    if(!el){el=document.createElement('div');el.id='rca-hypothesis-review-live';parent.appendChild(el);}
    const row=document.createElement('p');
    row.textContent=`独立角色复核：${event.validated_count||0}/${event.candidate_count||0} 项通过格式与引用校验；${event.blockers?.length?'存在未通过复核的确认或排除判断，将补证或转人工。':'最终处置仍受完整诊断门禁约束。'}`;
    el.appendChild(row);
}

function addEvidenceCard(agent, summary, success) {
    document.getElementById('rca-evidence').style.display = 'block';
    const grid = document.getElementById('rca-evidence-grid');
    const card = document.createElement('div');
    card.className = `evidence-card ${success ? 'success' : 'error'}`;
    card.innerHTML = `<div class="ev-agent">${success ? '&#10003;' : '&#9888;'} ${escapeHtml(rcaAgentLabel(agent))}</div><div class="ev-summary">${escapeZh(summary || (success ? '分析完成' : '分析失败'))}</div>`;
    grid.appendChild(card);
}

function addRCASkillRuntimeEvent(evt = {}) {
    const panel = document.getElementById('rca-skill-runtime');
    const list = document.getElementById('rca-skill-runtime-list');
    if (!panel || !list) return;
    panel.style.display = 'block';
    const card = document.createElement('div');
    card.className = 'evidence-card success rca-skill-runtime-card';
    const result = evt.result || {};
    const runtime = result.skill_runtime || {};
    const diagnosis = result.diagnosis || {};
    const plan = result.execution_plan || {};
    card.innerHTML = `<div class="ev-agent">Skill 调用 · ${escapeHtml(evt.primary_skill_id || runtime.primary_skill_id || '-')}</div>
        <div class="ev-summary">${escapeZh(evt.summary || diagnosis.root_cause || 'Agent 已调用 Skill 工具')}</div>
        <div class="skill-status-strip compact">
            <div><span>命中 Skill</span><strong>${escapeHtml(evt.matched_count ?? runtime.matched_count ?? 0)}</strong></div>
            <div><span>置信度</span><strong>${escapeHtml(formatConfidence(evt.confidence ?? diagnosis.confidence))}</strong></div>
            <div><span>自愈动作</span><strong>${escapeHtml(evt.command_count ?? plan.command_count ?? 0)}</strong></div>
        </div>
        ${renderAlertSkillExecutionProcess(result, ['detect', 'diagnose', 'heal', 'verify'], { open: true, showMissing: true })}`;
    list.prepend(card);
}

function renderJudge(data) {
    if (!data) return;
    const level = String(data.judge_level ?? '').trim().toLowerCase();
    const cls = ['0', 'gold'].includes(level) ? 'gold' : ['1', 'silver'].includes(level) ? 'silver' : 'bronze';
    const label = rcaQualityLabel(level);
    const rawScore = data.combined_score ?? data.score;
    const score = typeof rawScore === 'number' && Number.isFinite(rawScore) && rawScore >= 0 && rawScore <= 1
        ? rawScore.toFixed(3) : '未知';
    const judgeEl = document.createElement('div');
    judgeEl.id = 'rca-judge-info';
    judgeEl.style.marginTop = '12px';
    judgeEl.innerHTML = `<span class="judge-badge ${cls}">${escapeHtml(label)} · 评分 ${score}</span>${data.needs_review ? '<span class="badge badge-warning" style="margin-left:8px">需要人工复核</span>' : ''}`;
    state._pendingJudge = judgeEl;
}

function renderCollaborationPolicy(data) {
    if (!data) return;
    const el = document.createElement('div');
    el.className = 'rca-collab-info';
    const focus = (data.evidence_focus || []).slice(0, 5).map(x => `<span>${escapeZh(x)}</span>`).join('');
    el.innerHTML = `
        <div class="collab-title">协作策略：${escapeZh(data.strategy || 'plan_and_execute')}</div>
        <div class="collab-rationale">${escapeZh(data.rationale || '')}</div>
        ${focus ? `<div class="collab-focus">${focus}</div>` : ''}
    `;
    state._pendingCollaboration = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingCollaboration = null; }
}

function renderCollaborationCritique(data) {
    if (!data) return;
    const el = document.createElement('div');
    el.className = 'rca-collab-info critique';
    const score = Math.round((data.quality_score || 0) * 100);
    const weaknesses = (data.weaknesses || []).slice(0, 3).map(x => `<li>${escapeZh(x)}</li>`).join('');
    el.innerHTML = `
        <div class="collab-title">反思质量门：${score}% ${data.needs_human_review ? '<span class="badge badge-warning">建议人工复核</span>' : ''}</div>
        ${weaknesses ? `<ul>${weaknesses}</ul>` : '<div class="collab-rationale">未发现明显协作质量问题</div>'}
    `;
    state._pendingCritique = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingCritique = null; }
}

function renderDiagnosisClosure(data) {
    if (!data) return;
    const plan = data.plan || {};
    const el = document.createElement('div');
    el.className = 'rca-collab-info closure';
    const modes = (plan.failure_modes || []).map(x => `<span>${escapeZh(x)}</span>`).join('');
    const agents = (plan.target_agents || data.evidence_agents || []).map(x => `<span>${escapeHtml(rcaAgentLabel(x))}</span>`).join('');
    const score = data.judge_after?.combined_score;
    el.innerHTML = `
        <div class="collab-title">诊断闭环：${escapeHtml(statusLabel(data.status || (plan.should_iterate ? 'started' : 'skipped')))}${score != null ? ` · 复评 ${Number(score).toFixed(3)}` : ''}</div>
        <div class="collab-rationale">${escapeZh(plan.reason || data.reason || '')}</div>
        ${modes ? `<div class="collab-focus">${modes}</div>` : ''}
        ${agents ? `<div class="collab-focus">${agents}</div>` : ''}
    `;
    state._pendingClosure = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingClosure = null; }
}

function renderRCAFinalResult(result) {
    const card = document.getElementById('rca-result-card');
    const content = document.getElementById('rca-result-content');
    card.style.display = 'block';

    if (!result) { content.innerHTML = '<p class="text-danger">未获取到结果</p>'; return; }

    const status = result.status || 'unknown';
    const inner = result.result || result;
    const contextMeta = result.history_retrieval || inner.history_retrieval
        || result.agent_evidence?.agent_workflow?.context?.history_retrieval
        || inner.agent_evidence?.agent_workflow?.context?.history_retrieval;
    if (contextMeta?.status || !document.getElementById('rca-history-retrieval')) renderHistoryRetrieval(contextMeta);
    const rca = (inner.result && typeof inner.result === 'object' && !Array.isArray(inner.result)) ? inner.result : inner;
    const rootCause = zhText(rca.root_cause || rca.error || 'N/A');
    const conf = rca.confidence || 0;
    const confPct = Math.round(conf * 100);
    const confClass = conf >= 0.7 ? 'high' : conf >= 0.4 ? 'medium' : 'low';
    const needsReview = rca.diagnosis_status === 'requires_review' || rca.diagnosis_status === 'pending_review'
        || rca.automation_gate?.allowed === false || inner.judge?.needs_review === true;

    let html = `<div class="rca-result-structured">`;
    html += `<div class="result-banner ${status === 'completed' && !needsReview ? 'success' : 'failed'}"><h4>${status === 'completed' ? (needsReview ? '分析完成 · 待人工复核' : '&#10003; 根因分析完成') : '&#10007; 分析失败'}</h4></div>`;
    html += `<div class="rca-root-cause">${escapeHtml(rootCause)}</div>`;
    if (needsReview) html += '<p class="text-muted">诊断尚有未满足的通用准入条件；实际动作和恢复状态以对象策略与下方记录为准。</p>';
    if (rca.model_response_validation?.failed_calls?.length || rca.model_response_validation?.failed_stages?.length) {
        html += '<div class="rca-remediation rca-model-failure" role="status"><strong>模型调用或报告生成失败</strong><p>本次模型执行未完成，现有观测与候选仅供人工复核，自动动作已阻断。</p></div>';
    }
    if (rca.citation_validation) {
        const citation = rca.citation_validation;
        html += `<div class="rca-remediation"><strong>原始告警引用：</strong>${citation.status === 'passed' ? '逐字引用已核对' : '引用待核对'} · ${escapeHtml((citation.cited || []).join(', '))}</div>`;
        if (citation.unresolved?.length) html += `<p class="text-danger">未核实引用：${escapeHtml(citation.unresolved.join(', '))}</p>`;
    }
    if (rca.alert_evidence_coverage) {
        const coverage = rca.alert_evidence_coverage;
        html += `<div class="rca-remediation"><strong>告警证据覆盖：</strong>已读取 ${escapeHtml(coverage.loaded_alerts)} / ${escapeHtml(coverage.total_alerts)} 条${Number.isInteger(coverage.entity_count) ? `，涉及 ${escapeHtml(coverage.entity_count)} 个对象` : ''}，最终推理目录包含 ${escapeHtml(coverage.prompt_alerts)} 条，其中 ${escapeHtml(coverage.prompt_excerpted_messages)} 条为节选。未展示原文仍可按原扫描版本回查。</div>`;
        const url = coverage.evidence_url;
        if (typeof url === 'string' && /^\/api\/alerts\/compression\/scan-[0-9a-f]{32}\/groups\/g-[0-9a-f]{24}\/evidence\?revision=[1-9][0-9]*$/.test(url)) {
            html += `<p><a href="${escapeHtml(url)}" target="_blank" rel="noopener">读取同版本原始告警（分页）</a></p>`;
        }
    }
    if (Array.isArray(rca.evidence_references) && rca.evidence_references.length) {
        html += `<details class="skill-raw-fold"><summary>原始观测 ${rca.evidence_references.length} 项</summary><ul>${rca.evidence_references.map(item => `<li><strong>${escapeHtml(item.id)}</strong> · ${escapeHtml(item.namespace || '')}/${escapeHtml(item.service || '')}<pre>${escapeHtml(item.observation)}</pre></li>`).join('')}</ul></details>`;
    }
    if (rca.unverified_analysis) html += `<details class="skill-raw-fold"><summary>模型分析草稿（未经确认，仅供复核）</summary><pre>${escapeHtml(JSON.stringify(rca.unverified_analysis, null, 2))}</pre></details>`;
    html += '<div id="rca-ontology-panel" class="rca-remediation" aria-live="polite">正在读取本次调查的对象与证据…</div>';
    html += '<div id="rca-strategy-panel" class="rca-remediation" aria-live="polite">正在读取假设与调查调整记录…</div>';
    html += '<div id="rca-auto-recovery-panel" class="rca-remediation" aria-live="polite">正在读取自动恢复记录…</div>';
    html += `<div class="rca-conf-row"><span style="font-size:12px;color:var(--text-muted)">置信度</span><div class="rca-conf-bar"><div class="rca-conf-fill ${confClass}" style="width:${confPct}%"></div></div><span class="rca-conf-label">${confPct}%</span></div>`;
    html += renderConfidenceExplanation(rca.confidence_explanation);

    html += `<div class="rca-meta-grid">`;
    if (rca.fault_type) html += `<div class="rca-meta-item"><div class="meta-label">故障类型</div><div class="meta-value">${escapeHtml(faultTypeLabel(rca.fault_type))}</div></div>`;
    if (rca.affected_services?.length) html += `<div class="rca-meta-item"><div class="meta-label">受影响服务</div><div class="meta-value">${rca.affected_services.map(s => escapeHtml(s)).join(', ')}</div></div>`;
    if (rca.evidence_summary) { for (const [key, val] of Object.entries(rca.evidence_summary)) { if (val) html += `<div class="rca-meta-item"><div class="meta-label">${escapeHtml(rcaAgentLabel(key))}</div><div class="meta-value">${escapeZh(String(val).substring(0, 200))}</div></div>`; } }
    html += `</div>`;

    if (Array.isArray(rca.hypotheses) && rca.hypotheses.length) {
        const hg = rca.hypothesis_generation || {};
        html += renderRCAHypothesisGenerationSummary(rca.hypotheses, hg);
    }

    if (rca.timeline?.length) {
        html += `<div><h4 style="font-size:13px;color:var(--text-secondary);margin-bottom:8px">事件时间线</h4><div class="rca-timeline">`;
        rca.timeline.forEach(t => { html += `<div class="rca-timeline-item"><div class="tl-time">${escapeHtml(t.time || '')}</div><div class="tl-event">${escapeZh(t.event || '')}</div></div>`; });
        html += `</div></div>`;
    }

    if (rca.remediation_suggestion) html += `<div class="rca-remediation"><strong>修复建议：</strong>${escapeZh(rca.remediation_suggestion)}</div>`;
    if (rca.prevention) html += `<div class="rca-remediation" style="margin-top:8px"><strong>预防措施：</strong>${escapeZh(rca.prevention)}</div>`;

    html += `<div class="rca-heal-toolbar">
        <button class="btn btn-sm btn-primary" onclick="suggestHealFromRCA()">生成自愈能力</button>
        <span class="text-muted">基于当前诊断结论生成可执行建议、风险门禁和 dry-run 预演。</span>
    </div>
    <div id="rca-heal-capability"></div>`;

    html += `</div>`;
    content.innerHTML = html;
    state._lastRCAResult = rca;
    state._lastRCARunId = state.rcaRunId;
    loadRCAOntology(state.rcaRunId);
    loadRCAStrategy(state.rcaRunId);
    loadRCAAutoRecovery(state.rcaRunId);
    if (_activeRCAAlertKey && _alertAgentRunCache.get(_activeRCAAlertKey)?.run_id === state.rcaRunId) {
        _alertDiagnosisCache.set(_activeRCAAlertKey, rca);
        showToast('诊断结果已关联到该告警，自愈将优先使用诊断结论');
    }

    if (state._pendingJudge) { content.appendChild(state._pendingJudge); state._pendingJudge = null; }
    if (state._pendingCollaboration) { content.appendChild(state._pendingCollaboration); state._pendingCollaboration = null; }
    if (state._pendingCritique) { content.appendChild(state._pendingCritique); state._pendingCritique = null; }
    if (state._pendingClosure) { content.appendChild(state._pendingClosure); state._pendingClosure = null; }
    if (state._pendingRemediation) { content.appendChild(state._pendingRemediation); state._pendingRemediation = null; }
}

const ONTOLOGY_GAP_LABELS = {
    legacy_evidence_may_be_excerpted: '旧记录的证据可能为节选',
    no_structured_observations: '没有可绑定对象的结构化观测',
    unresolved_observation_identity: '部分观测缺少明确对象身份',
    object_uid_or_scope_missing: '部分对象缺少 UID 或完整范围',
    observation_time_missing: '部分观测缺少时间',
    collection_incomplete: '存在失败或未确认的采集',
    cross_scope_observations_retained_for_audit: '保留了范围外观测，供审计核查',
    investigation_not_completed: '调查尚未完成',
    target_incarnation_unresolved: '根因对象的唯一 UID 尚未确定',
    target_resource_version_missing: '缺少目标资源版本',
    independent_review_required: '需要独立审核',
    diagnosis_gate_not_allowed: '诊断执行门禁尚未通过',
    review_citation_unresolved: '部分审核引用尚未匹配原始观测',
};

const AUTO_RECOVERY_REASON_LABELS = {
    auto_gate_rejected: '现有自动执行门禁未通过', insufficient_history: '同动作、环境和验证标准的历史样本不足',
    history_success_lower_bound_low: '历史恢复成功率置信下界不足', verification_contract_missing: '缺少可核验的恢复标准',
    confidence_below_auto_threshold: '诊断置信度未达到自动执行要求', recipe_not_marked_safe: '恢复规则尚未标记为可自动执行',
    ontology_action_not_ready: '诊断对象或独立审核条件未满足', service_binding_incomplete_or_ambiguous: '服务与工作负载关联不完整或存在歧义',
    explicit_scale_policy_required: '未配置唯一的目标和期望副本数', unique_scoped_recipe_action_required: '没有唯一且范围匹配的规则动作',
    live_service_binding_changed: '实时服务身份、版本或选择器发生变化', live_deployment_not_scaled_to_zero: '实时 Deployment 并非零副本',
    service_binding_or_diagnosis_changed_after_preview: '预演后配置、服务或诊断时间条件发生变化',
    dispatch_or_verification_error: '动作或恢复验证结果不明，需要核对', interrupted_attempt_requires_reconciliation: '进程中断，需核对现场后再决定下一步',
};

function renderRCAAutoRecovery(record) {
    const status = record?.status;
    const verified = status === 'succeeded' && record?.success === true
        && record?.verification?.status === 'verified' && record?.verification?.recovered === true;
    const labels = {blocked: '已阻断', preparing: '准备中或已中断，需核对', dispatching: '提交中或结果未确认，需核对',
        outcome_unknown: '结果未知，需核对', failed: '恢复未通过', verification_pending: '动作已执行，恢复待验证',
        succeeded: verified ? '恢复验证通过' : '恢复状态待核对'};
    const blockers = Array.isArray(record?.blockers) ? record.blockers.filter(x => typeof x === 'string') : [];
    const reason = typeof record?.reason === 'string' ? record.reason : '';
    const recoveryId = typeof record?.heal_run_id === 'string' ? record.heal_run_id : '';
    return `<strong>自动恢复记录</strong><p>${escapeHtml(labels[status] || '状态未知，需核对')}</p>`
        + (reason ? `<p>原因：${escapeHtml(AUTO_RECOVERY_REASON_LABELS[reason] || reason)}</p>` : '')
        + (blockers.length ? `<p>阻断项：${blockers.map(x => escapeHtml(AUTO_RECOVERY_REASON_LABELS[x] || x)).join('；')}</p>` : '')
        + (recoveryId ? `<p>自愈记录：${escapeHtml(recoveryId)}</p>` : '')
        + '<p class="text-muted">本视图只读取记录；业务恢复以该次验证结果为准。</p>';
}

async function loadRCAAutoRecovery(runId) {
    const token = (state._autoRecoveryRequest || 0) + 1;
    state._autoRecoveryRequest = token;
    if (!runId) return;
    let record = null, missing = false;
    try {
        record = await api(`/api/rca/${encodeURIComponent(runId)}/auto-heal`,
            {throwOnError: true, suppressErrorLog: true, cache: 'no-store'});
    } catch (err) { missing = err?.status === 404; }
    if (state.rcaRunId !== runId || state._autoRecoveryRequest !== token) return;
    const panel = document.getElementById('rca-auto-recovery-panel');
    if (!panel) return;
    panel.innerHTML = (missing ? '<strong>自动恢复记录</strong><p>本次调查尚无自动恢复记录。</p>'
        : record ? renderRCAAutoRecovery(record)
        : '<strong>自动恢复记录</strong><p>记录暂时无法读取，请稍后重试。</p>')
        + '<button class="btn btn-sm" onclick="loadRCAAutoRecovery(state.rcaRunId)">刷新记录</button>';
}

async function loadRCAOntology(runId) {
    const token = (state._ontologyRequest || 0) + 1;
    state._ontologyRequest = token;
    state._lastOntology = null;
    state._ontologyHistory = null;
    state._ontologyHistoryRequest = (state._ontologyHistoryRequest || 0) + 1;
    if (!runId) return;
    const model = await api(`/api/rca/${encodeURIComponent(runId)}/ontology`);
    if (state.rcaRunId !== runId || state._ontologyRequest !== token) return;
    const panel = document.getElementById('rca-ontology-panel');
    if (!panel) return;
    if (!model?.revision) { panel.textContent = '本次调查的对象证据暂时无法读取，请重新打开记录。'; return; }
    state._lastOntology = model;
    const counts = model.counts || {};
    const action = model.action_context || {};
    panel.innerHTML = `<strong>运维对象与证据</strong>
        <p class="text-muted">本次调查记录 · 关系表示观测关联，不能单独证明因果；历史记录不代表当前集群状态。</p>
        <p>${Number(counts.objects || 0)} 个对象 · ${Number(counts.observations || 0)} 项观测 · ${Number(counts.relations || 0)} 条关系 · ${Number(counts.tool_executions || 0)} 次工具执行</p>
        ${(model.gaps || []).length ? `<p class="text-warning">数据缺口：${model.gaps.map(g => escapeHtml(ONTOLOGY_GAP_LABELS[g] || g)).join('；')}</p>` : ''}
        <details class="skill-raw-fold"><summary>对象动作前置条件</summary>
            <p>${action.recorded_conditions_met ? '记录条件满足；执行前仍须核对实时对象、命令范围及原有权限与风险门禁。' : (action.blockers || []).map(g => escapeHtml(ONTOLOGY_GAP_LABELS[g] || g)).join('；')}</p>
            <p>当前视图未授权或执行任何变更。</p></details>
        ${(model.review_evidence_links || []).length ? `<details class="skill-raw-fold"><summary>审核与原始观测的引用关联</summary>
            ${model.review_evidence_links.map(link => `<p>${escapeHtml(link.review_id || '')}${link.cited_by_reviewer ? ' · 审核引用' : ''}：${link.status === 'resolved' ? link.observation_ids.map(id => `<button class="btn btn-sm" onclick="loadOntologyCollection('observations','',0,'${escapeHtmlAttr(id)}')">${escapeHtml(id)}</button>`).join(' ') : '未匹配，需复核'}</p>`).join('')}</details>` : ''}
        <div class="rca-heal-toolbar">
            <button class="btn btn-sm" onclick="loadOntologyCollection('objects')">对象</button>
            <button class="btn btn-sm" onclick="loadOntologyCollection('observations')">观测</button>
            <button class="btn btn-sm" onclick="loadOntologyCollection('relations')">关系</button>
            <button class="btn btn-sm" onclick="loadOntologyCollection('tool_executions')">工具执行</button>
        </div><div id="rca-ontology-items"></div><div id="rca-object-history-panel" aria-live="polite"></div><div id="rca-lineage-panel"></div>`;
    loadOntologyCollection('objects');
    loadRCALineage(model);
}

async function loadRCAStrategy(runId) {
    const token=(state._strategyRequest||0)+1;state._strategyRequest=token;
    const data=await api(`/api/rca/${encodeURIComponent(runId)}/strategy`);
    if (state.rcaRunId!==runId||state._strategyRequest!==token) return;
    const panel=document.getElementById('rca-strategy-panel');if(!panel)return;
    if (!data?.revision) {panel.textContent='调查调整记录暂时无法读取，请重新打开记录。';return;}
    panel.innerHTML=renderStrategyAudit(data);
}

function renderStrategyAudit(data) {
    if (data.recording_status!=='recorded') return '<strong>假设产生与调查调整</strong><p>此旧记录未保存新格式的假设演变过程，不能从最终结论倒推。</p>';
    const source={deterministic_seed:'症状规则产生的候选',llm:'模型生成',llm_with_history:'模型结合历史生成',
        evidence_grounded_branch:'新观测触发的分支',generation_failed:'生成失败后的待调查占位',unspecified:'来源未记录'};
    const generation=data.generation||{};
    const refs=ids=>(ids||[]).map(id=>`<button class="btn btn-sm" onclick="openStrategyEvidence('${escapeHtmlAttr(id)}')">${escapeHtml(id)}</button>`).join(' ')||'无已解析引用';
    const assessments={supported:'观测支持该机制',contradicted:'直接观测反驳该机制',uncertain:'证据不足，仍不确定',invalid_or_missing:'复核缺失或校验未通过'};
    const tests={zero_cpu_memory_requests:'完整声明中的 CPU/memory requests 均为零',no_pvc_or_ephemeral_volume:'完整声明没有 PVC 或通用临时卷引用'};
    const limits={absence_or_indirect_rationale_is_not_refutation:'未提及或间接理由不能作为反证',falsification_test_not_observed:'未引用满足条件的完整声明检查',typed_test_contradicts_support:'完整声明检查与模型支持判断冲突'};
    const sourceStates={untried:'尚未尝试',failed:'采集失败或结果未知',observed:'已取得可引用观测',no_citable_observations:'采集完成，当前范围内无可引用观测'};
    const sourceReasons={hypothesis_requested_failed_source_retry:'候选仍需该来源补证，安排有限重试',hypothesis_requested_untried_source:'候选建议检查尚未尝试的来源',bounded_failed_source_retry:'此前采集失败，重试次数尚未用完',untried_source:'检查尚未尝试的信息来源'};
    const readinessReasons={no_active_hypothesis:'当前没有未排除候选',leading_hypothesis_not_grounded:'主候选缺少有效且无冲突的当前引用',leading_hypothesis_not_confirmed_above_threshold:'主候选尚未达到转审状态与评分条件',leading_hypothesis_open_questions:'主候选仍有待回答的问题',close_supported_alternatives:'评分接近的其他候选也有当前引用，仍需区分',unverified_candidate_rejections:'候选的排除结论缺少直接反证',requested_source_retry_pending:'主候选请求的失败来源尚未补齐，重试仍受来源额度限制'};
    const followupReasons={no_unverified_rejection:'没有待核实的排除断言',target_identity_or_scope_unresolved:'目标身份或范围尚未确定，未执行补证',invalid_target_identity:'目标身份无效，未执行补证',followup_budget_exhausted:'补证时间预算已用尽',followup_read_timeout:'只读查询超时，结果未知',followup_read_failed:'只读查询失败或结果未知',target_changed_or_unverifiable:'目标身份或资源版本变化，需重新诊断',followup_collected_review_required:'只读补证已采集，仍需依据独立复核与最终门禁判断'};
    return `<strong>假设产生与调查调整</strong><p>初始置信度用于候选排序；引用存在不等于支持关系或因果已成立。最终仍须独立审核。</p>
        ${data.scale_zero_evidence ? `<p>缩零取证：${data.scale_zero_evidence.eligible_for_further_review === true ? '结构化观测满足进一步核验条件' : '证据不足，继续保留缺口'}；不代表根因已证明或允许执行。
            ${(Array.isArray(data.scale_zero_evidence.blockers) ? data.scale_zero_evidence.blockers : []).map(escapeHtml).join('；')}</p>` : ''}
        ${data.adaptive_skill_replanning_enabled===false?(data.followup?.attempted?'<p>已尝试一次受限只读补证；自适应 Skill 重规划尚未启用。</p>':'<p>当前未执行受限补证，自适应 Skill 重规划尚未启用。</p>'):''}
        ${data.followup?`<details class="skill-raw-fold"><summary>受限补证 · ${data.followup.attempted?'已尝试':'未执行'}</summary>
            <p>涉及候选：${(data.followup.candidate_ids||[]).map(escapeHtml).join('、')||'无'}；${escapeHtml(followupReasons[data.followup.stop_reason]||data.followup.stop_reason||'状态未记录')}。</p>
            ${(data.followup.commands||[]).map(c=>`<p><code>${escapeHtml(c.command||'')}</code> · ${c.executed===true?(c.success===true?'执行成功':'执行失败'):'未确认执行'}</p>`).join('')}
            ${data.followup.reassessment_received===true?'<p>已收到补证后的候选重新评估；仍须以最终复核为准。</p>':data.followup.reassessment_received===false?'<p>未收到有效的补证后评估，保留原有缺口。</p>':''}
            <p>补证和候选评估均不授权执行动作。</p></details>`:''}
        <details class="skill-raw-fold"><summary>初始候选 · ${(generation.items||[]).length} 项</summary>
        ${(generation.items||[]).map(h=>`<p>${escapeHtml(h.id||'')} · ${escapeHtml(source[h.source]||h.source||'来源未记录')} · ${escapeHtml(formatConfidence(h.confidence))}<br>${escapeHtml(h.description||'')}
            ${h.supports_if?`<br>支持条件：${escapeHtml(h.supports_if)}`:''}${h.refutes_if?`<br>反驳条件：${escapeHtml(Array.isArray(h.refutes_if)?h.refutes_if.join('；'):h.refutes_if)}`:''}</p>`).join('')}</details>
        ${(data.rounds||[]).map(r=>`<details class="skill-raw-fold"><summary>第 ${r.iteration} 轮 · ${escapeHtml(STRATEGY_REASONS[r.decision?.reason]||r.decision?.reason||'')}</summary>
            <p>实际调查：${(r.selected_agents||[]).map(a=>escapeHtml(rcaAgentLabel(a))).join('、')}；新增差异观测 ${(r.novelty?.novel_observation_ids||[]).length} 项；重复观测 ${(r.novelty?.repeated_observation_ids||[]).length} 项。</p>
            <p>采集失败或不完整：${(r.collection_failures||[]).map(a=>escapeHtml(rcaAgentLabel(a))).join('、')||'无已记录项'}；采集失败不是反驳证据。</p>
            ${r.readiness?`<details class="skill-raw-fold rca-readiness"><summary>补证缺口与停止条件 · ${r.readiness.ready_for_independent_review?'满足转入独立审核的条件':'仍需补证或复核'}</summary>
                <p>待满足条件：${(r.readiness.blockers||[]).map(code=>escapeHtml(readinessReasons[code]||code)).join('；')||'没有已记录的阻断项'}。</p>
                ${(r.readiness.open_questions||[]).map(question=>`<p>待判别问题：${escapeHtml(question)}</p>`).join('')}
                ${r.readiness.close_alternative_ids?.length?`<p>需继续区分的候选：${r.readiness.close_alternative_ids.map(id=>escapeHtml(id)).join('、')}。</p>`:''}
                ${r.readiness.unverified_rejection_ids?.length?`<p>排除结论尚未核实的候选：${r.readiness.unverified_rejection_ids.map(id=>escapeHtml(id)).join('、')}。</p>`:''}
                <p>候选评分差值用于保守安排补证，不是校准概率，也不说明候选必然互斥。调查停止不等于缺口已解决。</p></details>`:'<p>此旧轮次未保存补证与停止条件，不能倒推当时已经满足。</p>'}
            ${r.source_states?`<details class="skill-raw-fold"><summary>取证来源状态与重试</summary>
                ${Object.entries(r.source_states).map(([agent,s])=>`<p>${escapeHtml(rcaAgentLabel(agent))} · ${escapeHtml(sourceStates[s.status]||s.status||'状态未知')} · 尝试 ${escapeHtml(String(s.attempts||0))} 次${s.last_iteration?`，最近在第 ${escapeHtml(String(s.last_iteration))} 轮` : ''}。
                    ${s.status==='failed'?(s.retry_eligible?'仍可有限重试。':'本轮不再建议重试，保留取证缺口。'):''}
                    ${s.last_observation_ids?.length?`<br>最近一次可引用观测：${refs(s.last_observation_ids)}`:''}</p>`).join('')}
                <p>当前无可引用观测不表示故障机制已被反驳；来源重试仍受总轮数和模型预算限制，必要的生命周期或传播核查按原流程执行。</p></details>`:'<p>此旧轮次未保存逐来源状态，不能倒推此前失败或重试。</p>'}
            ${(r.after||[]).map(h=>{const prior=(r.before||[]).find(p=>p.id===h.id);return `<div class="rca-remediation"><p>${escapeHtml(h.id||'')} · ${escapeHtml(h.description||'')}</p>
                <p>候选评分：${prior?escapeHtml(formatConfidence(prior.confidence)):'新分支'} → ${escapeHtml(formatConfidence(h.confidence))}；模型状态：${escapeHtml(h.status||'未评估')}</p>
                <p>支持引用：${refs(h.supporting_evidence_ids)}<br>反驳引用：${refs(h.contradicting_evidence_ids)}</p>
                <p>${escapeHtml(h.update_reason||'未记录更新说明')}</p>
                ${h.validation_errors?.length?`<p class="text-warning">引用或评估缺口：${h.validation_errors.map(e=>escapeHtml(e)).join('；')}</p>`:''}
                ${h.unresolved_questions?.length?`<p>待判别：${h.unresolved_questions.map(q=>escapeHtml(q)).join('；')}</p>`:''}</div>`;}).join('')}
            <details><summary>本轮验证计划</summary><pre>${escapeHtml(JSON.stringify(r.plan||{},null,2))}</pre></details>
            ${(r.branch_proposals||[]).length?`<details><summary>新分支的接受与拒绝</summary><pre>${escapeHtml(JSON.stringify(r.branch_proposals,null,2))}</pre></details>`:''}
            <p>下一步信息源：${(r.decision?.next_agents||[]).map(a=>escapeHtml(rcaAgentLabel(a))).join('、')||'本轮已停止，按最终审核状态继续处理'}。</p>
            ${(r.decision?.next_agents||[]).map(a=>`<p>${escapeHtml(rcaAgentLabel(a))}：${escapeHtml(sourceReasons[r.decision?.next_agent_reasons?.[a]]||'此轮未保存来源选择理由')}。</p>`).join('')}</details>`).join('')}
        <strong>竞争假设的独立角色复核</strong><p>此复核使用现有 Judge 调用，可与生成角色使用同一模型；不是人工审核，也不保证判断必然正确。</p>
        ${(data.independent_reviews||[]).length?(data.independent_reviews||[]).map((review,index)=>`<details class="skill-raw-fold"><summary>复核 ${index+1} · ${review.stage==='closure_quality_review'||(review.stage==='agent_skill_final_review'&&data.followup?.attempted)?'补证后复核':review.stage==='agent_skill_final_review'?'最终质量复核':'初次质量复核'} · ${review.blockers?.length?'仍有确认/排除判断未通过':'按最终诊断门禁判断'}</summary>
            <p>独立模型：${review.skill_review?.independent_role_call===true?'已调用':review.skill_review?.independent_role_call===false?'未调用（输入或预算未满足）':'调用状态未记录'}；${review.skill_review?.provider_receipt?.schema_version===1&&review.skill_review?.provider_receipt?.source==='provider_response'&&review.skill_review?.provider_receipt?.completion_status==='complete'&&review.skill_review?.provider_receipt?.finish_reason==='stop'&&review.skill_review?.provider_receipt?.refusal_present===false&&review.skill_review?.provider_receipt?.tool_call_present===false?'已记录完整结束凭据':'没有完整结束凭据'}。</p>
            ${(review.items||[]).map(h=>`<div class="rca-remediation"><p>${escapeHtml(h.id)} · ${escapeHtml(h.description||'')}</p>
                <p>提出角色状态：${escapeHtml(h.proposer_status||'')}；复核：${escapeHtml(assessments[h.status]||h.status||'未记录')}</p>
                ${h.verification_test?`<p>限定可证伪检查：${escapeHtml(tests[h.verification_test]||h.verification_test)}；直接检查引用：${refs([...new Set([...(h.verified_test_evidence_ids||[]),...(h.host_test_evidence_ids||[])])])}。仅适用于该条件，不代表所有资源或存储均健康。</p>`:''}
                ${h.rationale_limits?.length?`<p class="text-warning">复核模型原判断为 ${escapeHtml(assessments[h.reviewer_status]||h.reviewer_status)}；宿主保留不确定状态：${h.rationale_limits.map(code=>escapeHtml(limits[code]||code)).join('；')}。</p>`:''}
                <p>${escapeHtml(h.reason||'未返回有效说明')}<br>支持引用：${refs(h.supporting_evidence_ids)}<br>反驳引用：${refs(h.contradicting_evidence_ids)}</p>
                <p>待补观测：${(h.missing_evidence||[]).map(q=>escapeHtml(q)).join('；')||'无已记录项'}</p>
                ${h.validation_errors?.length?`<p class="text-warning">复核缺口：${h.validation_errors.map(q=>escapeHtml(q)).join('；')}</p>`:''}</div>`).join('')}</details>`).join(''):'<p>这条记录未保存逐假设独立复核。Agent+Skill 尚未接入等价独立 Judge；不能据此把模型自评当作独立审核。</p>'}
        <p>停止原因：${escapeHtml(STRATEGY_REASONS[data.stop_reason]||data.stop_reason||'仍在进行')}。本次调整不是已验证的跨事故策略学习或准确率提升。</p>`;
}

async function openStrategyEvidence(id) {
    const runId=state.rcaRunId;
    if (!runId) return;
    if (state._lastOntology?.run_id!==runId) await loadRCAOntology(runId);
    if (state.rcaRunId!==runId) return;
    await loadOntologyCollection('observations','',0,id);
    if (state.rcaRunId===runId) document.getElementById('rca-ontology-items')?.scrollIntoView({block:'nearest'});
}

async function loadOntologyCollection(collection, entityId = '', offset = 0, recordId = '') {
    const model = state._lastOntology;
    if (!model || state.rcaRunId !== model.run_id) return;
    const token = (state._ontologyPageRequest || 0) + 1;
    state._ontologyPageRequest = token;
    const query = new URLSearchParams({revision: model.revision, entity_id: entityId, record_id: recordId, offset: String(offset), limit: '25'});
    const page = await api(`/api/rca/${encodeURIComponent(model.run_id)}/ontology/${collection}?${query}`);
    if (state._lastOntology !== model || state.rcaRunId !== model.run_id || state._ontologyPageRequest !== token) return;
    const el = document.getElementById('rca-ontology-items');
    if (!el) return;
    if (!page?.items) { el.textContent = '读取失败或调查版本已变化，请重新打开记录。'; return; }
    const rows = page.items;
    let html = `<p class="text-muted">${entityId ? '已按所选对象筛选 · ' : ''}共 ${Number(page.total)} 项</p>`;
    if (!rows.length) html += '<p>此范围没有已记录的数据。</p>';
    rows.forEach(row => {
        if (collection === 'objects') {
            html += `<details class="skill-raw-fold"><summary>${escapeHtml(row.cluster_id || '集群未知')} / ${escapeHtml(row.namespace || '集群级')} · ${escapeHtml(row.kind)}/${escapeHtml(row.name)}</summary>
                <p>UID：${escapeHtml(row.uid || '未采集')} · ${row.identity_status === 'uid_bound' ? '已绑定对象版本身份' : '身份信息不完整'}</p>
                <button class="btn btn-sm" onclick="loadOntologyCollection('observations','${escapeHtmlAttr(row.id)}')">查看该对象观测</button>
                <button class="btn btn-sm" onclick="loadOntologyCollection('relations','${escapeHtmlAttr(row.id)}')">查看该对象关系</button>
                ${(model.history_eligible_object_ids || []).includes(row.id) ? `<button class="btn btn-sm" onclick="loadOntologyObjectHistory('${escapeHtmlAttr(row.id)}')">跨调查历史</button>` : ''}
                ${row.identity_status === 'uid_bound' && ['Pod','ReplicaSet','Deployment','StatefulSet','DaemonSet','Job','CronJob'].includes(row.kind) && row.cluster_id === model.scope.cluster_id && (!model.scope.namespace || row.namespace === model.scope.namespace) ? `<button class="btn btn-sm" onclick="collectRCALineage('${escapeHtmlAttr(row.id)}',this)">采集发布与镜像追溯</button>` : ''}
                <code>${escapeHtml(row.id)}</code></details>`;
        } else if (collection === 'observations') {
            html += `<details class="skill-raw-fold"><summary>${escapeHtml(row.source_agent || '')} · ${escapeHtml(row.signal_type || '观测')} · ${escapeHtml(row.id)}</summary>
                <p>对象：${escapeHtml(row.entity_id || '身份未确定')} · ${row.scope_status === 'outside_run' ? '范围外审计观测' : row.scope_status === 'unresolved' ? '对象范围未确定' : '本次调查范围'}</p>
                <p>采集时间：${escapeHtml(String(row.observed_at || '未采集'))} · 事件时间：${escapeHtml(String(row.event_time || '无'))} · 样本时间：${escapeHtml(String(row.sampled_at || '无'))}</p>
                <p>${row.temporality === 'historical' ? '历史状态' : '观测记录'} · 资源版本：${escapeHtml(row.resource_version || '未采集')}${row.excerpted ? ' · 内容为节选' : ''}</p>
                <p>来源：${escapeHtml(row.source || '')}</p><pre>${escapeHtml(row.observation || '')}</pre>
                <details><summary>结构化原始引用</summary><pre>${escapeHtml(JSON.stringify(row.raw_ref || {}, null, 2))}</pre></details></details>`;
        } else if (collection === 'relations') {
            const label = {scheduled_on: '调度到', owned_by: '归属于'}[row.type] || row.type;
            html += `<div class="rca-remediation"><p>${escapeHtml(row.source_label || row.source_id)} → ${escapeHtml(label)} → ${escapeHtml(row.target_label || row.target_id)}</p>
                <p>依据：<button class="btn btn-sm" onclick="loadOntologyCollection('observations','',0,'${escapeHtmlAttr(row.evidence_id)}')">${escapeHtml(row.evidence_id)}</button> · ${escapeHtml(String(row.observed_at || '时间未采集'))}</p></div>`;
        } else {
            const status = {unknown: '结果未确认', succeeded: '已确认执行成功', failed: '执行失败', not_executed: '未确认已执行'}[row.status] || row.status;
            html += `<details class="skill-raw-fold"><summary>${escapeHtml(status)} · ${escapeHtml(row.tool || '')}</summary><pre>${escapeHtml(row.command || '')}</pre><p>${escapeHtml(row.error || '')}</p></details>`;
        }
    });
    html += '<div class="rca-heal-toolbar">';
    if (offset > 0) html += `<button class="btn btn-sm" onclick="loadOntologyCollection('${collection}','${escapeHtmlAttr(entityId)}',${Math.max(0, offset - 25)})">上一页</button>`;
    if (offset + rows.length < page.total) html += `<button class="btn btn-sm" onclick="loadOntologyCollection('${collection}','${escapeHtmlAttr(entityId)}',${offset + 25})">下一页</button>`;
    html += '</div>';
    el.innerHTML = html;
}

async function loadOntologyObjectHistory(entityId, offset = 0) {
    const model = state._lastOntology;
    const panel = document.getElementById('rca-object-history-panel');
    if (!model || state.rcaRunId !== model.run_id || !panel) return;
    let context = state._ontologyHistory;
    if (!context || context.model !== model || context.entityId !== entityId) {
        context = {model, entityId, data: null, stale: false};
        state._ontologyHistory = context;
        panel.innerHTML = `<section class="rca-remediation"><strong>跨调查对象历史</strong>
            <p>保留窗口内的已记录观测，以所选调查的采集时间为上限；观测变化不单独证明因果。</p>
            <button class="btn btn-sm" data-history-action="refresh">刷新历史</button>
            <button class="btn btn-sm" data-history-action="reload">重新加载本次调查</button>
            <p data-history-status role="status"></p><div data-history-body></div><div data-history-pager></div></section>`;
        panel.onclick = event => {
            const button = event.target.closest('button[data-history-action]');
            if (!button || !panel.contains(button) || state._ontologyHistory !== context || state._lastOntology !== model || state.rcaRunId !== model.run_id) return;
            const action = button.dataset.historyAction;
            if (action === 'source') openRCAHistoryRun(button.dataset.runId);
            else if (action === 'reload') loadRCAOntology(model.run_id);
            else loadOntologyObjectHistory(entityId, action === 'page' ? Number(button.dataset.offset) : 0);
        };
    }
    const token = (state._ontologyHistoryRequest || 0) + 1;
    state._ontologyHistoryRequest = token;
    const active = () => state._lastOntology === model && state.rcaRunId === model.run_id &&
        state._ontologyHistory === context && state._ontologyHistoryRequest === token;
    const status = panel.querySelector('[data-history-status]');
    status.textContent = context.data ? '正在读取，现有内容保留至读取成功。' : '正在读取对象历史…';
    panel.querySelectorAll('[data-history-action="page"]').forEach(button => { button.disabled = true; });
    const query = new URLSearchParams({revision: model.revision, offset: String(offset), limit: '25'});
    if (offset > 0 && context.data) query.set('history_revision', context.data.history_revision);
    try {
        const data = await api(`/api/rca/${encodeURIComponent(model.run_id)}/objects/${encodeURIComponent(entityId)}/history?${query}`,
            {throwOnError: true, suppressErrorLog: true, cache: 'no-store'});
        if (!active()) return;
        if (!Array.isArray(data?.items) || data.target?.id !== entityId || data.source_run_id !== model.run_id ||
                data.source_revision !== model.revision || typeof data.history_revision !== 'string') throw new Error('对象历史响应不完整。');
        context.data = data;
        context.stale = false;
        const labels = {baseline:'首份可比较观测', changed:'共同字段存在变化', availability_changed:'字段采集范围变化',
            unchanged:'共同观测字段未变', ambiguous_time:'同一采集时间，先后顺序未知', context_only:'历史上下文，不更新比较基线', not_comparable:'状态投影不足，无法比较'};
        const gapLabels = {matching_object_uid_missing:'部分同名观测缺少 UID', matching_observation_time_missing:'部分观测缺少有效采集时间',
            collection_incomplete:'来源调查存在采集失败或不完整记录', history_run_identity_missing:'部分调查缺少有效来源标识',
            legacy_excerpts_excluded:'旧审核节选未纳入事实历史', non_json_tool_result_excluded:'部分非 JSON 工具结果未纳入',
            state_not_comparable:'部分状态不能比较', invalid_state_projection:'部分投影无法校验',
            run_observation_limit:'部分调查超过观测条数上限', run_event_limit:'部分调查超过事件扫描上限',
            run_command_limit:'部分调查超过工具记录上限', tool_result_size_limit:'部分工具结果超过体积上限',
            observation_size_limit:'部分观测超过体积或结构上限',
            tool_resource_limit:'部分工具资源清单超过上限', invalid_tool_projection:'部分工具投影无法校验'};
        const source = runId => `<button class="btn btn-sm" data-history-action="source" data-run-id="${escapeHtmlAttr(runId)}">查看来源调查</button>`;
        let html = `<p>${escapeHtml(data.target.cluster_id)} / ${escapeHtml(data.target.namespace || '集群级')} · ${escapeHtml(data.target.kind)}/${escapeHtml(data.target.name)}<br>
            UID：<code>${escapeHtml(data.target.uid)}</code> · 时间上限：${escapeHtml(data.as_of)}</p>
            <p>当前保留窗口最多 ${Number(data.coverage?.retained_terminal_limit || 200)} 条终态调查，另包含所选调查视图。共 ${Number(data.total)} 项同实例观测。
            ${data.coverage?.source_outside_retained_window ? '所选调查位于终态保留窗口之外。' : ''}</p>
            <p class="text-muted">这是离散采集记录；未列出不等于不存在，字段缺失不等于资源删除。补充发布谱系快照在下方独立展示。</p>`;
        if (data.gaps?.length) html += `<p class="text-warning">覆盖缺口：${data.gaps.map(g => escapeHtml(gapLabels[g] || g)).join('；')}</p>`;
        const omitted = data.coverage?.omitted || {};
        if (omitted.after_as_of) html += `<p>已排除 ${Number(omitted.after_as_of)} 项时间上限之后的观测。</p>`;
        if (!data.items.length) html += '<p>在当前保留范围和时间上限内，没有可展示的同实例观测。请同时核对上述覆盖缺口。</p>';
        for (const row of data.items) {
            const comparison = row.comparison || {};
            html += `<details class="skill-raw-fold"><summary>${escapeHtml(row.observed_at)} · ${escapeHtml(labels[comparison.status] || comparison.status)}</summary>
                <p>调查：${escapeHtml(row.run_id)} · ${escapeHtml(row.run_status)} ${source(row.run_id)}</p>
                ${row.collection_has_gaps ? '<p class="text-warning">所在调查有采集缺口；本条已记录观测仍保留供核对。</p>' : ''}
                <p>观测引用：<code>${escapeHtml(row.observation_id)}</code> · 资源版本：${escapeHtml(String(row.resource_version ?? '未采集'))}</p>
                ${comparison.previous_observation_id ? `<p>比较基线：${escapeHtml(comparison.previous_run_id)} / ${escapeHtml(comparison.previous_observation_id)}</p>` : ''}
                ${comparison.changes?.length ? `<p>两次均采集到的字段变化：</p><pre>${escapeHtml(JSON.stringify(comparison.changes, null, 2))}</pre>` : ''}
                ${comparison.availability?.added_fields?.length ? `<p>本次新增采集字段：${comparison.availability.added_fields.map(escapeHtml).join('、')}</p>` : ''}
                ${comparison.availability?.missing_fields?.length ? `<p>本次未采集字段：${comparison.availability.missing_fields.map(escapeHtml).join('、')}</p>` : ''}
                <details><summary>受限状态投影</summary><pre>${escapeHtml(JSON.stringify(row.state, null, 2))}</pre></details></details>`;
        }
        if (data.other_incarnations?.length) html += `<details class="skill-raw-fold"><summary>同名、不同 UID 的其他实例（${data.other_incarnations.length} 个，未合并）</summary>` +
            data.other_incarnations.map(row => `<p>UID：${escapeHtml(row.uid)} · 观测 ${Number(row.observations)} 项 · ${escapeHtml(row.first_seen)} 至 ${escapeHtml(row.last_seen)}</p>`).join('') + '</details>';
        panel.querySelector('[data-history-body]').innerHTML = html;
        let pager = '';
        if (data.offset > 0) pager += `<button class="btn btn-sm" data-history-action="page" data-offset="${Math.max(0, data.offset - data.limit)}">上一页</button>`;
        if (data.has_more) pager += `<button class="btn btn-sm" data-history-action="page" data-offset="${data.offset + data.limit}">下一页</button>`;
        panel.querySelector('[data-history-pager]').innerHTML = pager;
        status.textContent = data.items.length ? `已读取第 ${Number(data.offset) + 1}—${Number(data.offset) + data.items.length} 项；不表示当前集群状态。` :
            '当前范围没有可展示的观测；请核对覆盖缺口，不表示当前集群状态。';
    } catch (error) {
        if (!active()) return;
        context.stale = error.status === 409;
        status.textContent = `${error.message || '对象历史读取失败。'}${context.data ? ' 已保留原页面，尚未刷新。' : ''}`;
    } finally {
        if (active()) panel.querySelectorAll('[data-history-action="page"]').forEach(button => { button.disabled = context.stale; });
    }
}

async function loadRCALineage(model = state._lastOntology) {
    if (!model || state.rcaRunId !== model.run_id) return;
    const token = (state._lineageRequest || 0) + 1;
    state._lineageRequest = token;
    const data = await api(`/api/rca/${encodeURIComponent(model.run_id)}/lineage`);
    if (state._lastOntology !== model || state.rcaRunId !== model.run_id || state._lineageRequest !== token) return;
    const panel = document.getElementById('rca-lineage-panel');
    if (!panel) return;
    if (!data?.snapshots) { panel.textContent = '补充追溯暂时无法读取，请重新打开记录。'; return; }
    const labels = {resource_read_failed:'对象无法读取', resource_identity_changed:'同名对象身份已变化',
        resource_version_missing:'资源版本缺失', pod_has_no_controlling_owner:'Pod 未声明控制器归属',
        ambiguous_controlling_owner:'控制器归属不唯一', owner_identity_incomplete_or_unsupported:'上级身份不完整或类型尚未支持',
        owner_cycle:'归属引用存在循环', owner_depth_limit:'达到采集深度上限',
        git_and_release_events_not_connected:'Git 与发布事件数据源尚未接入'};
    panel.innerHTML = `<strong>发布与镜像追溯（${data.snapshots.length} 次补充采集）</strong>
        <p class="text-muted">在对象详情中发起只读采集。补充结果不会重跑诊断，也未经过原诊断审核；控制器修订号不代表 Git 提交，创建时间不代表发布时间。</p>` +
        [...data.snapshots].reverse().map(s => {
            const prior = data.snapshots.slice(0,data.snapshots.indexOf(s)).reverse().find(p =>
                ['cluster_id','namespace','kind','name','uid'].every(k=>p.target?.[k]===s.target?.[k]));
            return `<details class="skill-raw-fold"><summary>${escapeHtml(s.target.kind)}/${escapeHtml(s.target.name)} · ${{complete:'采集完成',partial:'部分可用',unavailable:'无法取证'}[s.status] || escapeHtml(s.status)} · ${escapeHtml(s.finished_at)}</summary>
            <p>${(s.gaps || []).map(g => escapeHtml(labels[g] || g)).join('；')}</p>
            <p>快照：${escapeHtml(s.id)} · 各对象分别采集，不能作为同一时刻的事务快照或因果证明。</p>
            ${prior ? `<button class="btn btn-sm" onclick="compareRCALineage('${escapeHtmlAttr(prior.id)}','${escapeHtmlAttr(s.id)}')">与该对象上次快照比较</button>` : ''}
            ${(s.observations || []).map(o => `<div class="rca-remediation"><p>${escapeHtml(o.entity.kind)}/${escapeHtml(o.entity.name)} · UID ${escapeHtml(o.entity.uid)}</p>
                <p>采集时间 ${escapeHtml(o.observed_at)} · 资源版本 ${escapeHtml(o.resource_version)} · 控制器修订号 ${escapeHtml(o.raw_ref.controller_revision || '未采集')}</p>
                ${(o.raw_ref.containers || []).map(c => `<p>${escapeHtml(c.container_kind)}/${escapeHtml(c.container)}<br>声明镜像：<code>${escapeHtml(c.declared_image)}</code><br>运行时镜像标识：<code>${escapeHtml(c.runtime_image_id || '未报告')}</code></p>`).join('')}
                ${renderLineageSourceMetadata(o.raw_ref.source_metadata)}
                <details><summary>已投影的来源数据 · ${escapeHtml(o.id)}</summary><pre>${escapeHtml(JSON.stringify(o.raw_ref,null,2))}</pre></details></div>`).join('')}
            ${(s.relations || []).map(r => {const a=s.observations.find(o=>o.id===r.source_id),b=s.observations.find(o=>o.id===r.target_id);return `<p>${escapeHtml(a?.entity.kind)}/${escapeHtml(a?.entity.name)} → 控制器 UID 已核对 → ${escapeHtml(b?.entity.kind)}/${escapeHtml(b?.entity.name)}</p>`;}).join('')}
            </details>`;
        }).join('') + '<div id="rca-lineage-comparison" aria-live="polite"></div>';
}

function renderLineageSourceMetadata(metadata) {
    if (!metadata) return '<p class="text-muted">旧快照未采集来源元数据，不能据此判定没有来源声明。</p>';
    const claims = metadata.claims || [];
    return `<details><summary>代码与发布来源声明 · ${claims.length} 项</summary>
        <p>这些值来自 Kubernetes 元数据，尚未核验 Git、镜像构建来源或发布系统对象；页面不会访问声明中的地址。</p>
        ${claims.length ? claims.map(c=>`<p>${escapeHtml(c.path)} · ${escapeHtml(c.key)}<br><code>${escapeHtml(c.value)}</code></p>`).join('') : '<p>该对象未采集到受支持的来源声明。</p>'}
        ${metadata.rejected_fields?.length ? `<p class="text-warning">${metadata.rejected_fields.length} 项值不受支持或含敏感参数，未保留原值。</p>` : ''}</details>`;
}

async function compareRCALineage(beforeId, afterId) {
    const model = state._lastOntology;
    if (!model || state.rcaRunId !== model.run_id) return;
    const token = (state._lineageCompareRequest || 0) + 1;
    state._lineageCompareRequest = token;
    const query = new URLSearchParams({before_id:beforeId,after_id:afterId});
    const result = await api(`/api/rca/${encodeURIComponent(model.run_id)}/lineage/compare?${query}`);
    if (state._lastOntology !== model || state.rcaRunId !== model.run_id || state._lineageCompareRequest !== token) return;
    const panel = document.getElementById('rca-lineage-comparison');
    if (!panel) return;
    if (!result?.changes) { panel.textContent = '快照无法比较，请核对对象、采集时间和记录是否仍存在。'; return; }
    const labels = {legacy_source_metadata_not_comparable:'旧快照的来源元数据没有采集，相关字段不参与比较',
        snapshot_coverage_differs_or_incomplete:'两次采集覆盖不同或存在缺口'};
    panel.innerHTML = `<strong>同一对象的快照比较</strong><p>${escapeHtml(beforeId)} → ${escapeHtml(afterId)}</p>
        <p>比较的是两次观测值；不能单独证明发布、删除或故障因果，也不能证明期间未发生变更。</p>
        ${(result.gaps||[]).length ? `<p class="text-warning">${result.gaps.map(g=>escapeHtml(labels[g]||g)).join('；')}</p>` : ''}
        ${result.changes.length ? result.changes.map(c=>`<div class="rca-remediation"><p>${escapeHtml(c.entity.kind)}/${escapeHtml(c.entity.name)} · ${escapeHtml(c.field)}</p>
            <p>${c.type==='observation_availability_changed'?'观测可用性变化':'观测值变化'}：<code>${escapeHtml(c.before==null?'未观测':String(c.before))}</code> → <code>${escapeHtml(c.after==null?'未观测':String(c.after))}</code></p>
            <p>来源：${c.evidence_ids.map(id=>escapeHtml(id)).join(' / ')}</p></div>`).join('') : '<p>未发现可比较字段的变化。</p>'}
        ${(result.unmatched_observations||[]).map(o=>`<p>${escapeHtml(o.entity.kind)}/${escapeHtml(o.entity.name)}：仅${o.observed_in==='before_only'?'前次':'后次'}采集到，不能据此判定创建或删除。来源：${escapeHtml(o.evidence_id)}</p>`).join('')}`;
}

async function collectRCALineage(entityId, button) {
    const model = state._lastOntology;
    if (!model || state.rcaRunId !== model.run_id || state._lineageCollecting) return;
    state._lineageCollecting = true;
    if (button) { button.disabled = true; button.textContent = '正在只读采集…'; }
    try {
        const result = await api(`/api/rca/${encodeURIComponent(model.run_id)}/lineage`, {
            method:'POST', timeoutMs:75000, headers:{'Content-Type':'application/json'},
            body:JSON.stringify({entity_id:entityId,revision:model.revision})});
        if (state._lastOntology !== model || state.rcaRunId !== model.run_id) return;
        if (result?.id) await loadRCALineage(model);
        else if (button) button.textContent = '采集未完成，点击重试';
    } finally {
        state._lineageCollecting = false;
        if (button) { button.disabled = false; if (button.textContent === '正在只读采集…') button.textContent = '采集发布与镜像追溯'; }
    }
}

function renderRCAHypothesisGenerationSummary(hypotheses = [], generation = {}) {
    const llmUsed = !!generation.llm_used || hypotheses.some(h => h.llm_used || h.source === 'llm');
    const top = hypotheses.slice(0, 3);
    const llmCount = generation.llm_hypothesis_count ?? hypotheses.filter(h => h.llm_used || h.source === 'llm').length;
    const ruleCount = generation.deterministic_hypothesis_count ?? Math.max(0, hypotheses.length - llmCount);
    return `<div class="agent-llm-hypothesis-box">
        <div class="agent-llm-head">
            <strong>假设生成</strong>
            <span class="badge badge-${llmUsed ? 'success' : 'gray'}">${llmUsed ? 'LLM 已生成' : '规则生成'}</span>
            <small>LLM ${escapeHtml(llmCount)} / 规则 ${escapeHtml(ruleCount)}</small>
        </div>
        <p>Agent 按“历史上下文检索 -> LLM 生成候选假设 -> 规则合并排序 -> 调查验证”的方式产生根因候选。</p>
        <div class="agent-llm-hyp-list">${top.map((h, i) => `
            <div>
                <span>#${i + 1}</span>
                <strong>${escapeHtml(formatConfidence(h.confidence))}</strong>
                <p>${escapeZh(h.description || h.root_cause || h.summary || '')}</p>
                <small>${escapeHtml(h.source === 'llm' || h.llm_used ? 'LLM生成' : '规则生成')}</small>
            </div>
        `).join('')}</div>
    </div>`;
}

function buildHealPayloadFromRCA(rca) {
    const services = rca?.affected_services || [];
    const primary = Array.isArray(services) && services.length ? services[0] : '';
    const root = rca?.root_entity || {};
    const ontology = state._lastOntology?.run_id === state._lastRCARunId ? state._lastOntology : null;
    return {
        source: 'rca',
        source_run_id: state._lastRCARunId || '',
        ontology_revision: ontology?.revision || '',
        cluster_id: ontology?.scope?.cluster_id || '',
        namespace: root.namespace || ontology?.scope?.namespace || document.getElementById('rca-ns')?.value || 'default',
        fault_type: rca?.fault_type || '',
        root_cause: rca?.root_cause || '',
        root_cause_component: rca?.root_cause_component || primary,
        message: `${rca?.fault_type || ''} ${rca?.root_cause || ''} ${rca?.remediation_suggestion || ''}`,
        object: primary,
        pod: root.kind === 'Pod' ? root.name : '',
        deployment: root.kind === 'Deployment' ? root.name : '',
        evidence: Object.values(rca?.evidence_summary || {}).map(v => String(v || '')),
        diagnosis: rca,
    };
}

async function suggestHealFromRCA() {
    const rca = state._lastRCAResult;
    if (!rca) return;
    const sourcePayload = buildHealPayloadFromRCA(rca);
    const panel = document.getElementById('rca-heal-capability');
    if (panel) panel.innerHTML = '<div class="loading">生成自愈能力</div>';
    const result = await api('/api/heal/capability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sourcePayload),
    });
    if (!result || !panel) return;
    state._lastRCAHealResult = result;
    state._lastRCAHealPayload = sourcePayload;
    const suggestions = result.suggestions || [];
    const blocked = result.blocked_templates || [];
    const capability = result.capability || {};
    panel.innerHTML = `<div class="alert-heal-panel rca-heal-panel">
        <div class="alert-heal-header">
            <div>
                <h4>诊断自愈能力</h4>
                <p class="text-muted">${escapeHtml(result.fault_type || 'unknown')} · ${escapeHtml(result.namespace || 'default')}</p>
            </div>
            <div class="table-actions">
                ${suggestions.length ? '<button class="btn btn-sm btn-primary" onclick="openHealPlanFromRCA()">查看/编辑方案</button>' : ''}
                ${suggestions.length ? '<button class="btn btn-sm" onclick="dryRunHealFromRCA()">快速 Dry-run</button>' : ''}
                <button class="btn btn-sm" onclick="switchView('healcenter')">自愈中心</button>
            </div>
        </div>
        <div class="alert-heal-meta">
            <span class="badge badge-${suggestions.length ? 'success' : 'warning'}">${suggestions.length ? '可自愈' : '仅建议'}</span>
            ${capability.supports_rollback ? '<span class="badge badge-success">支持回滚</span>' : ''}
            ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
        </div>
        ${result.recipe?.description ? `<div class="rca-remediation">${escapeHtml(result.recipe.description)}</div>` : ''}
        <div class="rem-actions-list">
            ${suggestions.map((s, i) => renderHealSuggestionItem(s, i)).join('')}
            ${blocked.map((s, i) => renderHealBlockedItem(s, i)).join('')}
        </div>
        <pre class="log-viewer" id="rca-heal-output" style="min-height:90px">${suggestions.length ? '点击 Dry-run 预演查看将执行的自愈动作。' : '诊断结果未匹配到可执行知识库策略。'}</pre>
    </div>`;
    if (suggestions.length) openHealPlanModal(result, sourcePayload, { source: 'rca-result' });
}

async function dryRunHealFromRCA() {
    const rca = state._lastRCAResult;
    if (!rca) return;
    const out = document.getElementById('rca-heal-output');
    if (out) out.textContent = 'Dry-run 执行中...';
    const result = await api('/api/heal/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...buildHealPayloadFromRCA(rca), dry_run: true, source: 'rca-result' }),
    });
    if (out) out.textContent = JSON.stringify(result, null, 2);
    showToast('诊断自愈 dry-run 已完成');
}

function openHealPlanFromRCA() {
    if (!state._lastRCAHealResult) return;
    openHealPlanModal(state._lastRCAHealResult, state._lastRCAHealPayload || buildHealPayloadFromRCA(state._lastRCAResult), { source: 'rca-result' });
}

function renderRemediation(data) {
    if (!data) return;
    const status = data.status || 'unknown';
    const el = document.createElement('div');
    el.id = 'rca-remediation-section';
    el.className = 'rca-remediation-section';

    if (status === 'pending_approval') {
        const plan = data.plan || {};
        const actions = plan.actions || [];
        let actionsHtml = actions.map((a, i) => {
            const risk = (a.risk_level || 'low').toLowerCase();
            const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
            return `<div class="rem-action-item"><div class="rem-action-header"><span class="rem-action-num">${i+1}</span><span class="badge badge-${riskClass}">${a.risk_level || 'low'}</span><span class="rem-action-desc">${escapeHtml(a.description || '')}</span></div><div class="rem-action-cmd"><code>${escapeHtml(a.command || '')}</code></div>${a.rollback_command ? `<div class="rem-action-rollback">${escapeHtml(a.rollback_command)}</div>` : ''}</div>`;
        }).join('');
        el.innerHTML = `<h4>自愈修复方案</h4><div class="rem-status-badge pending">等待审批</div>${plan.estimated_recovery_time ? `<div class="rem-meta">预计恢复时间: ${escapeHtml(plan.estimated_recovery_time)}</div>` : ''}<div class="rem-actions-list">${actionsHtml}</div><div class="rem-buttons"><button class="btn btn-primary" onclick="approveRemediation()">批准执行</button><button class="btn btn-secondary" onclick="dismissRemediation()">拒绝</button></div>`;
    } else if (status === 'disabled') { el.innerHTML = `<div class="rem-status-badge disabled">自愈已禁用</div>`; }
    else if (status === 'skipped') { el.innerHTML = `<div class="rem-status-badge skipped">置信度不足，跳过自愈</div>`; }
    else if (status === 'executed') { renderRemediationResult(data); return; }

    state._pendingRemediation = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingRemediation = null; }
}

function renderRemediationResult(data) {
    const section = document.getElementById('rca-remediation-section');
    const target = section || document.getElementById('rca-result-content');
    if (!target) return;
    const actions = data.actions || [];
    let html = `<div class="rca-remediation-section"><h4>自愈执行结果</h4><div class="rem-status-badge executed">已执行</div>`;
    actions.forEach((a, i) => {
        const ok = a.status === 'executed';
        html += `<div class="rem-result-item ${ok ? 'success' : 'failed'}"><span>${ok ? '&#10003;' : '&#10007;'} ${escapeHtml(a.description || `Action ${i+1}`)}</span><span class="rem-result-detail">${escapeHtml((a.result || '').substring(0, 100))}</span></div>`;
    });
    if (data.rollback_available) html += `<div class="rem-buttons"><button class="btn btn-warning" onclick="rollbackRemediation()">回滚</button></div>`;
    html += `</div>`;
    if (section) section.outerHTML = html; else target.insertAdjacentHTML('beforeend', html);
}

async function approveRemediation() {
    if (!state.rcaRunId) return;
    const btn = event.target; btn.disabled = true; btn.textContent = '执行中...';
    try { const result = await api(`/api/rca/${state.rcaRunId}/remediation/approve`, { method: 'POST' }); if (result) renderRemediationResult(result); }
    catch (e) { alert('执行失败: ' + e.message); }
    btn.disabled = false;
}

async function rollbackRemediation() {
    if (!state.rcaRunId) return;
    if (!confirm('确认回滚所有修复操作？')) return;
    try { const result = await api(`/api/rca/${state.rcaRunId}/remediation/rollback`, { method: 'POST' }); if (result) alert(`回滚完成: ${(result.actions || []).length} 个操作已撤销`); }
    catch (e) { alert('回滚失败: ' + e.message); }
}

function dismissRemediation() { const section = document.getElementById('rca-remediation-section'); if (section) section.remove(); }

function rcaHistoryListState() {
    if (!state.rcaHistoryList) state.rcaHistoryList = {
        items: [], query: '', displayedQuery: '', cursor: '', hasMore: false,
        total: 0, retainedTotal: 0, loaded: false, busy: false, error: '', generation: 0,
    };
    return state.rcaHistoryList;
}

function renderRCAHistoryList() {
    const history = rcaHistoryListState();
    const container = document.getElementById('rca-history');
    if (!container) return;
    container.setAttribute('aria-busy', String(history.busy));
    container.innerHTML = history.items.map(r => `
        <div class="signal-item" role="button" tabindex="0" data-rca-run-id="${escapeHtmlAttr(r.id || '')}"
             onclick="openRCAHistoryRun(this.dataset.rcaRunId)"
             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openRCAHistoryRun(this.dataset.rcaRunId)}">
            <span><span class="badge badge-${r.status === 'completed' ? 'success' : r.status === 'running' ? 'warning' : 'danger'}">${escapeHtml(statusLabel(r.status))}</span> ${escapeZh(r.query?.substring(0, 80) || '')}
                <small class="text-muted">${escapeHtml(r.id || '')}${r.namespace ? ' · ' + escapeHtml(r.namespace) : ''}${r.cluster_id ? ' · ' + escapeHtml(r.cluster_id) : ''}</small></span>
            <span class="text-muted">${formatTime(r.started_at ? r.started_at * 1000 : null)}</span>
        </div>
    `).join('') || (history.loaded ? `<p class="text-muted">${history.displayedQuery ? '未找到匹配记录' : '暂无历史记录'}</p>` : '');
    const summary = document.getElementById('rca-history-summary');
    if (summary) summary.textContent = (history.busy ? '正在加载历史记录… ' : '') + (history.loaded
        ? `${history.displayedQuery ? '搜索“' + history.displayedQuery + '”：' : ''}已显示 ${history.items.length} 条；当前匹配 ${history.total} 条，服务共保留 ${history.retainedTotal} 条。` : '');
    const error = document.getElementById('rca-history-error');
    if (error) error.textContent = history.error ? `历史记录加载失败。${history.loaded ? '仍显示此前加载的结果。' : ''}请重试或刷新列表。` : '';
    const more = document.getElementById('rca-history-more');
    if (more) {
        more.style.display = history.hasMore && !history.error ? 'inline-block' : 'none';
        more.disabled = history.busy;
    }
    const retry = document.getElementById('rca-history-retry');
    if (retry) { retry.style.display = history.error ? 'inline-block' : 'none'; retry.disabled = history.busy; }
    const refresh = document.getElementById('rca-history-refresh');
    if (refresh) refresh.disabled = history.busy;
}

async function loadRCAHistory(options = {}) {
    const history = rcaHistoryListState();
    const append = options.append === true;
    // Opening a retained run must not collapse its expanded or filtered history list.
    if (!options.reset && !append && (history.loaded || history.busy)) {
        renderRCAHistoryList();
        return;
    }
    if (append && (history.busy || !history.hasMore || history.query !== history.displayedQuery)) return;
    const generation = ++history.generation;
    const query = history.query;
    const params = new URLSearchParams({limit: '20', q: query});
    if (append) params.set('cursor', history.cursor);
    history.busy = true;
    history.error = '';
    history.failedAppend = append;
    renderRCAHistoryList();
    try {
        const data = await api(`/api/rca/history?${params}`, {
            cache: 'no-store', throwOnError: true, suppressErrorLog: true, timeoutMs: 20000,
        });
        if (generation !== history.generation) return;
        if (!Array.isArray(data?.runs) || !Number.isInteger(data.total) || !Number.isInteger(data.retained_total)
                || typeof data.has_more !== 'boolean' || (data.has_more && !data.next_cursor)) {
            throw new Error('Invalid history response');
        }
        const items = append ? [...history.items, ...data.runs] : data.runs;
        history.items = [...new Map(items.map(row => [row.id, row])).values()];
        history.displayedQuery = query;
        history.total = data.total;
        history.retainedTotal = data.retained_total;
        history.cursor = data.next_cursor || '';
        history.hasMore = data.has_more;
        history.loaded = true;
    } catch (error) {
        if (generation !== history.generation) return;
        history.error = 'load_failed';
    } finally {
        if (generation === history.generation) {
            history.busy = false;
            renderRCAHistoryList();
        }
    }
}

function searchRCAHistory() {
    rcaHistoryListState().query = (document.getElementById('rca-history-query')?.value || '').trim();
    return loadRCAHistory({reset: true});
}

function clearRCAHistorySearch() {
    const input = document.getElementById('rca-history-query');
    if (input) input.value = '';
    rcaHistoryListState().query = '';
    return loadRCAHistory({reset: true});
}

function retryRCAHistory() {
    return loadRCAHistory(rcaHistoryListState().failedAppend ? {append: true} : {reset: true});
}

function resetRCAWorkflowView() {
    document.getElementById('rca-history-retrieval')?.remove();
    document.getElementById('rca-strategy-live')?.remove();
    document.getElementById('rca-hypothesis-review-live')?.remove();
    const progress = document.getElementById('rca-progress');
    if (progress) progress.style.display = 'block';
    const resultCard = document.getElementById('rca-result-card');
    if (resultCard) resultCard.style.display = 'none';
    const ids = ['rca-phases', 'rca-hyp-list', 'rca-evidence-grid', 'rca-skill-runtime-list', 'rca-result-content'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
    });
    ['rca-hypotheses', 'rca-evidence', 'rca-skill-runtime', 'rca-iteration'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });
    const logEl = document.getElementById('rca-log');
    if (logEl) logEl.textContent = '';
    state._pendingJudge = null;
    state._pendingCollaboration = null;
    state._pendingCritique = null;
    state._pendingClosure = null;
    state._pendingRemediation = null;
}

function replayRCAWorkflowRun(run = {}, mode = '') {
    resetRCAWorkflowView();
    state.rcaRunId = run.id || '';
    state.rcaViewMode = mode || '';
    const namespaceInput = document.getElementById('rca-ns');
    if (namespaceInput) namespaceInput.value = run.namespace || '';
    const logEl = document.getElementById('rca-log');
    if (logEl && Array.isArray(run.logs)) logEl.textContent = run.logs.map(zhLogLine).join('\n');
    const visibleEvents = (run.events || []).filter(evt => {
        if (mode === 'multi_agent' && evt?.event === 'skill_runtime') return false;
        return true;
    });
    visibleEvents.forEach(evt => {
        try { handleRCAEvent(evt); } catch (err) { console.warn('Failed to replay RCA event', evt, err); }
    });
    if (run.result) renderRCAFinalResult(run.result);
    const progress = document.getElementById('rca-progress');
    if (progress && (!run.events || !run.events.length) && !run.result) {
        const phases = document.getElementById('rca-phases');
        if (phases) phases.innerHTML = '<span class="phase-badge active">等待工作流事件</span>';
    }
}

async function openRCAHistoryRun(runId, mode = '') {
    const generation = (state.rcaHistoryGeneration || 0) + 1;
    state.rcaHistoryGeneration = generation;
    state.rcaRunId = runId;
    if (state.rcaEventSource) state.rcaEventSource.close();
    closeAlertDispositionModal();
    switchView('rca');
    const query = document.getElementById('rca-query');
    const modeLabel = mode === 'multi_agent' ? '多智能体' : mode === 'agent_skill' ? 'Agent+Skill' : 'RCA';
    if (query && runId) query.value = `正在查看${modeLabel}工作流：${runId}`;
    if (!runId) {
        loadRCAHistory();
        return;
    }
    resetRCAWorkflowView();
    const phases = document.getElementById('rca-phases');
    if (phases) phases.innerHTML = '<span class="phase-badge active">加载 RCA 工作流...</span>';
    const run = await api(`/api/rca/${encodeURIComponent(runId)}?_=${Date.now()}`, { cache: 'no-store', timeoutMs: 20000 });
    if (generation !== state.rcaHistoryGeneration || state.rcaRunId !== runId) return;
    if (!run) {
        if (phases) phases.innerHTML = '<span class="phase-badge failed">RCA 工作流加载失败</span>';
        showToast('RCA 工作流加载失败', 'error');
        return;
    }
    replayRCAWorkflowRun(run, mode);
    loadRCAHistory();
    if (!['completed', 'failed', 'cancelled', 'expired'].includes(run.status)) {
        setTimeout(() => pollRCAHistoryRun(runId, mode, generation), 2500);
    }
}

async function pollRCAHistoryRun(runId, mode, generation) {
    if (generation !== state.rcaHistoryGeneration || state.rcaRunId !== runId || state.currentView !== 'rca') return;
    let run;
    try {
        run = await api(`/api/rca/${encodeURIComponent(runId)}?_=${Date.now()}`, {
            cache: 'no-store', throwOnError: true, suppressErrorLog: true, timeoutMs: 20000,
        });
    } catch (error) {
        if (generation !== state.rcaHistoryGeneration || state.rcaRunId !== runId) return;
        const phases = document.getElementById('rca-phases');
        if (phases && !phases.querySelector('[data-poll-error]')) {
            phases.insertAdjacentHTML('beforeend', `<span data-poll-error class="phase-badge failed">${error?.status === 404 ? '运行记录已过期或服务已重启' : '状态同步失败，正在重试'}</span>`);
        }
        if (error?.status === 404) return;
    }
    if (generation !== state.rcaHistoryGeneration || state.rcaRunId !== runId || state.currentView !== 'rca') return;
    if (run) replayRCAWorkflowRun(run, mode);
    if (['completed', 'failed', 'cancelled', 'expired'].includes(run?.status)) {
        loadRCAHistory({reset:true});
        return;
    }
    setTimeout(() => pollRCAHistoryRun(runId, mode, generation), run ? 2500 : 10000);
}

// ─────────────────────────────────────────
// Daemon
// ─────────────────────────────────────────

async function loadDaemonStatus() {
    const data = await api('/api/daemon/status');
    if (!data) return;
    document.getElementById('daemon-status').innerHTML = data.running ? '<span class="text-success">运行中</span>' : '<span class="text-danger">已停止</span>';
    document.getElementById('daemon-uptime').textContent = data.uptime_s ? `${Math.floor(data.uptime_s / 60)}m ${Math.floor(data.uptime_s % 60)}s` : '-';
    document.getElementById('daemon-cycles').textContent = data.cycles ?? '-';
    document.getElementById('daemon-pipelines').textContent = data.active_pipelines ?? '-';
}

async function startDaemon() {
    await api('/api/daemon/start', { method: 'POST' });
    loadDaemonStatus();
    if (state.daemonLogSSE) state.daemonLogSSE.close();
    const logEl = document.getElementById('daemon-log');
    logEl.textContent = '';
    state.daemonLogSSE = new EventSource('/api/daemon/logs/stream');
    state.daemonLogSSE.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'log') { logEl.textContent += msg.msg + '\n'; logEl.scrollTop = logEl.scrollHeight; }
            else if (msg.type === 'status') { document.getElementById('daemon-cycles').textContent = msg.data?.cycles ?? '-'; document.getElementById('daemon-pipelines').textContent = msg.data?.active_pipelines ?? '-'; }
        } catch {}
    };
}

async function stopDaemon() {
    await api('/api/daemon/stop', { method: 'POST' });
    if (state.daemonLogSSE) { state.daemonLogSSE.close(); state.daemonLogSSE = null; }
    setTimeout(loadDaemonStatus, 1000);
}

// ─────────────────────────────────────────
// Traces (Jaeger / SkyWalking)
// ─────────────────────────────────────────

async function loadTracesView() {
    await loadFaultTargets();
    const clusterId = document.getElementById('trace-cluster')?.value || '';
    const [jaeger, skywalking] = await Promise.all([
        api(`/api/jaeger/services?cluster_id=${encodeURIComponent(clusterId)}`),
        api(`/api/skywalking/services?cluster_id=${encodeURIComponent(clusterId)}`),
    ]);
    _traceSources = { jaeger, skywalking };
    const available = [
        { id: 'skywalking', name: 'SkyWalking', data: skywalking },
        { id: 'jaeger', name: 'Jaeger', data: jaeger },
    ].filter(item => item.data?.services?.length);
    if (!available.some(item => item.id === _traceSource)) {
        _traceSource = available[0]?.id || (skywalking?.selected_url ? 'skywalking' : 'jaeger');
    }
    const sourceSelect = document.getElementById('trace-source');
    if (sourceSelect) {
        sourceSelect.innerHTML = available.length
            ? available.map(item => `<option value="${item.id}">${item.name} · ${item.data.services.length} 个服务</option>`).join('')
            : '<option value="">未发现可用链路后端</option>';
        if (_traceSource) sourceSelect.value = _traceSource;
    }
    selectTraceSource();
}

function selectTraceSource() {
    _traceSource = document.getElementById('trace-source')?.value || _traceSource || 'jaeger';
    const data = _traceSources[_traceSource];
    const sel = document.getElementById('trace-service');
    const source = document.getElementById('trace-source-badge');
    const sourceName = _traceSource === 'skywalking' ? 'SkyWalking' : 'Jaeger';
    if (source) {
        source.textContent = data?.selected_url
            ? `${sourceName} ${data.services?.length || 0} 个服务 · ${data.selected_url}`
            : `${sourceName} 未连接`;
        source.className = `badge badge-${data?.error ? 'warning' : 'info'}`;
    }
    if (data?.services?.length) {
        const current = sel.value;
        const services = _traceSource === 'skywalking'
            ? data.services.map(item => ({ value: item.id, label: item.name }))
            : data.services.filter(Boolean).sort().map(item => ({ value: item, label: item }));
        sel.innerHTML = '<option value="">选择服务</option>' + services.map(item => `<option value="${escapeHtmlAttr(item.value)}" data-service-name="${escapeHtmlAttr(item.label)}" ${item.value === current ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('');
    } else if (data?.error) {
        sel.innerHTML = `<option value="">${sourceName} 连接失败</option>`;
        document.getElementById('trace-empty').style.display = 'block';
        document.getElementById('trace-empty').textContent = `${sourceName} 连接失败: ${data.error}`;
    }
    loadTraceOperations();
}

async function loadTraceOperations() {
    const service = document.getElementById('trace-service')?.value;
    const sel = document.getElementById('trace-operation');
    sel.innerHTML = '<option value="">所有操作</option>';
    if (!service || _traceSource === 'skywalking') return;
    const clusterId = document.getElementById('trace-cluster')?.value || '';
    const data = await api(`/api/jaeger/operations?service=${encodeURIComponent(service)}&cluster_id=${encodeURIComponent(clusterId)}`);
    if (data?.operations?.length) sel.innerHTML += data.operations.map(op => `<option value="${op}">${op}</option>`).join('');
}

async function searchTraces() {
    const service = document.getElementById('trace-service')?.value;
    if (!service) { alert('请先选择服务'); return; }
    const operation = document.getElementById('trace-operation')?.value || '';
    const minDuration = document.getElementById('trace-min-duration')?.value || '';
    const maxDuration = document.getElementById('trace-max-duration')?.value || '';
    const lookback = document.getElementById('trace-lookback')?.value || '1h';
    const limit = document.getElementById('trace-limit')?.value || 20;
    const clusterId = document.getElementById('trace-cluster')?.value || '';
    const serviceName = document.getElementById('trace-service')?.selectedOptions?.[0]?.dataset?.serviceName || service;
    let url = _traceSource === 'skywalking'
        ? `/api/skywalking/traces?service_id=${encodeURIComponent(service)}&service_name=${encodeURIComponent(serviceName)}&cluster_id=${encodeURIComponent(clusterId)}&lookback=${lookback}&limit=${limit}`
        : `/api/jaeger/traces?service=${encodeURIComponent(service)}&cluster_id=${encodeURIComponent(clusterId)}&lookback=${lookback}&limit=${limit}`;
    if (_traceSource === 'jaeger' && operation) url += `&operation=${encodeURIComponent(operation)}`;
    if (_traceSource === 'jaeger' && minDuration) url += `&min_duration=${encodeURIComponent(minDuration)}`;
    if (_traceSource === 'jaeger' && maxDuration) url += `&max_duration=${encodeURIComponent(maxDuration)}`;
    renderTraceTable(await api(url));
}

function renderTraceTable(data) {
    const tbody = document.getElementById('trace-table-body');
    const emptyEl = document.getElementById('trace-empty');
    const countEl = document.getElementById('trace-count');
    if (!data?.traces?.length) {
        tbody.innerHTML = ''; emptyEl.style.display = 'block';
        emptyEl.textContent = data?.error ? `错误: ${data.error}` : '未找到 Trace';
        countEl.textContent = ''; return;
    }
    emptyEl.style.display = 'none';
    countEl.textContent = `共 ${data.traces.length} 条${data.selected_url ? ` · ${data.selected_url}` : ''}`;
    tbody.innerHTML = data.traces.map(t => {
        const durationMs = (t.total_duration_us / 1000).toFixed(1);
        const startTime = t.start_time ? new Date(t.start_time / 1000).toLocaleString('zh-CN') : '-';
        const shortId = t.traceID?.substring(0, 16) || '';
        const services = (t.services || []).slice(0, 3).join(', ');
        const moreServices = t.services?.length > 3 ? ` +${t.services.length - 3}` : '';
        return `<tr>
            <td><code style="font-size:11px">${escapeHtml(shortId)}</code></td>
            <td>${escapeHtml(t.root_service || '-')}</td><td>${escapeHtml(t.root_operation || '-')}</td>
            <td>${t.span_count || '-'}</td><td style="font-size:11px">${escapeHtml(services)}${moreServices}</td>
            <td>${durationMs} ms</td><td style="font-size:11px">${startTime}</td>
            <td><button class="btn btn-sm" onclick="viewTraceDetail('${t.traceID}')">详情</button></td>
        </tr>`;
    }).join('');
}

async function lookupTraceById() {
    const traceId = document.getElementById('trace-id-input')?.value?.trim();
    if (!traceId) { alert('请输入 Trace ID'); return; }
    await viewTraceDetail(traceId);
}

async function viewTraceDetail(traceId) {
    const card = document.getElementById('trace-detail-card');
    const content = document.getElementById('trace-detail-content');
    card.style.display = 'block';
    content.innerHTML = '<p class="text-muted">加载中...</p>';
    const clusterId = document.getElementById('trace-cluster')?.value || '';
    const detailUrl = _traceSource === 'skywalking'
        ? `/api/skywalking/trace/${encodeURIComponent(traceId)}?cluster_id=${encodeURIComponent(clusterId)}`
        : `/api/jaeger/trace/${encodeURIComponent(traceId)}?cluster_id=${encodeURIComponent(clusterId)}`;
    const data = await api(detailUrl);
    if (!data || data.error) { content.innerHTML = `<p class="text-danger">加载失败: ${data?.error || '未知错误'}</p>`; return; }
    const spans = data.spans || [];
    if (!spans.length) { content.innerHTML = '<p class="text-muted">无 Span 数据</p>'; return; }
    const minStart = Math.min(...spans.map(s => s.startTime || Infinity));
    const maxEnd = Math.max(...spans.map(s => (s.startTime || 0) + (s.duration_us || 0)));
    const totalRange = maxEnd - minStart || 1;
    content.innerHTML = `
        <div style="margin-bottom:12px"><strong>Trace ID:</strong> <code>${escapeHtml(traceId)}</code> &nbsp; <strong>Span数:</strong> ${spans.length} &nbsp; <strong>总耗时:</strong> ${((maxEnd - minStart) / 1000).toFixed(1)} ms</div>
        <div class="trace-timeline">
            ${spans.map(s => {
                const left = ((s.startTime - minStart) / totalRange * 100).toFixed(2);
                const width = Math.max((s.duration_us / totalRange * 100), 0.5).toFixed(2);
                const dMs = (s.duration_us / 1000).toFixed(1);
                const hasError = s.tags?.['error'] === true || s.tags?.['otel.status_code'] === 'ERROR';
                const barColor = hasError ? 'var(--danger)' : 'var(--accent)';
                return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px;font-size:11px">
                    <span style="min-width:120px;text-align:right;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.serviceName)}">${escapeHtml(s.serviceName)}</span>
                    <span style="min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.operationName)}">${escapeHtml(s.operationName)}</span>
                    <div style="flex:1;position:relative;height:14px;background:var(--bg-primary);border-radius:3px">
                        <div style="position:absolute;left:${left}%;width:${width}%;height:100%;background:${barColor};border-radius:3px;min-width:2px" title="${dMs} ms"></div>
                    </div>
                    <span style="min-width:60px;text-align:right">${dMs} ms</span>
                </div>`;
            }).join('')}
        </div>
    `;
}

// ─────────────────────────────────────────
// Events
// ─────────────────────────────────────────

async function loadEvents() {
    const ns = document.getElementById('event-ns')?.value || '';
    const data = await api(`/api/cluster/events?namespace=${ns}&limit=100`);
    if (!data?.events) return;
    const tbody = document.querySelector('#event-table tbody');
    tbody.innerHTML = data.events.map(e => `
        <tr>
            <td><span class="badge badge-${e.type === 'Warning' ? 'warning' : 'info'}">${e.type}</span></td>
            <td>${escapeHtml(e.reason)}</td><td>${escapeHtml(e.object)}</td>
            <td>${escapeHtml(e.message?.substring(0, 120) || '')}</td>
            <td>${e.count}</td><td>${formatTime(e.last_seen)}</td>
        </tr>
    `).join('');
}

// ─────────────────────────────────────────
// 配置中心 / 自愈中心
// ─────────────────────────────────────────

let _lastHealSuggestions = [];

function initConfigCenterDirtyTracking() {
    document.querySelectorAll('#view-configcenter input, #view-configcenter select, #view-configcenter textarea').forEach(el => {
        el.addEventListener('input', () => { state.configCenterDirty = true; });
        el.addEventListener('change', () => { state.configCenterDirty = true; });
    });
}

async function loadConfigCenter(options = {}) {
    if (!options.force && state.loadedViews.configcenter && state.configCenterDirty) {
        const out = document.getElementById('configcenter-output');
        if (out && !out.textContent.includes('未保存修改')) {
            out.textContent = `配置中心存在未保存修改，已跳过自动刷新，避免覆盖当前输入。\n\n${out.textContent || ''}`;
        }
        return;
    }
    if (!options.force && state.loadedViews.configcenter && options.navigation) {
        return;
    }
    const data = await api('/api/platform/config');
    await loadFaultTargets();
    const eff = data?.effective || {};
    renderConfigCenterDocs(data?.schema || {});
    const llm = eff.llm || {};
    const det = eff.detection || {};
    const heal = eff.remediation || {};
    const setConfigVal = (id, value) => { const el = document.getElementById(id); if (el) el.value = value ?? ''; };
    const setCheck = (id, value) => { const el = document.getElementById(id); if (el) el.checked = !!value; };
    setConfigVal('cfg-llm-model', llm.model);
    setConfigVal('cfg-llm-base', llm.base_url);
    setConfigVal('cfg-llm-key', '');
    setConfigVal('cfg-llm-temp', llm.temperature);
    setConfigVal('cfg-llm-maxtokens', llm.max_tokens);
    setConfigVal('cfg-llm-timeout', llm.timeout);
    setConfigVal('cfg-det-algo', det.default_algorithm || 'zscore');
    setConfigVal('cfg-det-z', det.default_z_threshold);
    setConfigVal('cfg-det-lookback', det.default_lookback_m);
    setConfigVal('cfg-det-minsamples', det.min_samples);
    setConfigVal('cfg-det-confirm', det.confirmation_points);
    const aiThresholds = det.thresholds?.ai || {};
    setConfigVal('cfg-ai-th-waiting', aiThresholds.waiting_requests ?? 1);
    setConfigVal('cfg-ai-th-ttft', aiThresholds.ttft_p95 ?? 1.0);
    setConfigVal('cfg-ai-th-e2e', aiThresholds.e2e_latency_p95 ?? 3.0);
    setConfigVal('cfg-ai-th-gpumem', aiThresholds.gpu_memory_used ?? 90);
    setConfigVal('cfg-ai-th-nodecpu', aiThresholds.node_cpu_usage ?? 85);
    setConfigVal('cfg-ai-th-nodemem', aiThresholds.node_memory_usage ?? 85);
    setConfigVal('cfg-ai-th-rootfs', aiThresholds.node_rootfs_usage ?? 85);
    setConfigVal('cfg-ai-th-diskread', aiThresholds.node_disk_read ?? 200);
    setConfigVal('cfg-ai-th-diskwrite', aiThresholds.node_disk_write ?? 200);
    setConfigVal('cfg-ai-th-netrx', aiThresholds.node_network_receive ?? 200);
    setConfigVal('cfg-ai-th-nettx', aiThresholds.node_network_transmit ?? 200);
    setCheck('cfg-heal-enabled', heal.enabled);
    setCheck('cfg-heal-recommendonly', heal.recommend_only);
    setCheck('cfg-heal-dryrun', heal.dry_run);
    setCheck('cfg-heal-approval', heal.require_approval);
    setConfigVal('cfg-heal-confidence', heal.confidence_threshold);
    setConfigVal('cfg-heal-maxsteps', heal.max_steps);
    setConfigVal('cfg-heal-risk', heal.max_auto_risk_level || 'medium');
    const out = document.getElementById('configcenter-output');
    if (out) out.textContent = configCenterStatusText(data);
    renderConfigCenterStatusStrip(data);
    renderConfigCenterClusterSummary();
    state.loadedViews.configcenter = true;
    state.configCenterDirty = false;
}

function renderConfigCenterStatusStrip(data) {
    const el = document.getElementById('configcenter-status-strip');
    if (!el) return;
    const eff = data?.effective || {};
    const stored = data?.stored || {};
    const llm = eff.llm || {};
    const rem = eff.remediation || {};
    const generalClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || 'general').toLowerCase() !== 'ai').length;
    const aiClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai').length;
    el.innerHTML = `
        <div><span>持久化</span><strong>${Object.keys(stored).length ? '已写入' : '默认配置'}</strong></div>
        <div><span>大模型</span><strong>${escapeHtml(llm.model || '-')}</strong></div>
        <div><span>API Key</span><strong>${llm.api_key_configured ? '已配置' : '未配置'}</strong></div>
        <div><span>自愈策略</span><strong>${rem.enabled ? '真实执行可用' : '仅建议/预演'}</strong></div>
        <div><span>通算集群</span><strong>${generalClusters}</strong></div>
        <div><span>智算集群</span><strong>${aiClusters}</strong></div>
    `;
}

function renderConfigCenterClusterSummary() {
    const el = document.getElementById('configcenter-cluster-summary');
    if (!el) return;
    const clusters = _faultTargets.k8s_clusters || [];
    const hosts = _faultTargets.llm_hosts || [];
    const generalClusters = clusters.filter(c => String(c.kind || c.type || 'general').toLowerCase() !== 'ai');
    const aiClusters = clusters.filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai');
    const statEls = el.querySelectorAll('.config-cluster-stats span strong');
    if (statEls[0]) statEls[0].textContent = String(generalClusters.length);
    if (statEls[1]) statEls[1].textContent = String(aiClusters.length);
    if (statEls[2]) statEls[2].textContent = String(hosts.length);
}

function configCenterStatusText(data) {
    const eff = data?.effective || {};
    const stored = data?.stored || {};
    const llm = eff.llm || {};
    const det = eff.detection || {};
    const obs = eff.observability || {};
    const rem = eff.remediation || {};
    const aiThresholds = det.thresholds?.ai || {};
    return [
        '配置中心状态',
        `持久化文件: ${Object.keys(stored).length ? '已写入 data/platform_config.json' : '未设置覆盖，使用 YAML/env 默认值'}`,
        `大模型: model=${llm.model || '-'}, base_url=${llm.base_url || '-'}, api_key=${llm.api_key_configured ? '已配置' : '未配置'}, timeout=${llm.timeout || '-'}`,
        `异常检测: algorithm=${det.default_algorithm || '-'}, lookback=${det.default_lookback_m || '-'}m, min_samples=${det.min_samples || '-'}`,
        `智算阈值: waiting=${aiThresholds.waiting_requests ?? 1}, ttft=${aiThresholds.ttft_p95 ?? 1}s, e2e=${aiThresholds.e2e_latency_p95 ?? 3}s, node_cpu=${aiThresholds.node_cpu_usage ?? 85}%, node_mem=${aiThresholds.node_memory_usage ?? 85}%`,
        `可观测源: prometheus=${obs.prometheus_url || '-'}, jaeger=${obs.jaeger_url || '-'}`,
        `自愈策略: enabled=${!!rem.enabled}, recommend_only=${!!rem.recommend_only}, dry_run=${!!rem.dry_run}, approval=${!!rem.require_approval}`,
        '',
        JSON.stringify(eff, null, 2),
    ].join('\n');
}

function renderConfigCenterDocs(schema) {
    const el = document.getElementById('configcenter-docs');
    if (!el) return;
    const sections = Object.entries(schema || {});
    if (!sections.length) {
        el.innerHTML = '<div class="empty-state">暂无参数说明</div>';
        return;
    }
    el.innerHTML = sections.map(([sectionKey, section]) => {
        const fields = Object.entries(section.fields || {}).map(([fieldKey, field]) => `
            <tr>
                <td><code>${escapeHtml(sectionKey)}.${escapeHtml(fieldKey)}</code></td>
                <td>${escapeHtml(field.label || fieldKey)}</td>
                <td>${escapeHtml(formatConfigMeta(field))}</td>
                <td>${escapeHtml(field.description || '')}</td>
            </tr>
        `).join('');
        return `
            <div class="config-doc-card">
                <div class="config-doc-title">
                    <span>${escapeHtml(section.label || sectionKey)}</span>
                    <small>${escapeHtml(section.description || '')}</small>
                </div>
                <table class="config-doc-table">
                    <thead><tr><th>参数</th><th>名称</th><th>默认/范围</th><th>说明</th></tr></thead>
                    <tbody>${fields}</tbody>
                </table>
            </div>
        `;
    }).join('');
}

function formatConfigMeta(field) {
    const parts = [];
    if (field.default !== undefined) parts.push(`默认 ${field.default === '' ? '空' : field.default}`);
    if (field.options) parts.push(`可选 ${field.options.join('/')}`);
    const range = [];
    if (field.min !== undefined) range.push(`>=${field.min}`);
    if (field.max !== undefined) range.push(`<=${field.max}`);
    if (range.length) parts.push(range.join(' '));
    if (field.unit) parts.push(field.unit);
    if (field.risk) parts.push(`风险 ${field.risk}`);
    return parts.join('；');
}

async function saveConfigCenter() {
    const payload = {
        llm: {
            model: document.getElementById('cfg-llm-model')?.value || '',
            base_url: document.getElementById('cfg-llm-base')?.value || '',
            temperature: parseFloat(document.getElementById('cfg-llm-temp')?.value || '0.1'),
            max_tokens: parseInt(document.getElementById('cfg-llm-maxtokens')?.value || '65536', 10),
            timeout: parseInt(document.getElementById('cfg-llm-timeout')?.value || '300', 10),
        },
        detection: {
            default_algorithm: document.getElementById('cfg-det-algo')?.value || 'zscore',
            default_z_threshold: parseFloat(document.getElementById('cfg-det-z')?.value || '3'),
            default_lookback_m: parseInt(document.getElementById('cfg-det-lookback')?.value || '30', 10),
            min_samples: parseInt(document.getElementById('cfg-det-minsamples')?.value || '12', 10),
            confirmation_points: parseInt(document.getElementById('cfg-det-confirm')?.value || '1', 10),
            thresholds: {
                ai: {
                    waiting_requests: parseFloat(document.getElementById('cfg-ai-th-waiting')?.value || '1'),
                    ttft_p95: parseFloat(document.getElementById('cfg-ai-th-ttft')?.value || '1'),
                    e2e_latency_p95: parseFloat(document.getElementById('cfg-ai-th-e2e')?.value || '3'),
                    gpu_memory_used: parseFloat(document.getElementById('cfg-ai-th-gpumem')?.value || '90'),
                    node_cpu_usage: parseFloat(document.getElementById('cfg-ai-th-nodecpu')?.value || '85'),
                    node_memory_usage: parseFloat(document.getElementById('cfg-ai-th-nodemem')?.value || '85'),
                    node_rootfs_usage: parseFloat(document.getElementById('cfg-ai-th-rootfs')?.value || '85'),
                    node_disk_read: parseFloat(document.getElementById('cfg-ai-th-diskread')?.value || '200'),
                    node_disk_write: parseFloat(document.getElementById('cfg-ai-th-diskwrite')?.value || '200'),
                    node_network_receive: parseFloat(document.getElementById('cfg-ai-th-netrx')?.value || '200'),
                    node_network_transmit: parseFloat(document.getElementById('cfg-ai-th-nettx')?.value || '200'),
                },
            },
        },
        remediation: {
            enabled: document.getElementById('cfg-heal-enabled')?.checked === true,
            recommend_only: document.getElementById('cfg-heal-recommendonly')?.checked === true,
            dry_run: document.getElementById('cfg-heal-dryrun')?.checked !== false,
            require_approval: document.getElementById('cfg-heal-approval')?.checked !== false,
            confidence_threshold: parseFloat(document.getElementById('cfg-heal-confidence')?.value || '0.85'),
            max_steps: parseInt(document.getElementById('cfg-heal-maxsteps')?.value || '5', 10),
            max_auto_risk_level: document.getElementById('cfg-heal-risk')?.value || 'medium',
        },
    };
    const apiKey = document.getElementById('cfg-llm-key')?.value || '';
    if (apiKey) payload.llm.api_key = apiKey;
    const res = await api('/api/platform/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });
    const out = document.getElementById('configcenter-output');
    if (out) out.textContent = `保存成功，配置已持久化并重新加载生效。\n\n${configCenterStatusText(res)}`;
    state.configCenterDirty = false;
    state.loadedViews.configcenter = false;
    await loadConfigCenter({ force: true });
}

async function testLLMConnection() {
    const out = document.getElementById('configcenter-output');
    const button = document.getElementById('cfg-test-llm');
    if (state.configCenterDirty) {
        if (out) out.textContent = '当前表单存在未保存修改。请先点击“保存”，再测试当前有效的大模型配置。';
        showToast('请先保存大模型配置', 'warning');
        return;
    }
    if (button) button.disabled = true;
    if (out) out.textContent = '正在由 AgenticSRE 后端使用当前有效配置发起真实大模型请求…';
    try {
        const result = await api('/api/platform/config/test-llm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}',
            timeoutMs: 130000,
            throwOnError: true,
        });
        if (out) out.textContent = [
            '大模型真实调用成功',
            `模型: ${result.model || '-'}`,
            `服务地址: ${result.base_url || '-'}`,
            `请求耗时: ${result.latency_ms ?? '-'} ms`,
            `模型响应: ${result.response_preview || '-'}`,
            `AgenticSRE 使用范围: ${(result.runtime_scope || []).join('、') || '-'}`,
            '',
            result.note || '',
        ].join('\n');
        showToast(`${result.model || '大模型'} 连接测试成功`);
    } catch (error) {
        if (out) out.textContent = `大模型真实调用失败\n${error?.message || error}`;
        showToast('大模型连接测试失败', 'error');
    } finally {
        if (button) button.disabled = false;
    }
}

function healRequestBody() {
    return {
        namespace: document.getElementById('heal-namespace')?.value || 'default',
        message: document.getElementById('heal-message')?.value || '',
        pod: document.getElementById('heal-pod')?.value || '',
        node: document.getElementById('heal-node')?.value || '',
        dry_run: document.getElementById('heal-dryrun')?.checked !== false,
    };
}

function loadSkillOps() {
    loadSkillCatalog();
    loadSkillWorkflowRuns();
    switchSkillOpsTab(state.skillOpsTab || 'run', { silent: true });
    renderSkillOpsLiveSummary();
    if (!state.lastSkillWorkflow) {
        renderSkillFlowStrip();
        const status = document.getElementById('skill-result-status');
        if (status) {
            status.className = 'badge badge-gray';
            status.textContent = '未运行';
        }
    }
}

function switchSkillOpsTab(tab, options = {}) {
    const selected = tab || 'run';
    state.skillOpsTab = selected;
    document.querySelectorAll('.skillops-tab').forEach(el => {
        el.classList.toggle('active', el.dataset.skillTab === selected);
    });
    document.querySelectorAll('.skillops-panel').forEach(el => {
        el.classList.toggle('active', el.id === `skillops-tab-${selected}`);
    });
    if (!options.silent) {
        if (selected === 'catalog') loadSkillCatalog();
        if (selected === 'history') loadSkillWorkflowRuns();
    }
    renderSkillOpsLiveSummary();
}

async function loadSkillCatalog(force = false) {
    if (state.skillCatalogLoaded && !force) return;
    const select = document.getElementById('skill-fault-type');
    const current = select?.value || '';
    const data = await api('/api/skill-workflows/skills');
    const skills = Array.isArray(data?.skills) ? data.skills : [];
    if (!skills.length) return;
    state.skillCatalog = skills;
    const options = [];
    const seen = new Set();
    skills.forEach(skill => {
        const faultType = skillFaultTypeFromMetadata(skill);
        if (!faultType || seen.has(faultType)) return;
        seen.add(faultType);
        options.push({
            value: faultType,
            label: `${faultType} (${skill.id || skill.name || 'skill'})`,
            domain: skill.domain || '',
        });
    });
    if (select && options.length) {
        options.sort((a, b) => a.value.localeCompare(b.value));
        select.innerHTML = options.map(o => `<option value="${escapeHtml(o.value)}" data-domain="${escapeHtml(o.domain)}">${escapeHtml(o.label)}</option>`).join('');
        if (current && options.some(o => o.value === current)) {
            select.value = current;
        }
    }
    renderSkillCatalogFilters(skills);
    renderSkillCatalog();
    renderSkillOpsLiveSummary();
    state.skillCatalogLoaded = true;
}

function renderSkillCatalogFilters(skills = state.skillCatalog || []) {
    const domainSelect = document.getElementById('skill-catalog-domain');
    if (!domainSelect) return;
    const current = domainSelect.value || '';
    const domains = Array.from(new Set(skills.map(s => s.domain || 'generic'))).sort();
    domainSelect.innerHTML = '<option value="">全部领域</option>' + domains.map(d => `<option value="${escapeHtml(d)}">${escapeHtml(skillDomainLabel(d))}</option>`).join('');
    if (current && domains.includes(current)) domainSelect.value = current;
}

function renderSkillCatalog() {
    const grid = document.getElementById('skill-catalog-grid');
    const summary = document.getElementById('skill-catalog-summary');
    if (!grid || !summary) return;
    const skills = state.skillCatalog || [];
    const query = (document.getElementById('skill-catalog-search')?.value || '').trim().toLowerCase();
    const domain = document.getElementById('skill-catalog-domain')?.value || '';
    const purpose = document.getElementById('skill-catalog-purpose')?.value || '';
    const risk = document.getElementById('skill-catalog-risk')?.value || '';
    state.skillCatalogPurpose = purpose;
    const filtered = skills.filter(skill => {
        const text = [
            skill.id,
            skill.name,
            skill.description,
            skill.domain,
            skill.risk,
            ...(skill.tags || []),
            ...(skill.permissions || []),
            ...(skill.tool_dependencies || []),
        ].join(' ').toLowerCase();
        if (query && !text.includes(query)) return false;
        if (domain && (skill.domain || 'generic') !== domain) return false;
        if (purpose && skillPurposeKey(skill) !== purpose && !(purpose === 'heal' && (skill.stages || []).includes('heal'))) return false;
        if (risk && (skill.risk || 'medium') !== risk) return false;
        return true;
    });
    summary.innerHTML = renderSkillCatalogSummary(skills, filtered);
    grid.innerHTML = filtered.length
        ? filtered.map(renderSkillCatalogCard).join('')
        : '<p class="text-muted">没有匹配的 Skill。</p>';
}

function renderSkillCatalogSummary(allSkills = [], filtered = []) {
    const domains = new Set(allSkills.map(s => s.domain || 'generic'));
    const purposeCounts = allSkills.reduce((acc, skill) => {
        const key = skillPurposeKey(skill);
        acc[key] = (acc[key] || 0) + 1;
        return acc;
    }, {});
    const healReady = allSkills.filter(s => (s.stages || []).includes('heal')).length;
    const complete = allSkills.filter(s => s.contract_status?.is_complete).length;
    return `
        <div><span>总数</span><strong>${allSkills.length}</strong></div>
        <div><span>当前筛选</span><strong>${filtered.length}</strong></div>
        <div><span>领域</span><strong>${domains.size}</strong></div>
        <div><span>完整 Skill</span><strong>${complete}</strong></div>
        <div><span>场景诊断</span><strong>${purposeCounts.domain || 0}</strong></div>
        <div><span>含自愈</span><strong>${healReady}</strong></div>
    `;
}

function renderSkillOpsLiveSummary() {
    const el = document.getElementById('skillops-live-summary');
    if (!el) return;
    const skills = state.skillCatalog || [];
    if (!skills.length) {
        el.innerHTML = `
            <div class="skillops-live-card muted"><span>能力目录</span><strong>加载中</strong><small>等待 Runtime 返回 Skill 列表</small></div>
            <div class="skillops-live-card muted"><span>闭环状态</span><strong>未运行</strong><small>运行调试后显示最近结果</small></div>
            <div class="skillops-live-card muted"><span>推荐入口</span><strong>告警 / 事件</strong><small>生产链路从事件进入</small></div>
        `;
        return;
    }
    const complete = skills.filter(s => s.contract_status?.is_complete).length;
    const llm = skills.filter(s => (s.domain || '') === 'llm').length;
    const k8s = skills.filter(s => (s.domain || '') === 'k8s').length;
    const workflow = skills.filter(s => skillPurposeKey(s) === 'workflow').length;
    const heal = skills.filter(s => (s.stages || []).includes('heal')).length;
    const last = state.lastSkillWorkflow || {};
    const runtime = last.skill_runtime || {};
    const diagnosis = last.diagnosis || {};
    const lastLabel = runtime.primary_skill_id || diagnosis.skill_id || '未运行';
    el.innerHTML = `
        <button class="skillops-live-card" onclick="switchSkillOpsTab('catalog')">
            <span>能力覆盖</span><strong>${complete}/${skills.length}</strong><small>K8s ${k8s} · 智算 ${llm}</small>
        </button>
        <button class="skillops-live-card" onclick="switchSkillOpsTab('catalog'); setSkillCatalogPurposeFilter('workflow')">
            <span>通用编排</span><strong>${workflow}</strong><small>检测、证据、融合、门禁、验证</small>
        </button>
        <button class="skillops-live-card" onclick="switchSkillOpsTab('catalog'); setSkillCatalogPurposeFilter('heal')">
            <span>自愈能力</span><strong>${heal}</strong><small>默认 Dry-run 与门禁控制</small>
        </button>
        <button class="skillops-live-card" onclick="switchSkillOpsTab('history')">
            <span>最近闭环</span><strong>${escapeHtml(shortText(lastLabel, 28))}</strong><small>${last.workflow_run_id ? '已写入审计记录' : '暂无运行记录'}</small>
        </button>
    `;
}

function setSkillCatalogPurposeFilter(purpose) {
    const select = document.getElementById('skill-catalog-purpose');
    if (select) select.value = purpose || '';
    renderSkillCatalog();
}

function renderSkillCatalogCard(skill = {}) {
    const stages = skill.stages || [];
    const tags = skill.tags || [];
    const permissions = skill.permissions || [];
    const dependencies = skill.tool_dependencies || [];
    const sourceStatus = skill.source_status || {};
    const contractStatus = skill.contract_status || {};
    const domain = skill.domain || 'generic';
    const icon = skillCatalogIcon(skill);
    const helper = isHelperSkill(skill);
    const displayName = skillDisplayName(skill);
    const displayDescription = skillDisplayDescription(skill);
    return `<article class="skill-catalog-card ${escapeHtml(domain)} ${helper ? 'helper' : ''}">
        <div class="skill-catalog-head">
            ${skillCatalogIconMarkup(icon)}
            <div>
                <strong>${escapeHtml(displayName || skill.id || '-')}</strong>
                <code>${escapeHtml(skill.id || '-')}</code>
            </div>
            <span class="skill-catalog-category">${escapeHtml(skill.category || skillCategoryLabel(skill))}</span>
        </div>
        <div class="skill-catalog-badges">
            <span class="badge badge-info">${escapeHtml(skillDomainLabel(skill.domain))}</span>
            <span class="badge badge-${skillRiskBadge(skill.risk)}">${escapeHtml(riskLabel(skill.risk))}</span>
            <span class="badge badge-${skill.requires_approval ? 'warning' : 'success'}">${skill.requires_approval ? '需审批' : '免审批'}</span>
            <span class="badge badge-${skillContractBadge(contractStatus)}">${escapeHtml(contractStatus.label || '契约未知')}</span>
        </div>
        <div class="skill-stage-pills">${stages.map(s => `<span>${escapeHtml(skillStageLabel(s))}</span>`).join('')}</div>
        <p>${escapeZh(displayDescription || '暂无描述。')}</p>
        <div class="skill-catalog-metrics">
            <div><span>自愈阈值</span><strong>${escapeHtml(formatConfidence(skill.min_confidence_for_heal))}</strong></div>
            <div><span>证据源</span><strong>${escapeHtml(skill.min_evidence_sources || 1)}</strong></div>
            <div><span>入口</span><strong title="${escapeHtmlAttr(skill.entrypoint || 'skill.py')}">${escapeHtml(shortText(skill.entrypoint || 'skill.py', 16))}</strong></div>
        </div>
        <div class="skill-catalog-actions">
            <button class="btn btn-sm btn-primary" onclick="openSkillDetailModal('${escapeHtmlAttr(skill.id || '')}')">查看详情</button>
            <button class="btn btn-sm" onclick="fillSkillWorkflowFromCatalog('${escapeHtmlAttr(skill.id || '')}')">填入调试</button>
        </div>
        <details class="skill-catalog-more">
            <summary>工具与权限摘要</summary>
            <div class="skill-catalog-tags">
                <span>${helper ? '辅助能力' : '诊断能力'}</span>
                <span>${escapeHtml(sourceStatus.documentation_label || (sourceStatus.has_markdown ? '完整说明' : '元数据生成'))}</span>
                <span>${escapeHtml(sourceStatus.source_of_truth || contractStatus.source_of_truth || 'skill.yaml')}</span>
            </div>
            ${contractStatus.missing_sections?.length ? `<div class="skill-catalog-meta"><span>缺少章节 ${escapeHtml(contractStatus.missing_sections.slice(0, 3).join(', '))}</span></div>` : ''}
            ${contractStatus.metadata_warnings?.length ? `<div class="skill-catalog-meta"><span>元数据覆盖 ${escapeHtml(contractStatus.metadata_warnings.length)} 项</span></div>` : ''}
            ${tags.length ? `<div class="skill-catalog-tags">${tags.slice(0, 5).map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : ''}
            <div class="skill-catalog-meta">
                ${dependencies.length ? `<span>工具 ${escapeHtml(dependencies.slice(0, 2).join(', '))}</span>` : ''}
                ${permissions.length ? `<span>权限 ${escapeHtml(permissions.slice(0, 2).join(', '))}</span>` : ''}
                ${!dependencies.length && !permissions.length ? '<span>无额外工具依赖</span>' : ''}
            </div>
        </details>
    </article>`;
}

function skillContractBadge(contract = {}) {
    if (contract.is_complete) return 'success';
    if (contract.maturity === 'runtime_with_partial_contract') return 'warning';
    if (contract.maturity === 'runtime_only') return 'info';
    return 'gray';
}

function fillSkillWorkflowFromCatalog(skillId) {
    const skill = (state.skillCatalog || []).find(item => item.id === skillId);
    if (!skill) return;
    setVal('skill-domain', skill.domain || 'generic');
    setVal('skill-fault-type', skillFaultTypeFromMetadata(skill));
    setVal('skill-namespace', skill.domain === 'llm' ? 'llm-k8s' : 'default');
    setVal('skill-deployment', skill.domain === 'llm' ? 'vllm-server' : 'demo-service');
    setVal('skill-service', skill.domain === 'llm' ? 'vllm-server' : 'demo-service');
    setVal('skill-message', `${skillDisplayName(skill)}: ${skillDisplayDescription(skill)}`);
    switchSkillOpsTab('run', { silent: true });
    renderSkillOpsLiveSummary();
}

async function openSkillDetailModal(skillId) {
    const modal = document.getElementById('skill-detail-modal');
    const body = document.getElementById('skill-detail-modal-body');
    const title = document.getElementById('skill-detail-title');
    const subtitle = document.getElementById('skill-detail-subtitle');
    if (!modal || !body || !skillId) return;
    modal.style.display = 'flex';
    body.innerHTML = '<div class="skill-empty">正在加载 Skill 详情...</div>';
    try {
        const detail = state.skillDetailCache[skillId]
            || await api(`/api/skill-workflows/skills/${encodeURIComponent(skillId)}`, { timeoutMs: 20000 });
        state.skillDetailCache[skillId] = detail;
        const metadata = detail.metadata || {};
        if (title) title.textContent = skillDisplayName(metadata) || metadata.name || skillId;
        if (subtitle) {
            const source = detail.markdown_source === 'SKILL.md' ? 'SKILL.md 原文' : '由 skill.yaml 生成 Markdown';
            subtitle.textContent = `${metadata.id || skillId} · ${skillDomainLabel(metadata.domain)} · ${source}`;
        }
        body.innerHTML = renderSkillDetailModalBody(detail);
    } catch (err) {
        body.innerHTML = `<div class="alert alert-error">加载 Skill 详情失败：${escapeHtml(err.message || String(err))}</div>`;
    }
}

function closeSkillDetailModal() {
    const modal = document.getElementById('skill-detail-modal');
    if (modal) modal.style.display = 'none';
}

function renderSkillDetailModalBody(detail = {}) {
    const skill = detail.metadata || {};
    const stages = skill.stages || [];
    const tags = skill.tags || [];
    const permissions = skill.permissions || [];
    const dependencies = skill.tool_dependencies || [];
    const files = detail.files || {};
    const sourceStatus = detail.source_status || {};
    const contractStatus = detail.contract_status || {};
    const sourceFiles = detail.source_files || {};
    const frontmatter = detail.frontmatter || {};
    const markdown = detail.markdown || '';
    const yamlText = detail.yaml || '';
    const pythonText = detail.python || '';
    return `
        <div class="skill-detail-shell">
            <aside class="skill-detail-aside">
                <div class="skill-detail-id">
                    ${skillCatalogIconMarkup(skillCatalogIcon(skill))}
                    <div>
                        <strong>${escapeHtml(skillDisplayName(skill) || skill.name || skill.id || '-')}</strong>
                        <code>${escapeHtml(skill.id || '-')}</code>
                    </div>
                </div>
                <div class="skill-detail-kpis">
                    <div><span>领域</span><strong>${escapeHtml(skillDomainLabel(skill.domain))}</strong></div>
                    <div><span>风险</span><strong>${escapeHtml(riskLabel(skill.risk))}</strong></div>
                    <div><span>审批</span><strong>${skill.requires_approval ? '需要' : '不需要'}</strong></div>
                    <div><span>自动执行</span><strong>${skill.safe_auto_execute ? '允许' : '禁止'}</strong></div>
                </div>
                <div class="skill-detail-section">
                    <h4>Skill 契约</h4>
                    <div class="skill-detail-contract">
                        <span class="badge badge-${skillContractBadge(contractStatus)}">${escapeHtml(contractStatus.label || '契约未知')}</span>
                        <span>${escapeHtml(contractStatus.source_of_truth || sourceStatus.source_of_truth || 'skill.yaml')}</span>
                        <span>入口：${escapeHtml(contractStatus.entrypoint || skill.entrypoint || 'skill.py')}</span>
                    </div>
                    ${contractStatus.missing_sections?.length ? `<div class="skill-detail-list skill-source-warning"><span>缺少章节：${escapeHtml(contractStatus.missing_sections.join('、'))}</span></div>` : ''}
                    ${contractStatus.metadata_warnings?.length ? `<div class="skill-detail-list skill-source-warning">${contractStatus.metadata_warnings.map(item => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
                </div>
                <div class="skill-detail-section">
                    <h4>执行阶段</h4>
                    <div class="skill-stage-pills">${stages.map(s => `<span>${escapeHtml(skillStageLabel(s))}</span>`).join('') || '<span>未声明</span>'}</div>
                </div>
                <div class="skill-detail-section">
                    <h4>置信度门禁</h4>
                    <div class="skill-detail-list">
                        <span>自愈阈值：${escapeHtml(formatConfidence(skill.min_confidence_for_heal))}</span>
                        <span>自动阈值：${escapeHtml(formatConfidence(skill.min_confidence_for_auto_heal))}</span>
                        <span>最少证据源：${escapeHtml(skill.min_evidence_sources || 1)}</span>
                    </div>
                </div>
                <div class="skill-detail-section">
                    <h4>权限与依赖</h4>
                    <div class="skill-catalog-tags">
                        ${(permissions.length ? permissions : ['无额外权限']).map(t => `<span>${escapeHtml(t)}</span>`).join('')}
                    </div>
                    <div class="skill-catalog-tags">
                        ${(dependencies.length ? dependencies : ['无额外工具依赖']).map(t => `<span>${escapeHtml(t)}</span>`).join('')}
                    </div>
                </div>
                <div class="skill-detail-section">
                    <h4>标签</h4>
                    <div class="skill-catalog-tags">${(tags.length ? tags : ['未标注']).map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>
                </div>
                <div class="skill-detail-section">
                    <h4>文件</h4>
                    <div class="skill-detail-list">
                        <span>${escapeHtml(files.skill_dir || '-')}</span>
                        <span>${escapeHtml(sourceStatus.documentation_label || detail.markdown_source || '-')}</span>
                        ${sourceStatus.truncated ? '<span class="skill-source-warning">源码内容过长，已按后端上限截断展示。</span>' : ''}
                    </div>
                </div>
            </aside>
            <section class="skill-detail-main">
                <div class="skill-detail-tabs">
                    <button class="active" data-skill-detail-tab="doc" onclick="switchSkillDetailTab('doc')">说明</button>
                    <button data-skill-detail-tab="yaml" onclick="switchSkillDetailTab('yaml')">skill.yaml</button>
                    <button data-skill-detail-tab="frontmatter" onclick="switchSkillDetailTab('frontmatter')">frontmatter</button>
                    <button data-skill-detail-tab="python" onclick="switchSkillDetailTab('python')">skill.py</button>
                    <button data-skill-detail-tab="meta" onclick="switchSkillDetailTab('meta')">元数据</button>
                </div>
                <div class="skill-detail-pane active" id="skill-detail-pane-doc">${renderSimpleMarkdown(markdown || '暂无 Markdown 说明。')}</div>
                <div class="skill-detail-pane" id="skill-detail-pane-yaml">${renderSkillSourcePanel(yamlText, 'skill.yaml', sourceFiles.yaml)}</div>
                <div class="skill-detail-pane" id="skill-detail-pane-frontmatter"><pre class="skill-source-view">${escapeHtml(Object.keys(frontmatter).length ? JSON.stringify(frontmatter, null, 2) : 'SKILL.md 未声明 YAML frontmatter')}</pre></div>
                <div class="skill-detail-pane" id="skill-detail-pane-python">${renderSkillSourcePanel(pythonText, 'skill.py', sourceFiles.python)}</div>
                <div class="skill-detail-pane" id="skill-detail-pane-meta"><pre class="skill-source-view">${escapeHtml(JSON.stringify(skill, null, 2))}</pre></div>
            </section>
        </div>
    `;
}

function renderSkillSourcePanel(text, filename, info = {}) {
    const exists = info.exists !== false && Boolean(text);
    const size = Number(info.size_bytes || 0);
    const readChars = Number(info.read_chars || String(text || '').length);
    return `
        <div class="skill-source-head">
            <strong>${escapeHtml(filename)}</strong>
            <span>${exists ? `${formatBytes(size)} · 已读 ${readChars.toLocaleString()} 字符` : '未找到文件'}</span>
            ${info.truncated ? '<em>已截断</em>' : ''}
        </div>
        <pre class="skill-source-view">${escapeHtml(text || `未找到 ${filename}`)}</pre>
    `;
}

function formatBytes(bytes) {
    const n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function switchSkillDetailTab(tab) {
    document.querySelectorAll('[data-skill-detail-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.skillDetailTab === tab);
    });
    document.querySelectorAll('.skill-detail-pane').forEach(pane => {
        pane.classList.toggle('active', pane.id === `skill-detail-pane-${tab}`);
    });
}

function renderSimpleMarkdown(markdown) {
    const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let inCode = false;
    let codeLines = [];
    let inList = false;
    const flushList = () => {
        if (inList) {
            html.push('</ul>');
            inList = false;
        }
    };
    const flushCode = () => {
        html.push(`<pre class="skill-source-view"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = [];
    };
    lines.forEach(line => {
        if (line.trim().startsWith('```')) {
            if (inCode) {
                inCode = false;
                flushCode();
            } else {
                flushList();
                inCode = true;
                codeLines = [];
            }
            return;
        }
        if (inCode) {
            codeLines.push(line);
            return;
        }
        const trimmed = line.trim();
        if (!trimmed) {
            flushList();
            return;
        }
        const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
            flushList();
            const level = Math.min(4, heading[1].length + 1);
            html.push(`<h${level}>${renderMarkdownInline(heading[2])}</h${level}>`);
            return;
        }
        const bullet = trimmed.match(/^[-*]\s+(.+)$/);
        if (bullet) {
            if (!inList) {
                html.push('<ul>');
                inList = true;
            }
            html.push(`<li>${renderMarkdownInline(bullet[1])}</li>`);
            return;
        }
        flushList();
        html.push(`<p>${renderMarkdownInline(trimmed)}</p>`);
    });
    if (inCode) flushCode();
    flushList();
    return `<div class="skill-markdown-view">${html.join('')}</div>`;
}

function renderMarkdownInline(text) {
    return escapeHtml(text)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function skillCatalogIcon(skill = {}) {
    if (typeof skill === 'string') return skill || 'k8s';
    if (skill.icon) return String(skill.icon).toLowerCase();
    const id = String(skill.id || '').toLowerCase();
    const domain = String(skill.domain || '').toLowerCase();
    if (id.includes('knowledge') || id.includes('retrieval')) return 'search';
    if (id.includes('fusion')) return 'fuse';
    if (id.includes('slo') || id.includes('observability')) return 'activity';
    if (domain === 'llm' || id.includes('vllm') || id.includes('gpu')) return 'gpu';
    if (domain === 'db' || id.includes('db')) return 'database';
    if (id.startsWith('tool.')) return 'terminal';
    if (id.includes('node')) return 'server';
    if (id.includes('service')) return 'service';
    if (id.includes('pod') || id.includes('oom')) return 'box';
    if (id.includes('dns') || id.includes('network')) return 'network';
    if (id.includes('risk') || id.includes('gatekeeper')) return 'shield';
    if (id.includes('verifier') || id.includes('verify')) return 'check';
    return 'k8s';
}

function skillCatalogIconMarkup(icon) {
    const key = skillCatalogIcon(icon);
    const icons = {
        activity: '<path d="M4 12h4l2-6 4 12 2-6h4"/>',
        box: '<path d="M21 8 12 3 3 8l9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
        check: '<path d="m5 13 4 4L19 7"/>',
        database: '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v14c0 1.7 3.1 3 7 3s7-1.3 7-3V5"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
        fuse: '<path d="M7 7h4v4H7z"/><path d="M13 13h4v4h-4z"/><path d="M11 9h2a2 2 0 0 1 2 2v2"/><path d="M4 20h16"/><path d="M4 4h16"/>',
        gpu: '<rect x="5" y="7" width="14" height="10" rx="2"/><path d="M9 11h6v2H9z"/><path d="M8 3v4M12 3v4M16 3v4M8 17v4M12 17v4M16 17v4M3 10h2M3 14h2M19 10h2M19 14h2"/>',
        k8s: '<path d="M12 2 4 6v8c0 4 3 7 8 8 5-1 8-4 8-8V6l-8-4Z"/><path d="M12 8v8M8.5 10l7 4M15.5 10l-7 4"/>',
        network: '<circle cx="6" cy="12" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="m8.7 10.7 6.6-3.4M8.7 13.3l6.6 3.4"/>',
        search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
        server: '<rect x="4" y="4" width="16" height="6" rx="2"/><rect x="4" y="14" width="16" height="6" rx="2"/><path d="M8 7h.01M8 17h.01"/>',
        service: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><circle cx="12" cy="12" r="5"/>',
        shield: '<path d="M12 2 5 5v6c0 5 3 9 7 11 4-2 7-6 7-11V5l-7-3Z"/><path d="m9 12 2 2 4-5"/>',
        terminal: '<path d="m4 7 5 5-5 5"/><path d="M12 19h8"/>',
    };
    return `<span class="skill-catalog-icon" aria-hidden="true"><svg viewBox="0 0 24 24" role="img">${icons[key] || icons.k8s}</svg></span>`;
}

function skillCategoryLabel(skill = {}) {
    const id = String(skill.id || '').toLowerCase();
    const tags = (skill.tags || []).map(t => String(t).toLowerCase());
    if ((skill.stages || []).includes('heal')) return '自愈';
    if (id.includes('knowledge') || tags.includes('knowledge')) return '知识检索';
    if (id.includes('fusion')) return '诊断融合';
    if (id.includes('slo')) return '业务 SLO';
    if (tags.includes('tool')) return '工具';
    if (tags.includes('verification')) return '验证';
    if (tags.includes('gatekeeper') || tags.includes('risk')) return '风控';
    if ((skill.stages || []).includes('diagnose')) return '诊断';
    if ((skill.stages || []).includes('detect')) return '检测';
    return '通用';
}

function isHelperSkill(skill = {}) {
    const id = String(skill.id || '').toLowerCase();
    const tags = (skill.tags || []).map(t => String(t).toLowerCase());
    return id.includes('gatekeeper') || id.includes('verifier') || tags.some(t => ['tool', 'risk', 'gatekeeper', 'verification'].includes(t));
}

function skillPurposeKey(skill = {}) {
    const id = String(skill.id || '').toLowerCase();
    const tags = (skill.tags || []).map(t => String(t).toLowerCase());
    if (id.startsWith('tool.') || tags.includes('tool')) return 'tool';
    if (id.startsWith('generic.') || ['fusion', 'gatekeeper', 'verification', 'evidence', 'planner'].some(t => tags.includes(t))) return 'workflow';
    return 'domain';
}

const SKILL_DISPLAY_OVERRIDES = {
    'generic.evidence_collection': ['通用证据采集', '从事件、指标、日志和上下文中采集诊断证据，为场景专用 skill 提供输入。'],
    'generic.fault_detection': ['通用故障检测', '根据故障文本、告警、指标、事件和症状生成初步故障信号。'],
    'generic.healing_planner': ['通用自愈计划', '生成带审批和风险控制的恢复计划，避免直接执行高风险动作。'],
    'generic.recovery_verifier': ['恢复验证', '在自愈后检查服务端点、rollout 状态、延迟、错误率和 LLM 指标。'],
    'generic.risk_gatekeeper': ['风险门禁', '对恢复动作进行风险、审批、dry-run、环境和回滚条件检查。'],
    'generic.root_cause_diagnosis': ['通用根因诊断', '将症状和证据映射为可能根因，补充 LLM、数据库和依赖故障判断。'],
    'k8s.config_missing': ['K8s 配置缺失', '检测、诊断并给出 ConfigMap、Secret 或配置 key 缺失的受控恢复建议。'],
    'k8s.control_plane_unhealthy': ['K8s 控制面异常', '检测 API Server、etcd、kube-proxy、kubelet 等控制面和节点控制组件异常。'],
    'k8s.deployment_unhealthy': ['K8s 工作负载异常', '检测 Deployment 不可用、可用副本不足和 rollout 异常，并生成恢复建议。'],
    'k8s.dns_resolution': ['K8s DNS 解析异常', '诊断 CoreDNS 或 kube-dns 解析失败，并生成受控恢复建议。'],
    'k8s.hpa_scaling_failed': ['K8s HPA 扩缩容失败', '检测 HPA 扩缩容异常、指标缺失和副本数不达预期问题。'],
    'k8s.image_pull_error': ['K8s 镜像拉取失败', '诊断 ImagePullBackOff 和 ErrImagePull，并给出镜像仓库、凭据和镜像名排查建议。'],
    'k8s.legacy_recipe_bridge': ['旧版自愈知识桥接', '将旧版 Kubernetes 故障类型映射到已验证自愈知识库，保证迁移期间能力连续。'],
    'k8s.network_policy_deny': ['K8s 网络策略阻断', '诊断 NetworkPolicy 或 NetworkChaos 流量阻断问题。'],
    'k8s.node_not_ready': ['K8s 节点 NotReady', '诊断 NodeNotReady 和节点条件异常，并给出受控恢复建议。'],
    'k8s.observability_signal': ['K8s 观测信号诊断', '基于指标异常、TargetDown、时延和错误率生成只读调查计划。'],
    'k8s.oom_killed': ['K8s OOMKilled', '检测 OOMKilled 或退出码 137 的容器，并生成资源调整建议。'],
    'k8s.pod_crashloop': ['K8s Pod CrashLoop', '检测、诊断并生成 CrashLoopBackOff Pod 的受控恢复建议。'],
    'k8s.resource_pressure': ['K8s 资源压力', '诊断 CPU、内存、磁盘压力等资源瓶颈。'],
    'k8s.resource_quota_exceeded': ['K8s 资源配额超限', '检测 ResourceQuota 导致的调度或创建失败，并给出只读恢复建议。'],
    'k8s.scheduling_failed': ['K8s 调度失败', '诊断资源不足、污点、亲和性和调度约束导致的 Pending 问题。'],
    'k8s.service_unavailable': ['K8s Service 不可用', '检测、诊断并生成 Kubernetes Service 无可用 Endpoint 的恢复建议。'],
    'k8s.volume_mount_failed': ['K8s 卷挂载失败', '检测卷挂载、PVC 绑定和存储引用异常，并生成恢复建议。'],
    'llm.k8s_vllm_observability': ['智算 vLLM 观测', '基于 vLLM 指标和 Kubernetes 运行状态诊断智算服务异常。'],
    'llm.vllm_process_crash': ['vLLM 进程崩溃', '检测、诊断并生成 vLLM 推理进程崩溃后的恢复建议。'],
    'tool.db_connection_pool': ['数据库连接池诊断工具', '只读诊断数据库连接池饱和、慢查询和锁等待问题。'],
    'tool.k8s_readiness': ['K8s 就绪检查工具', '只读检查 Kubernetes 就绪状态、Pod、Service、Deployment、事件和 Endpoint。'],
    'tool.llm_host_readiness': ['智算主机就绪检查工具', '只读检查裸机 GPU 和 vLLM 主机状态。'],
};

function skillDisplayName(skill = {}) {
    const override = SKILL_DISPLAY_OVERRIDES[String(skill.id || '')];
    return override?.[0] || skill.name || skill.id || '';
}

function skillDisplayDescription(skill = {}) {
    const override = SKILL_DISPLAY_OVERRIDES[String(skill.id || '')];
    return override?.[1] || skill.description || '';
}

function skillDomainLabel(value) {
    const labels = { k8s: 'K8s', llm: '智算', db: '数据库', generic: '通用' };
    return labels[value] || value || '通用';
}

function skillRiskBadge(value) {
    const key = String(value || 'medium').toLowerCase();
    return key === 'low' ? 'success' : key === 'medium' ? 'warning' : 'danger';
}

function skillStageLabel(value) {
    const labels = { detect: '检测', diagnose: '诊断', heal: '自愈', verify: '验证' };
    return labels[value] || value;
}

function skillFaultTypeFromMetadata(skill = {}) {
    if (skill.fault_type) return String(skill.fault_type);
    const id = String(skill.id || '');
    if (id.includes('.')) return id.split('.').slice(1).join('_');
    if (id) return id.replace(/^k8s[_-]/, '').replace(/^llm[_-]/, '');
    return '';
}

function syncSkillDomainFromFaultType() {
    const selected = document.getElementById('skill-fault-type')?.selectedOptions?.[0];
    const domain = selected?.dataset?.domain;
    if (domain) setVal('skill-domain', domain);
}

function fillSkillWorkflowExample(kind) {
    const examples = {
        pod_crashloop: {
            domain: 'k8s',
            fault_type: 'pod_crashloop',
            namespace: 'default',
            deployment: 'checkout',
            service: '',
            pod: '',
            node: '',
            message: 'Pod 频繁重启，事件中出现 CrashLoopBackOff，业务错误率升高。',
        },
        service_unavailable: {
            domain: 'k8s',
            fault_type: 'service_unavailable',
            namespace: 'default',
            deployment: 'frontend',
            service: 'frontend',
            pod: '',
            node: '',
            message: 'Service 无可用 endpoint，前端访问返回 503，疑似 selector 或后端 Pod 异常。',
        },
        image_pull: {
            domain: 'k8s',
            fault_type: 'image_pull_error',
            namespace: 'default',
            deployment: 'recommendation',
            service: '',
            pod: '',
            node: '',
            message: '新版本发布后 Pod 停留在 ImagePullBackOff，镜像拉取失败。',
        },
        llm_vllm: {
            domain: 'llm',
            fault_type: 'vllm_process_crash',
            namespace: 'default',
            deployment: 'vllm-server',
            service: 'vllm-server',
            pod: '',
            node: '33.33.33.127',
            message: 'vLLM 推理请求失败，OpenAI 兼容接口不可用，GPU 主机上服务进程疑似退出。',
        },
    };
    const ex = examples[kind] || examples.pod_crashloop;
    setVal('skill-domain', ex.domain);
    setVal('skill-fault-type', ex.fault_type);
    setVal('skill-namespace', ex.namespace);
    setVal('skill-deployment', ex.deployment);
    setVal('skill-service', ex.service);
    setVal('skill-pod', ex.pod);
    setVal('skill-node', ex.node);
    setVal('skill-message', ex.message);
}

function skillWorkflowRequestBody() {
    const payload = {
        namespace: document.getElementById('skill-namespace')?.value?.trim() || 'default',
        fault_type: document.getElementById('skill-fault-type')?.value || '',
        message: document.getElementById('skill-message')?.value?.trim() || '',
        deployment: document.getElementById('skill-deployment')?.value?.trim() || '',
        service: document.getElementById('skill-service')?.value?.trim() || '',
        pod: document.getElementById('skill-pod')?.value?.trim() || '',
        node: document.getElementById('skill-node')?.value?.trim() || '',
        source: 'skillops-ui',
    };
    return {
        domain: document.getElementById('skill-domain')?.value || 'k8s',
        stages: (document.getElementById('skill-stages')?.value || 'detect,diagnose,heal,verify')
            .split(',')
            .map(x => x.trim())
            .filter(Boolean),
        dry_run: document.getElementById('skill-dryrun')?.checked !== false,
        allow_legacy_fallback: document.getElementById('skill-legacy-fallback')?.checked === true,
        payload,
    };
}

async function runSkillWorkflow() {
    const out = document.getElementById('skill-output');
    const status = document.getElementById('skill-result-status');
    if (out) out.textContent = '正在调用 Agent 已选 Skill，按 detect / diagnose / heal / verify 阶段执行...';
    renderSkillFlowStrip({ active: 'detect' });
    if (status) {
        status.className = 'badge badge-info';
        status.textContent = '运行中';
    }
    const body = skillWorkflowRequestBody();
    const res = await api('/api/skill-workflows/operate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        timeoutMs: 60000,
    });
    state.lastSkillWorkflow = res;
    if (!res) {
        if (out) out.textContent = 'Skill 调用失败，请检查 Agent 决策记录、Skill 服务和后端日志。';
        renderSkillFlowStrip({ failed: true });
        if (status) {
            status.className = 'badge badge-danger';
            status.textContent = '失败';
        }
        return;
    }
    renderSkillWorkflowResult(res);
    loadSkillWorkflowRuns();
}

function renderSkillWorkflowResult(res = {}) {
    const status = document.getElementById('skill-result-status');
    const summary = document.getElementById('skill-result-summary');
    const out = document.getElementById('skill-output');
    const diagnosis = res.diagnosis || {};
    const capability = res.capability || {};
    const plan = res.execution_plan || {};
    const migration = res.migration || {};
    const runtime = res.skill_runtime || {};
    const suggestions = plan.suggestions || res.suggestions || [];
    const commands = plan.commands || [];
    const checks = (res.verification_plan || {}).checks || [];
    const mode = migration.mode || res.mode || 'skill_primary';
    const ok = res.status === 'ok' || res.status === 'success' || diagnosis.status === 'diagnosed';
    renderSkillFlowStrip({ result: res });
    if (status) {
        status.className = `badge badge-${ok ? 'success' : 'warning'}`;
        status.textContent = ok ? '已生成闭环' : '需补充证据';
    }
    if (summary) {
        summary.innerHTML = `
            <div class="skill-status-strip">
                <div><span>主 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
                <div><span>匹配 Skill</span><strong>${escapeHtml(runtime.matched_count ?? '-')}</strong></div>
                <div><span>迁移模式</span><strong>${escapeHtml(skillMigrationModeLabel(mode))}</strong></div>
                <div><span>诊断置信度</span><strong>${escapeHtml(formatConfidence(diagnosis.confidence || capability.confidence))}</strong></div>
            </div>
            ${renderSkillWorkflowJourney(res)}
            <div class="skill-result-grid">
                ${renderSkillDiagnosisCard(diagnosis)}
                ${renderSkillCapabilityCard(capability)}
                ${renderSkillExecutionCard(plan, suggestions, commands)}
                ${renderSkillVerificationCard(res.verification_plan || {}, checks)}
            </div>
            ${renderSkillMigrationNotice(migration)}
            ${renderSkillTrace(res.trace || {}, runtime)}
        `;
    }
    if (out) {
        out.textContent = JSON.stringify({
            status: res.status,
            domain: res.domain,
            dry_run: res.dry_run,
            diagnosis,
            capability,
            execution_plan: plan,
            verification_plan: res.verification_plan,
            migration,
            dashboard_bridge: res.dashboard_bridge,
        }, null, 2);
    }
    renderSkillOpsLiveSummary();
}

function renderSkillWorkflowJourney(res = {}) {
    const runtime = res.skill_runtime || {};
    const diagnosis = res.diagnosis || {};
    const capability = res.capability || {};
    const plan = res.execution_plan || {};
    const verify = res.verification_plan || {};
    const migration = res.migration || {};
    const steps = [
        {
            label: '检测',
            value: `${runtime.matched_count ?? 0} 个命中`,
            detail: runtime.primary_skill_id ? `主路径 ${runtime.primary_skill_id}` : '等待主路径',
            ok: Number(runtime.matched_count || 0) > 0,
        },
        {
            label: '诊断',
            value: formatConfidence(diagnosis.confidence),
            detail: diagnosis.root_cause_component || faultTypeLabel(diagnosis.fault_type || '') || '证据待补充',
            ok: Number(diagnosis.confidence || 0) >= Number(capability.confidence_threshold || 0.7),
        },
        {
            label: '门禁',
            value: capability.heal_ready ? '通过' : '受限',
            detail: capability.requires_approval ? '需要审批' : 'Dry-run 可审计',
            ok: !!capability.heal_ready,
        },
        {
            label: '自愈',
            value: `${plan.command_count || (plan.commands || []).length || (plan.suggestions || []).length || 0} 个动作`,
            detail: res.dry_run === false ? '允许真实执行' : '默认 Dry-run',
            ok: !!(plan.command_count || (plan.commands || []).length || (plan.suggestions || []).length),
        },
        {
            label: '验证',
            value: `${(verify.checks || []).length} 项检查`,
            detail: migration.coverage_gap ? '存在覆盖缺口' : '闭环可追溯',
            ok: (verify.checks || []).length > 0 && !migration.coverage_gap,
        },
    ];
    return `<div class="skill-journey">
        ${steps.map((step, index) => `<div class="skill-journey-step ${step.ok ? 'ok' : 'warn'}">
            <span>${index + 1}</span>
            <div>
                <strong>${escapeHtml(step.label)}</strong>
                <b>${escapeHtml(step.value)}</b>
                <small>${escapeZh(step.detail)}</small>
            </div>
        </div>`).join('')}
    </div>`;
}

function renderSkillFlowStrip(options = {}) {
    const el = document.getElementById('skill-flow-strip');
    if (!el) return;
    const result = options.result || null;
    const active = options.active || '';
    const failed = options.failed === true;
    const diagnosis = result?.diagnosis || {};
    const capability = result?.capability || {};
    const plan = result?.execution_plan || {};
    const verify = result?.verification_plan || {};
    const steps = [
        { key: 'detect', label: '检测', done: !!result?.skill_runtime?.matched_count, text: result ? `匹配 ${result.skill_runtime?.matched_count || 0}` : '等待' },
        { key: 'diagnose', label: '诊断', done: !!diagnosis.status && diagnosis.status !== 'missing', text: result ? formatConfidence(diagnosis.confidence) : '等待' },
        { key: 'heal', label: '自愈', done: !!plan.command_count || !!capability.heal_ready, text: result ? `${plan.command_count || 0} 个动作` : '等待' },
        { key: 'verify', label: '验证', done: (verify.checks || []).length > 0, text: result ? `${(verify.checks || []).length} 项检查` : '等待' },
    ];
    el.innerHTML = steps.map((step, index) => {
        let cls = 'pending';
        if (failed) cls = 'failed';
        else if (active && step.key === active) cls = 'active';
        else if (step.done) cls = 'done';
        return `<div class="skill-flow-step ${cls}">
            <span>${index + 1}</span>
            <strong>${escapeHtml(step.label)}</strong>
            <small>${escapeHtml(step.text)}</small>
        </div>`;
    }).join('');
}

function renderSkillDiagnosisCard(diagnosis = {}) {
    const evidence = diagnosis.evidence || [];
    const method = diagnosis.external_method || {};
    const gate = diagnosis.evaluation_gate || {};
    const expectedEvidence = diagnosis.expected_evidence || method.expected_evidence || [];
    const metricNames = method.metrics ? Object.keys(method.metrics).slice(0, 6) : [];
    const gateChecks = gate.checks || {};
    return `<div class="skill-result-card">
        <div class="skill-card-head">
            <strong>诊断结果</strong>
            <span class="badge badge-${diagnosis.confidence >= 0.85 ? 'success' : 'warning'}">${escapeHtml(formatConfidence(diagnosis.confidence))}</span>
        </div>
        <div class="skill-kv"><span>根因类型</span><strong>${escapeHtml(faultTypeLabel(diagnosis.fault_type || '-'))}</strong></div>
        <div class="skill-kv"><span>根因组件</span><strong>${escapeHtml(diagnosis.root_cause_component || '-')}</strong></div>
        <p>${escapeZh(diagnosis.root_cause || diagnosis.summary || '未形成明确诊断结论。')}</p>
        ${evidence.length ? `<ul>${evidence.slice(0, 6).map(x => `<li>${escapeZh(x)}</li>`).join('')}</ul>` : '<p class="text-muted">暂无结构化证据。</p>'}
        ${method.source ? `<div class="skill-method-box">
            <div class="skill-kv"><span>外部方法</span><strong>${escapeHtml(method.source)}</strong></div>
            <div class="skill-kv"><span>评测场景</span><strong>${escapeHtml(method.scenario || '-')}</strong></div>
            <div class="skill-kv"><span>故障域</span><strong>${escapeHtml((method.diagnosis_domains || method.fault_domains || []).slice(0, 6).join(' / ') || '-')}</strong></div>
            ${metricNames.length ? `<div class="skill-method-tags">${metricNames.map(x => `<span>${escapeHtml(x)}</span>`).join('')}</div>` : ''}
            ${expectedEvidence.length ? `<details class="skill-raw-fold"><summary>AISHPerf 期望证据</summary><ul>${expectedEvidence.slice(0, 8).map(x => `<li>${escapeZh(x)}</li>`).join('')}</ul></details>` : ''}
        </div>` : ''}
        ${gate.method ? `<div class="skill-method-box">
            <div class="skill-kv"><span>评测门禁</span><strong>${gate.ready ? '已满足' : '证据待补充'}</strong></div>
            <div class="skill-method-tags">${Object.entries(gateChecks).map(([k, v]) => `<span class="${v ? 'ok' : 'warn'}">${escapeHtml(k)}=${v ? 'yes' : 'no'}</span>`).join('')}</div>
            <small>${escapeZh(gate.score_hint || '')}</small>
        </div>` : ''}
        ${renderConfidenceExplanation(diagnosis.confidence_explanation)}
    </div>`;
}

function renderSkillCapabilityCard(capability = {}) {
    const blockers = capability.heal_blocker_messages || capability.heal_blockers || [];
    return `<div class="skill-result-card">
        <div class="skill-card-head">
            <strong>自愈门禁</strong>
            <span class="badge badge-${capability.heal_ready ? 'success' : 'warning'}">${capability.heal_ready ? '可生成方案' : '受限'}</span>
        </div>
        <div class="skill-kv"><span>诊断准入</span><strong>${capability.diagnosis_ready === false ? '未通过' : '通过'}</strong></div>
        <div class="skill-kv"><span>执行审批</span><strong>${capability.requires_approval ? '需要审批' : '无需额外审批'}</strong></div>
        <div class="skill-kv"><span>风险确认</span><strong>${capability.requires_risk_ack ? '需要确认' : '不需要'}</strong></div>
        <div class="skill-kv"><span>置信度阈值</span><strong>${escapeHtml(formatConfidence(capability.confidence_threshold))}</strong></div>
        ${renderAutoExecutionGate(capability.auto_execution_gate)}
        ${blockers.length ? `<div class="skill-note warning">${escapeZh(blockers.join(' / '))}</div>` : '<div class="skill-note success">当前门禁没有发现阻断项。</div>'}
    </div>`;
}

function renderSkillExecutionCard(plan = {}, suggestions = [], commands = []) {
    return `<div class="skill-result-card">
        <div class="skill-card-head">
            <strong>自愈方案</strong>
            <span class="badge badge-${suggestions.length || commands.length ? 'success' : 'gray'}">${suggestions.length || commands.length} 个动作</span>
        </div>
        ${(suggestions || []).slice(0, 5).map((s, i) => `
            <div class="skill-action">
                <span>${i + 1}</span>
                <div>
                    <strong>${escapeHtml(s.step || s.action || `动作 ${i + 1}`)}</strong>
                    <p>${escapeZh([riskLabel(s.risk), s.selection_reason || s.skill_name || s.skill_id || ''].filter(Boolean).join(' · '))}</p>
                    ${s.command ? `<code>${escapeHtml(s.command)}</code>` : ''}
                    ${s.rollback_command ? `<small>回滚: ${escapeHtml(s.rollback_command)}</small>` : ''}
                </div>
            </div>
        `).join('') || (commands.length ? commands.slice(0, 5).map((c, i) => `<div class="skill-action"><span>${i + 1}</span><div><code>${escapeHtml(c)}</code></div></div>`).join('') : '<p class="text-muted">未生成可执行动作。</p>')}
    </div>`;
}

function renderSkillVerificationCard(plan = {}, checks = []) {
    return `<div class="skill-result-card">
        <div class="skill-card-head">
            <strong>验证计划</strong>
            <span class="badge badge-${checks.length ? 'info' : 'gray'}">${checks.length} 项检查</span>
        </div>
        <div class="skill-kv"><span>状态</span><strong>${escapeHtml(statusLabel(plan.status || '-'))}</strong></div>
        <div class="skill-kv"><span>来源</span><strong>${escapeZh(plan.source || '-')}</strong></div>
        ${checks.length ? checks.slice(0, 6).map(c => `<code>${escapeHtml(c.command || c)}</code>`).join('') : '<p class="text-muted">暂无验证命令。</p>'}
    </div>`;
}

function renderSkillMigrationNotice(migration = {}) {
    if (!migration || !Object.keys(migration).length) return '';
    const gap = migration.coverage_gap;
    return `<div class="skill-migration">
        <span class="badge badge-${gap ? 'warning' : 'success'}">${escapeHtml(skillMigrationModeLabel(migration.mode))}</span>
        <span>${escapeZh(migration.next_action || (gap ? '存在 Skill 覆盖缺口，已尝试兼容回退。' : 'Skill 主路径已覆盖当前闭环。'))}</span>
    </div>`;
}

function renderSkillTrace(trace = {}, runtime = {}) {
    const runs = runtime.runs || [];
    if (!Object.keys(trace).length && !runs.length) return '';
    return `<details class="skill-trace">
        <summary>查看 Skill 调用轨迹</summary>
        <pre>${escapeHtml(JSON.stringify({ trace, runs }, null, 2))}</pre>
    </details>`;
}

async function loadSkillWorkflowRuns() {
    const el = document.getElementById('skill-run-list');
    if (!el) return;
    const data = await api('/api/skill-workflows/runs?limit=20');
    const runs = Array.isArray(data?.runs) ? data.runs : [];
    const runtimeRuns = Array.isArray(data?.runtime_runs) ? data.runtime_runs : [];
    if (!runs.length && !runtimeRuns.length) {
        el.innerHTML = '<p class="text-muted">暂无 Skill 调用记录。运行一次闭环后会在这里看到过程。</p>';
        return;
    }
    const localHtml = runs.map(renderSkillRunItem).join('');
    const runtimeOnly = runtimeRuns
        .filter(r => !runs.some(local => local.runtime_run_id && local.runtime_run_id === r.id))
        .slice(0, 10);
    const runtimeHtml = runtimeOnly.length
        ? `<details class="skill-runtime-runs"><summary>Runtime 侧记录 ${runtimeOnly.length} 条</summary>${runtimeOnly.map(renderSkillRunItem).join('')}</details>`
        : '';
    el.innerHTML = localHtml + runtimeHtml;
}

function renderSkillRunItem(run = {}) {
    const confidence = run.confidence === null || run.confidence === undefined ? '-' : formatConfidence(run.confidence);
    const statusClass = run.status === 'failed' ? 'danger' : (run.status === 'no_match' ? 'warning' : 'success');
    return `<div class="skill-run-item" id="skill-run-${escapeHtml(run.id || '')}">
        <div class="skill-run-main">
            <div>
                <strong>${escapeHtml(run.fault_type || run.primary_skill_id || run.id || '-')}</strong>
                <p>${escapeHtml([run.primary_skill_id || '-', skillMigrationModeLabel(run.migration_mode), `匹配 ${run.matched_count || 0}`, `动作 ${run.command_count || 0}`].join(' · '))}</p>
            </div>
            <div class="skill-run-meta">
                <span class="badge badge-${statusClass}">${escapeHtml(statusLabel(run.status || '-'))}</span>
                <span class="badge badge-info">${escapeHtml(confidence)}</span>
                <span class="text-muted">${escapeHtml(formatTime((run.started_at || 0) * 1000))}</span>
                <button class="btn btn-sm" onclick="showSkillWorkflowRun('${escapeHtml(run.id || '')}')">详情</button>
            </div>
        </div>
        <div class="skill-run-detail" id="skill-run-detail-${escapeHtml(run.id || '')}"></div>
    </div>`;
}

async function showSkillWorkflowRun(runId) {
    const el = document.getElementById(`skill-run-detail-${runId}`);
    if (!el) return;
    if (el.dataset.open === 'true') {
        el.innerHTML = '';
        el.dataset.open = 'false';
        return;
    }
    el.innerHTML = '<p class="text-muted">加载调用详情...</p>';
    const detail = state.skillRunDetailCache[runId] || await api(`/api/skill-workflows/runs/${encodeURIComponent(runId)}`);
    if (!detail) {
        el.innerHTML = '<p class="text-danger">调用记录加载失败。</p>';
        return;
    }
    if (['completed', 'failed', 'no_match'].includes(detail.status || detail.result?.status || '')) {
        state.skillRunDetailCache[runId] = detail;
    }
    const result = detail.result || detail;
    const runs = result.skill_runtime?.runs || [];
    el.dataset.open = 'true';
    el.innerHTML = `
        <div class="skill-run-detail-grid">
            <div>
                <h4>完整工作流</h4>
                ${renderSkillWorkflowRunsDetail(result, ['detect', 'diagnose', 'heal', 'verify'])}
            </div>
            <div>
                <h4>关键结论</h4>
                <div class="skill-kv"><span>Dashboard Run</span><strong>${escapeHtml(detail.id || result.dashboard_run_id || '-')}</strong></div>
                <div class="skill-kv"><span>Runtime Run</span><strong>${escapeHtml(detail.runtime_run_id || result.workflow_run_id || '-')}</strong></div>
                <div class="skill-kv"><span>代理模式</span><strong>${escapeHtml(detail.dashboard_bridge?.mode || result.dashboard_bridge?.mode || '-')}</strong></div>
                <div class="skill-kv"><span>根因</span><strong>${escapeZh(result.diagnosis?.root_cause || '-')}</strong></div>
                <div class="skill-kv"><span>门禁</span><strong>${escapeHtml(result.capability?.heal_ready ? '自愈可执行' : '自愈受限')}</strong></div>
                <div class="skill-kv"><span>验证</span><strong>${escapeHtml((result.verification_plan?.checks || []).length)} 项检查</strong></div>
                <details class="skill-raw-fold">
                    <summary>查看原始审计 JSON</summary>
                    <pre>${escapeHtml(JSON.stringify({
                        diagnosis: result.diagnosis,
                        capability: result.capability,
                        execution_plan: result.execution_plan,
                        verification_plan: result.verification_plan,
                        migration: result.migration,
                        trace: result.trace,
                    }, null, 2))}</pre>
                </details>
            </div>
        </div>`;
}

function renderSkillWorkflowRunsDetail(result = {}, stagesFilter = []) {
    const runtime = result.skill_runtime || {};
    const runs = runtime.runs || [];
    if (!runs.length) return '<p class="text-muted">没有记录到 Skill 工作流阶段。</p>';
    return `<div class="skill-workflow-detail">
        ${renderSkillWorkflowJourney(result)}
        ${renderAlertSkillExecutionProcess(result, stagesFilter, { open: true, showMissing: true })}
    </div>`;
}

function renderSkillRunStages(runs = []) {
    if (!runs.length) return '<p class="text-muted">没有记录到 skill 阶段。</p>';
    return runs.map(run => {
        const stages = run.trace || [];
        return `<div class="skill-stage-run">
            <strong>${escapeHtml(run.skill?.id || '-')}</strong>
            ${stages.map(s => `<div class="skill-stage-line">
                <span class="badge badge-${s.status === 'ok' || s.status === 'matched' ? 'success' : (s.status === 'error' ? 'danger' : 'gray')}">${escapeHtml(s.stage || '-')}</span>
                <span>${escapeHtml(s.status || '-')}</span>
                <span>${escapeHtml(formatConfidence(s.confidence))}</span>
                <span>${escapeHtml(s.duration_ms ?? 0)} ms</span>
            </div>`).join('') || '<p class="text-muted">该 skill 没有阶段 trace。</p>'}
        </div>`;
    }).join('');
}

function skillMigrationModeLabel(value) {
    const labels = {
        skill_primary: 'Skill 主路径',
        skill_bridge: 'Skill 兼容桥接',
        legacy_fallback: 'Agent 回退',
        local_fallback: '本地回退',
        http_proxy: 'Runtime 代理',
    };
    return labels[value] || value || '-';
}

function openSkillHealPlan() {
    const res = state.lastSkillWorkflow;
    if (!res) {
        showToast('请先运行 Skill 闭环调试', 'warning');
        return;
    }
    const plan = res.execution_plan || {};
    const payload = skillWorkflowRequestBody().payload;
    const healPlan = {
        ...plan,
        suggestions: plan.suggestions || [],
        blocked_templates: res.capability?.heal_blockers || [],
        capability: res.capability || {},
        diagnosis: res.diagnosis || {},
        diagnosis_gate: {
            ready: res.capability?.diagnosis_ready,
            confidence: res.diagnosis?.confidence || res.capability?.confidence,
            confidence_threshold: res.capability?.confidence_threshold,
            blocker_messages: res.capability?.heal_blocker_messages || [],
        },
        verification_plan: res.verification_plan || {},
        target: payload,
        fault_type: res.diagnosis?.fault_type || payload.fault_type,
        namespace: payload.namespace,
        policy: { dry_run: res.dry_run, recommend_only: res.dry_run },
    };
    openHealPlanModal(healPlan, { ...payload, diagnosis: res.diagnosis, source: 'skillops' }, { source: 'skillops' });
}

let _healRecipeLibrary = null;
let _healRecipeEdit = null;
let _healRecipeHistory = null;

async function showHealRecipeHistory(index) {
    const recipe = _healRecipeLibrary?.recipes?.[index];
    if (!recipe) return;
    try {
        const data = await api(`/api/heal/recipes/${encodeURIComponent(recipe.fault_type)}/history`, {throwOnError: true, suppressErrorLog: true});
        _healRecipeHistory = data;
        let panel = document.getElementById('heal-recipe-history');
        if (!panel) {
            panel = document.createElement('div');
            panel.id = 'heal-recipe-history';
            document.getElementById('heal-recipes-meta').after(panel);
        }
        panel.innerHTML = `<h4>策略历史：${escapeHtml(recipe.fault_type)}</h4>
            <p>最多保留全库最近 ${escapeHtml(data.retention_limit)} 次修改。恢复先提交草稿，审核通过后生成新版本；不会执行自愈动作。</p>
            <p id="recipe-restore-error" role="alert"></p>
            ${(data.history || []).map((entry, i) => `<details><summary>${escapeHtml(formatTime(entry.at * 1000))} · ${entry.operation === 'restore' ? '恢复版本' : '编辑策略'}</summary>
                <strong>修改前</strong><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(entry.before, null, 2))}</pre>
                <button class="btn btn-sm" onclick="restoreHealRecipe(${i}, 'before')">恢复到修改前</button>
                <strong>修改后</strong><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(entry.after, null, 2))}</pre>
                <button class="btn btn-sm" onclick="restoreHealRecipe(${i}, 'after')">恢复到修改后</button></details>`).join('') || '<p>暂无修改历史</p>'}
            <button class="btn btn-sm" onclick="document.getElementById('heal-recipe-history').remove()">关闭</button>`;
        panel.scrollIntoView({behavior: 'smooth', block: 'center'});
    } catch (e) { showToast('读取历史失败：' + e.message, 'error'); }
}

async function restoreHealRecipe(index, snapshot) {
    const data = _healRecipeHistory;
    const entry = data?.history?.[index];
    if (!entry || !confirm('确认将所选历史内容提交为待审核草稿？')) return;
    const panel = document.getElementById('heal-recipe-history');
    const buttons = panel.querySelectorAll('button');
    buttons.forEach(b => b.disabled = true);
    try {
        await api(`/api/heal/recipes/${encodeURIComponent(data.fault_type)}/restore`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({revision: data.revision, entry_id: entry.entry_id, snapshot}),
            throwOnError: true, suppressErrorLog: true,
        });
        document.getElementById('heal-recipe-editor')?.remove();
        _healRecipeEdit = null;
        await loadHealCenter();
        await showHealRecipeHistory(_healRecipeLibrary.recipes.findIndex(r => r.fault_type === data.fault_type));
        showToast('恢复草稿已提交，审核通过后生效');
    } catch (e) {
        document.getElementById('recipe-restore-error').textContent = e.message;
    } finally { buttons.forEach(b => b.disabled = false); }
}

function editHealRecipe(index) {
    const recipe = _healRecipeLibrary?.recipes?.[index];
    if (!recipe) return;
    _healRecipeEdit = { fault_type: recipe.fault_type, revision: _healRecipeLibrary.revision };
    let panel = document.getElementById('heal-recipe-editor');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'heal-recipe-editor';
        document.getElementById('heal-recipes-meta').after(panel);
    }
    panel.innerHTML = `<h4>编辑策略：${escapeHtml(recipe.fault_type)}</h4>
        <p>用于自愈中心及知识库桥接生成的新方案。专用 Skill 的动作需在 Skill 中单独维护；已有审批方案保持原内容。保存会提交待审核草稿，审核通过后才生效；策略仍需验证。</p>
        <label>描述<textarea id="recipe-description" class="textarea" rows="3">${escapeHtml(recipe.description || '')}</textarea></label>
        <label>匹配信号（每行一条）<textarea id="recipe-signals" class="textarea" rows="3">${escapeHtml((recipe.signals || []).join('\n'))}</textarea></label>
        <label>动作模板（JSON：step、command、rollback_command、risk）<textarea id="recipe-actions" class="textarea" rows="12">${escapeHtml(JSON.stringify(recipe.actions || [], null, 2))}</textarea></label>
        <p id="recipe-save-error" role="alert"></p>
        <button id="recipe-save" class="btn btn-primary" onclick="saveHealRecipe()">校验并提交审核</button>
        <button class="btn" onclick="document.getElementById('heal-recipe-editor').remove(); _healRecipeEdit = null">取消</button>`;
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveHealRecipe() {
    if (!_healRecipeEdit) return;
    const button = document.getElementById('recipe-save');
    const error = document.getElementById('recipe-save-error');
    button.disabled = true;
    error.textContent = '';
    try {
        const recipe = {
            description: document.getElementById('recipe-description').value.trim(),
            signals: document.getElementById('recipe-signals').value.split('\n').map(s => s.trim()).filter(Boolean),
            actions: JSON.parse(document.getElementById('recipe-actions').value),
        };
        await api(`/api/heal/recipes/${encodeURIComponent(_healRecipeEdit.fault_type)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ recipe, revision: _healRecipeEdit.revision }),
            throwOnError: true, suppressErrorLog: true,
        });
        document.getElementById('heal-recipe-editor')?.remove();
        _healRecipeEdit = null;
        showToast('草稿已提交审核，当前生效策略未改变');
        await loadHealCenter();
    } catch (e) {
        error.textContent = e.message || '保存失败，编辑内容已保留';
    } finally {
        button.disabled = false;
    }
}

let _healRecipeDrafts = [];
let _healDraftReview = null;

function renderHealRecipeDrafts(data) {
    let panel = document.getElementById('heal-recipe-drafts');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'heal-recipe-drafts';
        document.getElementById('heal-recipes-meta').after(panel);
    }
    if (!data) { panel.textContent = '草稿列表读取失败，请刷新重试'; return; }
    _healRecipeDrafts = data.drafts || [];
    const pending = _healRecipeDrafts.filter(d => d.status === 'pending');
    panel.innerHTML = `<h4>知识发布审核 · ${pending.length} 条待处理</h4>
        <p>审核通过仅发布知识，不会执行集群操作，也不会自动获得自愈执行权限。</p>
        ${pending.map(d => `<div><strong>${escapeHtml(d.fault_type)}</strong>
            <span>${escapeHtml(formatTime(d.created_at * 1000))}</span>
            <button class="btn btn-sm" onclick="reviewHealRecipeDraft(${_healRecipeDrafts.indexOf(d)})">审核草稿</button></div>`).join('') || '<p>暂无待审核草稿</p>'}
        <details><summary>最近审核记录</summary>${_healRecipeDrafts.filter(d => d.status !== 'pending').slice(0, 20).map(d => `<p>${escapeHtml(d.fault_type)} · ${d.status === 'approved' ? '已发布' : '已驳回'} · ${escapeHtml(d.reviewer || '')} · ${escapeHtml(d.comment || '')}</p>`).join('') || '暂无记录'}</details>`;
}

function reviewHealRecipeDraft(index) {
    const draft = _healRecipeDrafts[index];
    if (!draft) return;
    _healDraftReview = JSON.parse(JSON.stringify(draft));
    let panel = document.getElementById('heal-draft-review');
    if (!panel) { panel = document.createElement('div'); panel.id = 'heal-draft-review'; document.getElementById('heal-recipe-drafts').after(panel); }
    panel.innerHTML = `<h4>审核：${escapeHtml(draft.fault_type)}</h4>
        <strong>原生效内容</strong><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(draft.before, null, 2))}</pre>
        <strong>待发布内容</strong><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(draft.recipe, null, 2))}</pre>
        <label>审核人（登录时以账户为准）<input id="recipe-reviewer" class="input-sm" maxlength="100"></label>
        <label>审核意见<textarea id="recipe-review-comment" class="textarea" maxlength="2000"></textarea></label>
        <p id="recipe-review-error" role="alert"></p>
        <button class="btn btn-primary" onclick="decideHealRecipeDraft('approve')">通过并发布</button>
        <button class="btn btn-danger" onclick="decideHealRecipeDraft('reject')">驳回草稿</button>
        <button class="btn" onclick="document.getElementById('heal-draft-review').remove()">关闭</button>`;
    panel.scrollIntoView({behavior: 'smooth', block: 'center'});
}

async function decideHealRecipeDraft(decision) {
    const draft = _healDraftReview;
    const panel = document.getElementById('heal-draft-review');
    if (!draft || !panel) return;
    const reviewer = document.getElementById('recipe-reviewer').value.trim();
    const error = document.getElementById('recipe-review-error');
    if (!reviewer) { error.textContent = '请填写审核人记录'; return; }
    const buttons = panel.querySelectorAll('button'); buttons.forEach(b => b.disabled = true);
    try {
        await api(`/api/heal/recipe-drafts/${encodeURIComponent(draft.id)}/decision`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({decision, reviewer, comment: document.getElementById('recipe-review-comment').value.trim(),
                content_hash: draft.content_hash, revision: draft.base_revision}),
            throwOnError: true, suppressErrorLog: true,
        });
        panel.remove(); _healDraftReview = null;
        document.getElementById('heal-recipe-history')?.remove();
        await loadHealCenter();
        showToast(decision === 'approve' ? '知识已发布，自动执行仍受门禁限制' : '草稿已驳回，生效策略未改变');
    } catch (e) { error.textContent = e.message; }
    finally { buttons.forEach(b => b.disabled = false); }
}

async function loadHealCenter() {
    const [recipes, , drafts] = await Promise.all([
        api('/api/heal/recipes'),
        loadHealRuns(_healRunsOffset),
        api('/api/heal/recipe-drafts'),
    ]);
    _healRecipeLibrary = recipes;
    renderHealRecipeDrafts(drafts);
    renderHealRecipesMeta(recipes || {});
    renderHealRecipes(recipes?.recipes || [], recipes?.grouped || null);
}

let _healRunsOffset = 0;
let _healRunsRequest = 0;

async function loadHealRuns(offset = 0) {
    const request = ++_healRunsRequest;
    const filter = document.getElementById('heal-run-filter')?.value || 'all';
    const el = document.getElementById('heal-runs');
    const pager = document.getElementById('heal-runs-pagination');
    if (el) el.textContent = '加载记录中…';
    if (pager) pager.innerHTML = '';
    const params = new URLSearchParams({limit: 10, offset, filter});
    const data = await api(`/api/heal/runs?${params}`);
    if (request !== _healRunsRequest) return;
    if (!data) {
        if (el) el.textContent = '运行记录加载失败，请刷新重试。';
        return;
    }
    if (offset && !data.runs.length && data.filtered_total) return loadHealRuns(0);
    _healRunsOffset = data.offset;
    _lastHealRuns = data.runs;
    renderHealRunsSummary(data.summary);
    renderHealRuns(data.runs);
    if (pager) pager.innerHTML = `
        <span class="text-muted">匹配 ${Number(data.filtered_total)} 条 · 第 ${Math.floor(data.offset / data.limit) + 1} 页 / ${Math.max(1, Math.ceil(data.filtered_total / data.limit))} 页</span>
        <button class="btn btn-sm" ${data.offset ? '' : 'disabled'} onclick="loadHealRuns(${Math.max(0, data.offset - data.limit)})">上一页</button>
        <button class="btn btn-sm" ${data.next_offset == null ? 'disabled' : ''} onclick="loadHealRuns(${data.next_offset ?? 0})">下一页</button>`;
}

function renderHealRecipesMeta(data) {
    const el = document.getElementById('heal-recipes-meta');
    if (!el) return;
    const validation = data.validation || {};
    const ok = validation.ok !== false;
    const issues = validation.issues || [];
    el.innerHTML = `
        <div class="heal-recipes-meta-row">
            <span class="badge badge-info">version ${escapeHtml(data.version || '-')}</span>
            <span class="badge badge-info">${escapeHtml(data.total || 0)} 条策略</span>
            <span class="badge badge-${ok ? 'success' : 'danger'}">${ok ? '校验通过' : '校验失败'}</span>
            ${validation.warning_count ? `<span class="badge badge-warning">${validation.warning_count} warnings</span>` : ''}
            ${validation.error_count ? `<span class="badge badge-danger">${validation.error_count} errors</span>` : ''}
        </div>
        <div class="heal-recipes-source">${escapeHtml(data.source || '')}</div>
        ${issues.length ? `<details class="heal-recipe-validation"><summary>查看校验问题</summary>${issues.map(renderHealRecipeIssue).join('')}</details>` : ''}
    `;
}

function renderHealRecipeIssue(issue) {
    return `<div class="heal-recipe-issue">
        <span class="badge badge-${issue.severity === 'error' ? 'danger' : 'warning'}">${escapeHtml(issue.severity || 'warning')}</span>
        <strong>${escapeHtml(issue.recipe || '-')}</strong>
        <span>${escapeHtml(issue.field || '')}</span>
        <small>${escapeHtml(issue.message || '')}</small>
    </div>`;
}

function renderHealRecipes(recipes, grouped = null) {
    const el = document.getElementById('heal-recipes');
    if (!el) return;
    if (!recipes.length) {
        el.innerHTML = '<p class="text-muted">暂无自愈知识库规则</p>';
        return;
    }
    const groups = grouped && Object.keys(grouped).length
        ? grouped
        : recipes.reduce((acc, r) => {
            const key = r.category || 'unknown';
            acc[key] = acc[key] || [];
            acc[key].push(r);
            return acc;
        }, {});
    el.innerHTML = Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, 'zh-CN')).map(([category, items]) => `
        <details class="heal-recipe-group" ${category === '应用' || category === '资源' ? 'open' : ''}>
            <summary>
                <strong>${escapeHtml(category)}</strong>
                <span class="badge badge-info">${items.length} 条策略</span>
            </summary>
            <div class="heal-recipe-group-body">
                ${items.map(r => `<div class="fault-card heal-recipe-card">
                    <div><span class="badge badge-info">${escapeHtml(r.category || '')}</span>
                    <span class="badge badge-${r.risk === 'high' ? 'danger' : r.risk === 'medium' ? 'warning' : 'success'}">${escapeHtml(r.risk || '')}</span>
                    <span class="badge badge-info">${escapeHtml(r.tier || 'kubectl')}</span>
                    <span class="badge badge-success">${escapeHtml(r.coverage || 'tested')}</span>
                    ${r.requires_approval ? '<span class="badge badge-warning">审批</span>' : '<span class="badge badge-success">可预演</span>'}</div>
                    <strong>${escapeHtml(r.fault_type || '')}</strong>
                    <button class="btn btn-sm" onclick="editHealRecipe(${(_healRecipeLibrary?.recipes || []).findIndex(item => item.fault_type === r.fault_type)})">编辑策略</button>
                    <button class="btn btn-sm" onclick="showHealRecipeHistory(${(_healRecipeLibrary?.recipes || []).findIndex(item => item.fault_type === r.fault_type)})">版本历史</button>
                    <p>${escapeHtml(r.description || '')}</p>
                    ${r.how_fixed ? `<small class="text-muted">${escapeHtml(r.how_fixed)}</small>` : ''}
                    <div class="heal-recipe-signals">${(r.signals || []).slice(0, 4).map(s => `<span>${escapeHtml(s)}</span>`).join('')}</div>
                    <details class="heal-recipe-actions-fold">
                        <summary>动作模板 ${(r.actions || []).length} 条</summary>
                        <div class="heal-recipe-actions">
                            ${(r.actions || []).map((a, i) => renderHealRecipeAction(a, i)).join('') || '<p class="text-muted">暂无动作模板</p>'}
                        </div>
                    </details>
                </div>`).join('')}
            </div>
        </details>
    `).join('');
}

function renderHealRecipeAction(action, index) {
    const risk = (action.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="heal-recipe-action">
        <div class="heal-recipe-action-head">
            <span class="rem-action-num">${index + 1}</span>
            <span class="badge badge-${riskClass}">${escapeHtml(riskLabel(action.risk))}</span>
            <strong>${escapeHtml(action.step || '')}</strong>
        </div>
        <div class="rem-action-cmd"><code>${escapeHtml(action.command || '')}</code></div>
        ${action.rollback_command ? `<div class="rem-action-rollback">回滚命令: ${escapeHtml(action.rollback_command)}</div>` : ''}
    </div>`;
}

function healVerificationBadge(verification) {
    if (!verification) return '';
    if (verification.reason === 'action_execution_failed' && verification.verification_scope === 'action_execution') {
        return '<span class="badge badge-danger">动作执行失败，业务未验证</span>';
    }
    if (verification.status === 'skipped') {
        return '<span class="badge badge-info">验证跳过</span>';
    }
    if (verification.recovered === true) {
        if (verification.verification_scope === 'infrastructure') {
            return '<span class="badge badge-warning">基础设施就绪，业务未验证</span>';
        }
        if (verification.verification_scope === 'infrastructure_and_business_http') {
            if (verification.business_validation === 'json_assertions') {
                return '<span class="badge badge-success">业务响应断言通过</span>';
            }
            return '<span class="badge badge-success">业务 HTTP 探测通过</span>';
        }
        return '<span class="badge badge-success">已验证恢复</span>';
    }
    if (verification.recovered === false) {
        return '<span class="badge badge-danger">验证未恢复</span>';
    }
    return '<span class="badge badge-warning">验证未知</span>';
}

function renderHealRunsSummary(summary = {}) {
    const el = document.getElementById('heal-runs-summary');
    if (!el) return;
    el.innerHTML = `
        <span class="badge badge-info">total ${escapeHtml(summary.total ?? 0)}</span>
        <span class="badge badge-info">real ${escapeHtml(summary.real ?? 0)}</span>
        <span class="badge badge-info">dry-run ${escapeHtml(summary.dry_run ?? 0)}</span>
        <span class="badge badge-success">recovered ${escapeHtml(summary.verified_recovered ?? 0)}</span>
        <span class="badge badge-danger">unrecovered ${escapeHtml(summary.verified_unrecovered ?? 0)}</span>
        <span class="badge badge-warning">待验证 ${escapeHtml(summary.verification_pending ?? 0)}</span>
        <span class="badge badge-warning">failed ${escapeHtml(summary.failed ?? 0)}</span>
    `;
}

function renderHealRuns(runs) {
    const el = document.getElementById('heal-runs');
    if (!el) return;
    const filtered = runs || [];
    el.innerHTML = filtered.length ? filtered.map(r => `<div class="fault-exp-run">
        <div>
            <span class="badge badge-${r.status === 'verification_pending' ? 'warning' : r.success ? 'success' : r.status === 'dry_run' ? 'info' : 'danger'}">${escapeHtml(statusLabel(r.status || ''))}</span>
            <span class="badge badge-${r.diagnosis_mode === 'rca_result' ? 'success' : 'warning'}">${r.diagnosis_mode === 'rca_result' ? '诊断驱动' : '告警驱动'}</span>
            ${healVerificationBadge(r.verification)}
            <strong>${escapeHtml(r.fault_type || r.id)}</strong>
            <span class="text-muted">${escapeHtml(r.namespace || '')} · actions=${(r.commands || []).length} · ${formatTime((r.started_at || 0) * 1000)}</span>
        </div>
        <div class="table-actions">
            <button class="btn btn-sm" onclick="showHealRun('${escapeHtml(r.id)}')">查看</button>
            ${r.status === 'verification_pending' ? `<button class="btn btn-sm btn-warning" onclick="reverifyHealRun('${escapeHtml(r.id)}')">重新验证（不执行动作）</button>` : ''}
            ${(r.rollback_commands || []).length ? `<button class="btn btn-sm btn-warning" onclick="rollbackHealRun('${escapeHtml(r.id)}')">回滚预演</button>` : ''}
        </div>
    </div>`).join('') : '<p class="text-muted">暂无匹配的自愈运行记录</p>';
}

async function suggestHeal() {
    const res = await api('/api/heal/suggest', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(healRequestBody()),
    });
    _lastHealResult = res || null;
    _lastHealSuggestions = res?.suggestions || [];
    renderHealSuggestions(_lastHealSuggestions, res?.blocked_templates || []);
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    if (res) openHealPlanModal(res, healRequestBody(), { source: 'heal-center' });
}

function renderHealSuggestions(suggestions, blocked) {
    const el = document.getElementById('heal-suggestions');
    if (!el) return;
    const cards = suggestions.map(s => `<div class="heal-card">
        <span class="badge badge-${s.risk === 'high' ? 'danger' : s.risk === 'medium' ? 'warning' : 'success'}">${escapeHtml(riskLabel(s.risk))}</span>
        <strong>${escapeHtml(s.step || '')}</strong>
        <code>${escapeHtml(s.command || '')}</code>
    </div>`);
    const blockedCards = blocked.map(s => `<div class="heal-card">
        <span class="badge badge-gray">已阻断</span>
        <strong>${escapeHtml(s.step || '')}</strong>
        <code>${escapeHtml(s.command || '')}</code>
        <p class="text-muted">${escapeHtml(s.blocked_reason || '')}</p>
    </div>`);
    el.innerHTML = cards.concat(blockedCards).join('') || '<p class="text-muted">未生成可执行建议</p>';
}

async function executeHealDryRun() {
    const body = healRequestBody();
    if (_lastHealSuggestions.length) {
        body.commands = _lastHealSuggestions.map(s => s.command).filter(Boolean);
        body.rollback_commands = _lastHealSuggestions.map(s => s.rollback_command).filter(Boolean);
    }
    body.approved = true;
    const res = await api('/api/heal/execute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadHealCenter();
}

async function openHealPlanFromCenter() {
    if (_lastHealResult) {
        openHealPlanModal(_lastHealResult, healRequestBody(), { source: 'heal-center' });
        return;
    }
    await suggestHeal();
}

function openHealPlanModal(result, sourcePayload = {}, options = {}) {
    _currentHealPlan = result || {};
    _currentHealPlanSourcePayload = { ...(sourcePayload || {}), source: options.source || sourcePayload.source || 'heal-plan-modal' };
    if (result?.ontology_action_context) {
        _currentHealPlanSourcePayload.ontology_revision = result.ontology_action_context.ontology_revision;
        _currentHealPlanSourcePayload.source_run_id = result.ontology_action_context.source_run_id;
    }
    const modal = document.getElementById('heal-plan-modal');
    if (!modal) return;
    const target = _currentHealPlan.target || {};
    const capability = _currentHealPlan.capability || {};
    const policy = _currentHealPlan.policy || {};
    const suggestions = (_currentHealPlan.suggestions || []).map((s, i) => ({ ...s, _idx: i }));
    const blocked = _currentHealPlan.blocked_templates || [];
    const diagnosis = _currentHealPlanSourcePayload?.diagnosis || _currentHealPlan?.diagnosis || null;
    const diagnosisGate = _currentHealPlan.diagnosis_gate || {};
    const diagnosisMode = diagnosis ? '诊断结果驱动' : '告警文本降级';
    const verificationPlan = _currentHealPlan.verification_plan || {};
    document.getElementById('heal-plan-subtitle').textContent =
        `${_currentHealPlan.fault_type || sourcePayload.fault_type || 'unknown'} · namespace ${_currentHealPlan.namespace || sourcePayload.namespace || 'default'}`;
    document.getElementById('heal-plan-summary').innerHTML = `
        <div>
            <span class="badge badge-${suggestions.length ? 'success' : 'warning'}">${suggestions.length ? '可执行方案' : '无可执行动作'}</span>
            <span class="badge badge-${diagnosis ? 'success' : 'warning'}">${diagnosisMode}</span>
            <span class="badge badge-info">pod=${escapeHtml(target.pod || sourcePayload.pod || '-')}</span>
            <span class="badge badge-info">deployment=${escapeHtml(target.deployment || sourcePayload.deployment || '-')}</span>
            <span class="badge badge-info">node=${escapeHtml(target.node || sourcePayload.node || '-')}</span>
            ${capability.execution_enabled ? '<span class="badge badge-success">真实执行已启用</span>' : '<span class="badge badge-warning">真实执行未启用</span>'}
            <span class="badge badge-${diagnosisGate.ready ? 'success' : 'warning'}">${diagnosisGate.ready ? '诊断门禁通过' : '诊断门禁未通过'}</span>
            <span class="badge badge-info">置信度=${escapeHtml(formatConfidence(diagnosisGate.confidence))}/${escapeHtml(formatConfidence(diagnosisGate.confidence_threshold))}</span>
            ${policy.recommend_only ? '<span class="badge badge-warning">仅建议模式</span>' : ''}
            ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
            ${capability.requires_risk_ack ? '<span class="badge badge-danger">高风险确认</span>' : ''}
            <span class="badge badge-${capability.auto_execution_eligible ? 'success' : 'warning'}">${capability.auto_execution_eligible ? '全自动资格通过' : '全自动资格受限'}</span>
        </div>
        <p>${escapeZh(diagnosis ? `诊断根因: ${diagnosis.root_cause || faultTypeLabel(diagnosis.fault_type) || '-'}` : '当前未关联分析诊断结果，方案基于告警文本生成；建议先完成分析再执行真实自愈。')}</p>
        ${renderDiagnosisGate(diagnosisGate)}
        ${renderAutoExecutionGate(capability.auto_execution_gate)}
        <p>${escapeHtml(_currentHealPlan.diagnosis_summary || _currentHealPlan.recipe?.description || '可修改命令后执行；默认 Dry-run 不会改变集群。')}</p>
        ${renderHealVerificationPlan(verificationPlan)}
    `;
    document.getElementById('heal-plan-dryrun').checked = true;
    document.getElementById('heal-plan-approved').checked = false;
    document.getElementById('heal-plan-riskack').checked = false;
    document.getElementById('heal-plan-select-all').checked = true;
    document.getElementById('heal-plan-actions').innerHTML = suggestions.length
        ? suggestions.map((s, i) => renderHealPlanActionEditor(s, i)).join('')
        : '<p class="text-muted">未生成可执行动作。请补齐 namespace/pod/deployment/node 后重新生成。</p>';
    document.getElementById('heal-plan-blocked').innerHTML = blocked.length
        ? `<details class="heal-blocked-fold"><summary>被阻断模板 ${blocked.length} 条</summary>${blocked.map(renderHealBlockedPlanItem).join('')}</details>`
        : '';
    const out = document.getElementById('heal-plan-output');
    if (out) out.textContent = suggestions.length
        ? healPlanGateMessage(capability, policy)
        : '当前没有可执行命令。';
    modal.style.display = 'flex';
}

function formatConfidence(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return '0%';
    return `${Math.round(n * 100)}%`;
}

function zhLabel(value, dict = {}) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';
    return dict[raw] || dict[raw.toLowerCase()] || TEXT_TRANSLATIONS.get(raw) || TEXT_TRANSLATIONS.get(raw.toLowerCase()) || raw;
}

function faultTypeLabel(value) {
    return zhLabel(value, FAULT_TYPE_LABELS) || '-';
}

function statusLabel(value) {
    if (value === 'verification_pending') return '动作已执行，恢复待验证';
    return zhLabel(value, STATUS_LABELS) || '-';
}

function rcaAgentLabel(value) {
    return zhLabel(value, RCA_AGENT_LABELS) || '-';
}

function rcaQualityLabel(value) {
    return zhLabel(value, RCA_QUALITY_LABELS) || '-';
}

function zhText(value) {
    if (value === null || value === undefined) return '';
    let text = String(value);
    const exact = TEXT_TRANSLATIONS.get(text.trim()) || TEXT_TRANSLATIONS.get(text.trim().toLowerCase());
    if (exact) return exact;
    for (const [source, target] of TEXT_TRANSLATIONS.entries()) {
        if (!source || source.length < 5) continue;
        text = text.replaceAll(source, target);
    }
    text = text.replace(/\bUnknown\b/g, '未知')
        .replace(/\bunknown\b/g, '未知')
        .replace(/\bcompleted\b/g, '已完成')
        .replace(/\bfailed\b/g, '失败')
        .replace(/\brunning\b/g, '运行中')
        .replace(/\bno_match\b/g, '未匹配')
        .replace(/\bmissing\b/g, '缺失')
        .replace(/\bunavailable\b/g, '不可用')
        .replace(/\bready\b/g, '就绪')
        .replace(/\bplan_and_execute\b/g, '规划后执行')
        .replace(/\bfast_path\b/g, '快速路径')
        .replace(/\bdeep_investigation\b/g, '深度调查')
        .replace(/\bmetric_agent\b/g, '指标智能体')
        .replace(/\blog_agent\b/g, '日志智能体')
        .replace(/\btrace_agent\b/g, '链路智能体')
        .replace(/\bevent_agent\b/g, '事件智能体')
        .replace(/\bllm_inference_agent\b/g, '智算推理智能体')
        .replace(/\bstructured_evidence\b/g, '结构化证据')
        .replace(/\bgraph_rca\b/g, '图根因定位')
        .replace(/\bcorrelation\b/g, '跨信号关联')
        .replace(/\bconf=/g, '置信度=')
        .replace(/\btimeout=/g, '超时=')
        .replace(/\bagents\b/g, '智能体')
        .replace(/\bhypotheses\b/g, '假设')
        .replace(/\bhypothesis\b/g, '假设')
        .replace(/\bevidence\b/g, '证据')
        .replace(/\biteration\b/g, '迭代')
        .replace(/\bmanual review\b/g, '人工复核')
        .replace(/\bhuman review\b/g, '人工复核');
    return text;
}

function escapeZh(value) {
    return escapeHtml(zhText(value));
}

function zhLogLine(value) {
    let text = zhText(value);
    text = text
        .replace(/Found (\d+) 条相似规则, (\d+) 条相似故障/g, '找到 $1 条相似规则、$2 条相似故障')
        .replace(/Found (\d+) similar rules, (\d+) similar faults/g, '找到 $1 条相似规则、$2 条相似故障')
        .replace(/(\d+) hypotheses generated/g, '已生成 $1 个根因假设')
        .replace(/Plan: (\d+) steps/g, '调查计划：$1 个步骤')
        .replace(/Fast 证据采集计划: (\d+) 智能体/g, '快速证据采集计划：$1 个智能体')
        .replace(/正在运行证据智能体: ([^(]+)\(超时=([^)]+)\)/g, (_, agents, timeout) => `正在运行证据智能体：${zhText(agents.trim())}（超时=${timeout}）`)
        .replace(/证据采集: ([\d.]+)s/g, '证据采集耗时：$1 秒')
        .replace(/Top 置信度: ([\d.]+)/g, '最高置信度：$1')
        .replace(/首位假设: \[([^\]]+)\] 置信度=([\d.]+) — /g, '首位假设：[$1] 置信度=$2 - ')
        .replace(/已达到高置信度 \(([\d.]+) ≥ ([\d.]+)\), 停止继续迭代/g, '已达到高置信度（$1 >= $2），停止继续迭代')
        .replace(/质量等级: ([a-zA-Z]+), score: ([\d.]+)/g, (_, level, score) => `质量等级：${rcaQualityLabel(level)}，评分：${score}`)
        .replace(/校准后置信度: ([\d.]+)/g, '校准后置信度：$1')
        .replace(/根因: /g, '根因：')
        .replace(/置信度: /g, '置信度：');
    return text;
}

function renderConfidenceExplanation(expl = {}) {
    if (!expl || typeof expl !== 'object' || !expl.method) return '';
    const factors = expl.factors || {};
    const reasons = expl.reasons || [];
    const caps = expl.caps || [];
    const evidence = expl.evidence || {};
    const history = expl.history || {};
    const isRca = String(expl.method || '').includes('evidence_calibrated');
    const factorLabels = {
        raw: '校准前置信度（Raw）',
        hypothesis: '假设分支稳定性（Hypothesis）',
        evidence: '多源证据充分性（Evidence）',
        quality: '诊断质量评审（Quality）',
        specificity: '根因具体性（Specificity）',
        history: '历史诊断正确性（History）',
        scenario: '实验场景',
        observation: '观测结果',
        endpoint: '服务端点',
        metrics: '指标采集',
        process: '进程状态',
        gpu: 'GPU 状态',
    };
    const methodText = isRca
        ? 'RCA 诊断置信度不是直接采用模型原始分，而是将原始判断、多假设评审、多源证据、诊断质量、根因具体性和经确认的历史诊断正确性加权校准。恢复结果不参与诊断打分。'
        : '智算故障置信度由实验场景、实际观测、vLLM 端点、Prometheus 指标、进程和 GPU 信号共同校准。';
    const formulaText = isRca
        ? (expl.formula || '0.18*raw + 0.18*hypothesis + 0.27*evidence + 0.17*quality + 0.10*specificity + 0.10*history，然后应用限幅规则')
        : '基于场景、观测、端点、指标、进程/GPU 等信号加权；Dry-run 或缺少关键观测时会限制最高置信度。';
    const factorText = Object.entries(factors)
        .map(([k, v]) => `${factorLabels[k] || k}: ${formatConfidence(v)}`)
        .join('；');
    const evidenceText = evidence.agent_count !== undefined
        ? `证据源 ${evidence.agent_count || 0} 个，结构化证据 ${evidence.structured_count || 0} 条`
        : '';
    const historyText = history.diagnosis_sample_count !== undefined
        ? `历史诊断标签 ${history.diagnosis_sample_count || 0} 条${history.diagnosis_accuracy_lower_bound !== null && history.diagnosis_accuracy_lower_bound !== undefined ? `，正确率置信下界 ${formatConfidence(history.diagnosis_accuracy_lower_bound)}` : ''}`
        : '';
    return `<details class="confidence-explain">
        <summary>置信度计算说明</summary>
        <div class="confidence-explain-body">
            <p>${escapeHtml(methodText)}</p>
            <div class="confidence-explain-grid">
                <span>方法</span><strong>${escapeHtml(expl.method)}</strong>
                <span>原始 → 校准</span><strong>${escapeHtml(formatConfidence(expl.raw_confidence))} → ${escapeHtml(formatConfidence(expl.calibrated_confidence))}</strong>
                <span>最高上限</span><strong>${escapeHtml(formatConfidence(expl.cap ?? 1))}</strong>
                <span>公式/规则</span><strong>${escapeHtml(formulaText)}</strong>
            </div>
            ${factorText ? `<p><strong>因子得分：</strong>${escapeHtml(factorText)}</p>` : ''}
            ${evidenceText || historyText ? `<p><strong>证据与历史：</strong>${escapeHtml([evidenceText, historyText].filter(Boolean).join('；'))}</p>` : ''}
            ${reasons.length ? `<p><strong>增信依据：</strong>${escapeHtml(reasons.slice(0, 4).join('；'))}</p>` : ''}
            ${caps.length ? `<p><strong>降信/限幅：</strong>${escapeHtml(caps.slice(0, 4).join('；'))}</p>` : ''}
            <p class="confidence-explain-note">自愈真实执行另行检查恢复历史；全自动执行要求 90% 以上诊断置信度、历史恢复成功率的 95% 单侧置信下界不低于 80%、无近期发布变更、规则显式标记安全，并且中高风险动作具有回滚操作。</p>
        </div>
    </details>`;
}

function riskLabel(value) {
    const key = String(value || 'low').toLowerCase();
    return RISK_LABELS[key] || value || '低风险';
}

function sourceLabel(value) {
    const key = String(value || 'dynamic').toLowerCase();
    return SOURCE_LABELS[key] || value || '动态生成';
}

function diagnosisBlockerLabel(value) {
    const key = String(value || '').trim();
    return DIAGNOSIS_BLOCKER_LABELS[key] || value || '缺少可执行诊断结论';
}

function diagnosisBlockerText(gate = {}) {
    const messages = gate.blocker_messages || [];
    const blockers = gate.blockers || [];
    const items = messages.length ? messages : blockers.map(diagnosisBlockerLabel);
    return items.length ? items.join(' / ') : '缺少可执行诊断结论';
}

function autoExecutionBlockerLabel(value) {
    const key = String(value || '').trim();
    return AUTO_EXECUTION_BLOCKER_LABELS[key] || value || '全自动条件未满足';
}

function autoExecutionBlockerText(gate = {}) {
    const messages = gate.blocker_messages || [];
    const blockers = gate.blockers || [];
    const items = messages.length ? messages : blockers.map(autoExecutionBlockerLabel);
    return items.length ? items.join(' / ') : '全自动条件未满足';
}

function llmFaultTypeLabel(value) {
    const raw = String(value || '').replace(/^LLM::/, '');
    return LLM_FAULT_TYPE_LABELS[raw] || LLM_FAULT_TYPE_LABELS[raw.toLowerCase()] || value || '-';
}

function llmLayerLabel(value) {
    const key = String(value || '').toLowerCase();
    return LLM_LAYER_LABELS[key] || value || '-';
}

function severityLabel(value) {
    const key = String(value || '').toLowerCase();
    return SEVERITY_LABELS[key] || value || '-';
}

function k8sFaultLabel(value) {
    return K8S_FAULT_LABELS[value] || value || '-';
}

function faultCategoryLabel(value) {
    const labels = {
        resource: '资源',
        application: '应用',
        network: '网络',
        availability: '可用性',
        cascade: '级联',
        config: '配置',
        storage: '存储',
        database: '数据库',
        cache: '缓存',
    };
    const key = String(value || '').toLowerCase();
    return labels[key] || zhText(value || '');
}

function diagnosisModeLabel(value) {
    return DIAGNOSIS_MODE_LABELS[value] || value || '智算故障实验诊断';
}

const GENERAL_FAULT_SCENARIO_LABELS = {
    'sn-cpu-stress': ['Social-Network CPU 压力', '在 test-social-network 命名空间制造 CPU 压力，观察接口响应时间上升、CPU throttling 和下游延迟传播。'],
    'sn-memory-pressure': ['Social-Network 内存压力', '制造内存压力并触发容器异常退出，观察 OOMKilled、Pod 重启和服务可用性变化。'],
    'sn-crashloop': ['Social-Network CrashLoop', '让目标容器启动后立即退出，观察 CrashLoopBackOff、重启次数和服务降级。'],
    'sn-entry-replica-exhaustion': ['入口副本耗尽', '降低入口服务可用副本，模拟入口层容量不足和请求失败。'],
    'sn-service-scale-down': ['服务副本缩容', '将关键服务缩容到低副本或零副本，验证无 Endpoint、503 和调用链故障传播。'],
    'sn-network-delay': ['Social-Network 网络延迟', '注入网络延迟，观察前端 P99 时延、下游调用超时和错误率变化。'],
    'sn-network-loss': ['Social-Network 网络丢包', '注入网络丢包，观察请求重试、超时和服务间通信失败。'],
    'sn-db-latency': ['数据库访问延迟', '模拟数据库慢查询或连接延迟，观察业务请求排队和端到端时延升高。'],
    'sn-cache-failure': ['缓存服务异常', '模拟缓存不可用或访问失败，观察缓存旁路、数据库压力和业务延迟。'],
};

function generalFaultScenarioTitle(s = {}) {
    const override = GENERAL_FAULT_SCENARIO_LABELS[String(s.id || '')];
    return override?.[0] || zhText(s.name || s.id || '');
}

function generalFaultScenarioDescription(s = {}) {
    const override = GENERAL_FAULT_SCENARIO_LABELS[String(s.id || '')];
    return override?.[1] || zhText(s.description || '');
}

function llmSymptomLabel(value) {
    return LLM_SYMPTOM_LABELS[value] || value || '';
}

function llmDescriptionLabel(s = {}) {
    const key = String(s.fault_type || '').toLowerCase();
    return LLM_DESCRIPTION_LABELS[key] || s.description || '';
}

function renderDiagnosisGate(gate = {}) {
    const blockers = gate.blockers || [];
    if (gate.ready) {
        return `<div class="heal-diagnosis-gate success">
            <span class="badge badge-success">真实执行准入</span>
            <span>RCA 置信度、根因和故障类型满足配置中心门禁。</span>
        </div>`;
    }
    return `<div class="heal-diagnosis-gate warning">
        <span class="badge badge-warning">真实执行受限</span>
        <span>${escapeHtml(diagnosisBlockerText(gate))}</span>
    </div>`;
}

function renderAutoExecutionGate(gate = {}) {
    if (!gate || typeof gate !== 'object' || !Object.keys(gate).length) return '';
    if (gate.ready) {
        return `<div class="heal-diagnosis-gate success">
            <span class="badge badge-success">全自动准入</span>
            <span>高置信度、历史成功率、安全规则和风险上限均满足。</span>
        </div>`;
    }
    return `<div class="heal-diagnosis-gate warning">
        <span class="badge badge-warning">全自动受限</span>
        <span>${escapeHtml(autoExecutionBlockerText(gate))}</span>
    </div>`;
}

function renderHealVerificationPlan(plan = {}) {
    const checks = plan.checks || [];
    if (!checks.length) return '';
    return `<div class="heal-verification-plan">
        <div class="heal-verification-title">
            <span class="badge badge-info">执行后验证</span>
            <strong>${escapeHtml(plan.mode || 'namespace')}</strong>
        </div>
        ${checks.map(c => `<code>${escapeHtml(c.command || '')}</code>`).join('')}
    </div>`;
}

function healPlanGateMessage(capability = {}, policy = {}) {
    const blockers = [];
    if (policy.recommend_only || capability.policy === 'recommend_only') blockers.push('配置中心处于仅建议模式');
    if (!capability.execution_enabled) blockers.push('配置中心未启用真实自愈');
    if (capability.diagnosis_ready === false) blockers.push('诊断门禁未通过');
    if (capability.requires_approval) blockers.push('真实执行需要勾选“已审批”');
    if (capability.requires_risk_ack) blockers.push('包含高风险动作，需要勾选“高风险确认”');
    if (capability.auto_execution_eligible === false) blockers.push('全自动执行资格未满足');
    const prefix = '可编辑命令和回滚命令。Dry-run 只记录计划。';
    return blockers.length ? `${prefix}\n真实执行门禁：${blockers.join('；')}。` : `${prefix}\n真实执行门禁已满足配置要求，仍建议先 Dry-run。`;
}

function renderHealPlanActionEditor(action, index) {
    const risk = (action.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="heal-plan-action" data-index="${index}">
        <div class="heal-plan-action-head">
            <label class="inline-check"><input class="heal-plan-action-enabled" type="checkbox" checked> 执行</label>
            <span class="rem-action-num">${index + 1}</span>
            <span class="badge badge-${riskClass}">${escapeHtml(riskLabel(action.risk))}</span>
            <span class="badge badge-info">${escapeHtml(sourceLabel(action.source))}</span>
            <input class="input-sm heal-plan-step" value="${escapeHtml(action.step || '')}" placeholder="动作说明">
        </div>
        <label class="config-field">
            <span>命令</span>
            <textarea class="heal-plan-command" rows="2">${escapeHtml(action.command || '')}</textarea>
        </label>
        <label class="config-field">
            <span>回滚命令</span>
            <textarea class="heal-plan-rollback" rows="1">${escapeHtml(action.rollback_command || '')}</textarea>
        </label>
        ${action.selection_reason ? `<small class="text-muted">匹配分=${escapeHtml(action.selection_score ?? '-')} · ${escapeHtml(action.selection_reason)}</small>` : ''}
        ${action.impact ? `<small class="text-muted">${escapeHtml(action.impact)}</small>` : ''}
    </div>`;
}

function renderHealBlockedPlanItem(item) {
    return `<div class="heal-plan-blocked-item">
        <strong>${escapeHtml(item.step || '已阻断')}</strong>
        <code>${escapeHtml(item.command || '')}</code>
        <small>${escapeHtml(item.blocked_reason || '')}</small>
    </div>`;
}

function closeHealPlanModal() {
    const modal = document.getElementById('heal-plan-modal');
    if (modal) modal.style.display = 'none';
}

function toggleHealPlanAll(checked) {
    document.querySelectorAll('#heal-plan-actions .heal-plan-action-enabled').forEach(el => { el.checked = checked; });
}

function collectHealPlanActions() {
    return Array.from(document.querySelectorAll('#heal-plan-actions .heal-plan-action')).map((el, index) => {
        const enabled = el.querySelector('.heal-plan-action-enabled')?.checked === true;
        return {
            enabled,
            step: el.querySelector('.heal-plan-step')?.value || `动作 ${index + 1}`,
            command: (el.querySelector('.heal-plan-command')?.value || '').trim(),
            rollback_command: (el.querySelector('.heal-plan-rollback')?.value || '').trim(),
        };
    }).filter(a => a.enabled && a.command);
}

async function executeHealPlan(forceDryRun = null) {
    const dryRunEl = document.getElementById('heal-plan-dryrun');
    const dryRun = forceDryRun === null ? dryRunEl?.checked !== false : !!forceDryRun;
    if (dryRunEl) dryRunEl.checked = dryRun;
    const actions = collectHealPlanActions();
    const out = document.getElementById('heal-plan-output');
    if (!actions.length) {
        if (out) out.textContent = '没有选中任何可执行动作。';
        return;
    }
    const capability = _currentHealPlan?.capability || {};
    const policy = _currentHealPlan?.policy || {};
    const diagnosisGate = _currentHealPlan?.diagnosis_gate || {};
    if (!dryRun) {
        const localBlockers = [];
        if (policy.recommend_only || capability.policy === 'recommend_only') localBlockers.push('配置中心处于仅建议模式');
        if (!capability.execution_enabled) localBlockers.push('配置中心未启用真实自愈');
        if (diagnosisGate.ready === false || capability.diagnosis_ready === false) localBlockers.push('诊断门禁未通过');
        if (capability.requires_approval && document.getElementById('heal-plan-approved')?.checked !== true) localBlockers.push('需要勾选“已审批”');
        if (capability.requires_risk_ack && document.getElementById('heal-plan-riskack')?.checked !== true) localBlockers.push('需要勾选“高风险确认”');
        if (localBlockers.length) {
            if (out) out.textContent = `真实执行未提交：${localBlockers.join('；')}。`;
            showToast('真实执行门禁未满足', 'error');
            return;
        }
    }
    const payload = {
        ...(_currentHealPlanSourcePayload || {}),
        namespace: _currentHealPlan?.namespace || _currentHealPlanSourcePayload?.namespace || 'default',
        fault_type: _currentHealPlan?.fault_type || _currentHealPlanSourcePayload?.fault_type || '',
        dry_run: dryRun,
        approved: document.getElementById('heal-plan-approved')?.checked === true,
        risk_ack: document.getElementById('heal-plan-riskack')?.checked === true,
        commands: actions.map(a => a.command),
        rollback_commands: actions.map(a => a.rollback_command).filter(Boolean),
        edited_actions: actions,
    };
    if (out) out.textContent = dryRun ? 'Dry-run 预演中...' : '真实执行中...';
    try {
        const res = await api('/api/heal/execute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        if (dryRun && res?.id && _currentHealPlanSourcePayload?.source_run_id) {
            _currentHealPlanSourcePayload.ontology_proposal_id = res.id;
        }
        if (out) out.textContent = JSON.stringify(res, null, 2);
        showToast(dryRun ? '自愈 dry-run 已完成' : '自愈执行已提交');
        await loadHealCenter();
    } catch (e) {
        const detail = e?.data?.detail || e?.data || e?.message || '执行失败';
        if (out) out.textContent = typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2);
        showToast('自愈执行被拦截或失败', 'error');
    }
}

async function submitHealPlanFeedback() {
    const actions = collectHealPlanActions();
    const rating = document.getElementById('heal-plan-feedback-rating')?.value || 'helpful';
    const note = document.getElementById('heal-plan-feedback-note')?.value || '';
    const payload = {
        incident_id: `heal-plan-${Date.now()}`,
        expert_diagnosis: [
            `自愈方案反馈: ${rating}`,
            `故障类型: ${_currentHealPlan?.fault_type || '-'}`,
            `目标: ${JSON.stringify(_currentHealPlan?.target || {})}`,
            `动作: ${actions.map(a => `${a.step}: ${a.command}`).join(' | ')}`,
        ].join('\n'),
        comment: note || `source=${_currentHealPlanSourcePayload?.source || 'heal-plan-modal'}`,
    };
    const res = await api('/api/knowledge/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });
    const out = document.getElementById('heal-plan-output');
    if (out) out.textContent = JSON.stringify(res || payload, null, 2);
    showToast(res ? '自愈反馈已记录' : '反馈提交失败', res ? 'success' : 'error');
}

async function showHealRun(runId) {
    const res = await api(`/api/heal/runs/${runId}`);
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

async function reverifyHealRun(runId) {
    const out = document.getElementById('heal-output');
    if (out) out.textContent = '正在重新检查恢复状态，不重复执行自愈动作…';
    try {
        const res = await api(`/api/heal/runs/${encodeURIComponent(runId)}/verify`, {
            method: 'POST', throwOnError: true,
        });
        if (out) out.textContent = JSON.stringify(res, null, 2);
        await loadHealCenter();
    } catch (e) {
        if (out) out.textContent = e.message || '恢复复核失败';
        showToast('恢复复核未完成，请查看详情', 'error');
    }
}

async function rollbackHealRun(runId) {
    const res = await api(`/api/heal/runs/${runId}/rollback`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ dry_run: true, approved: true }),
    });
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    if (res?.success) {
        showToast('自愈回滚 dry-run 已完成');
        await loadHealCenter();
    } else {
        showToast('自愈回滚 dry-run 失败', 'error');
    }
}

// ─────────────────────────────────────────
// Knowledge Base (故障知识库)
// ─────────────────────────────────────────

async function loadKnowledge() {
    const [stats, rulesData, faultsData, fbData] = await Promise.all([
        api('/api/knowledge/stats'),
        api('/api/knowledge/rules'),
        api('/api/knowledge/faults'),
        api('/api/knowledge/feedback'),
    ]);

    if (stats) {
        document.getElementById('kb-rules-count').textContent = stats.rules_count || 0;
        document.getElementById('kb-faults-count').textContent = stats.faults_count || 0;
        document.getElementById('kb-feedback-count').textContent = stats.feedback_count || 0;
        document.getElementById('kb-generated-count').textContent = stats.total_rules_generated || 0;
    }

    if (rulesData) {
        _allRules = rulesData.rules || [];
        renderRulesTable(_allRules);
    } else {
        _allRules = [];
        document.getElementById('kb-rules-body').innerHTML = '<tr><td colspan="7">规则读取不可用，请重试；不能据此认定没有规则。</td></tr>';
    }
    if (faultsData) renderFaultsTable(faultsData.faults || []);
    if (fbData) renderFeedbackList(fbData.feedback || []);
    loadFeedbackIncidents();
}

// ─────────────────────────────────────────
// Evolution / HITL / Capability Alignment
// ─────────────────────────────────────────

async function loadEvolution() {
    const [sow, evo, hitl, governance] = await Promise.all([
        api('/api/sow/alignment'),
        api('/api/evolution/report'),
        api('/api/hitl/reviews?limit=30'),
        api('/api/knowledge/governance'),
    ]);

    if (governance) {
        document.getElementById('evo-memory-health').textContent = Math.round((governance.health_score || 0) * 100) + '%';
        document.getElementById('evo-low-quality').textContent = governance.low_quality_rules || 0;
        renderMemoryGovernance(governance);
    }
    if (hitl?.stats) {
        document.getElementById('evo-hitl-pending').textContent = hitl.stats.pending || 0;
        renderHitlReviews(hitl.reviews || []);
    }
    if (evo) {
        document.getElementById('evo-snapshots').textContent = evo.total_snapshots || 0;
        const recs = evo.recommendations || [];
        document.getElementById('evolution-recommendations').innerHTML = recs.length
            ? recs.map(r => `<div class="signal-item"><span>${escapeHtml(r)}</span></div>`).join('')
            : '<p class="text-muted">暂无建议</p>';
    }
    if (sow) renderSowAlignment(sow.items || [], sow.signals || {});
}

function renderSowAlignment(items, signals) {
    const el = document.getElementById('sow-alignment-list');
    el.innerHTML = items.map(item => {
        const cls = item.status === 'implemented' ? 'success' : item.status === 'verifying' ? 'warning' : 'gray';
        return `<div class="sow-item">
            <div><strong>${escapeHtml(item.requirement)}</strong><p>${escapeHtml(item.implementation)}</p></div>
            <span class="badge badge-${cls}">${escapeHtml(item.status)}</span>
        </div>`;
    }).join('');

    if (signals.fault_scenarios) {
        el.innerHTML += `<div class="sow-footnote">故障场景 ${signals.fault_scenarios.count || 0} 个；类型：${escapeHtml((signals.fault_scenarios.fault_types || []).join(', '))}</div>`;
    }
}

function renderMemoryGovernance(g) {
    const conflicts = g.conflicts || [];
    document.getElementById('memory-governance').innerHTML = `
        <div class="grid-3col" style="margin-bottom:12px">
            <div class="mini-metric"><span>平均质量</span><strong>${Math.round((g.avg_quality_score || 0) * 100)}%</strong></div>
            <div class="mini-metric"><span>陈旧规则</span><strong>${g.stale_rules || 0}</strong></div>
            <div class="mini-metric"><span>冲突组</span><strong>${conflicts.length}</strong></div>
        </div>
        ${conflicts.length ? conflicts.map(c => `<div class="signal-item"><span><span class="badge badge-warning">冲突</span> ${escapeHtml(c.condition || '')}</span><span class="text-muted">${(c.rule_ids || []).join(', ')}</span></div>`).join('') : '<p class="text-muted">未发现明显规则冲突</p>'}
    `;
}

function renderHitlReviews(reviews) {
    const el = document.getElementById('hitl-review-list');
    if (!reviews.length) {
        el.innerHTML = '<p class="text-muted">暂无待处理审核项</p>';
        return;
    }
    el.innerHTML = reviews.map(r => {
        const root = r.rca_result?.root_cause || '';
        const score = r.judge?.combined_score ?? '-';
        const disabled = r.status !== 'pending' ? 'disabled' : '';
        return `<div class="hitl-item">
            <div class="hitl-main">
                <div><span class="badge badge-${r.priority === 'high' ? 'danger' : 'warning'}">${escapeHtml(r.priority || 'medium')}</span> <strong>${escapeHtml(r.review_id)}</strong> <span class="text-muted">${formatTime(r.created_at ? r.created_at * 1000 : null)}</span></div>
                <p>${escapeHtml(r.reason || '')}</p>
                <p><strong>根因:</strong> ${escapeHtml(root.substring(0, 180))}</p>
                <p><strong>Judge:</strong> ${score} / ${escapeHtml(String(r.judge?.judge_level ?? '-'))}</p>
                <textarea class="textarea" rows="2" id="hitl-diag-${r.review_id}" placeholder="专家诊断结论，可用于监督学习">${escapeHtml(r.expert_diagnosis || '')}</textarea>
            </div>
            <div class="hitl-actions">
                <button class="btn btn-sm btn-primary" ${disabled} onclick="decideHitl('${r.review_id}','approve')">通过</button>
                <button class="btn btn-sm btn-danger" ${disabled} onclick="decideHitl('${r.review_id}','reject')">驳回</button>
                <button class="btn btn-sm" ${disabled} onclick="decideHitl('${r.review_id}','needs_more_evidence')">补证据</button>
            </div>
        </div>`;
    }).join('');
}

async function decideHitl(reviewId, decision) {
    const diagnosis = document.getElementById(`hitl-diag-${reviewId}`)?.value?.trim() || '';
    const result = await api(`/api/hitl/reviews/${reviewId}/decision`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, reviewer: 'web', expert_diagnosis: diagnosis, comment: 'submitted from dashboard' }),
    });
    if (result?.status === 'ok') {
        showToast('审核已提交');
        loadEvolution();
        loadKnowledge();
    } else {
        showToast('审核提交失败', 'error');
    }
}

// ─────────────────────────────────────────
// 通算故障实验
// ─────────────────────────────────────────

let _faultScenarios = [];
let _k8sFaultExperimentSteps = [];
let _faultTargets = { k8s_clusters: [], llm_hosts: [] };
let _loadgenMode = 'general';
let _generalLoadgenBenchmarks = [];
let _traceSources = { jaeger: null, skywalking: null };
let _traceSource = '';
let _aiLoadgenTargets = [];
let _faultLabTab = 'single';
let _faultModeVersionState = null;
let _faultLabTopologyTargets = {};
let _selectedFaultLabTopologyTargetKey = '';
let _lastFaultLabTargetTopologyData = null;
let _lastLLMFaultTargetTopologyData = null;

function normalizeTargetOptions(values = []) {
    const seen = new Set();
    return values.flatMap(item => {
        const value = String(typeof item === 'object' && item !== null
            ? (item.value ?? item.name ?? item.id ?? '')
            : (item ?? '')).trim();
        if (!value || seen.has(value)) return [];
        seen.add(value);
        const label = String(typeof item === 'object' && item !== null
            ? (item.label ?? item.name ?? item.id ?? value)
            : value).trim() || value;
        return [{ value, label }];
    }).sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'));
}

function replaceSelectOptions(elemId, values = [], options = {}) {
    const select = document.getElementById(elemId);
    if (!select) return '';
    const previous = select.value;
    const normalized = normalizeTargetOptions(values);
    const fragment = document.createDocumentFragment();
    if (options.allowEmpty) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = options.placeholder || '使用默认值';
        fragment.appendChild(emptyOption);
    }
    normalized.forEach(item => {
        const option = document.createElement('option');
        option.value = item.value;
        option.textContent = item.label;
        fragment.appendChild(option);
    });
    if (!normalized.length && !options.allowEmpty) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = options.emptyText || '未发现可选项';
        emptyOption.disabled = true;
        fragment.appendChild(emptyOption);
    }
    select.replaceChildren(fragment);
    select.disabled = !normalized.length && !options.allowEmpty;

    const candidates = [options.preferred, previous, options.fallback]
        .map(value => String(value ?? '').trim())
        .filter(Boolean);
    const selected = candidates.find(value => normalized.some(item => item.value === value));
    if (selected) {
        select.value = selected;
    } else if (options.allowEmpty && options.preferEmpty) {
        select.value = '';
    } else if (normalized.length) {
        select.value = normalized[0].value;
    } else {
        select.value = '';
    }
    return select.value;
}

function ensureSelectOption(elemId, value, label = '') {
    const select = document.getElementById(elemId);
    const normalizedValue = String(value ?? '').trim();
    if (!select || !normalizedValue) return;
    if (select.tagName !== 'SELECT') {
        select.value = normalizedValue;
        return;
    }
    if (![...select.options].some(option => option.value === normalizedValue)) {
        const option = document.createElement('option');
        option.value = normalizedValue;
        option.textContent = String(label || normalizedValue);
        select.appendChild(option);
    }
    select.disabled = false;
    select.value = normalizedValue;
}

function topologyRecords(data, key) {
    const records = [
        ...(Array.isArray(data?.[key]) ? data[key] : []),
        ...(Array.isArray(data?.topology?.[key]) ? data.topology[key] : []),
    ];
    const seen = new Set();
    return records.filter(item => {
        const name = String(item?.name || '').trim();
        if (!name) return false;
        const recordKey = `${String(item?.namespace || '')}/${name}`;
        if (seen.has(recordKey)) return false;
        seen.add(recordKey);
        return true;
    });
}

function topologyNamespaces(...payloads) {
    const namespaces = new Set();
    payloads.filter(Boolean).forEach(data => {
        (Array.isArray(data.namespaces) ? data.namespaces : []).forEach(name => {
            if (name) namespaces.add(String(name));
        });
        ['pods', 'deployments', 'services'].forEach(key => {
            topologyRecords(data, key).forEach(item => {
                if (item.namespace) namespaces.add(String(item.namespace));
            });
        });
        const inventoryNamespace = data.inventory?.k8s?.namespace;
        if (inventoryNamespace) namespaces.add(String(inventoryNamespace));
    });
    return [...namespaces];
}

function topologyOptions(data, key, namespace = '') {
    return topologyRecords(data, key)
        .filter(item => !namespace || !item.namespace || item.namespace === namespace)
        .map(item => ({ value: item.name, label: item.name }));
}

function selectorsMatch(left = {}, right = {}) {
    const entries = Object.entries(left || {});
    return entries.length > 0 && entries.every(([key, value]) => right?.[key] === value);
}

function selectedLLMFaultCluster() {
    const clusterId = document.getElementById('llmfaultlab-cluster')?.value || '';
    return (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
}

function populateFaultLabNamespaceOptions(data = null, preferred) {
    const clusterId = document.getElementById('faultlab-cluster')?.value || '';
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    const current = document.getElementById('faultlab-ns-override')?.value || '';
    const targetValue = preferred === undefined ? current : String(preferred || '');
    const namespaces = topologyNamespaces(data);
    if (cluster.default_namespace) namespaces.push(cluster.default_namespace);
    return replaceSelectOptions('faultlab-ns-override', namespaces, {
        allowEmpty: true,
        placeholder: '使用场景默认 Namespace',
        preferred: targetValue,
        preferEmpty: !targetValue,
    });
}

function populateLLMFaultModelOptions(dashboardData = null, preferred = '') {
    const cluster = selectedLLMFaultCluster();
    const host = selectedLLMFaultHost();
    const current = document.getElementById('llmfaultlab-model')?.value || '';
    const models = aiServedModelRecords(dashboardData || _lastLLMFaultTopologyData || {}).map(record => record.name);
    [
        cluster.model,
        cluster.served_model,
        cluster.served_model_name,
        host?.model,
        host?.served_model,
        host?.served_model_name,
    ].filter(Boolean).forEach(model => models.push(model));
    return replaceSelectOptions('llmfaultlab-model', models, {
        allowEmpty: true,
        placeholder: '使用目标默认 Model',
        preferred: preferred || current || cluster.model || host?.model || '',
    });
}

function llmFaultModelForDeployment(deployment = '', dashboardData = null) {
    const target = String(deployment || '').trim();
    if (!target) return '';
    const records = aiServedModelRecords(dashboardData || _lastLLMFaultTopologyData || {});
    const matched = records.find(record =>
        record.deployment === target
        || record.app === target
        || record.pod === target
        || String(record.pod || '').startsWith(`${target}-`)
    );
    return matched?.name || '';
}

function populateLLMFaultWorkloadOptions(options = {}) {
    const cluster = selectedLLMFaultCluster();
    const namespace = document.getElementById('llmfaultlab-namespace')?.value || '';
    const topology = _lastLLMFaultTargetTopologyData || _lastLLMFaultTopologyData || {};
    const currentDeployment = document.getElementById('llmfaultlab-deployment')?.value || '';
    const currentService = document.getElementById('llmfaultlab-service')?.value || '';
    const clusterNamespace = cluster.default_namespace || cluster.namespace || cluster.vllm_namespace || '';
    const useClusterProfileDefaults = !namespace || !clusterNamespace || namespace === clusterNamespace;
    const deployments = topologyOptions(topology, 'deployments', namespace);
    if (useClusterProfileDefaults) {
        [cluster.vllm_deployment, cluster.deployment].filter(Boolean).forEach(name => deployments.push({ value: name, label: name }));
    }
    const deployment = replaceSelectOptions('llmfaultlab-deployment', deployments, {
        preferred: options.preferredDeployment || currentDeployment || cluster.vllm_deployment || cluster.deployment || '',
        emptyText: '未发现 Deployment',
    });

    const services = topologyOptions(topology, 'services', namespace);
    if (useClusterProfileDefaults) {
        [cluster.vllm_service, cluster.service].filter(Boolean).forEach(name => services.push({ value: name, label: name }));
    }
    const serviceRecords = topologyRecords(topology, 'services').filter(item => !namespace || !item.namespace || item.namespace === namespace);
    const selectedDeployment = topologyRecords(topology, 'deployments').find(item => item.name === deployment && (!namespace || !item.namespace || item.namespace === namespace));
    const matchedService = serviceRecords.find(item => selectorsMatch(item.selector, selectedDeployment?.selector))
        || serviceRecords.find(item => item.name === deployment);
    replaceSelectOptions('llmfaultlab-service', services, {
        preferred: options.preferredService || currentService || matchedService?.name || cluster.vllm_service || cluster.service || '',
        emptyText: '未发现 Service',
    });
    return {
        deployment,
        service: document.getElementById('llmfaultlab-service')?.value || '',
    };
}

function populateLLMFaultTargetSelectors(targetTopology = null, dashboardData = null, preferred = {}) {
    if (targetTopology) _lastLLMFaultTargetTopologyData = targetTopology;
    const cluster = selectedLLMFaultCluster();
    const currentNamespace = document.getElementById('llmfaultlab-namespace')?.value || '';
    const namespaces = topologyNamespaces(_lastLLMFaultTargetTopologyData, dashboardData || _lastLLMFaultTopologyData);
    [cluster.default_namespace, cluster.namespace, cluster.vllm_namespace].filter(Boolean).forEach(name => namespaces.push(name));
    replaceSelectOptions('llmfaultlab-namespace', namespaces, {
        preferred: preferred.namespace || currentNamespace || cluster.default_namespace || cluster.namespace || cluster.vllm_namespace || '',
        emptyText: '未发现 Namespace',
    });
    const selectedWorkload = populateLLMFaultWorkloadOptions({
        preferredDeployment: preferred.deployment,
        preferredService: preferred.service,
    });
    const discoveredModel = llmFaultModelForDeployment(
        selectedWorkload.deployment,
        dashboardData || _lastLLMFaultTopologyData,
    );
    populateLLMFaultModelOptions(dashboardData, preferred.model || discoveredModel || '');
}

async function loadFaultTargets() {
    const data = await api('/api/fault-targets');
    if (data) {
        _faultTargets = {
            k8s_clusters: data.k8s_clusters || [],
            llm_hosts: data.llm_hosts || [],
        };
    }
    populateClusterDropdown('faultlab-cluster');
    populateClusterDropdown('precisionfaultlab-cluster');
    populateGeneralClusterDropdown('alert-cluster-filter');
    populateAIClusterDropdown('llmfaultlab-cluster');
    populateAIClusterDropdown('ai-dashboard-cluster');
    populateAIClusterDropdown('ai-events-cluster');
    populateGeneralClusterDropdown('loadgen-general-cluster');
    populateGeneralClusterDropdown('trace-cluster');
    populateAIClusterDropdown('loadgen-ai-cluster');
    populateHostDropdown('llmfaultlab-host');
}

function populateGeneralClusterDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const previous = sel.value;
    const clusters = (_faultTargets.k8s_clusters || []).filter(
        c => String(c.kind || c.type || c.cluster_kind || 'general').toLowerCase() !== 'ai'
    );
    sel.innerHTML = clusters.length
        ? clusters.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}</option>`).join('')
        : '<option value="">未配置通算集群</option>';
    if (previous && clusters.some(c => c.id === previous)) sel.value = previous;
    else if (clusters.length) sel.value = clusters[0].id;
}

function populateClusterDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = _faultTargets.k8s_clusters.length
        ? _faultTargets.k8s_clusters.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}${c.context ? ' · ' + escapeHtml(c.context) : ''}</option>`).join('')
        : '<option value="">未配置集群</option>';
    if (prev && _faultTargets.k8s_clusters.some(c => c.id === prev)) sel.value = prev;
    if (elemId === 'faultlab-cluster') {
        sel.onchange = onFaultLabClusterChange;
        onFaultLabClusterChange(false);
    }
}

function populateAIClusterDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const prev = sel.value;
    const aiClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai');
    sel.innerHTML = aiClusters.length
        ? aiClusters.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}${c.prometheus_url ? ' · Prometheus' : ''}</option>`).join('')
        : '<option value="">未配置智算集群</option>';
    if (prev && aiClusters.some(c => c.id === prev)) {
        sel.value = prev;
    } else if (aiClusters.length) {
        sel.value = aiClusters[0].id;
    }
}

function populateHostDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const prev = sel.value;
    const configuredHosts = (_faultTargets.llm_hosts || []).filter(h => String(h.host || '').trim());
    sel.innerHTML = configuredHosts.length
        ? configuredHosts.map(h => `<option value="${escapeHtml(h.id)}">${escapeHtml(h.name || h.id)} · ${escapeHtml(h.host || '')}</option>`).join('')
        : '<option value="">未配置 GPU 主机</option>';
    if (prev && configuredHosts.some(h => h.id === prev)) {
        sel.value = prev;
    } else if (elemId === 'llmfaultlab-host') {
        const observableHost = configuredHosts.find(h => h.prometheus_url);
        if (observableHost) sel.value = observableHost.id;
        else if (configuredHosts.length) sel.value = configuredHosts[0].id;
    }
    if (elemId === 'llmfaultlab-host') {
        sel.onchange = onLLMFaultHostChange;
        onLLMFaultHostChange(false);
    }
}

function selectedLLMFaultHost() {
    const hostId = document.getElementById('llmfaultlab-host')?.value || '';
    return (_faultTargets.llm_hosts || []).find(h => h.id === hostId) || null;
}

function onLLMFaultHostChange(refreshMetrics = true) {
    const host = selectedLLMFaultHost();
    populateLLMFaultModelOptions(null, host?.model || host?.served_model || host?.served_model_name || '');
    renderLLMFaultParamHelp();
}

function onLLMFaultClusterChange() {
    _lastLLMFaultTargetTopologyData = null;
    _lastLLMFaultTopologyData = null;
    populateLLMFaultTargetSelectors();
    loadLLMFaultTopology();
}

function onLLMFaultNamespaceChange(options = {}) {
    populateLLMFaultWorkloadOptions({
        preferredDeployment: options.preferredDeployment || '',
        preferredService: options.preferredService || '',
    });
}

function onLLMFaultDeploymentChange(preferredService = '') {
    const selectedWorkload = populateLLMFaultWorkloadOptions({
        preferredDeployment: document.getElementById('llmfaultlab-deployment')?.value || '',
        preferredService,
    });
    const discoveredModel = llmFaultModelForDeployment(selectedWorkload.deployment);
    if (discoveredModel) populateLLMFaultModelOptions(null, discoveredModel);
}

async function loadFaultLab() {
    await loadFaultTargets();
    const data = await api('/api/faultlab/scenarios');
    _faultScenarios = data?.scenarios || [];
    const sel = document.getElementById('faultlab-scenario');
    if (!sel) return;
    sel.innerHTML = _faultScenarios.map(s => `<option value="${s.id}">${s.id} — ${escapeHtml(generalFaultScenarioTitle(s))}</option>`).join('');
    sel.onchange = renderFaultScenarioDetail;
    renderFaultScenarioDetail();
    renderFaultLabSummary();
    renderFaultLabParamHelp();
    renderFaultScenarioGrid();
    renderK8sFaultExperimentSteps();
    loadK8sFaultExperiments();
    loadFaultLabTopology();
}

function switchFaultLabTab(tab) {
    _faultLabTab = tab || 'single';
    document.querySelectorAll('[data-faultlab-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.faultlabTab === _faultLabTab);
    });
    document.querySelectorAll('#view-faultlab .llmfaultlab-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `faultlab-tab-${_faultLabTab}`);
    });
    if (_faultLabTab === 'sequence') {
        renderK8sFaultExperimentSteps();
        loadK8sFaultExperiments();
    } else if (_faultLabTab === 'precision') {
        openPrecisionFaultLab();
    } else if (_faultLabTab === 'catalog') {
        renderFaultScenarioGrid();
        loadFaultModeVersionManager();
        requestAnimationFrame(() => document.querySelector('#view-faultlab')?.scrollIntoView({ block: 'start' }));
    } else if (_faultLabTab === 'single') {
        loadFaultLabTopology();
    }
}

function faultModeFamily() {
    return document.getElementById('fault-mode-family')?.value || 'general';
}

function selectedFaultModeVersion(id = 'fault-mode-version') {
    return document.getElementById(id)?.value || '';
}

function faultModeVersionLabel(item, activeVersion) {
    const active = item.version === activeVersion ? ' · 当前生效' : '';
    const date = item.created_at ? new Date(Number(item.created_at) * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    return `${item.version}${active} · ${item.scenario_count || 0} 场景${date ? ` · ${date}` : ''}`;
}

function faultModeOutputLoading(message) {
    const output = document.getElementById('fault-mode-version-output');
    if (output) output.innerHTML = `<div class="fault-mode-empty"><span class="spinner"></span>${escapeHtml(message)}</div>`;
}

function faultModeOutputError(error) {
    const output = document.getElementById('fault-mode-version-output');
    if (output) output.innerHTML = `<div class="alert alert-danger">${escapeHtml(error?.message || String(error || '操作失败'))}</div>`;
}

function renderFaultModeVersionList(family) {
    const output = document.getElementById('fault-mode-version-output');
    if (!output) return;
    const versions = family?.versions || [];
    if (!versions.length) {
        output.innerHTML = '<div class="fault-mode-empty">尚无版本快照。点击“生成当前快照”建立第一个可对比、可回退的基线版本。</div>';
        return;
    }
    output.innerHTML = `
        <div class="fault-mode-result-head"><div><span class="fault-mode-kicker">版本时间线</span><h4>${escapeHtml(family.label || family.family)}</h4></div><span>${versions.length} 个不可变快照</span></div>
        <div class="fault-mode-version-list">${versions.map(item => `
            <div class="fault-mode-version-row ${item.version === family.active_version ? 'active' : ''}">
                <div><strong>${escapeHtml(item.version)}</strong>${item.version === family.active_version ? '<span class="badge badge-success">当前生效</span>' : ''}<p>${escapeHtml(item.note || '未填写版本说明')}</p></div>
                <div class="fault-mode-version-meta"><span>${item.scenario_count || 0} 场景</span><span>${escapeHtml(String(item.checksum || '').slice(0, 10))}</span><span>${item.created_at ? escapeHtml(new Date(Number(item.created_at) * 1000).toLocaleString('zh-CN', { hour12: false })) : '-'}</span></div>
            </div>`).join('')}</div>`;
}

async function loadFaultModeVersionManager() {
    const familyName = faultModeFamily();
    const summary = document.getElementById('fault-mode-version-summary');
    try {
        const data = await api(`/api/fault-mode-versions?family=${encodeURIComponent(familyName)}`, { throwOnError: true });
        const family = data?.families?.[0] || { family: familyName, versions: [] };
        _faultModeVersionState = family;
        const versions = family.versions || [];
        const options = versions.map(item => `<option value="${escapeHtmlAttr(item.version)}">${escapeHtml(faultModeVersionLabel(item, family.active_version))}</option>`).join('');
        const emptyOption = '<option value="">暂无版本快照</option>';
        ['fault-mode-version', 'fault-mode-compare-from', 'fault-mode-compare-to'].forEach(id => {
            const select = document.getElementById(id);
            if (select) select.innerHTML = options || emptyOption;
        });
        const from = document.getElementById('fault-mode-compare-from');
        const to = document.getElementById('fault-mode-compare-to');
        if (from && versions.length > 1) from.value = versions[versions.length - 1].version;
        if (to && versions.length) to.value = versions[0].version;
        if (summary) summary.innerHTML = `
            <div class="fault-mode-stat"><span>目录种类</span><strong>${escapeHtml(family.label || familyName)}</strong></div>
            <div class="fault-mode-stat"><span>工作区场景</span><strong>${family.current_scenario_count ?? (familyName === 'general' ? _faultScenarios.length : '-')}</strong></div>
            <div class="fault-mode-stat"><span>版本数量</span><strong>${versions.length}</strong></div>
            <div class="fault-mode-stat"><span>当前生效</span><strong class="fault-mode-version-name">${escapeHtml(family.active_version || '工作区目录')}</strong></div>
            <div class="fault-mode-stat"><span>工作区校验和</span><strong class="fault-mode-version-name">${escapeHtml(String(family.current_checksum || '').slice(0, 10) || '-')}</strong></div>`;
        renderFaultModeVersionList(family);
    } catch (error) {
        faultModeOutputError(error);
    }
}

async function snapshotFaultModeCatalog() {
    const family = faultModeFamily();
    faultModeOutputLoading('正在生成不可变目录快照…');
    try {
        const result = await api(`/api/fault-mode-versions/${encodeURIComponent(family)}/snapshot`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, throwOnError: true,
            body: JSON.stringify({ actor: 'dashboard', note: '由故障实验控制台生成的目录基线' }),
        });
        showToast(`已生成版本 ${result.version}`);
        await loadFaultModeVersionManager();
    } catch (error) {
        faultModeOutputError(error);
    }
}

function renderFaultModeDiff(result) {
    const output = document.getElementById('fault-mode-version-output');
    if (!output) return;
    const changed = result.changed || [];
    const added = result.added || [];
    const removed = result.removed || [];
    const scenarioRows = [
        ...added.map(item => ({ id: item.id, type: '新增', fields: ['完整场景'] })),
        ...removed.map(item => ({ id: item.id, type: '删除', fields: ['完整场景'] })),
        ...changed.map(item => ({ id: item.id, type: '修改', fields: item.fields || [] })),
    ];
    output.innerHTML = `
        <div class="fault-mode-result-head"><div><span class="fault-mode-kicker">版本对比</span><h4>${escapeHtml(result.from_version)} → ${escapeHtml(result.to_version)}</h4></div><span>按场景 ID 与字段路径比较</span></div>
        <div class="fault-mode-diff-stats"><div class="added"><strong>${added.length}</strong><span>新增</span></div><div class="changed"><strong>${changed.length}</strong><span>修改</span></div><div class="removed"><strong>${removed.length}</strong><span>删除</span></div></div>
        ${scenarioRows.length ? `<div class="fault-mode-change-list">${scenarioRows.map(item => `<div><span class="fault-mode-change-type ${item.type}">${item.type}</span><strong>${escapeHtml(item.id)}</strong><p>${escapeHtml(item.fields.join('、'))}</p></div>`).join('')}</div>` : '<div class="fault-mode-empty success">两个版本内容一致，没有场景或字段变化。</div>'}`;
}

async function compareFaultModeVersions() {
    const family = faultModeFamily();
    const fromVersion = selectedFaultModeVersion('fault-mode-compare-from');
    const toVersion = selectedFaultModeVersion('fault-mode-compare-to');
    if (!fromVersion || !toVersion) return showToast('至少需要一个版本快照', 'error');
    faultModeOutputLoading('正在比较场景与字段变化…');
    try {
        const query = new URLSearchParams({ from_version: fromVersion, to_version: toVersion });
        const result = await api(`/api/fault-mode-versions/${encodeURIComponent(family)}/diff?${query}`, { throwOnError: true });
        renderFaultModeDiff(result);
    } catch (error) {
        faultModeOutputError(error);
    }
}

async function previewFaultModeMerge() {
    const family = faultModeFamily();
    const baseVersion = selectedFaultModeVersion('fault-mode-compare-from');
    const incomingVersion = selectedFaultModeVersion('fault-mode-compare-to');
    if (!baseVersion || !incomingVersion) return showToast('请选择两个版本进行合并预检', 'error');
    faultModeOutputLoading('正在执行确定性合并预检…');
    try {
        const result = await api(`/api/fault-mode-versions/${encodeURIComponent(family)}/merge`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, throwOnError: true,
            body: JSON.stringify({ base_version: baseVersion, incoming_version: incomingVersion, commit: false }),
        });
        const output = document.getElementById('fault-mode-version-output');
        const conflicts = result.conflicts || [];
        output.innerHTML = `
            <div class="fault-mode-result-head"><div><span class="fault-mode-kicker">合并预检</span><h4>${escapeHtml(baseVersion)} + ${escapeHtml(incomingVersion)}</h4></div><span>${result.scenario_count || 0} 个可直接合并场景</span></div>
            <div class="fault-mode-merge-status ${conflicts.length ? 'conflict' : 'ready'}"><strong>${conflicts.length ? `${conflicts.length} 个冲突待人工裁决` : '无冲突，可创建合并版本'}</strong><p>同一场景 ID 的不同定义不会由大模型自动裁决。</p></div>
            ${conflicts.length ? `<div class="fault-mode-change-list">${conflicts.map(item => `<div><span class="fault-mode-change-type 修改">冲突</span><strong>${escapeHtml(item.id)}</strong><p>${escapeHtml((item.fields || []).join('、'))}</p></div>`).join('')}</div>` : ''}`;
    } catch (error) {
        faultModeOutputError(error);
    }
}

function renderFaultModeAnalysis(result) {
    const output = document.getElementById('fault-mode-version-output');
    if (!output) return;
    const analysis = result.analysis || {};
    const llm = result.llm;
    const classes = Object.entries(analysis.class_counts || {});
    const risks = Object.entries(analysis.risk_counts || {});
    output.innerHTML = `
        <div class="fault-mode-result-head"><div><span class="fault-mode-kicker">总结与分类</span><h4>${escapeHtml(analysis.version || 'working-tree')} · ${analysis.scenario_count || 0} 个场景</h4></div><span>规则基线${llm ? ' + 大模型辅助' : ''}</span></div>
        <div class="fault-mode-analysis-grid">
            <section><h5>确定性种类分布</h5>${classes.map(([name, count]) => `<div class="fault-mode-bar-row"><span>${escapeHtml(name)}</span><div><i style="width:${Math.max(8, Math.round(count / Math.max(1, analysis.scenario_count) * 100))}%"></i></div><strong>${count}</strong></div>`).join('') || '<p>暂无分类数据</p>'}</section>
            <section><h5>风险分布</h5><div class="fault-mode-tags">${risks.map(([name, count]) => `<span>${escapeHtml(name)} <b>${count}</b></span>`).join('') || '<span>未标注</span>'}</div><p class="fault-mode-help">规则分类提供稳定基线，可复现、可审计。</p></section>
        </div>
        ${llm ? `<section class="fault-mode-llm-result"><div><span class="badge badge-info">辅助结论</span><strong>大模型语义归纳</strong><em>${escapeHtml(llm.model || '当前有效模型')}${Number.isFinite(Number(llm.batch_count)) ? ` · ${escapeHtml(llm.succeeded_batches || 0)}/${escapeHtml(llm.batch_count)} 批成功` : ''} · 不参与发布与回退决策</em></div><p>${escapeHtml(llm.summary || llm.reason || (llm.status === 'ok' ? '已完成语义分类，详见下方分组。' : '大模型分析不可用。'))}</p>${Array.isArray(llm.classes) ? `<div class="fault-mode-llm-classes">${llm.classes.map(item => `<article><strong>${escapeHtml(item.name || '未命名种类')}</strong><p>${escapeHtml(item.reason || '')}</p><small>${escapeHtml((item.scenario_ids || []).join('、'))}</small></article>`).join('')}</div>` : ''}</section>` : ''}`;
}

async function analyzeFaultModeCatalog() {
    const family = faultModeFamily();
    const version = selectedFaultModeVersion();
    const useLlm = document.getElementById('fault-mode-use-llm')?.checked === true;
    faultModeOutputLoading(useLlm ? '正在生成规则基线并请求大模型辅助分析…' : '正在执行可复现的规则分类…');
    try {
        const result = await api(`/api/fault-mode-versions/${encodeURIComponent(family)}/analyze`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, throwOnError: true, timeoutMs: useLlm ? 90000 : 30000,
            body: JSON.stringify({ version, use_llm: useLlm }),
        });
        renderFaultModeAnalysis(result);
    } catch (error) {
        faultModeOutputError(error);
    }
}

async function rollbackFaultModeCatalog() {
    const family = faultModeFamily();
    const version = selectedFaultModeVersion();
    if (!version) return showToast('请选择要切换的目录版本', 'error');
    if (!window.confirm(`确认将 ${family} 故障模式目录切换到 ${version}？\n该操作不会执行运行时故障清理或自愈。`)) return;
    faultModeOutputLoading('正在校验并切换目录版本…');
    try {
        const result = await api(`/api/fault-mode-versions/${encodeURIComponent(family)}/rollback`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, throwOnError: true,
            body: JSON.stringify({ version, actor: 'dashboard', reason: '人工选择目录版本' }),
        });
        showToast(`当前生效版本：${result.active_version}`);
        await loadFaultModeVersionManager();
        if (family === 'general') await loadFaultLab();
    } catch (error) {
        faultModeOutputError(error);
    }
}

function onFaultLabClusterChange(refreshTopology = true) {
    const clusterId = document.getElementById('faultlab-cluster')?.value || '';
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    _lastFaultLabTargetTopologyData = null;
    populateFaultLabNamespaceOptions(null, '');
    const hint = document.getElementById('faultlab-cluster-hint');
    if (hint) {
        const kubectl = cluster.kubectl_path || 'kubectl';
        const ctx = cluster.context ? ` · context=${cluster.context}` : '';
        const kc = cluster.kubeconfig ? ' · kubeconfig 已配置' : ' · 使用容器默认 kubeconfig';
        hint.textContent = cluster.id ? `kubectl=${kubectl}${ctx}${kc}` : '请选择目标 K8s 集群。';
    }
    if (refreshTopology) loadFaultLabTopology();
}

function renderFaultLabSummary() {
    const el = document.getElementById('faultlab-summary');
    if (!el) return;
    const clusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || 'general').toLowerCase() !== 'ai');
    const categories = [...new Set(_faultScenarios.map(s => s.category).filter(Boolean))];
    const faultTypes = [...new Set(_faultScenarios.map(s => s.fault_type).filter(Boolean))];
    el.innerHTML = `
        <div class="mini-metric"><span>通算集群</span><strong>${clusters.length || _faultTargets.k8s_clusters.length || 0}</strong></div>
        <div class="mini-metric"><span>场景数</span><strong>${_faultScenarios.length}</strong></div>
        <div class="mini-metric"><span>故障类型</span><strong>${faultTypes.length}</strong></div>
        <div class="mini-metric"><span>覆盖类别</span><strong>${categories.length || '-'}</strong></div>
    `;
}

function renderFaultLabParamHelp() {
    const el = document.getElementById('faultlab-param-help');
    if (!el) return;
    el.innerHTML = [
        '<div><code>目标集群</code>: 使用配置中心的 K8s 集群档案，命令自动带上 kubectl_path、kubeconfig 和 context。</div>',
        '<div><code>Namespace 覆盖</code>: 留空时使用场景默认命名空间；从依赖图选择目标后会自动填充。</div>',
        '<div><code>背景负载</code>: 注入前后生成业务侧背景流量，用于观察传播效果。</div>',
        '<div><code>Dry-run</code>: 默认只生成注入和清理命令，不修改集群。</div>',
        '<div><code>确认真实执行</code>: 取消 Dry-run 后必须勾选，才允许真实注入或清理。</div>',
        '<div><code>依赖图注入</code>: 点击节点或连线后自动推荐适配场景，仍受 Dry-run 和确认保护。</div>',
    ].join('');
}

function selectedFaultScenario() {
    const id = document.getElementById('faultlab-scenario')?.value;
    return _faultScenarios.find(s => s.id === id);
}

function renderFaultScenarioDetail() {
    const s = selectedFaultScenario();
    const el = document.getElementById('faultlab-scenario-detail');
    if (!s || !el) return;
    const cmds = s.inject?.commands || [];
    el.innerHTML = `<div class="fault-detail">
        <div><span class="badge badge-info">${escapeHtml(faultCategoryLabel(s.category))}</span> <span class="badge badge-warning">${escapeHtml(faultTypeLabel(s.fault_type))}</span> <strong>${escapeHtml(s.target_service || '')}</strong></div>
        <p>${escapeHtml(generalFaultScenarioDescription(s))}</p>
        <pre>${escapeHtml(cmds.join('\n'))}</pre>
    </div>`;
}

function renderFaultScenarioGrid() {
    const el = document.getElementById('faultlab-grid');
    if (!el) return;
    el.innerHTML = _faultScenarios.map(s => `<div class="fault-card">
        <div><span class="badge badge-info">${escapeHtml(faultCategoryLabel(s.category))}</span></div>
        <strong>${escapeHtml(generalFaultScenarioTitle(s))}</strong>
        <p>${escapeHtml(generalFaultScenarioDescription(s).substring(0, 120))}</p>
    </div>`).join('');
}

function scenarioForFaultLabTarget(target) {
    const kind = String(target?.kind || '').toLowerCase();
    const text = `${target?.name || ''} ${target?.fault_reason || ''}`.toLowerCase();
    const candidates = _faultScenarios;
    const byType = types => candidates.find(s => types.includes(String(s.fault_type || '').toLowerCase()));
    if (kind === 'edge' || text.includes('network')) return byType(['network_delay', 'network_loss', 'network_partition', 'network_policy_deny']) || candidates[0];
    if (kind === 'node') return byType(['node_not_ready', 'cpu_stress', 'memory_stress', 'disk_pressure']) || candidates[0];
    if (kind === 'pod') return byType(['pod_kill', 'container_crash', 'oom_killed', 'pod_delete']) || candidates[0];
    if (kind === 'deployment') return byType(['deployment_scale_zero', 'image_pull_error', 'deployment_unhealthy']) || candidates[0];
    if (kind === 'service') return byType(['service_endpoint_down', 'network_delay', 'service_unavailable']) || candidates[0];
    return candidates[0];
}

function buildFaultLabTarget(kind, item, data, byId = {}) {
    const clusterId = document.getElementById('faultlab-cluster')?.value || '';
    if (kind === 'edge') {
        const from = byId[item.from] || {};
        const to = byId[item.to] || {};
        return {
            kind: 'edge',
            name: `${from.name || item.from} -> ${to.name || item.to}`,
            namespace: to.namespace || from.namespace || '',
            reason: '依赖边适合注入网络延迟、丢包或依赖阻断类故障，用于验证故障传播。',
            cluster_id: clusterId,
        };
    }
    return {
        kind,
        name: item.name || '-',
        namespace: item.namespace || '',
        deployment: kind === 'deployment' ? item.name : '',
        pod: kind === 'pod' ? item.name : '',
        node: kind === 'node' ? item.name : '',
        service: kind === 'service' ? item.name : '',
        fault_reason: item.status || item.fault_reason || '',
        reason: item.faulty ? '该实体当前存在异常信号，可用于复现实验或验证自愈。' : '该实体可作为通算故障注入目标。',
        cluster_id: clusterId,
    };
}

function openFaultLabTargetPicker(key) {
    const target = _faultLabTopologyTargets[key];
    if (!target) return;
    _selectedFaultLabTopologyTargetKey = key;
    const existing = document.getElementById('faultlab-target-picker');
    if (existing) existing.remove();
    const container = document.getElementById('faultlab-topology');
    if (!container) return;
    const scenario = scenarioForFaultLabTarget(target);
    const scenarioOptions = _faultScenarios.map(s =>
        `<option value="${escapeHtml(s.id)}" ${scenario && s.id === scenario.id ? 'selected' : ''}>${escapeHtml(s.id)} - ${escapeHtml(generalFaultScenarioTitle(s))}</option>`
    ).join('');
    const panel = document.createElement('div');
    panel.id = 'faultlab-target-picker';
    panel.className = 'llm-fault-target-picker';
    panel.innerHTML = `
        <div class="llm-fault-target-head">
            <div>
                <span class="badge badge-info">通算拓扑注入目标</span>
                <strong>${escapeHtml(target.name || '-')}</strong>
                <small>${escapeHtml(target.reason || '')}</small>
            </div>
            <button class="btn btn-sm" onclick="document.getElementById('faultlab-target-picker')?.remove()">关闭</button>
        </div>
        <div class="llm-fault-target-grid">
            <div><span>类型</span><strong>${escapeHtml(target.kind || '-')}</strong></div>
            <div><span>Namespace</span><strong>${escapeHtml(target.namespace || '-')}</strong></div>
            <div><span>集群</span><strong>${escapeHtml(target.cluster_id || '-')}</strong></div>
        </div>
        <label class="llm-fault-target-scenario">
            <span>故障场景</span>
            <select id="faultlab-target-scenario" class="select-sm">${scenarioOptions}</select>
        </label>
        <div class="llm-fault-target-actions">
            <button class="btn btn-sm btn-primary" onclick="applyFaultLabTargetToLab()">填充目标</button>
            <button class="btn btn-sm" onclick="injectFaultScenario()">Dry-run 注入</button>
        </div>
    `;
    container.appendChild(panel);
}

async function applyFaultLabTargetToLab() {
    const target = _faultLabTopologyTargets[_selectedFaultLabTopologyTargetKey];
    if (!target) return;
    switchFaultLabTab('single');
    if (target.cluster_id) setVal('faultlab-cluster', target.cluster_id);
    if (target.namespace) ensureSelectOption('faultlab-ns-override', target.namespace);
    const selectedScenarioId = document.getElementById('faultlab-target-scenario')?.value || '';
    const scenario = _faultScenarios.find(s => s.id === selectedScenarioId) || scenarioForFaultLabTarget(target);
    if (scenario) {
        setVal('faultlab-scenario', scenario.id);
        renderFaultScenarioDetail();
    }
    const out = document.getElementById('faultlab-output');
    if (out) {
        out.textContent = [
            '已从通算依赖图选择注入目标。',
            `目标: ${target.name || '-'}`,
            `类型: ${target.kind || '-'}`,
            `Namespace: ${target.namespace || '-'}`,
            `推荐场景: ${scenario ? `${scenario.id} / ${faultTypeLabel(scenario.fault_type)}` : '-'}`,
            `说明: ${target.reason || '-'}`,
            '当前仍为 Dry-run 模式；执行真实注入前需要取消 Dry-run 并勾选确认真实执行。'
        ].join('\n');
    }
}

async function loadFaultLabTopology() {
    const status = document.getElementById('faultlab-topology-status');
    const el = document.getElementById('faultlab-topology');
    if (!el) return;
    const clusterId = document.getElementById('faultlab-cluster')?.value || '';
    if (status) status.innerHTML = '<span class="badge badge-info">加载中</span> 正在读取通算集群依赖图...';
    try {
        const url = clusterId ? `/api/cluster/topology?cluster_id=${encodeURIComponent(clusterId)}` : '/api/cluster/topology';
        const data = await api(url, { throwOnError: true });
        _lastFaultLabTargetTopologyData = data;
        populateFaultLabNamespaceOptions(data);
        renderFaultLabTopology(data);
        const s = data.summary || {};
        if (status) status.innerHTML = `<span class="badge badge-success">已加载</span> ${escapeHtml(data.cluster?.name || clusterId || '默认集群')} · 节点 ${s.total_nodes || 0} · Service ${s.total_services || 0} · Deployment ${s.total_deployments || 0} · Pod ${s.total_pods || 0}`;
    } catch (err) {
        populateFaultLabNamespaceOptions();
        if (status) status.innerHTML = '<span class="badge badge-danger">失败</span> 依赖图加载失败，可继续使用表单注入。';
        el.innerHTML = `<div class="ai-empty-note">${escapeHtml(err?.message || '依赖图加载失败')}</div>`;
    }
}

function renderFaultLabTopology(data) {
    const el = document.getElementById('faultlab-topology');
    if (!el) return;
    const nodes = data.nodes || [];
    const pods = data.pods || [];
    const deployments = data.deployments || [];
    const services = data.services || [];
    if (!nodes.length && !pods.length && !deployments.length && !services.length) {
        el.innerHTML = '<div class="ai-empty-note">未获取到 K8s 拓扑数据。</div>';
        return;
    }
    const namespaces = [...new Set(pods.map(p => p.namespace).filter(Boolean))].slice(0, 5);
    const visibleServices = services.slice(0, 6);
    const visibleDeployments = deployments.slice(0, 7);
    const visiblePods = [
        ...pods.filter(p => p.is_faulty),
        ...pods.filter(p => !p.is_faulty),
    ].slice(0, 14);
    const visibleNodes = nodes.slice(0, 6);
    const graphNodes = [];
    const graphEdges = [];
    const addNode = (id, kind, name, meta, x, y, status = '', faulty = false, extra = {}) => graphNodes.push({ id, kind, name, meta, x, y, status, faulty, ...extra });
    const addEdge = (from, to, label = '') => {
        if (graphNodes.some(n => n.id === from) && graphNodes.some(n => n.id === to)) graphEdges.push({ from, to, label });
    };
    const rowY = (index, total, height = 68, gap = 24) => Math.round(64 + index * (height + gap) + Math.max(0, 4 - total) * 18);
    namespaces.forEach((ns, i) => addNode(`ns:${ns}`, 'namespace', ns, `${pods.filter(p => p.namespace === ns).length} Pod`, 28, rowY(i, namespaces.length), '', false, { namespace: ns }));
    visibleServices.forEach((svc, i) => addNode(`svc:${svc.namespace}/${svc.name}`, 'service', svc.name, svc.namespace || '-', 268, rowY(i, visibleServices.length), svc.type || '', false, { namespace: svc.namespace }));
    visibleDeployments.forEach((dep, i) => addNode(`dep:${dep.namespace}/${dep.name}`, 'deployment', dep.name, `${dep.namespace || '-'} · ${dep.available || 0}/${dep.replicas || 0}`, 508, rowY(i, visibleDeployments.length), dep.is_faulty ? '异常' : 'OK', dep.is_faulty, { namespace: dep.namespace }));
    visiblePods.forEach((pod, i) => addNode(`pod:${pod.namespace}/${pod.name}`, 'pod', pod.name, `${pod.namespace || '-'} · ${pod.ready || '-'}`, 748, rowY(i, visiblePods.length, 68, 16), pod.phase || '', pod.is_faulty, { namespace: pod.namespace, fault_reason: pod.fault_reason }));
    visibleNodes.forEach((node, i) => addNode(`node:${node.name}`, 'node', node.name, `${(node.roles || []).join(',') || 'worker'} · CPU ${node.cpu || '-'}`, 988, rowY(i, visibleNodes.length), node.ready ? 'Ready' : 'NotReady', !node.ready));
    const matchesSelector = (selector, pod) => {
        const labels = pod.labels || {};
        return selector && Object.keys(selector).length && Object.entries(selector).every(([k, v]) => labels[k] === v);
    };
    visibleServices.forEach(svc => {
        addEdge(`ns:${svc.namespace}`, `svc:${svc.namespace}/${svc.name}`, '包含');
        visiblePods.filter(p => p.namespace === svc.namespace && matchesSelector(svc.selector, p)).slice(0, 3).forEach(p => addEdge(`svc:${svc.namespace}/${svc.name}`, `pod:${p.namespace}/${p.name}`, 'selector'));
    });
    visibleDeployments.forEach(dep => {
        addEdge(`ns:${dep.namespace}`, `dep:${dep.namespace}/${dep.name}`, '包含');
        visiblePods.filter(p => p.namespace === dep.namespace && matchesSelector(dep.selector, p)).slice(0, 3).forEach(p => addEdge(`dep:${dep.namespace}/${dep.name}`, `pod:${p.namespace}/${p.name}`, '控制'));
    });
    visiblePods.forEach(p => addEdge(`pod:${p.namespace}/${p.name}`, `node:${p.node}`, '调度'));
    const byId = Object.fromEntries(graphNodes.map(n => [n.id, n]));
    const safeKey = text => String(text || '').replace(/[^A-Za-z0-9_-]/g, '_');
    _faultLabTopologyTargets = {};
    graphNodes.forEach(n => { _faultLabTopologyTargets[`node_${safeKey(n.id)}`] = buildFaultLabTarget(n.kind, n, data); });
    graphEdges.forEach(e => { _faultLabTopologyTargets[`edge_${safeKey(e.from + '_' + e.to)}`] = buildFaultLabTarget('edge', e, data, byId); });
    const nodeWidth = 176;
    const nodeHeight = 68;
    const svgWidth = 1210;
    const svgHeight = Math.max(360, Math.max(0, ...graphNodes.map(n => n.y + nodeHeight)) + 42);
    const pathFor = e => {
        const a = byId[e.from];
        const b = byId[e.to];
        const x1 = a.x + nodeWidth;
        const y1 = a.y + 34;
        const x2 = b.x;
        const y2 = b.y + 34;
        const mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    };
    const clip = (text, max = 18) => {
        const value = String(text || '-');
        return value.length > max ? `${value.slice(0, max - 1)}...` : value;
    };
    el.innerHTML = `
        <div class="llm-topology-hint">点击节点或连线选择注入目标；选择后会自动填充 Namespace 和推荐场景，真实执行仍受 Dry-run 与确认保护。</div>
        <div class="ai-topology-canvas llm-fault-topology-canvas general-fault-topology-canvas" style="--llm-topology-width: ${svgWidth}px; --llm-topology-height: ${svgHeight}px;">
            <svg viewBox="0 0 ${svgWidth} ${svgHeight}" role="img" aria-label="通算故障依赖图">
                <defs>
                    <marker id="generalFaultArrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L0,6 L8,3 z"></path>
                    </marker>
                </defs>
                ${graphEdges.map(e => `<path class="ai-topology-edge llm-fault-edge" d="${pathFor(e)}"></path>`).join('')}
                ${graphEdges.map(e => `<path class="ai-topology-edge-hit llm-fault-clickable" d="${pathFor(e)}" onclick="openFaultLabTargetPicker('edge_${safeKey(e.from + '_' + e.to)}')"></path>`).join('')}
                ${graphNodes.map(n => `<g class="ai-topology-node topo-dep-node llm-fault-clickable ${escapeHtml(n.kind)} ${n.faulty ? 'faulty' : ''}" transform="translate(${n.x}, ${n.y})" onclick="openFaultLabTargetPicker('node_${safeKey(n.id)}')">
                    <rect width="${nodeWidth}" height="${nodeHeight}" rx="5"></rect>
                    <text x="12" y="18" class="kind">${escapeHtml(n.kind)}</text>
                    <text x="12" y="39" class="name">${escapeHtml(clip(n.name, 18))}</text>
                    <text x="12" y="57" class="meta">${escapeHtml(clip(n.meta, 24))}</text>
                    ${n.status ? `<text x="164" y="18" class="status" text-anchor="end">${escapeHtml(clip(n.status, 8))}</text>` : ''}
                </g>`).join('')}
            </svg>
        </div>
    `;
}

function faultLabBaseBody() {
    const dryRun = document.getElementById('faultlab-dryrun')?.checked !== false;
    const confirmed = document.getElementById('faultlab-confirm')?.checked === true;
    const body = {
        confirm: !dryRun && confirmed,
        experiment_ack: !dryRun && confirmed,
    };
    const clusterId = document.getElementById('faultlab-cluster')?.value || '';
    if (clusterId) body.cluster_id = clusterId;
    const nsOverride = document.getElementById('faultlab-ns-override')?.value?.trim();
    if (nsOverride) body.namespace = nsOverride;
    return body;
}

function faultLabEnsureConfirmed(action, scenario, body) {
    if (body.dry_run !== false) return true;
    if (!body.confirm) {
        alert('取消 Dry-run 后需要先勾选“确认真实执行”。');
        return false;
    }
    return confirm(`${action}: ${scenario?.name || scenario?.id || ''}\n\n${scenario?.description || ''}`);
}

function normalizeK8sSkillFaultType(raw = '') {
    const text = String(raw || '').toLowerCase();
    if (/(crashloop|crash_loop|container_crash|application_crash|pod_kill|pod_delete|restart)/.test(text)) return 'pod_crashloop';
    if (/(oom|memory|mem_pressure)/.test(text)) return 'oom_killed';
    if (/(cpu|resource|pressure|saturation|capacity)/.test(text)) return 'resource_pressure';
    if (/(service_unavailable|service_endpoint|scale_down|dependency_timeout|dependency_failure|cache_failure|database_degradation|503)/.test(text)) return 'service_unavailable';
    if (/(network|partition|loss|delay|deny|policy)/.test(text)) return 'network_policy_deny';
    if (/(node_not_ready|node)/.test(text)) return 'node_not_ready';
    if (/(schedule|scheduling|pending|capacity_failure)/.test(text)) return 'scheduling_failed';
    if (/(dns|resolve)/.test(text)) return 'dns_resolution';
    if (/(image|pull)/.test(text)) return 'image_pull_error';
    if (/(config|secret|missing)/.test(text)) return 'config_missing';
    if (/(volume|mount|pvc)/.test(text)) return 'volume_mount_failed';
    if (/(observability|blindspot|gray|transient|compound|distractor)/.test(text)) return 'observability_signal';
    return text || 'service_unavailable';
}

function faultLabSkillWorkflowBody(stages = ['detect', 'diagnose', 'heal', 'verify']) {
    const s = selectedFaultScenario() || {};
    const base = faultLabBaseBody();
    const namespace = base.namespace || s.inject?.namespace || 'default';
    const rawFaultType = s.fault_type || s.expected?.fault_type || '';
    const targetService = s.target_service || s.inject?.target_service || '';
    const message = [
        `通算故障实验 ${s.id || ''}`,
        faultTypeLabel(rawFaultType),
        s.description || '',
        targetService ? `target=${targetService}` : '',
    ].filter(Boolean).join(' | ');
    return {
        domain: 'k8s',
        stages,
        dry_run: true,
        allow_legacy_fallback: true,
        payload: {
            source: 'faultlab',
            domain: 'k8s',
            cluster_id: base.cluster_id || 'default',
            namespace,
            scenario_id: s.id || '',
            scenario_name: s.name || s.id || '',
            raw_fault_type: rawFaultType,
            fault_type: normalizeK8sSkillFaultType(rawFaultType),
            category: s.category || '',
            severity: 'warning',
            target_service: targetService,
            service: targetService,
            deployment: targetService,
            root_cause_component: targetService || s.id || '',
            message,
            description: s.description || '',
            expected: s.expected || {},
            expected_symptoms: s.expected?.root_cause_contains || [],
            validation: s.validation || {},
            inject_method: s.inject?.method || 'kubectl',
        },
    };
}

async function runFaultLabSkillWorkflow(stages = ['detect', 'diagnose', 'heal', 'verify'], stage = 'healing') {
    const s = selectedFaultScenario();
    if (!s) return null;
    const out = document.getElementById('faultlab-output');
    const body = faultLabSkillWorkflowBody(stages);
    const payload = body.payload || {};
    const query = `[通算故障实验][${payload.fault_type || s.id}] ${payload.message || s.description || s.name || s.id}; namespace=${payload.namespace || 'default'}; deployment=${payload.deployment || ''}`;
    const result = await runAgentSkillDiagnosis(query, payload.namespace || 'default', 'faultlab-output', { skillWorkflow: body });
    renderAgentSkillDiagnosisResult('faultlab-skill-result', result, stage === 'analysis' ? '通算 Agent+Skill 诊断' : '通算 Agent+Skill 闭环建议');
    return result;
}

function renderFaultLabSkillResult(res = {}, options = {}) {
    const panel = document.getElementById('faultlab-skill-result');
    const out = document.getElementById('faultlab-output');
    if (!panel) return;
    const diagnosis = res.diagnosis || {};
    const capability = res.capability || {};
    const plan = res.execution_plan || {};
    const runtime = res.skill_runtime || {};
    const migration = res.migration || {};
    const checks = res.verification_plan?.checks || [];
    const suggestions = plan.suggestions || [];
    const commands = plan.commands || [];
    const stageLabel = options.stage === 'analysis' ? '通算 Skill 诊断' : '通算 Skill 自愈闭环';
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-info">${escapeHtml(stageLabel)}</span>
                <strong>K8s Skill 调用结果</strong>
                <span class="text-muted">置信度=${escapeHtml(formatConfidence(diagnosis.confidence))}</span>
            </div>
            <span class="text-muted">${escapeHtml(skillMigrationModeLabel(migration.mode || 'skill_primary'))}</span>
        </div>
        <div class="skill-status-strip llm-skill-status">
            <div><span>主 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
            <div><span>匹配 Skill</span><strong>${escapeHtml(runtime.matched_count ?? '-')}</strong></div>
            <div><span>自愈门禁</span><strong>${capability.heal_ready ? '通过' : '受限'}</strong></div>
            <div><span>动作 / 验证</span><strong>${escapeHtml((plan.action_count ?? ((plan.command_count || 0) + (plan.operation_count || 0)) ?? commands.length ?? 0) + ' / ' + checks.length)}</strong></div>
        </div>
        <div class="skill-result-grid">
            ${renderSkillDiagnosisCard(diagnosis)}
            ${renderSkillCapabilityCard(capability)}
            ${renderSkillExecutionCard(plan, suggestions, commands)}
            ${renderSkillVerificationCard(res.verification_plan || {}, checks)}
        </div>
        ${renderAlertSkillExecutionProcess(res, ['detect', 'diagnose', 'heal', 'verify'], { open: true, showMissing: true })}
        ${renderSkillMigrationNotice(migration)}
        <div class="fault-closure-actions">
            <button class="btn btn-sm" onclick="switchView('skillops')">查看 Skill 调用记录</button>
        </div>
        ${renderSkillTrace(res.trace || {}, runtime)}
    `;
    if (out) {
        out.textContent = JSON.stringify({
            status: res.status,
            domain: res.domain,
            dry_run: res.dry_run,
            diagnosis,
            capability,
            execution_plan: plan,
            verification_plan: res.verification_plan,
            migration,
            dashboard_bridge: res.dashboard_bridge,
        }, null, 2);
    }
}

async function injectFaultScenario() {
    const s = selectedFaultScenario();
    if (!s) return;
    const dryRun = document.getElementById('faultlab-dryrun')?.checked;
    const backgroundLoad = document.getElementById('faultlab-load')?.checked;
    const out = document.getElementById('faultlab-output');
    const body = { ...faultLabBaseBody(), dry_run: dryRun, background_load: backgroundLoad };
    if (!faultLabEnsureConfirmed('执行通算故障注入', s, body)) return;
    out.textContent = '执行中...';
    try {
        const res = await api(`/api/faultlab/inject/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
        out.textContent = llmFaultFormatError(err);
    }
}

async function cleanupFaultScenario() {
    const s = selectedFaultScenario();
    if (!s) return;
    const dryRun = document.getElementById('faultlab-dryrun')?.checked;
    const out = document.getElementById('faultlab-output');
    const body = { ...faultLabBaseBody(), dry_run: dryRun };
    if (!faultLabEnsureConfirmed('执行通算故障清理', s, body)) return;
    out.textContent = '清理中...';
    try {
        const res = await api(`/api/faultlab/cleanup/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
        out.textContent = llmFaultFormatError(err);
    }
}

async function openPrecisionFaultLab() {
    const panel = document.getElementById('faultlab-precision-panel');
    const out = document.getElementById('precisionfaultlab-output');
    if (!panel) return;
    panel.style.display = '';
    panel.innerHTML = '<div class="text-muted">正在加载 AgenticSRE 细粒度场景...</div>';
    try {
        const data = await api('/api/faultlab/precision-scenarios');
        const scenarios = Array.isArray(data?.scenarios) ? data.scenarios : [];
        const methodCounts = data?.method_counts || {};
        window._precisionFaultScenarios = scenarios;
        window._precisionFaultRecoverySources = window._precisionFaultRecoverySources || {};
        scenarios.forEach(item => {
            if (!window._precisionFaultRecoverySources[item.id] && item?.latest_recovery_source?.fault_run_id) {
                window._precisionFaultRecoverySources[item.id] = item.latest_recovery_source;
            }
        });
        panel.innerHTML = `
            <div class="fault-result-head">
                <div class="fault-result-title">
                    <span class="badge badge-success">受控执行</span>
                    <strong>${escapeHtml(data?.family || 'AgenticSRE 细粒度故障注入')}</strong>
                </div>
                <span class="text-muted">${escapeHtml(`${scenarios.length} 场景 / ${Object.keys(methodCounts).length} 机制 / ${data?.adapter_ready_count || 0} 适配器就绪`)}</span>
            </div>
            <div class="fault-result-section">
                <p>所有场景都由服务端固定适配器执行，不接受命令、脚本或临时改写目标。真实实验按“基线 → 注入 → 效果验证 → Undo → 恢复验证 → 残留检查”闭环执行，未获得故障效果证据不会判定通过。</p>
                <div class="fault-result-code">${escapeHtml(Object.entries(methodCounts).map(([name, count]) => `${name}: ${count}`).join('\n'))}</div>
            </div>
            <div id="faultlab-precision-detail" class="fault-result-section" style="display:none"></div>
            <div class="faultlab-grid">
                ${scenarios.map(item => `
                    <div class="fault-card">
                        <div class="fault-card-title">
                            ${escapeHtml(item.name || item.id)}
                            <span class="badge ${item?.adapter_preflight?.ready ? 'badge-success' : 'badge-warning'}">${item?.adapter_preflight?.ready ? '适配器已实现' : '适配器阻断'}</span>
                        </div>
                        <div class="text-muted">${escapeHtml(item.fault_type || '')} · ${escapeHtml(item.precision?.method || '')}</div>
                        <p>${escapeHtml(item.description || '')}</p>
                        <div class="fault-action-buttons">
                            <button class="btn btn-sm" onclick="planPrecisionFaultScenario('${escapeHtml(item.id)}')">合同</button>
                            <button class="btn btn-sm" onclick="preflightPrecisionFaultScenario('${escapeHtml(item.id)}')">环境预检</button>
                            <button class="btn btn-sm btn-primary" onclick="runPrecisionFaultScenario('${escapeHtml(item.id)}', true)">预演</button>
                            <button class="btn btn-sm btn-danger" onclick="runPrecisionFaultScenario('${escapeHtml(item.id)}', false)">真实闭环</button>
                            <button class="btn btn-sm" onclick="healPrecisionFaultScenario('${escapeHtml(item.id)}')">自愈校验</button>
                            <button class="btn btn-sm" onclick="cleanupPrecisionFaultScenario('${escapeHtml(item.id)}', false)">强制清理</button>
                            <button class="btn btn-sm" onclick="verifyPrecisionFaultScenario('${escapeHtml(item.id)}')">残留检查</button>
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
        if (out) out.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
        panel.innerHTML = `<div class="alert alert-danger">${escapeHtml(llmFaultFormatError(err))}</div>`;
    }
}

function openPrecisionFaultLabView() {
    switchFaultLabTab('precision');
}

function precisionFaultLabBaseBody() {
    const confirmed = document.getElementById('precisionfaultlab-confirm')?.checked === true;
    const body = {
        confirm: confirmed,
        experiment_ack: confirmed,
    };
    const clusterId = document.getElementById('precisionfaultlab-cluster')?.value || '';
    if (clusterId) body.cluster_id = clusterId;
    const namespace = document.getElementById('precisionfaultlab-namespace')?.value?.trim();
    if (namespace) body.namespace = namespace;
    return body;
}

function precisionFaultScenarioById(scenarioId) {
    return (window._precisionFaultScenarios || []).find(item => item.id === scenarioId) || { id: scenarioId };
}

function renderPrecisionFaultResult(title, result, status = 'info') {
    const detail = document.getElementById('faultlab-precision-detail');
    const out = document.getElementById('precisionfaultlab-output');
    if (out) out.textContent = JSON.stringify(result, null, 2);
    if (!detail) return;
    const badgeClass = status === 'success' ? 'badge-success' : (status === 'danger' ? 'badge-danger' : 'badge-info');
    const transaction = result?.result?.transaction || result?.transaction || {};
    const healStages = Object.keys(result?.transactional_heal?.run?.stages || {});
    const stages = transaction?.stages || healStages;
    const evidence = result?.result?.evidence || result?.evidence || {};
    detail.style.display = '';
    detail.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title"><span class="badge ${badgeClass}">${escapeHtml(result?.status || status)}</span><strong>${escapeHtml(title)}</strong></div>
            <span class="text-muted">${escapeHtml(result?.fault_run_id || '')}</span>
        </div>
        ${stages.length ? `<div class="precision-stage-flow">${stages.map((stage, index) => `<span><b>${index + 1}</b>${escapeHtml(stage)}</span>`).join('')}</div>` : ''}
        ${Object.keys(evidence).length ? `<div class="fault-result-section"><h4>验证证据</h4><ul>${Object.entries(evidence).map(([key, value]) => `<li><strong>${escapeHtml(key)}</strong>: ${escapeHtml(value)}</li>`).join('')}</ul></div>` : ''}
        <div class="fault-result-code">${escapeHtml(JSON.stringify(result, null, 2))}</div>
    `;
}

async function planPrecisionFaultScenario(scenarioId) {
    const out = document.getElementById('precisionfaultlab-output');
    const base = precisionFaultLabBaseBody();
    if (out) out.textContent = '正在生成细粒度受控合同...';
    try {
        const result = await api(`/api/faultlab/precision/plan/${encodeURIComponent(scenarioId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                dry_run: true,
                cluster_id: base.cluster_id || '',
                namespace: base.namespace || 'agenticsre-faultlab',
            }),
            throwOnError: true,
        });
        renderPrecisionFaultResult('受控合同', result, result?.guarded_plan?.real_execution_ready ? 'success' : 'info');
        showToast('细粒度受控合同已生成', 'success');
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function preflightPrecisionFaultScenario(scenarioId) {
    const base = precisionFaultLabBaseBody();
    const scenario = precisionFaultScenarioById(scenarioId);
    renderPrecisionFaultResult(`${scenario.name || scenarioId} · 环境预检`, { status: 'running' }, 'info');
    try {
        const result = await api(`/api/faultlab/precision/preflight/${encodeURIComponent(scenarioId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(base), throwOnError: true,
        });
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 环境预检`, result, result?.preflight?.ready ? 'success' : 'danger');
    } catch (err) {
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 环境预检失败`, { status: 'failed', error: llmFaultFormatError(err) }, 'danger');
    }
}

async function runPrecisionFaultScenario(scenarioId, dryRun = true) {
    const scenario = precisionFaultScenarioById(scenarioId);
    const base = precisionFaultLabBaseBody();
    const body = { ...base, dry_run: dryRun };
    if (!faultLabEnsureConfirmed('执行细粒度真实故障闭环', scenario, body)) return;
    renderPrecisionFaultResult(`${scenario.name || scenarioId} · ${dryRun ? '预演' : '真实闭环'}`, { status: 'running' }, 'info');
    try {
        const result = await api(`/api/faultlab/precision/experiment/${encodeURIComponent(scenarioId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body), throwOnError: true,
        });
        if (!dryRun && result?.status === 'succeeded' && result?.fault_run_id) {
            window._precisionFaultRecoverySources = window._precisionFaultRecoverySources || {};
            window._precisionFaultRecoverySources[scenarioId] = {
                fault_run_id: result.fault_run_id,
                cluster_id: base.cluster_id || '',
                namespace: base.namespace || 'agenticsre-faultlab',
            };
        }
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · ${dryRun ? '预演' : '真实闭环'}`, result, result?.status === 'succeeded' || result?.status === 'dry_run' ? 'success' : 'danger');
        showToast(dryRun ? '细粒度实验预演完成' : '细粒度真实闭环完成', 'success');
    } catch (err) {
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 执行失败`, { status: 'failed', error: llmFaultFormatError(err) }, 'danger');
    }
}

async function healPrecisionFaultScenario(scenarioId) {
    const scenario = precisionFaultScenarioById(scenarioId);
    const source = window._precisionFaultRecoverySources?.[scenarioId] || scenario?.latest_recovery_source;
    if (!source?.fault_run_id) {
        renderPrecisionFaultResult(
            `${scenario.name || scenarioId} · 自愈校验`,
            { status: 'blocked', error: '请先完成一次成功的细粒度真实闭环，自愈校验只复用服务端持久化的原交易 Undo。' },
            'danger',
        );
        return;
    }
    renderPrecisionFaultResult(`${scenario.name || scenarioId} · 自愈校验`, { status: 'running' }, 'info');
    try {
        const result = await api(`/api/faultlab/precision/heal/${encodeURIComponent(scenarioId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                dry_run: false,
                fault_run_id: source.fault_run_id,
                cluster_id: source.cluster_id || '',
                namespace: source.namespace || 'agenticsre-faultlab',
            }),
            throwOnError: true,
        });
        renderPrecisionFaultResult(
            `${scenario.name || scenarioId} · 自愈校验`,
            result,
            result?.status === 'already_recovered' && result?.recovery?.passed ? 'success' : 'danger',
        );
        if (result?.recovery?.passed) showToast('原交易 Undo 复用与恢复校验通过', 'success');
    } catch (err) {
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 自愈校验失败`, { status: 'failed', error: llmFaultFormatError(err) }, 'danger');
    }
}

async function cleanupPrecisionFaultScenario(scenarioId, dryRun = false) {
    const scenario = precisionFaultScenarioById(scenarioId);
    const base = precisionFaultLabBaseBody();
    const body = { ...base, dry_run: dryRun };
    if (!faultLabEnsureConfirmed('清理细粒度故障残留', scenario, body)) return;
    try {
        const result = await api(`/api/faultlab/precision/cleanup/${encodeURIComponent(scenarioId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body), throwOnError: true,
        });
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 清理`, result, result?.status === 'executed' || result?.status === 'dry_run' ? 'success' : 'danger');
    } catch (err) {
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 清理失败`, { status: 'failed', error: llmFaultFormatError(err) }, 'danger');
    }
}

async function verifyPrecisionFaultScenario(scenarioId) {
    const scenario = precisionFaultScenarioById(scenarioId);
    const base = precisionFaultLabBaseBody();
    try {
        const result = await api(`/api/faultlab/precision/verify/${encodeURIComponent(scenarioId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(base), throwOnError: true,
        });
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 残留检查`, result, result?.verification?.passed ? 'success' : 'danger');
    } catch (err) {
        renderPrecisionFaultResult(`${scenario.name || scenarioId} · 残留检查失败`, { status: 'failed', error: llmFaultFormatError(err) }, 'danger');
    }
}

function addK8sFaultExperimentStep() {
    const s = selectedFaultScenario();
    if (!s) return;
    _k8sFaultExperimentSteps.push({
        scenario_id: s.id,
        name: s.name || s.id,
        fault_type: s.fault_type || '',
        category: s.category || '',
        action: 'inject',
        hold_s: 30,
        wait_after_s: parseInt(document.getElementById('k8s-fault-exp-wait')?.value || '5', 10) || 0,
    });
    renderK8sFaultExperimentSteps();
}

function scenarioByFaultKeywords(scenarios, keywords = []) {
    const terms = keywords.map(x => String(x).toLowerCase());
    return scenarios.find(s => {
        const text = [s.id, s.name, s.fault_type, s.category, s.description, s.target_service].join(' ').toLowerCase();
        return terms.some(term => text.includes(term));
    }) || null;
}

function buildK8sFaultExperimentStep(scenario, options = {}) {
    return {
        scenario_id: scenario.id,
        name: scenario.name || scenario.id,
        fault_type: scenario.fault_type || '',
        category: scenario.category || '',
        action: options.action || 'experiment',
        hold_s: options.hold_s ?? 45,
        wait_after_s: options.wait_after_s ?? (parseInt(document.getElementById('k8s-fault-exp-wait')?.value || '5', 10) || 0),
    };
}

function applyK8sFaultExperimentPreset(kind = 'availability') {
    const recipes = {
        availability: [
            [['service_unavailable', 'scale_down', 'svc-scale'], { hold_s: 45 }],
            [['crashloop', 'crash_loop'], { hold_s: 40 }],
        ],
        resource: [
            [['cpu_pressure', 'cpu-stress', 'cpu'], { hold_s: 60 }],
            [['memory_pressure', 'mem-pressure', 'oom'], { hold_s: 45 }],
        ],
        network: [
            [['network_partition', 'network-delay', 'dependency_timeout'], { hold_s: 45 }],
            [['service_unavailable', 'scale_down'], { hold_s: 40 }],
        ],
        mixed: [
            [['service_unavailable', 'scale_down'], { hold_s: 35 }],
            [['cpu_pressure', 'cpu'], { hold_s: 45 }],
            [['crashloop', 'crash_loop'], { hold_s: 35 }],
            [['network_partition', 'dependency_timeout'], { hold_s: 40 }],
        ],
    };
    const selected = [];
    (recipes[kind] || recipes.availability).forEach(([keywords, options]) => {
        const scenario = scenarioByFaultKeywords(_faultScenarios, keywords);
        if (scenario && !selected.some(item => item.scenario_id === scenario.id)) {
            selected.push(buildK8sFaultExperimentStep(scenario, options));
        }
    });
    if (!selected.length) {
        showToast('没有找到匹配的通算模板场景，请先刷新场景。', 'warning');
        return;
    }
    _k8sFaultExperimentSteps = selected;
    if (!document.getElementById('k8s-fault-exp-name')?.value) {
        setVal('k8s-fault-exp-name', `k8s-${kind}-${new Date().toISOString().slice(11, 16).replace(':', '')}`);
    }
    renderK8sFaultExperimentSteps();
}

function clearK8sFaultExperimentSteps() {
    _k8sFaultExperimentSteps = [];
    renderK8sFaultExperimentSteps();
}

function removeK8sFaultExperimentStep(index) {
    _k8sFaultExperimentSteps.splice(index, 1);
    renderK8sFaultExperimentSteps();
}

function updateK8sFaultExperimentStep(index, field, value) {
    if (!_k8sFaultExperimentSteps[index]) return;
    if (field === 'hold_s' || field === 'wait_after_s') {
        _k8sFaultExperimentSteps[index][field] = Math.max(0, parseInt(value || '0', 10) || 0);
    } else {
        _k8sFaultExperimentSteps[index][field] = value;
    }
}

function renderK8sFaultExperimentSteps() {
    const el = document.getElementById('k8s-fault-exp-steps');
    if (!el) return;
    renderK8sFaultExperimentSummary();
    if (!_k8sFaultExperimentSteps.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px">尚未添加连续实验步骤。可选择模板，或在单次实验/依赖图中选中目标后添加当前场景。</p>';
        return;
    }
    el.innerHTML = _k8sFaultExperimentSteps.map((step, i) => `
        <div class="fault-exp-step">
            <div class="fault-exp-step-main">
                <span class="badge badge-info">#${i + 1}</span>
                <strong>${escapeHtml(step.scenario_id)}</strong>
                <span class="text-muted">${escapeHtml(step.name || '')}</span>
                <span class="badge badge-warning">${escapeHtml(step.fault_type || '')}</span>
                <span class="badge badge-gray">${escapeHtml(step.category || 'k8s')}</span>
            </div>
            <div class="fault-exp-step-flow"><span>baseline</span><i></i><span>inject</span><i></i><span>${step.hold_s || 0}s</span><i></i><span>cleanup</span><i></i><span>verify</span></div>
            <select class="select-sm" onchange="updateK8sFaultExperimentStep(${i}, 'action', this.value)">
                <option value="experiment" ${step.action === 'experiment' ? 'selected' : ''}>完整实验</option>
                <option value="inject" ${step.action === 'inject' ? 'selected' : ''}>只注入</option>
                <option value="cleanup" ${step.action === 'cleanup' ? 'selected' : ''}>只清理</option>
            </select>
            <input class="input-sm" type="number" min="0" value="${step.hold_s}" title="hold seconds"
                onchange="updateK8sFaultExperimentStep(${i}, 'hold_s', this.value)">
            <input class="input-sm" type="number" min="0" value="${step.wait_after_s}" title="wait after seconds"
                onchange="updateK8sFaultExperimentStep(${i}, 'wait_after_s', this.value)">
            <button class="btn btn-sm btn-danger" onclick="removeK8sFaultExperimentStep(${i})">移除</button>
        </div>
    `).join('');
}

function renderK8sFaultExperimentSummary() {
    const el = document.getElementById('k8s-fault-exp-summary');
    if (!el) return;
    const dryRun = document.getElementById('faultlab-dryrun')?.checked !== false;
    const totalHold = _k8sFaultExperimentSteps.reduce((sum, step) => sum + (Number(step.hold_s) || 0), 0);
    const totalWait = _k8sFaultExperimentSteps.reduce((sum, step) => sum + (Number(step.wait_after_s) || 0), 0);
    const types = [...new Set(_k8sFaultExperimentSteps.map(s => s.fault_type).filter(Boolean))];
    el.innerHTML = `
        <div><span>步骤</span><strong>${_k8sFaultExperimentSteps.length}</strong></div>
        <div><span>预计观察</span><strong>${totalHold + totalWait}s</strong></div>
        <div><span>故障类型</span><strong>${escapeHtml(types.slice(0, 3).join(' / ') || '-')}</strong></div>
        <div><span>执行模式</span><strong>${dryRun ? 'Dry-run 计划' : '真实执行需确认'}</strong></div>
    `;
}

function k8sFaultExperimentRequestBody() {
    return {
        ...faultLabBaseBody(),
        benchmark_type: 'k8s',
        dry_run: document.getElementById('faultlab-dryrun')?.checked !== false,
        background_load: document.getElementById('faultlab-load')?.checked !== false,
        name: document.getElementById('k8s-fault-exp-name')?.value || '',
        business_system: document.getElementById('k8s-fault-exp-system')?.value || 'social-network',
        total_duration_s: parseInt(document.getElementById('k8s-fault-exp-total')?.value || '0', 10) || 0,
        default_wait_after_s: parseInt(document.getElementById('k8s-fault-exp-wait')?.value || '5', 10) || 0,
        steps: _k8sFaultExperimentSteps.map(step => ({
            scenario_id: step.scenario_id,
            action: step.action,
            hold_s: step.hold_s,
            wait_after_s: step.wait_after_s,
        })),
    };
}

async function startK8sFaultExperiment() {
    const out = document.getElementById('k8s-fault-exp-output');
    if (!_k8sFaultExperimentSteps.length) {
        if (out) out.textContent = '请先添加至少一个通算实验步骤。';
        return;
    }
    const body = k8sFaultExperimentRequestBody();
    if (!faultLabEnsureConfirmed('启动通算连续故障实验', { name: `${_k8sFaultExperimentSteps.length} 个步骤` }, body)) return;
    if (out) out.textContent = '通算连续实验启动中...';
    try {
        const res = await api('/api/fault-experiments/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        if (out) out.textContent = JSON.stringify(res, null, 2);
        await loadK8sFaultExperiments();
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function loadK8sFaultExperiments() {
    const data = await api('/api/fault-experiments?limit=20');
    const runs = (data?.experiments || []).filter(run => run.spec?.benchmark_type === 'k8s').slice(0, 10);
    renderFaultExperimentRuns('k8s-fault-exp-runs', 'k8s-fault-exp-output', runs);
}

// ─────────────────────────────────────────
// 智算故障实验
// ─────────────────────────────────────────

let _llmFaultScenarios = [];
let _llmFaultTool = {};
let _aishperfStatus = {};
let _aishperfScenarios = [];
let _aishperfDomainCounts = {};
let _selectedAISHPerfCaseId = '';
let _lastAISHPerfDetail = null;
let _lastAISHPerfDryRun = null;
let _lastAISHPerfSkillWorkflow = null;
let _faultExperimentSteps = [];
let _lastLLMFaultResult = null;
let _lastLLMHostObservation = null;
let _lastLLMFaultDiagnosis = null;
let _lastLLMFaultHeal = null;
let _lastLLMFaultHealPayload = null;
let _lastLLMFaultEvaluation = null;
let _lastLLMFaultPreflight = null;
let _lastLLMFaultSkillWorkflow = null;
let _activeLLMFaultRuns = [];
let _lastAIDashboardData = null;
let _lastLLMFaultTopologyData = null;
let _llmFaultTopologyRequestSeq = 0;
let _aiDashboardTab = 'topology';
let _aiMetricTab = 'llm';
let _aiResourceTab = 'nodes';
let _aiResourcePage = { pods: 1 };
let _llmFaultLabTab = 'single';
let _llmTopologyFaultTargets = {};
let _selectedLLMTopologyFaultTargetKey = '';
const AI_DASHBOARD_CHARTS = {
    running: { group: 'llm', canvas: 'chart-ai-running', unit: '' },
    waiting: { group: 'llm', canvas: 'chart-ai-waiting', unit: '' },
    kv_cache: { group: 'llm', canvas: 'chart-ai-kv-cache', unit: '%' },
    ttft_p95: { group: 'llm', canvas: 'chart-ai-ttft', unit: 's' },
    e2e_latency_p95: { group: 'llm', canvas: 'chart-ai-e2e', unit: 's' },
    node_cpu_usage: { group: 'node', canvas: 'chart-ai-node-cpu', unit: '%' },
    node_memory_usage: { group: 'node', canvas: 'chart-ai-node-memory', unit: '%' },
    node_rootfs_usage: { group: 'node', canvas: 'chart-ai-node-rootfs', unit: '%' },
    node_disk_read: { group: 'node', canvas: 'chart-ai-node-disk-read', unit: ' MB/s' },
    node_disk_write: { group: 'node', canvas: 'chart-ai-node-disk-write', unit: ' MB/s' },
    node_network_receive: { group: 'node', canvas: 'chart-ai-node-net-rx', unit: ' MB/s' },
    node_network_transmit: { group: 'node', canvas: 'chart-ai-node-net-tx', unit: ' MB/s' },
    gpu_util: { group: 'gpu', canvas: 'chart-ai-gpu-util', unit: '%' },
    gpu_memory_used: { group: 'gpu', canvas: 'chart-ai-gpu-mem', unit: '%' },
    gpu_temp: { group: 'gpu', canvas: 'chart-ai-gpu-temp', unit: '℃' },
    gpu_util_per_device: { group: 'gpu_device', canvas: 'chart-ai-gpu-device-util', unit: '%' },
    gpu_memory_used_per_device: { group: 'gpu_device', canvas: 'chart-ai-gpu-device-mem', unit: '%' },
    gpu_temp_per_device: { group: 'gpu_device', canvas: 'chart-ai-gpu-device-temp', unit: '℃' },
};

async function loadLLMFaultLab() {
    await loadFaultTargets();
    await ensureLLMFaultScenariosLoaded({ force: true });
    renderLLMFaultSummary();
    renderLLMFaultEnvironment();
    onLLMFaultModeChange();

    const sel = document.getElementById('llmfaultlab-scenario');
    if (!sel) return;
    sel.innerHTML = _llmFaultScenarios.length
        ? _llmFaultScenarios.map(s => `<option value="${s.id}">${s.id} — ${escapeHtml(llmFaultTypeLabel(s.fault_type) || s.name || '')}</option>`).join('')
        : '<option value="">未发现 vLLM 故障场景</option>';
    sel.onchange = renderLLMFaultScenarioDetail;
    renderLLMFaultScenarioDetail();
    renderLLMFaultScenarioGrid();
    renderFaultExperimentSteps();
    loadFaultExperiments();
    loadActiveLLMFaultRuns();
    loadLLMFaultTopology();
}

function llmFaultRunDisplayLabel(run = {}) {
    const scenario = _llmFaultScenarios.find(item => item.id === run.scenario_id) || {};
    const faultLabel = llmFaultTypeLabel(run.fault_type) || scenario.name || run.scenario_id || '未知故障';
    const target = [run.cluster_id, run.namespace, run.deployment].filter(Boolean).join(' / ') || run.host_id || '未知目标';
    const age = Number(run.age_seconds || 0);
    const ageLabel = age >= 60 ? `${Math.floor(age / 60)}分${Math.round(age % 60)}秒` : `${Math.round(age)}秒`;
    return `${faultLabel} · ${target} · 已注入 ${ageLabel} · ${run.id || '-'}`;
}

function renderActiveLLMFaultRuns() {
    const select = document.getElementById('llmfaultlab-active-run');
    const button = document.getElementById('llmfaultlab-recover-selected');
    const hint = document.getElementById('llmfaultlab-active-run-hint');
    if (!select) return;
    const previous = select.value;
    select.innerHTML = _activeLLMFaultRuns.length
        ? _activeLLMFaultRuns.map(run => `<option value="${escapeHtmlAttr(run.id || '')}">${escapeHtml(llmFaultRunDisplayLabel(run))}</option>`).join('')
        : '<option value="">当前没有待恢复的故障</option>';
    if (previous && _activeLLMFaultRuns.some(run => run.id === previous)) select.value = previous;
    if (button) button.disabled = !_activeLLMFaultRuns.length;
    if (hint) hint.textContent = _activeLLMFaultRuns.length
        ? `已发现 ${_activeLLMFaultRuns.length} 个活跃故障；恢复时将使用该次注入保存的原始恢复契约。`
        : '当前没有已记录且尚未恢复的真实故障。';
}

async function loadActiveLLMFaultRuns() {
    const data = await api('/api/fault-runs?active_only=true&limit=100', { cache: 'no-store' });
    _activeLLMFaultRuns = data?.runs || [];
    renderActiveLLMFaultRuns();
    if (_activeLLMFaultRuns.length) await applySelectedLLMFaultRun();
}

async function applySelectedLLMFaultRun() {
    const runId = document.getElementById('llmfaultlab-active-run')?.value || '';
    const run = _activeLLMFaultRuns.find(item => item.id === runId);
    if (!run) return null;
    const modeInput = document.querySelector(`input[name="llmfaultlab-mode"][value="${run.target_mode || 'k8s'}"]`);
    if (modeInput) modeInput.checked = true;
    onLLMFaultModeChange();
    if ((run.target_mode || 'k8s') === 'k8s') {
        if (run.cluster_id) ensureSelectOption('llmfaultlab-cluster', run.cluster_id);
        populateLLMFaultTargetSelectors(null, _lastLLMFaultTopologyData, {
            namespace: run.namespace || '',
            deployment: run.deployment || '',
            service: run.service || run.deployment || '',
        });
        if (run.namespace) ensureSelectOption('llmfaultlab-namespace', run.namespace);
        if (run.deployment) ensureSelectOption('llmfaultlab-deployment', run.deployment);
        if (run.service || run.deployment) ensureSelectOption('llmfaultlab-service', run.service || run.deployment);
    }
    if (run.scenario_id) setVal('llmfaultlab-scenario', run.scenario_id);
    renderLLMFaultScenarioDetail();
    const hint = document.getElementById('llmfaultlab-active-run-hint');
    if (hint) hint.textContent = `已选择 ${llmFaultRunDisplayLabel(run)}。恢复请求将携带 fault_run_id=${run.id}。`;
    return run;
}

async function ensureLLMFaultScenariosLoaded(options = {}) {
    if (!options.force && _llmFaultScenarios.length) return;
    const data = await api('/api/llm-faultlab/scenarios');
    _llmFaultScenarios = data?.scenarios || [];
    _llmFaultTool = data?.tool || {};
    const aishperf = data?.external_sources?.aishperf || {};
    _aishperfStatus = aishperf.status || {};
    _aishperfDomainCounts = aishperf.domain_counts || {};
    if (Array.isArray(aishperf.sample_gpu_cases) && !_aishperfScenarios.length) {
        _aishperfScenarios = aishperf.sample_gpu_cases;
    }
}

function switchLLMFaultLabTab(tab) {
    _llmFaultLabTab = tab || 'single';
    document.querySelectorAll('[data-llmfaultlab-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.llmfaultlabTab === _llmFaultLabTab);
    });
    document.querySelectorAll('.llmfaultlab-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `llmfaultlab-tab-${_llmFaultLabTab}`);
    });
    if (_llmFaultLabTab === 'sequence') {
        renderFaultExperimentSteps();
        loadFaultExperiments();
    } else if (_llmFaultLabTab === 'catalog') {
        renderLLMFaultScenarioGrid();
    } else if (_llmFaultLabTab === 'aishperf') {
        loadAISHPerfScenarios();
    }
}

function getLLMFaultMode() {
    const r = document.querySelector('input[name="llmfaultlab-mode"]:checked');
    return r ? r.value : 'k8s';
}

function getLLMFaultEngine() {
    return document.getElementById('llmfaultlab-engine')?.value || 'agent';
}

function onLLMFaultModeChange() {
    const mode = getLLMFaultMode();
    const k8sRow = document.getElementById('llmfaultlab-k8s-row');
    const hostRow = document.getElementById('llmfaultlab-host-row');
    if (k8sRow) k8sRow.style.display = mode === 'k8s' ? '' : 'none';
    if (hostRow) hostRow.style.display = mode === 'host' ? '' : 'none';
    const cluster = selectedLLMFaultCluster();
    const host = selectedLLMFaultHost();
    populateLLMFaultModelOptions(
        null,
        mode === 'host'
            ? (host?.model || host?.served_model || host?.served_model_name || '')
            : (cluster.model || cluster.served_model || cluster.served_model_name || '')
    );
    renderLLMFaultParamHelp();
}

async function probeLLMHost() {
    const hostId = document.getElementById('llmfaultlab-host')?.value;
    const hint = document.getElementById('llmfaultlab-host-hint');
    if (!hostId) { if (hint) hint.textContent = '请先选择主机'; return; }
    if (hint) hint.textContent = '探测中...';
    const res = await api(`/api/fault-targets/llm-host/${hostId}/probe`, { method: 'POST' });
    if (!hint) return;
    if (res?.ok) {
        const lines = (res.stdout || '').split('\n').filter(Boolean);
        const summary = lines[0] ? lines[0].substring(0, 80) : '已连接';
        hint.textContent = `✓ ${summary}${lines.length > 1 ? ` (+${lines.length - 1})` : ''}`;
        hint.style.color = '#16a34a';
        renderLLMHostProbeResult(res);
    } else {
        hint.textContent = `✗ ${(res?.stderr || res?.error || '不可达').substring(0, 100)}`;
        hint.style.color = '#dc2626';
        renderLLMHostProbeResult(res);
    }
}

async function observeLLMHost() {
    const hostId = document.getElementById('llmfaultlab-host')?.value;
    const hint = document.getElementById('llmfaultlab-host-hint');
    const out = document.getElementById('llmfaultlab-output');
    if (!hostId) { if (hint) hint.textContent = '请先选择主机'; return; }
    if (hint) hint.textContent = '观测中...';
    if (out) out.textContent = '正在通过 SSH 直连采样 GPU、vLLM 进程和 endpoint 状态...';
    try {
        const res = await api(`/api/fault-targets/llm-host/${hostId}/observe`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
            throwOnError: true,
        });
        if (hint) {
            hint.textContent = res?.ok ? '✓ 已完成直连观测' : '✗ 观测失败';
            hint.style.color = res?.ok ? '#16a34a' : '#dc2626';
        }
        renderLLMHostObservation(res);
    } catch (err) {
        if (hint) {
            hint.textContent = `✗ ${llmFaultFormatError(err).substring(0, 100)}`;
            hint.style.color = '#dc2626';
        }
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

function emptyLLMMetricCharts(message = '暂无 Prometheus 时序数据', chartMap = AI_DASHBOARD_CHARTS) {
    Object.values(chartMap).forEach(spec => {
        const canvas = document.getElementById(spec.canvas);
        if (!canvas) return;
        if (state.charts[spec.canvas]) {
            state.charts[spec.canvas].destroy();
            delete state.charts[spec.canvas];
        }
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#94a3b8';
        ctx.font = '13px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(message, canvas.width / 2, canvas.height / 2);
    });
}

function llmMetricStats(chartData) {
    const series = chartData?.series || [];
    const values = series.flatMap(s => (s.values || []).map(v => Number(v)).filter(Number.isFinite));
    if (!values.length) return { latest: null, max: null, allZero: false };
    const latestSeries = series[0]?.values || [];
    const latest = Number(latestSeries[latestSeries.length - 1]);
    const max = Math.max(...values);
    return {
        latest: Number.isFinite(latest) ? latest : null,
        max,
        allZero: values.every(v => Math.abs(v) < 1e-9),
    };
}

function llmMetricFormatNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
    const n = Number(value);
    return Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(4).replace(/\.?0+$/, '') || '0';
}

function renderLLMChartBadge(canvas, chartData, unit = '') {
    const header = canvas.closest('.card')?.querySelector('.chart-header');
    if (!header) return;
    let badge = header.querySelector('.llm-chart-value');
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'badge badge-info llm-chart-value';
        header.appendChild(badge);
    }
    const stats = llmMetricStats(chartData);
    const latest = llmMetricFormatNumber(stats.latest);
    const max = llmMetricFormatNumber(stats.max);
    badge.textContent = `当前 ${latest}${unit ? ` ${unit}` : ''} · 峰值 ${max}${unit ? ` ${unit}` : ''}`;
}

function renderLLMMetricChart(canvasId, chartData, unit = '') {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    renderLLMChartBadge(canvas, chartData, unit);
    if (state.charts[canvasId]) {
        state.charts[canvasId].destroy();
        delete state.charts[canvasId];
    }
    const series = chartData?.series || [];
    if (!series.length) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#94a3b8';
        ctx.font = '13px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(chartData?.error ? 'PromQL 无数据' : '暂无数据', canvas.width / 2, canvas.height / 2);
        return;
    }
    const ref = series[0];
    const labels = (ref.timestamps || []).map(t => _fmtTs(t));
    const stats = llmMetricStats(chartData);
    const datasets = series.map((s, i) => ({
        label: normalizeSeriesLabel(s.label),
        data: s.values || [],
        borderColor: CHART_COLORS[i % CHART_COLORS.length],
        backgroundColor: CHART_COLORS[i % CHART_COLORS.length] + '24',
        borderWidth: 3,
        pointRadius: stats.allZero ? 1.5 : 0,
        pointHoverRadius: 5,
        fill: true,
        tension: 0.25,
    }));
    state.charts[canvasId] = new Chart(canvas, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: datasets.length > 1, position: 'bottom' },
                tooltip: {
                    callbacks: {
                        label: ctx => `${ctx.dataset.label}: ${Number(ctx.parsed.y).toFixed(4)}${unit ? ` ${unit}` : ''}`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { color: '#e2e8f0' },
                    ticks: { maxTicksLimit: 8, color: '#64748b' },
                },
                y: {
                    beginAtZero: true,
                    suggestedMax: stats.allZero ? 1 : undefined,
                    grid: { color: '#e2e8f0' },
                    ticks: { color: '#64748b' },
                },
            },
        },
    });
}

async function loadAIDashboard() {
    await loadFaultTargets();
    const select = document.getElementById('ai-dashboard-cluster');
    const status = document.getElementById('ai-dashboard-status');
    const clusterId = select?.value || '';
    const duration = document.getElementById('ai-dashboard-range')?.value || '15m';
    updateAIDashboardTimeInputs(duration);
    if (!clusterId) {
        if (status) status.innerHTML = '<span class="badge badge-warning">未配置</span> 请在“管理目标”中添加 kind=ai 的智算 K8s 集群。';
        renderAIDashboardEmpty('未配置智算集群');
        return;
    }
    const promUrl = document.getElementById('ai-dashboard-prometheus')?.value || '';
    if (status) status.innerHTML = '<span class="badge badge-info">加载中</span> 正在读取智算集群指标...';
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    const tellerTraceDir = cluster.teller_trace_dir || '';
    const params = new URLSearchParams({
        duration: duration === 'custom' ? '15m' : duration,
        prometheus_url: promUrl,
        teller_trace_dir: tellerTraceDir,
    });
    const timeRange = aiDashboardSelectedTimeRange();
    if (duration === 'custom' && timeRange.start && timeRange.end) {
        params.set('start', String(timeRange.start));
        params.set('end', String(timeRange.end));
    }
    const data = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/dashboard?${params.toString()}`, { timeoutMs: 45000 });
    if (!data) {
        if (status) status.innerHTML = '<span class="badge badge-danger">失败</span> 智算 Dashboard 加载失败。';
        renderAIDashboardEmpty('加载失败');
        return;
    }
    renderAIDashboard(data);
}

function openAIDashboardForCluster(clusterId = '', duration = '15m') {
    switchView('aidashboard');
    setTimeout(() => {
        if (clusterId) setVal('ai-dashboard-cluster', clusterId);
        if (duration) setVal('ai-dashboard-range', duration);
        loadAIDashboard();
    }, 0);
}

function updateAIDashboardTimeInputs(duration) {
    const showCustom = duration === 'custom';
    ['ai-dashboard-start', 'ai-dashboard-end'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = showCustom ? '' : 'none';
    });
}

function aiDashboardSelectedTimeRange() {
    const startValue = document.getElementById('ai-dashboard-start')?.value || '';
    const endValue = document.getElementById('ai-dashboard-end')?.value || '';
    const start = startValue ? Math.floor(new Date(startValue).getTime() / 1000) : 0;
    const end = endValue ? Math.floor(new Date(endValue).getTime() / 1000) : 0;
    return { start: Number.isFinite(start) ? start : 0, end: Number.isFinite(end) ? end : 0 };
}

function aiDashboardFormatTime(epochSeconds) {
    if (!epochSeconds) return '-';
    return new Date(Number(epochSeconds) * 1000).toLocaleString('zh-CN', { hour12: false });
}

function renderAIDashboardEmpty(message) {
    const summary = document.getElementById('ai-dashboard-summary');
    const profile = document.getElementById('ai-dashboard-profile');
    const topology = document.getElementById('ai-dashboard-topology');
    const resources = document.getElementById('ai-dashboard-resources');
    const targets = document.getElementById('ai-dashboard-targets');
    if (summary) summary.innerHTML = '';
    if (profile) profile.innerHTML = `<p class="text-muted">${escapeHtml(message)}</p>`;
    if (topology) topology.innerHTML = `<p class="text-muted">${escapeHtml(message)}</p>`;
    if (resources) resources.innerHTML = '';
    if (targets) targets.innerHTML = '';
    emptyLLMMetricCharts(message, AI_DASHBOARD_CHARTS);
}

function aiSummaryValue(value, unit = '') {
    const text = llmMetricFormatNumber(value);
    return text === '-' ? '-' : `${text}${unit}`;
}

function aiPct(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
    return `${Number(value).toFixed(1).replace(/\.0$/, '')}%`;
}

function storageStatusText(storage) {
    if (!storage) return '-';
    return storage.available ? 'available' : (storage.message || 'unavailable');
}

let _aiEventsTab = 'events';
let _aiEventsCache = [];
let _aiEventsDetection = {};
let _aiEventsAutoDetecting = false;
let _aiEventsLiveTimer = null;
let _aiEventsLiveRunning = false;
let _aiEventsLiveLast = null;
let _aiEventsLiveIntervalMs = 15000;
let _aiEventsLastWindow = { duration: '15m', seconds: 900 };
let _activeAIEventDispositionId = '';
const _aiEventAgentStatusCache = new Map();
const _aiEventAgentPollers = new Map();
const _aiEventActionInFlight = new Set();

function aiEventActionKey(eventId, lane, mode = 'analysis') {
    return `${String(eventId || '')}:${String(lane || '')}:${String(mode || 'analysis')}`;
}

function aiEventActionPending(eventId, lane, mode = 'analysis') {
    return _aiEventActionInFlight.has(aiEventActionKey(eventId, lane, mode));
}

function aiEventRunTerminal(run, status) {
    return ['completed', 'failed', 'cancelled', 'expired'].includes(status?.status || run?.status || '');
}

function switchAIEventsTab(tab) {
    _aiEventsTab = tab || 'events';
    document.querySelectorAll('[data-ai-events-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.aiEventsTab === _aiEventsTab);
    });
    document.querySelectorAll('.ai-events-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `ai-events-tab-${_aiEventsTab}`);
    });
    renderAIEventsTab();
}

async function loadAIEvents() {
    const statusEl = document.getElementById('ai-events-detection');
    if (statusEl) statusEl.innerHTML = '<span class="badge badge-info">加载中</span><span>正在读取智算事件...</span>';
    try {
        await loadFaultTargets();
    } catch (err) {
        console.warn('loadFaultTargets failed before AI events:', err);
    }
    const clusterId = document.getElementById('ai-events-cluster')?.value || '';
    const status = document.getElementById('ai-events-status')?.value || '';
    const duration = document.getElementById('ai-events-window')?.value || '1h';
    const params = new URLSearchParams({ limit: '50' });
    if (clusterId) params.set('cluster_id', clusterId);
    if (status) params.set('status', status);
    if (duration) params.set('duration', duration);
    let data = null;
    try {
        data = await api(`/api/ai-events?${params.toString()}`, { throwOnError: true, timeoutMs: 20000 });
    } catch (err) {
        const message = err?.message || '未知错误';
        renderAIEventsEmpty(`智算事件加载失败：${message}`);
        const detectionEl = document.getElementById('ai-events-detection');
        if (detectionEl) {
            detectionEl.innerHTML = `
                <span class="badge badge-danger">加载失败</span>
                <span>${escapeHtml(message)}</span>
                <button class="btn btn-xs" onclick="loadAIEvents()">重试</button>
            `;
        }
        return;
    }
    _aiEventsCache = data.events || [];
    _aiEventsDetection = data.detection || {};
    restoreAIEventAgentPolling();
    renderAIEventsLiveStatus();
    renderAIEventsCenter();
    if (!_aiEventsAutoDetecting && !_aiEventsCache.length && clusterId) {
        _aiEventsAutoDetecting = true;
        const detectionEl = document.getElementById('ai-events-detection');
        if (detectionEl) detectionEl.insertAdjacentHTML('beforeend', '<span class="badge badge-warning">首次加载自动检测中</span>');
        try {
            await refreshAIEvents({ silent: true });
        } finally {
            _aiEventsAutoDetecting = false;
        }
    }
}

async function refreshAIEvents(options = {}) {
    await loadFaultTargets();
    const clusterId = document.getElementById('ai-events-cluster')?.value || '';
    const detectionWindow = document.getElementById('ai-events-window')?.value || '15m';
    const promUrl = document.getElementById('ai-dashboard-prometheus')?.value || '';
    if (options.live) renderAIEventsLiveStatus('running');
    const data = await api('/api/ai-events/refresh', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            cluster_id: clusterId,
            duration: detectionWindow,
            prometheus_url: promUrl,
            include_experiments: true,
        }),
        timeoutMs: 60000,
    });
    if (!data) {
        showToast('智算事件检测失败', 'error');
        return;
    }
    _aiEventsCache = data.events || [];
    _aiEventsDetection = data.detection || {};
    _aiEventsLastWindow = data.window || { duration: detectionWindow, seconds: 0 };
    restoreAIEventAgentPolling();
    renderAIEventsCenter();
    if (options.live) {
        _aiEventsLiveLast = {
            at: new Date(),
            count: Number(data.count || 0),
            total: Array.isArray(data.events) ? data.events.length : 0,
        };
        renderAIEventsLiveStatus('on');
    }
    if (!options.silent) showToast(`智算检测完成，命中 ${data.count || 0} 个候选事件`);
}

function renderAIEventsLiveStatus(state = '') {
    const btn = document.getElementById('ai-events-live-toggle');
    const el = document.getElementById('ai-events-live-status');
    const enabled = _aiEventsLiveRunning;
    if (btn) {
        btn.classList.toggle('btn-primary', enabled);
        btn.textContent = enabled ? '停止实时检测' : '实时检测';
    }
    if (!el) return;
    if (state === 'running') {
        const windowText = document.getElementById('ai-events-window')?.selectedOptions?.[0]?.textContent || '最近 15 分钟';
        el.innerHTML = `<span class="badge badge-info">实时检测中</span><span>正在采集${escapeHtml(windowText)}内的指标、K8s 事件和故障实验记录...</span>`;
        return;
    }
    if (!enabled) {
        el.innerHTML = '<span class="badge badge-muted">实时检测未开启</span><span>点击“实时检测”后每 15 秒按所选检测窗口生成或更新智算事件。</span>';
        return;
    }
    const lastText = _aiEventsLiveLast?.at
        ? `${_aiEventsLiveLast.at.toLocaleTimeString()} 命中 ${_aiEventsLiveLast.count} 个候选，当前列表 ${_aiEventsLiveLast.total} 条`
        : '等待第一次检测';
    el.innerHTML = `<span class="badge badge-success">实时检测已开启</span><span>${escapeHtml(lastText)}</span>`;
}

async function runAIEventsLiveTick() {
    if (!_aiEventsLiveRunning) return;
    try {
        await refreshAIEvents({ silent: true, live: true });
    } catch (err) {
        const message = err?.message || '检测失败';
        renderAIEventsLiveStatus('error');
        const el = document.getElementById('ai-events-live-status');
        if (el) el.innerHTML = `<span class="badge badge-danger">实时检测失败</span><span>${escapeHtml(message)}</span>`;
    }
}

function stopAIEventsLive() {
    _aiEventsLiveRunning = false;
    if (_aiEventsLiveTimer) clearInterval(_aiEventsLiveTimer);
    _aiEventsLiveTimer = null;
    renderAIEventsLiveStatus('off');
}

function startAIEventsLive() {
    if (_aiEventsLiveTimer) clearInterval(_aiEventsLiveTimer);
    _aiEventsLiveRunning = true;
    renderAIEventsLiveStatus('on');
    runAIEventsLiveTick();
    _aiEventsLiveTimer = setInterval(runAIEventsLiveTick, _aiEventsLiveIntervalMs);
}

function toggleAIEventsLive() {
    if (_aiEventsLiveRunning) {
        stopAIEventsLive();
    } else {
        startAIEventsLive();
    }
}

function renderAIEventsEmpty(message) {
    ['ai-events-summary', 'ai-events-list', 'ai-events-experiments', 'ai-events-rules', 'ai-events-history'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = `<div class="ai-empty-note">${escapeHtml(message)}</div>`;
    });
}

function aiEventStatusLabel(status) {
    return {
        observing: '观察中',
        active: '活跃',
        acknowledged: '已确认',
        diagnosed: '已诊断',
        healing: '自愈中',
        resolved: '已恢复',
    }[status] || status || '-';
}

function aiEventSeverityClass(severity) {
    if (severity === 'critical') return 'danger';
    if (severity === 'warning') return 'warning';
    return 'info';
}

function aiEventSourceLabel(source) {
    const labels = {
        ai_dashboard: 'Dashboard 指标',
        k8s_event: 'K8s Event',
        k8s_pod_health: 'Pod 状态',
        fault_experiment: '连续实验',
    };
    return labels[source] || sourceLabel(source || 'ai_dashboard');
}

function aiEventMetricText(value) {
    if (value === null || value === undefined || value === '') return '-';
    if (typeof value === 'object') {
        return Object.entries(value).map(([k, v]) => `${k}=${llmMetricFormatNumber(v)}`).join(' · ');
    }
    return String(value);
}

function aiEventActionAttrs(action, eventId = '', extra = {}) {
    const attrs = {
        'data-ai-event-action': action,
        'data-ai-event-id': eventId,
        ...extra,
    };
    return Object.entries(attrs)
        .filter(([, value]) => value !== undefined && value !== null && value !== '')
        .map(([key, value]) => `${key}="${escapeHtmlAttr(value)}"`)
        .join(' ');
}

function renderAIEventsCenter() {
    renderAIEventsDetection();
    renderAIEventsSummary();
    renderAIEventsTab();
}

function renderAIEventsDetection() {
    const el = document.getElementById('ai-events-detection');
    if (!el) return;
    const rules = _aiEventsDetection.rules || {};
    const probes = Array.isArray(_aiEventsDetection.k8s_event_sources) ? _aiEventsDetection.k8s_event_sources : [];
    const sourceBadges = probes.length ? probes.map(probe => {
        const stateClass = !probe.configured ? 'badge-muted' : (!probe.available ? 'badge-danger' : (probe.warning_count > 0 ? 'badge-warning' : 'badge-success'));
        const stateText = !probe.configured
            ? 'K8s 未配置'
            : (!probe.available ? 'K8s 连接失败' : (probe.warning_count > 0 ? `K8s Warning ${probe.warning_count}` : 'K8s 已连接无 Warning'));
        const nodes = Array.isArray(probe.nodes) && probe.nodes.length ? `节点 ${probe.nodes.join(', ')}` : '节点未返回';
        const reasons = Array.isArray(probe.recent_reasons) && probe.recent_reasons.length ? `；近期原因 ${probe.recent_reasons.join(', ')}` : '';
        return `<span class="badge ${stateClass}" title="${escapeHtml(`${probe.message || ''}；namespace=${probe.namespace || '-'}；${nodes}${reasons}`)}">${escapeHtml(probe.cluster_id || 'AI 集群')} · ${escapeHtml(stateText)}</span>`;
    }).join('') : '<span class="badge badge-muted">K8s 事件源待探测</span>';
    el.innerHTML = `
        <span class="badge badge-info">${escapeHtml(_aiEventsDetection.algorithm || '智算检测')}</span>
        <span>确认点 ${escapeHtml(_aiEventsDetection.confirmation_points ?? 1)}</span>
        <span>等待队列 >= ${escapeHtml(rules.waiting_requests ?? 1)}</span>
        <span>TTFT P95 >= ${escapeHtml(rules.ttft_p95 ?? 1)}s</span>
        <span>E2E P95 >= ${escapeHtml(rules.e2e_latency_p95 ?? 3)}s</span>
        <span>GPU 显存/KV Cache >= ${escapeHtml(rules.gpu_memory_used ?? 90)}%</span>
        ${sourceBadges}
    `;
}

function renderAIEventsSummary() {
    const el = document.getElementById('ai-events-summary');
    if (!el) return;
    const total = _aiEventsCache.length;
    const active = _aiEventsCache.filter(e => ['active', 'observing', 'acknowledged', 'diagnosed', 'healing'].includes(e.status)).length;
    const critical = _aiEventsCache.filter(e => e.severity === 'critical' && e.status !== 'resolved').length;
    const resolved = _aiEventsCache.filter(e => e.status === 'resolved').length;
    const windowLabel = document.getElementById('ai-events-window')?.selectedOptions?.[0]?.textContent || (_aiEventsLastWindow?.duration || '15m');
    el.innerHTML = `
        <div class="mini-metric"><span>事件总数</span><strong>${total}</strong></div>
        <div class="mini-metric"><span>待处理</span><strong>${active}</strong></div>
        <div class="mini-metric"><span>严重</span><strong>${critical}</strong></div>
        <div class="mini-metric"><span>已恢复</span><strong>${resolved}</strong></div>
        <div class="mini-metric"><span>检测窗口</span><strong>${escapeHtml(windowLabel.replace(/^最近\s*/, ''))}</strong></div>
    `;
}

function renderAIEventsTab() {
    if (_aiEventsTab === 'rules') {
        renderAIEventRules();
    } else if (_aiEventsTab === 'experiments') {
        renderAIEventList('ai-events-experiments', _aiEventsCache.filter(e => e.source === 'fault_experiment'), '暂无连续实验事件');
    } else if (_aiEventsTab === 'history') {
        renderAIEventList('ai-events-history', _aiEventsCache.filter(e => ['diagnosed', 'healing', 'resolved'].includes(e.status)), '暂无处置历史');
    } else {
        renderAIEventList('ai-events-list', _aiEventsCache.filter(e => e.source !== 'fault_experiment' && e.status !== 'resolved'), '暂无待处理智算事件。点击“检测并生成事件”从 Dashboard 指标、K8s Events 和故障实验记录生成；已恢复事件可在“处置历史”查看。');
    }
}

function renderAIEventRules() {
    const el = document.getElementById('ai-events-rules');
    if (!el) return;
    const rules = _aiEventsDetection.rules || {};
    const windowLabel = document.getElementById('ai-events-window')?.selectedOptions?.[0]?.textContent || '最近 15 分钟';
    const editableRows = [
        ['waiting_requests', 'vLLM 等待队列', '个', 0, 1, '请求堆积，常见于服务变慢、下游卡住或负载过高'],
        ['ttft_p95', '推理首 token 延迟', '秒', 0, 0.1, '用户感知首响变慢'],
        ['e2e_latency_p95', '端到端推理时延', '秒', 0, 0.1, '完整请求处理时间恶化'],
        ['gpu_memory_used', 'GPU 显存压力', '%', 0, 1, '可能导致 OOM、KV Cache 不足或队列堆积'],
        ['kv_cache', 'KV Cache 压力', '%', 0, 1, 'KV Cache 接近上限时可能导致队列堆积或请求失败'],
        ['gpu_util_busy', 'GPU 忙但无请求', '%', 0, 1, 'GPU 高占用且无运行请求时提示后台异常负载'],
        ['node_cpu_usage', '节点 CPU 使用率', '%', 0, 1, 'node-exporter CPU 聚合水位'],
        ['node_memory_usage', '节点内存使用率', '%', 0, 1, 'node-exporter 内存水位'],
        ['node_rootfs_usage', '根分区使用率', '%', 0, 1, '根分区压力可能影响日志、镜像和容器运行时'],
        ['node_disk_read', '磁盘读速率', 'MB/s', 0, 1, '节点聚合磁盘读取压力'],
        ['node_disk_write', '磁盘写速率', 'MB/s', 0, 1, '节点聚合磁盘写入压力'],
        ['node_network_receive', '网络接收速率', 'MB/s', 0, 1, '非 lo 网卡接收流量压力'],
        ['node_network_transmit', '网络发送速率', 'MB/s', 0, 1, '非 lo 网卡发送流量压力'],
        ['teller_request_span_ms', 'TELLER 请求跨度', 'ms', 0, 10, '请求级执行路径总耗时过高'],
        ['teller_kernel_p95_ms', 'TELLER Kernel P95', 'ms', 0, 1, 'CUDA Kernel 耗时异常，提示算子、调度或资源竞争瓶颈'],
    ];
    const rows = [
        ['vLLM 等待队列', 'waiting_requests', `>= ${rules.waiting_requests ?? 1}`, '请求堆积，常见于服务变慢、下游卡住或负载过高'],
        ['推理首 token 延迟', 'ttft_p95', `>= ${rules.ttft_p95 ?? 1}s`, '用户感知首响变慢'],
        ['端到端推理时延', 'e2e_latency_p95', `>= ${rules.e2e_latency_p95 ?? 3}s`, '完整请求处理时间恶化'],
        ['GPU 显存压力', 'gpu_memory_used / kv_cache', `>= ${rules.gpu_memory_used ?? 90}%`, '可能导致 OOM、KV Cache 不足或队列堆积'],
        ['GPU 忙但无请求', 'gpu_util + running_requests', `GPU >= ${rules.gpu_util_busy ?? 95}% 且 running=0`, '可能存在异常后台负载或进程泄漏'],
        ['Prometheus 不可用', 'storage.available', 'false', '观测链路失效，需要先恢复采集'],
        ['K8s Warning Event', 'Kubernetes Events', `type=Warning，${windowLabel}`, '采集智算命名空间内 FailedScheduling、BackOff、ImagePull、OOM、NodeNotReady 等事件，并归一化为可诊断故障'],
        ['TELLER 请求上下文', 'request_context.available', 'false', '已采集 trace 但缺少 request_id 时，无法还原完整请求执行路径'],
        ['TELLER Kernel P95', 'kernel_p95_ms', `>= ${rules.teller_kernel_p95_ms ?? 50}ms`, 'CUDA Kernel 耗时异常，提示算子、调度或资源竞争瓶颈'],
    ];
    el.innerHTML = `<div class="ai-rule-note">当前检测窗口：${escapeHtml(windowLabel)}。指标、K8s Event 和真实故障运行记录均按该窗口生成候选事件；已恢复事件保留在处置历史中。</div>
    <div class="ai-threshold-editor">
        <div class="ai-threshold-editor-head">
            <div>
                <strong>阈值配置</strong>
                <small>保存到 <code>detection.thresholds.ai</code>，立即影响下一次“检测并生成事件”。</small>
            </div>
            <div class="table-actions">
                <label class="ai-inline-field"><span>连续确认点</span><input id="ai-rule-confirmation-points" type="number" min="1" step="1" value="${escapeHtml(_aiEventsDetection.confirmation_points ?? 1)}"></label>
                <button class="btn btn-sm btn-primary" onclick="saveAIEventThresholds()">保存阈值</button>
                <button class="btn btn-sm" onclick="refreshAIEvents()">保存后检测</button>
            </div>
        </div>
        <div class="ai-threshold-grid">
            ${editableRows.map(([key, label, unit, min, step, help]) => `
                <label class="ai-threshold-field">
                    <span>${escapeHtml(label)}</span>
                    <div><input data-ai-threshold-key="${escapeHtmlAttr(key)}" type="number" min="${escapeHtml(min)}" step="${escapeHtml(step)}" value="${escapeHtml(rules[key] ?? aiEventDefaultThreshold(key))}"><em>${escapeHtml(unit)}</em></div>
                    <small>${escapeHtml(help)}</small>
                </label>
            `).join('')}
        </div>
        <div id="ai-events-threshold-status" class="ai-threshold-status"></div>
    </div>
    <table class="ai-table">
        <thead><tr><th>规则</th><th>信号</th><th>阈值</th><th>说明</th></tr></thead>
        <tbody>${rows.map(r => `<tr><td><strong>${escapeHtml(r[0])}</strong></td><td>${escapeHtml(r[1])}</td><td>${escapeHtml(r[2])}</td><td>${escapeHtml(r[3])}</td></tr>`).join('')}</tbody>
    </table>`;
}

function aiEventDefaultThreshold(key) {
    return ({
        waiting_requests: 1,
        ttft_p95: 1,
        e2e_latency_p95: 3,
        gpu_memory_used: 90,
        kv_cache: 90,
        gpu_util_busy: 95,
        node_cpu_usage: 85,
        node_memory_usage: 85,
        node_rootfs_usage: 85,
        node_disk_read: 200,
        node_disk_write: 200,
        node_network_receive: 200,
        node_network_transmit: 200,
        teller_request_span_ms: 1000,
        teller_kernel_p95_ms: 50,
    })[key] ?? 0;
}

async function saveAIEventThresholds() {
    const statusEl = document.getElementById('ai-events-threshold-status');
    const inputs = Array.from(document.querySelectorAll('[data-ai-threshold-key]'));
    const thresholds = {};
    inputs.forEach(input => {
        const key = input.dataset.aiThresholdKey;
        if (!key) return;
        const value = Number(input.value);
        thresholds[key] = Number.isFinite(value) ? value : aiEventDefaultThreshold(key);
    });
    const confirmValue = Number(document.getElementById('ai-rule-confirmation-points')?.value || _aiEventsDetection.confirmation_points || 1);
    if (statusEl) statusEl.textContent = '正在保存阈值...';
    const res = await api('/api/platform/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            detection: {
                confirmation_points: Math.max(1, Math.floor(Number.isFinite(confirmValue) ? confirmValue : 1)),
                thresholds: { ai: thresholds },
            },
        }),
    });
    if (!res) {
        if (statusEl) statusEl.textContent = '保存失败，请查看后端日志。';
        showToast('智算阈值保存失败', 'error');
        return;
    }
    await loadAIEvents();
    switchAIEventsTab('rules');
    const nextStatus = document.getElementById('ai-events-threshold-status');
    if (nextStatus) nextStatus.textContent = '已保存并生效。下一次检测会使用这些阈值。';
    showToast('智算检测阈值已保存并生效');
}

function aiEventCompactText(text = '', limit = 140) {
    const normalized = String(text || '').replace(/\s+/g, ' ').trim();
    if (!normalized || normalized.length <= limit) return normalized;
    return `${normalized.slice(0, Math.max(0, limit - 1))}…`;
}

function aiEventPrimaryObject(event = {}) {
    if (event.k8s_object_kind || event.k8s_object_name) {
        return `${event.k8s_object_kind || 'Object'}/${event.k8s_object_name || '-'}`;
    }
    if (event.deployment) return `Deployment/${event.deployment}`;
    if (event.service) return `Service/${event.service}`;
    return '-';
}

function aiEventSummaryFacts(event = {}) {
    const facts = [
        ['集群', event.cluster_name || event.cluster_id || '-'],
        ['命名空间', event.namespace || '-'],
        ['对象', aiEventPrimaryObject(event)],
        ['原因', event.k8s_reason || event.event_type || '-'],
        ['时间', formatTime((event.last_seen_at || event.updated_at || event.created_at) * 1000)],
    ];
    return facts
        .filter(([, value]) => value && value !== '-')
        .slice(0, 5)
        .map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`)
        .join('');
}

function renderAIEventList(elemId, events, emptyText) {
    const el = document.getElementById(elemId);
    if (!el) return;
    if (!events.length) {
        el.innerHTML = `<div class="ai-empty-note">${escapeHtml(emptyText)}</div>`;
        return;
    }
    el.innerHTML = events.map(event => {
        const evidence = Array.isArray(event.evidence) ? event.evidence : [];
        const confirmed = `${event.confirmations || 0}/${event.required_confirmations || 1}`;
        const diagnosis = event.diagnosis || {};
        const runs = event.agent_runs || {};
        const multiRun = runs.multi_agent || (event.agent_run?.mode === 'multi_agent' ? event.agent_run : null);
        const skillRun = runs.agent_skill || (event.agent_run?.mode === 'agent_skill' ? event.agent_run : null);
        const confidence = Number.isFinite(Number(diagnosis.confidence)) ? `${Math.round(Number(diagnosis.confidence) * 100)}%` : '-';
        const diagnosisText = diagnosis.root_cause || diagnosis.summary || '';
        const diagnosisTextZh = diagnosisText ? zhText(diagnosisText) : '';
        const detailFacts = [
            ['工作负载', event.deployment || event.service || '-'],
            ['K8s 对象', `${event.k8s_object_kind || (event.deployment ? 'Deployment' : event.service ? 'Service' : '-')}/${event.k8s_object_name || event.deployment || event.service || '-'}`],
            ['模型', event.model || '-'],
            ['指标', event.metric || '-'],
            ['当前值', aiEventMetricText(event.value)],
            ['阈值', aiEventMetricText(event.threshold)],
            ['确认点', confirmed],
            ['来源', aiEventSourceLabel(event.source)],
        ];
        const eventId = String(event.id || '');
        return `<div class="ai-event-card severity-${escapeHtml(event.severity || 'info')}">
            <div class="ai-event-main">
                <div class="ai-event-icon">${event.severity === 'critical' ? '!' : event.status === 'resolved' ? 'OK' : 'AI'}</div>
                <div class="ai-event-body">
                    <div class="ai-event-title">
                        <strong>${escapeHtml(event.title || event.event_type || '智算事件')}</strong>
                        <span class="badge badge-${aiEventSeverityClass(event.severity)}">${escapeHtml(event.severity || 'info')}</span>
                        <span class="badge badge-info">${escapeHtml(aiEventStatusLabel(event.status))}</span>
                        ${diagnosisText ? `<span class="badge badge-success">诊断 ${escapeHtml(confidence)}</span>` : ''}
                    </div>
                    <p>${escapeHtml(aiEventCompactText(event.description || '', 150))}</p>
                    <div class="ai-event-meta">
                        ${aiEventSummaryFacts(event)}
                    </div>
                    ${diagnosisText ? `<div class="ai-event-diagnosis compact">
                        <strong>诊断</strong>
                        <span>${escapeZh(aiEventCompactText(diagnosisTextZh, 120))}</span>
                    </div>` : ''}
                    <details class="ai-event-detail">
                        <summary>查看指标、证据和处理状态</summary>
                        <div class="ai-event-detail-grid">
                            ${detailFacts.map(([label, value]) => `<div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></div>`).join('')}
                            ${multiRun?.run_id ? `<div><strong>多智能体</strong><span>${escapeHtml(statusLabel(_aiEventAgentStatusCache.get(multiRun.run_id)?.status || multiRun.status || 'running'))}</span></div>` : ''}
                            ${skillRun?.run_id ? `<div><strong>Agent+Skill</strong><span>${escapeHtml(statusLabel(_aiEventAgentStatusCache.get(skillRun.run_id)?.status || skillRun.status || 'running'))}</span></div>` : ''}
                        </div>
                        ${evidence.length ? `<div class="ai-event-evidence compact">${evidence.slice(0, 6).map(e => `<span>${escapeHtml(e.source || '-')}:${escapeHtml(e.signal || '-')}=${escapeHtml(aiEventMetricText(e.value))}</span>`).join('')}</div>` : ''}
                        ${event.heal_result ? '<div class="ai-event-evidence compact"><span>自愈方案已生成</span></div>' : ''}
                    </details>
                </div>
            </div>
            <div class="ai-event-actions">
                <button class="btn btn-sm" ${aiEventActionAttrs('open', eventId, {'data-stage': 'details'})}>详情</button>
                <button class="btn btn-sm btn-primary" ${aiEventActionAttrs('open', eventId, {'data-stage': 'analysis'})}>分析</button>
                <button class="btn btn-sm" ${aiEventActionAttrs('open', eventId, {'data-stage': 'healing'})}>自愈</button>
                <button class="btn btn-sm" ${aiEventActionAttrs('status', eventId, {'data-status': 'resolved'})}>标记恢复</button>
            </div>
        </div>`;
    }).join('');
}

function aiEventById(eventId) {
    return (_aiEventsCache || []).find(e => String(e.id || '') === String(eventId || '')) || null;
}

function isAIEventActionable(event = {}) {
    return !!event && event.status !== 'resolved';
}

function aiEventRun(event = {}, lane = 'multi_agent') {
    const runs = event.agent_runs || {};
    const direct = event.agent_run || {};
    return runs[lane] || (direct.mode === lane ? direct : null) || null;
}

function aiEventRunStatus(run = {}) {
    return run?.run_id ? _aiEventAgentStatusCache.get(run.run_id) : null;
}

const AI_ANALYSIS_PHASES = [
    { id: 'context', label: '上下文检索', optional: true },
    { id: 'hypothesis', label: '假设生成', optional: true },
    { id: 'plan', label: '验证规划', optional: true },
    { id: 'investigation', label: '调查取证', optional: false },
    { id: 'reasoning', label: '推理裁决', optional: false },
];

function aiAnalysisPhaseStorageKey(lane) {
    return `agenticsre.ai.analysis-phases.${lane}`;
}

function aiAnalysisPhases(lane) {
    let saved = [];
    try { saved = JSON.parse(localStorage.getItem(aiAnalysisPhaseStorageKey(lane)) || '[]'); } catch (_) {}
    const selected = new Set(Array.isArray(saved) && saved.length ? saved : AI_ANALYSIS_PHASES.map(item => item.id));
    AI_ANALYSIS_PHASES.filter(item => !item.optional).forEach(item => selected.add(item.id));
    return AI_ANALYSIS_PHASES.map(item => item.id).filter(id => selected.has(id));
}

function updateAIAnalysisPhase(lane, phase, enabled) {
    const selected = new Set(aiAnalysisPhases(lane));
    if (enabled) selected.add(phase); else selected.delete(phase);
    AI_ANALYSIS_PHASES.filter(item => !item.optional).forEach(item => selected.add(item.id));
    localStorage.setItem(aiAnalysisPhaseStorageKey(lane), JSON.stringify(
        AI_ANALYSIS_PHASES.map(item => item.id).filter(id => selected.has(id))
    ));
}

function renderAIAnalysisPhaseConfig(lane, run = null) {
    const selected = new Set(run?.analysis_phases || aiAnalysisPhases(lane));
    const locked = !!run;
    return `<div class="ai-analysis-phase-config" data-analysis-lane="${escapeHtmlAttr(lane)}">
        <div class="ai-analysis-phase-head"><strong>分析流程</strong><span>${locked ? '本次运行配置' : '缺省全部执行'}</span></div>
        <div class="ai-analysis-phase-options">
            ${AI_ANALYSIS_PHASES.map(item => `<label class="ai-analysis-phase-option ${item.optional ? '' : 'required'}">
                <input type="checkbox" ${selected.has(item.id) ? 'checked' : ''} ${locked || !item.optional ? 'disabled' : ''}
                    onchange="updateAIAnalysisPhase('${escapeHtmlAttr(lane)}','${escapeHtmlAttr(item.id)}',this.checked)">
                <span>${escapeHtml(item.label)}</span>
            </label>`).join('')}
        </div>
    </div>`;
}

function aiEventSkillResult(event = {}) {
    const skillRun = aiEventRun(event, 'agent_skill');
    const status = aiEventRunStatus(skillRun) || {};
    const requestedStages = skillRun?.requested_stages || [];
    const live = agentSkillResultFromEvent(latestAgentSkillEvent(status), requestedStages);
    const persisted = normalizeAIEventSkillResult(event.skill_result || event.diagnosis, event, requestedStages);
    if (live?.diagnosis?.root_cause || live?.skill_runtime?.primary_skill_id) {
        return normalizeAIEventSkillResult({ ...persisted, ...live }, event, requestedStages);
    }
    return persisted || null;
}

function normalizeAIEventSkillResult(result = {}, event = {}, requestedStages = []) {
    if (!result || typeof result !== 'object') return null;
    const diagnosis = result.diagnosis && typeof result.diagnosis === 'object'
        ? result.diagnosis
        : (result.root_cause || result.summary || result.skill_id ? result : (event.diagnosis || {}));
    if (!diagnosis || typeof diagnosis !== 'object') return null;
    const skillId = result.skill_runtime?.primary_skill_id || diagnosis.skill_id || event.diagnosis?.skill_id || '';
    const executionPlan = result.execution_plan || {};
    const commands = Array.isArray(executionPlan.commands) ? executionPlan.commands : [];
    const normalized = {
        status: result.status || 'completed',
        domain: result.domain || event.domain || 'llm',
        dry_run: result.dry_run !== false,
        workflow_run_id: result.workflow_run_id || '',
        diagnosis: {
            ...diagnosis,
            confidence: Number.isFinite(Number(diagnosis.confidence)) ? Number(diagnosis.confidence) : undefined,
        },
        skill_runtime: {
            ...(result.skill_runtime || {}),
            primary_skill_id: skillId,
            matched_count: result.skill_runtime?.matched_count || (skillId ? 1 : 0),
        },
        execution_plan: {
            ...executionPlan,
            commands,
            command_count: executionPlan.command_count ?? commands.length,
        },
        requested_stages: requestedStages.length ? requestedStages : (result.requested_stages || ['detect', 'diagnose']),
        invocation_mode: 'agent_tool',
    };
    if (!Array.isArray(normalized.skill_runtime.runs) || !normalized.skill_runtime.runs.length) {
        normalized.skill_runtime.runs = [{
            skill: { id: skillId || 'llm.diagnosis', name: skillId || '智算诊断 Skill' },
            stages: {
                detect: { status: 'matched', summary: event.title || diagnosis.summary || '事件已匹配' },
                diagnose: {
                    status: diagnosis.status || 'ok',
                    summary: diagnosis.root_cause || diagnosis.summary || '已生成诊断结论',
                    confidence: normalized.diagnosis.confidence,
                    evidence: diagnosis.evidence || [],
                },
            },
            evidence: diagnosis.evidence || [],
            confidence: normalized.diagnosis.confidence,
        }];
    }
    return normalized;
}

function aiEventSkillPayload(event = {}, stages = ['detect', 'diagnose']) {
    const evidence = Array.isArray(event.evidence) ? event.evidence : [];
    const text = [
        event.event_type,
        event.fault_type,
        event.title,
        event.description,
        event.metric,
        event.k8s_reason,
        event.model,
        event.deployment,
        event.service,
    ].map(x => String(x || '')).join(' ').toLowerCase();
    let faultType = event.fault_type || event.event_type || 'endpoint_timeout';
    if (/imagepull|errimagepull|failedpull|pull image|拉取镜像|镜像拉取/.test(text)) faultType = 'image_pull_error';
    else if (/schedule|failedscheduling|unschedulable|insufficient/.test(text)) faultType = 'scheduling_failed';
    else if (/memory|oom|显存|kv/.test(text)) faultType = 'gpu_oom';
    else if (/busy|util|cuda|kernel|gpu/.test(text)) faultType = 'gpu_resource_pressure';
    else if (/crash|restart|backoff|unavailable|success_rate_zero/.test(text)) faultType = 'vllm_process_crash';
    else if (/observability|prometheus|target|metrics|采集/.test(text)) faultType = 'observability_unavailable';
    else if (/timeout|latency|ttft|tpot|queue|backlog|endpoint/.test(text)) faultType = 'endpoint_timeout';
    const isK8sEvent = ['k8s_event', 'k8s_pod_health'].includes(event.source) || !!(event.k8s_object_kind || event.k8s_object_name || event.k8s_reason);
    const domain = isK8sEvent && [
        'scheduling_failed',
        'image_pull_error',
        'pod_crashloop',
        'oom_killed',
        'volume_mount_failed',
        'node_not_ready',
        'service_unavailable',
        'k8s_event_warning',
    ].includes(faultType) ? 'k8s' : 'llm';
    return {
        domain,
        stages: stages || ['detect', 'diagnose', 'heal', 'verify'],
        dry_run: true,
        allow_legacy_fallback: true,
        payload: {
            source: 'ai_event_center',
            domain,
            event_id: event.id || '',
            cluster_id: event.cluster_id || '',
            namespace: event.namespace || 'llm-k8s',
            deployment: event.deployment || event.service || 'vllm-server',
            service: event.service || event.deployment || 'vllm-server',
            model: event.model || '',
            endpoint: event.vllm_endpoint || '',
            fault_type: faultType,
            raw_fault_type: event.event_type || '',
            severity: event.severity || 'warning',
            message: `${event.title || ''} | ${event.description || ''}`,
            metrics: { metric: event.metric || '', value: event.value, threshold: event.threshold },
            evidence,
            k8s_event: {
                reason: event.k8s_reason || '',
                object_kind: event.k8s_object_kind || '',
                object_name: event.k8s_object_name || '',
                namespace: event.namespace || '',
            },
            observation: event.observation || {},
            experiment_context: event.experiment_context || {},
        },
    };
}

function aiEventQuery(event = {}, mode = 'analysis') {
    const payload = aiEventSkillPayload(event).payload || {};
    const action = mode === 'healing'
        ? '分析根因并给出受控自愈建议、风险门禁和验证步骤'
        : '分析根因、证据、影响范围和置信度';
    return [
        `[智算事件][${payload.fault_type || event.event_type || 'llm'}] ${event.title || '智算事件'}`,
        event.description || '',
        `cluster=${event.cluster_id || ''}`,
        `namespace=${payload.namespace || ''}`,
        `deployment=${payload.deployment || ''}`,
        `metric=${event.metric || ''}`,
        `value=${aiEventMetricText(event.value)}`,
        `请输出中文结论，并${action}。`,
    ].filter(Boolean).join('; ');
}

function renderAIEventDetailSummary(event = {}, skillBody = {}) {
    return `<div class="alert-context-strip compact">
        <div><span>事件</span><strong>${escapeHtml(event.title || event.event_type || '-')}</strong></div>
        <div><span>目标</span><strong>${escapeHtml(event.deployment || event.service || skillBody.payload?.deployment || '-')}</strong></div>
        <div><span>命名空间</span><strong>${escapeHtml(event.namespace || skillBody.payload?.namespace || '-')}</strong></div>
        <div><span>来源</span><strong>${escapeHtml(aiEventSourceLabel(event.source || 'ai_dashboard'))}</strong></div>
    </div>`;
}

function renderAIEventDetailsSection(event = {}, context = {}) {
    const evidence = Array.isArray(event.evidence) ? event.evidence : [];
    const observation = event.observation || {};
    const multiRun = context.multiRun || null;
    const skillRun = context.skillRun || null;
    const healResult = context.healResult || null;
    return `<div class="alert-details-grid">
        <div class="alert-detail-card">
            <div class="skill-card-head"><strong>事件详情</strong><span class="badge badge-${aiEventSeverityClass(event.severity)}">${escapeHtml(event.severity || '-')}</span></div>
            <div class="skill-kv"><span>标题</span><strong>${escapeHtml(event.title || '-')}</strong></div>
            <div class="skill-kv"><span>描述</span><strong>${escapeHtml(event.description || '-')}</strong></div>
            <div class="skill-kv"><span>指标</span><strong>${escapeHtml(event.metric || '-')} = ${escapeHtml(aiEventMetricText(event.value))}</strong></div>
            <div class="skill-kv"><span>阈值</span><strong>${escapeHtml(aiEventMetricText(event.threshold))}</strong></div>
            <div class="skill-kv"><span>时间</span><strong>${escapeHtml(formatTime((event.last_seen_at || event.updated_at || event.created_at) * 1000))}</strong></div>
        </div>
        <div class="alert-detail-card">
            <div class="skill-card-head"><strong>处理状态</strong><span class="badge badge-info">智算事件</span></div>
            <div class="skill-kv"><span>多智能体分析</span><strong>${escapeHtml(multiRun ? statusLabel(aiEventRunStatus(multiRun)?.status || multiRun.status || 'running') : '未运行')}</strong></div>
            <div class="skill-kv"><span>Agent+Skill</span><strong>${escapeHtml(skillRun ? statusLabel(aiEventRunStatus(skillRun)?.status || skillRun.status || 'running') : '未运行')}</strong></div>
            <div class="skill-kv"><span>自愈方案</span><strong>${escapeHtml(healResult ? '已生成' : '未生成')}</strong></div>
            <div class="skill-kv"><span>模型</span><strong>${escapeHtml(event.model || '-')}</strong></div>
        </div>
        ${evidence.length ? `<details class="alert-detail-raw" open>
            <summary>检测证据</summary>
            <ul>${evidence.slice(0, 10).map(e => `<li>${escapeHtml(e.source || '-')}:${escapeHtml(e.signal || '-')}=${escapeHtml(aiEventMetricText(e.value))}</li>`).join('')}</ul>
        </details>` : ''}
        ${Object.keys(observation).length ? `<details class="alert-detail-raw">
            <summary>观测上下文</summary>
            <pre>${escapeHtml(JSON.stringify(observation, null, 2))}</pre>
        </details>` : ''}
    </div>`;
}

function renderAIEventAgentAnalysisCard(event = {}, run, status, active = true) {
    const eventId = String(event.id || '');
    const pending = aiEventActionPending(eventId, 'multi_agent', 'analysis');
    if (!run) {
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>多智能体分析</strong><span class="badge badge-gray">未运行</span></div>
            <p>复用原多智能体 RCA 流程，展示阶段、假设、证据和最终根因。</p>
            ${renderAIAnalysisPhaseConfig('multi_agent')}
            <button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-agent-analysis', eventId)}>${pending ? '启动中...' : '启动多智能体分析'}</button>
        </div>`;
    }
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>多智能体分析</strong><span class="badge badge-${status?.status === 'completed' ? 'success' : status?.status === 'failed' ? 'danger' : 'info'}">${escapeHtml(statusLabel(status?.status || run.status || 'running'))}</span></div>
        <div class="skill-kv"><span>Run</span><strong>${escapeHtml(run.run_id || '-')}</strong></div>
        ${renderAIAnalysisPhaseConfig('multi_agent', run)}
        ${status ? renderAgentWorkflowDetail(status) : '<p class="text-muted">等待 RCA 阶段事件返回。</p>'}
        <div class="table-actions">
            <button class="btn btn-sm" ${aiEventActionAttrs('refresh-run', eventId, {'data-run-id': run.run_id || ''})}>刷新过程</button>
            <button class="btn btn-sm" ${aiEventActionAttrs('open-rca', eventId, {'data-run-id': run.run_id || '', 'data-mode': 'multi_agent'})}>打开 RCA 工作流</button>
            ${aiEventRunTerminal(run, status) ? `<button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-agent-analysis', eventId)}>${pending ? '启动中...' : '重新启动'}</button>` : ''}
        </div>
    </div>`;
}

function renderAIEventSkillAnalysisCard(event = {}, skillResult, run, status, active = true) {
    const eventId = String(event.id || '');
    const pending = aiEventActionPending(eventId, 'agent_skill', 'diagnose');
    if (!skillResult) {
        if (run?.run_id) {
            return `<div class="skill-result-card alert-lane-card">
                <div class="skill-card-head"><strong>Agent+Skill 分析</strong><span class="badge badge-info">${escapeHtml(statusLabel(status?.status || run.status || 'running'))}</span></div>
                <div class="skill-kv"><span>Run</span><strong>${escapeHtml(run.run_id || '-')}</strong></div>
                <p>Agent 正在按“假设生成 -> 验证规划 -> 调查取证 -> 推理裁决”推进，命中 Skill 后会继续展示工具调用和证据。</p>
                ${status ? renderAgentWorkflowDetail(status) : renderAgentSkillCompactProgress(status || {})}
                <div class="table-actions">
                    <button class="btn btn-sm" ${aiEventActionAttrs('refresh-run', eventId, {'data-run-id': run.run_id || ''})}>刷新过程</button>
                    <button class="btn btn-sm" ${aiEventActionAttrs('open-rca', eventId, {'data-run-id': run.run_id || '', 'data-mode': 'agent_skill'})}>打开 RCA 工作流</button>
                    ${aiEventRunTerminal(run, status) ? `<button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-skill', eventId, {'data-mode': 'diagnose'})}>${pending ? '启动中...' : '重新启动'}</button>` : ''}
                </div>
            </div>`;
        }
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>Agent+Skill 分析</strong><span class="badge badge-gray">未运行</span></div>
            <p>Agent 根据智算事件上下文生成假设，规划验证步骤，调用匹配 Skill 调查证据并输出中文结论。</p>
            ${renderAIAnalysisPhaseConfig('agent_skill')}
            <button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-skill', eventId, {'data-mode': 'diagnose'})}>${pending ? '启动中...' : '启动 Agent+Skill 分析'}</button>
        </div>`;
    }
    const diagnosis = skillResult.diagnosis || {};
    const runtime = skillResult.skill_runtime || {};
    const evidence = Array.isArray(diagnosis.evidence) ? diagnosis.evidence : [];
    const actionCount = skillResult.execution_plan?.command_count ?? (skillResult.execution_plan?.suggestions || []).length ?? 0;
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>Agent+Skill 分析</strong><span class="badge badge-${diagnosis.confidence >= 0.85 ? 'success' : 'warning'}">${escapeHtml(formatConfidence(diagnosis.confidence))}</span></div>
        <div class="skill-kv"><span>Run</span><strong>${escapeHtml(run?.run_id || '-')}</strong></div>
        ${renderAIAnalysisPhaseConfig('agent_skill', run)}
        ${status ? renderAgentWorkflowDetail(status) : renderAgentSkillCompactProgress({}, skillResult)}
        <div class="rca-remediation">
            <strong>最终结论：</strong>${escapeZh(diagnosis.root_cause || diagnosis.summary || '暂无诊断结论')}
        </div>
        <div class="skill-kv"><span>关键 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
        <div class="skill-kv"><span>根因类型</span><strong>${escapeHtml(faultTypeLabel(diagnosis.fault_type || '-'))}</strong></div>
        ${renderEvidenceList(evidence, { title: '关键证据', maxItems: 3 })}
        ${actionCount ? `<div class="skill-kv"><span>建议动作</span><strong>${escapeHtml(actionCount)} 个，默认 Dry-run</strong></div>` : ''}
        <details class="skill-raw-fold" open>
            <summary>Skill 调用细节</summary>
            ${renderAgentSkillInvocationFlow(skillResult, { mode: 'analysis' })}
            ${renderAlertSkillExecutionProcess(skillResult, skillResult.requested_stages || [], { open: false, showMissing: false })}
        </details>
        <div class="table-actions">
            <button class="btn btn-sm" ${aiEventActionAttrs('refresh-run', eventId, {'data-run-id': run?.run_id || ''})}>刷新过程</button>
            <button class="btn btn-sm" ${aiEventActionAttrs('open-rca', eventId, {'data-run-id': run?.run_id || '', 'data-mode': 'agent_skill'})}>打开 RCA 工作流</button>
            ${aiEventRunTerminal(run, status) ? `<button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-skill', eventId, {'data-mode': 'diagnose'})}>${pending ? '启动中...' : '重新启动'}</button>` : ''}
        </div>
    </div>`;
}

function renderAIEventAgentHealingCard(event = {}, run, status, healResult, active = true) {
    const eventId = String(event.id || '');
    return `<div class="skill-result-card alert-lane-card">
        <div class="skill-card-head"><strong>多智能体自愈</strong><span class="badge badge-${healResult ? 'success' : 'gray'}">${healResult ? '方案已生成' : '未生成'}</span></div>
        <p>复用原自愈知识库，基于智算事件上下文输出 Dry-run 方案、风险和验证步骤。</p>
        ${run ? `<div class="skill-kv"><span>关联 RCA</span><strong>${escapeHtml(run.run_id || '-')} · ${escapeHtml(statusLabel(status?.status || run.status || 'running'))}</strong></div>` : ''}
        ${healResult ? `<div class="skill-kv"><span>动作数</span><strong>${escapeHtml((healResult.suggestions || []).length)}</strong></div>` : '<p class="text-muted">尚未生成多智能体自愈方案。</p>'}
        <div class="table-actions">
            <button class="btn btn-sm" ${active ? '' : 'disabled'} ${aiEventActionAttrs('run-agent-healing', eventId)}>生成多智能体自愈</button>
        </div>
        ${healResult ? renderAIEventHealResultHtml(healResult) : ''}
    </div>`;
}

function renderAIEventHealResultHtml(result = {}) {
    const summary = result.summary || {};
    const target = result.target || {};
    const suggestions = result.suggestions || [];
    const blocked = result.blocked_templates || [];
    return `<div class="alert-heal-nested ai-event-heal-result">
        <div class="alert-heal-meta">
            <span class="badge badge-${suggestions.length ? 'success' : 'warning'}">${suggestions.length ? '可生成自愈方案' : '暂无可执行方案'}</span>
            <span class="badge badge-warning">Dry-run</span>
            <span class="badge badge-info">故障 ${escapeHtml(summary.fault_type || result.fault_type || '-')}</span>
            <span class="badge badge-info">namespace ${escapeHtml(summary.namespace || result.namespace || '-')}</span>
            <span class="badge badge-info">目标 ${escapeHtml(target.deployment || target.pod || target.service || summary.target || '-')}</span>
        </div>
        <div class="rem-actions-list">
            ${suggestions.map((s, i) => renderHealSuggestionItem(s, i)).join('')}
            ${blocked.map((s, i) => renderHealBlockedItem(s, i)).join('')}
        </div>
    </div>`;
}

function renderAIEventSkillHealingCard(event = {}, skillResult, run, status, active = true) {
    const eventId = String(event.id || '');
    const pending = aiEventActionPending(eventId, 'agent_skill', 'heal');
    if (!skillResult) {
        if (run?.run_id) {
            return `<div class="skill-result-card alert-lane-card">
                <div class="skill-card-head"><strong>Agent+Skill 自愈</strong><span class="badge badge-info">${escapeHtml(statusLabel(status?.status || run.status || 'running'))}</span></div>
                <p>Agent 正在调用 Skill 生成受控恢复方案，默认只生成 Dry-run 建议。</p>
                ${renderAgentSkillCompactProgress(status || {})}
                <div class="table-actions">
                    <button class="btn btn-sm" ${aiEventActionAttrs('refresh-run', eventId, {'data-run-id': run.run_id || ''})}>刷新过程</button>
                    <button class="btn btn-sm" ${aiEventActionAttrs('open-rca', eventId, {'data-run-id': run.run_id || '', 'data-mode': 'agent_skill'})}>打开 RCA 工作流</button>
                </div>
            </div>`;
        }
        return `<div class="skill-result-card alert-lane-card">
            <div class="skill-card-head"><strong>Agent+Skill 自愈</strong><span class="badge badge-gray">未运行</span></div>
            <p>Agent 调用 Skill 生成门禁、恢复动作和验证步骤，默认 Dry-run。</p>
            <button class="btn btn-sm btn-primary" ${active && !pending ? '' : 'disabled'} ${aiEventActionAttrs('run-skill', eventId, {'data-mode': 'heal'})}>${pending ? '启动中...' : '启动 Agent+Skill 自愈'}</button>
        </div>`;
    }
    return renderAlertSkillHealingCard(skillResult, `'${eventId}'`, active, { agentRun: run, agentStatus: status })
        .replace(/runSkillHealingFromAlert\('([^']+)'\)/g, "runAIEventSkill('$1','heal')")
        .replace(/refreshAlertSkillAgentRun\('([^']+)'/g, "refreshAIEventAgentRun('$1'");
}

function renderAIEventDispositionPanelHtml(event = {}, stage = 'details') {
    const active = isAIEventActionable(event);
    const skillBody = aiEventSkillPayload(event, ['detect', 'diagnose']);
    const multiRun = aiEventRun(event, 'multi_agent');
    const skillRun = aiEventRun(event, 'agent_skill');
    const multiStatus = aiEventRunStatus(multiRun);
    const skillStatus = aiEventRunStatus(skillRun);
    const skillResult = aiEventSkillResult(event);
    const healResult = event.heal_result || null;
    const eventId = String(event.id || '');
    const activeDetails = stage === 'details' || stage === 'ready';
    const activeAnalysis = stage === 'analysis';
    const activeHealing = stage === 'healing';
    return `<div class="alert-disposition-workbench ai-event-disposition-workbench">
        <div class="alert-disposition-toolbar">
            <div class="alert-disposition-tabs">
                <button class="btn btn-sm ${activeDetails ? 'btn-primary' : ''}" ${aiEventActionAttrs('switch', eventId, {'data-stage': 'details'})}>详情</button>
                <button class="btn btn-sm ${activeAnalysis ? 'btn-primary' : ''}" ${aiEventActionAttrs('switch', eventId, {'data-stage': 'analysis'})}>分析分支</button>
                <button class="btn btn-sm ${activeHealing ? 'btn-primary' : ''}" ${aiEventActionAttrs('switch', eventId, {'data-stage': 'healing'})}>自愈分支</button>
            </div>
            <div class="alert-disposition-actions">
                ${activeDetails ? `
                    <button class="btn btn-sm btn-primary" ${active ? '' : 'disabled'} ${aiEventActionAttrs('switch', eventId, {'data-stage': 'analysis'})}>去分析分支</button>
                    <button class="btn btn-sm" ${active ? '' : 'disabled'} ${aiEventActionAttrs('switch', eventId, {'data-stage': 'healing'})}>去自愈分支</button>
                ` : activeAnalysis ? `
                    <span class="text-muted">选择“多智能体”或“Agent+Skill”分析链路</span>
                ` : `
                    <span class="text-muted">选择“多智能体”或“Agent+Skill”自愈链路</span>
                `}
            </div>
        </div>
        <div class="alert-disposition-status">
            <span class="badge badge-${active ? 'success' : 'gray'}">${active ? '事件活跃' : '事件已恢复'}</span>
            <span class="badge badge-info">${escapeHtml(skillBody.domain || skillBody.payload?.domain || 'llm')}</span>
            <span class="badge badge-info">${escapeHtml(skillBody.payload?.fault_type || '-')}</span>
            <span class="badge badge-warning">Dry-run</span>
            ${multiRun ? `<span class="badge badge-success">多智能体 ${escapeHtml(statusLabel(multiStatus?.status || multiRun.status || 'running'))}</span>` : ''}
            ${skillRun ? `<span class="badge badge-info">Agent+Skill ${escapeHtml(statusLabel(skillStatus?.status || skillRun.status || 'running'))}</span>` : ''}
            ${skillResult ? '<span class="badge badge-success">Skill 工具已运行</span>' : ''}
            ${healResult ? '<span class="badge badge-success">自愈方案已生成</span>' : ''}
        </div>
        ${active ? '' : '<div class="alert-resolved-note">最新检测未返回该智算事件，系统不会再触发新的分析或自愈动作。已完成的处理过程仍可查看。</div>'}
        ${renderAIEventDetailSummary(event, skillBody)}
        ${activeDetails ? renderAIEventDetailsSection(event, { multiRun, skillRun, healResult }) : ''}
        ${activeAnalysis ? `<div class="alert-section-block">
            <div class="alert-section-title"><strong>分析过程</strong><span>根因和证据</span></div>
            <div class="alert-compare-grid">
                ${renderAIEventAgentAnalysisCard(event, multiRun, multiStatus, active)}
                ${renderAIEventSkillAnalysisCard(event, skillResult, skillRun, skillStatus, active)}
            </div>
        </div>` : ''}
        ${activeHealing ? `<div class="alert-section-block">
            <div class="alert-section-title"><strong>自愈方案</strong><span>门禁、动作和验证</span></div>
            <div class="alert-compare-grid">
                ${renderAIEventAgentHealingCard(event, multiRun, multiStatus, healResult, active)}
                ${renderAIEventSkillHealingCard(event, skillResult, skillRun, skillStatus, active)}
            </div>
        </div>` : ''}
    </div>`;
}

function showAIEventDisposition(eventId, stage = 'details') {
    const event = aiEventById(eventId);
    if (!event) {
        showToast('智算事件不存在或已刷新', 'error');
        return;
    }
    _activeAIEventDispositionId = String(eventId || '');
    const modal = document.getElementById('alert-disposition-modal');
    const body = document.getElementById('alert-disposition-modal-body');
    const title = document.getElementById('alert-disposition-title');
    const subtitle = document.getElementById('alert-disposition-subtitle');
    if (!modal || !body) return;
    if (title) title.textContent = '智算事件分析与自愈';
    if (subtitle) subtitle.textContent = isAIEventActionable(event)
        ? '复用通算告警处置模型，支持多智能体和 Agent+Skill 两条链路'
        : '事件已恢复，仅展示历史处理状态';
    body.innerHTML = renderAIEventDispositionPanelHtml(event, stage);
    modal.style.display = 'flex';
    [aiEventRun(event, 'multi_agent'), aiEventRun(event, 'agent_skill')].filter(Boolean).forEach(run => {
        if (run.run_id && !_aiEventAgentStatusCache.has(run.run_id)) refreshAIEventAgentRun(event.id, run.run_id);
    });
}

function switchAIEventDispositionStage(eventId, stage) {
    showAIEventDisposition(eventId, stage || 'details');
}

function refreshOpenAIEventDisposition(stage = '') {
    if (!_activeAIEventDispositionId) return;
    const event = aiEventById(_activeAIEventDispositionId);
    if (!event) return;
    const currentStage = stage || (document.querySelector('.ai-event-disposition-workbench .alert-disposition-tabs .btn-primary')?.textContent.includes('自愈') ? 'healing'
        : document.querySelector('.ai-event-disposition-workbench .alert-disposition-tabs .btn-primary')?.textContent.includes('分析') ? 'analysis'
        : 'details');
    showAIEventDisposition(_activeAIEventDispositionId, currentStage);
}

function restoreAIEventAgentPolling() {
    (_aiEventsCache || []).forEach(event => {
        const runs = event.agent_runs || {};
        const candidates = [event.agent_run, runs.multi_agent, runs.agent_skill].filter(Boolean);
        candidates.forEach(run => {
            if (!run.run_id) return;
            if (['completed', 'failed', 'cancelled', 'expired'].includes(run.status)) return;
            startAIEventAgentPolling(event.id, run.run_id, { requestedStages: run.requested_stages || [] });
        });
    });
}

function startAIEventAgentPolling(eventId, runId, options = {}) {
    if (!eventId || !runId) return;
    const existing = _aiEventAgentPollers.get(runId);
    if (existing) clearInterval(existing);
    const requestedStages = options.requestedStages || [];
    let attempts = 0;
    const tick = async () => {
        attempts += 1;
        let status = null;
        try {
            status = await api(`/api/rca/${encodeURIComponent(runId)}?_=${Date.now()}`, {
                cache: 'no-store',
                throwOnError: true,
                suppressErrorLog: true,
            });
        } catch (error) {
            if (error?.status === 404) {
                const stale = { id: runId, status: 'expired', error: '运行记录已过期或服务已重启' };
                _aiEventAgentStatusCache.set(runId, stale);
                const event = _aiEventsCache.find(e => e.id === eventId);
                const runs = event?.agent_runs || {};
                [event?.agent_run, runs.multi_agent, runs.agent_skill].filter(Boolean).forEach(run => {
                    if (run.run_id === runId) run.status = 'expired';
                });
                clearInterval(_aiEventAgentPollers.get(runId));
                _aiEventAgentPollers.delete(runId);
                if (_activeAIEventDispositionId === String(eventId || '')) refreshOpenAIEventDisposition();
                renderAIEventsTab();
                return;
            }
        }
        if (!status) {
            if (attempts >= 60) {
                clearInterval(_aiEventAgentPollers.get(runId));
                _aiEventAgentPollers.delete(runId);
            }
            return;
        }
        _aiEventAgentStatusCache.set(runId, status);
        const event = _aiEventsCache.find(e => e.id === eventId);
        const runs = event?.agent_runs || {};
        [event?.agent_run, runs.multi_agent, runs.agent_skill].filter(Boolean).forEach(run => {
            if (run.run_id !== runId) return;
            run.status = status.status || run.status || 'running';
            run.requested_stages = requestedStages.length ? requestedStages : (run.requested_stages || []);
        });
        if (_activeAIEventDispositionId === String(eventId || '')) {
            refreshOpenAIEventDisposition();
        }
        renderAIEventsTab();
        if (['completed', 'failed', 'cancelled', 'expired'].includes(status.status) || attempts >= 60) {
            clearInterval(_aiEventAgentPollers.get(runId));
            _aiEventAgentPollers.delete(runId);
        }
    };
    const timer = setInterval(tick, 2500);
    _aiEventAgentPollers.set(runId, timer);
    tick();
}

async function refreshAIEventAgentRun(eventId, runId) {
    if (!runId) return null;
    const status = await api(`/api/rca/${encodeURIComponent(runId)}?_=${Date.now()}`, { cache: 'no-store' });
    if (!status) {
        showToast('Agent+Skill 过程刷新失败', 'error');
        return null;
    }
    _aiEventAgentStatusCache.set(runId, status);
    const event = _aiEventsCache.find(e => e.id === eventId);
    const runs = event?.agent_runs || {};
    [event?.agent_run, runs.multi_agent, runs.agent_skill].filter(Boolean).forEach(run => {
        if (run.run_id === runId) run.status = status.status || run.status || 'running';
    });
    if (_activeAIEventDispositionId === String(eventId || '')) refreshOpenAIEventDisposition();
    renderAIEventsTab();
    return status;
}

async function runAIEventAgentAnalysis(eventId) {
    const event = aiEventById(eventId);
    if (!isAIEventActionable(event)) {
        showToast('该智算事件已恢复，不再触发新的分析动作', 'error');
        refreshOpenAIEventDisposition('analysis');
        return;
    }
    const actionKey = aiEventActionKey(eventId, 'multi_agent', 'analysis');
    if (_aiEventActionInFlight.has(actionKey)) return;
    _aiEventActionInFlight.add(actionKey);
    refreshOpenAIEventDisposition('analysis');
    let data;
    try {
        data = await api(`/api/ai-events/${encodeURIComponent(eventId)}/agent`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ mode: 'analysis', analysis_phases: aiAnalysisPhases('multi_agent') }),
            timeoutMs: 60000,
        });
    } finally {
        _aiEventActionInFlight.delete(actionKey);
        refreshOpenAIEventDisposition('analysis');
    }
    if (!data?.agent_run?.run_id) {
        showToast('多智能体分析触发失败', 'error');
        return;
    }
    const current = aiEventById(eventId);
    if (current) {
        current.agent_runs = current.agent_runs || {};
        current.agent_runs.multi_agent = data.agent_run;
    }
    startAIEventAgentPolling(eventId, data.agent_run.run_id, { requestedStages: data.agent_run.requested_stages || ['detect', 'diagnose'] });
    refreshOpenAIEventDisposition('analysis');
    renderAIEventsTab();
    showToast('多智能体分析已启动');
}

async function runAIEventAgentHealing(eventId) {
    const event = aiEventById(eventId);
    if (!isAIEventActionable(event)) {
        showToast('该智算事件已恢复，不再触发新的自愈动作', 'error');
        refreshOpenAIEventDisposition('healing');
        return;
    }
    const currentRun = aiEventRun(event, 'multi_agent');
    if (!currentRun?.run_id) {
        await runAIEventAgentAnalysis(eventId);
    }
    const latest = aiEventById(eventId) || event;
    const multiRun = aiEventRun(latest, 'multi_agent');
    const multiStatus = aiEventRunStatus(multiRun);
    const result = multiStatus?.result?.result || multiStatus?.result || {};
    const data = await api(`/api/ai-events/${encodeURIComponent(eventId)}/heal`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ diagnosis: result }),
        timeoutMs: 12000,
    });
    if (!data?.heal_result) {
        showToast('多智能体自愈方案生成失败', 'error');
        return;
    }
    const updated = aiEventById(eventId);
    if (updated) updated.heal_result = data.heal_result;
    refreshOpenAIEventDisposition('healing');
    renderAIEventsTab();
    showToast('多智能体自愈方案已生成');
}

async function runAIEventSkill(eventId, mode) {
    const event = aiEventById(eventId);
    if (!isAIEventActionable(event)) {
        showToast('该智算事件已恢复，不再触发新的 Agent+Skill 动作', 'error');
        refreshOpenAIEventDisposition(mode === 'heal' ? 'healing' : 'analysis');
        return;
    }
    const stages = mode === 'heal' ? ['detect', 'diagnose', 'plan', 'heal', 'verify'] : ['detect', 'diagnose'];
    const actionMode = mode === 'heal' ? 'heal' : 'diagnose';
    const actionKey = aiEventActionKey(eventId, 'agent_skill', actionMode);
    if (_aiEventActionInFlight.has(actionKey)) return;
    _aiEventActionInFlight.add(actionKey);
    refreshOpenAIEventDisposition(mode === 'heal' ? 'healing' : 'analysis');
    let data;
    try {
        data = await api(`/api/ai-events/${encodeURIComponent(eventId)}/skill`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ stages, analysis_phases: aiAnalysisPhases('agent_skill') }),
            timeoutMs: 60000,
        });
    } finally {
        _aiEventActionInFlight.delete(actionKey);
        refreshOpenAIEventDisposition(mode === 'heal' ? 'healing' : 'analysis');
    }
    if (!data) {
        showToast('智算事件 Skill 执行失败', 'error');
        return;
    }
    const runId = data.agent_run?.run_id || data.event?.agent_run?.run_id || '';
    const current = aiEventById(eventId);
    if (current && data.agent_run) {
        current.agent_runs = current.agent_runs || {};
        current.agent_runs.agent_skill = data.agent_run;
        current.agent_run = data.agent_run;
    }
    if (runId) {
        _aiEventAgentStatusCache.delete(runId);
        startAIEventAgentPolling(eventId, runId, { requestedStages: stages });
    }
    await loadAIEvents();
    refreshOpenAIEventDisposition(mode === 'heal' ? 'healing' : 'analysis');
    showToast(mode === 'heal' ? 'Agent+Skill 闭环已触发' : 'Agent+Skill 诊断已触发');
}

async function updateAIEventStatus(eventId, status) {
    const data = await api(`/api/ai-events/${encodeURIComponent(eventId)}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ status }),
    });
    if (!data) {
        showToast('事件状态更新失败', 'error');
        return;
    }
    await loadAIEvents();
    refreshOpenAIEventDisposition();
    showToast('事件状态已更新');
}

function switchAIDashboardTab(tab) {
    _aiDashboardTab = tab || 'topology';
    document.querySelectorAll('[data-ai-dashboard-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.aiDashboardTab === _aiDashboardTab);
    });
    document.querySelectorAll('.ai-dashboard-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `ai-dashboard-tab-${_aiDashboardTab}`);
    });
    if (_aiDashboardTab === 'metrics') {
        setTimeout(() => {
            resizeAIMetricCharts(_aiMetricTab);
        }, 30);
    }
}

function switchAIMetricTab(tab) {
    _aiMetricTab = tab || 'llm';
    document.querySelectorAll('[data-ai-metric-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.aiMetricTab === _aiMetricTab);
    });
    document.querySelectorAll('.ai-metric-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `ai-metric-tab-${_aiMetricTab}`);
    });
    setTimeout(() => resizeAIMetricCharts(_aiMetricTab), 30);
}

function resizeAIMetricCharts(group = '') {
    Object.values(AI_DASHBOARD_CHARTS)
        .filter(spec => !group || spec.group === group)
        .forEach(spec => state.charts[spec.canvas]?.resize?.());
}

function aiTraceValue(value, unit = '') {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
    const n = Number(value);
    const text = Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(3).replace(/\.?0+$/, '') || '0';
    return `${text}${unit}`;
}

function aiTraceRequestCoverageMap(trace = {}) {
    return new Map((trace?.e2e_trace?.requests || []).map(item => [String(item.request_id || ''), item]));
}

function aiTraceRequests(ctx = {}, trace = null) {
    const sourceTrace = trace || state.aiTraceLastTrace || {};
    const e2eById = aiTraceRequestCoverageMap(sourceTrace);
    const filter = state.aiTraceFilter || 'all';
    const query = String(state.aiTraceQuery || '').trim().toLowerCase();
    return [...(ctx.requests || [])]
        .filter(req => {
            const requestId = String(req.request_id || '');
            const coverage = e2eById.get(requestId)?.coverage || 'trace_only';
            if (filter !== 'all' && coverage !== filter) return false;
            if (query && !requestId.toLowerCase().includes(query)) return false;
            return true;
        })
        .sort((a, b) => Number(b.span_ms || 0) - Number(a.span_ms || 0));
}

function aiTraceSelectedRequest(ctx = {}, trace = null) {
    const requests = aiTraceRequests(ctx, trace);
    if (!requests.length) return null;
    const selected = state.aiTraceSelectedRequestId
        ? requests.find(req => String(req.request_id || '') === String(state.aiTraceSelectedRequestId))
        : null;
    return selected || requests[0];
}

function selectAIDashboardTraceRequest(requestId = '') {
    state.aiTraceSelectedRequestId = String(requestId || '');
    const trace = state.aiTraceLastTrace || {};
    renderAIDashboardRequestContext(trace.request_context || {});
    renderAIDashboardRequestDetail(trace.request_context || {}, trace);
}

function setAIDashboardTraceFilter(filter = 'all') {
    state.aiTraceFilter = filter || 'all';
    const trace = state.aiTraceLastTrace || {};
    renderAIDashboardRequestContext(trace.request_context || {});
    renderAIDashboardRequestDetail(trace.request_context || {}, trace);
}

function setAIDashboardTraceQuery(value = '') {
    state.aiTraceQuery = String(value || '');
    const trace = state.aiTraceLastTrace || {};
    renderAIDashboardRequestContext(trace.request_context || {});
    renderAIDashboardRequestDetail(trace.request_context || {}, trace);
}

function renderAIDashboardTraceTable(elId, rows, emptyText) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!rows?.length) {
        el.innerHTML = `<div class="ai-empty-note">${escapeHtml(emptyText)}</div>`;
        return;
    }
    el.innerHTML = `<table class="ai-table ai-trace-table">
        <thead><tr><th>名称</th><th>次数</th><th>总耗时</th><th>平均</th><th>P95</th><th>最大</th></tr></thead>
        <tbody>${rows.map(r => `
            <tr>
                <td><strong title="${escapeHtmlAttr(r.name || '-')}">${escapeHtml(r.name || '-')}</strong></td>
                <td>${escapeHtml(r.count ?? '-')}</td>
                <td>${escapeHtml(aiTraceValue(r.total_ms, ' ms'))}</td>
                <td>${escapeHtml(aiTraceValue(r.avg_ms, ' ms'))}</td>
                <td>${escapeHtml(aiTraceValue(r.p95_ms, ' ms'))}</td>
                <td>${escapeHtml(aiTraceValue(r.max_ms, ' ms'))}</td>
            </tr>
        `).join('')}</tbody>
    </table>`;
}

function renderAIDashboardEventTypeStats(elId, rows, emptyText) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!rows?.length) {
        el.innerHTML = `<div class="ai-empty-note">${escapeHtml(emptyText)}</div>`;
        return;
    }
    const total = rows.reduce((sum, row) => sum + Number(row.total_ms || 0), 0) || 1;
    el.innerHTML = `
        <div class="ai-trace-type-bars">
            ${rows.map(row => {
                const pct = Math.max(1, Math.min(100, (Number(row.total_ms || 0) / total) * 100));
                return `<div class="ai-trace-type-row">
                    <div class="ai-trace-type-head">
                        <strong>${escapeHtml(row.label || row.type || '-')}</strong>
                        <span>${escapeHtml(row.count ?? 0)} 次 · ${escapeHtml(aiTraceValue(row.total_ms, ' ms'))}</span>
                    </div>
                    <div class="ai-trace-type-bar"><i style="width:${pct}%"></i></div>
                    <small>P95 ${escapeHtml(aiTraceValue(row.p95_ms, ' ms'))} · 最大 ${escapeHtml(aiTraceValue(row.max_ms, ' ms'))}</small>
                </div>`;
            }).join('')}
        </div>`;
}

function renderAIDashboardKeyDurations(elId, rows, emptyText) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!rows?.length) {
        el.innerHTML = `<div class="ai-empty-note">${escapeHtml(emptyText)}</div>`;
        return;
    }
    el.innerHTML = `<table class="ai-table ai-trace-events">
        <thead><tr><th>事件</th><th>类型</th><th>开始</th><th>耗时</th><th>PID/TID</th></tr></thead>
        <tbody>${rows.slice(0, 12).map(e => `
            <tr>
                <td><strong title="${escapeHtmlAttr(e.name || '-')}">${escapeHtml(e.name || '-')}</strong></td>
                <td><span class="badge badge-info">${escapeHtml(traceLaneLabel(e.type || '-'))}</span></td>
                <td>${escapeHtml(aiTraceValue(e.start_ms, ' ms'))}</td>
                <td><strong>${escapeHtml(aiTraceValue(e.duration_ms, ' ms'))}</strong></td>
                <td>${escapeHtml(e.process_id ?? 0)} / ${escapeHtml(e.thread_id ?? 0)}</td>
            </tr>
        `).join('')}</tbody>
    </table>`;
}

function traceLaneLabel(type) {
    return {
        HTTP: 'HTTP 入站',
        STEP: 'TELLER Step',
        FRONTEND: 'PyTorch Frontend',
        BACKEND: 'PyTorch Backend',
        NVTX: 'NVTX / 阶段',
        ENGINE: 'vLLM 引擎',
        SCHEDULER: 'vLLM 调度',
        PREFILL: 'Prefill',
        DECODE: 'Decode',
        PYTORCH: 'PyTorch 前端',
        LIBTORCH: 'LibTorch / Aten',
        RUNTIME: 'CUDA Runtime',
        DRIVER: 'CUDA Driver',
        KERNEL: 'GPU Kernel',
    }[type] || type || '其他';
}

function traceEventColor(type, durationMs, maxDurationMs) {
    if (type === 'HTTP') return '#0284c7';
    if (type === 'STEP') return '#0f766e';
    if (type === 'FRONTEND') return '#0284c7';
    if (type === 'BACKEND') return '#4f46e5';
    if (type === 'NVTX') return '#2563eb';
    if (type === 'ENGINE') return '#0f766e';
    if (type === 'SCHEDULER') return '#059669';
    if (type === 'PREFILL') return '#22c55e';
    if (type === 'DECODE') return '#84cc16';
    if (type === 'PYTORCH') return '#0369a1';
    if (type === 'LIBTORCH') return '#4338ca';
    if (type === 'RUNTIME') return '#0891b2';
    if (type === 'DRIVER') return '#7c3aed';
    if (durationMs >= maxDurationMs * 0.5) return '#dc2626';
    if (durationMs >= maxDurationMs * 0.2) return '#f59e0b';
    return '#16a34a';
}

function renderAIDashboardTraceProfiler(trace) {
    const el = document.getElementById('ai-dashboard-trace-profiler');
    if (!el) return;
    const timeline = trace?.timeline || {};
    const events = timeline.events || [];
    if (!events.length) {
        el.innerHTML = '<div class="ai-empty-note">暂无可绘制的 profiler 时间轴。点击“采集一次”生成真实 CUDA/NVTX 事件。</div>';
        return;
    }
    const range = timeline.display_time_range_ns || timeline.time_range_ns || [];
    const eventStartNs = e => Number(e.display_start_ns ?? e.start ?? 0);
    const eventDurationNs = e => Number(e.display_duration_ns ?? e.duration_ns ?? 0);
    const minStart = Number(range[0] ?? Math.min(...events.map(eventStartNs)));
    const maxEnd = Number(range[1] ?? Math.max(...events.map(e => eventStartNs(e) + eventDurationNs(e))));
    const span = Math.max(1, maxEnd - minStart);
    const lanes = ['HTTP', 'STEP', 'ENGINE', 'SCHEDULER', 'PREFILL', 'DECODE', 'FRONTEND', 'PYTORCH', 'BACKEND', 'LIBTORCH', 'NVTX', 'RUNTIME', 'DRIVER', 'KERNEL']
        .map(type => ({ type, events: events.filter(e => e.type === type) }))
        .filter(lane => lane.events.length);
    const maxDurationMs = Math.max(...events.map(e => eventDurationNs(e) / 1000000), 0.001);
    const top = [...events]
        .sort((a, b) => eventDurationNs(b) - eventDurationNs(a))
        .slice(0, 5);
    const bottlenecks = trace?.bottlenecks || [];
    const ticks = [0, 0.25, 0.5, 0.75, 1].map(p => ({
        left: p * 100,
        label: aiTraceValue((span * p) / 1000000, ' ms'),
    }));
    el.innerHTML = `
        <div class="ai-trace-bottleneck">
            <strong>瓶颈候选</strong>
            ${(bottlenecks.length ? bottlenecks : top).slice(0, 5).map(e => {
                if (e.reason) return `<span title="${escapeHtmlAttr(e.name || '-')}">${escapeHtml(e.name || '-')} · ${escapeHtml(e.reason || '')} · ${escapeHtml(aiTraceValue(e.total_ms, ' ms'))}</span>`;
                return `<span title="${escapeHtmlAttr(e.name || '-')}">${escapeHtml(e.type || '-')} · ${escapeHtml(aiTraceValue((e.duration_ns || 0) / 1000000, ' ms'))} · ${escapeHtml(e.name || '-')}</span>`;
            }).join('')}
            ${timeline.truncated ? '<em>事件较多，已优先展示长耗时和早期事件。</em>' : ''}
            ${timeline.display_note ? `<em>${escapeHtml(timeline.display_note)}</em>` : ''}
        </div>
        <div class="ai-trace-ruler">
            ${ticks.map(t => `<i style="left:${t.left}%"><span>${escapeHtml(t.label)}</span></i>`).join('')}
        </div>
        <div class="ai-trace-lanes">
            ${lanes.map(lane => `
                <div class="ai-trace-lane">
                    <div class="ai-trace-lane-label">
                        <strong>${escapeHtml(traceLaneLabel(lane.type))}</strong>
                        <span>${lane.events.length} events</span>
                    </div>
                    <div class="ai-trace-lane-track">
                        ${lane.events.slice(0, 180).map(e => {
                            const startNs = eventStartNs(e);
                            const durationNs = eventDurationNs(e);
                            const startPct = Math.max(0, Math.min(100, ((startNs - minStart) / span) * 100));
                            const widthPct = Math.max(0.35, Math.min(100 - startPct, (durationNs / span) * 100));
                            const durationMs = durationNs / 1000000;
                            const startMs = (startNs - minStart) / 1000000;
                            const color = traceEventColor(lane.type, durationMs, maxDurationMs);
                            const title = `${e.type} | ${e.path || e.name || '-'} | start ${aiTraceValue(startMs, ' ms')} | duration ${aiTraceValue(durationMs, ' ms')} | ${e.source_file || ''} | pid/tid ${e.process_id || 0}/${e.thread_id || 0}`;
                            const label = widthPct >= 7 ? aiTraceValue(durationMs, ' ms') : '';
                            return `<span class="ai-trace-slice ${durationMs >= maxDurationMs * 0.2 ? 'hot' : ''}" style="left:${startPct}%;width:${widthPct}%;background:${color}" title="${escapeHtmlAttr(title)}">${escapeHtml(label)}</span>`;
                        }).join('')}
                    </div>
                    <div class="ai-trace-lane-events">
                        ${lane.events.slice(0, 8).map(e => {
                            const startMs = (eventStartNs(e) - minStart) / 1000000;
                            const durationMs = eventDurationNs(e) / 1000000;
                            const endMs = startMs + durationMs;
                            return `<span title="${escapeHtmlAttr(e.name || '-')}">
                                <b>${escapeHtml(e.path || e.name || '-')}</b>
                                <small>${escapeHtml(aiTraceValue(startMs, ' ms'))} - ${escapeHtml(aiTraceValue(endMs, ' ms'))} · ${escapeHtml(aiTraceValue(durationMs, ' ms'))}</small>
                            </span>`;
                        }).join('')}
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

function renderAIDashboardFineGrainedTrace(data) {
    const trace = data.fine_grained_trace || {};
    state.aiTraceLastTrace = trace;
    const summaryEl = document.getElementById('ai-dashboard-trace-summary');
    const filesEl = document.getElementById('ai-dashboard-trace-files');
    const eventsEl = document.getElementById('ai-dashboard-trace-events');
    const s = trace.summary || {};
    if (summaryEl) {
        const hierarchy = trace.hierarchy || {};
        summaryEl.innerHTML = `
            <div class="ai-trace-status ${trace.available ? 'ok' : trace.configured ? 'warn' : 'muted'}">
                <span class="badge ${trace.available ? 'badge-success' : trace.configured ? 'badge-warning' : 'badge-secondary'}">${trace.available ? '已接入' : trace.configured ? '路径不可用' : '未配置'}</span>
                <strong>${escapeHtml(trace.message || '-')}</strong>
                <small title="${escapeHtmlAttr(trace.trace_dir || '')}">${escapeHtml(trace.trace_dir || '可在配置中心为智算集群设置 teller_trace_dir')}</small>
            </div>
            <div class="ai-trace-metrics">
                <div><span>事件总数</span><strong>${escapeHtml(s.total_events ?? 0)}</strong></div>
                <div><span>Kernel</span><strong>${escapeHtml(s.kernel_events ?? 0)}</strong></div>
                <div><span>NVTX</span><strong>${escapeHtml(s.nvtx_events ?? 0)}</strong></div>
                <div><span>执行树 Step</span><strong>${escapeHtml(s.step_events ?? 0)}</strong></div>
                <div><span>CUDA API</span><strong>${escapeHtml((s.runtime_events ?? 0) + (s.driver_events ?? 0))}</strong></div>
                <div><span>Kernel 总耗时</span><strong>${escapeHtml(aiTraceValue(s.kernel_total_ms, ' ms'))}</strong></div>
                <div><span>Kernel P95</span><strong>${escapeHtml(aiTraceValue(s.kernel_p95_ms, ' ms'))}</strong></div>
            </div>
            <div class="ai-trace-hierarchy ${hierarchy.available ? 'ok' : 'warn'}">
                <strong>${escapeHtml(hierarchy.available ? 'TELLER 执行树已解析' : 'TELLER 执行树未接入')}</strong>
                <span>${escapeHtml(hierarchy.message || '')}</span>
                ${(hierarchy.roots || []).length ? `<div>${(hierarchy.roots || []).slice(0, 6).map(root => `<em title="${escapeHtmlAttr(root.path || root.name || '-')}">${escapeHtml(root.name || '-')} · ${escapeHtml(aiTraceValue(root.duration_ms, ' ms'))}</em>`).join('')}</div>` : ''}
            </div>
        `;
    }
    if (filesEl) {
        const files = trace.files || [];
        filesEl.innerHTML = files.length ? `<div class="ai-trace-file-list">${files.slice(0, 8).map(f => `
            <div class="ai-trace-file">
                <strong title="${escapeHtmlAttr(f.path || '')}">${escapeHtml(f.name || '-')}</strong>
                <span>${escapeHtml(aiTraceValue((f.size || 0) / 1024, ' KiB'))}</span>
            </div>
        `).join('')}</div>` : '<div class="ai-empty-note">没有发现 TELLER JSONL/JSON trace 文件。</div>';
    }
    renderAIDashboardRequestContext(trace.request_context || {});
    renderAIDashboardRequestDetail(trace.request_context || {}, trace);
    renderAIDashboardTellerPlan(data.teller_integration || {});
    renderAIDashboardTraceProfiler(trace);
    renderAIDashboardKeyDurations('ai-dashboard-key-durations', trace.key_durations || [], '暂无可统计的关键事件耗时。');
    renderAIDashboardEventTypeStats('ai-dashboard-event-type-stats', trace.event_type_stats || [], '暂无事件类型耗时分布。');
    renderAIDashboardTraceTable('ai-dashboard-top-kernels', trace.top_kernels || [], '暂无 CUDA kernel 事件。');
    renderAIDashboardTraceTable('ai-dashboard-top-nvtx', trace.top_nvtx || [], '暂无 NVTX range 事件。');
    renderAIDashboardTraceTable('ai-dashboard-top-runtime', trace.top_runtime || [], '暂无 CUDA Runtime / Driver 事件。');
    if (eventsEl) {
        const events = trace.recent_events || [];
        eventsEl.innerHTML = events.length ? `<table class="ai-table ai-trace-events">
            <thead><tr><th>类型</th><th>名称</th><th>耗时</th><th>PID/TID</th></tr></thead>
            <tbody>${events.slice(0, 40).map(e => `
                <tr>
                    <td><span class="badge badge-info">${escapeHtml(e.type || '-')}</span></td>
                    <td><strong title="${escapeHtmlAttr(e.name || '-')}">${escapeHtml(e.name || '-')}</strong></td>
                    <td>${escapeHtml(aiTraceValue((e.duration_ns || 0) / 1000000, ' ms'))}</td>
                    <td>${escapeHtml(e.process_id ?? 0)} / ${escapeHtml(e.thread_id ?? 0)}</td>
                </tr>
            `).join('')}</tbody>
        </table>` : '<div class="ai-empty-note">暂无可展示的细粒度事件。</div>';
    }
}

function aiTraceRequestDiagnosis(req = {}, trace = {}) {
    const summary = trace.summary || {};
    const spanMs = Number(req.span_ms || 0);
    const kernelP95 = Number(summary.kernel_p95_ms || 0);
    const runtimeTotal = Number(summary.runtime_total_ms || 0) + Number(summary.driver_total_ms || 0);
    const kernelTotal = Number(summary.kernel_total_ms || 0);
    const topRuntime = (req.top_events || []).find(ev => ['RUNTIME', 'DRIVER'].includes(ev.type));
    const topKernel = (req.top_events || []).find(ev => ev.type === 'KERNEL');
    if (!req || !req.request_id) {
        return { level: 'muted', title: '暂无请求级诊断', detail: '当前 trace 没有关联 request id，无法把事件精确归因到单次推理请求。' };
    }
    if (spanMs > 0 && kernelP95 > Math.max(10, spanMs * 0.2)) {
        return { level: 'danger', title: '疑似 GPU Kernel 瓶颈', detail: `Kernel P95 达到 ${aiTraceValue(kernelP95, ' ms')}，优先检查算子耗时、batch 大小、显存压力和 attention backend。` };
    }
    if (runtimeTotal > kernelTotal * 1.5 && topRuntime) {
        return { level: 'warn', title: '疑似 CUDA Runtime / Driver 开销偏高', detail: `${topRuntime.name || 'CUDA API'} 在请求路径中耗时靠前，优先检查同步、拷贝、launch overhead 或驱动侧阻塞。` };
    }
    if ((req.stages || []).some(stage => ['ENGINE', 'FRONTEND', 'PYTORCH'].includes(stage.type) && Number(stage.total_ms || 0) > spanMs * 0.35)) {
        return { level: 'warn', title: '疑似 vLLM 调度或框架前端瓶颈', detail: '请求耗时主要集中在引擎、PyTorch 前端或调度阶段，建议结合队列长度、running 请求数和 prefill/decode 阶段继续定位。' };
    }
    if (topKernel) {
        return { level: 'ok', title: '请求链路已关联 GPU 执行', detail: `已捕捉到请求级 Kernel 事件，最长 Kernel 为 ${topKernel.name || '-'}，耗时 ${aiTraceValue(topKernel.duration_ms, ' ms')}。` };
    }
    return { level: 'ok', title: '请求链路可观测', detail: '已捕捉请求上下文和细粒度事件，可继续结合故障注入前后同一指标窗口对比。' };
}

function renderAIDashboardRequestPathTimeline(events = []) {
    const rows = (events || []).slice(0, 18);
    if (!rows.length) return '<div class="ai-empty-note">该请求暂未解析到 critical_path。</div>';
    const starts = rows.map(ev => Number(ev.start_ms || 0));
    const ends = rows.map(ev => Number(ev.start_ms || 0) + Number(ev.duration_ms || 0));
    const minStart = Math.min(...starts);
    const span = Math.max(0.001, Math.max(...ends) - minStart);
    const maxDuration = Math.max(...rows.map(ev => Number(ev.duration_ms || 0)), 0.001);
    return `
        <div class="ai-request-path-timeline">
            ${rows.map(ev => {
                const start = Number(ev.start_ms || 0);
                const duration = Number(ev.duration_ms || 0);
                const left = Math.max(0, Math.min(96, ((start - minStart) / span) * 100));
                const width = Math.max(2, Math.min(100 - left, (duration / span) * 100));
                const hot = duration >= maxDuration * 0.4 ? 'hot' : '';
                return `<div class="ai-request-path-row ${hot}">
                    <div class="ai-request-path-meta">
                        <span>${escapeHtml(traceLaneLabel(ev.type || '-'))}</span>
                        <strong title="${escapeHtmlAttr(ev.path || ev.name || '-')}">${escapeHtml(ev.path || ev.name || '-')}</strong>
                    </div>
                    <div class="ai-request-path-track">
                        <i style="left:${left}%;width:${width}%"></i>
                    </div>
                    <small>${escapeHtml(aiTraceValue(duration, ' ms'))}</small>
                </div>`;
            }).join('')}
        </div>`;
}

function aiTraceE2ERequest(trace = {}, requestId = '') {
    const requests = trace?.e2e_trace?.requests || [];
    return requests.find(item => String(item.request_id || '') === String(requestId || '')) || null;
}

function renderAIDashboardE2ETrace(e2eReq = null, trace = {}) {
    if (!e2eReq) {
        return '<div class="ai-empty-note">暂无端到端标准 Trace。当前只能展示原始 TELLER 请求上下文。</div>';
    }
    const nodes = e2eReq.nodes || [];
    const gaps = e2eReq.gaps || [];
    return `
        <div class="ai-e2e-trace">
            <div class="ai-e2e-head">
                <div><span>覆盖度</span><strong>${escapeHtml(e2eReq.coverage || trace?.e2e_trace?.coverage || '-')}</strong></div>
                <div><span>已观测层</span><strong>${escapeHtml((e2eReq.observed_layers || []).join(' / ') || '-')}</strong></div>
                <div><span>缺失层</span><strong>${escapeHtml((e2eReq.missing_layers || []).join(' / ') || '-')}</strong></div>
            </div>
            <div class="ai-e2e-chain">
                ${nodes.map((node, idx) => `
                    <div class="ai-e2e-node ${node.status === 'observed' ? 'observed' : 'missing'}">
                        <span>${idx + 1}</span>
                        <strong>${escapeHtml(node.label || node.id || '-')}</strong>
                        <small>${escapeHtml(node.status === 'observed' ? aiTraceValue(node.duration_ms, ' ms') : '未接入')}</small>
                        <em title="${escapeHtmlAttr(node.evidence || '')}">${escapeHtml(node.evidence || '')}</em>
                    </div>
                `).join('')}
            </div>
            ${gaps.length ? `<div class="ai-e2e-gaps">
                ${gaps.map(gap => `<span>${escapeHtml(gap)}</span>`).join('')}
            </div>` : ''}
        </div>`;
}

function renderAIDashboardRequestDetail(ctx = {}, trace = {}) {
    const el = document.getElementById('ai-dashboard-request-detail');
    if (!el) return;
    const req = aiTraceSelectedRequest(ctx, trace);
    if (!req) {
        el.innerHTML = '<div class="ai-empty-note">暂无请求级 trace 详情。请确认 vLLM 通过 TELLER wrapper 启动，并产生真实推理请求。</div>';
        return;
    }
    state.aiTraceSelectedRequestId = String(req.request_id || '');
    const diagnosis = aiTraceRequestDiagnosis(req, trace);
    const stages = req.stages || [];
    const stageTotal = stages.reduce((sum, stage) => sum + Number(stage.total_ms || 0), 0) || 1;
    const processIds = (req.process_ids || []).join(',') || '-';
    const threadIds = (req.thread_ids || []).join(',') || '-';
    const e2eReq = aiTraceE2ERequest(trace, req.request_id || '');
    el.innerHTML = `
        <div class="ai-request-detail-head">
            <div>
                <span>Request ID</span>
                <strong title="${escapeHtmlAttr(req.request_id || '')}">${escapeHtml(req.request_id || '-')}</strong>
            </div>
            <div><span>端到端跨度</span><strong>${escapeHtml(aiTraceValue(req.span_ms, ' ms'))}</strong></div>
            <div><span>关联事件</span><strong>${escapeHtml(req.event_count ?? 0)}</strong></div>
            <div><span>进程 / 线程</span><strong title="PID ${escapeHtmlAttr(processIds)} / TID ${escapeHtmlAttr(threadIds)}">PID ${escapeHtml(processIds)}</strong></div>
        </div>
        <div class="ai-request-diagnosis ${diagnosis.level}">
            <strong>${escapeHtml(diagnosis.title)}</strong>
            <span>${escapeHtml(diagnosis.detail)}</span>
        </div>
        <div class="ai-request-detail-section">
            <h4>端到端链路视图</h4>
            ${renderAIDashboardE2ETrace(e2eReq, trace)}
        </div>
        <div class="ai-request-detail-grid">
            <div class="ai-request-detail-section">
                <h4>阶段耗时构成</h4>
                ${stages.length ? `<div class="ai-request-stage-detail">${stages.slice(0, 10).map(stage => {
                    const pct = Math.max(2, Math.min(100, (Number(stage.total_ms || 0) / stageTotal) * 100));
                    return `<div>
                        <label><strong>${escapeHtml(traceLaneLabel(stage.type || stage.label || '-'))}</strong><span>${escapeHtml(stage.count ?? 0)} 次 · ${escapeHtml(aiTraceValue(stage.total_ms, ' ms'))}</span></label>
                        <i><b style="width:${pct}%"></b></i>
                    </div>`;
                }).join('')}</div>` : '<div class="ai-empty-note">暂无阶段统计。</div>'}
            </div>
            <div class="ai-request-detail-section">
                <h4>Top 事件</h4>
                ${(req.top_events || []).length ? `<table class="ai-table ai-request-detail-table">
                    <thead><tr><th>类型</th><th>事件</th><th>耗时</th></tr></thead>
                    <tbody>${(req.top_events || []).slice(0, 8).map(ev => `
                        <tr>
                            <td><span class="badge badge-info">${escapeHtml(traceLaneLabel(ev.type || '-'))}</span></td>
                            <td><strong title="${escapeHtmlAttr(ev.name || '-')}">${escapeHtml(ev.name || '-')}</strong></td>
                            <td>${escapeHtml(aiTraceValue(ev.duration_ms, ' ms'))}</td>
                        </tr>
                    `).join('')}</tbody>
                </table>` : '<div class="ai-empty-note">暂无 Top 事件。</div>'}
            </div>
        </div>
        <div class="ai-request-detail-section">
            <h4>关键路径时间条</h4>
            ${renderAIDashboardRequestPathTimeline(req.critical_path || [])}
        </div>
        <details class="ai-request-raw">
            <summary>查看请求原始上下文</summary>
            <pre>${escapeHtml(JSON.stringify({
                request_id: req.request_id,
                span_ms: req.span_ms,
                event_count: req.event_count,
                process_ids: req.process_ids || [],
                thread_ids: req.thread_ids || [],
                stages: req.stages || [],
                critical_path: req.critical_path || [],
                top_events: req.top_events || [],
            }, null, 2))}</pre>
        </details>
    `;
}

function renderAIDashboardRequestContext(ctx = {}) {
    const el = document.getElementById('ai-dashboard-request-context');
    if (!el) return;
    const dashboardData = _lastAIDashboardData || state.aiDashboardData || {};
    const trace = dashboardData.fine_grained_trace || {};
    const requests = aiTraceRequests(ctx, trace);
    const selected = aiTraceSelectedRequest(ctx, trace);
    const e2eRequests = trace?.e2e_trace?.requests || [];
    const e2eById = aiTraceRequestCoverageMap(trace);
    const fullCount = Number(trace?.e2e_trace?.full_e2e_count ?? e2eRequests.filter(item => item.coverage === 'full_e2e').length);
    const coverageLabel = e2eRequests.length ? `${fullCount}/${e2eRequests.length} ${trace?.e2e_trace?.coverage || 'e2e'}` : (ctx.coverage || 'none');
    const health = trace.trace_health || {};
    const healthChecks = health.checks || [];
    el.innerHTML = `
        <div class="ai-trace-status ${ctx.available ? 'ok' : 'warn'}">
            <span class="badge ${ctx.available ? 'badge-success' : 'badge-warning'}">${ctx.available ? '请求级已接入' : '仅有事件级 Trace'}</span>
            <strong>${escapeHtml(ctx.message || '-')}</strong>
            <small>TELLER 需要通过 vLLM hook 写入 step_execute_model[request_ids:...]。</small>
        </div>
        ${healthChecks.length ? `<div class="ai-trace-health">
            ${healthChecks.map(check => `<span class="${check.ok ? 'ok' : 'warn'}" title="${escapeHtmlAttr(check.remediation || '')}">
                ${escapeHtml(check.label || '-')} · ${escapeHtml(check.count ?? 0)}
            </span>`).join('')}
        </div>` : ''}
        <div class="ai-trace-metrics">
            <div><span>请求数</span><strong>${escapeHtml(requests.length)} / ${escapeHtml(ctx.request_count ?? 0)}</strong></div>
            <div><span>端到端覆盖</span><strong>${escapeHtml(coverageLabel)}</strong></div>
            <div><span>Top 请求耗时</span><strong>${escapeHtml(requests[0] ? aiTraceValue(requests[0].span_ms, ' ms') : '-')}</strong></div>
            <div><span>Top 请求事件</span><strong>${escapeHtml(requests[0]?.event_count ?? 0)}</strong></div>
        </div>
        <div class="ai-trace-request-tools">
            <input class="input-sm" value="${escapeHtmlAttr(state.aiTraceQuery || '')}" placeholder="搜索 Request ID" oninput="setAIDashboardTraceQuery(this.value)">
            <select class="select-sm" onchange="setAIDashboardTraceFilter(this.value)">
                ${[
                    ['all', '全部请求'],
                    ['full_e2e', '只看 full_e2e'],
                    ['partial_e2e', '只看 partial'],
                    ['trace_only', '只看 trace_only'],
                ].map(([value, label]) => `<option value="${value}" ${state.aiTraceFilter === value ? 'selected' : ''}>${label}</option>`).join('')}
            </select>
        </div>
        ${requests.length ? `<div class="ai-request-list">${requests.slice(0, 8).map(req => {
            const e2eReq = e2eById.get(String(req.request_id || '')) || {};
            const coverage = e2eReq.coverage || 'trace_only';
            const coverageClass = coverage === 'full_e2e' ? 'badge-success' : 'badge-warning';
            return `
            <details class="ai-request-item ${String(req.request_id || '') === String(selected?.request_id || '') ? 'active' : ''}">
                <summary>
                    <strong title="${escapeHtmlAttr(req.request_id || '')}">${escapeHtml(req.request_id || '-')}</strong>
                    <span class="badge ${coverageClass}">${escapeHtml(coverage)}</span>
                    <span>${escapeHtml(aiTraceValue(req.span_ms, ' ms'))}</span>
                    <span>${escapeHtml(req.event_count || 0)} events</span>
                    <button class="btn btn-xs" type="button" onclick="event.preventDefault(); selectAIDashboardTraceRequest('${escapeHtmlAttr(req.request_id || '')}')">查看详情</button>
                </summary>
                <div class="ai-request-events">
                    ${(req.stages || []).length ? `<div class="ai-request-stage-strip">
                        ${(req.stages || []).slice(0, 6).map(stage => `
                            <span title="${escapeHtmlAttr(stage.label || stage.type || '-')}">
                                <b>${escapeHtml(traceLaneLabel(stage.type || stage.label || '-'))}</b>
                                <small>${escapeHtml(stage.count ?? 0)} 次 · ${escapeHtml(aiTraceValue(stage.total_ms, ' ms'))}</small>
                            </span>
                        `).join('')}
                    </div>` : ''}
                    ${(req.critical_path || []).length ? `<div class="ai-request-path">
                        <strong>请求执行路径</strong>
                        ${(req.critical_path || []).slice(0, 12).map(ev => `
                            <span style="--depth:${Math.min(Number(ev.depth || 0), 5)}" title="${escapeHtmlAttr(ev.path || ev.name || '-')}">
                                <i>${escapeHtml(traceLaneLabel(ev.type || '-'))}</i>
                                <b>${escapeHtml(ev.path || ev.name || '-')}</b>
                                <small>${escapeHtml(aiTraceValue(ev.start_ms, ' ms'))} · ${escapeHtml(aiTraceValue(ev.duration_ms, ' ms'))}</small>
                            </span>
                        `).join('')}
                    </div>` : ''}
                    ${(req.top_events || []).slice(0, 8).map(ev => `
                        <div>
                            <span class="badge badge-info">${escapeHtml(ev.type || '-')}</span>
                            <strong>${escapeHtml(ev.name || '-')}</strong>
                            <small>${escapeHtml(aiTraceValue(ev.duration_ms, ' ms'))}</small>
                        </div>
                    `).join('')}
                </div>
            </details>
        `; }).join('')}</div>` : '<div class="ai-empty-note">当前 trace 没有请求 ID。请先将真实 vLLM 服务用 TELLER wrapper 启动。</div>'}
    `;
}

function renderAIDashboardTellerPlan(plan = {}) {
    const el = document.getElementById('ai-dashboard-teller-plan');
    if (!el) return;
    if (!plan || !Object.keys(plan).length) {
        el.innerHTML = '<div class="ai-empty-note">暂无 TELLER 接入计划。</div>';
        return;
    }
    el.innerHTML = `
        <div class="ai-teller-plan-head">
            <span class="badge badge-info">${escapeHtml(plan.mode || 'plan')}</span>
            <strong>${escapeHtml(plan.namespace || '-')} / ${escapeHtml(plan.deployment || '-')}</strong>
            <small>${escapeHtml(plan.teller_code_path || '-')}</small>
        </div>
        ${renderAIDashboardTellerPreflight(plan.preflight || {})}
        <div class="ai-trace-metrics">
            <div><span>容器</span><strong>${escapeHtml(plan.container || '-')}</strong></div>
            <div><span>模型</span><strong>${escapeHtml(plan.model || '-')}</strong></div>
            <div><span>Trace Dir</span><strong title="${escapeHtmlAttr(plan.trace_dir || '')}">${escapeHtml(plan.trace_dir || '-')}</strong></div>
        </div>
        <details class="ai-teller-plan-fold">
            <summary>查看 vLLM TELLER 启动命令</summary>
            <pre>${escapeHtml(plan.wrapped_command || '')}</pre>
        </details>
        <details class="ai-teller-plan-fold">
            <summary>查看接入命令和验证命令</summary>
            <pre>${escapeHtml(JSON.stringify({ commands: plan.commands || [], verification: plan.verification || [], limitations: plan.limitations || [] }, null, 2))}</pre>
        </details>
    `;
}

function renderAIDashboardTellerPreflight(preflight = {}) {
    const checks = preflight.checks || [];
    if (!checks.length) return '<div class="ai-empty-note">尚未执行 TELLER 接入预检。</div>';
    const status = preflight.status || 'unknown';
    const badge = status === 'ready' ? 'badge-success' : status === 'partial' ? 'badge-warning' : 'badge-danger';
    return `
        <div class="ai-teller-preflight ${escapeHtmlAttr(status)}">
            <div class="ai-teller-preflight-head">
                <span class="badge ${badge}">${escapeHtml(status === 'ready' ? '可接入' : status === 'partial' ? '部分满足' : '暂不可接入')}</span>
                <strong>预检 ${escapeHtml(preflight.summary?.required_passed ?? 0)} / ${escapeHtml(preflight.summary?.required_total ?? checks.length)}</strong>
                <small>${escapeHtml(preflight.pod || '-')} · ${escapeHtml((preflight.nodes || []).join(', ') || '-')}</small>
            </div>
            <div class="ai-teller-checks">
                ${checks.map(c => `
                    <div class="ai-teller-check ${c.ok ? 'ok' : 'fail'}">
                        <span>${c.ok ? 'OK' : 'MISS'}</span>
                        <div>
                            <strong>${escapeHtml(c.label || c.id || '-')}</strong>
                            <small>${escapeHtml(c.ok ? (c.stdout || '通过') : (c.remediation || c.stderr || '未通过'))}</small>
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

async function loadAIDashboardTellerPlan() {
    await loadFaultTargets();
    const clusterId = document.getElementById('ai-dashboard-cluster')?.value || '';
    const status = document.getElementById('ai-dashboard-trace-collect-status');
    if (!clusterId) {
        if (status) status.innerHTML = '<span class="badge badge-warning">未配置</span> 请先选择智算集群。';
        return;
    }
    const plan = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/teller/integration-plan`);
    renderAIDashboardTellerPlan(plan || {});
    if (status) {
        const preflight = plan?.preflight || {};
        const ok = preflight.status === 'ready';
        status.innerHTML = `<span class="badge ${ok ? 'badge-success' : 'badge-warning'}">${escapeHtml(ok ? '预检通过' : '需要补齐条件')}</span> 已生成真实 TELLER 请求级采集接入方案，未执行集群变更。`;
    }
}

async function collectAIDashboardTellerTrace() {
    await loadFaultTargets();
    const clusterId = document.getElementById('ai-dashboard-cluster')?.value || '';
    const status = document.getElementById('ai-dashboard-trace-collect-status');
    if (!clusterId) {
        if (status) status.innerHTML = '<span class="badge badge-warning">未配置</span> 请先选择智算集群。';
        return;
    }
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    const ok = window.confirm('将启动一个短时 GPU profiler 探针 Job。注意：这不是请求级 TELLER wrapper，只用于验证 GPU/NVTX/CUPTI 解析链路。是否继续？');
    if (!ok) return;
    if (status) status.innerHTML = '<span class="badge badge-info">采集中</span> 正在启动 TELLER/CUPTI 采集 Job...';
    const body = {
        confirm: true,
        namespace: cluster.default_namespace || 'llm-k8s',
        teller_trace_dir: cluster.teller_trace_dir || '',
    };
    const res = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/teller/collect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        timeoutMs: 240000,
    });
    if (!res) {
        if (status) status.innerHTML = '<span class="badge badge-danger">失败</span> 采集请求失败，请检查 K8s/GPU 镜像和事件日志。';
        return;
    }
    if (status) {
        const trace = res.fine_grained_trace || {};
        const summary = trace.summary || {};
        status.innerHTML = `<span class="badge ${res.status === 'completed' ? 'badge-success' : 'badge-warning'}">${escapeHtml(res.status || '-')}</span>
            run=${escapeHtml(res.run_id || '-')} · node=${escapeHtml(res.node || '-')} · Kernel=${escapeHtml(summary.kernel_events ?? 0)} · NVTX=${escapeHtml(summary.nvtx_events ?? 0)}
            <small>${escapeHtml(res.error || trace.message || '')}</small>`;
    }
    await loadAIDashboard();
}

function aiServedModelRecords(data) {
    const topo = data.topology || {};
    const pods = topo.pods || [];
    const records = [];
    const seen = new Set();
    const add = (name, fields = {}) => {
        const modelName = String(name || '').trim();
        if (!modelName) return;
        const key = modelName.toLowerCase();
        if (seen.has(key)) {
            const existing = records.find(r => String(r.name || '').toLowerCase() === key);
            if (existing) Object.entries(fields).forEach(([k, v]) => {
                if (v !== undefined && v !== null && v !== '' && (existing[k] === undefined || existing[k] === null || existing[k] === '')) existing[k] = v;
            });
            return;
        }
        seen.add(key);
        records.push({ name: modelName, ...fields });
    };
    (topo.models || []).forEach(m => add(m.name || m.model, m));
    (topo.vllm_series || []).forEach(s => add(s.model, { source: 'prometheus', instance: s.instance, running: s.running }));
    pods.forEach(p => (p.served_models || (p.served_model_name ? [p.served_model_name] : [])).forEach(name => add(name, {
        source: 'k8s_pod',
        namespace: p.namespace,
        pod: p.name,
        app: p.app || p.labels?.app || '',
        owner: p.owner || '',
        node: p.node,
        phase: p.phase,
    })));
    add(data.model || data.cluster?.model, { source: 'cluster_config', endpoint: data.vllm_endpoint || data.cluster?.vllm_endpoint });
    return records;
}

function isAIServingPod(pod = {}) {
    const name = String(pod.name || '').toLowerCase();
    const app = String(pod.app || pod.labels?.app || '').toLowerCase();
    const servedModels = pod.served_models || (pod.served_model_name ? [pod.served_model_name] : []);
    return servedModels.length > 0
        || ((name.includes('vllm') || app.includes('vllm')) && !name.includes('prometheus') && !app.includes('prometheus'));
}

function aiModelNodeId(name = '') {
    return `model:${String(name || 'model').replace(/[^A-Za-z0-9_.-]/g, '_')}`;
}

function renderAIDashboardTopology(data) {
    const el = document.getElementById('ai-dashboard-topology');
    if (!el) return;
    const topo = data.topology || {};
    const nodes = topo.nodes || [];
    const pods = (topo.pods || []).filter(p => !['Succeeded', 'Failed'].includes(p.phase));
    const services = topo.services || [];
    const targets = topo.targets || [];
    const gpus = topo.gpu_devices || [];
    const modelRecords = aiServedModelRecords(data);
    const nodeNames = nodes.length ? nodes.map(n => n.name) : Array.from(new Set([
        ...gpus.map(g => g.instance || g.Hostname).filter(Boolean),
        ...pods.map(p => p.node).filter(Boolean),
        ...(data.hosts || []).map(h => h.name || h.host).filter(Boolean),
    ]));
    const nodeCards = nodeNames.map(name => {
        const node = nodes.find(n => n.name === name) || {};
        const nodeGpus = gpus.filter(g => (g.instance || g.Hostname) === name);
        const nodePods = pods.filter(p => p.node === name);
        const nodeTargets = targets.filter(t => t.instance === name || (t.labels || {}).instance === name);
        const vllmPods = nodePods.filter(isAIServingPod);
        const vllm = vllmPods[0];
        const exporter = nodePods.find(p => (p.name || '').includes('gpu-metrics') || (p.name || '').includes('dcgm'));
        const prom = nodePods.find(p => (p.name || '').includes('prometheus'));
        return { name, node, nodeGpus, nodePods, nodeTargets, vllm, vllmPods, exporter, prom };
    });
    const vllmPods = pods.filter(isAIServingPod);
    const exporterPods = pods.filter(p => (p.name || '').includes('gpu-metrics') || (p.name || '').includes('dcgm') || p.app === 'gpu-metrics-exporter');
    const prometheusPods = pods.filter(p => (p.name || '').includes('prometheus') || p.app === 'vllm-prometheus');
    const matchedBySelector = svc => pods.filter(p => {
        const selector = svc.selector || {};
        const labels = p.labels || {};
        return Object.keys(selector).length && Object.entries(selector).every(([k, v]) => labels[k] === v);
    });
    const corePods = [...new Map(
        [...vllmPods, ...prometheusPods, ...exporterPods].map(pod => [pod.name, pod])
    ).values()];
    const readyCorePods = corePods.filter(p => p.phase === 'Running' && p.containers > 0 && p.ready === p.containers);
    const healthyTargets = targets.filter(t => t.health === 'up').length;
    const servingServices = services.filter(service => matchedBySelector(service).some(isAIServingPod));
    const displayServices = [...new Map(
        [...servingServices, ...services.filter(service => !servingServices.includes(service))]
            .map(service => [service.name, service])
    ).values()].slice(0, 4);
    const graphNodes = [];
    const graphEdges = [];
    const addGraphNode = (id, kind, name, meta, x, y, status = '', payload = {}) => {
        graphNodes.push({ id, kind, name, meta, x, y, status, payload });
    };
    const addGraphEdge = (from, to, label = '') => {
        if (graphNodes.some(n => n.id === from) && graphNodes.some(n => n.id === to)) {
            graphEdges.push({ from, to, label });
        }
    };
    addGraphNode('cluster', 'cluster', data.cluster?.name || data.cluster?.id || 'Cluster', `${nodes.length} nodes · ${data.cluster?.default_namespace || '-'}`, 44, 70, '', { cluster: data.cluster });
    addGraphNode('prometheus', 'prometheus', 'Prometheus', data.prometheus_url || '-', 44, 250, data.storage?.available ? 'available' : 'unavailable', { prometheus_url: data.prometheus_url });
    displayServices.forEach((s, i) => addGraphNode(`svc:${s.name}`, 'service', s.name, `${s.type || '-'} · ${(s.ports || []).join(', ') || '-'}`, 300, 70 + i * 110, '', { service: s }));
    const corePodRows = [...prometheusPods, ...vllmPods, ...exporterPods].slice(0, 7);
    targets.slice(0, 5).forEach((t, i) => addGraphNode(`target:${t.job}:${t.instance}`, 'target', t.job || 'target', t.instance || '-', 300, 360 + i * 86, t.health || '', { target: t }));
    corePodRows.forEach((p, i) => addGraphNode(`pod:${p.name}`, 'pod', p.name, p.node || '-', 560, 70 + i * 92, p.phase || '', { pod: p }));
    modelRecords.slice(0, 6).forEach((m, i) => addGraphNode(aiModelNodeId(m.name), 'model', m.name, `${m.pod || m.instance || m.endpoint || '-'}${m.running !== undefined && m.running !== null ? ` · running ${m.running}` : ''}`, 790, 70 + i * 96, m.phase || '', { model: m }));
    nodeCards.slice(0, 4).forEach((card, i) => addGraphNode(`node:${card.name}`, 'node', card.name, `${card.node.internal_ip || '-'} · ${card.node.roles || '-'}`, 1020, 80 + i * 145, card.node.ready === false ? 'NotReady' : 'Ready', { node: card.node, node_name: card.name }));
    nodeCards.flatMap(card => card.nodeGpus.slice(0, 4).map(g => ({ card, g }))).slice(0, 12).forEach((item, i) => {
        addGraphNode(
            `gpu:${item.card.name}:${item.g.gpu}`,
            'gpu',
            `${item.card.name} / GPU${item.g.gpu || '-'}`,
            `利用率 ${aiPct(item.g.utilization)} · 显存 ${aiPct(item.g.memory_used_pct)}`,
            1240,
            52 + i * 70,
            `${llmMetricFormatNumber(item.g.temperature)}℃`,
            { node_name: item.card.name, gpu: item.g }
        );
    });
    displayServices.forEach(s => {
        addGraphEdge('cluster', `svc:${s.name}`, '暴露');
        matchedBySelector(s).slice(0, 4).forEach(p => addGraphEdge(`svc:${s.name}`, `pod:${p.name}`, 'selector'));
    });
    targets.slice(0, 5).forEach(t => addGraphEdge('prometheus', `target:${t.job}:${t.instance}`, 'scrape'));
    corePodRows.forEach(p => addGraphEdge(`pod:${p.name}`, `node:${p.node}`, '调度'));
    modelRecords.slice(0, 6).forEach(m => {
        const modelId = aiModelNodeId(m.name);
        const matchedPod = corePodRows.find(p => p.name === m.pod || (p.served_models || []).includes(m.name) || p.served_model_name === m.name)
            || vllmPods[0];
        if (matchedPod) addGraphEdge(`pod:${matchedPod.name}`, modelId, 'serve');
        if (m.node) addGraphEdge(modelId, `node:${m.node}`, '运行于');
        const matchedTarget = targets.find(t => (m.instance && (t.instance === m.instance || (t.labels || {}).instance === m.instance)) || String(t.job || '').toLowerCase().includes('vllm'));
        if (matchedTarget) addGraphEdge(modelId, `target:${matchedTarget.job}:${matchedTarget.instance}`, 'metrics');
        if (!matchedPod && !m.node) addGraphEdge('cluster', modelId, '模型服务');
    });
    nodeCards.forEach(card => card.nodeGpus.slice(0, 4).forEach(g => addGraphEdge(`node:${card.name}`, `gpu:${card.name}:${g.gpu}`, '挂载')));
    const graphNodeById = Object.fromEntries(graphNodes.map(n => [n.id, n]));
    const safeTopoSvgId = text => String(text || '').replace(/[^A-Za-z0-9_-]/g, '_');
    const edgePath = e => {
        const a = graphNodeById[e.from];
        const b = graphNodeById[e.to];
        const x1 = a.x + 176;
        const y1 = a.y + 34;
        const x2 = b.x;
        const y2 = b.y + 34;
        const mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    };
    const graphEdgePaths = graphEdges.map(e => ({
        ...e,
        pathId: `ai-edge-label-${safeTopoSvgId(`${e.from}-${e.to}-${e.label || ''}`)}`,
        d: edgePath(e),
    }));
    const graphWidth = Math.max(900, Math.ceil(Math.max(...graphNodes.map(n => Number(n.x || 0) + 210), 0) + 28));
    const graphHeight = Math.max(360, Math.ceil(Math.max(...graphNodes.map(n => Number(n.y || 0) + 95), 0) + 28));
    const graphSvg = `
        <div class="ai-topology-canvas">
            <svg viewBox="0 0 ${graphWidth} ${graphHeight}" role="img" aria-label="智算集群依赖拓扑图">
                <defs>
                    <marker id="aiArrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L0,6 L8,3 z"></path>
                    </marker>
                </defs>
                ${graphEdgePaths.map(e => `<path id="${e.pathId}" class="ai-topology-edge-label-path" d="${e.d}"></path>`).join('')}
                ${graphEdgePaths.map(e => `
                    <path class="ai-topology-edge" d="${e.d}"></path>
                    <text class="ai-topology-edge-label"><textPath href="#${e.pathId}" startOffset="48%">${escapeHtml(e.label || '')}</textPath></text>
                `).join('')}
                ${graphEdgePaths.map(e => `<path class="ai-topology-edge-hit" d="${e.d}"></path>`).join('')}
                ${graphNodes.map(n => `
                    <g class="ai-topology-node ${escapeHtml(n.kind)}" transform="translate(${n.x}, ${n.y})">
                        <rect width="176" height="68" rx="5"></rect>
                        <text x="12" y="18" class="kind">${escapeHtml(n.kind)}</text>
                        <text x="12" y="39" class="name">${escapeHtml(n.name || '-')}</text>
                        <text x="12" y="57" class="meta">${escapeHtml(n.meta || '')}</text>
                        ${n.status ? `<text x="164" y="18" class="status" text-anchor="end">${escapeHtml(n.status)}</text>` : ''}
                    </g>
                `).join('')}
            </svg>
        </div>
    `;
    el.innerHTML = `
        <div class="ai-topology-head">
            <div>
                <span>Cluster</span>
                <strong>${escapeHtml(data.cluster?.name || data.cluster?.id || '-')}</strong>
                <small title="${escapeHtml(data.prometheus_url || '-')}">${escapeHtml(data.cluster?.default_namespace || '-')} · ${escapeHtml(data.prometheus_url || '-')}</small>
            </div>
            <div class="ai-topology-health">
                <span class="badge ${!targets.length ? 'badge-secondary' : healthyTargets === targets.length ? 'badge-success' : 'badge-warning'}">${healthyTargets}/${targets.length} 采集正常</span>
                <span class="badge badge-info">${gpus.length} GPU 已观测</span>
                <span class="badge badge-purple">${modelRecords.length} 模型服务</span>
                <span class="badge badge-primary">${readyCorePods.length}/${corePods.length} 当前核心 Pod 就绪</span>
            </div>
        </div>
        <p class="text-muted">实时拓扑展示未结束的 Pod，包含 Pending 和未就绪实例；已结束的历史 Pod 不计入当前状态。</p>
        ${graphSvg}
        <div class="ai-node-lanes">
            ${nodeCards.map(card => `
                <div class="ai-node-lane ${card.node.ready === false ? 'not-ready' : ''}">
                    <div class="ai-node-lane-head">
                        <div>
                            <strong>${escapeHtml(card.name || '-')}</strong>
                            <span>${escapeHtml(card.node.internal_ip || '')} ${card.node.roles ? ' · ' + escapeHtml(card.node.roles) : ''}</span>
                        </div>
                        <span class="badge ${card.node.ready === false ? 'badge-danger' : 'badge-success'}">${card.node.ready === false ? 'NotReady' : 'Ready'}</span>
                    </div>
                    <div class="ai-node-lane-body">
                        <div class="ai-lane-section">
                            <span>GPU</span>
                            <div>${card.nodeGpus.length ? card.nodeGpus.map(g => `
                                <div class="ai-gpu-mini">
                                    <b>GPU${escapeHtml(g.gpu || '-')}</b>
                                    <small>利用率 ${escapeHtml(aiPct(g.utilization))}</small>
                                    <small>显存 ${escapeHtml(aiPct(g.memory_used_pct))} · 温度 ${escapeHtml(llmMetricFormatNumber(g.temperature))}℃</small>
                                </div>
                            `).join('') : '<em>无 GPU 指标</em>'}</div>
                        </div>
                        <div class="ai-lane-section">
                            <span>Workloads</span>
                            <div>
                                ${card.vllmPods.length ? `<b>vLLM (${escapeHtml(card.vllmPods.length)})</b><small>${escapeHtml(card.vllmPods.map(p => `${p.app || p.name}: ${p.phase || '-'}`).join(' · '))}</small>` : ''}
                                ${card.prom ? `<b>Prometheus</b><small>${escapeHtml(card.prom.phase || '')}</small>` : ''}
                                ${card.exporter ? `<b>GPU Exporter</b><small>${escapeHtml(card.exporter.phase || '')}</small>` : ''}
                                ${!card.vllm && !card.prom && !card.exporter ? '<em>无核心工作负载</em>' : ''}
                            </div>
                        </div>
                        <div class="ai-lane-section">
                            <span>Targets</span>
                            <div>${card.nodeTargets.length ? card.nodeTargets.map(t => `<b>${escapeHtml(t.job || '-')}</b><small>${escapeHtml(t.health || '')}</small>`).join('') : '<em>无采集目标</em>'}</div>
                        </div>
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

function buildLLMFaultTargetFromTopology(source, item, data, nodeMap = {}) {
    const cluster = data.cluster || {};
    const target = {
        source,
        kind: item.kind || 'edge',
        name: item.name || item.label || item.id || '',
        cluster_id: cluster.id || '',
        namespace: cluster.default_namespace || 'default',
        deployment: 'vllm-server',
        scenario_id: '',
        scenario_hint: '',
        target_mode: 'k8s',
        reason: '',
    };
    const payload = item.payload || {};
    if (item.kind === 'service' && payload.service) {
        target.namespace = payload.service.namespace || target.namespace;
        target.service = payload.service.name || item.name;
        target.scenario_hint = 'bandwidth_throttle';
        target.reason = 'Service 链路适合注入带宽限制或丢包，观察 token streaming 与请求时延变化。';
    } else if (item.kind === 'pod' && payload.pod) {
        target.namespace = payload.pod.namespace || target.namespace;
        target.pod = payload.pod.name || item.name;
        target.node = payload.pod.node || '';
        target.deployment = (payload.pod.owner || '').replace(/^ReplicaSet\//, '').replace(/-[a-f0-9]{8,10}$/, '') || target.deployment;
        target.scenario_hint = (item.name || '').includes('vllm') ? 'continuous_batch_disruption' : 'request_queue_overflow';
        target.reason = 'Pod 代表推理工作负载，适合注入批处理退化或请求队列积压类故障。';
    } else if (item.kind === 'container' && payload.container) {
        target.namespace = payload.pod?.namespace || target.namespace;
        target.pod = payload.pod?.name || '';
        target.container = payload.container.name || item.name;
        target.node = payload.pod?.node || '';
        target.deployment = (payload.pod?.owner || '').replace(/^ReplicaSet\//, '').replace(/-[a-f0-9]{8,10}$/, '') || target.deployment;
        target.scenario_hint = (payload.pod?.name || item.name || '').includes('vllm') ? 'process_crash' : 'request_queue_overflow';
        target.reason = 'Container 是业务运行单元，适合注入进程崩溃、资源耗尽或容器级异常，观察 Pod 和服务指标传播。';
    } else if (item.kind === 'model' && payload.model) {
        target.namespace = payload.model.namespace || target.namespace;
        target.pod = payload.model.pod || '';
        target.node = payload.model.node || '';
        target.model = payload.model.name || item.name;
        target.scenario_hint = 'request_queue_overflow';
        target.reason = '模型服务代表 vLLM/OpenAI API 暴露的推理业务入口，适合注入背景流量、队列积压和业务侧延迟，观察 TTFT、TPOT、E2E 和 GPU 利用率变化。';
    } else if (item.kind === 'node' && payload.node) {
        target.node = payload.node_name || payload.node.name || item.name;
        target.scenario_hint = 'disk_pressure';
        target.reason = '节点级目标适合注入磁盘压力或 OS 资源类故障，观察 Pod 和 vLLM 指标传播。';
    } else if (item.kind === 'gpu' && payload.gpu) {
        target.node = payload.node_name || payload.gpu.instance || '';
        target.gpu = payload.gpu.gpu || '';
        target.scenario_hint = 'gpu_clock_throttle';
        target.reason = 'GPU 卡级目标适合注入 GPU 降频或 CUDA 上下文类故障，观察单卡利用率和推理吞吐。';
    } else if (item.kind === 'target') {
        target.scenario_hint = 'request_queue_overflow';
        target.reason = 'Prometheus target 代表观测链路，适合用队列积压或端点类场景验证指标反馈。';
    } else if (source === 'edge') {
        const from = nodeMap[item.from] || {};
        const to = nodeMap[item.to] || {};
        target.kind = 'edge';
        target.name = `${from.name || item.from} → ${to.name || item.to}`;
        target.from_kind = from.kind || '';
        target.to_kind = to.kind || '';
        target.edge_label = item.label || '';
        target.scenario_hint = item.label === '调度' || item.label === '挂载' ? 'pcie_throttle' : 'packet_loss';
        target.reason = '依赖边适合注入链路/依赖传播类故障，用于验证故障如何从依赖关系传播到业务指标。';
    } else {
        target.scenario_hint = 'request_queue_overflow';
        target.reason = '集群级入口默认推荐业务侧请求队列积压，用于观察端到端 SLA 变化。';
    }
    return target;
}

function llmScenarioMatchesTarget(scenario = {}, target = {}) {
    const kind = String(target?.kind || 'cluster').toLowerCase();
    const source = String(target?.source || '').toLowerCase();
    const faultType = String(scenario.fault_type || '').toLowerCase();
    const layer = String(scenario.layer || '').toLowerCase();
    const applicable = Array.isArray(scenario.applicable_targets) ? scenario.applicable_targets.map(x => String(x).toLowerCase()) : [];
    if (applicable.length && (applicable.includes(kind) || (source === 'edge' && applicable.includes('edge')))) return true;
    if (kind === 'service') return layer === 'network' || ['endpoint_timeout', 'http_503', 'packet_loss', 'bandwidth_throttle', 'grpc_failure', 'request_queue_overflow'].includes(faultType);
    if (kind === 'pod' || kind === 'container') return ['software', 'os', 'driver'].includes(layer) || ['process_crash', 'process_hang', 'gpu_oom', 'cpu_stress', 'memory_pressure'].includes(faultType);
    if (kind === 'model') return ['software', 'network'].includes(layer) || ['model_load_failure', 'tokenizer_error', 'weight_corruption', 'model_permission_error', 'request_queue_overflow'].includes(faultType);
    if (kind === 'node') return ['os', 'driver'].includes(layer) || ['disk_pressure', 'fd_exhaustion', 'cpu_stress', 'memory_pressure', 'pcie_throttle'].includes(faultType);
    if (kind === 'gpu') return layer === 'driver' || ['gpu_oom', 'cuda_error', 'cuda_ctx_corruption', 'gpu_clock_throttle', 'state_contamination', 'cupti_trace_overhead'].includes(faultType);
    if (kind === 'target') return ['endpoint_timeout', 'request_queue_overflow', 'http_503'].includes(faultType);
    if (kind === 'edge' || source === 'edge') return layer === 'network' || ['packet_loss', 'bandwidth_throttle', 'endpoint_timeout', 'pcie_throttle', 'rdma_failure', 'tp_communication_loss', 'nccl_failure'].includes(faultType);
    return true;
}

function llmScenarioApplicabilityReason(scenario = {}, target = {}) {
    const kind = String(target?.kind || '').toLowerCase();
    const layer = String(scenario.layer || '').toLowerCase();
    if (!llmScenarioMatchesTarget(scenario, target)) {
        return `该场景主要作用于 ${llmLayerLabel(layer) || layer || '未知层'}，不适合当前 ${llmFaultTopologyKindLabel(kind)} 目标`;
    }
    if (kind === 'service' || kind === 'edge') return '匹配服务/依赖链路，可观察请求时延、连接错误和故障传播';
    if (kind === 'pod' || kind === 'container') return '匹配运行时工作负载，可观察 Pod 状态、重启、服务可用性和 vLLM 指标';
    if (kind === 'model') return '匹配模型服务入口，可观察 TTFT、TPOT、E2E、吞吐和模型资产异常';
    if (kind === 'node') return '匹配节点资源，可观察 CPU、内存、磁盘、网络及其对 Pod 的传播';
    if (kind === 'gpu') return '匹配 GPU/驱动资源，可观察单卡利用率、显存、CUDA 和吞吐变化';
    if (kind === 'target') return '匹配观测目标，可验证采集链路和业务指标反馈';
    return `匹配 ${llmLayerLabel(layer) || layer || '智算'} 场景`;
}

function llmFaultScenariosForTarget(target) {
    const applicable = _llmFaultScenarios
        .filter(s => llmScenarioMatchesTarget(s, target))
        .map(s => ({ ...s, applicability_reason: llmScenarioApplicabilityReason(s, target) }));
    const hint = String(target?.scenario_hint || '').toLowerCase();
    applicable.sort((a, b) => {
        const ah = String(a.fault_type || '').toLowerCase() === hint || String(a.id || '').toLowerCase().includes(hint);
        const bh = String(b.fault_type || '').toLowerCase() === hint || String(b.id || '').toLowerCase().includes(hint);
        if (ah !== bh) return ah ? -1 : 1;
        const sev = { critical: 0, warning: 1, info: 2 };
        return (sev[a.severity] ?? 3) - (sev[b.severity] ?? 3) || String(a.id || '').localeCompare(String(b.id || ''));
    });
    return applicable;
}

function scenarioForLLMFaultTarget(target) {
    if (!_llmFaultScenarios.length) return null;
    const candidates = llmFaultScenariosForTarget(target);
    if (!candidates.length) return null;
    const hint = String(target?.scenario_hint || '').toLowerCase();
    return candidates.find(s => String(s.fault_type || '').toLowerCase() === hint)
        || candidates.find(s => String(s.id || '').toLowerCase().includes(hint))
        || candidates[0];
}

async function openLLMFaultTargetPicker(key) {
    const target = _llmTopologyFaultTargets[key];
    if (!target) return;
    _selectedLLMTopologyFaultTargetKey = key;
    if (!_llmFaultScenarios.length) await ensureLLMFaultScenariosLoaded();
    renderLLMFaultTargetPicker(target);
}

function renderLLMFaultTargetPicker(target) {
    const el = document.getElementById('llmfaultlab-topology');
    if (!el) return;
    const existing = document.getElementById('llm-fault-target-picker');
    if (existing) existing.remove();
    const scenarios = llmFaultScenariosForTarget(target);
    const scenario = scenarioForLLMFaultTarget(target);
    const blockedCount = Math.max(0, _llmFaultScenarios.length - scenarios.length);
    const scenarioOptions = scenarios.length
        ? scenarios.map(s => `<option value="${escapeHtmlAttr(s.id)}" title="${escapeHtmlAttr(s.applicability_reason || '')}" ${scenario?.id === s.id ? 'selected' : ''}>${escapeHtml(s.id)} — ${escapeHtml(llmFaultTypeLabel(s.fault_type) || s.name || '')}</option>`).join('')
        : '<option value="">当前目标暂无适用场景</option>';
    const panel = document.createElement('div');
    panel.id = 'llm-fault-target-picker';
    panel.className = 'llm-fault-target-picker';
    panel.innerHTML = `
        <div class="llm-fault-target-head">
            <div>
                <span class="badge badge-info">${escapeHtml(llmFaultTopologyKindLabel(target.kind))}</span>
                <strong><span class="llm-fault-target-icon">${escapeHtml(llmFaultTopologyIcon(target.kind))}</span>${escapeHtml(target.name || '-')}</strong>
                <small>${escapeHtml(target.reason || '点击进入 GPU Lab 后仍需 Dry-run / 预检 / 确认真实执行。')}</small>
            </div>
            <button class="btn btn-sm" onclick="document.getElementById('llm-fault-target-picker')?.remove()">关闭</button>
        </div>
        <div class="llm-fault-target-grid">
            <div><span>目标类型</span><strong>${escapeHtml(target.kind || '-')}</strong></div>
            <div><span>集群</span><strong>${escapeHtml(target.cluster_id || '-')}</strong></div>
            <div><span>Namespace</span><strong>${escapeHtml(target.namespace || '-')}</strong></div>
            <div><span>推荐场景</span><strong>${escapeHtml(scenario ? `${scenario.id} / ${llmFaultTypeLabel(scenario.fault_type)}` : '待加载')}</strong></div>
            <div><span>可选场景</span><strong>${escapeHtml(scenarios.length)} 个适用 / ${escapeHtml(blockedCount)} 个已过滤</strong></div>
        </div>
        <label class="llm-fault-target-scenario">
            <span>故障类型</span>
            <select id="llm-fault-target-scenario" class="select-sm">${scenarioOptions}</select>
        </label>
        <div id="llm-fault-target-scenario-help" class="llm-fault-target-help">${escapeHtml(scenario?.applicability_reason || (scenarios.length ? '' : '请换一个节点/连线，或在单场景页手动选择全量故障。'))}</div>
        <div class="llm-fault-target-actions">
            <button class="btn btn-sm btn-primary" ${scenarios.length ? '' : 'disabled'} onclick="applyLLMFaultTargetToLab()">使用该目标</button>
            <button class="btn btn-sm" ${scenarios.length ? '' : 'disabled'} onclick="preflightSelectedLLMFaultTarget()">预检</button>
        </div>
    `;
    panel.querySelector('#llm-fault-target-scenario')?.addEventListener('change', evt => {
        const selected = scenarios.find(s => s.id === evt.target.value);
        const help = document.getElementById('llm-fault-target-scenario-help');
        if (help) help.textContent = selected?.applicability_reason || '';
    });
    el.prepend(panel);
}

function llmFaultTopologyIcon(kind = '') {
    const k = String(kind || '').toLowerCase();
    if (k === 'service') return 'SVC';
    if (k === 'pod') return 'POD';
    if (k === 'container') return 'CTR';
    if (k === 'model') return 'MDL';
    if (k === 'node') return 'NOD';
    if (k === 'gpu') return 'GPU';
    if (k === 'target') return 'PRM';
    if (k === 'host') return 'SSH';
    if (k === 'edge') return 'DEP';
    return 'AI';
}

function llmFaultTopologyKindLabel(kind = '') {
    const k = String(kind || '').toLowerCase();
    return {
        cluster: '集群',
        service: '服务',
        pod: 'Pod',
        container: '容器',
        model: '模型服务',
        node: '节点',
        gpu: 'GPU',
        target: '采集目标',
        host: '裸机主机',
        edge: '依赖关系'
    }[k] || kind || '目标';
}

async function applyLLMFaultTargetToLab() {
    const target = _llmTopologyFaultTargets[_selectedLLMTopologyFaultTargetKey];
    if (!target) return;
    switchLLMFaultLabTab('single');
    if (!_llmFaultScenarios.length) await ensureLLMFaultScenariosLoaded();
    const modeInput = document.querySelector('input[name="llmfaultlab-mode"][value="k8s"]');
    if (modeInput) modeInput.checked = true;
    onLLMFaultModeChange();
    const previousClusterId = document.getElementById('llmfaultlab-cluster')?.value || '';
    if (target.cluster_id) setVal('llmfaultlab-cluster', target.cluster_id);
    if (target.cluster_id && target.cluster_id !== previousClusterId) await loadLLMFaultTopology();
    populateLLMFaultTargetSelectors(null, null, {
        namespace: target.namespace || '',
        deployment: target.deployment || '',
        service: target.service || target.deployment || '',
    });
    if (target.namespace) ensureSelectOption('llmfaultlab-namespace', target.namespace);
    if (target.deployment) ensureSelectOption('llmfaultlab-deployment', target.deployment);
    if (target.service) {
        ensureSelectOption('llmfaultlab-service', target.service);
    } else if (target.deployment) {
        ensureSelectOption('llmfaultlab-service', target.deployment);
    }
    const selectedScenarioId = document.getElementById('llm-fault-target-scenario')?.value || '';
    const scenario = _llmFaultScenarios.find(s => s.id === selectedScenarioId) || scenarioForLLMFaultTarget(target);
    if (scenario) {
        setVal('llmfaultlab-scenario', scenario.id);
        renderLLMFaultScenarioDetail();
    }
    const out = document.getElementById('llmfaultlab-output');
    if (out) {
        out.textContent = [
            '已从 GPU Lab 依赖图选择注入目标。',
            `目标: ${target.name || '-'}`,
            `类型: ${target.kind || '-'}`,
            `推荐场景: ${scenario ? `${scenario.id} / ${llmFaultTypeLabel(scenario.fault_type)}` : '-'}`,
            `适用原因: ${scenario?.applicability_reason || llmScenarioApplicabilityReason(scenario || {}, target) || '-'}`,
            `说明: ${target.reason || '-'}`,
            '当前仍为 Dry-run 模式；执行真实注入前需要取消 Dry-run、勾选确认真实执行并通过预检。'
        ].join('\n');
    }
}

async function preflightSelectedLLMFaultTarget() {
    await applyLLMFaultTargetToLab();
    return preflightLLMFaultScenario();
}

async function loadLLMFaultTopology() {
    if (!(_faultTargets.k8s_clusters || []).length) await loadFaultTargets();
    await ensureLLMFaultScenariosLoaded();
    const status = document.getElementById('llmfaultlab-topology-status');
    const aiClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai');
    const selectedClusterId = document.getElementById('llmfaultlab-cluster')?.value || '';
    const selectedCluster = aiClusters.find(c => c.id === selectedClusterId);
    const clusterId = (selectedCluster || aiClusters[0] || {}).id || '';
    if (clusterId && selectedClusterId !== clusterId) setVal('llmfaultlab-cluster', clusterId);
    if (!clusterId) {
        _llmFaultTopologyRequestSeq += 1;
        _lastLLMFaultTargetTopologyData = null;
        _lastLLMFaultTopologyData = null;
        populateLLMFaultTargetSelectors();
        if (status) status.innerHTML = '<span class="badge badge-warning">未配置</span> 请先在“管理目标”中添加智算 K8s 集群。';
        renderLLMFaultTopologyEmpty('未配置智算集群');
        return;
    }
    const duration = document.getElementById('ai-dashboard-range')?.value || '15m';
    const cluster = aiClusters.find(c => c.id === clusterId) || {};
    const requestSeq = ++_llmFaultTopologyRequestSeq;
    if (status) status.innerHTML = '<span class="badge badge-info">加载中</span> 正在读取智算依赖图...';
    const dashboardPromise = api(
        `/api/ai-clusters/${encodeURIComponent(clusterId)}/dashboard?duration=${encodeURIComponent(duration)}&prometheus_url=${encodeURIComponent(cluster.prometheus_url || '')}`,
        { timeoutMs: 45000 },
    );
    const targetTopology = await api(`/api/cluster/topology?cluster_id=${encodeURIComponent(clusterId)}`, { timeoutMs: 45000 });
    if (requestSeq !== _llmFaultTopologyRequestSeq) return;
    const cachedClusterId = String(_lastLLMFaultTopologyData?.cluster?.id || _lastLLMFaultTopologyData?.cluster_id || '');
    const cachedData = cachedClusterId === String(clusterId) ? _lastLLMFaultTopologyData : null;
    const k8sTopologyData = targetTopology ? {
        mode: 'k8s',
        cluster: targetTopology.cluster || cluster,
        cluster_id: targetTopology.cluster_id || clusterId,
        model: targetTopology.cluster?.model || cluster.model || '',
        vllm_endpoint: targetTopology.cluster?.vllm_endpoint || cluster.vllm_endpoint || '',
        topology: {
            nodes: targetTopology.nodes || [],
            pods: targetTopology.pods || [],
            services: targetTopology.services || [],
            targets: [],
            gpu_devices: [],
        },
        storage: { message: '实时指标超时，已使用 K8s 资源拓扑。' },
    } : null;
    const initialData = cachedData || k8sTopologyData;
    if (initialData) {
        populateLLMFaultTargetSelectors(targetTopology, initialData);
        renderLLMFaultSummary();
        renderLLMFaultEnvironment();
        renderLLMFaultTopology(initialData);
        if (status) status.innerHTML = '<span class="badge badge-success">基础拓扑已加载</span> 节点已可点击，正在后台补充 Prometheus、GPU 和模型指标...';
    }

    const freshData = await dashboardPromise;
    if (requestSeq !== _llmFaultTopologyRequestSeq) return;
    const data = freshData || initialData;
    populateLLMFaultTargetSelectors(targetTopology, data);
    if (!data) {
        if (status) status.innerHTML = '<span class="badge badge-danger">失败</span> 依赖图加载失败。';
        renderLLMFaultTopologyEmpty('加载失败');
        return;
    }
    if (freshData) {
        _lastLLMFaultTopologyData = freshData;
        renderLLMFaultSummary();
        renderLLMFaultEnvironment();
        renderLLMFaultTopology(freshData);
    }
    if (status) status.innerHTML = !freshData && cachedData
        ? '<span class="badge badge-warning">使用最近拓扑</span> 实时刷新超时，已保留同一集群最近一次成功的可点击依赖图。'
        : !freshData && k8sTopologyData
            ? '<span class="badge badge-warning">K8s 拓扑</span> 实时指标刷新超时，仍可点击 K8s 节点选择故障目标。'
        : data.mode === 'prometheus'
            ? `<span class="badge badge-success">Prometheus 已连接</span> 点击节点或连线选择注入目标。`
            : `<span class="badge badge-warning">观测不完整</span> ${escapeHtml(data.storage?.message || '仍可基于 K8s 拓扑选择目标。')}`;
}

function renderLLMFaultTopologyEmpty(message) {
    const el = document.getElementById('llmfaultlab-topology');
    if (el) el.innerHTML = `<p class="text-muted">${escapeHtml(message)}</p>`;
}

function renderLLMFaultTopology(data) {
    const el = document.getElementById('llmfaultlab-topology');
    if (!el) return;
    const topo = data.topology || {};
    const nodes = topo.nodes || [];
    const pods = topo.pods || [];
    const services = topo.services || [];
    const targets = topo.targets || [];
    const gpus = topo.gpu_devices || [];
    const modelRecords = aiServedModelRecords(data);
    const nodeNames = nodes.length ? nodes.map(n => n.name) : Array.from(new Set([
        ...gpus.map(g => g.instance || g.Hostname).filter(Boolean),
        ...pods.map(p => p.node).filter(Boolean),
    ]));
    const nodeCards = nodeNames.map(name => {
        const node = nodes.find(n => n.name === name) || {};
        const nodeGpus = gpus.filter(g => (g.instance || g.Hostname) === name);
        const nodePods = pods.filter(p => p.node === name);
        return { name, node, nodeGpus, nodePods };
    });
    const matchedBySelector = svc => pods.filter(p => {
        const selector = svc.selector || {};
        const labels = p.labels || {};
        return Object.keys(selector).length && Object.entries(selector).every(([k, v]) => labels[k] === v);
    });
    const hostRecords = data.hosts || [];
    const displayServices = services.slice(0, 12);
    const displayPods = pods.slice(0, 36);
    const containerRecords = displayPods.flatMap(p => {
        const specs = Array.isArray(p.container_specs) ? p.container_specs : [];
        const statuses = Array.isArray(p.container_statuses) ? p.container_statuses : [];
        const statusByName = Object.fromEntries(statuses.map(s => [s.name, s]));
        if (specs.length) {
            return specs.map(c => ({ pod: p, spec: c, status: statusByName[c.name] || {} }));
        }
        return Array.from({ length: Number(p.containers || 0) || 0 }, (_, i) => ({ pod: p, spec: { name: `container-${i + 1}` }, status: {} }));
    }).slice(0, 48);
    const displayTargets = targets.slice(0, 16);
    const displayHosts = hostRecords.slice(0, 8);
    const displayModels = modelRecords.slice(0, 12);
    const targetInstance = target => String(target.instance || target.discoveredLabels?.__address__ || target.labels?.instance || '');
    const targetNodeName = target => {
        const inst = targetInstance(target);
        const host = inst.split(':')[0];
        return nodeNames.find(name => name === host || name.includes(host) || host.includes(name)) || '';
    };
    const graphNodes = [];
    const graphEdges = [];
    const addNode = (id, kind, name, meta, x, y, status = '', payload = {}) => graphNodes.push({ id, kind, name, meta, x, y, status, payload });
    const addEdge = (from, to, label = '') => {
        if (!graphNodes.some(n => n.id === from) || !graphNodes.some(n => n.id === to)) return;
        if (graphEdges.some(e => e.from === from && e.to === to && e.label === label)) return;
        graphEdges.push({ from, to, label });
    };
    const columns = [
        { kind: 'cluster', x: 36, y: 92, gap: 0 },
        { kind: 'service', x: 272, y: 44, gap: 96 },
        { kind: 'pod', x: 524, y: 44, gap: 86 },
        { kind: 'container', x: 776, y: 44, gap: 74 },
        { kind: 'model', x: 1030, y: 44, gap: 82 },
        { kind: 'node', x: 1284, y: 58, gap: 126 },
        { kind: 'gpu', x: 1530, y: 44, gap: 74 },
        { kind: 'target', x: 1774, y: 44, gap: 74 },
        { kind: 'host', x: 2020, y: 44, gap: 86 },
    ];
    const col = kind => columns.find(c => c.kind === kind) || columns[0];
    addNode('cluster', 'cluster', data.cluster?.name || data.cluster?.id || 'Cluster', `${nodes.length} nodes · ${pods.length} pods · ${gpus.length} GPUs`, col('cluster').x, col('cluster').y, '', { cluster: data.cluster });
    displayServices.forEach((s, i) => addNode(`svc:${s.namespace}:${s.name}`, 'service', s.name, `${s.namespace || '-'} · ${s.type || '-'} · ${(s.ports || []).join(', ') || '-'}`, col('service').x, col('service').y + i * col('service').gap, '', { service: s }));
    displayPods.forEach((p, i) => addNode(`pod:${p.namespace}:${p.name}`, 'pod', p.name, `${p.namespace || '-'} · ${p.node || '-'}`, col('pod').x, col('pod').y + i * col('pod').gap, p.phase || '', { pod: p }));
    containerRecords.forEach((item, i) => {
        const name = item.spec?.name || item.status?.name || `container-${i + 1}`;
        const status = item.status?.state || (item.status?.ready ? 'ready' : '');
        addNode(
            `container:${item.pod.namespace}:${item.pod.name}:${name}`,
            'container',
            name,
            `${item.pod.name || '-'} · restarts ${item.status?.restartCount ?? 0}`,
            col('container').x,
            col('container').y + i * col('container').gap,
            status,
            { pod: item.pod, container: item.spec, container_status: item.status }
        );
    });
    displayModels.forEach((m, i) => addNode(
        aiModelNodeId(m.name),
        'model',
        m.name,
        `${m.pod || m.instance || m.endpoint || '-'}${m.running !== undefined && m.running !== null ? ` · running ${m.running}` : ''}`,
        col('model').x,
        col('model').y + i * col('model').gap,
        m.phase || '',
        { model: m }
    ));
    nodeCards.forEach((card, i) => addNode(`node:${card.name}`, 'node', card.name, `${card.node.internal_ip || '-'} · ${card.node.roles || '-'}`, col('node').x, col('node').y + i * col('node').gap, card.node.ready === false ? 'NotReady' : 'Ready', { node: card.node, node_name: card.name }));
    nodeCards.flatMap(card => card.nodeGpus.map(g => ({ card, g }))).forEach((item, i) => addNode(
        `gpu:${item.card.name}:${item.g.gpu}`,
        'gpu',
        `${item.card.name} / GPU${item.g.gpu || '-'}`,
        `利用率 ${aiPct(item.g.utilization)} · 显存 ${aiPct(item.g.memory_used_pct)}`,
        col('gpu').x,
        col('gpu').y + i * col('gpu').gap,
        `${llmMetricFormatNumber(item.g.temperature)}℃`,
        { node_name: item.card.name, gpu: item.g }
    ));
    displayTargets.forEach((t, i) => addNode(
        `target:${targetInstance(t) || t.job || i}`,
        'target',
        t.job || t.scrapePool || 'target',
        targetInstance(t) || '-',
        col('target').x,
        col('target').y + i * col('target').gap,
        t.health || '',
        { target: t }
    ));
    displayHosts.forEach((h, i) => addNode(
        `host:${h.id || h.host || i}`,
        'host',
        h.name || h.id || h.host || 'host',
        `${h.host || '-'} · ${h.gpu || ''}`,
        col('host').x,
        col('host').y + i * col('host').gap,
        h.role || '',
        { host: h }
    ));
    displayServices.forEach(s => {
        const serviceId = `svc:${s.namespace}:${s.name}`;
        addEdge('cluster', serviceId, '暴露');
        matchedBySelector(s).forEach(p => addEdge(serviceId, `pod:${p.namespace}:${p.name}`, 'selector'));
    });
    displayPods.forEach(p => {
        addEdge('cluster', `pod:${p.namespace}:${p.name}`, '工作负载');
        const specs = Array.isArray(p.container_specs) ? p.container_specs : [];
        const containersForPod = specs.length ? specs : Array.from({ length: Number(p.containers || 0) || 0 }, (_, i) => ({ name: `container-${i + 1}` }));
        containersForPod.forEach(c => {
            const containerId = `container:${p.namespace}:${p.name}:${c.name || ''}`;
            addEdge(`pod:${p.namespace}:${p.name}`, containerId, '包含');
            addEdge(containerId, `node:${p.node}`, '运行于');
        });
        if (!containersForPod.length) addEdge(`pod:${p.namespace}:${p.name}`, `node:${p.node}`, '调度');
    });
    displayModels.forEach(m => {
        const modelId = aiModelNodeId(m.name);
        const matchedPod = displayPods.find(p => p.name === m.pod || (p.served_models || []).includes(m.name) || p.served_model_name === m.name)
            || displayPods.find(p => (p.name || '').includes('vllm-server') || p.app === 'vllm-server');
        if (matchedPod) {
            const modelContainers = containerRecords.filter(item => item.pod?.name === matchedPod.name && item.pod?.namespace === matchedPod.namespace);
            if (modelContainers.length) {
                modelContainers.forEach(item => addEdge(`container:${matchedPod.namespace}:${matchedPod.name}:${item.spec?.name || item.status?.name || ''}`, modelId, 'serve'));
            } else {
                addEdge(`pod:${matchedPod.namespace}:${matchedPod.name}`, modelId, 'serve');
            }
        } else {
            addEdge('cluster', modelId, '模型服务');
        }
        if (m.node) addEdge(modelId, `node:${m.node}`, '运行于');
        const matchedTarget = displayTargets.find(t => (m.instance && (targetInstance(t) === m.instance || (t.labels || {}).instance === m.instance)) || String(t.job || '').toLowerCase().includes('vllm'));
        if (matchedTarget) addEdge(modelId, `target:${targetInstance(matchedTarget) || matchedTarget.job || displayTargets.indexOf(matchedTarget)}`, 'metrics');
    });
    nodeCards.forEach(card => card.nodeGpus.forEach(g => addEdge(`node:${card.name}`, `gpu:${card.name}:${g.gpu}`, '挂载')));
    displayTargets.forEach((t, i) => {
        const targetId = `target:${targetInstance(t) || t.job || i}`;
        const matchedNode = targetNodeName(t);
        if (matchedNode) addEdge(`node:${matchedNode}`, targetId, 'scrape');
        else addEdge('cluster', targetId, 'scrape');
    });
    displayHosts.forEach((h, i) => {
        const hostId = `host:${h.id || h.host || i}`;
        const matchedNode = nodeNames.find(name => name === h.host || name.includes(h.host || '') || String(h.host || '').includes(name));
        if (matchedNode) addEdge(`node:${matchedNode}`, hostId, 'SSH/裸机');
        else addEdge('cluster', hostId, 'SSH/裸机');
    });
    const byId = Object.fromEntries(graphNodes.map(n => [n.id, n]));
    const safeTargetKey = text => String(text || '').replace(/[^A-Za-z0-9_-]/g, '_');
    _llmTopologyFaultTargets = {};
    graphNodes.forEach(n => {
        _llmTopologyFaultTargets[`node_${safeTargetKey(n.id)}`] = buildLLMFaultTargetFromTopology('node', n, data);
    });
    graphEdges.forEach(e => {
        _llmTopologyFaultTargets[`edge_${safeTargetKey(e.from + '_' + e.to)}`] = buildLLMFaultTargetFromTopology('edge', e, data, byId);
    });
    const pathFor = e => {
        const a = byId[e.from];
        const b = byId[e.to];
        const x1 = a.x + 176;
        const y1 = a.y + 34;
        const x2 = b.x;
        const y2 = b.y + 34;
        const mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    };
    const nodeWidth = 176;
    const nodeHeight = 68;
    const canvasPadding = 44;
    const svgWidth = Math.max(720, Math.max(0, ...graphNodes.map(n => n.x + nodeWidth)) + canvasPadding);
    const svgHeight = Math.max(260, Math.max(0, ...graphNodes.map(n => n.y + nodeHeight)) + canvasPadding);
    const edgePaths = graphEdges.map(e => ({ ...e, key: safeTargetKey(e.from + '-' + e.to), d: pathFor(e) }));
    el.innerHTML = `
        <div class="llm-topology-hint">点击节点或连线选择注入目标；当前展示 Service ${displayServices.length}/${services.length}、Pod ${displayPods.length}/${pods.length}、Container ${containerRecords.length}/${pods.reduce((sum, p) => sum + (Number(p.containers || 0) || 0), 0)}、Model ${displayModels.length}/${modelRecords.length}、Node ${nodeCards.length}/${nodeCards.length}、GPU ${gpus.length}/${gpus.length}、Target ${displayTargets.length}/${targets.length}、Host ${displayHosts.length}/${hostRecords.length}。真实执行仍受 Dry-run、确认和预检保护。</div>
        <div class="ai-topology-canvas llm-fault-topology-canvas" style="--llm-topology-width: ${svgWidth}px; --llm-topology-height: ${svgHeight}px;">
            <svg viewBox="0 0 ${svgWidth} ${svgHeight}" role="img" aria-label="GPU Lab 智算故障依赖图">
                <defs>
                    <marker id="llmFaultArrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L0,6 L8,3 z"></path>
                    </marker>
                </defs>
                ${edgePaths.map(e => `<path id="llm-edge-label-${e.key}" class="ai-topology-edge-label-path" d="${e.d}"></path>`).join('')}
                ${edgePaths.map(e => `
                    <path class="ai-topology-edge llm-fault-edge" d="${e.d}"></path>
                    <text class="ai-topology-edge-label"><textPath href="#llm-edge-label-${e.key}" startOffset="48%">${escapeHtml(e.label || '')}</textPath></text>
                `).join('')}
                ${edgePaths.map(e => `<path class="ai-topology-edge-hit llm-fault-clickable" d="${e.d}" onclick="openLLMFaultTargetPicker('edge_${safeTargetKey(e.from + '_' + e.to)}')"></path>`).join('')}
                ${graphNodes.map(n => `<g class="ai-topology-node llm-fault-clickable ${escapeHtml(n.kind)}" transform="translate(${n.x}, ${n.y})" onclick="openLLMFaultTargetPicker('node_${safeTargetKey(n.id)}')">
                    <rect width="176" height="68" rx="5"></rect>
                    <text x="12" y="18" class="kind">${escapeHtml(llmFaultTopologyIcon(n.kind))}</text>
                    <text x="12" y="39" class="name">${escapeHtml(n.name || '-')}</text>
                    <text x="12" y="57" class="meta">${escapeHtml(n.meta || '')}</text>
                    ${n.status ? `<text x="164" y="18" class="status" text-anchor="end">${escapeHtml(n.status)}</text>` : ''}
                </g>`).join('')}
            </svg>
        </div>
    `;
}

function renderAIDashboardTargets(data) {
    const el = document.getElementById('ai-dashboard-targets');
    if (!el) return;
    const targets = data.topology?.targets || data.inventory?.prometheus?.targets?.targets || [];
    if (!targets.length) {
        el.innerHTML = '<p class="text-muted">未获取到 Prometheus target；检查 /api/v1/targets 或 scrape 配置。</p>';
        return;
    }
    el.innerHTML = `
        <div class="ai-side-summary">
            <div><span>Targets</span><strong>${targets.length}</strong></div>
            <div><span>GPU</span><strong>${(data.topology?.gpu_devices || []).length}</strong></div>
            <div><span>Pods</span><strong>${(data.topology?.pods || []).length}</strong></div>
        </div>
        ${targets.slice(0, 4).map(t => `
        <div class="ai-target-item ${t.health === 'up' ? 'ok' : 'bad'}">
            <div><strong title="${escapeHtml(t.job || '-')}">${escapeHtml(t.job || '-')}</strong><span title="${escapeHtml(t.instance || '-')}">${escapeHtml(t.instance || '-')}</span></div>
            <span class="badge ${t.health === 'up' ? 'badge-success' : 'badge-danger'}">${escapeHtml(t.health || 'unknown')}</span>
            ${t.error ? `<small>${escapeHtml(t.error)}</small>` : ''}
        </div>
    `).join('')}`;
}

function switchAIResourceTab(tab) {
    _aiResourceTab = tab || 'nodes';
    document.querySelectorAll('[data-ai-resource-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.aiResourceTab === _aiResourceTab);
    });
    if (_lastAIDashboardData) renderAIDashboardResources(_lastAIDashboardData);
}

function setAIResourcePage(tab, page) {
    const key = tab || _aiResourceTab || 'pods';
    _aiResourcePage[key] = Math.max(1, Number(page) || 1);
    if (_lastAIDashboardData) renderAIDashboardResources(_lastAIDashboardData);
}

function renderAIDashboardResources(data) {
    const el = document.getElementById('ai-dashboard-resources');
    if (!el) return;
    const nodes = data.topology?.nodes || [];
    const gpus = data.topology?.gpu_devices || [];
    const pods = data.topology?.pods || [];
    const services = data.topology?.services || [];
    const targets = data.topology?.targets || [];
    if (_aiResourceTab === 'gpus') {
        el.innerHTML = gpus.length ? `<div class="ai-gpu-grid">${gpus.map(g => `
                <div class="ai-gpu-card">
                    <strong>${escapeHtml(g.instance || '-')} / GPU ${escapeHtml(g.gpu || '-')}</strong>
                    <span>${escapeHtml(g.model || g.device || '-')}</span>
                    <small>利用率 ${escapeHtml(aiPct(g.utilization))} · 显存 ${escapeHtml(aiPct(g.memory_used_pct))} · 温度 ${escapeHtml(llmMetricFormatNumber(g.temperature))}℃</small>
                </div>
            `).join('')}</div>` : '<div class="ai-empty-note">未采集到 GPU 指标。</div>';
        return;
    }
    if (_aiResourceTab === 'pods') {
        const pageSize = 8;
        const totalPages = Math.max(1, Math.ceil(pods.length / pageSize));
        const page = Math.min(Math.max(1, Number(_aiResourcePage.pods) || 1), totalPages);
        _aiResourcePage.pods = page;
        const visiblePods = pods.slice((page - 1) * pageSize, page * pageSize);
        el.innerHTML = pods.length ? `
            <table class="ai-table">
                <thead><tr><th>Pod</th><th>状态</th><th>节点</th><th>IP</th><th>Ready</th><th>重启</th><th>容器 / 模型</th></tr></thead>
                <tbody>${visiblePods.map(p => `
                    <tr>
                        <td><strong>${escapeHtml(p.name || '-')}</strong><small>${escapeHtml(p.owner || p.app || '')}</small></td>
                        <td><span class="badge ${p.phase === 'Running' ? 'badge-success' : p.phase === 'Succeeded' ? 'badge-info' : 'badge-warning'}">${escapeHtml(p.phase || '-')}</span></td>
                        <td>${escapeHtml(p.node || '-')}</td>
                        <td>${escapeHtml(p.pod_ip || '-')}</td>
                        <td>${escapeHtml(`${p.ready ?? 0}/${p.containers ?? 0}`)}</td>
                        <td>${escapeHtml(p.restarts ?? 0)}</td>
                        <td>
                            <strong>${escapeHtml((p.container_specs || []).map(c => c.name).filter(Boolean).join(', ') || '-')}</strong>
                            <small>${escapeHtml((p.served_models || []).join(', ') || (p.images || []).map(x => x.split('/').pop()).join(', ') || '-')}</small>
                        </td>
                    </tr>
                `).join('')}</tbody>
            </table>
            <div class="ai-resource-pagination">
                <div><strong>${escapeHtml(pods.length)}</strong> 个 Pod · 第 <strong>${escapeHtml(page)}</strong> / ${escapeHtml(totalPages)} 页</div>
                <div class="ai-resource-pagination-actions">
                    <button class="btn btn-sm" ${page <= 1 ? 'disabled' : ''} onclick="setAIResourcePage('pods', ${page - 1})">上一页</button>
                    <button class="btn btn-sm" ${page >= totalPages ? 'disabled' : ''} onclick="setAIResourcePage('pods', ${page + 1})">下一页</button>
                </div>
            </div>
        ` : `<div class="ai-empty-note">${escapeHtml(data.inventory?.k8s?.message || '未采集到 Pod。')}</div>`;
        return;
    }
    if (_aiResourceTab === 'services') {
        el.innerHTML = services.length ? `<table class="ai-table">
            <thead><tr><th>Service</th><th>类型</th><th>ClusterIP</th><th>端口</th></tr></thead>
            <tbody>${services.map(s => `
                <tr>
                    <td><strong>${escapeHtml(s.name || '-')}</strong></td>
                    <td>${escapeHtml(s.type || '-')}</td>
                    <td>${escapeHtml(s.cluster_ip || '-')}</td>
                    <td>${escapeHtml((s.ports || []).join(', ') || '-')}</td>
                </tr>
            `).join('')}</tbody>
        </table>` : '<div class="ai-empty-note">未采集到 Service。</div>';
        return;
    }
    if (_aiResourceTab === 'targets') {
        el.innerHTML = targets.length ? `<table class="ai-table">
            <thead><tr><th>Job</th><th>Instance</th><th>状态</th><th>Scrape URL</th></tr></thead>
            <tbody>${targets.map(t => `
                <tr>
                    <td><strong>${escapeHtml(t.job || '-')}</strong></td>
                    <td>${escapeHtml(t.instance || '-')}</td>
                    <td><span class="badge ${t.health === 'up' ? 'badge-success' : 'badge-danger'}">${escapeHtml(t.health || '-')}</span></td>
                    <td>${escapeHtml(t.scrape_url || '-')}</td>
                </tr>
            `).join('')}</tbody>
        </table>` : '<div class="ai-empty-note">未获取到 Prometheus target。</div>';
        return;
    }
    el.innerHTML = nodes.length ? `
        <table class="ai-table">
            <thead><tr><th>节点</th><th>状态</th><th>IP</th><th>角色</th><th>GPU</th><th>运行时</th></tr></thead>
            <tbody>${nodes.map(n => `
                <tr>
                    <td><strong>${escapeHtml(n.name || '-')}</strong></td>
                    <td><span class="badge ${n.ready ? 'badge-success' : 'badge-danger'}">${n.ready ? 'Ready' : 'NotReady'}</span></td>
                    <td>${escapeHtml(n.internal_ip || '-')}</td>
                    <td>${escapeHtml(n.roles || '-')}</td>
                    <td>${escapeHtml(n.gpu || '-')}</td>
                    <td>${escapeHtml(n.runtime || '-')}</td>
                </tr>
            `).join('')}</tbody>
        </table>
    ` : `<div class="ai-empty-note">${escapeHtml(data.inventory?.k8s?.message || '未采集到 K8s 节点。')}</div>`;
}

function renderAIDashboard(data) {
    _lastAIDashboardData = data;
    state.aiDashboardData = data;
    const status = document.getElementById('ai-dashboard-status');
    const cluster = data.cluster || {};
    if (status) {
        status.innerHTML = data.mode === 'prometheus'
            ? `<span class="badge badge-success">Prometheus 已连接</span> ${escapeHtml(data.prometheus_url || '')}`
            : `<span class="badge badge-warning">未连接时序库</span> ${escapeHtml(data.storage?.message || '')}`;
        status.innerHTML += (data.storage?.warnings || []).map(message => `<p class="text-warning">${escapeHtml(message)}</p>`).join('');
    }
    const timeWindow = document.getElementById('ai-dashboard-time-window');
    if (timeWindow) {
        const range = data.time_range || {};
        timeWindow.innerHTML = `
            <span class="badge badge-info">${escapeHtml(range.mode === 'absolute' ? '自定义时间' : '相对时间')}</span>
            <strong>${escapeHtml(aiDashboardFormatTime(range.start))}</strong>
            <span>至</span>
            <strong>${escapeHtml(aiDashboardFormatTime(range.end))}</strong>
            <span>步长 ${escapeHtml(data.step || '-')} · ${escapeHtml(range.duration_s || 0)} 秒</span>
        `;
    }
    const s = data.summary || {};
    const summary = document.getElementById('ai-dashboard-summary');
    if (summary) {
        const trace = data.fine_grained_trace || {};
        const traceSummary = trace.summary || {};
        summary.innerHTML = `
            <div class="ai-info-box bg-info"><span>运行中请求</span><strong>${escapeHtml(aiSummaryValue(s.running_requests))}</strong></div>
            <div class="ai-info-box bg-warning"><span>等待队列</span><strong>${escapeHtml(aiSummaryValue(s.waiting_requests))}</strong></div>
            <div class="ai-info-box bg-danger"><span>KV Cache</span><strong>${escapeHtml(aiSummaryValue(s.kv_cache, '%'))}</strong></div>
            <div class="ai-info-box bg-success"><span>成功请求速率</span><strong>${escapeHtml(aiSummaryValue(s.success_rate, '/s'))}</strong></div>
            <div class="ai-info-box bg-primary"><span>TTFT P95</span><strong>${escapeHtml(aiSummaryValue(s.ttft_p95, 's'))}</strong></div>
            <div class="ai-info-box bg-purple"><span>GPU 利用率</span><strong>${escapeHtml(aiSummaryValue(s.gpu_util, '%'))}</strong></div>
            <div class="ai-info-box bg-danger"><span>GPU 显存</span><strong>${escapeHtml(aiSummaryValue(s.gpu_memory_used, '%'))}</strong></div>
            <div class="ai-info-box bg-info" data-node-metric="node_cpu_usage"><span>节点 CPU 最高</span><strong>${escapeHtml(aiPct(s.node_cpu_usage))}</strong><small>${escapeHtml(data.summary_nodes?.node_cpu_usage || '暂无节点样本')}</small></div>
            <div class="ai-info-box bg-warning" data-node-metric="node_memory_usage"><span>节点内存最高</span><strong>${escapeHtml(aiPct(s.node_memory_usage))}</strong><small>${escapeHtml(data.summary_nodes?.node_memory_usage || '暂无节点样本')}</small></div>
            <div class="ai-info-box bg-secondary" data-node-metric="node_rootfs_usage"><span>根分区最高</span><strong>${escapeHtml(aiPct(s.node_rootfs_usage))}</strong><small>${escapeHtml(data.summary_nodes?.node_rootfs_usage || '暂无节点样本')}</small></div>
            <div class="ai-info-box bg-dark"><span>网络接收</span><strong>${escapeHtml(aiSummaryValue(s.node_network_receive, ' MB/s'))}</strong></div>
            <div class="ai-info-box bg-secondary"><span>TELLER 事件</span><strong>${escapeHtml(trace.available ? traceSummary.total_events : '-')}</strong></div>
            <div class="ai-info-box bg-dark"><span>Kernel P95</span><strong>${escapeHtml(trace.available ? aiTraceValue(traceSummary.kernel_p95_ms, 'ms') : '-')}</strong></div>
        `;
    }
    renderAIDashboardTopology(data);
    renderAIDashboardTargets(data);
    renderAIDashboardResources(data);
    renderAIDashboardFineGrainedTrace(data);
    const charts = data.charts || {};
    Object.entries(AI_DASHBOARD_CHARTS).forEach(([key, spec]) => {
        const chartData = (charts[spec.group] || {})[key];
        renderLLMMetricChart(spec.canvas, chartData, spec.unit);
    });
    const profile = document.getElementById('ai-dashboard-profile');
    if (profile) {
        profile.innerHTML = `
            <div class="ai-profile-grid">
                <div><span>集群</span><strong>${escapeHtml(cluster.name || cluster.id || '-')}</strong></div>
                <div><span>ID</span><strong>${escapeHtml(cluster.id || '-')}</strong></div>
                <div><span>Namespace</span><strong>${escapeHtml(cluster.default_namespace || '-')}</strong></div>
                <div><span>模型</span><strong>${escapeHtml(data.model || cluster.model || '-')}</strong></div>
                <div><span>vLLM Endpoint</span><strong>${escapeHtml(data.vllm_endpoint || cluster.vllm_endpoint || '-')}</strong></div>
                <div><span>Prometheus</span><strong>${escapeHtml(data.prometheus_url || cluster.prometheus_url || '-')}</strong></div>
                <div><span>TELLER Trace</span><strong>${escapeHtml(data.fine_grained_trace?.trace_dir || cluster.teller_trace_dir || '-')}</strong></div>
            </div>
        `;
    }
    const hosts = document.getElementById('ai-dashboard-hosts');
    if (hosts) {
        const items = data.hosts || [];
        hosts.innerHTML = items.length ? items.map(h => `
            <div class="ai-host-item">
                <strong>${escapeHtml(h.name || h.id || '-')}</strong>
                <span>${escapeHtml(h.ssh_user || '-')}@${escapeHtml(h.host || '-')}</span>
                <small>${escapeHtml(h.gpu || '')} ${h.role ? ' · ' + escapeHtml(h.role) : ''}</small>
            </div>
        `).join('') : '<p class="text-muted">未关联裸机 GPU 主机。</p>';
    }
}

function switchLoadgenMode(mode) {
    _loadgenMode = mode === 'ai' ? 'ai' : 'general';
    document.querySelectorAll('[data-loadgen-mode]').forEach(button => {
        button.classList.toggle('active', button.dataset.loadgenMode === _loadgenMode);
    });
    document.querySelectorAll('.loadgen-mode-panel').forEach(panel => panel.classList.remove('active'));
    document.getElementById(`loadgen-panel-${_loadgenMode}`)?.classList.add('active');
    if (_loadgenMode === 'ai') loadAILoadgenTargets();
    else loadGeneralLoadgenRuns();
}

async function loadLoadGenerator() {
    await loadFaultTargets();
    if (_loadgenMode === 'general') {
        await loadGeneralLoadgenBenchmarks();
    } else {
        await loadAILoadgenTargets();
    }
}

async function loadGeneralLoadgenBenchmarks() {
    const clusterId = document.getElementById('loadgen-general-cluster')?.value || '';
    const status = document.getElementById('loadgen-general-action-status');
    if (!clusterId) {
        _generalLoadgenBenchmarks = [];
        if (status) status.textContent = '未配置通算集群';
        return;
    }
    if (status) status.textContent = '正在识别已部署的微服务 Benchmark...';
    const data = await api(`/api/loadgen/general/${encodeURIComponent(clusterId)}/benchmarks`, { timeoutMs: 60000 });
    if (!data) {
        if (status) status.textContent = 'Benchmark 识别失败';
        return;
    }
    _generalLoadgenBenchmarks = data.benchmarks || [];
    const select = document.getElementById('loadgen-general-benchmark');
    const current = select?.value || '';
    const available = _generalLoadgenBenchmarks.filter(item => item.available);
    if (select) {
        select.innerHTML = available.length
            ? available.map(item => `<option value="${escapeHtmlAttr(item.id)}">${escapeHtml(item.name || item.id)}</option>`).join('')
            : '<option value="">未发现可用 Benchmark</option>';
        if (current && available.some(item => item.id === current)) select.value = current;
    }
    populateGeneralLoadgenScenarios();
    if (status) status.textContent = available.length
        ? `已识别 ${available.length} 个可用 Benchmark`
        : '当前集群未部署已配置的微服务 Benchmark';
    await loadGeneralLoadgenRuns();
}

function selectedGeneralLoadgenBenchmark() {
    const benchmarkId = document.getElementById('loadgen-general-benchmark')?.value || '';
    return _generalLoadgenBenchmarks.find(item => item.id === benchmarkId) || null;
}

function populateGeneralLoadgenScenarios() {
    const benchmark = selectedGeneralLoadgenBenchmark();
    const select = document.getElementById('loadgen-general-scenario');
    if (!select) return;
    const current = select.value;
    const scenarios = benchmark?.scenarios || [];
    select.innerHTML = scenarios.length
        ? scenarios.map(scenario => `<option value="${escapeHtmlAttr(scenario.id)}">${escapeHtml(scenario.name || scenario.id)}</option>`).join('')
        : '<option value="">未配置业务场景</option>';
    if (current && scenarios.some(scenario => scenario.id === current)) select.value = current;
    renderGeneralLoadgenSelection();
    loadGeneralLoadgenRuns();
}

function renderGeneralLoadgenSelection() {
    const benchmark = selectedGeneralLoadgenBenchmark();
    const scenarioId = document.getElementById('loadgen-general-scenario')?.value || '';
    const scenario = (benchmark?.scenarios || []).find(item => item.id === scenarioId) || null;
    const container = document.getElementById('loadgen-general-selection');
    if (!container) return;
    container.innerHTML = benchmark && scenario ? `
        <strong>${escapeHtml(benchmark.name)}</strong>
        <span>${escapeHtml(scenario.description || benchmark.description || '')}</span>
        <small>目标 ${escapeHtml(benchmark.namespace)}/${escapeHtml(benchmark.service)}:${escapeHtml(benchmark.port)} · ${escapeHtml(scenario.method)} ${escapeHtml(scenario.path)} · 调用链 ${escapeHtml(benchmark.trace_backend || '未配置')}</small>
    ` : '';
}

function generalLoadgenBody() {
    return {
        benchmark_id: document.getElementById('loadgen-general-benchmark')?.value || '',
        scenario_id: document.getElementById('loadgen-general-scenario')?.value || '',
        duration_s: parseInt(document.getElementById('loadgen-general-duration')?.value || '180', 10) || 180,
        concurrency: parseInt(document.getElementById('loadgen-general-concurrency')?.value || '8', 10) || 8,
        qps: parseFloat(document.getElementById('loadgen-general-qps')?.value || '0') || 0,
        timeout_s: parseInt(document.getElementById('loadgen-general-timeout')?.value || '10', 10) || 10,
    };
}

async function startGeneralLoadgen() {
    const clusterId = document.getElementById('loadgen-general-cluster')?.value || '';
    const body = generalLoadgenBody();
    const status = document.getElementById('loadgen-general-action-status');
    if (!clusterId || !body.benchmark_id || !body.scenario_id) {
        if (status) status.textContent = '请选择集群、Benchmark 和业务负载场景';
        return;
    }
    if (status) status.textContent = '正在启动负载...';
    const result = await api(`/api/loadgen/general/${encodeURIComponent(clusterId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        timeoutMs: 120000,
    });
    if (status) status.textContent = result?.status === 'started'
        ? `负载已启动：${result.loadgen?.run_id || ''}`
        : '负载启动失败';
    await loadGeneralLoadgenRuns();
}

function renderLoadgenRuns(containerId, runs, stopFunction) {
    const container = document.getElementById(containerId);
    if (!container) return;
    if (!runs?.length) {
        container.innerHTML = '<div class="empty-state compact">当前没有负载任务</div>';
        return;
    }
    container.innerHTML = runs.map(run => {
        const active = ['Running', 'Pending'].includes(run.phase);
        const badge = active ? 'badge-success' : run.phase === 'Succeeded' ? 'badge-info' : 'badge-warning';
        return `<div class="loadgen-run-item">
            <div class="loadgen-run-main">
                <span class="badge ${badge}">${escapeHtml(run.phase || 'Unknown')}</span>
                <strong>${escapeHtml(run.run_id || run.name || '-')}</strong>
                <span>${escapeHtml(run.model || run.benchmark_id || run.deployment || '负载')} · ${escapeHtml(run.namespace || '-')}/${escapeHtml(run.service || '-')}</span>
                <small>${escapeHtml(run.started_at || '')}${run.node ? ` · ${escapeHtml(run.node)}` : ''}</small>
            </div>
            <button class="btn btn-sm btn-danger" onclick="${stopFunction}('${escapeHtmlAttr(run.run_id || '')}','${escapeHtmlAttr(run.namespace || '')}')">停止</button>
        </div>`;
    }).join('');
}

async function loadGeneralLoadgenRuns() {
    const clusterId = document.getElementById('loadgen-general-cluster')?.value || '';
    const namespace = selectedGeneralLoadgenBenchmark()?.namespace || '';
    if (!clusterId || !namespace) {
        renderLoadgenRuns('loadgen-general-runs', [], 'stopGeneralLoadgen');
        return;
    }
    const data = await api(`/api/loadgen/general/${encodeURIComponent(clusterId)}?namespace=${encodeURIComponent(namespace)}`);
    renderLoadgenRuns('loadgen-general-runs', data?.runs || [], 'stopGeneralLoadgen');
}

async function stopGeneralLoadgen(runId, namespace) {
    const clusterId = document.getElementById('loadgen-general-cluster')?.value || '';
    const status = document.getElementById('loadgen-general-action-status');
    if (!clusterId || !runId) return;
    if (status) status.textContent = `正在停止 ${runId}...`;
    const result = await api(`/api/loadgen/general/${encodeURIComponent(clusterId)}/${encodeURIComponent(runId)}?namespace=${encodeURIComponent(namespace || '')}`, {
        method: 'DELETE',
        timeoutMs: 120000,
    });
    if (status) status.textContent = result?.status === 'stopped' ? `负载已停止：${runId}` : `停止失败：${runId}`;
    await loadGeneralLoadgenRuns();
}

function selectedAILoadgenTarget() {
    const service = document.getElementById('loadgen-ai-service')?.value || '';
    return _aiLoadgenTargets.find(item => item.service === service) || null;
}

async function loadAILoadgenTargets() {
    const clusterId = document.getElementById('loadgen-ai-cluster')?.value || '';
    const status = document.getElementById('loadgen-ai-action-status');
    if (!clusterId) {
        _aiLoadgenTargets = [];
        if (status) status.textContent = '未配置智算集群';
        return;
    }
    if (status) status.textContent = '正在发现推理服务和 Served Model...';
    const data = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen-targets`, { timeoutMs: 90000 });
    _aiLoadgenTargets = data?.targets || [];
    const select = document.getElementById('loadgen-ai-service');
    const current = select?.value || '';
    if (select) {
        select.innerHTML = _aiLoadgenTargets.length
            ? _aiLoadgenTargets.map(target => `<option value="${escapeHtmlAttr(target.service)}">${escapeHtml(target.service)} · ${escapeHtml(target.namespace)}</option>`).join('')
            : '<option value="">未发现可用推理服务</option>';
        if (current && _aiLoadgenTargets.some(item => item.service === current)) select.value = current;
    }
    populateAILoadgenModels();
    if (status) status.textContent = _aiLoadgenTargets.length
        ? `已发现 ${_aiLoadgenTargets.length} 个可用推理服务`
        : '没有发现同时具备就绪 Pod 和 Served Model 的服务';
    await loadAILoadgenRuns();
}

function populateAILoadgenModels() {
    const target = selectedAILoadgenTarget();
    const select = document.getElementById('loadgen-ai-model');
    if (!select) return;
    const current = select.value;
    const models = target?.models || [];
    select.innerHTML = models.length
        ? models.map(model => `<option value="${escapeHtmlAttr(model)}">${escapeHtml(model)}</option>`).join('')
        : '<option value="">未发现 Served Model</option>';
    if (current && models.includes(current)) select.value = current;
    else if (target?.default_model && models.includes(target.default_model)) select.value = target.default_model;
    renderAILoadgenSelection();
}

function renderAILoadgenSelection() {
    const target = selectedAILoadgenTarget();
    const model = document.getElementById('loadgen-ai-model')?.value || '';
    const container = document.getElementById('loadgen-ai-selection');
    if (!container) return;
    container.innerHTML = target && model ? `
        <strong>${escapeHtml(model)}</strong>
        <span>由 ${escapeHtml(target.service)} 提供</span>
        <small>目标 ${escapeHtml(target.namespace)}/${escapeHtml(target.service)}:${escapeHtml(target.port)} · Pod ${(target.pods || []).map(escapeHtml).join(', ')} · 来源 ${escapeHtml(target.source || '-')}</small>
    ` : '';
}

function aiIndependentLoadgenBody() {
    const clusterId = document.getElementById('loadgen-ai-cluster')?.value || '';
    const cluster = (_faultTargets.k8s_clusters || []).find(item => item.id === clusterId) || {};
    const target = selectedAILoadgenTarget() || {};
    return {
        namespace: target.namespace || cluster.default_namespace || 'llm-k8s',
        deployment: target.deployment || target.service || cluster.deployment || 'vllm-server',
        service: target.service || '',
        model: document.getElementById('loadgen-ai-model')?.value || '',
        loadgen_duration_s: parseInt(document.getElementById('loadgen-ai-duration')?.value || '180', 10) || 180,
        loadgen_concurrency: parseInt(document.getElementById('loadgen-ai-concurrency')?.value || '8', 10) || 8,
        loadgen_prompt_len: parseInt(document.getElementById('loadgen-ai-prompt')?.value || '1024', 10) || 1024,
        loadgen_max_tokens: parseInt(document.getElementById('loadgen-ai-tokens')?.value || '64', 10) || 64,
        loadgen_qps: parseFloat(document.getElementById('loadgen-ai-qps')?.value || '0') || 0,
    };
}

async function startAILoadgen() {
    const clusterId = document.getElementById('loadgen-ai-cluster')?.value || '';
    const status = document.getElementById('loadgen-ai-action-status');
    const body = aiIndependentLoadgenBody();
    if (!clusterId || !body.service || !body.model) {
        if (status) status.textContent = '请选择智算集群、推理服务和 Served Model';
        return;
    }
    if (status) status.textContent = '正在启动负载...';
    const result = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiIndependentLoadgenBody()),
        timeoutMs: 120000,
    });
    if (status) status.textContent = result?.status === 'started'
        ? `负载已启动：${result.loadgen?.model || body.model} · ${result.loadgen?.run_id || ''}`
        : '负载启动失败';
    await loadAILoadgenRuns();
}

async function loadAILoadgenRuns() {
    const clusterId = document.getElementById('loadgen-ai-cluster')?.value || '';
    const body = aiIndependentLoadgenBody();
    if (!clusterId) {
        renderLoadgenRuns('loadgen-ai-runs', [], 'stopAILoadgen');
        return;
    }
    const data = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen?namespace=${encodeURIComponent(body.namespace)}`);
    renderLoadgenRuns('loadgen-ai-runs', data?.runs || [], 'stopAILoadgen');
}

async function stopAILoadgen(runId, namespace) {
    const clusterId = document.getElementById('loadgen-ai-cluster')?.value || '';
    const status = document.getElementById('loadgen-ai-action-status');
    if (!clusterId || !runId) return;
    if (status) status.textContent = `正在停止 ${runId}...`;
    const body = { ...aiIndependentLoadgenBody(), namespace, run_id: runId };
    const result = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        timeoutMs: 120000,
    });
    if (status) status.textContent = result?.status === 'cleaned' ? `负载已停止：${runId}` : `停止失败：${runId}`;
    await loadAILoadgenRuns();
}

function aiDashboardLoadgenBody() {
    const clusterId = document.getElementById('ai-dashboard-cluster')?.value || '';
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    return {
        namespace: cluster.default_namespace || 'llm-k8s',
        deployment: cluster.deployment || 'vllm-server',
        service: cluster.service || cluster.deployment || 'vllm-server',
        model: cluster.model || 'vllm',
        loadgen_duration_s: parseInt(document.getElementById('ai-loadgen-duration')?.value || '180', 10) || 180,
        loadgen_concurrency: parseInt(document.getElementById('ai-loadgen-concurrency')?.value || '8', 10) || 8,
        loadgen_prompt_len: parseInt(document.getElementById('ai-loadgen-prompt')?.value || '1024', 10) || 1024,
        loadgen_max_tokens: parseInt(document.getElementById('ai-loadgen-tokens')?.value || '64', 10) || 64,
        loadgen_qps: parseFloat(document.getElementById('ai-loadgen-qps')?.value || '0') || 0,
    };
}

function renderAIDashboardLoadgenResult(result, actionLabel) {
    const el = document.getElementById('ai-loadgen-status');
    if (!el) return;
    if (!result) {
        el.innerHTML = `<span class="badge badge-danger">失败</span> ${escapeHtml(actionLabel)}请求失败`;
        return;
    }
    const loadgen = result.loadgen || {};
    const ok = ['started', 'cleaned', 'dry_run'].includes(result.status);
    const results = loadgen.results || [];
    const failed = results.filter(r => !r.ok);
    el.innerHTML = `
        <span class="badge ${ok && !failed.length ? 'badge-success' : 'badge-warning'}">${escapeHtml(result.status || '-')}</span>
        <strong>${escapeHtml(actionLabel)}</strong>
        <span>${escapeHtml(loadgen.namespace || '-')}/${escapeHtml(loadgen.pod || loadgen.service || '-')}</span>
        <span>并发 ${escapeHtml(loadgen.concurrency ?? '-')} · ${escapeHtml(loadgen.duration_s ?? '-')}s · QPS ${escapeHtml(loadgen.qps ?? 0)}</span>
        ${failed.length ? `<small>${escapeHtml(failed[0].stderr || failed[0].stdout || '存在失败命令')}</small>` : ''}
    `;
}

async function startAIDashboardLoadgen() {
    await loadFaultTargets();
    const clusterId = document.getElementById('ai-dashboard-cluster')?.value || '';
    const el = document.getElementById('ai-loadgen-status');
    if (!clusterId) {
        if (el) el.innerHTML = '<span class="badge badge-warning">未配置</span> 请先选择智算集群。';
        return;
    }
    if (el) el.innerHTML = '<span class="badge badge-info">启动中</span> 正在创建 vLLM 访问负载 Pod...';
    const result = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiDashboardLoadgenBody()),
        timeoutMs: 60000,
    });
    renderAIDashboardLoadgenResult(result, '启动负载');
    setTimeout(loadAIDashboard, 2000);
}

async function cleanupAIDashboardLoadgen() {
    await loadFaultTargets();
    const clusterId = document.getElementById('ai-dashboard-cluster')?.value || '';
    const el = document.getElementById('ai-loadgen-status');
    if (!clusterId) {
        if (el) el.innerHTML = '<span class="badge badge-warning">未配置</span> 请先选择智算集群。';
        return;
    }
    if (el) el.innerHTML = '<span class="badge badge-info">清理中</span> 正在删除访问负载 Pod...';
    const result = await api(`/api/ai-clusters/${encodeURIComponent(clusterId)}/loadgen`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiDashboardLoadgenBody()),
        timeoutMs: 60000,
    });
    renderAIDashboardLoadgenResult(result, '清理负载');
    setTimeout(loadAIDashboard, 2000);
}

function renderLLMFaultParamHelp() {
    const el = document.getElementById('llmfaultlab-param-help');
    if (!el) return;
    const mode = getLLMFaultMode();
    const engine = getLLMFaultEngine();
    const engineText = {
        agent: '当前诊断/自愈走原 AgenticSRE 确定性诊断和自愈知识库链路。',
        skill: '当前诊断/自愈走 Agent+Skill：Agent 检索实时目录、选择精确 Skill ID，再通过 agenticsre_skill_run 调用。',
        compare: '当前诊断/自愈会同时运行原链路和 Agent+Skill 链路，用于迁移对比。',
    }[engine] || '';
    const common = [
            '<div><code>Dry-run</code>: 默认只生成注入/恢复命令，不改变业务。</div>',
            '<div><code>确认真实执行</code>: 取消 Dry-run 后必须勾选，才允许真实注入。</div>',
            '<div><code>实验</code>: 自动执行基线采样、注入、故障期采样和恢复。</div>',
            '<div><code>背景负载</code>: 勾选后 K8s 模式会在目标 Namespace 内启动有限时长的 vLLM 请求 Pod；Host 模式会用同一组并发、Prompt、Tokens 参数压测本机 endpoint。需要观察 TTFT/TPOT/E2E、队列、成功率和吞吐变化时建议保持开启。</div>',
            '<div><code>观测效果</code>: 没有业务请求时，故障注入通常只能体现为 Pod/K8s Event/GPU/节点状态变化，vLLM 请求时延和队列曲线可能保持 0。</div>',
            '<div><code>并发/持续秒/Prompt/Tokens/QPS</code>: 控制请求压力形态；QPS=0 表示不限速，由并发和服务响应速度决定。</div>',
            `<div><code>闭环引擎</code>: ${engineText}</div>`,
            '<div><code>诊断</code>: Agent 模式生成 RCA 摘要；Agent+Skill 模式由 Agent 调用 Skill 工具；对比模式两条链路都运行。</div>',
            '<div><code>自愈建议</code>: Agent 模式复用自愈中心知识库；Agent+Skill 模式展示 Agent 工具调用产生的 Skill 工作流。</div>',
        ];
    const modeHelp = mode === 'host'
        ? [
            '<div><code>GPU 主机</code>: 裸机 vLLM 所在节点，探测会检查 GPU、工具链和本机 vLLM endpoint。</div>',
            '<div><code>观测</code>: 无 Prometheus 时通过 SSH 直连读取 nvidia-smi、vLLM 进程和 endpoint。</div>',
            '<div><code>interface</code>: 网络故障默认使用 eth0，可在 API 请求中覆盖。</div>',
            '<div><code>endpoint</code>: Host 模式默认探测 http://127.0.0.1:8000/v1/completions。</div>',
            '<div><code>model_dir</code>: tokenizer/权重类故障需要模型目录；未填时相关场景会提示不可执行。</div>',
        ]
        : [
            '<div><code>namespace</code>: vLLM Deployment 所在命名空间。</div>',
            '<div><code>deployment</code>: 被注入故障的 vLLM 工作负载名称。</div>',
            '<div><code>service</code>: 背景负载访问的 vLLM Service；未填时默认等于 Deployment 名称。</div>',
            '<div><code>cluster</code>: 选择目标 K8s 集群配置，命令会自动带上 kubeconfig/context。</div>',
            '<div><code>采样参数</code>: 实验模式默认 baseline=2、fault=3、interval=3s。</div>',
        ];
    el.innerHTML = [...modeHelp, ...common].join('');
}

function renderLLMFaultSummary() {
    const el = document.getElementById('llmfaultlab-summary');
    if (!el) return;
    const layerText = (_llmFaultTool.layers || []).join(' / ') || '-';
    const scenarioCount = _llmFaultTool.scenario_count || _llmFaultScenarios.length || 0;
    const aiClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai');
    const aiHosts = (_faultTargets.llm_hosts || []).filter(h => h.ai_cluster_id || h.gpu || h.role);
    const selectedClusterId = document.getElementById('llmfaultlab-cluster')?.value || aiClusters[0]?.id || '';
    const selectedCluster = aiClusters.find(c => c.id === selectedClusterId) || aiClusters[0] || {};
    const topology = _lastLLMFaultTopologyData?.topology || {};
    const gpuCount = (topology.gpu_devices || []).length;
    const podCount = (topology.pods || []).length;
    const statusBadge = _llmFaultTool.available
        ? '<span class="badge badge-success">工具可用</span>'
        : '<span class="badge badge-warning">待部署工具</span>';
    el.innerHTML = `
        <div class="mini-metric llm-summary-card"><span><b class="metric-icon">OK</b>工具状态</span><strong>${statusBadge}</strong><small>支持 Dry-run、预检、注入、恢复和闭环诊断</small></div>
        <div class="mini-metric llm-summary-card"><span><b class="metric-icon">SC</b>内置场景</span><strong>${escapeHtml(scenarioCount)}</strong><small>覆盖 ${escapeHtml(layerText)} 层级</small></div>
        <div class="mini-metric llm-summary-card"><span><b class="metric-icon">CL</b>目标集群</span><strong>${escapeHtml(selectedCluster.name || selectedCluster.id || '-')}</strong><small>${escapeHtml(aiClusters.length)} 个智算集群 / ${escapeHtml(aiHosts.length)} 个 GPU 主机</small></div>
        <div class="mini-metric llm-summary-card"><span><b class="metric-icon">TP</b>依赖实体</span><strong>${escapeHtml(podCount)} Pod / ${escapeHtml(gpuCount)} GPU</strong><small>从 Prometheus、K8s API 和配置中心聚合</small></div>
    `;
}

function renderLLMFaultEnvironment() {
    const el = document.getElementById('llmfaultlab-env');
    if (!el) return;
    const env = _llmFaultTool.environment || {};
    const jump = env.jump_host || {};
    const targets = env.targets || [];
    const aiClusters = (_faultTargets.k8s_clusters || []).filter(c => String(c.kind || c.type || '').toLowerCase() === 'ai');
    const cluster = aiClusters.find(c => c.id === (document.getElementById('llmfaultlab-cluster')?.value || '')) || aiClusters[0] || {};
    el.innerHTML = `
        <div class="fault-env-head">
            <span class="badge badge-info">智算测试环境</span>
            <span class="text-muted">凭据仅运行时使用，不在系统中持久化；集群与主机来自配置中心</span>
        </div>
        <div class="fault-env-grid">
            <div class="fault-env-node env-node-cluster">
                <div class="env-node-icon">K8S</div>
                <strong>${escapeHtml(cluster.name || cluster.id || '智算 K8s')}</strong>
                <span>${escapeHtml(cluster.default_namespace || 'llm-k8s')} · ${escapeHtml(cluster.vllm_endpoint || 'vLLM endpoint 未配置')}</span>
                <small>${escapeHtml(cluster.prometheus_url || 'Prometheus 未配置')}</small>
            </div>
            <div class="fault-env-node env-node-jump">
                <div class="env-node-icon">SSH</div>
                <strong>跳板机</strong>
                <span>${escapeHtml(jump.user || '-')}@${escapeHtml(jump.host || '-')}</span>
                <small>${escapeHtml(jump.network || '')}</small>
            </div>
            ${targets.map(t => `<div class="fault-env-node">
                <div class="env-node-icon">GPU</div>
                <strong>${escapeHtml(t.name || 'T4')}</strong>
                <span>${escapeHtml(t.user || '-')}@${escapeHtml(t.host || '-')}</span>
                <small>${escapeHtml(t.gpu || '')} · ${escapeHtml(t.role || '')}</small>
            </div>`).join('')}
        </div>
    `;
}

function selectedLLMFaultScenario() {
    const id = document.getElementById('llmfaultlab-scenario')?.value;
    return _llmFaultScenarios.find(s => s.id === id);
}

function llmFaultRiskAckNeeded(s) {
    const severity = String(s?.severity || '').toLowerCase();
    return Boolean(s?.requires_risk_ack) || severity === 'critical' || severity === 'high';
}

function llmFaultExperimentRiskAckNeeded() {
    return _faultExperimentSteps.some(step => llmFaultRiskAckNeeded(step));
}

function llmFaultRequestBody(options = {}) {
    const mode = getLLMFaultMode();
    const dryRun = document.getElementById('llmfaultlab-dryrun')?.checked !== false;
    const confirmed = document.getElementById('llmfaultlab-confirm')?.checked === true;
    const action = options.action || 'inject';
    const scenario = options.scenario || selectedLLMFaultScenario();
    const realConfirmed = !dryRun && confirmed;
    const body = {
        dry_run: dryRun,
        target_mode: mode,
        confirm: realConfirmed,
        experiment_ack: realConfirmed && action === 'experiment',
        host_ack: realConfirmed && mode === 'host',
        risk_ack: realConfirmed && (options.riskAckNeeded ?? llmFaultRiskAckNeeded(scenario)),
    };
    const loadEnabled = document.getElementById('llmfaultlab-load')?.checked === true;
    body.background_load = loadEnabled;
    const modelValue = document.getElementById('llmfaultlab-model')?.value?.trim() || '';
    if (modelValue) body.model = modelValue;
    body.loadgen_duration_s = parseInt(document.getElementById('llmfaultlab-load-duration')?.value || '180', 10) || 180;
    body.loadgen_concurrency = parseInt(document.getElementById('llmfaultlab-load-concurrency')?.value || '8', 10) || 8;
    body.loadgen_prompt_len = parseInt(document.getElementById('llmfaultlab-load-prompt-len')?.value || '2048', 10) || 2048;
    body.loadgen_max_tokens = parseInt(document.getElementById('llmfaultlab-load-max-tokens')?.value || '128', 10) || 128;
    body.loadgen_qps = parseFloat(document.getElementById('llmfaultlab-load-qps')?.value || '0') || 0;
    if (mode === 'k8s') {
        const clusterId = document.getElementById('llmfaultlab-cluster')?.value || '';
        const namespace = document.getElementById('llmfaultlab-namespace')?.value || '';
        const deployment = document.getElementById('llmfaultlab-deployment')?.value || '';
        const service = document.getElementById('llmfaultlab-service')?.value || '';
        if (clusterId) body.cluster_id = clusterId;
        if (namespace) body.namespace = namespace;
        if (deployment) body.deployment = deployment;
        if (service) body.service = service;
    } else {
        body.host_id = document.getElementById('llmfaultlab-host')?.value || '';
        body.endpoint = document.getElementById('llmfaultlab-endpoint')?.value || 'http://127.0.0.1:8000/v1/completions';
        body.interface = document.getElementById('llmfaultlab-interface')?.value || 'eth0';
        body.model_dir = document.getElementById('llmfaultlab-model-dir')?.value || '';
        body.concurrent = body.loadgen_concurrency || parseInt(document.getElementById('llmfaultlab-concurrent')?.value || '16', 10);
        body.rounds = parseInt(document.getElementById('llmfaultlab-rounds')?.value || '2', 10);
        body.prompt_len = body.loadgen_prompt_len || parseInt(document.getElementById('llmfaultlab-prompt-len')?.value || '2048', 10);
        body.max_tokens = body.loadgen_max_tokens || parseInt(document.getElementById('llmfaultlab-max-tokens')?.value || '128', 10);
        body.recovery_timeout_s = parseInt(document.getElementById('llmfaultlab-recovery-timeout')?.value || '60', 10);
    }
    return body;
}

function llmFaultFormatError(err) {
    const msg = err?.message || String(err || '未知错误');
    return `失败: ${msg}`;
}

function llmFaultEnsureConfirmed(action, scenario, body) {
    if (body.dry_run) return true;
    if (!body.confirm) {
        alert('取消 Dry-run 后需要先勾选“确认真实执行”。');
        return false;
    }
    const actionLabel = action === 'experiment' ? '执行完整实验' : (action === 'cleanup' ? '执行恢复操作' : '执行故障注入');
    const target = body.target_mode === 'host'
        ? `host=${body.host_id || '-'}`
        : `namespace=${body.namespace || '-'}, deployment=${body.deployment || '-'}`;
    const runText = body.fault_run_id ? `\n故障运行: ${body.fault_run_id}` : '';
    return confirm(`${actionLabel}: ${llmFaultTypeLabel(scenario?.fault_type) || scenario?.id || ''}\n\n${llmDescriptionLabel(scenario)}\n\n目标: ${target}${runText}`);
}

function llmFaultStatusBadge(summary) {
    if (summary?.dry_run) return '<span class="badge badge-info">Dry-run</span>';
    if (summary?.ok) return '<span class="badge badge-success">已执行</span>';
    return '<span class="badge badge-danger">失败/需检查</span>';
}

function llmFaultDisplayValue(value) {
    if (value === undefined || value === null || value === '') return '-';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
}

function llmFaultResultLog(res) {
    const parts = [];
    if (res?.stdout) parts.push(`stdout:\n${res.stdout}`);
    if (res?.stderr) parts.push(`stderr:\n${res.stderr}`);
    const result = res?.result;
    if (result?.command) parts.push(`command:\n${result.command}`);
    if (result?.message) parts.push(`message:\n${result.message}`);
    if (Array.isArray(res?.results)) {
        res.results.forEach((r, idx) => {
            parts.push(`结果[${idx}]: ${llmLayerLabel(r?.layer) || llmFaultTypeLabel(r?.fault_type)} ${r?.success ? '成功' : '失败'} ${llmFaultDisplayValue(r?.message)}`);
            if (r?.command) parts.push(`command[${idx}]:\n${r.command}`);
        });
    }
    return parts.join('\n\n') || JSON.stringify(res || {}, null, 2);
}

function renderLLMFaultPreflight(res) {
    _lastLLMFaultPreflight = res?.preflight || res || null;
    const panel = document.getElementById('llmfaultlab-result');
    const out = document.getElementById('llmfaultlab-output');
    const pre = _lastLLMFaultPreflight || {};
    const blockers = pre.blockers || [];
    const warnings = pre.warnings || [];
    const params = pre.parameters || [];
    if (!panel) {
        if (out) out.textContent = JSON.stringify(res, null, 2);
        return;
    }
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-${pre.ok ? 'success' : 'warning'}">${pre.ok ? '预检通过' : '预检未通过'}</span>
                <strong>智算执行预检</strong>
                <span class="text-muted">${escapeHtml(pre.target_mode === 'host' ? '裸机 GPU 主机' : 'K8s 集群')} · ${escapeHtml(llmFaultTypeLabel(pre.fault_type))}</span>
            </div>
            <span class="text-muted">${pre.high_risk ? '高风险' : '常规风险'}</span>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>执行器支持</span><strong>${pre.supported ? '是' : '否'}</strong></div>
            <div class="fault-result-metric"><span>观测模式</span><strong>${escapeHtml(pre.observability?.mode || '-')}</strong></div>
            <div class="fault-result-metric"><span>层级</span><strong>${escapeHtml(llmLayerLabel(pre.layer))}</strong></div>
            <div class="fault-result-metric"><span>阻断项</span><strong>${blockers.length}</strong></div>
        </div>
        ${params.length ? `<div class="fault-result-section"><h4>执行参数</h4><ul>${params.map(p => `<li><code>${escapeHtml(p.name)}</code> ${p.required === 'true' ? '必填' : '可选'} · ${escapeHtml(p.description || '')}</li>`).join('')}</ul></div>` : ''}
        ${blockers.length ? `<div class="fault-result-section"><h4>阻断项</h4><ul>${blockers.map(b => `<li><strong>${escapeHtml(b.field || '-')}</strong>: ${escapeHtml(b.message || '')}</li>`).join('')}</ul></div>` : ''}
        ${warnings.length ? `<div class="fault-result-section"><h4>提示</h4><ul>${warnings.map(w => `<li><strong>${escapeHtml(w.field || '-')}</strong>: ${escapeHtml(w.message || '')}</li>`).join('')}</ul></div>` : ''}
        ${pre.command_preview ? `<div class="fault-result-section"><h4>命令预览</h4><div class="fault-result-code">${escapeHtml(pre.command_preview)}</div></div>` : ''}
        ${pre.recovery_preview ? `<div class="fault-result-section"><h4>恢复命令预览</h4><div class="fault-result-code">${escapeHtml(pre.recovery_preview)}</div></div>` : ''}
        ${(pre.observability?.signals || []).length ? `<div class="fault-result-section"><h4>观测信号</h4><ul>${pre.observability.signals.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul></div>` : ''}
    `;
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

async function preflightLLMFaultScenario(action = 'inject') {
    const s = selectedLLMFaultScenario();
    if (!s) return null;
    const out = document.getElementById('llmfaultlab-output');
    const body = llmFaultRequestBody({ action, scenario: s });
    body.action = action;
    if (out) out.textContent = '正在执行智算场景预检...';
    try {
        const res = await api(`/api/llm-faultlab/preflight/${s.id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        renderLLMFaultPreflight(res);
        return res?.preflight || null;
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
        return null;
    }
}

async function llmFaultEnsurePreflight(action, body) {
    const pre = await preflightLLMFaultScenario(action);
    if (!pre) return false;
    if (body.dry_run) return true;
    if (!pre.ok) {
        alert('预检未通过，不能真实执行。请先处理阻断项。');
        return false;
    }
    return true;
}

function renderLLMFaultResult(res) {
    _lastLLMFaultResult = res || null;
    const panel = document.getElementById('llmfaultlab-result');
    const out = document.getElementById('llmfaultlab-output');
    if (!panel) {
        if (out) out.textContent = JSON.stringify(res, null, 2);
        return;
    }
    const scenario = res?.scenario || selectedLLMFaultScenario() || {};
    const summary = res?.effect_summary || {
        ok: res?.status === 'executed' || res?.status === 'dry_run',
        dry_run: res?.status === 'dry_run',
        status: res?.status || '-',
        action: res?.action || '-',
        target: res?.target_mode === 'host' ? (res?.host?.id || res?.host?.host || '-') : [res?.cluster_id || 'default', res?.namespace || '-', res?.deployment || '-'].join('/'),
        fault_type: res?.fault_type || scenario?.fault_type || '-',
        layer: res?.layer || scenario?.layer || '-',
        outcome: res?.status === 'dry_run' ? 'Dry-run 已生成命令，尚未真实注入。' : '命令已返回，请结合日志和指标确认效果。',
        message: res?.message || res?.result?.message || '',
        expected_detection_time_s: scenario?.expected_detection_time_s,
        observation_hints: scenario?.expected_symptoms || [],
    };
    const hints = summary.observation_hints || [];
    const command = res?.remote_command || res?.recovery_command || res?.command || res?.result?.command || '';
    const recovery = res?.recovery_verification || null;
    const eventDetection = res?.ai_event_detection || null;
    const detectedEvents = Array.isArray(eventDetection?.events) ? eventDetection.events : [];
    const recoveryLast = recovery?.last || {};
    const loadgen = res?.loadgen || null;
    const clusterId = res?.cluster_id || (getLLMFaultMode() === 'k8s' ? (document.getElementById('llmfaultlab-cluster')?.value || '') : '');
    const observeDuration = loadgen?.duration_s && Number(loadgen.duration_s) >= 900 ? '30m' : '15m';
    const observableSignals = [
        '运行中请求 / 等待队列',
        '成功请求速率',
        'TTFT / TPOT / E2E P95',
        'GPU 利用率 / 显存 / 温度',
        'K8s Event 与 Pod 状态',
    ];
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                ${llmFaultStatusBadge(summary)}
                <strong>${escapeHtml(llmFaultTypeLabel(scenario?.fault_type) || scenario?.id || '智算故障')}</strong>
                <span class="text-muted">${escapeHtml(summary.outcome || '')}</span>
            </div>
            <span class="text-muted">${escapeHtml(summary.status || '')}</span>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>目标</span><strong>${escapeHtml(llmFaultDisplayValue(summary.target))}</strong></div>
            <div class="fault-result-metric"><span>动作</span><strong>${escapeHtml(llmFaultDisplayValue(summary.action))}</strong></div>
            <div class="fault-result-metric"><span>层级 / 类型</span><strong>${escapeHtml(`${llmLayerLabel(summary.layer)} / ${llmFaultTypeLabel(summary.fault_type)}`)}</strong></div>
            <div class="fault-result-metric"><span>预计可观测时间</span><strong>${escapeHtml(summary.expected_detection_time_s ? `${summary.expected_detection_time_s}s` : '-')}</strong></div>
        </div>
        ${summary.message ? `<div class="fault-result-section"><h4>执行反馈</h4><div class="fault-result-code">${escapeHtml(summary.message)}</div></div>` : ''}
        ${eventDetection ? `<div class="fault-result-section"><h4>智算事件告警</h4>
            <div class="fault-observe-callout ${eventDetection.triggered ? '' : 'warning'}">
                <strong>${eventDetection.triggered ? `已自动检测 · ${escapeHtml(llmFaultDisplayValue(eventDetection.active_count))} 条活跃事件` : '自动检测未完成'}</strong>
                <span>${eventDetection.triggered
                    ? escapeHtml(detectedEvents.slice(0, 3).map(event => `${event.severity || 'info'}: ${event.title || event.event_type || '-'}`).join('；') || '当前观测未达到已配置告警阈值。')
                    : escapeHtml(eventDetection.message || '可在事件中心重新执行检测。')}</span>
                ${eventDetection.follow_up?.scheduled ? `<span class="text-muted">将在 ${escapeHtml(llmFaultDisplayValue(eventDetection.follow_up.delay_s))} 秒后按场景可观测时间自动复检。</span>` : ''}
                ${clusterId ? `<button class="btn btn-xs btn-primary" onclick="openAIEventsForCluster('${escapeHtmlAttr(clusterId)}')">查看事件中心</button>` : ''}
            </div>
        </div>` : ''}
        ${recovery ? `<div class="fault-result-section"><h4>恢复验证</h4><ul>
            <li>状态: ${recovery.recovered ? '<span class="badge badge-success">已恢复</span>' : '<span class="badge badge-warning">等待/未确认</span>'}</li>
            <li>队列: running=${escapeHtml(llmFaultDisplayValue(recoveryLast.running))}, waiting=${escapeHtml(llmFaultDisplayValue(recoveryLast.waiting))}</li>
            <li>轮询: ${escapeHtml(llmFaultDisplayValue(recovery.attempts))} 次 / ${escapeHtml(llmFaultDisplayValue(recovery.timeout_s))}s</li>
        </ul></div>` : ''}
        ${loadgen ? `<div class="fault-result-section"><h4>背景负载</h4><ul>
            <li>状态: ${loadgen.enabled ? '<span class="badge badge-info">已启用</span>' : '<span class="badge badge-muted">未启用</span>'}</li>
            <li>目标: ${escapeHtml(loadgen.namespace || '-')}/${escapeHtml(loadgen.service || '-')} · ${escapeHtml(loadgen.model || '-')}</li>
            <li>形态: 并发 ${escapeHtml(llmFaultDisplayValue(loadgen.concurrency))}, ${escapeHtml(llmFaultDisplayValue(loadgen.duration_s))}s, prompt ${escapeHtml(llmFaultDisplayValue(loadgen.prompt_len))}, tokens ${escapeHtml(llmFaultDisplayValue(loadgen.max_tokens))}, QPS ${escapeHtml(llmFaultDisplayValue(loadgen.qps))}</li>
        </ul>
        <div class="fault-observe-callout">
            <strong>观测建议</strong>
            <span>负载启动后在智算观测查看 ${observableSignals.map(escapeHtml).join('、')}；故障期和恢复期的差异会体现在同一时间窗口内。</span>
            ${clusterId ? `<button class="btn btn-xs btn-primary" onclick="openAIDashboardForCluster('${escapeHtmlAttr(clusterId)}','${escapeHtmlAttr(observeDuration)}')">查看智算观测</button>` : ''}
        </div>
        ${(loadgen.commands || []).length ? `<div class="fault-result-code">${escapeHtml((loadgen.commands || []).join('\n'))}</div>` : ''}
        ${(loadgen.log_commands || []).length ? `<div class="fault-result-code">${escapeHtml((loadgen.log_commands || []).join('\n'))}</div>` : ''}</div>` : ''}
        ${!loadgen ? `<div class="fault-result-section fault-observe-callout warning">
            <strong>业务曲线提示</strong>
            <span>本次结果没有背景负载记录。若没有外部请求，vLLM 请求数、队列、TTFT/TPOT/E2E 和吞吐曲线可能不会明显变化；仍可观察 K8s Event、Pod 状态和 GPU/节点指标。</span>
            ${clusterId ? `<button class="btn btn-xs" onclick="openAIDashboardForCluster('${escapeHtmlAttr(clusterId)}','15m')">查看智算观测</button>` : ''}
        </div>` : ''}
        ${hints.length ? `<div class="fault-result-section"><h4>观察项</h4><ul>${hints.map(x => `<li>${escapeHtml(llmSymptomLabel(x))}</li>`).join('')}</ul></div>` : ''}
        ${command ? `<div class="fault-result-section"><h4>${summary.dry_run ? '将执行的命令' : '已执行/恢复命令'}</h4><div class="fault-result-code">${escapeHtml(command)}</div></div>` : ''}
    `;
    if (out) out.textContent = llmFaultResultLog(res);
}

async function openAIEventsForCluster(clusterId) {
    switchView('aievents');
    await loadFaultTargets();
    if (clusterId) setVal('ai-events-cluster', clusterId);
    await loadAIEvents();
}

function renderLLMHostProbeResult(res) {
    const panel = document.getElementById('llmfaultlab-result');
    const out = document.getElementById('llmfaultlab-output');
    if (!panel) return;
    const tools = res?.tools || {};
    const caps = res?.capability || {};
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                ${res?.ok ? '<span class="badge badge-success">主机可达</span>' : '<span class="badge badge-danger">主机不可达</span>'}
                <strong>GPU 主机探测</strong>
                <span class="text-muted">${escapeHtml(res?.hostname || '')}</span>
            </div>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>GPU</span><strong>${escapeHtml((res?.gpu || []).join('; ') || '-')}</strong></div>
            <div class="fault-result-metric"><span>工具链</span><strong>${escapeHtml(Object.entries(tools).map(([k, v]) => `${k}:${v ? 'yes' : 'no'}`).join(' ') || '-')}</strong></div>
            <div class="fault-result-metric"><span>故障能力</span><strong>${escapeHtml(Object.entries(caps).map(([k, v]) => `${k}:${v ? 'yes' : 'no'}`).join(' ') || '-')}</strong></div>
            <div class="fault-result-metric"><span>网卡</span><strong>${escapeHtml((res?.interfaces || []).slice(0, 6).join(' ') || '-')}</strong></div>
        </div>
        <div class="fault-result-section"><h4>下一步</h4><ul>
            <li>endpoint_probe=false 时，说明当前主机没有可用的本机 vLLM HTTP endpoint。</li>
            <li>执行真实注入前，先确认目标场景需要的工具链和业务服务均可用。</li>
        </ul></div>
    `;
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

function renderLLMHostObservation(res) {
    _lastLLMHostObservation = res || null;
    const panel = document.getElementById('llmfaultlab-result');
    const out = document.getElementById('llmfaultlab-output');
    const obs = res?.observation || {};
    if (!panel) {
        if (out) out.textContent = JSON.stringify(res, null, 2);
        return;
    }
    const gpus = obs.gpus || [];
    const processes = obs.processes || [];
    const metrics = obs.metrics || {};
    const metricKeys = Object.keys(metrics);
    const gpuRows = gpus.length ? gpus.map(g => `
        <div class="fault-result-metric">
            <span>GPU ${escapeHtml(g.index ?? '')} · ${escapeHtml(g.name || '')}</span>
            <strong>${escapeHtml(llmFaultDisplayValue(g.utilization_gpu_pct))}% / ${escapeHtml(llmFaultDisplayValue(g.memory_used_mb))}MiB</strong>
            <span>${escapeHtml(llmFaultDisplayValue(g.power_watts))}W · ${escapeHtml(llmFaultDisplayValue(g.temperature_c))}C · ${escapeHtml(llmFaultDisplayValue(g.clock_mhz))}MHz</span>
        </div>
    `).join('') : '<div class="fault-result-metric"><span>GPU</span><strong>未采集到 nvidia-smi 数据</strong></div>';
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                ${res?.ok ? '<span class="badge badge-success">直连观测完成</span>' : '<span class="badge badge-danger">直连观测失败</span>'}
                <strong>${escapeHtml(obs.hostname || res?.host?.name || 'GPU 主机')}</strong>
                <span class="text-muted">无需 Prometheus，采集源: SSH + nvidia-smi + curl</span>
            </div>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>endpoint</span><strong>${escapeHtml(obs.endpoint_status || 'unknown')}</strong></div>
            <div class="fault-result-metric"><span>vLLM /metrics</span><strong>${escapeHtml(obs.metrics_status || 'unknown')}</strong></div>
            <div class="fault-result-metric"><span>vLLM 进程</span><strong>${processes.length}</strong></div>
            <div class="fault-result-metric"><span>采集模式</span><strong>host_direct</strong></div>
        </div>
        <div class="fault-result-section"><h4>GPU 实时状态</h4><div class="fault-result-grid">${gpuRows}</div></div>
        ${processes.length ? `<div class="fault-result-section"><h4>vLLM 进程</h4><ul>${processes.map(p => `<li>${escapeHtml(p)}</li>`).join('')}</ul></div>` : '<div class="fault-result-section"><h4>vLLM 进程</h4><ul><li>未发现 vLLM/api_server 进程；业务侧效果需要先启动推理服务。</li></ul></div>'}
        ${metricKeys.length ? `<div class="fault-result-section"><h4>vLLM 原生指标</h4><ul>${metricKeys.map(k => `<li>${escapeHtml(k)} = ${escapeHtml(llmFaultDisplayValue(metrics[k]))}</li>`).join('')}</ul></div>` : '<div class="fault-result-section"><h4>vLLM 原生指标</h4><ul><li>当前主机 /metrics 不可用；前端将使用 GPU/进程/endpoint 作为兜底反馈。</li></ul></div>'}
        <div class="fault-result-section"><h4>故障效果判断</h4><ul>
            <li>注入前后重复点击“观测”，对比 GPU 利用率、显存、进程数、endpoint 和 /metrics 状态。</li>
            <li>如果 endpoint 或 /metrics 不可用，只能证明故障命令执行和主机资源变化，不能证明业务请求侧 SLA 变化。</li>
        </ul></div>
    `;
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

function llmFaultContextPayload() {
    const s = selectedLLMFaultScenario() || {};
    const body = llmFaultRequestBody({ action: 'diagnose', scenario: s });
    const result = _lastLLMFaultResult || {};
    const observation = _lastLLMHostObservation?.observation || result?.observation || null;
    return {
        ...body,
        scenario_id: s.id || result?.scenario?.id || '',
        fault_run_id: result?.fault_run_id || '',
        fault_type: s.fault_type || result?.fault_type || '',
        effect_summary: result?.effect_summary || {},
        result,
        observation,
    };
}

function llmFaultSkillFaultType(s = selectedLLMFaultScenario() || {}, result = _lastLLMFaultResult || {}) {
    const raw = String(s.fault_type || result.fault_type || '').toLowerCase();
    if (raw === 'process_crash' || raw.includes('crash')) return 'vllm_process_crash';
    if (raw.includes('timeout') || raw.includes('latency') || raw.includes('queue')) return 'endpoint_timeout';
    if (raw.includes('oom') || raw.includes('memory') || raw.includes('kv_cache')) return 'gpu_oom';
    return raw || 'endpoint_timeout';
}

function llmFaultSkillWorkflowBody(stages = ['detect', 'diagnose', 'heal', 'verify']) {
    const s = selectedLLMFaultScenario() || {};
    const ctx = llmFaultContextPayload();
    const result = ctx.result || {};
    const observation = ctx.observation || {};
    const metrics = observation.metrics || result.metrics || result.effect_summary?.metrics || {};
    const mode = ctx.target_mode || getLLMFaultMode();
    const targetText = mode === 'host'
        ? (ctx.host_id || observation.hostname || 'gpu-host')
        : [ctx.cluster_id || 'default', ctx.namespace || 'default', ctx.deployment || 'vllm-server'].join('/');
    return {
        domain: 'llm',
        stages,
        dry_run: true,
        allow_legacy_fallback: true,
        payload: {
            ...ctx,
            source: 'llm_faultlab',
            domain: 'llm',
            scenario_id: s.id || ctx.scenario_id || '',
            scenario_name: s.name || s.id || '',
            fault_type: llmFaultSkillFaultType(s, result),
            raw_fault_type: s.fault_type || result.fault_type || '',
            layer: s.layer || result.layer || '',
            severity: s.severity || 'warning',
            message: [
                `智算故障实验 ${s.id || ''}`,
                llmFaultTypeLabel(s.fault_type || result.fault_type || ''),
                s.description || result.message || '',
                `target=${targetText}`,
            ].filter(Boolean).join(' | '),
            root_cause_component: ctx.deployment || ctx.host_id || result.deployment || 'vllm-server',
            namespace: ctx.namespace || 'llm-k8s',
            deployment: ctx.deployment || 'vllm-server',
            service: ctx.service || 'vllm-service',
            host_id: ctx.host_id || '',
            endpoint: ctx.endpoint || observation.endpoint || '',
            model: ctx.model || '',
            cluster_id: ctx.cluster_id || '',
            target_mode: mode,
            metrics,
            endpoint_status: observation.endpoint_status || result.endpoint_status || '',
            metrics_status: observation.metrics_status || result.metrics_status || '',
            metrics_count: Object.keys(metrics || {}).length,
            gpu_count: (observation.gpus || []).length || result.gpu_count || 0,
            process_count: (observation.processes || []).length || result.process_count || 0,
            expected_symptoms: s.expected_symptoms || [],
            expected_detection_time_s: s.expected_detection_time_s,
            diagnosis: _lastLLMFaultDiagnosis || undefined,
        },
    };
}

async function runLLMFaultSkillWorkflow(stages = ['detect', 'diagnose', 'heal', 'verify'], stage = 'healing') {
    const s = selectedLLMFaultScenario();
    if (!s) return null;
    const out = document.getElementById('llmfaultlab-output');
    const body = llmFaultSkillWorkflowBody(stages);
    const payload = body.payload || {};
    const query = `[智算故障实验][${payload.fault_type || s.id}] ${payload.message || s.description || s.name || s.id}; namespace=${payload.namespace || 'llm-k8s'}; deployment=${payload.deployment || 'vllm-server'}; endpoint=${payload.endpoint || ''}`;
    const result = await runAgentSkillDiagnosis(query, payload.namespace || 'llm-k8s', 'llmfaultlab-output', { skillWorkflow: body });
    _lastLLMFaultSkillWorkflow = result?.skillEvent?.result || null;
    renderAgentSkillDiagnosisResult('llmfaultlab-closure', result, stage === 'analysis' ? '智算 Agent+Skill 诊断' : '智算 Agent+Skill 闭环建议');
    return result;
}

function renderLLMFaultSkillClosure(res = {}, options = {}) {
    const panel = document.getElementById('llmfaultlab-closure');
    const out = document.getElementById('llmfaultlab-output');
    if (!panel) return;
    const diagnosis = res.diagnosis || {};
    const runtime = res.skill_runtime || {};
    const migration = res.migration || {};
    const plan = res.execution_plan || {};
    const capability = res.capability || {};
    const checks = res.verification_plan?.checks || [];
    const commands = plan.commands || [];
    const suggestions = plan.suggestions || [];
    const stageLabel = options.stage === 'analysis' ? 'Skill 诊断' : 'Skill 自愈闭环';
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-info">${escapeHtml(stageLabel)}</span>
                <strong>智算 Skill 调用结果</strong>
                <span class="text-muted">置信度=${escapeHtml(formatConfidence(diagnosis.confidence))}</span>
            </div>
            <span class="text-muted">${escapeHtml(skillMigrationModeLabel(migration.mode || 'skill_primary'))}</span>
        </div>
        <div class="skill-status-strip llm-skill-status">
            <div><span>主 Skill</span><strong>${escapeHtml(runtime.primary_skill_id || diagnosis.skill_id || '-')}</strong></div>
            <div><span>匹配 Skill</span><strong>${escapeHtml(runtime.matched_count ?? '-')}</strong></div>
            <div><span>自愈门禁</span><strong>${capability.heal_ready ? '通过' : '受限'}</strong></div>
            <div><span>动作 / 验证</span><strong>${escapeHtml((plan.command_count ?? commands.length ?? 0) + ' / ' + checks.length)}</strong></div>
        </div>
        <div class="skill-result-grid">
            ${renderSkillDiagnosisCard(diagnosis)}
            ${renderSkillCapabilityCard(capability)}
            ${renderSkillExecutionCard(plan, suggestions, commands)}
            ${renderSkillVerificationCard(res.verification_plan || {}, checks)}
        </div>
        ${renderAlertSkillExecutionProcess(res, ['detect', 'diagnose', 'heal', 'verify'], { open: true, showMissing: true })}
        ${renderSkillMigrationNotice(migration)}
        <div class="fault-closure-actions">
            <button class="btn btn-sm btn-primary" onclick="openLLMFaultSkillHealPlan()">打开 Skill 自愈计划</button>
            <button class="btn btn-sm" onclick="switchView('skillops')">查看 Skill 调用记录</button>
            <button class="btn btn-sm" onclick="evaluateLLMFaultRun()">生成评测摘要</button>
        </div>
        ${renderSkillTrace(res.trace || {}, runtime)}
    `;
    if (out) {
        out.textContent = JSON.stringify({
            status: res.status,
            domain: res.domain,
            diagnosis,
            capability,
            execution_plan: plan,
            verification_plan: res.verification_plan,
            migration,
            dashboard_bridge: res.dashboard_bridge,
        }, null, 2);
    }
}

function renderLLMFaultCompareClosure(agentData = {}, skillData = {}) {
    const panel = document.getElementById('llmfaultlab-closure');
    if (!panel) return;
    const agentDiagnosis = agentData.diagnosis || {};
    const skillDiagnosis = skillData.diagnosis || {};
    const skillRuntime = skillData.skill_runtime || {};
    const skillPlan = skillData.execution_plan || {};
    const agentSuggestions = agentData.heal?.suggestions || [];
    const skillSuggestions = skillPlan.suggestions || skillPlan.commands || [];
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-info">对比模式</span>
                <strong>智算诊断/自愈双链路对比</strong>
            </div>
            <span class="text-muted">AgenticSRE vs Agent+Skill</span>
        </div>
        <div class="alert-compare-grid">
            <div class="skill-result-card">
                <div class="skill-card-head"><strong>智能体/知识库链路</strong><span class="badge badge-info">${escapeHtml(formatConfidence(agentDiagnosis.confidence))}</span></div>
                <p>${escapeZh(agentDiagnosis.root_cause || '暂无诊断结论')}</p>
                <div class="skill-kv"><span>故障类型</span><strong>${escapeHtml(llmFaultTypeLabel(agentDiagnosis.fault_type) || agentDiagnosis.fault_type || '-')}</strong></div>
                <div class="skill-kv"><span>自愈动作</span><strong>${escapeHtml(agentSuggestions.length)}</strong></div>
            </div>
            <div class="skill-result-card">
                <div class="skill-card-head"><strong>Agent+Skill 链路</strong><span class="badge badge-info">${escapeHtml(formatConfidence(skillDiagnosis.confidence))}</span></div>
                <p>${escapeZh(skillDiagnosis.root_cause || skillDiagnosis.summary || '暂无诊断结论')}</p>
                <div class="skill-kv"><span>主 Skill</span><strong>${escapeHtml(skillRuntime.primary_skill_id || skillDiagnosis.skill_id || '-')}</strong></div>
                <div class="skill-kv"><span>自愈动作</span><strong>${escapeHtml(skillSuggestions.length || skillPlan.command_count || 0)}</strong></div>
            </div>
        </div>
        ${renderSkillTrace(skillData.trace || {}, skillRuntime)}
        <div class="fault-closure-actions">
            <button class="btn btn-sm" onclick="openLLMFaultHealPlan()">打开知识库自愈计划</button>
            <button class="btn btn-sm btn-primary" onclick="openLLMFaultSkillHealPlan()">打开 Skill 自愈计划</button>
        </div>
    `;
}

function renderLLMFaultClosure(data) {
    const panel = document.getElementById('llmfaultlab-closure');
    const out = document.getElementById('llmfaultlab-output');
    if (!panel) {
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return;
    }
    const diagnosis = data?.diagnosis || _lastLLMFaultDiagnosis || {};
    const heal = data?.heal || _lastLLMFaultHeal || null;
    const suggestions = heal?.suggestions || [];
    const blocked = heal?.blocked || [];
    const gate = heal?.diagnosis_gate || {};
    const capability = heal?.capability || {};
    const k8sPlanAvailable = heal && (heal.mode !== 'host') && Array.isArray(heal.suggestions);
    _lastLLMFaultEvaluation = data?.evaluation || _lastLLMFaultEvaluation;
    panel.style.display = '';
    panel.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-${diagnosis.diagnosis_ready ? 'success' : 'warning'}">诊断${diagnosis.diagnosis_ready ? '可执行' : '待确认'}</span>
                <strong>智算诊断与自愈闭环</strong>
                <span class="text-muted">置信度=${escapeHtml(formatConfidence(diagnosis.confidence))}</span>
            </div>
            <span class="text-muted">${escapeHtml(diagnosisModeLabel(diagnosis.diagnosis_mode || 'llm_faultlab'))}</span>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>根因类型</span><strong>${escapeHtml(llmFaultTypeLabel(diagnosis.fault_type || '-'))}</strong></div>
            <div class="fault-result-metric"><span>K8s 映射</span><strong>${escapeHtml(k8sFaultLabel(diagnosis.k8s_fault_type || '-'))}</strong></div>
            <div class="fault-result-metric"><span>根因组件</span><strong>${escapeHtml(diagnosis.root_cause_component || '-')}</strong></div>
            <div class="fault-result-metric"><span>诊断门禁</span><strong>${escapeHtml(gate.ready === undefined ? (diagnosis.diagnosis_ready ? '通过' : '待确认') : (gate.ready ? '通过' : '未通过'))}</strong></div>
        </div>
        <div class="fault-result-section"><h4>诊断结论</h4><div class="fault-result-code">${escapeHtml(diagnosis.root_cause || '-')}</div></div>
        ${renderConfidenceExplanation(diagnosis.confidence_explanation)}
        ${(diagnosis.evidence || []).length ? `<div class="fault-result-section"><h4>证据</h4><ul>${diagnosis.evidence.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul></div>` : ''}
        ${heal ? `<div class="fault-result-section"><h4>自愈建议</h4><ul>${suggestions.map(x => `<li><strong>${escapeHtml(x.step || '-')}</strong> · ${escapeHtml(riskLabel(x.risk))} · ${escapeHtml(x.selection_reason || x.action || '')}</li>`).join('') || '<li>未生成可执行建议。</li>'}</ul></div>` : ''}
        ${blocked.length ? `<div class="fault-result-section"><h4>阻断项</h4><ul>${blocked.map(x => `<li>${escapeHtml(x.reason || x.command || JSON.stringify(x))}</li>`).join('')}</ul></div>` : ''}
        ${capability.real_execution_endpoint ? `<div class="fault-result-section"><h4>执行约束</h4><ul>
            <li>裸机恢复不接受任意命令，只能通过当前场景“恢复”按钮走确认和风险门禁。</li>
            <li>需要 host_ack${capability.requires_risk_ack ? ' 和 risk_ack' : ''}，执行前建议先 Dry-run。</li>
        </ul></div>` : ''}
        <div class="fault-closure-actions">
            ${k8sPlanAvailable ? '<button class="btn btn-sm btn-primary" onclick="openLLMFaultHealPlan()">打开自愈计划</button>' : ''}
            <button class="btn btn-sm" onclick="evaluateLLMFaultRun()">生成评测摘要</button>
        </div>
        <div id="llmfaultlab-evaluation">${_lastLLMFaultEvaluation ? renderLLMFaultEvaluation(_lastLLMFaultEvaluation) : ''}</div>
    `;
    if (out) out.textContent = JSON.stringify(data, null, 2);
}

function renderLLMFaultEvaluation(evaluation) {
    const metrics = evaluation?.metrics || {};
    const diagnosis = evaluation?.diagnosis || {};
    const injection = evaluation?.fault_injection || {};
    const recovery = evaluation?.recovery || {};
    const resultJson = evaluation?.result_json || {};
    const metricItems = [
        ['诊断通过率', metrics.diagnosis_pass_rate, 'ratio'],
        ['注入成功率', metrics.fault_injection_success_rate, 'ratio'],
        ['恢复成功率', metrics.recovery_success_rate, 'ratio'],
        ['覆盖率', metrics.evaluation_coverage, 'ratio'],
        ['平均轮次', metrics.avg_turns, 'number'],
        ['平均时延', metrics.avg_latency_s, 'seconds'],
        ['错误率', metrics.error_rate, 'ratio'],
    ];
    const formatMetric = (value, kind) => {
        const num = Number(value || 0);
        if (kind === 'ratio') return `${Math.round(num * 100)}%`;
        if (kind === 'seconds') return `${num.toFixed(2)}s`;
        return `${num}`;
    };
    return `
        <div class="fault-result-section">
            <h4>AISHPerf 风格评测摘要</h4>
            <div class="fault-result-grid">
                ${metricItems.map(([label, value, kind]) => `<div class="fault-result-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatMetric(value, kind))}</strong></div>`).join('')}
            </div>
            <ul>
                <li>诊断: ${diagnosis.passed ? '<span class="badge badge-success">通过</span>' : '<span class="badge badge-warning">未通过/待证实</span>'} · ${escapeHtml(diagnosis.reason || '')}</li>
                <li>注入: ${injection.passed ? '<span class="badge badge-success">通过</span>' : '<span class="badge badge-warning">未计入</span>'} · ${escapeHtml(injection.reason || '')}</li>
                <li>恢复: ${recovery.passed ? '<span class="badge badge-success">通过</span>' : '<span class="badge badge-warning">未验证</span>'} · ${escapeHtml(recovery.reason || '')}</li>
            </ul>
            <div class="fault-closure-actions">
                <button class="btn btn-sm" onclick="archiveLLMFaultEvaluation()">归档评测结果</button>
            </div>
            <div class="fault-result-code">${escapeHtml(JSON.stringify(resultJson, null, 2))}</div>
        </div>
    `;
}

async function evaluateLLMFaultRun() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    const payload = {
        ...llmFaultContextPayload(),
        diagnosis: _lastLLMFaultDiagnosis || undefined,
        heal: _lastLLMFaultHeal || undefined,
    };
    if (out) out.textContent = '正在生成 AISHPerf 风格评测摘要...';
    try {
        const res = await api(`/api/llm-faultlab/evaluate/${s.id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        _lastLLMFaultEvaluation = res;
        renderLLMFaultClosure({
            status: 'evaluated',
            diagnosis: _lastLLMFaultDiagnosis || res?.diagnosis || {},
            heal: _lastLLMFaultHeal || null,
            evaluation: res,
        });
        if (out) out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function archiveLLMFaultEvaluation() {
    const s = selectedLLMFaultScenario();
    if (!s || !_lastLLMFaultEvaluation) return;
    const out = document.getElementById('llmfaultlab-output');
    const payload = {
        ...llmFaultContextPayload(),
        diagnosis: _lastLLMFaultDiagnosis || undefined,
        heal: _lastLLMFaultHeal || undefined,
        run_id: _lastLLMFaultEvaluation.run_id,
    };
    if (out) out.textContent = '正在归档 AISHPerf 风格评测结果...';
    try {
        const res = await api(`/api/llm-faultlab/evaluate/${s.id}/archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        const archive = res?.archive || {};
        const evaluationEl = document.getElementById('llmfaultlab-evaluation');
        if (evaluationEl && _lastLLMFaultEvaluation) {
            evaluationEl.innerHTML = renderLLMFaultEvaluation(_lastLLMFaultEvaluation)
                + `<div class="fault-result-section"><h4>归档结果</h4><ul>
                    <li>Run ID: <code>${escapeHtml(archive.id || '')}</code></li>
                    <li>Artifact: <code>${escapeHtml(archive.artifact_dir || '')}</code></li>
                    <li>状态: ${escapeHtml(archive.status || '')}</li>
                </ul></div>`;
        }
        if (out) out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function diagnoseLLMFaultScenario() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const engine = getLLMFaultEngine();
    if (engine === 'skill') {
        return runLLMFaultSkillWorkflow(['detect', 'diagnose'], 'analysis');
    }
    const out = document.getElementById('llmfaultlab-output');
    if (out) out.textContent = '正在生成智算诊断...';
    try {
        const res = await api(`/api/llm-faultlab/diagnose/${s.id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(llmFaultContextPayload()),
            throwOnError: true,
        });
        _lastLLMFaultDiagnosis = res?.diagnosis || null;
        if (engine === 'compare') {
            const skillRes = await runLLMFaultSkillWorkflow(['detect', 'diagnose'], 'analysis');
            renderLLMFaultCompareClosure(res, skillRes || {});
        } else {
            renderLLMFaultClosure(res);
        }
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function suggestLLMFaultHeal() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const engine = getLLMFaultEngine();
    if (engine === 'skill') {
        return runLLMFaultSkillWorkflow(['detect', 'diagnose', 'heal', 'verify'], 'healing');
    }
    const out = document.getElementById('llmfaultlab-output');
    if (!_lastLLMFaultDiagnosis) {
        await diagnoseLLMFaultScenario();
    }
    const payload = {
        ...llmFaultContextPayload(),
        diagnosis: _lastLLMFaultDiagnosis || undefined,
    };
    if (out) out.textContent = '正在生成智算自愈建议...';
    try {
        const res = await api(`/api/llm-faultlab/heal/${s.id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        _lastLLMFaultDiagnosis = res?.diagnosis || _lastLLMFaultDiagnosis;
        _lastLLMFaultHeal = res?.heal || null;
        _lastLLMFaultHealPayload = res?.heal_payload || null;
        if (engine === 'compare') {
            const skillRes = await runLLMFaultSkillWorkflow(['detect', 'diagnose', 'heal', 'verify'], 'healing');
            renderLLMFaultCompareClosure(res, skillRes || {});
        } else {
            renderLLMFaultClosure(res);
        }
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

function openLLMFaultHealPlan() {
    if (!_lastLLMFaultHeal || !_lastLLMFaultDiagnosis) return;
    const payload = _lastLLMFaultHealPayload || {
        ...llmFaultContextPayload(),
        diagnosis: _lastLLMFaultDiagnosis,
        source: 'llm_faultlab',
    };
    openHealPlanModal(_lastLLMFaultHeal, payload, { source: 'llm-faultlab' });
}

function openLLMFaultSkillHealPlan() {
    const res = _lastLLMFaultSkillWorkflow;
    if (!res) {
        showToast('请先运行 Skill 诊断或 Skill 自愈', 'warning');
        return;
    }
    const plan = res.execution_plan || {};
    const payload = llmFaultSkillWorkflowBody(['detect', 'diagnose', 'heal', 'verify']).payload;
    const healPlan = {
        ...plan,
        suggestions: plan.suggestions || [],
        blocked_templates: res.capability?.heal_blockers || [],
        capability: res.capability || {},
        diagnosis: res.diagnosis || {},
        diagnosis_gate: {
            ready: res.capability?.diagnosis_ready,
            confidence: res.diagnosis?.confidence || res.capability?.confidence,
            confidence_threshold: res.capability?.confidence_threshold,
            blocker_messages: res.capability?.heal_blocker_messages || [],
        },
        verification_plan: res.verification_plan || {},
        target: payload,
        fault_type: res.diagnosis?.fault_type || payload.fault_type,
        namespace: payload.namespace,
        policy: { dry_run: res.dry_run !== false, recommend_only: true },
    };
    openHealPlanModal(healPlan, { ...payload, diagnosis: res.diagnosis, source: 'llm_faultlab_skill' }, { source: 'llm-faultlab-skill' });
}

function renderLLMFaultScenarioDetail() {
    const s = selectedLLMFaultScenario();
    const el = document.getElementById('llmfaultlab-scenario-detail');
    if (!el) return;
    if (!s) {
        el.innerHTML = `<div class="fault-detail">
            <div><span class="badge badge-warning">未加载</span></div>
            <p>未找到 vLLM 故障工具场景文件。请在集群节点设置 VLLM_FAULT_INJECTOR_DIR，或部署 vllm_fault_injector 工具包。</p>
            <pre>${escapeHtml(_llmFaultTool.scenarios_file || '')}</pre>
        </div>`;
        return;
    }
    const symptoms = s.expected_symptoms || [];
    el.innerHTML = `<div class="fault-detail">
        <div>
            <span class="badge badge-info">${escapeHtml(llmLayerLabel(s.layer))}</span>
            <span class="badge badge-warning">${escapeHtml(llmFaultTypeLabel(s.fault_type))}</span>
            <span class="badge badge-${s.severity === 'critical' ? 'danger' : 'info'}">${escapeHtml(severityLabel(s.severity))}</span>
            <strong>${escapeHtml(llmFaultTypeLabel(s.fault_type) || s.name || s.id)}</strong>
        </div>
        <p>${escapeHtml(llmDescriptionLabel(s))}</p>
        <div class="fault-symptoms">
            ${symptoms.map(x => `<span>${escapeHtml(llmSymptomLabel(x))}</span>`).join('')}
        </div>
        <pre>${escapeHtml([
            `层级: ${llmLayerLabel(s.layer)}`,
            `故障类型: ${llmFaultTypeLabel(s.fault_type)}`,
            `预期发现时间: ${s.expected_detection_time_s || '-'} 秒`,
            `参考: ${s.reference || '智算推理场景运行时测试工具设计报告'}`
        ].join('\n'))}</pre>
    </div>`;
}

function renderLLMFaultScenarioGrid() {
    const el = document.getElementById('llmfaultlab-grid');
    if (!el) return;
    if (!_llmFaultScenarios.length) {
        el.innerHTML = '<p class="text-muted" style="padding:12px">暂无智算故障场景</p>';
        return;
    }
    el.innerHTML = _llmFaultScenarios.map(s => `<div class="fault-card">
        <div>
            <span class="badge badge-info">${escapeHtml(llmLayerLabel(s.layer))}</span>
            <span class="badge badge-${s.severity === 'critical' ? 'danger' : 'warning'}">${escapeHtml(severityLabel(s.severity))}</span>
        </div>
        <strong>${escapeHtml(llmFaultTypeLabel(s.fault_type) || s.name || s.id)}</strong>
        <p>${escapeHtml(llmDescriptionLabel(s).substring(0, 140))}</p>
        <p class="text-muted">${escapeHtml(llmFaultTypeLabel(s.fault_type))}</p>
    </div>`).join('');
}

async function loadAISHPerfScenarios() {
    const domain = document.getElementById('aishperf-domain-filter')?.value || '';
    const query = document.getElementById('aishperf-search')?.value?.trim() || '';
    const output = document.getElementById('aishperf-output');
    if (output) output.textContent = '正在加载 AISHPerf 场景...';
    try {
        const status = await api('/api/integrations/aishperf/status');
        _aishperfStatus = status?.source || {};
        _aishperfDomainCounts = status?.domain_counts || {};
        const params = new URLSearchParams({ limit: '500' });
        if (domain) params.set('domain', domain);
        if (query) params.set('query', query);
        const data = await api(`/api/integrations/aishperf/scenarios?${params.toString()}`);
        _aishperfScenarios = data?.scenarios || [];
        if (_selectedAISHPerfCaseId && !_aishperfScenarios.some(s => s.id === _selectedAISHPerfCaseId)) {
            _selectedAISHPerfCaseId = '';
            _lastAISHPerfDetail = null;
            _lastAISHPerfDryRun = null;
            _lastAISHPerfSkillWorkflow = null;
        }
        renderAISHPerfScenarios();
        if (output) output.textContent = '';
    } catch (err) {
        if (output) output.textContent = llmFaultFormatError(err);
    }
}

function selectedAISHPerfScenario() {
    return _aishperfScenarios.find(s => s.id === _selectedAISHPerfCaseId) || null;
}

function resetAISHPerfFilters() {
    const search = document.getElementById('aishperf-search');
    const domain = document.getElementById('aishperf-domain-filter');
    const risk = document.getElementById('aishperf-risk-filter');
    if (search) search.value = '';
    if (domain) domain.value = '';
    if (risk) risk.value = '';
    _selectedAISHPerfCaseId = '';
    _lastAISHPerfDetail = null;
    _lastAISHPerfDryRun = null;
    _lastAISHPerfSkillWorkflow = null;
    loadAISHPerfScenarios();
}

function renderAISHPerfScenarios() {
    const summary = document.getElementById('aishperf-summary');
    const pills = document.getElementById('aishperf-domain-pills');
    const grid = document.getElementById('aishperf-grid');
    const riskFilter = document.getElementById('aishperf-risk-filter')?.value || '';
    const visibleScenarios = riskFilter
        ? _aishperfScenarios.filter(s => String(s.risk || '').toLowerCase() === riskFilter)
        : _aishperfScenarios;
    if (summary) {
        const available = _aishperfStatus?.available;
        const modeLabel = {
            external: '外部仓库',
            builtin_fallback: '扩展内置库',
            unavailable: '未配置',
        }[_aishperfStatus?.mode] || (_aishperfStatus?.available ? '已接入' : '未配置');
        const total = Object.values(_aishperfDomainCounts || {}).reduce((a, b) => a + Number(b || 0), 0) || _aishperfScenarios.length;
        const domains = Object.entries(_aishperfDomainCounts || {}).sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0));
        const riskCounts = visibleScenarios.reduce((acc, s) => {
            const key = String(s.risk || 'unknown').toLowerCase();
            acc[key] = (acc[key] || 0) + 1;
            return acc;
        }, {});
        const stepTotals = visibleScenarios.reduce((acc, s) => {
            acc.inject += Number(s.inject_steps || 0);
            acc.recover += Number(s.recover_steps || 0);
            return acc;
        }, { inject: 0, recover: 0 });
        const topDomains = domains.slice(0, 5).map(([k, v]) => `<span>${escapeHtml(k)} <b>${escapeHtml(v)}</b></span>`).join('');
        const riskHtml = ['critical', 'high', 'medium', 'low', 'unknown']
            .filter(k => riskCounts[k])
            .map(k => `<span class="aishperf-risk-chip risk-${escapeHtmlAttr(k)}">${escapeHtml(k)} ${escapeHtml(riskCounts[k])}</span>`)
            .join('');
        const sourceRoot = _aishperfStatus?.root || '-';
        summary.innerHTML = `
            <div class="aishperf-overview">
                <div class="aishperf-info-box info">
                    <span class="aishperf-info-icon">CAT</span>
                    <div><span>Catalog 状态</span><strong>${escapeHtml(modeLabel)}</strong><small>${available ? '可生成计划和 Agent+Skill 诊断' : '使用内置扩展场景'}</small></div>
                </div>
                <div class="aishperf-info-box primary">
                    <span class="aishperf-info-icon">ALL</span>
                    <div><span>场景总数</span><strong>${escapeHtml(total)}</strong><small>当前显示 ${escapeHtml(visibleScenarios.length)}</small></div>
                </div>
                <div class="aishperf-info-box success">
                    <span class="aishperf-info-icon">DOM</span>
                    <div><span>覆盖域</span><strong>${escapeHtml(domains.length)}</strong><small>${escapeHtml(domains.slice(0, 3).map(([k]) => k).join(' / ') || '-')}</small></div>
                </div>
                <div class="aishperf-info-box warning">
                    <span class="aishperf-info-icon">RUN</span>
                    <div><span>注入 / 恢复</span><strong>${escapeHtml(stepTotals.inject)} / ${escapeHtml(stepTotals.recover)}</strong><small>命令预案数量</small></div>
                </div>
                <div class="aishperf-status-strip">
                    <div><span>风险分布</span><div class="aishperf-risk-row">${riskHtml || '<span class="text-muted">暂无</span>'}</div></div>
                    <div><span>Top 域</span><div class="aishperf-domain-strip">${topDomains || '<span>暂无域分布</span>'}</div></div>
                    <div><span>来源</span><strong title="${escapeHtmlAttr(sourceRoot)}">${escapeHtml(sourceRoot)}</strong><small>${escapeHtml(_aishperfStatus?.external_available ? '外部 AISHPerf/Alcor 仓库' : '内置扩展 catalog')}</small></div>
                </div>
            </div>
        `;
    }
    if (pills) {
        const selectedDomain = document.getElementById('aishperf-domain-filter')?.value || '';
        const items = Object.entries(_aishperfDomainCounts || {})
            .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
            .slice(0, 12);
        pills.innerHTML = [
            `<button class="aishperf-domain-pill ${selectedDomain ? '' : 'active'}" onclick="document.getElementById('aishperf-domain-filter').value=''; loadAISHPerfScenarios()">全部</button>`,
            ...items.map(([domain, count]) => `<button class="aishperf-domain-pill ${selectedDomain === domain ? 'active' : ''}" onclick="document.getElementById('aishperf-domain-filter').value='${escapeHtmlAttr(domain)}'; loadAISHPerfScenarios()">${escapeHtml(domain)} <span>${escapeHtml(count)}</span></button>`),
        ].join('');
    }
    if (!grid) return;
    if (!visibleScenarios.length) {
        grid.innerHTML = '<p class="text-muted" style="padding:12px">未发现匹配的 AISHPerf 场景。请调整域、风险或搜索条件。</p>';
        return;
    }
    const selected = selectedAISHPerfScenario() || visibleScenarios[0];
    const activeFilters = [
        document.getElementById('aishperf-domain-filter')?.value ? `域=${document.getElementById('aishperf-domain-filter')?.value}` : '',
        riskFilter ? `风险=${riskFilter}` : '',
        document.getElementById('aishperf-search')?.value?.trim() ? `搜索=${document.getElementById('aishperf-search')?.value?.trim()}` : '',
    ].filter(Boolean);
    const renderLimit = 80;
    const renderedScenarios = visibleScenarios.slice(0, renderLimit);
    const rows = renderedScenarios.map(s => {
        const risk = String(s.risk || 'high');
        const riskClass = risk === 'critical' ? 'danger' : (risk === 'high' ? 'warning' : (risk === 'medium' ? 'info' : 'success'));
        const caseId = escapeHtmlAttr(s.id || '');
        const isSelected = s.id === _selectedAISHPerfCaseId;
        const domainText = (s.domains || []).slice(0, 3).join(' / ') || '-';
        const source = String(s.source || 'aishperf.aiops-chaos').replace(/^aishperf\./, '');
        return `<tr class="${isSelected ? 'selected' : ''}" onclick="showAISHPerfScenarioDetail('${caseId}')">
            <td><strong>${escapeHtml(s.name || s.id)}</strong><small>${escapeHtml(s.summary || s.id || '')}</small></td>
            <td>${escapeHtml(domainText)}</td>
            <td><span class="badge badge-${riskClass}">${escapeHtml(risk)}</span></td>
            <td><span>${escapeHtml(s.inject_steps || 0)}</span> / <span>${escapeHtml(s.recover_steps || 0)}</span></td>
            <td>${escapeHtml(source)}</td>
            <td class="aishperf-row-actions" onclick="event.stopPropagation()">
                <button class="btn btn-xs" data-aishperf-case="${caseId}" onclick="showAISHPerfScenarioDetail(this.dataset.aishperfCase)">详情</button>
                <button class="btn btn-xs" data-aishperf-case="${caseId}" onclick="dryRunAISHPerfScenario(this.dataset.aishperfCase)">Dry-run</button>
                <button class="btn btn-xs btn-primary" data-aishperf-case="${caseId}" onclick="runAISHPerfSkillWorkflow(this.dataset.aishperfCase)">Agent+Skill</button>
            </td>
        </tr>`;
    }).join('');
    const selectedDomains = (selected?.domains || []).map(d => `<span class="badge badge-info">${escapeHtml(d)}</span>`).join('');
    grid.innerHTML = `
        <div class="aishperf-list-toolbar">
            <div>
                <strong>场景 Catalog</strong>
                <span>当前显示 ${escapeHtml(renderedScenarios.length)} / 匹配 ${escapeHtml(visibleScenarios.length)} / 总数 ${escapeHtml(_aishperfScenarios.length)}，默认只生成 dry-run 计划</span>
            </div>
            ${visibleScenarios.length > renderLimit ? `<span class="badge badge-warning">仅展示前 ${renderLimit} 条，请缩小筛选</span>` : ''}
            ${activeFilters.length ? `<span class="badge badge-warning">${escapeHtml(activeFilters.join(' · '))}</span><button class="btn btn-xs" onclick="resetAISHPerfFilters()">清空</button>` : '<span class="badge badge-success">未筛选</span>'}
        </div>
        <div class="aishperf-catalog">
            <div class="aishperf-table-wrap">
                <table class="aishperf-table">
                    <thead><tr><th>场景</th><th>域</th><th>风险</th><th>注入/恢复</th><th>来源</th><th>动作</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <aside class="aishperf-inspector">
                <span class="badge badge-${selected?.risk === 'critical' ? 'danger' : 'warning'}">${escapeHtml(selected?.risk || 'review')}</span>
                <h4>${escapeHtml(selected?.name || selected?.id || '选择场景')}</h4>
                <p>${escapeHtml(selected?.summary || '点击左侧场景查看详情、生成 dry-run 计划或进入 Agent+Skill 诊断。')}</p>
                <div class="aishperf-inspector-domains">${selectedDomains || '<span class="text-muted">未标注域</span>'}</div>
                <dl>
                    <div><dt>场景 ID</dt><dd>${escapeHtml(selected?.id || '-')}</dd></div>
                    <div><dt>注入步骤</dt><dd>${escapeHtml(selected?.inject_steps || 0)}</dd></div>
                    <div><dt>恢复步骤</dt><dd>${escapeHtml(selected?.recover_steps || 0)}</dd></div>
                    <div><dt>执行策略</dt><dd>默认 dry-run，真实执行需要审批</dd></div>
                </dl>
                <div class="aishperf-card-actions">
                    <button class="btn btn-sm" data-aishperf-case="${escapeHtmlAttr(selected?.id || '')}" onclick="showAISHPerfScenarioDetail(this.dataset.aishperfCase)">查看详情</button>
                    <button class="btn btn-sm" data-aishperf-case="${escapeHtmlAttr(selected?.id || '')}" onclick="dryRunAISHPerfScenario(this.dataset.aishperfCase)">生成计划</button>
                    <button class="btn btn-sm btn-primary" data-aishperf-case="${escapeHtmlAttr(selected?.id || '')}" onclick="runAISHPerfSkillWorkflow(this.dataset.aishperfCase)">Agent+Skill 诊断</button>
                </div>
            </aside>
        </div>
    `;
}

function aishperfTargetBody() {
    const mode = getLLMFaultMode();
    const host = selectedLLMFaultHost();
    const clusterId = document.getElementById('llmfaultlab-cluster')?.value || '';
    const cluster = (_faultTargets.k8s_clusters || []).find(c => c.id === clusterId) || {};
    const namespace = document.getElementById('llmfaultlab-namespace')?.value || cluster.default_namespace || '';
    const deployment = document.getElementById('llmfaultlab-deployment')?.value || cluster.vllm_deployment || cluster.deployment || '';
    const service = document.getElementById('llmfaultlab-service')?.value || cluster.vllm_service || cluster.service || deployment;
    return {
        target_mode: mode,
        cluster_id: clusterId,
        namespace,
        deployment,
        service,
        host_id: document.getElementById('llmfaultlab-host')?.value || '',
        node: mode === 'host'
            ? (host?.ssh_host || host?.host || host?.name || host?.id || '')
            : (cluster.node || clusterId || '${NODE}'),
        prometheus_url: cluster.prometheus_url || host?.prometheus_url || '',
        vllm_endpoint: cluster.vllm_endpoint || document.getElementById('llmfaultlab-endpoint')?.value || '',
    };
}

function renderAISHPerfPlan(plan, detail = _lastAISHPerfDetail) {
    const el = document.getElementById('aishperf-plan');
    if (!el) return;
    const scenario = detail?.scenario || plan?.case || selectedAISHPerfScenario() || {};
    const safety = plan?.safety || {};
    const requires = plan?.requires || [];
    const inject = plan?.case?.inject || scenario.inject || [];
    const recover = plan?.case?.recover || scenario.recover || [];
    el.style.display = '';
    el.innerHTML = `
        <div class="fault-result-head">
            <div class="fault-result-title">
                <span class="badge badge-info">AISHPerf</span>
                <strong>${escapeHtml(scenario.name || scenario.id || '外部故障场景')}</strong>
                <span class="text-muted">${escapeHtml((scenario.domains || []).join(' / ') || '-')}</span>
            </div>
            <span class="badge badge-${safety.risk === 'critical' ? 'danger' : 'warning'}">${escapeHtml(safety.risk || scenario.risk || 'review')}</span>
        </div>
        <div class="fault-result-grid">
            <div class="fault-result-metric"><span>执行器</span><strong>${escapeHtml(plan?.executor || 'aishperf.aemu')}</strong></div>
            <div class="fault-result-metric"><span>注入步骤</span><strong>${escapeHtml(inject.length || scenario.inject_steps || 0)}</strong></div>
            <div class="fault-result-metric"><span>恢复步骤</span><strong>${escapeHtml(recover.length || scenario.recover_steps || 0)}</strong></div>
            <div class="fault-result-metric"><span>自动执行</span><strong>${safety.auto_execute ? '允许' : '禁止'}</strong></div>
        </div>
        ${requires.length ? `<div class="fault-result-section"><h4>执行前置条件</h4><ul>${requires.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul></div>` : ''}
        <div class="fault-result-section"><h4>AEMU 调用</h4><div class="fault-result-code">${escapeHtml(JSON.stringify(plan?.aemu_http || {}, null, 2))}</div></div>
        <div class="fault-result-section"><h4>注入 / 恢复命令预览</h4>
            <div class="aishperf-command-columns">
                <div><strong>Inject</strong>${inject.slice(0, 6).map(x => `<code>${escapeHtml(x)}</code>`).join('') || '<p class="text-muted">无</p>'}</div>
                <div><strong>Recover</strong>${recover.slice(0, 6).map(x => `<code>${escapeHtml(x)}</code>`).join('') || '<p class="text-muted">无</p>'}</div>
            </div>
        </div>
        <div class="fault-closure-actions">
            <button class="btn btn-sm btn-primary" data-aishperf-case="${escapeHtmlAttr(scenario.id || '')}" onclick="runAISHPerfSkillWorkflow(this.dataset.aishperfCase)">进入 Agent+Skill 诊断</button>
            <button class="btn btn-sm" onclick="buildAISHPerfEvalFromSelection()">生成评测摘要</button>
        </div>
    `;
}

async function showAISHPerfScenarioDetail(caseId) {
    if (!caseId) return null;
    const output = document.getElementById('aishperf-output');
    _selectedAISHPerfCaseId = caseId;
    renderAISHPerfScenarios();
    if (output) output.textContent = '正在加载 AISHPerf 场景详情...';
    try {
        const detail = await api(`/api/integrations/aishperf/scenarios/${encodeURIComponent(caseId)}`, { throwOnError: true });
        _lastAISHPerfDetail = detail || null;
        const currentPlan = _lastAISHPerfDryRun?.case?.id === caseId
            ? _lastAISHPerfDryRun
            : { case: detail?.scenario, safety: { risk: detail?.scenario?.risk, auto_execute: false } };
        renderAISHPerfPlan(currentPlan, detail);
        if (output) output.textContent = JSON.stringify(detail, null, 2);
        return detail;
    } catch (err) {
        if (output) output.textContent = llmFaultFormatError(err);
        return null;
    }
}

async function dryRunAISHPerfScenario(caseId) {
    const output = document.getElementById('aishperf-output');
    if (!caseId) return null;
    _selectedAISHPerfCaseId = caseId;
    const body = aishperfTargetBody();
    if (output) output.textContent = '正在生成 AISHPerf dry-run 计划...';
    try {
        const detail = _lastAISHPerfDetail?.scenario?.id === caseId ? _lastAISHPerfDetail : await showAISHPerfScenarioDetail(caseId);
        const res = await api(`/api/integrations/aishperf/scenarios/${encodeURIComponent(caseId)}/dry-run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        _lastAISHPerfDryRun = res || null;
        renderAISHPerfPlan(res, detail || _lastAISHPerfDetail);
        renderAISHPerfScenarios();
        if (output) output.textContent = JSON.stringify(res, null, 2);
        return res;
    } catch (err) {
        if (output) output.textContent = llmFaultFormatError(err);
        return null;
    }
}

function buildAISHPerfSkillWorkflowBody(caseId = _selectedAISHPerfCaseId, stages = ['detect', 'diagnose', 'heal', 'verify']) {
    const scenario = (_lastAISHPerfDetail?.scenario?.id === caseId ? _lastAISHPerfDetail.scenario : null)
        || selectedAISHPerfScenario()
        || {};
    const target = aishperfTargetBody();
    const dryRun = _lastAISHPerfDryRun || {};
    return {
        domain: 'llm',
        stages,
        dry_run: true,
        allow_legacy_fallback: true,
        payload: {
            ...target,
            source: 'aishperf',
            external_source: 'aishperf.aiops-chaos',
            domain: 'llm',
            scenario_id: caseId || scenario.id || '',
            aemu_fault_id: scenario.name || String(caseId || '').replace(/^aishperf-/, ''),
            scenario_name: scenario.name || caseId || '',
            fault_type: 'aishperf_aiinfra_fault',
            fault_domain: (scenario.domains || [])[0] || '',
            domains: scenario.domains || [],
            risk: scenario.risk || dryRun.safety?.risk || 'high',
            message: `${scenario.summary || 'AISHPerf AI Infra 外部故障场景'} | ${caseId || ''}`,
            inject_steps: scenario.inject_steps || dryRun.case?.inject_steps || 0,
            recover_steps: scenario.recover_steps || dryRun.case?.recover_steps || 0,
            dry_run_plan: dryRun,
            signals: {
                aemu_plan_ready: !!dryRun.aemu_http,
                external_available: !!_aishperfStatus.external_available,
                builtin_fallback: _aishperfStatus.mode === 'builtin_fallback',
            },
            metrics: _lastLLMFaultEvaluation?.metrics || {},
        },
    };
}

async function runAISHPerfSkillWorkflow(caseId) {
    const selected = caseId || _selectedAISHPerfCaseId;
    const output = document.getElementById('aishperf-output');
    const resultEl = document.getElementById('aishperf-skill-result');
    if (!selected) {
        showToast('请先选择 AISHPerf 场景', 'warning');
        return null;
    }
    _selectedAISHPerfCaseId = selected;
    if (!_lastAISHPerfDryRun || _lastAISHPerfDryRun.case?.id !== selected) {
        await dryRunAISHPerfScenario(selected);
    }
    const body = buildAISHPerfSkillWorkflowBody(selected);
    const payload = body.payload || {};
    const query = [
        `[AISHPerf外部场景][${payload.scenario_id || selected}]`,
        payload.message || payload.scenario_name || selected,
        `domain=${(payload.domains || []).join('/') || payload.fault_domain || 'llm'}`,
        `namespace=${payload.namespace || 'llm-k8s'}`,
        `deployment=${payload.deployment || 'vllm-server'}`,
        `risk=${payload.risk || 'high'}`,
        '请由 Agent 先列出并选择精确 Skill ID，再通过 agenticsre_skill_run 执行 detect/diagnose/heal/verify，输出中文根因、证据、置信度、恢复计划和验证步骤。',
    ].filter(Boolean).join('; ');
    if (output) output.textContent = '正在启动 AISHPerf Agent RCA，Agent 将选择并调用 Skill...';
    try {
        const result = await runAgentSkillDiagnosis(query, payload.namespace || 'llm-k8s', 'aishperf-output', { maxPolls: 36, skillWorkflow: body });
        const skillResult = result?.skillEvent?.result || null;
        _lastAISHPerfSkillWorkflow = skillResult;
        state.lastSkillWorkflow = skillResult || state.lastSkillWorkflow;
        renderAgentSkillDiagnosisResult('aishperf-skill-result', result, 'AISHPerf Agent+Skill 诊断闭环');
        if (output) output.textContent = JSON.stringify({ agent_run: result?.run || null, status: result?.status?.status || 'unknown', skill_result: skillResult }, null, 2);
        return result;
    } catch (err) {
        if (output) output.textContent = llmFaultFormatError(err);
        return null;
    }
}

async function buildAISHPerfEvalFromSelection() {
    const output = document.getElementById('aishperf-output');
    const caseId = _selectedAISHPerfCaseId || selectedAISHPerfScenario()?.id || '';
    if (!caseId) {
        showToast('请先选择 AISHPerf 场景', 'warning');
        return null;
    }
    const diagnosis = _lastAISHPerfSkillWorkflow?.diagnosis || _lastLLMFaultDiagnosis || {};
    const heal = _lastAISHPerfSkillWorkflow?.execution_plan || _lastLLMFaultHeal || {};
    const payload = {
        ...aishperfTargetBody(),
        run_id: `aishperf-${Date.now()}`,
        result: _lastAISHPerfDryRun || { status: 'dry_run', action: 'aishperf_dry_run' },
        diagnosis,
        heal,
        skill_runs: _lastAISHPerfSkillWorkflow?.skill_runtime?.runs || [],
    };
    if (output) output.textContent = '正在生成 AISHPerf 风格评测摘要...';
    try {
        const res = await api(`/api/integrations/aishperf/scenarios/${encodeURIComponent(caseId)}/evaluate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        if (output) output.textContent = JSON.stringify(res, null, 2);
        return res;
    } catch (err) {
        if (output) output.textContent = llmFaultFormatError(err);
        return null;
    }
}

async function injectLLMFaultScenario() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    const body = llmFaultRequestBody({ action: 'inject', scenario: s });
    if (!llmFaultEnsureConfirmed('inject', s, body)) return;
    if (!(await llmFaultEnsurePreflight('inject', body))) return;
    if (out) out.textContent = '执行中...';
    try {
        const res = await api(`/api/llm-faultlab/inject/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        renderLLMFaultResult(res);
        if (res?.status === 'executed') await loadActiveLLMFaultRuns();
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function runLLMFaultExperiment() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    const body = {
        ...llmFaultRequestBody({ action: 'experiment', scenario: s }),
        baseline_samples: 2,
        fault_samples: 3,
        sample_interval: 3,
    };
    if (!llmFaultEnsureConfirmed('experiment', s, body)) return;
    if (!(await llmFaultEnsurePreflight('experiment', body))) return;
    if (out) out.textContent = '实验执行中...';
    try {
        const res = await api(`/api/llm-faultlab/experiment/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        renderLLMFaultResult(res);
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function cleanupSelectedLLMFaultRun() {
    const run = await applySelectedLLMFaultRun();
    if (!run) {
        alert('请先从“当前活跃故障”中选择要恢复的注入记录。');
        return;
    }
    return cleanupLLMFaultScenario({ faultRun: run });
}

async function cleanupLLMFaultScenario(options = {}) {
    const faultRun = options.faultRun || null;
    const s = faultRun
        ? _llmFaultScenarios.find(item => item.id === faultRun.scenario_id)
        : selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    const body = llmFaultRequestBody({ action: 'cleanup', scenario: s });
    if (faultRun?.id) body.fault_run_id = faultRun.id;
    if (!llmFaultEnsureConfirmed('cleanup', s, body)) return;
    if (out) out.textContent = '恢复中...';
    try {
        const res = await api(`/api/llm-faultlab/cleanup/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            timeoutMs: 180000,
            throwOnError: true,
        });
        renderLLMFaultResult(res);
        if (res?.status === 'executed') await loadActiveLLMFaultRuns();
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

function addFaultExperimentStep() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    _faultExperimentSteps.push({
        scenario_id: s.id,
        name: s.name || s.id,
        fault_type: s.fault_type || '',
        layer: s.layer || '',
        action: 'experiment',
        hold_s: 30,
        wait_after_s: parseInt(document.getElementById('fault-exp-wait')?.value || '5', 10) || 0,
    });
    renderFaultExperimentSteps();
}

function buildLLMFaultExperimentStep(scenario, options = {}) {
    return {
        scenario_id: scenario.id,
        name: scenario.name || scenario.id,
        fault_type: scenario.fault_type || '',
        layer: scenario.layer || '',
        action: options.action || 'experiment',
        hold_s: options.hold_s ?? 45,
        wait_after_s: options.wait_after_s ?? (parseInt(document.getElementById('fault-exp-wait')?.value || '5', 10) || 0),
    };
}

function applyLLMFaultExperimentPreset(kind = 'smoke') {
    const recipes = {
        smoke: [
            [['process_crash', 'crash', 'service_unavailable'], { hold_s: 35 }],
            [['endpoint_timeout', 'latency', 'timeout'], { hold_s: 40 }],
        ],
        performance: [
            [['endpoint_timeout', 'latency', 'ttft', 'tpot'], { hold_s: 60 }],
            [['business_slo', 'queue', 'waiting'], { hold_s: 60 }],
        ],
        gpu: [
            [['gpu_oom', 'kv_cache', 'memory'], { hold_s: 45 }],
            [['gpu_resource', 'gpu', 'utilization'], { hold_s: 45 }],
        ],
        mixed: [
            [['process_crash', 'crash'], { hold_s: 35 }],
            [['endpoint_timeout', 'latency'], { hold_s: 45 }],
            [['gpu_oom', 'memory', 'kv_cache'], { hold_s: 45 }],
        ],
    };
    const selected = [];
    (recipes[kind] || recipes.smoke).forEach(([keywords, options]) => {
        const scenario = scenarioByFaultKeywords(_llmFaultScenarios, keywords);
        if (scenario && !selected.some(item => item.scenario_id === scenario.id)) {
            selected.push(buildLLMFaultExperimentStep(scenario, options));
        }
    });
    if (!selected.length) {
        showToast('没有找到匹配的智算模板场景，请先刷新场景。', 'warning');
        return;
    }
    _faultExperimentSteps = selected;
    if (!document.getElementById('fault-exp-name')?.value) {
        setVal('fault-exp-name', `llm-${kind}-${new Date().toISOString().slice(11, 16).replace(':', '')}`);
    }
    renderFaultExperimentSteps();
}

function clearFaultExperimentSteps() {
    _faultExperimentSteps = [];
    renderFaultExperimentSteps();
}

function removeFaultExperimentStep(index) {
    _faultExperimentSteps.splice(index, 1);
    renderFaultExperimentSteps();
}

function updateFaultExperimentStep(index, field, value) {
    if (!_faultExperimentSteps[index]) return;
    if (field === 'hold_s' || field === 'wait_after_s') {
        _faultExperimentSteps[index][field] = Math.max(0, parseInt(value || '0', 10) || 0);
    } else {
        _faultExperimentSteps[index][field] = value;
    }
}

function renderFaultExperimentSteps() {
    const el = document.getElementById('fault-exp-steps');
    if (!el) return;
    renderFaultExperimentSummary();
    if (!_faultExperimentSteps.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px">尚未添加连续实验步骤。可选择模板，或在单次实验/依赖图中选中目标后添加当前场景。</p>';
        return;
    }
    el.innerHTML = _faultExperimentSteps.map((step, i) => `
        <div class="fault-exp-step">
            <div class="fault-exp-step-main">
                <span class="badge badge-info">#${i + 1}</span>
                <strong>${escapeHtml(step.scenario_id)}</strong>
                <span class="text-muted">${escapeHtml(step.name || '')}</span>
                <span class="badge badge-warning">${escapeHtml(step.fault_type || '')}</span>
                <span class="badge badge-gray">${escapeHtml(step.layer || 'llm')}</span>
            </div>
            <div class="fault-exp-step-flow"><span>baseline</span><i></i><span>inject</span><i></i><span>${step.hold_s || 0}s</span><i></i><span>diagnose</span><i></i><span>recover</span></div>
            <select class="select-sm" onchange="updateFaultExperimentStep(${i}, 'action', this.value)">
                <option value="experiment" ${step.action === 'experiment' ? 'selected' : ''}>完整实验</option>
                <option value="inject" ${step.action === 'inject' ? 'selected' : ''}>只注入</option>
                <option value="cleanup" ${step.action === 'cleanup' ? 'selected' : ''}>只恢复</option>
            </select>
            <input class="input-sm" type="number" min="0" value="${step.hold_s}" title="hold seconds"
                onchange="updateFaultExperimentStep(${i}, 'hold_s', this.value)">
            <input class="input-sm" type="number" min="0" value="${step.wait_after_s}" title="wait after seconds"
                onchange="updateFaultExperimentStep(${i}, 'wait_after_s', this.value)">
            <button class="btn btn-sm btn-danger" onclick="removeFaultExperimentStep(${i})">移除</button>
        </div>
    `).join('');
}

function renderFaultExperimentSummary() {
    const el = document.getElementById('fault-exp-summary');
    if (!el) return;
    const dryRun = document.getElementById('llmfaultlab-dryrun')?.checked !== false;
    const totalHold = _faultExperimentSteps.reduce((sum, step) => sum + (Number(step.hold_s) || 0), 0);
    const totalWait = _faultExperimentSteps.reduce((sum, step) => sum + (Number(step.wait_after_s) || 0), 0);
    const types = [...new Set(_faultExperimentSteps.map(s => s.fault_type).filter(Boolean))];
    const mode = getLLMFaultMode ? getLLMFaultMode() : 'k8s';
    el.innerHTML = `
        <div><span>步骤</span><strong>${_faultExperimentSteps.length}</strong></div>
        <div><span>预计观察</span><strong>${totalHold + totalWait}s</strong></div>
        <div><span>目标</span><strong>${escapeHtml(mode === 'host' ? '裸机 GPU' : 'K8s vLLM')}</strong></div>
        <div><span>执行模式</span><strong>${dryRun ? 'Dry-run 计划' : '真实执行需确认'}</strong></div>
        <div><span>故障类型</span><strong>${escapeHtml(types.slice(0, 3).join(' / ') || '-')}</strong></div>
    `;
}

function faultExperimentRequestBody() {
    const cluster = selectedLLMFaultCluster();
    return {
        ...llmFaultRequestBody({ action: 'experiment', riskAckNeeded: llmFaultExperimentRiskAckNeeded() }),
        benchmark_type: 'llm',
        name: document.getElementById('fault-exp-name')?.value || '',
        business_system: document.getElementById('fault-exp-system')?.value || 'llm-inference',
        total_duration_s: parseInt(document.getElementById('fault-exp-total')?.value || '0', 10) || 0,
        default_wait_after_s: parseInt(document.getElementById('fault-exp-wait')?.value || '5', 10) || 0,
        endpoint: document.getElementById('llmfaultlab-endpoint')?.value || 'http://127.0.0.1:8000/v1/completions',
        model: document.getElementById('llmfaultlab-model')?.value || cluster.model || '',
        interface: document.getElementById('llmfaultlab-interface')?.value || 'eth0',
        model_dir: document.getElementById('llmfaultlab-model-dir')?.value || '',
        steps: _faultExperimentSteps.map(step => ({
            scenario_id: step.scenario_id,
            action: step.action,
            hold_s: step.hold_s,
            wait_after_s: step.wait_after_s,
        })),
    };
}

async function startFaultExperiment() {
    const out = document.getElementById('fault-exp-output');
    if (!_faultExperimentSteps.length) {
        if (out) out.textContent = '请先添加至少一个实验步骤。';
        return;
    }
    const body = faultExperimentRequestBody();
    if (!body.dry_run && !body.confirm) {
        alert('取消 Dry-run 后需要先在智算故障实验区勾选“确认真实执行”。');
        return;
    }
    if (!body.dry_run && !confirm(`确认启动智算连续故障实验？\n\n步骤数: ${_faultExperimentSteps.length}`)) return;
    if (out) out.textContent = '连续实验启动中...';
    try {
        const res = await api('/api/fault-experiments/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            throwOnError: true,
        });
        if (out) out.textContent = JSON.stringify(res, null, 2);
        await loadFaultExperiments();
    } catch (err) {
        if (out) out.textContent = llmFaultFormatError(err);
    }
}

async function cancelFaultExperiment(runId, outputId = 'fault-exp-output') {
    const out = document.getElementById(outputId);
    const res = await api(`/api/fault-experiments/${runId}/cancel`, { method: 'POST' });
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadFaultExperiments();
    await loadK8sFaultExperiments();
}

async function showFaultExperiment(runId, outputId = 'fault-exp-output') {
    const out = document.getElementById(outputId);
    const res = await api(`/api/fault-experiments/${runId}`);
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

async function loadFaultExperiments() {
    const data = await api('/api/fault-experiments?limit=10');
    const runs = (data?.experiments || []).filter(run => (run.spec?.benchmark_type || 'llm') === 'llm');
    renderFaultExperimentRuns('fault-exp-runs', 'fault-exp-output', runs);
}

function renderFaultExperimentRuns(containerId, outputId, runs) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!runs.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px;margin-top:10px">暂无连续实验记录</p>';
        return;
    }
    el.innerHTML = runs.map(run => {
        const status = run.status || 'unknown';
        const cls = status === 'completed' ? 'success' : status === 'failed' ? 'danger' : status === 'running' ? 'warning' : 'info';
        const steps = run.spec?.steps?.length || 0;
        return `<div class="fault-exp-run">
            <div>
                <span class="badge badge-${cls}">${escapeHtml(status)}</span>
                <strong>${escapeHtml(run.name || run.id)}</strong>
                <span class="text-muted">${steps} steps · ${formatTime((run.started_at || run.created_at) * 1000)}</span>
            </div>
            <div>
                <button class="btn btn-sm" onclick="showFaultExperiment('${escapeHtml(run.id)}', '${escapeHtml(outputId)}')">查看</button>
                ${status === 'running' || status === 'queued' ? `<button class="btn btn-sm btn-danger" onclick="cancelFaultExperiment('${escapeHtml(run.id)}', '${escapeHtml(outputId)}')">取消</button>` : ''}
            </div>
        </div>`;
    }).join('');
}

// ─────────────────────────────────────────
// 故障目标管理 Modal
// ─────────────────────────────────────────

function openFaultTargetManager() {
    const modal = document.getElementById('fault-target-modal');
    if (!modal) return;
    modal.style.display = 'flex';
    renderFaultTargetTables();
    switchTargetTab('k8s');
}

function closeFaultTargetManager() {
    const modal = document.getElementById('fault-target-modal');
    if (modal) modal.style.display = 'none';
}

function switchTargetTab(tab) {
    const k8sPane = document.getElementById('tgt-pane-k8s');
    const hostPane = document.getElementById('tgt-pane-host');
    const k8sBtn = document.getElementById('tgt-tab-k8s');
    const hostBtn = document.getElementById('tgt-tab-host');
    if (tab === 'k8s') {
        if (k8sPane) k8sPane.style.display = '';
        if (hostPane) hostPane.style.display = 'none';
        if (k8sBtn) k8sBtn.classList.add('btn-primary');
        if (hostBtn) hostBtn.classList.remove('btn-primary');
    } else {
        if (k8sPane) k8sPane.style.display = 'none';
        if (hostPane) hostPane.style.display = '';
        if (k8sBtn) k8sBtn.classList.remove('btn-primary');
        if (hostBtn) hostBtn.classList.add('btn-primary');
    }
}

function renderFaultTargetTables() {
    const k8sBody = document.getElementById('tgt-k8s-body');
    if (k8sBody) {
        k8sBody.innerHTML = _faultTargets.k8s_clusters.map((c, i) => clusterRowHtml(c, i)).join('') ||
            '<tr><td colspan="13" class="text-muted" style="text-align:center;padding:16px">尚无集群，点击下方"新增"</td></tr>';
    }
    const hostBody = document.getElementById('tgt-host-body');
    if (hostBody) {
        hostBody.innerHTML = _faultTargets.llm_hosts.map((h, i) => hostRowHtml(h, i)).join('') ||
            '<tr><td colspan="12" class="text-muted" style="text-align:center;padding:16px">尚无主机，点击下方"新增"</td></tr>';
    }
}

function clusterRowHtml(c, i) {
    return `<tr data-idx="${i}">
        <td><input class="input-sm tgt-k8s-id" value="${escapeHtml(c.id || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-name" value="${escapeHtml(c.name || '')}" style="width:140px"></td>
        <td><select class="select-sm tgt-k8s-kind" style="width:92px"><option value="general"${String(c.kind || 'general') !== 'ai' ? ' selected' : ''}>通算</option><option value="ai"${String(c.kind || '') === 'ai' ? ' selected' : ''}>智算</option></select></td>
        <td><input class="input-sm tgt-k8s-kubectl" value="${escapeHtml(c.kubectl_path || 'kubectl')}" placeholder="kubectl" style="width:120px"></td>
        <td><input class="input-sm tgt-k8s-kubeconfig" value="${escapeHtml(c.kubeconfig || '')}" placeholder="留空=容器默认" style="width:160px"></td>
        <td><input class="input-sm tgt-k8s-context" value="${escapeHtml(c.context || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-ns" value="${escapeHtml(c.default_namespace || 'default')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-prom" value="${escapeHtml(c.prometheus_url || '')}" placeholder="http://prom:9090" style="width:170px"></td>
        <td><input class="input-sm tgt-k8s-vllm" value="${escapeHtml(c.vllm_endpoint || '')}" placeholder="http://node:30080/v1/completions" style="width:210px"></td>
        <td><input class="input-sm tgt-k8s-teller" value="${escapeHtml(c.teller_trace_dir || c.teller_processed_dir || '')}" placeholder="/data/teller/trace" style="width:180px"></td>
        <td><input class="input-sm tgt-k8s-model" value="${escapeHtml(c.model || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-k8s-desc" value="${escapeHtml(c.description || '')}" style="width:160px"></td>
        <td><button class="btn btn-sm btn-danger" onclick="removeClusterRow(${i})">删除</button></td>
    </tr>`;
}

function hostRowHtml(h, i) {
    return `<tr data-idx="${i}">
        <td><input class="input-sm tgt-h-id" value="${escapeHtml(h.id || '')}" style="width:90px"></td>
        <td><input class="input-sm tgt-h-name" value="${escapeHtml(h.name || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-h-ai-cluster" value="${escapeHtml(h.ai_cluster_id || h.cluster_id || '')}" placeholder="t4-vllm-k8s" style="width:130px"></td>
        <td><input class="input-sm tgt-h-host" value="${escapeHtml(h.host || '')}" style="width:120px"></td>
        <td><input class="input-sm tgt-h-user" value="${escapeHtml(h.ssh_user || 'root')}" style="width:80px"></td>
        <td><input class="input-sm tgt-h-key" value="${escapeHtml(h.ssh_key_path || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-h-jump" value="${escapeHtml(h.jump_host || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-h-pass-env" value="${escapeHtml((h.ssh_password_env || '').replace(/^\\$/, ''))}" placeholder="变量名" style="width:140px"></td>
        <td><input class="input-sm tgt-h-jump-pass-env" value="${escapeHtml((h.jump_password_env || '').replace(/^\\$/, ''))}" placeholder="变量名" style="width:150px"></td>
        <td><input class="input-sm tgt-h-prom" value="${escapeHtml(h.prometheus_url || '')}" placeholder="http://host:9090" style="width:180px"></td>
        <td><input class="input-sm tgt-h-gpu" value="${escapeHtml(h.gpu || '')}" style="width:110px"></td>
        <td><input class="input-sm tgt-h-role" value="${escapeHtml(h.role || '')}" style="width:120px"></td>
        <td><button class="btn btn-sm btn-danger" onclick="removeHostRow(${i})">删除</button></td>
    </tr>`;
}

function collectFromTables() {
    const k8s = [];
    document.querySelectorAll('#tgt-k8s-body tr[data-idx]').forEach(tr => {
        k8s.push({
            id: tr.querySelector('.tgt-k8s-id')?.value?.trim() || '',
            name: tr.querySelector('.tgt-k8s-name')?.value?.trim() || '',
            kind: tr.querySelector('.tgt-k8s-kind')?.value?.trim() || 'general',
            kubectl_path: tr.querySelector('.tgt-k8s-kubectl')?.value?.trim() || 'kubectl',
            kubeconfig: tr.querySelector('.tgt-k8s-kubeconfig')?.value?.trim() || '',
            context: tr.querySelector('.tgt-k8s-context')?.value?.trim() || '',
            default_namespace: tr.querySelector('.tgt-k8s-ns')?.value?.trim() || 'default',
            prometheus_url: tr.querySelector('.tgt-k8s-prom')?.value?.trim() || '',
            vllm_endpoint: tr.querySelector('.tgt-k8s-vllm')?.value?.trim() || '',
            teller_trace_dir: tr.querySelector('.tgt-k8s-teller')?.value?.trim() || '',
            model: tr.querySelector('.tgt-k8s-model')?.value?.trim() || '',
            description: tr.querySelector('.tgt-k8s-desc')?.value?.trim() || '',
        });
    });
    const hosts = [];
    document.querySelectorAll('#tgt-host-body tr[data-idx]').forEach(tr => {
        hosts.push({
            id: tr.querySelector('.tgt-h-id')?.value?.trim() || '',
            name: tr.querySelector('.tgt-h-name')?.value?.trim() || '',
            ai_cluster_id: tr.querySelector('.tgt-h-ai-cluster')?.value?.trim() || '',
            host: tr.querySelector('.tgt-h-host')?.value?.trim() || '',
            ssh_user: tr.querySelector('.tgt-h-user')?.value?.trim() || 'root',
            ssh_key_path: tr.querySelector('.tgt-h-key')?.value?.trim() || '',
            jump_host: tr.querySelector('.tgt-h-jump')?.value?.trim() || '',
            ssh_password_env: tr.querySelector('.tgt-h-pass-env')?.value?.trim()?.replace(/^\$/, '') || '',
            jump_password_env: tr.querySelector('.tgt-h-jump-pass-env')?.value?.trim()?.replace(/^\$/, '') || '',
            prometheus_url: tr.querySelector('.tgt-h-prom')?.value?.trim() || '',
            gpu: tr.querySelector('.tgt-h-gpu')?.value?.trim() || '',
            role: tr.querySelector('.tgt-h-role')?.value?.trim() || '',
        });
    });
    return { k8s_clusters: k8s, llm_hosts: hosts };
}

function addClusterRow() {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.k8s_clusters.push({ id: '', name: '', kind: 'general', kubectl_path: 'kubectl', kubeconfig: '', context: '', default_namespace: 'default', prometheus_url: '', vllm_endpoint: '', teller_trace_dir: '', model: '', description: '' });
    renderFaultTargetTables();
}

function removeClusterRow(idx) {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.k8s_clusters.splice(idx, 1);
    renderFaultTargetTables();
}

function addHostRow() {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.llm_hosts.push({ id: '', name: '', ai_cluster_id: '', host: '', ssh_user: 'root', ssh_key_path: '', jump_host: '', ssh_password_env: '', jump_password_env: '', prometheus_url: '', gpu: '', role: '' });
    renderFaultTargetTables();
    switchTargetTab('host');
}

function removeHostRow(idx) {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.llm_hosts.splice(idx, 1);
    renderFaultTargetTables();
    switchTargetTab('host');
}

async function saveFaultTargets() {
    const payload = collectFromTables();
    // Basic validation
    for (const c of payload.k8s_clusters) {
        if (!c.id || !c.name) { showToast('集群 id/名称 不能为空', 'error'); return; }
    }
    for (const h of payload.llm_hosts) {
        if (!h.id || !h.name) { showToast('主机 id/名称 不能为空', 'error'); return; }
    }
    const res = await api('/api/fault-targets', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (res?.status === 'ok' || res?.ok || (res && !res.error && !res.detail)) {
        _faultTargets = payload;
        populateClusterDropdown('faultlab-cluster');
        populateClusterDropdown('precisionfaultlab-cluster');
        populateAIClusterDropdown('llmfaultlab-cluster');
        populateAIClusterDropdown('ai-dashboard-cluster');
        populateHostDropdown('llmfaultlab-host');
        renderConfigCenterClusterSummary();
        const pendingHosts = payload.llm_hosts.filter(h => !h.host).length;
        showToast(pendingHosts ? `故障目标已保存；${pendingHosts} 个主机档案待配置地址` : '故障目标已保存');
        closeFaultTargetManager();
    } else {
        showToast('保存失败：' + (res?.error || '未知错误'), 'error');
    }
}

function renderRulesTable(rules) {
    const selected = document.getElementById('kb-rule-status')?.value;
    if (selected) rules = rules.filter(r => (r.status || 'active') === selected);
    const tbody = document.getElementById('kb-rules-body');
    if (!rules.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">当前筛选下没有规则</td></tr>';
        return;
    }
    tbody.innerHTML = rules.map(r => {
        const srcBadge = r.source === 'auto_learn' ? '<span class="badge badge-info">自动学习</span>'
            : r.source === 'supervised' ? '<span class="badge badge-success">监督学习</span>'
            : '<span class="badge badge-gray">手动</span>';
        const condition = r.condition_zh || r.condition || r.text_zh || r.text || '';
        const conclusion = r.conclusion_zh || r.conclusion || '';
        const name = r.name || condition.substring(0, 20) || r.rule_id || '';
        const state = r.status || 'active';
        const statusText = {candidate:'候选待审核', active:'已启用', inactive:'已停用', rejected:'已驳回',source_blocked:'反馈来源隔离'}[state] || state;
        const actionText = state === 'source_blocked' ? '查看隔离原因' : state === 'active' ? '停用' : state === 'candidate' ? '审核' : '重新提交候选';
        return `<tr>
            <td style="font-weight:600;min-width:100px">${escapeHtml(name)}<small style="display:block">${escapeHtml(r.cluster_id || '未指定集群')} / ${escapeHtml(r.namespace || '未指定命名空间')}</small></td>
            <td style="max-width:250px">${escapeZh(condition)}</td>
            <td style="max-width:250px">${escapeZh(conclusion)}</td>
            <td><span class="badge badge-info">${escapeHtml(r.fault_type_zh || faultTypeLabel(r.fault_type || '-'))}</span></td>
            <td>${escapeHtml(String(r.confidence ?? '-'))}</td>
            <td>${srcBadge}<span class="badge badge-${state === 'active' ? 'info' : 'warning'}">${escapeHtml(statusText)}</span></td>
            <td style="white-space:nowrap">
                <button class="btn btn-sm" onclick="editRule('${escapeHtml(r.rule_id||'')}')">编辑</button>
                <button class="btn btn-sm" onclick="openRuleReview('${escapeHtml(r.rule_id||'')}')">${escapeHtml(actionText)}</button>
                <button class="btn btn-sm" onclick="showRuleHistory('${escapeHtml(r.rule_id||'')}')">历史</button>
            </td>
        </tr>`;
    }).join('');
}

// ── Rule form helpers ──

let _allRules = [];

function showAddRuleForm() {
    document.getElementById('rule-form').style.display = 'block';
    document.getElementById('rule-form-title').textContent = '添加诊断规则';
    document.getElementById('rule-edit-id').value = '';
    document.getElementById('rule-f-name').value = '';
    document.getElementById('rule-f-condition').value = '';
    document.getElementById('rule-f-conclusion').value = '';
    document.getElementById('rule-f-type').value = '资源不足';
    document.getElementById('rule-f-namespace').value = '';
    document.getElementById('rule-f-cluster').value = '';
    document.getElementById('rule-f-confidence').value = '0.8';
}

function hideRuleForm() {
    document.getElementById('rule-form').style.display = 'none';
}

function editRule(ruleId) {
    const rule = _allRules.find(r => r.rule_id === ruleId);
    if (!rule) return;
    document.getElementById('rule-form').style.display = 'block';
    document.getElementById('rule-form-title').textContent = '编辑诊断规则';
    document.getElementById('rule-edit-id').value = ruleId;
    document.getElementById('rule-edit-id').dataset.revision = rule.revision || '';
    document.getElementById('rule-f-name').value = rule.name || '';
    document.getElementById('rule-f-condition').value = rule.condition || '';
    document.getElementById('rule-f-conclusion').value = rule.conclusion || '';
    ensureSelectOption('rule-f-type', rule.fault_type || '其他');
    document.getElementById('rule-f-type').value = rule.fault_type || '其他';
    document.getElementById('rule-f-namespace').value = rule.namespace || '';
    document.getElementById('rule-f-cluster').value = rule.cluster_id || '';
    document.getElementById('rule-f-confidence').value = rule.confidence ?? 0.8;
    document.getElementById('rule-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveRule() {
    const editId = document.getElementById('rule-edit-id').value;
    const payload = {
        name: document.getElementById('rule-f-name').value.trim(),
        condition: document.getElementById('rule-f-condition').value.trim(),
        conclusion: document.getElementById('rule-f-conclusion').value.trim(),
        fault_type: document.getElementById('rule-f-type').value,
        namespace: document.getElementById('rule-f-namespace').value.trim(),
        cluster_id: document.getElementById('rule-f-cluster').value.trim(),
        confidence: Number(document.getElementById('rule-f-confidence').value),
    };
    if (!payload.condition || !payload.conclusion) { alert('请填写条件和结论'); return; }
    if (!payload.name) payload.name = payload.condition.substring(0, 30);

    if (editId) payload.revision = document.getElementById('rule-edit-id').dataset.revision;
    let result;
    try {
        result = await api(editId ? `/api/knowledge/rules/${encodeURIComponent(editId)}` : '/api/knowledge/rules', {
            method: editId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload), throwOnError: true, suppressErrorLog: true,
        });
    } catch (e) {
        showToast('保存失败：' + e.message, 'error');
        return;
    }
    if (result?.status === 'ok') {
        showToast(result.rule_status === 'candidate' ? '候选已保存，尚未启用' : '规则已保存');
        hideRuleForm();
        loadKnowledge();
    } else {
        showToast('保存失败', 'error');
    }
}

function openRuleReview(ruleId) {
    const rule = _allRules.find(r => r.rule_id === ruleId);
    if (!rule) return;
    if (rule.status === 'source_blocked') {
        showToast(`该规则因反馈来源或投票依据失效而隔离（${rule.source_block_reason || '待核对'}）；原来源撤销后不能直接启用。`, 'error');
        return;
    }
    const field = document.getElementById('rule-review-id');
    field.value = ruleId;field.dataset.revision = rule.revision;
    field.dataset.eventId = `review-${globalThis.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)}`;
    const choices = rule.status === 'active' ? [['deactivate','停用当前版本']]
        : rule.status === 'candidate' ? [['approve','人工审核并启用'],['reject','驳回候选']]
        : [['resubmit','重新提交为候选（不会直接启用）']];
    const select = document.getElementById('rule-review-decision');select.replaceChildren();
    for (const [value, text] of choices) select.add(new Option(text, value));
    document.getElementById('rule-review-summary').textContent = `${rule.name || rule.condition}；范围 ${rule.cluster_id || '未指定集群'} / ${rule.namespace || '未指定命名空间'}；来源事故 ${rule.source_incident_id || '未记录'}；来源反馈 ${rule.source_feedback_id || '未记录'}；内容版本 ${rule.revision}`;
    document.getElementById('rule-review-reason').value = '';
    document.getElementById('rule-review-result').textContent = '';
    const form = document.getElementById('rule-review-form');form.style.display = 'block';
    form.scrollIntoView({behavior:'smooth',block:'center'});
}

async function submitRuleReview() {
    const field = document.getElementById('rule-review-id'), button = document.getElementById('rule-review-submit');
    const output = document.getElementById('rule-review-result');
    const payload = {revision:field.dataset.revision,event_id:field.dataset.eventId,
        decision:document.getElementById('rule-review-decision').value,
        reviewer:document.getElementById('rule-reviewer').value.trim(),reason:document.getElementById('rule-review-reason').value.trim()};
    if (!payload.reason) {output.textContent='请填写证据与适用条件核对依据';return;}
    button.disabled=true;
    try {
        const result=await api(`/api/knowledge/rules/${encodeURIComponent(field.value)}/decision`,{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),throwOnError:true,suppressErrorLog:true});
        const labels={active:'已启用',candidate:'候选待审核',inactive:'已停用',rejected:'已驳回',source_blocked:'反馈来源隔离'};
        output.textContent=`${result.replayed?'已返回原审核记录；':''}当前状态：${labels[result.current_status]||result.current_status}。人工审核不代表独立收益已验证。`;
        await loadKnowledge();
    } catch(e) {output.textContent=`审核未完成：${e.message}。若版本变化，请刷新后重新审核。`;}
    finally {button.disabled=false;}
}

async function showRuleHistory(ruleId) {
    const panel=document.getElementById('rule-history-panel'), output=document.getElementById('rule-history-content');
    panel.style.display='block';output.textContent='正在读取历史…';
    try {
        let result=await api(`/api/knowledge/rules/${encodeURIComponent(ruleId)}/history?limit=100`,{throwOnError:true,suppressErrorLog:true});
        if(result.total>result.events.length) result=await api(`/api/knowledge/rules/${encodeURIComponent(ruleId)}/history?limit=100&offset=${Math.max(0,result.total-100)}&revision=${encodeURIComponent(result.revision)}`,{throwOnError:true,suppressErrorLog:true});
        const labels={approve:'人工启用',reject:'驳回',deactivate:'停用',resubmit:'重提候选',edit:'编辑后待审'};
        output.textContent=result.events.length ? `共 ${result.total} 项，展示最近 ${result.events.length} 项\n\n`+result.events.map(e=>`${formatTime(e.at*1000)} ${labels[e.operation]||e.operation}\n审核人：${e.reviewer||'编辑入口'}（${e.identity_source||'未记录身份'}）\n依据：${e.reason||'内容编辑，须重新审核'}\n原版本：${e.before_revision}\n新版本：${e.revision}`).join('\n\n') : '此规则尚无版本审核记录；旧状态不代表独立验证。';
    } catch(e) {output.textContent=`历史不可用：${e.message}`;}
    panel.scrollIntoView({behavior:'smooth',block:'center'});
}

function renderFaultsTable(faults) {
    const tbody = document.getElementById('kb-faults-body');
    if (!faults.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-muted" style="text-align:center;padding:20px">暂无故障记录</td></tr>';
        return;
    }
    tbody.innerHTML = faults.map(f => {
        const ts = f.timestamp ? formatTime(f.timestamp * 1000) : '-';
        const description = f.description_zh || f.description || f.text_zh || f.text || '';
        const rootCause = f.root_cause_zh || f.root_cause || '';
        return `<tr>
            <td style="font-size:11px;font-family:var(--font-mono)">${escapeHtml((f.fault_id||'').substring(0,12))}</td>
            <td>${escapeZh(description.substring(0,80))}</td>
            <td>${escapeZh(rootCause.substring(0,80))}</td>
            <td>${escapeHtml(f.fault_type_zh || faultTypeLabel(f.fault_type || '-'))}${f.memory_status === 'candidate' ? '<span class="badge badge-warning">候选事例，未用于诊断</span>' : ''}</td>
            <td style="font-size:12px">${ts}</td>
            <td><button class="btn btn-sm btn-danger" onclick="deleteFault('${escapeHtml(f.fault_id||'')}')">删除</button></td>
        </tr>`;
    }).join('');
}

function feedbackLearningText(result = {}) {
    if (result.feedback_state === 'retracted') return '已撤销：不再用于新诊断统计；关联规则停止消费或隔离，历史保留';
    if (result.replayed && result.learning_status !== 'outcome_unknown') return '已返回原反馈收据，未重复学习';
    const states = {
        blocked_no_trace: '反馈已记录，未匹配原诊断，未学习',
        blocked_trace_unavailable: '原诊断不可核验，未学习',
        blocked_stale_diagnosis: '诊断版本已变化，未学习',
        blocked_no_feedback_store: '缺少持久反馈通道，未学习',
        invalid_correctness: '模型对照标签无效，未计票',
        invalid_rules: '模型输出结构无效，未写入规则',
        no_learner: '反馈已记录，模型未配置，未学习',
        processing: '反馈处理中；重复提交不会重启学习',
        outcome_unknown: '处理结果待核对，未自动重试',
        error: '反馈处理未完成',
    };
    if (result.learning_status === 'success') {
        if (result.rule_status === 'no_new_rules') return '已完成模型对照，未新增规则；已有规则保留原审核状态，未重新启用';
        return result.rule_status === 'candidate'
            ? `已完成模型对照，生成 ${Number(result.rules_generated) || 0} 条候选规则，尚未启用；尚未证明独立诊断收益`
            : `已完成模型对照，生成 ${Number(result.rules_generated) || 0} 条规则，启用状态未记录；尚未证明独立诊断收益`;
    }
    return states[result.learning_status] || '反馈已记录，学习状态待核对';
}

let _knowledgeFeedback = [];
function renderFeedbackList(feedback) {
    _knowledgeFeedback = feedback;
    const container = document.getElementById('kb-feedback-list');
    if (!feedback.length) { container.innerHTML = '<p class="text-muted">暂无反馈记录</p>'; return; }
    container.innerHTML = feedback.map(f => {
        const statusBadge = `<span class="badge badge-${f.learning_status === 'success' ? 'info' : 'warning'}">${escapeHtml(feedbackLearningText(f))}</span>`;
        return `<div class="signal-item">
            <span><span class="badge badge-info">${escapeHtml(f.incident_id||'')}</span> ${escapeZh((f.expert_diagnosis||'').substring(0,100))} ${statusBadge}</span>
            <span class="text-muted">${formatTime(f.timestamp ? f.timestamp * 1000 : null)}</span>
            ${f.feedback_state !== 'retracted' ? `<button class="btn btn-sm" onclick="openFeedbackRetraction('${escapeHtml(f.feedback_id || '')}')">撤销反馈</button>` : `<details><summary>撤销记录</summary><pre>${escapeHtml(JSON.stringify(f.feedback_history || [], null, 2))}</pre></details>`}
        </div>`;
    }).join('');
}

function openFeedbackRetraction(feedbackId) {
    const entry = _knowledgeFeedback.find(f => f.feedback_id === feedbackId);
    if (!entry) return;
    const field = document.getElementById('feedback-retract-id');
    field.value = feedbackId;
    field.dataset.revision = entry.feedback_revision;
    field.dataset.eventId = `retract-${globalThis.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)}`;
    document.getElementById('feedback-retract-reason').value = '';
    document.getElementById('feedback-retract-result').textContent = '';
    const form = document.getElementById('feedback-retract-form');
    form.style.display = 'block';form.scrollIntoView({behavior:'smooth',block:'center'});
}

async function submitFeedbackRetraction() {
    const field = document.getElementById('feedback-retract-id');
    const button = document.getElementById('feedback-retract-submit');
    const output = document.getElementById('feedback-retract-result');
    const payload = {revision:field.dataset.revision,event_id:field.dataset.eventId,
        reviewer:document.getElementById('feedback-retract-reviewer').value.trim(),
        reason:document.getElementById('feedback-retract-reason').value.trim()};
    if (!payload.reviewer || !payload.reason) { output.textContent='请填写操作人及撤销依据';return; }
    button.disabled = true;
    try {
        const result = await api(`/api/knowledge/feedback/${encodeURIComponent(field.value)}/retract`, {
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        output.textContent = result?.feedback_state === 'retracted'
            ? feedbackLearningText(result) : '撤销未确认；请核对后用同一表单重试，版本冲突时刷新记录。';
        if (result?.feedback_state === 'retracted') await loadKnowledge();
    } finally { button.disabled=false; }
}

async function deleteRule(ruleId) {
    openRuleReview(ruleId);
}

async function addFault() {
    const description = prompt('故障描述:');
    if (!description) return;
    const rootCause = prompt('根因:');
    const faultType = prompt('故障类型:', '');
    const resolution = prompt('解决方案:', '');

    const result = await api('/api/knowledge/faults', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description, root_cause: rootCause || '', fault_type: faultType || '', resolution: resolution || '' }),
    });
    if (result?.status === 'ok') { showToast('故障记录添加成功'); loadKnowledge(); }
    else showToast('添加失败', 'error');
}

async function deleteFault(faultId) {
    if (!confirm('确认删除此故障记录？')) return;
    await api(`/api/knowledge/faults/${faultId}`, { method: 'DELETE' });
    showToast('故障记录已删除');
    loadKnowledge();
}

async function searchKnowledge() {
    const q = document.getElementById('kb-search-input')?.value?.trim();
    const container = document.getElementById('kb-search-results');
    if (!q) { container.innerHTML = ''; return; }

    container.innerHTML = '<div class="loading">搜索中</div>';
    const data = await api(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
    if (!data) { container.innerHTML = '<p class="text-danger">搜索失败</p>'; return; }

    let html = '';
    if (data.rules?.length) {
        html += '<h3 style="margin:12px 0 8px">匹配的诊断规则</h3>';
        data.rules.forEach(r => {
            html += `<div class="signal-item"><span><span class="badge badge-info">规则</span> <strong>${escapeZh(r.condition_zh || r.condition || r.text_zh || r.text || '')}</strong> → ${escapeZh(r.conclusion_zh || r.conclusion || '')}</span></div>`;
        });
    }
    if (data.faults?.length) {
        html += '<h3 style="margin:12px 0 8px">匹配的故障记录</h3>';
        data.faults.forEach(f => {
            html += `<div class="signal-item"><span><span class="badge badge-warning">故障</span> ${escapeZh(f.description_zh || f.description || f.text_zh || f.text || '')} — <strong>${escapeZh(f.root_cause_zh || f.root_cause || '')}</strong></span></div>`;
        });
    }
    if (!data.rules?.length && !data.faults?.length) {
        html = '<p class="text-muted" style="padding:12px">未找到匹配结果</p>';
    }
    container.innerHTML = html;
}

async function loadFeedbackIncidents() {
    const sel = document.getElementById('fb-incident-select');
    if (!sel) return;
    const data = await api('/api/rca/history?limit=20');
    sel.innerHTML = '<option value="">— 选择最近 RCA 分析 —</option>';
    (data?.runs || []).forEach(r => {
        const sid = r.session_id || r.id;
        const ts = r.started_at ? new Date(r.started_at * 1000).toLocaleString() : '';
        const q = (r.query || '').slice(0, 50);
        const opt = document.createElement('option');
        opt.value = sid;
        opt.textContent = `${sid} · ${ts} · ${q}`;
        sel.appendChild(opt);
    });
}

function onIncidentSelectChange() {
    const sel = document.getElementById('fb-incident-select');
    const input = document.getElementById('fb-incident-id');
    if (sel && input && sel.value) input.value = sel.value;
}

async function submitFeedback() {
    const incidentId = document.getElementById('fb-incident-id')?.value?.trim();
    const diagnosis = document.getElementById('fb-diagnosis')?.value?.trim();
    const comment = document.getElementById('fb-comment')?.value?.trim();
    const resultEl = document.getElementById('fb-result');

    if (!incidentId || !diagnosis) { alert('请选择或填写分析ID，并填写专家诊断结论'); return; }

    resultEl.innerHTML = '<div class="loading">正在提交并核对原诊断与反馈收据</div>';
    const result = await api('/api/knowledge/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ incident_id: incidentId, expert_diagnosis: diagnosis, comment: comment || '' }),
    });

    if (result) {
        const parts = [escapeHtml(feedbackLearningText(result))];
        if (result.learning_status === 'success' && !result.replayed) {
            parts.push(`规则投票 ${Number(result.rules_voted) || 0} 条，仅计入显式有用性判定且版本匹配的规则`);
        }
        resultEl.innerHTML = `<div class="result-banner success" style="margin-top:12px"><h4>反馈已提交</h4><p>${parts.join('；')}</p></div>`;
        showToast('专家反馈已提交');
        loadKnowledge();
    } else {
        resultEl.innerHTML = '<p class="text-danger">提交失败</p>';
    }
}

// ─────────────────────────────────────────
// Reports (故障报告)
// ─────────────────────────────────────────

let _selectedReportRunId = null;

async function loadReportList() {
    const data = await api('/api/rca/history?limit=50');
    const container = document.getElementById('report-list');
    if (!data?.runs?.length) {
        container.innerHTML = '<p class="text-muted" style="padding:12px">暂无历史分析记录。请先在"根因分析"页面运行一次分析。</p>';
        return;
    }

    container.innerHTML = data.runs.map(r => {
        const statusBadge = r.status === 'completed' ? 'badge-success' : r.status === 'running' ? 'badge-warning' : 'badge-danger';
        const canGenerate = r.status && r.status !== 'running';
        return `<div class="signal-item ${canGenerate ? 'report-item-ready' : 'report-item-disabled'}" style="cursor:${canGenerate ? 'pointer' : 'default'}" data-report-run-id="${escapeHtmlAttr(r.id || '')}" ${canGenerate ? 'onclick="generateReportFromItem(this)"' : ''}>
            <span>
                <span class="badge ${statusBadge}">${escapeHtml(statusLabel(r.status))}</span>
                <strong>${escapeHtml(r.id)}</strong> — ${escapeZh((r.query||'').substring(0,80))}
            </span>
            <span class="text-muted">${canGenerate ? '点击生成报告' : '分析中，完成后可生成'} · ${formatTime(r.started_at ? r.started_at * 1000 : null)}</span>
        </div>`;
    }).join('');
}

function generateReportFromItem(el) {
    const runId = el?.dataset?.reportRunId || '';
    if (!runId) {
        showToast('未找到报告运行 ID', 'error');
        return;
    }
    generateReport(runId);
}

async function generateReport(runId) {
    _selectedReportRunId = runId;
    const card = document.getElementById('report-preview-card');
    const preview = document.getElementById('report-preview');
    card.style.display = 'block';
    preview.innerHTML = '<div class="loading">生成报告中</div>';

    const report = await api(`/api/report/${runId}`);
    if (!report) {
        preview.innerHTML = '<p class="text-danger">报告生成失败。请确认该 RCA 已完成，运行中的 Agent+Skill 分析不会生成最终故障报告。</p>';
        return;
    }

    const bi = report.basic_info || {};
    const diag = report.diagnosis || {};
    const rem = report.remediation || {};
    const qual = report.quality || {};
    const sugg = report.suggestions || {};

    let html = `<div style="border-bottom:2px solid var(--accent);padding-bottom:12px;margin-bottom:16px">
        <h3 style="color:var(--accent);font-size:18px;margin-bottom:4px">故障诊断报告</h3>
        <span class="text-muted">${escapeHtml(bi['报告编号']||'')} | ${escapeHtml(bi['开始时间']||'')}</span>
    </div>`;

    // 基本信息
    html += '<h3 style="margin-bottom:8px">一、基本信息</h3><div class="rca-meta-grid">';
    for (const [k, v] of Object.entries(bi)) {
        html += `<div class="rca-meta-item"><div class="meta-label">${escapeHtml(k)}</div><div class="meta-value">${escapeHtml(String(v))}</div></div>`;
    }
    html += '</div>';

    // 假设列表
    html += '<h3 style="margin:16px 0 8px">二、诊断假设</h3>';
    if (diag['假设列表']?.length) {
        html += diag['假设列表'].map((h,i) => `<div class="hyp-item"><span class="hyp-rank">#${h['排名']}</span><div class="hyp-bar-wrap"><div class="hyp-bar" style="width:${parseInt(h['置信度'])}%"></div></div><span class="hyp-conf">${h['置信度']}</span><span class="hyp-desc">${escapeZh(h['描述'])}</span></div>`).join('');
    } else { html += '<p class="text-muted">无假设数据</p>'; }

    // 证据收集
    html += '<h3 style="margin:16px 0 8px">三、证据收集</h3>';
    if (diag['证据收集']?.length) {
        html += '<div class="evidence-grid">';
        diag['证据收集'].forEach(e => {
            html += `<div class="evidence-card ${e['成功'] ? 'success' : 'error'}"><div class="ev-agent">${e['成功'] ? '&#10003;' : '&#9888;'} ${escapeHtml(e['智能体'])}</div><div class="ev-summary">${escapeZh(e['结果'])}</div></div>`;
        });
        html += '</div>';
    } else { html += '<p class="text-muted">无证据数据</p>'; }

    // 根因结论
    html += '<h3 style="margin:16px 0 8px">四、根因结论</h3>';
    html += `<div class="root-cause" style="background:var(--accent-bg);border-left:4px solid var(--accent);padding:14px;border-radius:0 6px 6px 0;font-size:15px;font-weight:600">${escapeZh(diag['根因结论']||'N/A')}</div>`;
    html += `<p style="margin:8px 0;font-size:13px"><strong>置信度:</strong> ${diag['置信度']||'-'} &nbsp; <strong>故障类型:</strong> ${escapeHtml(faultTypeLabel(diag['故障类型']||'-'))} &nbsp; <strong>受影响服务:</strong> ${escapeHtml((diag['受影响服务']||[]).join(', ')||'-')}</p>`;

    // 自愈
    html += '<h3 style="margin:16px 0 8px">五、自愈修复</h3>';
    html += `<p><strong>状态:</strong> ${escapeHtml(statusLabel(rem['状态']||'未触发'))}</p>`;

    // 质量评估
    if (qual['评级']) {
        html += '<h3 style="margin:16px 0 8px">六、质量评估</h3>';
        html += `<p><span class="badge badge-info">${escapeHtml(qual['评级'])}</span> 评分: ${typeof qual['评分'] === 'number' ? qual['评分'].toFixed(3) : '-'}</p>`;
    }

    // 建议
    if (sugg['修复建议'] || sugg['预防措施']) {
        html += '<h3 style="margin:16px 0 8px">七、建议</h3>';
        if (sugg['修复建议']) html += `<div class="rca-remediation"><strong>修复建议:</strong> ${escapeZh(sugg['修复建议'])}</div>`;
        if (sugg['预防措施']) html += `<div class="rca-remediation" style="margin-top:8px"><strong>预防措施:</strong> ${escapeZh(sugg['预防措施'])}</div>`;
    }

    if (report.supplemental_lineage?.length) {
        html += '<h3 style="margin:16px 0 8px">补充：发布与镜像追溯</h3><p>诊断后独立采集，未经过原诊断审核；关系不能单独证明因果，修订号不代表 Git 提交。</p>';
        html += report.supplemental_lineage.map(s => `<details class="skill-raw-fold"><summary>${escapeHtml(s.target.kind)}/${escapeHtml(s.target.name)} · ${escapeHtml(s.finished_at)}</summary><pre>${escapeHtml(JSON.stringify(s,null,2))}</pre></details>`).join('');
        if (report.lineage_comparisons?.length) html += `<details class="skill-raw-fold"><summary>同一对象最近两次快照比较</summary><p>仅表示已采集观测的差异，不能证明发布或故障因果。</p><pre>${escapeHtml(JSON.stringify(report.lineage_comparisons,null,2))}</pre></details>`;
    }
    html += `<h3 style="margin:16px 0 8px">历史知识检索</h3>${historyRetrievalHtml(report.history_retrieval)}`;
    if (report.diagnostic_strategy) html += `<h3 style="margin:16px 0 8px">假设产生与调查调整</h3><details class="skill-raw-fold"><summary>已记录的候选与策略</summary><pre>${escapeHtml(JSON.stringify(report.diagnostic_strategy,null,2))}</pre></details>`;
    preview.innerHTML = html;
}

function exportReport() {
    if (!_selectedReportRunId) { alert('请先选择一次分析记录'); return; }
    window.open(`/api/report/${_selectedReportRunId}/export`, '_blank');
}

function exportReportWord() {
    if (!_selectedReportRunId) { alert('请先选择一次分析记录'); return; }
    window.open(`/api/report/${_selectedReportRunId}/word`, '_blank');
}

// ─────────────────────────────────────────
// Hermes Agent Chat
// ─────────────────────────────────────────

let _hermesSessionId = '';
let _hermesEventSource = null;
let _hermesCapabilities = null;
let _hermesBusy = false;

function loadHermesView() {
    Promise.allSettled([loadHermesCapabilities(), loadHermesSessions()]);
}

async function loadHermesCapabilities() {
    const runtimeDot = document.getElementById('hermes-runtime-dot');
    const modelDot = document.getElementById('hermes-model-dot');
    const toolDot = document.getElementById('hermes-tool-dot');
    const runtimeSummary = document.getElementById('hermes-runtime-summary');
    const modelSummary = document.getElementById('hermes-model-summary');
    const toolSummary = document.getElementById('hermes-tool-summary');
    if (!runtimeDot || !runtimeSummary || !modelSummary || !toolSummary) return;

    runtimeDot.className = 'hermes-capability-dot pending';
    if (modelDot) modelDot.className = 'hermes-capability-dot pending';
    if (toolDot) toolDot.className = 'hermes-capability-dot pending';
    runtimeSummary.textContent = '检查中';
    try {
        const data = await api('/api/hermes/capabilities');
        if (!data) throw new Error('能力状态不可用');
        _hermesCapabilities = data;
        const runtime = data.skill_runtime || {};
        const llm = data.llm || {};
        const toolset = data.toolset || {};
        const agent = data.hermes_agent || {};
        const domains = Array.isArray(runtime.domains) ? runtime.domains : [];

        runtimeDot.className = `hermes-capability-dot ${runtime.connected ? 'ready' : 'error'}`;
        if (modelDot) modelDot.className = `hermes-capability-dot ${llm.configured ? 'ready' : 'error'}`;
        if (toolDot) toolDot.className = `hermes-capability-dot ${agent.available ? 'ready' : 'error'}`;
        runtimeSummary.textContent = runtime.connected
            ? `${runtime.skill_count || 0} Skills · ${domains.length} 领域`
            : '未连接';
        runtimeSummary.title = runtime.connected
            ? domains.map(item => `${item.id}: ${item.count}`).join(' · ')
            : (runtime.error || 'Skill 服务不可用');
        modelSummary.textContent = llm.configured ? (llm.model || '已配置') : '未配置';
        modelSummary.title = llm.model || '';
        toolSummary.textContent = agent.available
            ? `${toolset.skill_tool_count ?? runtime.skill_count ?? 0} 个 Skill 可调用`
            : '助手不可用';
        toolSummary.title = Array.isArray(toolset.skill_tools)
            ? toolset.skill_tools.map(item => `${item.skill_id} → ${item.tool}`).join('\n')
            : '';

        const statusEl = document.getElementById('hermes-status');
        if (!_hermesBusy && statusEl) {
            statusEl.textContent = data.status === 'ready' ? '就绪' : '能力异常';
            statusEl.className = data.status === 'ready' ? 'badge badge-info' : 'badge badge-danger';
        }
    } catch (error) {
        _hermesCapabilities = null;
        runtimeDot.className = 'hermes-capability-dot error';
        if (modelDot) modelDot.className = 'hermes-capability-dot error';
        if (toolDot) toolDot.className = 'hermes-capability-dot error';
        runtimeSummary.textContent = '检查失败';
        runtimeSummary.title = error.message || '';
        modelSummary.textContent = '-';
        toolSummary.textContent = '-';
    } finally {
        _hermesSetBusy(_hermesBusy);
    }
}

async function loadHermesSessions() {
    const select = document.getElementById('hermes-session-select');
    if (!select) return;
    const selected = _hermesSessionId || select.value;
    try {
        const data = await api('/api/hermes/sessions');
        const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
        select.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = sessions.length ? '最近会话' : '暂无历史会话';
        select.appendChild(placeholder);
        sessions.forEach(session => {
            const option = document.createElement('option');
            option.value = String(session.id || '');
            const created = session.created_at ? new Date(session.created_at * 1000).toLocaleString('zh-CN', {
                month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
            }) : '';
            option.textContent = `${shortText(session.last_message || session.id || '', 26)}${created ? ` · ${created}` : ''}`;
            select.appendChild(option);
        });
        select.value = sessions.some(item => item.id === selected) ? selected : '';
    } catch (error) {
        select.title = error.message || '会话列表加载失败';
    }
}

async function hermesLoadSession(sessionId) {
    const cleanId = String(sessionId || '').trim();
    if (!cleanId || cleanId === _hermesSessionId) return;
    _hermesEventSource?.close();
    _hermesEventSource = null;
    _hermesSessionId = cleanId;
    _hermesSetBusy(true);
    try {
        const data = await api(`/api/hermes/chat/${encodeURIComponent(cleanId)}`);
        const messages = document.getElementById('hermes-messages');
        messages.replaceChildren();
        (data.messages || []).forEach(message => {
            if (message.role === 'user' || message.role === 'assistant') {
                _hermesAddMessage(
                    message.role,
                    message.content,
                    message.tool_calls,
                    message.skill_tool_results,
                );
            }
        });
        if (!(data.messages || []).length) _hermesRenderWelcome();
        messages.scrollTop = 0;
        const statusEl = document.getElementById('hermes-status');
        const thinking = data.status === 'thinking';
        statusEl.textContent = thinking ? '思考中...' : (data.status === 'error' ? '错误' : '就绪');
        statusEl.className = thinking ? 'badge badge-warning' : (data.status === 'error' ? 'badge badge-danger' : 'badge badge-info');
        await _hermesUpdateTokens(cleanId);
        if (thinking) {
            _hermesShowThinking();
            _hermesStartStream(cleanId, (data.messages || []).length);
        } else {
            _hermesSetBusy(false);
        }
    } catch (error) {
        _hermesAddMessage('assistant', `会话加载失败: ${error.message}`);
        _hermesSetBusy(false);
    }
}

function hermesNewSession() {
    _hermesEventSource?.close();
    _hermesEventSource = null;
    _hermesSessionId = '';
    document.getElementById('hermes-messages').replaceChildren();
    _hermesRenderWelcome();
    document.getElementById('hermes-status').textContent = '就绪';
    document.getElementById('hermes-status').className = 'badge badge-info';
    document.getElementById('hermes-tokens').textContent = 'Token: 0';
    const select = document.getElementById('hermes-session-select');
    if (select) select.value = '';
    _hermesSetBusy(false);
    document.getElementById('hermes-input')?.focus();
}

function _hermesRenderWelcome() {
    const messages = document.getElementById('hermes-messages');
    if (!messages) return;
    const welcome = document.createElement('div');
    welcome.className = 'hermes-welcome';
    const mark = document.createElement('div');
    mark.className = 'hermes-welcome-mark';
    mark.textContent = 'AI';
    const title = document.createElement('h3');
    title.textContent = '开始一次运维分析';
    const description = document.createElement('p');
    description.textContent = '描述故障现象、目标集群或需要调用的 Skill。';
    welcome.append(mark, title, description);
    messages.appendChild(welcome);
}

function _hermesChatAvailable() {
    if (!_hermesCapabilities) return true;
    return Boolean(_hermesCapabilities.hermes_agent?.available && _hermesCapabilities.llm?.configured);
}

function _hermesSetBusy(busy) {
    _hermesBusy = Boolean(busy);
    const available = _hermesChatAvailable();
    const send = document.getElementById('hermes-send-btn');
    const input = document.getElementById('hermes-input');
    const sessions = document.getElementById('hermes-session-select');
    const newSession = document.getElementById('hermes-new-session-btn');
    if (send) send.disabled = _hermesBusy || !available;
    if (input) input.disabled = !available;
    if (sessions) sessions.disabled = _hermesBusy;
    if (newSession) newSession.disabled = _hermesBusy;
}

function _hermesShowThinking() {
    _hermesRemoveThinking();
    const messages = document.getElementById('hermes-messages');
    const pending = document.createElement('div');
    pending.id = 'hermes-thinking';
    pending.className = 'hermes-thinking';
    const dot = document.createElement('span');
    dot.className = 'hermes-thinking-dot';
    const label = document.createElement('span');
    label.textContent = '智能助手正在分析，工具结果将在本轮完成后显示';
    pending.append(dot, label);
    messages.appendChild(pending);
    messages.scrollTop = messages.scrollHeight;
}

function _hermesRemoveThinking() {
    document.getElementById('hermes-thinking')?.remove();
}

function hermesSendInput() {
    const input = document.getElementById('hermes-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    hermesResizeInput(input);
    hermesSend(msg);
}

function hermesResizeInput(input) {
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

function hermesHandleInputKeydown(event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    hermesSendInput();
}

async function hermesSend(message) {
    // Remove welcome if first message
    const welcome = document.querySelector('.hermes-welcome');
    if (welcome) welcome.remove();

    // Add user message
    _hermesAddMessage('user', message);
    _hermesShowThinking();

    // Update status
    const statusEl = document.getElementById('hermes-status');
    statusEl.textContent = '思考中...';
    statusEl.className = 'badge badge-warning';
    _hermesSetBusy(true);

    // Send to API
    try {
        const resp = await fetch('/api/hermes/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message, session_id: _hermesSessionId}),
        });
        const text = await resp.text();
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text || resp.statusText }; }
        if (!resp.ok) {
            const msg = typeof data?.detail === 'string'
                ? data.detail
                : data?.detail?.message || data?.message || data?.error || `HTTP ${resp.status}`;
            throw new Error(msg);
        }
        if (!data.session_id) throw new Error('智能助手会话创建失败');
        if (data.session_id) _hermesSessionId = data.session_id;

        // Poll for response via SSE
        _hermesStartStream(data.session_id, data.message_count);
    } catch (e) {
        _hermesRemoveThinking();
        _hermesAddMessage('assistant', `Error: ${e.message}`);
        statusEl.textContent = '错误';
        statusEl.className = 'badge badge-danger';
        _hermesSetBusy(false);
    }
}

function _hermesStartStream(sessionId, afterCount) {
    _hermesEventSource?.close();
    const query = Number.isInteger(afterCount) ? `?after=${afterCount}` : '';
    const es = new EventSource(`/api/hermes/chat/${sessionId}/stream${query}`);
    _hermesEventSource = es;
    es.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'done') {
                es.close();
                _hermesEventSource = null;
                _hermesRemoveThinking();
                const statusEl = document.getElementById('hermes-status');
                statusEl.textContent = data.status === 'idle' ? '就绪' : '错误';
                statusEl.className = data.status === 'idle' ? 'badge badge-info' : 'badge badge-danger';
                _hermesSetBusy(false);
                // Update token count
                _hermesUpdateTokens(sessionId);
                loadHermesSessions();
                return;
            }
            if (data.role === 'assistant') {
                _hermesRemoveThinking();
                _hermesAddMessage('assistant', data.content, data.tool_calls, data.skill_tool_results);
            }
        } catch(e) {}
    };
    es.onerror = () => {
        es.close();
        _hermesEventSource = null;
        _hermesRemoveThinking();
        _hermesSetBusy(false);
        // Try fetching final state
        setTimeout(() => _hermesUpdateTokens(sessionId), 1000);
    };
}

async function _hermesUpdateTokens(sessionId) {
    try {
        const data = await api(`/api/hermes/chat/${sessionId}`);
        if (data) {
            document.getElementById('hermes-tokens').textContent =
                `Token: ${(data.total_tokens || 0).toLocaleString()} · 工具调用: ${data.tool_calls_count || 0}`;
        }
    } catch(e) {}
}

function _hermesAddMessage(role, content, toolCalls, skillToolResults) {
    const msgs = document.getElementById('hermes-messages');
    const div = document.createElement('div');
    div.className = `hermes-message hermes-message-${role === 'user' ? 'user' : 'assistant'}`;
    const contentNode = document.createElement('div');
    contentNode.className = 'hermes-message-content';
    contentNode.innerHTML = _hermesFormatContent(content);
    const hasStructuredResults = role !== 'user' && Array.isArray(skillToolResults) && skillToolResults.length > 0;
    if (hasStructuredResults && String(content || '').length > 1200) {
        const details = document.createElement('details');
        details.className = 'hermes-response-details';
        const summary = document.createElement('summary');
        summary.textContent = '文本回复';
        details.append(summary, contentNode);
        div.appendChild(details);
    } else {
        div.appendChild(contentNode);
    }

    if (role !== 'user' && Array.isArray(toolCalls) && toolCalls.length) {
        div.appendChild(_hermesRenderToolActivity(toolCalls));
    }
    if (hasStructuredResults) {
        skillToolResults.forEach(item => div.appendChild(_hermesRenderSkillResult(item)));
    }
    msgs.appendChild(div);
    if (hasStructuredResults) {
        const anchor = div.previousElementSibling?.classList.contains('hermes-message-user')
            ? div.previousElementSibling
            : div;
        msgs.scrollTop = Math.max(0, anchor.offsetTop - msgs.offsetTop - 8);
    } else {
        msgs.scrollTop = msgs.scrollHeight;
    }
}

function _hermesFormatContent(text) {
    if (!text) return '<span style="color:#94a3b8">（无响应）</span>';
    const source = String(text);
    const blockPattern = /```[^\n]*\n?([\s\S]*?)```/g;
    let output = '';
    let cursor = 0;
    let match;
    while ((match = blockPattern.exec(source)) !== null) {
        output += _hermesFormatInline(source.slice(cursor, match.index));
        output += `<pre>${escapeHtml(match[1])}</pre>`;
        cursor = match.index + match[0].length;
    }
    output += _hermesFormatInline(source.slice(cursor));
    return output;
}

function _hermesFormatInline(text) {
    const escaped = escapeHtml(String(text || ''));
    return escaped.split(/(`[^`\n]+`)/g).map(part => {
        if (part.startsWith('`') && part.endsWith('`')) {
            return `<code>${part.slice(1, -1)}</code>`;
        }
        return part
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\n/g, '<br>');
    }).join('');
}

function _hermesCompactJson(value, maxLength = 180) {
    let text;
    try {
        text = typeof value === 'string' ? value : JSON.stringify(value || {});
    } catch {
        text = String(value || '');
    }
    return shortText(text.replace(/\s+/g, ' ').trim(), maxLength);
}

function _hermesStatusTone(status) {
    const value = String(status || '').toLowerCase();
    if (['error', 'failed', 'failure', 'blocked', 'cancelled'].some(item => value.includes(item))) return 'error';
    if (['warning', 'partial', 'pending', 'dry_run', 'skipped'].some(item => value.includes(item))) return 'warning';
    if (['ok', 'completed', 'matched', 'success', 'ready', 'healthy'].some(item => value.includes(item))) return 'success';
    return '';
}

function _hermesConfidence(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '';
    return `${Math.round((number <= 1 ? number * 100 : number) * 10) / 10}%`;
}

function _hermesBadge(text, tone = '') {
    const badge = document.createElement('span');
    badge.className = `hermes-mini-badge ${tone}`.trim();
    badge.textContent = String(text || '-');
    return badge;
}

function _hermesRenderToolActivity(toolCalls) {
    const details = document.createElement('details');
    details.className = 'hermes-tool-activity';
    const summary = document.createElement('summary');
    const skillCount = toolCalls.filter(item => item.is_skill_tool || String(item.tool || '').startsWith('agenticsre_')).length;
    summary.textContent = `工具过程 ${toolCalls.length}${skillCount ? ` · Skill ${skillCount}` : ''}`;
    const list = document.createElement('div');
    list.className = 'hermes-tool-list';
    toolCalls.forEach(call => {
        const row = document.createElement('div');
        const isSkill = call.is_skill_tool || String(call.tool || '').startsWith('agenticsre_');
        row.className = `hermes-tool-row${isSkill ? ' skill-tool' : ''}`;
        const state = document.createElement('span');
        const tone = _hermesStatusTone(call.status);
        state.className = `hermes-tool-state${tone === 'error' ? ' error' : ''}`;
        const name = document.createElement('span');
        name.className = 'hermes-tool-name';
        name.textContent = call.tool || 'unknown_tool';
        const args = document.createElement('span');
        args.className = 'hermes-tool-args';
        args.textContent = _hermesCompactJson(call.args_json || call.args || {});
        args.title = typeof call.args === 'string' ? call.args : _hermesCompactJson(call.args_json || {}, 1000);
        row.append(state, name, args);
        list.appendChild(row);
    });
    details.append(summary, list);
    return details;
}

function _hermesResultSection(title, metaItems = []) {
    const section = document.createElement('section');
    section.className = 'hermes-skill-result';
    const header = document.createElement('div');
    header.className = 'hermes-result-header';
    const heading = document.createElement('strong');
    heading.textContent = title;
    header.appendChild(heading);
    if (metaItems.length) {
        const meta = document.createElement('div');
        meta.className = 'hermes-result-meta';
        metaItems.filter(Boolean).forEach(item => {
            const span = document.createElement('span');
            span.textContent = item;
            meta.appendChild(span);
        });
        header.appendChild(meta);
    }
    section.appendChild(header);
    return section;
}

function _hermesRenderSkillResult(activity) {
    const tool = String(activity?.tool || '');
    const result = activity?.result && typeof activity.result === 'object' ? activity.result : {};
    if (tool === 'agenticsre_skills_list') return _hermesRenderSkillCatalog(result);
    if (tool === 'agenticsre_skill_detail') return _hermesRenderSkillDetail(result);
    if (tool === 'agenticsre_skill_run') return _hermesRenderDirectSkillRun(result);
    if (tool === 'agenticsre_skill_workflow') return _hermesRenderSkillWorkflow(result);

    const section = _hermesResultSection(tool || 'Skill 调用结果', [activity?.status || '']);
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(activity?.result ?? {}, null, 2);
    section.appendChild(pre);
    return section;
}

function _hermesRenderSkillCatalog(result) {
    const skills = Array.isArray(result.skills) ? result.skills : [];
    const returnedCount = result.returned_count ?? skills.length;
    const matchedCount = result.matched_count ?? skills.length;
    const runtimeCount = result.runtime_skill_count ?? skills.length;
    const catalogMeta = returnedCount === runtimeCount && matchedCount === runtimeCount
        ? [`${runtimeCount} Skills`]
        : [`显示 ${returnedCount}`, `匹配 ${matchedCount}`, `总计 ${runtimeCount}`];
    const section = _hermesResultSection('Skill 目录', catalogMeta);
    const toolbar = document.createElement('div');
    toolbar.className = 'hermes-catalog-toolbar';
    const search = document.createElement('input');
    search.type = 'search';
    search.className = 'hermes-catalog-search';
    search.placeholder = '筛选名称、ID、描述或标签';
    const visibleCount = document.createElement('span');
    visibleCount.className = 'hermes-mini-badge';
    toolbar.append(search, visibleCount);

    const tabs = document.createElement('div');
    tabs.className = 'hermes-domain-tabs';
    const list = document.createElement('div');
    list.className = 'hermes-skill-list';
    const domainCounts = new Map();
    skills.forEach(skill => {
        const domain = String(skill.domain || 'generic');
        domainCounts.set(domain, (domainCounts.get(domain) || 0) + 1);
    });
    let activeDomain = '';

    const renderTabs = () => {
        tabs.replaceChildren();
        [['', skills.length], ...Array.from(domainCounts.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))]
            .forEach(([domain, count]) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = `hermes-domain-tab${activeDomain === domain ? ' active' : ''}`;
                button.textContent = `${domain || '全部'} ${count}`;
                button.addEventListener('click', () => {
                    activeDomain = domain;
                    renderTabs();
                    renderRows();
                });
                tabs.appendChild(button);
            });
    };
    const renderRows = () => {
        const query = search.value.trim().toLocaleLowerCase();
        const filtered = skills.filter(skill => {
            if (activeDomain && String(skill.domain || 'generic') !== activeDomain) return false;
            if (!query) return true;
            const haystack = [skill.id, skill.name, skill.description, skill.domain, ...(skill.tags || [])]
                .join(' ').toLocaleLowerCase();
            return haystack.includes(query);
        });
        visibleCount.textContent = `${filtered.length} 项`;
        visibleCount.hidden = filtered.length === skills.length;
        list.replaceChildren();
        if (!filtered.length) {
            const empty = document.createElement('div');
            empty.className = 'hermes-result-empty';
            empty.textContent = '没有符合条件的 Skill';
            list.appendChild(empty);
            return;
        }
        filtered.forEach(skill => {
            const row = document.createElement('div');
            row.className = 'hermes-skill-row';
            const name = document.createElement('div');
            name.className = 'hermes-skill-name';
            const strong = document.createElement('strong');
            strong.textContent = skill.name || skill.id || '未命名 Skill';
            const id = document.createElement('span');
            id.textContent = skill.id || '-';
            name.append(strong, id);
            const description = document.createElement('div');
            description.className = 'hermes-skill-description';
            description.textContent = skill.description || '暂无描述';
            const flags = document.createElement('div');
            flags.className = 'hermes-skill-flags';
            flags.appendChild(_hermesBadge(skill.domain || 'generic'));
            flags.appendChild(_hermesBadge(skill.risk || 'unknown', `risk-${skill.risk || ''}`));
            if (skill.requires_approval) flags.appendChild(_hermesBadge('需审批', 'warning'));
            if (skill.id) {
                const inspect = document.createElement('button');
                inspect.type = 'button';
                inspect.className = 'hermes-skill-action';
                inspect.textContent = '查看';
                inspect.addEventListener('click', () => hermesSend(
                    `请调用 agenticsre_skill_detail 查看 Skill ${skill.id} 的契约、风险和使用说明。`,
                ));
                flags.appendChild(inspect);
            }
            row.append(name, description, flags);
            list.appendChild(row);
        });
    };
    search.addEventListener('input', renderRows);
    renderTabs();
    renderRows();
    section.append(toolbar, tabs, list);
    return section;
}

function _hermesRenderSkillDetail(result) {
    const skill = result.skill && typeof result.skill === 'object' ? result.skill : {};
    const section = _hermesResultSection(skill.name || skill.id || 'Skill 详情', [
        skill.id || '',
        skill.domain || '',
        `风险 ${skill.risk || 'unknown'}`,
    ]);
    if (skill.description) {
        const description = document.createElement('div');
        description.className = 'hermes-skill-description';
        description.textContent = skill.description;
        section.appendChild(description);
    }
    const flags = document.createElement('div');
    flags.className = 'hermes-domain-tabs';
    (skill.stages || []).forEach(stage => flags.appendChild(_hermesBadge(stage)));
    if (skill.requires_approval) flags.appendChild(_hermesBadge('执行需审批', 'warning'));
    if (skill.safe_auto_execute) flags.appendChild(_hermesBadge('可安全自动执行', 'success'));
    section.appendChild(flags);
    if (result.instructions) {
        const details = document.createElement('details');
        details.className = 'hermes-tool-activity';
        const summary = document.createElement('summary');
        summary.textContent = '查看 Skill 指令';
        const pre = document.createElement('pre');
        pre.textContent = result.instructions;
        details.append(summary, pre);
        section.appendChild(details);
    }
    return section;
}

function _hermesRenderStageList(stages) {
    const list = document.createElement('div');
    list.className = 'hermes-stage-list';
    Object.entries(stages || {}).forEach(([stage, recordValue]) => {
        const record = recordValue && typeof recordValue === 'object' ? recordValue : {};
        const status = record.status || '-';
        const item = document.createElement('div');
        item.className = `hermes-stage-item ${_hermesStatusTone(status)}`.trim();
        const title = document.createElement('div');
        title.className = 'hermes-stage-title';
        const name = document.createElement('span');
        name.textContent = stage;
        const state = document.createElement('span');
        const confidence = _hermesConfidence(record.confidence);
        state.textContent = confidence ? `${status} · ${confidence}` : status;
        title.append(name, state);
        const summary = document.createElement('div');
        summary.className = 'hermes-stage-summary';
        const output = record.output && typeof record.output === 'object' ? record.output : {};
        summary.textContent = record.summary || output.summary || output.root_cause || (record.evidence || [])[0] || '阶段已完成';
        item.append(title, summary);
        list.appendChild(item);
    });
    return list;
}

function _hermesAppendDiagnosis(section, diagnosis) {
    if (!diagnosis || typeof diagnosis !== 'object') return;
    const rootCause = diagnosis.root_cause || diagnosis.summary;
    if (!rootCause && diagnosis.confidence == null) return;
    const grid = document.createElement('div');
    grid.className = 'hermes-diagnosis-grid';
    const cause = document.createElement('strong');
    cause.textContent = rootCause || '诊断结果';
    const confidence = document.createElement('span');
    confidence.textContent = _hermesConfidence(diagnosis.confidence) || '-';
    const component = document.createElement('span');
    component.textContent = diagnosis.root_cause_component || diagnosis.fault_type || '';
    const source = document.createElement('span');
    source.textContent = diagnosis.source ? `来源: ${diagnosis.source}` : '';
    grid.append(cause, confidence, component, source);
    section.appendChild(grid);
}

function _hermesRenderDirectSkillRun(result) {
    const skill = result.skill && typeof result.skill === 'object' ? result.skill : {};
    const section = _hermesResultSection(skill.name || skill.id || 'Skill 执行结果', [
        result.status || 'completed',
        result.dry_run === false ? '真实执行' : 'Dry-run',
        skill.id || '',
    ]);
    const stages = result.stages && typeof result.stages === 'object' ? result.stages : {};
    if (Object.keys(stages).length) section.appendChild(_hermesRenderStageList(stages));
    else {
        const empty = document.createElement('div');
        empty.className = 'hermes-result-empty';
        empty.textContent = '未返回阶段结果';
        section.appendChild(empty);
    }
    const diagnosis = Object.values(stages).map(record => record?.output).find(output => output?.root_cause);
    _hermesAppendDiagnosis(section, diagnosis);
    return section;
}

function _hermesRenderSkillWorkflow(result) {
    const section = _hermesResultSection('Skill 工作流结果', [
        result.status || 'completed',
        result.dry_run === false ? '真实执行' : 'Dry-run',
        result.primary_skill_id ? `主 Skill: ${result.primary_skill_id}` : '',
    ]);
    _hermesAppendDiagnosis(section, result.diagnosis || {});
    const matched = Array.isArray(result.matched_skills) ? result.matched_skills : [];
    if (matched.length) {
        const skills = document.createElement('div');
        skills.className = 'hermes-domain-tabs';
        matched.forEach(skill => skills.appendChild(_hermesBadge(skill.name || skill.id || 'Skill')));
        section.appendChild(skills);
    }
    const execution = result.execution_plan && typeof result.execution_plan === 'object' ? result.execution_plan : {};
    const verification = result.verification_plan && typeof result.verification_plan === 'object' ? result.verification_plan : {};
    const planStages = {};
    if (Object.keys(execution).length) {
        planStages.execution_plan = {
            status: execution.status || 'ready',
            summary: `${execution.command_count ?? (execution.commands || []).length ?? 0} 条执行命令`,
        };
    }
    if (Object.keys(verification).length) {
        planStages.verification_plan = {
            status: verification.status || 'ready',
            summary: `${(verification.checks || []).length} 项验证检查`,
        };
    }
    if (Object.keys(planStages).length) section.appendChild(_hermesRenderStageList(planStages));
    return section;
}


// ─────────────────────────────────────────
// Health Check
// ─────────────────────────────────────────

async function healthCheck() {
    const data = await api('/api/health');
    const dot = document.querySelector('#health-dot .dot');
    const text = document.querySelector('#health-dot span:last-child');
    const badge = document.getElementById('cluster-badge');
    const llmWarning = document.getElementById('llm-warning');

    if (data?.status === 'ok') {
        dot.className = 'dot dot-green'; text.textContent = '系统正常';
        badge.className = 'badge badge-success'; badge.textContent = '已连接';
        if (llmWarning) llmWarning.style.display = data.llm_configured ? 'none' : 'flex';
    } else {
        dot.className = 'dot dot-red'; text.textContent = '连接异常';
        badge.className = 'badge badge-danger'; badge.textContent = '连接异常';
    }
}
