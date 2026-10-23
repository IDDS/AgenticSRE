# Job Skill 路由修复与远端验证

## 问题与改动

上一轮真实 Job 故障诊断中，RCA 主流程能够保留容器退出码 7，但 Skill bridge 选择了 `k8s.service_unavailable`，产生了没有目标依据的 endpoints/Deployment 诊断和动作计划。

远端复现确认：`build_skill_payload("请调查 demo 中 job/probe 的告警事件：JobFailed", "demo")` 返回 `fault_type=service_unavailable`。原因是通用分类函数对未识别的 Kubernetes 查询默认使用该类型，而当时没有 Job 专用 Skill。

应用修复提交为 `10fe597`。为降低对既有行为的影响，没有修改全局分类默认值；只在明确的 Job 目标、无显式故障类型、无 Service/Deployment 目标、无具体服务故障信号时，将 bridge 的通用 fallback 改为 `job_failure_investigation`。该值仅表示调查方向，不代表已证实根因。镜像、OOM、CrashLoop 等具体类型保持原路由；多个 Job 目标及不合法名称不自动选取。

新增 `k8s.job_failed`，仅声明 detect、diagnose、verify 三个阶段。事件智能体从 Pod 的 controller ownerReference 保留 Job 名称和 UID。诊断只接受 event_agent 当前结构化容器终止观测，要求命名空间、Job 名称、UID、Pod 和容器身份齐全，退出码为非零整数；不同命名空间、无关 Job、成功退出、归属缺失及只有历史假设的输入返回 unknown。这里的 UID 是 Pod 所属控制器引用，不代表已另行查询 Job 当前对象或终止条件。

该 Skill 不生成写操作，不宣称执行过恢复核查。最终输出仅证明 Job 所属容器异常退出；Job 条件、更上游配置或业务原因仍需补充 Job 状态、命令及日志。verify 提供作用域明确的只读核查计划，`recovery_verified=false`。

## 回归门槛与结果

所有执行测试均在远端 `snail` 完成，本地仅编辑和整理证据。

- 修复前，新增测试在旧镜像得到 4 failed、8 passed，确认测试能够捕获原问题及缺失的 Job 能力。
- 定向验证 15 passed，覆盖原有事件采集以及初步路由隔离。
- 首轮全套 616 passed、1 failed；失败来自新 Skill 缺少标准文档契约章节。按既有检查要求补全，没有跳过或放宽该检查。
- 补充证据隔离测试后，最终全套 **623 passed，104.58 秒**。旧测试中只有注册表数量随新增 Skill 从 61 更新为 62，并新增阶段契约断言。
- 对 Service、ImagePullBackOff、OOM、CrashLoop、Prometheus scrape 五类既有场景，在修复前后镜像运行相同 workflow，比较主 Skill、匹配集合、诊断分类、动作计划、验证计划，JSON 完全一致。

测试文件包括 `tests/test_job_skill_routing.py`；真实闭环脚本 `tests/e2e/test_live_alert_lifecycle.py` 已增加硬性验收：必须实际调用 `k8s.job_failed`，该 Skill 自身也必须输出与退出码 7 一致的 job_failed 诊断。

## 隔离实测与部署

候选镜像为 `agenticsre:job-skill-20260916`，候选应用使用 18086、Skill runtime 使用 18096，仅绑定远端回环地址，并使用独立数据和日志目录。线上 8080/8090 在验收期间继续运行旧版。只复制候选运行所需配置，不输出凭据。

实际测试在独立的 `agenticsre-diag-e2e-lifecycle-` 命名空间创建 Job 和基于真实 kube_job_failed 指标的 PrometheusRule，验证退出 7、告警形成、压缩组启动诊断、退出 0 后解除、再次退出 7 时沿用事故 ID 并递增 episode、最终解除及清理。恢复动作由测试脚本执行，不计为系统自主修复。

本轮实际命名空间为 `agenticsre-diag-e2e-lifecycle-b8ba93`，运行 `rca-82f84f15`。脚本退出码 0，最终 `status=passed`，诊断、生命周期和清理三项验收均为 true。事故 `incident-83d9a85221274f2a91e67d90` 在连续扫描中保持身份，完成 `active(1) → resolved(1) → active(2) → resolved(2)`。

独立只读探针与完整 RCA 均选中 `k8s.job_failed`，该 Skill 输出与目标 Job 控制器 UID 匹配的实际退出码 7，校准后置信度 0.555。组合 workflow 的 4 条动作均为只读检查，没有 Deployment 扩容、删除或 Job 重跑。最终 RCA 类型为 application_crash，置信度 0.696，明确上游业务原因缺少命令、日志和依赖证据，未将此场景计为更深层根因完全查明。

本轮端到端诊断耗时 **595.17 秒**，其中 Skill bridge 阶段 **0.441 秒**。上一轮分别为 495.76 秒和 2.117 秒；本轮候选使用独立历史数据，且模型响应时间不同，不能据此宣称整体性能改善或完成严格的时延回归判定。阶段记录显示主要耗时在调查 181.86 秒、协作复核 146.82 秒及假设生成 81.21 秒。

待部署镜像与通过实测的候选实例已核对关键代码 SHA-256 相同。发布脚本在任一服务健康检查失败时回滚到 `agenticsre:lifecycle-20260916`；原版本镜像和配置保留。

线上已切换到 `10fe597`，应用和 Skill runtime 的镜像标签版本一致，Docker 健康检查均为 healthy，8080/8090 的 health API 均返回 ok。新 Skill 可由线上 API 查询。切换前后，上一轮命名空间中的 3 条生命周期记录 JSON 完全一致，事故 `incident-ae1cfefe45b7c529ef4093fb` 保持 episode=2、resolved；没有因部署丢失或改写历史状态。

原始回归、对比结果及实际运行记录存放于 `outputs/job-skill-20260916/`。本轮未修改 Trace 接入或全局诊断时限；单次耗时变化不能直接作为性能提升结论。
