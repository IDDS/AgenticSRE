# 多智能体版本远端实测（2026-09-24）

后续状态：本文记录当时默认混合版本的测试；其后已改为纯多智能体并部署，见[最终发布验证](pure-agent-release-20260924.md)。

## 结论

默认多智能体链路能够完成真实故障诊断，但本轮完整验收未通过：10 项门禁通过 9 项，最终 Judge 要求人工复核。不能据此认定版本完全正常，也不能作为纯多智能体的独立成绩。

## 环境与方法

- 远端：`snail`；代码基线 `b12869bd2587bf04564f1f77835832ee3761336c`。
- 与正式服务相同的镜像：`sha256:bdf556f750c3df0e07298a8a798f5822bac364c879c2b622ebab751002e6c939`。
- 独立候选容器通过实际 `/api/rca/run` 接口运行，任务 `rca-cb59943b`；未走专用 Skill 工作流入口。
- 保留协作优化和默认 Skill 桥接；模型为远端已配置的 `deepseek-v4-pro`。关闭候选实例自愈、记忆写入和演化。
- Kubernetes 独立命名空间 `agenticsre-propagation-d409fb3c` 创建 caller → middle → dependency 三服务。实际 HTTP 请求经历 200 → 503 → 200，各阶段均采集到 5 个 Jaeger spans；另有 12 次请求用于统计传播验证。
- 采用仓库 `tests/e2e/test_live_propagation.py`，验收适配器改为调用实际 HTTP API；未修改生产算法。

## 结果

| 项目 | 实测结果 |
|---|---|
| 多智能体任务 | completed，809.51 秒，调查两轮 |
| 配置、Trace、日志、事件智能体 | 两轮均返回成功 |
| 指标智能体 | 两轮均返回空摘要，未形成有效诊断结论 |
| 根因实体 | 正确定位 dependency Pod |
| 故障机制 | 识别 FAILED=true 导致 HTTP 503；明确请求发起方及原因尚未证实 |
| 证据 | 有真实 Trace、目标日志、状态变更、运行态观测 |
| 正常/恢复负例 | 均弃权，不生成根因候选 |
| 模型预算 | 未耗尽，剩余约 90.5 秒；记录 3 次调用超时 |
| 最终 Judge | 未通过，汇总分 0，要求人工复核，最终置信度降至 0.75 |
| 浏览器 | 运行过程及真实结果回放通过，无捕获到的页面脚本错误 |
| 清理与正式服务 | 测试命名空间已清理，候选容器已移除；正式 Web 和 Skill Runtime 均 healthy |

验收通过项：pipeline_completed、trace_observations、target_logs、component_identified、exact_root_entity、root_entity_observed、state_transition_observed、runtime_state_observed、budget_available。失败项：judge_accepted。

## 已发现的问题

1. 指标智能体连续空摘要，需要检查模型响应、摘要生成和目标指标范围，不能将其解释为指标正常。
2. 假设重排、规划、跨信号关联出现模型调用超时。系统降级完成，但约 13.5 分钟的单场景耗时需要优化。
3. Judge 的维度评分为 0.9/0.8/0.9/0.7，却将“FAILED 状态变更发起方及原因未确定”列为 unsupported_claims。`memory/rca_judge.py` 对任何此类阻断项清零，因此出现正面文字评价与总分 0 并存。诊断本身已承认该未知点，需进一步验证评审是否混淆“明确未知”与“无证据断言”；本轮不能绕过门禁算通过。
4. 最终时间线有将采集时间作为多条事件时间的现象，应以原始日志时间核对顺序，避免把历史恢复事件排到当前故障之后。

## 为什么多智能体调用 Skill

`configs/config_loader.py` 默认 `enable_agent_skill_bridge=True`；`orchestrator/rca_engine.py` 在跨信号关联后固定安排桥接阶段，通过 ToolRegistry 调用 Skill Runtime，再将结果加入诊断上下文。这不是本次测试临时添加的，也不是智能体自主选择的可选动作。

本轮主 Skill 为 `k8s.service_unavailable`，匹配 8 个 Skill。调用采用 dry-run；计划中出现自愈和验证阶段不代表真实执行。返回的部分工具记录明确为 `executed=false`、运行器未配置。通用根因 Skill 还给出了未被本轮观测证实的 Endpoint/就绪异常描述，因此 Skill 输出不能自动视作观测证据。

若需要纯多智能体基线，应关闭桥接另做对照实验；本轮仅代表当前默认组合版本。

## 证据与边界

本地证据包：`outputs/multi-agent-check-20260924/evidence.tar.gz`。

SHA-256：`f9b7f4ac19f59eb7c5ad2f30093fc05cbee8321f67cb30339df55dd73899ee40`（远端及本地一致）。包含 `live/root-acceptance-report.json`、`live/rca-result.json`、`live/api-state.json`、`live/live-report.json`、HTTP/Trace 观测及浏览器截图；不包含部署配置或 API 密钥。

这是远端真实执行的受控故障实验，不是自然生产事故准确率评测。未覆盖 Prometheus 告警入口、自愈真实写操作、知识库演化，也未证明所有 Skill 都有效。前端最终结果检查为对本轮实际结果的回放，没有在正式服务新建诊断任务。
