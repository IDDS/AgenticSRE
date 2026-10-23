# Skill 覆盖矩阵

本文档用于实事求是地说明 AgenticSRE Skill 当前覆盖能力。结论是：框架已具备
标准 Skill 契约、Agent 调用、受控工具上下文和核心通算/智算闭环；但故障场景
尚未全量专用化，部分场景仍依赖通用 Skill、观测 Skill 或 legacy recipe bridge。

## 当前总体状态

- Skill 数量：2026-09-18 源码清点为 62 个；注册数量不代表真实故障验收数量。
- 格式：每个 Skill 必须包含 `SKILL.md`、`skill.yaml`、`skill.py`。
- 阶段：全局支持 `detect / diagnose / heal / verify`；每个 Skill 按职责声明其中一个或多个阶段。
- 工具调用：Agent+Skill 路径支持 `context["tools"].execute(...)`。
- 已真实打通：只读 `kubectl` 工具调用，可记录在 `trace[].tool_calls`。
- 工具可用性取决于调用路径及部署环境：Agent 侧可委托已注册工具，独立 Runtime
  需要注入 runner。不能根据 Skill 声明判定实际工具已接入；以执行轨迹的
  `executed`、`success` 和原始观测为准。

## 常用能力实际验证状态（2026-09-18）

下表整理既有证据，不是本日重新执行全部故障实验。历史场景的通过不自动覆盖
新版本、其他集群、其他故障机制或自动修复。

| Skill / 能力 | 已有证据 | 未覆盖部分 / 下一步 |
| --- | --- | --- |
| `k8s.service_unavailable` | 9 月 15 日真实缩容取证通过；人工脚本恢复 | 最新 503 证据约束仍在未发布候选中；不能把 503 等同于空后端 |
| `k8s.image_pull_error` | 9 月 15 日真实错误镜像标签诊断通过 | 恢复由脚本完成，不算自主自愈 |
| `k8s.job_failed` | 9 月 16 日真实 Job 退出 7、告警、诊断、解除及复发通过 | 只证明容器异常退出，业务根因和自动恢复未确认 |
| `k8s.pod_crashloop` | 历史真实场景有诊断和人工执行恢复记录 | 当前版本需重新完成专项验收 |
| `llm.k8s_vllm_observability` | 历史真实 T4/vLLM 的服务和观测链路实验 | 当前版本与目标 GPU 环境需重新验收；不外推硬件故障覆盖 |
| `tool.llm_host_readiness` | 历史只读 GPU、进程和端点检查 | 不证明主机修复能力 |
| `tool.prometheus_query` | 已有执行结果成功/失败/未接入的契约回归 | 需逐部署路径验证实际 runner、查询及观测来源 |
| `tool.browser_frontend_validation` | 标准包及可选 runner 调用接口 | 未接入 runner 时仅生成计划；Codex 浏览器检查不算产品 Skill 执行 |
| `tool.internet_search_research` | 标准包及可选检索调用接口 | 未接入 runner 时仅生成计划；需审计来源和检索时间 |
| GPU 硬件、切片、分布式通信等 | 存在专用 Skill 和部分契约验证 | 仍缺逐场景、当前版本真实故障验收，不能统称全部有效 |

依据：[9 月系统验收](../docs/validation/system-review-release-20260915.md)、
[Job 闭环](../docs/validation/job-skill-routing-20260916.md)、
[历史实际故障记录](REAL_FAULT_TESTS.md)、
[未发布候选验收](../docs/validation/alert-root-candidates-20260918.md)。

后续每项验收应记录 Skill 内容哈希、镜像、环境、工具调用原文、独立目标标签、
正反例、诊断结果和恢复执行者。没有独立结果记录的能力保留“未验收”。

## 通算集群

专用覆盖较完整：

