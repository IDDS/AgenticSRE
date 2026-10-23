# Agent 与 Skill 诊断效果复验（2026-10-13）

## 结论

两条链路均能正确定位本轮镜像拉取故障，但只有纯多智能体通过该场景的本轮
门禁。Agent＋Skill 的 LLM 假设生成失败后回退到确定性候选，虽然最终定位
正确且实际调用了 Skill，仍不满足完整假设驱动流程。跨服务传播场景均未通过
完整验收。纯多智能体的根因方向正确，诊断闭环改写后丢失结构化根因实体并触发
人工复核；Agent＋Skill 两次传播实验均在模型输出解析或选择环节失败，
没有实际调用 Skill。独立 Skill 具备症状分类能力，但其模板结论和固定分数
不能单独作为具体根因已证实的依据。

| 场景 | 纯多智能体 | Agent＋Skill |
|---|---|---|
| 镜像拉取失败 | 69.58 秒，定位到镜像不存在，本轮检查通过 | 114.084 秒，定位正确、实际调用镜像 Skill；LLM 假设失败，完整流程未通过 |
| HTTP 503 三服务传播（最终轮） | 169.16 秒，根因方向正确；12/16 门禁通过，进入人工复核 | 179.771 秒，失败；真实调查执行，但没有有效 Skill 调用 |

本轮优先整改项是闭环改写保留已验证的根因实体、Agent＋Skill 的有效模型
输出与格式门禁，以及检测未匹配时禁止将模板 diagnose 输出当作有效结论。
本次只做验证和记录，没有修改或部署生产算法。

## 范围与方法

在远端 `snail` 使用正式发布镜像
`sha256:807569a268af4f74f7b8502ad687b97bba3eb988ae0751ee3814b67612aaf201`
启动两个独立 Web 实例，经实际 `POST /api/rca/run` 分别执行纯多智能体和
Agent＋Skill。模型沿用正式配置的 `deepseek-v4-pro`。两实例数据与日志目录
独立，关闭自动修复、记忆学习和演化；不向诊断输入添加注入类型或场景真值。
历史记忆为空，结果不代表启用生产知识库后的表现。

实验包含 caller → middle → dependency 的 HTTP 503 传播，以及独立
Kubernetes Pod 的镜像拉取失败。每个场景的两条链路面对同一现场，输入适配
和实际可调用工具不同，因此不是严格的算法消融或统计性能基准。

另在远端无网络容器中，将已采集的真实故障与恢复观测回放给单个 Skill，
用于区分 Skill 本身、LLM 路由和最终推理能力。这部分没有实时工具调查，
不与完整诊断耗时直接比较。

## 镜像故障实测

命名空间 `agenticsre-diagnosis-audit-e47ed5af` 中的 `worker-a533a2` 使用
不存在的镜像 tag。Kubernetes 实际返回 `ErrImagePull` / `ImagePullBackOff`
及镜像不存在的错误，而不是仅由测试脚本给出故障标签。

| 链路 | 实际结论 | 耗时 | 检查结果 |
|---|---|---:|---|
| 纯多智能体 | 定位目标 Pod、镜像引用及仓库 `not found`，判断容器无法启动 | 69.58 秒 | 本轮 7 项检查通过；Judge 0.90，无人工复核，零 Skill 调用 |
| Agent＋Skill | 定位同一镜像问题，并将 Service/Endpoints 不存在区分为伴生观测 | 114.084 秒 | 实际调用 `k8s.image_pull_error`；严格复核为 5/6，LLM 假设门禁失败 |

上述检查覆盖任务完成、目标名称、镜像故障描述、真实调查和链路边界。
它们不能替代全部诊断质量评价。原脚本将“出现 llm_hypotheses 事件”记为通过，
但原始事件实际为 success=false、items=[]，并产生 llm_hypothesis_fallback。
复核门禁要求 success=true 且 items 非空，因此不得沿用原脚本的 6/6 结论。
独立复核保存在 `outputs/diagnosis-comparison-20261013/evaluation-summary.json`；
原始现场报告保留原样以供追溯。

Agent＋Skill 最终置信指数为 0.69，纯多智能体为 0.82。这些值来自各自的
工程评分流程，不是独立标注样本上的准确率，也不适合直接比较为概率。

实际调用的 `k8s.image_pull_error` 在 detect 阶段返回 matched、confidence
0.86；diagnose 返回常见原因列表，confidence 0.88，组件字段为
`<deployment>`。两个阶段的 tool_calls 均为空。最终“该 tag 不存在”的具体
结论来自 Agent 对只读调查的裁决，不能全部归功于 Skill 内部独立取证。

## 传播场景与失败记录

首轮模型预处理因辅助进程漏读配置中心覆盖项而使用旧凭据，返回 401。
它未完成任一诊断链路；该失败属于验收脚本的配置问题。随后修正读取方式，
保留原始证据并重跑，正式模型配置未修改。

