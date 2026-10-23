# 候选知识隔离与人工版本审核

2026-10-22，接续[范围与时间准入](memory-admission.md)的第三个切片。目标是阻止提取结果自动成为可复用经验，让人工操作绑定实际内容版本。本轮不是独立评测晋升平台，也不证明记忆带来诊断收益。代码尚未提交或部署生产。

## 从提取到复用

自动和监督提取均在写入第一条规则前校验完整数组：至多3条，条件与结论非空，置信度为0至1的有限数值，相关文本类型正确。置信度0按0保存和计分，不再被默认值替换。存储层强制新auto_learn/supervised规则为candidate，即使传入active也不能直接启用；手工新增API同样先生成candidate。

自动存档的事故保留为候选事例，不立即回流；本轮没有提供事例晋升入口。原始专家反馈继续供管理查看和带来源说明的统计，不再直接注入后续诊断；审核派生规则后才复用这部分经验，避免绕过候选门禁。

已存在的auto_learn/supervised规则若无当前内容的启用审核，在消费时按candidate隔离，包括原始状态仍为active的旧记录。原文件不批量重写，API分别返回stored_status和实际status。旧的手工/内部导入保留兼容，不因此视为已经独立验证；不能将底层add_rule的全部可信导入都概括成候选。

存储在规则锁内返回写入收据，区分created、存储/实际状态及revision。重复提取返回同一ID，保留已有编辑、驳回、停用或启用状态。rules_added/rules_generated只计本次实际新增，rule_results说明已有记录，candidate_rule_ids只含本次新增候选。rules_activated=0表示本次没有执行启用，不代表已有规则均未启用。监督候选保留source_incident_id和source_feedback_id；模型比较标签仍不是人工签署的独立真值。

## 状态与版本

| 操作 | 原状态 | 结果 |
| --- | --- | --- |
| approve | candidate | active，须有明确本地范围、有效内容/时间和人工依据 |
| reject | candidate | rejected，保留驳回原因 |
| resubmit | rejected / inactive | candidate，不直接启用 |
| deactivate | active | inactive，保留停用原因 |
| 编辑内容或范围 | 任意 | candidate，新内容重新审核 |

同内容保存不制造新版本。编辑或转换状态增加revision_seq，revision还覆盖来源、适用限制及启用内容哈希。状态绕一圈后不能复用旧版本审核，旧trace的迟到反馈也不能转投新版本。人工启用的activation_content_revision绑定当前内容和适用限制；后续召回仍须通过范围、时间与状态门禁。

普通审批只支持本地规则。缺少集群/namespace、过期、尚未生效或缺少记录时间时不放行。global候选需要另行建设通用审核发布流程，不能从此入口放行。没有UID/版本字段也不会自动获得身份验证。

编辑后旧正负票数转入lifetime_positive_votes/lifetime_negative_votes，历史保留原值，新版本从零计票；累计曝光仍保留曝光语义。启用审核也不将继承的旧票数当新版本收益。这些累计值不是独立事故样本数。

## 审核依据与身份

审核理由必填，其内容是人工陈述，不是服务器已经核实的证据。登录启用时，服务端使用会话用户名或api-token覆盖请求中的审核人；未启用登录时标记operator_label，不宣传为已认证身份。旧编辑入口未保存的个人身份不补造。本轮没有新增细粒度资源授权。

结果始终标注review_basis=manual_evidence_review、independent_benefit_verified=false。人工启用允许受约束复用，不等于冻结留出评测通过。后续仍需旧记忆与候选记忆的成对对照、学习/留出防泄漏、故障域退化和成本检验，不能以回归数量替代收益证明。

## 原子记录与重放

`POST /api/knowledge/rules/{id}/decision`接受revision、decision、reviewer、reason、event_id。相同event_id与相同内容返回原审核事件，改写内容返回409。收据同时返回原事件status/revision和current_status/current_revision；停用或编辑后的原审核重放不会重新启用，页面显示当前状态。

JSON在规则共用锁内重读，将状态、版本及历史一起原子保存。Chroma使用同一锁，在一次记录更新中保存文档及状态/历史元数据。失败返回503，不假装空库或审核完成；未知写入结果应以同一event_id重查。这不是跨存储分布式事务，也不是掉电级exactly-once保证。

新审核和编辑保存前后版本、内容、依据及已知身份来源。每规则上限100条新生命周期事件，达到上限后拒绝继续编辑或晋升，不静默删历史；安全停用仍可追加第101条。历史API支持offset/limit和可选revision校验，页面展示最近100条并说明总数。

物理DELETE规则接口返回409，要求使用带版本和原因的停用。原内容、来源及历史继续保留。本轮只停止该规则的新消费，没有实现派生规则递归撤销、反馈修订传播或重写已冻结调查。

## 页面与验收

知识库支持状态筛选、来源/范围展示、审核、编辑后重审、停用和历史查看。手工名称保持原文，置信度0正确显示，候选事例明确标注未用于诊断；读取失败不当作空库。反馈提示区分新增候选与未新增，不再把候选保存叫作已启用。

证据位于`outputs/memory-candidates-20261022/`。应用测试、真实Chroma和浏览器均在snail执行；真实模型禁用，受控输出和离线嵌入只验证契约。最终数量、未通过记录、重启和归档以该目录verification.md为准。此前真实模型402是否恢复未重新验证。