- Service 无可用后端：`k8s.service_unavailable`
- Ingress/Gateway/LB 入口路由异常：`k8s.ingress_gateway_traffic`
- TLS 证书过期、SNI/握手失败：`k8s.tls_certificate_expiry`
- Deployment 异常：`k8s.deployment_unhealthy`
- Pod CrashLoop：`k8s.pod_crashloop`
- OOMKilled：`k8s.oom_killed`
- 镜像拉取失败：`k8s.image_pull_error`
- 调度失败：`k8s.scheduling_failed`
- 节点 NotReady：`k8s.node_not_ready`
- 资源压力：`k8s.resource_pressure`
- DNS 故障：`k8s.dns_resolution`
- NetworkPolicy/网络阻断：`k8s.network_policy_deny`
- PVC/Volume 挂载失败：`k8s.volume_mount_failed`
- 存储性能退化、PVC 容量/磁盘 I/O/iowait：`k8s.storage_performance`
- HPA 扩缩容失败：`k8s.hpa_scaling_failed`
- ResourceQuota：`k8s.resource_quota_exceeded`
- 控制面异常：`k8s.control_plane_unhealthy`
- 配置缺失：`k8s.config_missing`

仍需增强：

- DNS 上游异常、多租户限流、服务网格熔断、Sidecar 异常仍缺少专用 Skill。
- MySQL/PostgreSQL 目前仍是连接池工具型覆盖，缺少数据库专用闭环 Skill。
- Redis/Kafka 已有只读健康诊断 Skill，但写操作、自愈和 offset/主从切换仍必须人工审批。

## 智算集群

已有专用或半专用覆盖：

- vLLM 进程崩溃：`llm.vllm_process_crash`
- GPU/显存/KV Cache 压力：`llm.gpu_resource_pressure`
- vLLM 队列、连续批处理和 Prefill/Decode 调度退化：`llm.vllm_scheduler_queue`
- vLLM 负载生成器、背景流量和实验流量有效性：`llm.vllm_load_generator`
- 多卡 TP/PP/world size/rank/model shard 拓扑一致性：`llm.parallel_topology_consistency`
- GPU device plugin、`nvidia.com/gpu` allocatable 和节点注册异常：`llm.gpu_device_plugin_health`
- 多副本/多模型流量倾斜、热点副本、endpoint 不均衡：`llm.replica_traffic_imbalance`
- 模型加载、模型权限、Tokenizer 和权重损坏：`llm.model_asset_failure`
- 模型与硬件兼容性、dtype、量化、CUDA arch、attention backend：`llm.model_hardware_compatibility`
- vLLM/OpenAI 兼容接口、模型名和请求参数：`llm.model_serving_endpoint`
- CUDA runtime/context 和 kernel launch 异常：`llm.cuda_driver_failure`
- GPU ECC、Xid、retired pages、温度/功耗降频、NVLink/PCIe 链路健康：`llm.gpu_hardware_health`
- MIG/vGPU/MPS/time-slicing/GPU sharing 切片和隔离异常：`llm.gpu_partition_isolation`
- 模型仓库、对象存储、HF token、模型缓存、checksum、分片下载异常：`llm.model_repository_access`
- 拓扑感知调度、NUMA/NVLink/RDMA locality、跨节点放置异常：`llm.topology_aware_scheduling`
- CUPTI/Profiler 开销：`llm.cuda_driver_failure`
- NCCL/RDMA/Tensor Parallel/多卡通信异常：`llm.distributed_communication`
- 业务 SLO、TTFT/TPOT/E2E 退化：`llm.business_slo`
- K8S vLLM 可观测与服务不可用：`llm.k8s_vllm_observability`
- DCGM exporter、GPU 指标缺失和 Prometheus target down：`llm.dcgm_exporter_observability`
- TELLER 请求级 Trace 诊断：`llm.teller_request_trace_diagnosis`
- TELLER 持续采集、request_id 缺失、NVTX/CUDA 覆盖质量：`llm.teller_trace_collection`
- AISHPerf/AI Infra 场景诊断：`llm.aishperf_aiinfra_diagnosis`
- 裸机 LLM 主机只读巡检：`tool.llm_host_readiness`

仍需增强：

