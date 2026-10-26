# 业务响应恢复与验证标准隔离验收

2026-10-26 的两轮增量补充了有界 JSON 业务响应断言，以及按恢复验证标准隔离历史的完整数据流。配置说明见 [业务响应验证](../business-recovery.md)，标准、兼容和门禁见 [历史隔离](../recovery-verification-history.md)。这次仓库同步没有执行生产部署。

## 已验证的行为

- 配置 JSON 条件后，HTTP 200 但字段不符、缺失或空结果不能算恢复；布尔与数字不混同，缺失不等于 null。读取不可得或超预算保留未知，不泄露响应体或断言值。
- 复用单次观察窗口的连接；使用有界读取与完整响应时延检查，支持普通及 chunked 响应，非 identity 内容编码保持未知。
- 方案、实际记录、历史读取与自动门禁绑定相同验证标准。默认值和普通等效数字规范化，标准变化后旧样本不参与新成功率。
- 未标记旧记录保留，不补猜标准；未知、未完成验证及冲突元数据排除。通用旧统计仍可概览，但不能用于自动放行。
- 复验只更新观测，不重放动作、不补记因果成功；原要求在探测前固定，异常及重复复验不能逐渐采用较弱标准，已知基础设施失败仍保留。
- 镜像 COPY 包含实际恢复模块，避免既有基础镜像中的旧文件掩盖发布遗漏。
- JSON 数字按十进制精确比较，避免 `9007199254740993.0` 被浮点舍入后错误匹配整数 `9007199254740992`。
- 明确执行失败的动作按计划标准计入失败尝试分母；不运行恢复探针、不声称业务已验证。超时、传输异常、结果未知、未派发与预演不进入二项统计。

## 冻结候选结果

最终发布候选 `recovery-publish-20261026-r2` 冻结 846 个文件，包含业务断言、历史标准隔离及独立审查发现的数值精度和失败分母修复。应用测试、镜像构建及真实 HTTP 请求均在远程执行；本机只整理文件、进行静态审查和核验哈希。测试结束后仅补充文档和证据，运行时与测试字节保持一致，见 [源码 SHA-256 清单](evidence/recovery-verification-20261026/runtime-source-hashes.json) 和 [公开证据校验清单](evidence/recovery-verification-20261026/evidence-sha256.json)。早期业务阶段 88/2156 和历史功能阶段 165/2199 的结果保留在各自说明中，不作为最终候选总数。

| 检查 | 结果 | 记录 |
|---|---|---|
| 专项：新标准、旧探针/复验与置信度门禁 | 181 passed，5 项现有弃用警告 | [日志](evidence/recovery-verification-20261026/packaged-targeted.txt) |
| 完整回归 | 2215 passed，40 项现有弃用警告 | [日志](evidence/recovery-verification-20261026/packaged-full.txt) |
| 执行来源、JS语法、冻结前后哈希 | passed，禁网/只读源码 | [收据](evidence/recovery-verification-20261026/packaged-verification.json) |
| 实际应用 COPY 层镜像 | 完整Web应用导入、三模块SHA与测试源码一致，无源码挂载 | [收据](evidence/recovery-verification-20261026/packaging-verification.json) |
| 镜像内历史读写、门禁、异常复验 | 五阶段通过 | [收据](evidence/recovery-verification-20261026/image-history-verification.json) |
| 镜像内真实loopback HTTP | 正确、业务错误、超体积、慢响应、数值精度五项通过 | [收据](evidence/recovery-verification-20261026/image-http-verification.json) |
| 实际镜像前端徽标函数 | 七项通过，区分业务断言成功与动作失败未验证 | [收据](evidence/recovery-verification-20261026/ui-verification.json) |

镜像内合成历史实测：14条旧成功记录对应合格样本0、门禁拒绝；14条匹配标准的记录重启读回后门禁计算通过；修改标准后这14条被排除；一个新失败独立计数；首次复验故意抛错后仍坚持原要求。门禁计算通过没有执行操作，合成数据没有写入生产记忆。正常HTTP/1.1两次采样使用同一连接，仅证明复用机制，不能外推生产效率提升。

专项另验证真实 JSON HTTP 成功经原生 writer 写入、JSON 重启读回和同标准查询形成一个成功样本，因样本不足仍不放行。执行失败回归走真实 `heal_execute`、原生 writer 与重启统计，仅替代实际命令和探针：14 个同标准成功加 5 个明确失败后得到 19 个样本、14 成功、5 失败，门禁拒绝；其余十种未知、未派发或预演场景不计样本，所有这些场景均无恢复探针调用。明确失败收据标注 `action_execution` 与 `planned_acceptance`，不表示实测业务未恢复。

## 修复前反例

- [旧业务合同24项失败](evidence/recovery-verification-20261026/business-pre-fix-body-contract-24-failures.txt)：原探针忽略业务内容及相关配置。
- [镜像复制失败](evidence/recovery-verification-20261026/business-pre-fix-image-copy-failure.txt)：源码测试通过但恢复模块未被COPY；已补齐并在新镜像核对哈希。
- [配置错误掩盖基础设施失败](evidence/recovery-verification-20261026/business-pre-fix-infrastructure-failure-preservation.txt)：三项失败及三个对照，修正后保留已知失败。
- [旧历史标准24项失败](evidence/recovery-verification-20261026/pre-fix-legacy-contract-24-failures.txt)：包括14条无标准成功记录被错误统计。
- [异常锚点两项失败](evidence/recovery-verification-20261026/pre-fix-exception-anchor-2-failures.txt)：修正要求固定时机，覆盖基础设施与业务探测异常。
- [数值精度反例](evidence/recovery-verification-20261026/pre-fix-number-precision.txt)：四个对照在修复前两项失败、两项通过，复现大数小数和高精度小数误匹配。
- [执行失败统计反例](evidence/recovery-verification-20261026/pre-fix-execution-failure.txt)：十一场景在修复前失败，包含明确失败遗漏、未知分类和不应调用探针的旧路径。

这些失败来自不同阶段，不把它们拼成同一轮旧版测试总数。既有门禁的正对照补充了动作/环境/验证标准元数据，保留统计下界断言，没有通过删除测试放宽门禁。

## 验证边界

真实JSON文件验证了持久化重启；扁平元数据采用受控集合适配器，不等于实际Chroma服务全面验收。列表接口的既有某些错误可能降级为空集，零样本不证明存储健康；样本不足仍不能放行。镜像构建复用已有依赖，不代替从头重建依赖。本轮没有完整浏览器页面验收，前端证据限于既有徽标函数和语法检查。

没有真实业务故障注入、真实自愈动作、模型诊断准确率、长期复发或MTTR收益评测。指纹用于条件匹配，不是来源签名或因果证明；恢复成功不作为根因正确标签。本次没有扩大自动执行权限、启用现场探针或替换生产服务。
