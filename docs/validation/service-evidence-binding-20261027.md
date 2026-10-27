# Service 定向取证与根因身份绑定验收

本轮针对前一轮真实站点故障暴露的两个缺口：没有 Pod 时未发现实际缩零的 Deployment，以及 Agent+Skill 未导出结构化根因身份。实现已补齐，以下分别记录代码契约、受控回归和真实测试床结果。身份定位成功不代表独立复核或自主处置已经通过。

## 取证与推理契约

通用采集器先读取目标 Service 的真实 selector 和 Endpoints，再用该 selector 查询 Pod，并在当前 namespace 的有限 Deployment 模板清单中查找匹配候选。Service 与 Deployment 可以不同名，不使用 `app=<service>` 或名称惯例推断后端。唯一模板候选还需单独读取命名 Deployment 的 UID、resourceVersion 和副本状态。多个候选保持歧义；查询失败、超限、对象重建、采集中 Service 变化均保留缺口。

有 Pod 时另外读取真实 controller ownerReference 指向的 ReplicaSet，核对 UID 及其 Deployment 引用。没有 Pod 时，模板匹配只表达预期标签关系；它不是已观测的运行期所有权，也不是独立的因果证明。多个控制器、其它工作负载类型或权限缺口不强行归并为单一 Deployment。

固定 `jsonpath-as-json` 投影只请求身份、标签、必要的控制器和状态字段，不请求容器环境变量或任意 annotations。每个字段单独编码，缺失字段保留空数组；缺失副本状态不补成零。最后必须收到固定结束标记，完整行边界的输出截断也会被拒绝。原始完整输出经验证、白名单投影后才进入有界 trace，超过模型样本预算时生成有效 JSON 摘要。大健康 Endpoints 摘要保留一个真实就绪地址，避免旧消费者把丢失 `subsets` 当成空后端。

每个通用采集阶段最多执行 12 次只读工具调用，并受已有阶段期限约束。结构采集后保留模型显式选择的合法检查；没有显式检查时，按已观测 Pod 名读取近期日志，按工作负载 UID 读取事件。selectorless/ExternalName 不触发默认 namespace 扫描。首次 Service 读取不可用时允许既有只读检查继续，但范围保持未解决，不能据此绑定根因对象。一次完整诊断可以包含多个 Skill 和复读，不能把单阶段预算当成全诊断调用总数。

模型只能从已成功执行且已提交的命名对象目录中显式选择 `root_entity`。宿主核对 cluster、namespace、kind、name、UID 和 resourceVersion；不从结论文字补全身份，不把同名新 UID 当旧对象，也不对不透明版本号排序。调用者的 `target_cluster_id` 在进入 Skill 前规范为 `cluster_id`，命令记录、专项判据与最终目录使用同一范围。

`identity_bound` 仅表示身份与观测一致。它不修改独立审核状态、自动化门禁、权限或审批；后续写操作仍须核对实时目标及恢复条件。

## 验证记录

全部应用执行、测试、构建和浏览器检查均在远端进行；本机仅编辑、归档核对和 Git 发布。冻结快照 `service-evidence-20261027-r4` 包含 876 个文件，在禁网隔离容器中通过 **310 项专项、2292 项完整回归**，保留 40 项现有弃用警告。真实镜像未挂载应用源码，全部 425 个应用 COPY 源文件与该快照逐字节一致。之后仅更新文档和选择性证据，生产服务未替换。详见[回归收据](evidence/service-evidence-20261027/packaged-verification.json)、[完整日志](evidence/service-evidence-20261027/full.txt)、[镜像核对](evidence/service-evidence-20261027/copy-bytes-verification.json)和[源码标识](evidence/service-evidence-20261027/source-version-receipt.json)。

新增 38 项回归覆盖不同名后端、零 Pod、多模板、无 selector、ExternalName、范围变化、权限失败、非法及行边界截断、超限、健康大 Endpoints、UID/RV 冲突、未知执行结果、根因伪造，以及两种 cluster 字段的纯 HTTP 502 完整 Agent+Skill 链路。7 项初始反例先在旧版失败；审查另外修复健康摘要误判、判别检查被挤掉和 cluster 别名未贯通等边界。模拟回归不计为真实故障注入。

## 真实站点故障结果

沿用既有非生产 Train Ticket Java `ts-station-service`，通过 `ts-ui-dashboard` 的 `GET /api/v1/stationservice/stations` 判断业务成功；需 HTTP 200、整数 `status=1` 和非空 `data`。同入口的路由查询为独立对照。注入前核对单副本健康状态、Service→Pod→ReplicaSet→Deployment 归属、无 HPA 和并发 Chaos；注入和恢复使用 UID、spec 指纹及 resourceVersion 条件，并先启动独立看门狗。诊断输入只含对象范围和业务观测，没有提供注入类型、Deployment 副本答案或预设根因。

| 阶段 | 实际结果 |
|---|---|
| 健康基线 | 20/20 业务成功，对照全部成功，P95 61.554 ms |
| 真实故障 | 实际 Deployment 缩为 0，0/5 业务成功，对照全部成功 |
| Agent+Skill | 26.707 秒完成；读到空 Endpoints、0 个匹配 Pod、唯一匹配模板与命名 Deployment 副本为 0 |
| 根因身份 | 显式选择 Deployment，UID 与实际注入对象一致，resourceVersion 存在；`identity_bound`，校准置信度 0.72 |
| 多智能体 | 165.364 秒观察预算内仍运行；后续快照报告同一 Deployment 缩零，仍为 `requires_review`、置信度 0.65 |
| 处置门禁 | 两条链路均未执行原生自愈；Agent+Skill 阻断原因仍含独立复核及诊断门禁 |
| 独立恢复 | 实验控制器恢复原副本；冷启动观测保留 16 次失败，第 17 次首个有效业务响应，观测耗时 23.743 秒 |
| 连续恢复 | 预先声明的三个 10 笔窗口全部成功，对照正常；P95 分别 55.002、57.699、50.925 ms |
| 最终状态 | 原 Deployment UID、非副本 spec 保持，1/1 Ready，无遗留 fence；原生自愈记录 0、隔离记忆规则/故障记录均 0 |

上述时长是具体调用的观测，不是总体准确率或效率评估。没有把多智能体后续完成改写成预算内完成，也没有把 Java 冷启动失败隐藏或算成成功。实验撤销故障不计为系统自愈。Agent+Skill 的这一例补齐了前轮根因身份缺失，但它仍未完成独立因果复核。

两条保存的诊断记录均经远端 Chromium 打开过程页、策略视图和 HTML 导出，0 页面错误、0 写请求。页面可读和诊断正确分别判定，见[浏览器收据](evidence/service-evidence-20261027/browser.json)。完整选择性证据索引见[本轮收据目录](evidence/service-evidence-20261027/README.md)，公开摘要不含对象 UID、私有配置和工具原文。

## 已知边界

本轮工作集中在 Agent+Skill 的 Service 证据链与身份导出，没有实现独立因果复核器，也没有放宽原生自愈条件。单次缩零实验不能证明所有故障场景的准确率、调查流程自优化收益或 MTTR 改善。其它控制器类型、跨 namespace 依赖及多控制器服务需要更多有范围的取证能力和独立样本评估。
