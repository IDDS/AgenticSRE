# 多智能体证据传递与耗时修复

## 根因及修复范围

远端旧任务 `rca-077a9d21` 的阶段耗时合计约 803 秒。三轮调查约 403 秒，
额外复查约 197 秒。它的 structured_evidence 事件中已存在 `deployment_scaled_to_zero`
和 `service_endpoints_empty`，均指向本次测试对象；报告中“未读取 Deployment/Endpoints”
是证据传递丢失后的错误陈述，不能当成采集层事实。

重排、报告、关联、评审和复查的提示构造只传摘要，空摘要遮蔽了真实观测。现在统一
优先传递结构化事实，保留 source、namespace、对象和 raw_ref，再补充摘要。事件摘要
最多等待 20 秒；失败或空输出保留采集结果，并标明 summary_error。

多智能体使用独立的 DeadlineLLMClient：默认单次模型调用 90 秒、共享模型阶段时间
预算 480 秒，SDK 禁止自动重试。超时的返回留在私有 Future，不能覆盖后来的结果。
预算用尽后不启动额外模型调用/复查，并保留人工复核标志。该预算不包含数据库或
集群命令等全部外部耗时，因此仍需用 720 秒端到端测试核验。Agent+Skill 的模型配置
不受这个多智能体专用预算影响。

连续采集得到完全相同证据时停止重复重排，不标记根因已确认。复查中的证据 Agent
同样使用已有 evidence_agent_timeout_s。置信度和人工审核门禁保留。

## 验证

- 修复前在远端旧发布镜像运行三项证据丢失反例：3 failed。
- 新增回归覆盖空摘要、长摘要挤占上下文、摘要超时保留采集事实、拒绝模型迟到结果、
  总预算耗尽后不再启动调用。
- 最终远端 Python 3.12 回归：**514 passed，3 warnings，96.01 秒**，网络隔离、
  临时测试数据。源码修复提交 `4935b9f`。
- 生产两个服务已部署 `agenticsre:multi-20260915`，均 healthy；全部 10 个修复文件
  在两个运行容器内的 SHA-256 均与源码匹配，同机 agenticops ID/启动时间不变。
- 多智能体真实服务缩容验收 `rca-1c6f2261` 通过：当前 Deployment desired_replicas=0
  与 Endpoints ready_addresses=0 正确进入报告；调查由 3 轮降至 1 轮，没有额外复查。
  本次单次流程耗时 **458.0 秒**，旧案例约 **803 秒**（降低约 43%）。
- 多智能体真实镜像故障 `rca-329386f4` 通过：正确定位指定 Pod 的 ImagePullBackOff
  和不存在的镜像标签；1 轮调查，流程耗时 **422.2 秒**。两项均在原 720 秒窗口内完成。
- 测试仍使用真实集群和真实 DeepSeek 模型，没有把注入类型或候选根因放入请求。
  恢复由测试脚本显式执行，业务/rollout 检查通过，测试命名空间清理通过。
- 两次 critic 仍触发 45 秒超时，人工审核记录仍为 pending，分数上限 0.75；每次
  model_budget 均记录 1 次模型调用超时，总预算未耗尽。此版本修复了证据丢失和
  无休止的重复调查，不宣称所有模型调用均成功或无需人工复核。
- 网络中断后通过远端持久日志和结果 API 复核成功结果，未把 SSH 中断计为诊断通过。

完整成功/失败测试日志、运行轨迹、阶段耗时和部署哈希在
`outputs/multi-agent-fix-20260915/`，精简结果为 `live-summary.json`。

## 发布边界

仅更新 AgenticSRE 两个服务。保留现有配置数据文件、数据目录及模型凭据；将 configs
目录整体挂载改为逐项挂载配置文件/数据目录，使镜像内新 config_loader.py 能生效。
同机 agenticops、Redis、Cloudflare 隧道均保留，旧 release 可用于回退。

## 离线交付

远端生成 `agenticsre-offline-20260915-r2-linux-amd64.run`，源码修复版本 `4935b9f`。
SHA-256：`a40b9b49738de8a210e68f9653db1f510e687ac82a0a8fbdffc11906e7e7a07e`。
保存于远端 `/home/ubuntu/agenticsre/release/`，本地副本在 `release/`。

在独立 Docker/containerd 的 `--network none` 目标中通过实际 `.run` 安装、数据检查、
备份恢复和重启验收。修正验收脚本：重启后等待两个容器 healthy 再产生 PASS，
本次最终状态均为 `running/healthy`。Docker 29.1.3，Compose 5.5.0，overlay2，
无 systemd 模式；本轮未重新验证 systemd 模式。构建前复用了经过 SHA-256 核验的
旧离线包运行时和模型依赖，现场安装没有下载、构建或拉取镜像。

首次启动验收时目标基础镜像仍在传输，因找不到该镜像未启动测试；等待加载完成后
重新执行验收通过。原始构建日志和成功验收日志分别保留，不把未执行当成通过。