- TELLER 已有采集质量 Skill，但真实 runner 仍需接入 collector 状态、trace 文件和 CUPTI 事件源。
- GPU ECC、Xid、温度/功耗 throttle 已拆到硬件健康 Skill；真实 GPU reset、节点隔离和硬件维修仍必须审批。
- MIG/vGPU、GPU 隔离、MPS/MIG 切片已专用化；后续仍需补厂商插件差异和配额策略细节。
- 多节点推理的拓扑感知调度已专用化；RDMA/NCCL 细分根因仍可继续深化。
- 模型仓库/对象存储/HF token/模型缓存链路已专用化；后续要补不同对象存储和缓存系统的 runner。
- 真正端到端闭环仍依赖 runner：Prometheus、TELLER、kubectl、loadgen、host GPU 工具必须在部署环境接入并审计。

## 智算覆盖结论

当前 Skill 对智算故障是“层级覆盖基本完整，细分场景仍需持续专用化”：

- 已覆盖推理服务层：进程崩溃、服务入口、模型 API、业务 SLO、队列/调度、负载生成。
- 已覆盖 GPU/运行时层：显存/KV Cache、CUDA runtime/context、GPU ECC/Xid/温度/功耗、PCIe/NVLink/CUPTI、DCGM 指标、device plugin、MIG/vGPU/MPS 隔离。
- 已覆盖多卡/分布式层：TP/PP 拓扑、NCCL/RDMA/TP 通信、多副本流量倾斜、拓扑感知调度。
- 已覆盖模型层：模型资产、Tokenizer、权重、权限、模型硬件兼容性、模型仓库/对象存储/缓存。
- 已覆盖观测层：Prometheus/DCGM、TELLER 请求级 Trace、TELLER 持续采集质量、AISHPerf 外部场景映射。
- 未承诺逐个 AISHPerf/厂商场景一一专用化；未命中的新场景会先落到 AISHPerf/通用观测/知识检索 Skill，再沉淀成专用 Skill。

## 浏览器与演示验证

已新增 `tool.browser_frontend_validation`，用于前端页面、弹窗、按钮、截图和演示
路径验证。该 Skill 是标准包格式，但当前产品运行时尚未接入 browser/playwright
runner；因此默认只生成验证计划。Codex 环境可用浏览器 Skill 做人工/自动前端
测试，产品内要真实执行浏览器点击，需要部署 Playwright/browser runner 并通过
`context["tools"]` 注入。

## 互联网检索与外部知识

已新增 `tool.internet_search_research`，用于官方文档、开源 issue、论文、社区
Skill、AISHPerf/AI Infra 故障案例和已知问题检索。自然语言中出现“互联网搜索、
官方文档、GitHub issue、论文、开源社区”等信号时会路由到 `tool` 域，并优先
命中该 Skill。该 Skill 支持通过 `context["tools"].execute("web_search", ...)`
调用受控搜索 runner；当前产品运行时尚未内置互联网搜索 runner，因此默认只生成
检索计划。Codex 执行环境可用 Web 工具做临时研究，但产品内要形成可审计闭环，
需要部署专门的 `web_search` runner，并记录 URL、标题、摘要、检索时间和可信度。

## Prometheus 与指标查询

已新增 `tool.prometheus_query`，作为 Agent+Skill 假设验证的只读指标查询工具；
已新增 `k8s.prometheus_scrape_target`，处理 ServiceMonitor、PodMonitor、metrics
endpoint、`metricsBindAddress` 等抓取配置问题。普通 TargetDown 仍保留给
`k8s.observability_signal` 或具体业务 Skill，避免把所有服务不可用都误判成采集问题。

## 后续优先级

1. 优先补智算真实 runner：Prometheus、TELLER、loadgen、host SSH/nvidia-smi。
2. 再补智算真实实验：GPU 硬件健康、MIG/vGPU/MPS、对象存储/模型仓库、拓扑感知调度需要在 127/128 集群形成可复现实验记录。
3. 通算中间件和入口链路作为平台依赖继续保留，但不作为智算演示主线。
4. 每新增一个 Skill 必须有契约测试、命中测试、Agent+Skill 调用测试和至少一个真实或可复现实验记录。