第二轮 `agenticsre-propagation-cce1f3a7` 的真实 HTTP、Trace 采集和数值
观测通过，但候选诊断实例未取得同 namespace 原始 Span。纯多智能体
仍根据实际日志、声明源码和指标定位到 dependency 的 `FAILED=true` 状态变化，
耗时 155.42 秒，Judge 0.90，无人工复核；16 项检查中 15 项通过，Trace 项失败。
该轮不得记为完整传播验收通过。最终轮为候选实例显式绑定实验的 Jaeger
查询地址，以排除这项取证配置差异。

该轮 Agent＋Skill 在 178.569 秒后失败，没有实际调用 Skill。模型返回了一个
单独的假设对象，但适配器未将其识别为有效假设；事件
`llm_hypotheses.success=false`，items 为空。随后 Skill 选择首先返回空内容，
重试返回未闭合 JSON，原始文本长 270 字符；最终推理也返回未闭合 JSON。
轨迹中的 `llm_skill_selection_result.success=true` 表示调用返回，不表示其
内容可解析或 Skill 已执行。不能把该轮解释为专项 Skill 已诊断失败或已成功。

最终轮 `agenticsre-propagation-65180b3e` 已取得同 namespace 原始 Span。
纯多智能体耗时 169.16 秒，文字结论正确指向 dependency 的 FAILED 状态，
但未通过完整质量门禁：Judge 为 0，needs_review=true，进入人工复核。
最终置信指数被限制为 0.65。

轨迹能够确认一个字段保留问题：闭环改写前的多个 result 事件均包含完整
`root_entity={namespace, kind: Pod, name: ...-dependency}`；
`diagnosis_closure` 后的 result 不再包含该字段，最终
root_entity_validation.observed=false。质量评审还将外部操作来源及传播
时序列为未证实项。现场取证成功不等于结论字段与质量门禁都通过。

最终轮 Agent＋Skill 耗时 179.771 秒后失败。假设生成、Skill 选择、选择重试
和最终推理的事件均记录 `parse_error=true`、raw_response 为空。真实只读
调查已执行，但没有有效模型假设、Skill 调用或最终根因。因此两次传播实验
均不能算 Agent＋Skill 诊断通过，也不能据此断言某个具体 Skill 的调查算法
在现场失败——两轮均未进入实际 Skill 执行。

## 单个 Skill 的正负观测回放

| Skill 与真实观测 | detect | diagnose 输出 | 判断 |
|---|---|---|---|
| `k8s.service_unavailable`，依赖返回 503 | matched | 服务不可用、根因待验证，要求补充证据；confidence 0.35 | 能识别症状，未独立确认应用状态变更机制 |
| 同一 Skill，恢复后返回 200 | skipped | 仍生成同一服务不可用描述，status=ok | 消费方必须检查 detect；diagnose.ok 本身不能作为故障成立条件 |
| `k8s.image_pull_error`，真实拉取失败状态 | matched | tag、仓库连通性、凭据等常见原因列表；confidence 0.88 | 识别故障类别，未独立区分具体原因 |
| 同一 Skill，健康 HTTP 200 观测 | skipped | 仍生成镜像拉取失败描述，status=ok、confidence 0.88 | 显式调用不匹配 Skill 仍会产生模板结论，需联用检测与真实证据门禁 |

健康回放是显式指定 Skill 的组件级负例，不代表前端正常路由必然产生同样
误报。它验证的是不能只凭单个 diagnose 的成功状态或固定分数接受结论。

## 证据与边界

脚本及取回的现场证据集中于 `outputs/diagnosis-comparison-20261013/`。
归档仅包含允许的脚本、运行结果和真实观测，不包含生产配置、候选数据目录
或凭据。前两轮失败与最终结果一并保留，不用后续通过覆盖早期失败。

远端实验调度状态为 finished；镜像实验退出码 0，最终传播实验因质量门禁
未通过而退出码 1。这是保留失败结果的预期行为，不把脚本结束等同验收通过。

最终核验：实验归属的容器和命名空间均无残留。正式 Web 与 Skill Runtime
均 healthy，镜像及 9 月 24 日启动时间保持不变；知识规则 114 条、故障记忆
82 条，与本轮开始时一致。没有执行生产故障注入、自愈写操作或发布。

证据包：`outputs/diagnosis-comparison-20261013/evidence.tar.gz`，共 129 个
验证文件；本地逐文件哈希核验通过。归档 SHA-256：
`35205fce22812f0cff45ac1967ccdfd9cbd95c47daedba18e9f3f5b5e3121b5b`。

本次不覆盖自然生产事故准确率、Prometheus 告警入口、真实自愈效果、智算
故障或知识库演化。少量场景不能得出总体准确率或普遍速度优势。
