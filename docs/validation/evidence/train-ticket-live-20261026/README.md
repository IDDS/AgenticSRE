# 站点查询真实故障验收证据

完整说明见 [验收报告](../../train-ticket-live-recovery-20261026.md)。以下为实际归档的选择性收据，保留预算内状态、后续快照和未通过结果。

| 内容 | 证据 |
|---|---|
| 模型恢复访问预检 | [provider-preflight.json](provider-preflight.json) |
| 第一轮原判定与后来恢复确认 | [r1-summary.json](r1-summary.json)、[r1-followup.json](r1-followup.json) |
| 第二轮故障、门禁与恢复 | [r2-summary.json](r2-summary.json)、[恢复窗口](r2-recovery-windows.json) |
| 第二轮冷启动观测 | [r2-business-readiness.json](r2-business-readiness.json)，13 笔失败后首笔有效响应，全部原样留存于私有业务采样 |
| 诊断预算内状态与后来快照 | [diagnosis-snapshots.json](diagnosis-snapshots.json) |
| 原对象、规格、无遗留 fence、原生自愈记录为零 | [cleanup-verification.json](cleanup-verification.json) |
| 实际与最终源码版本差异 | [source-version-receipt.json](source-version-receipt.json) |
| 专项/完整回归 | [执行收据](packaged-verification.json)、[220 项专项](packaged-targeted.txt)、[2254 项完整日志](packaged-full.txt) |
| 镜像 COPY 与实际代码字节 | [模块核对](packaging-verification.json)、[424 个 COPY 文件核对](copy-bytes-verification.json) |
| 六条过程记录可读 | [browser.json](browser.json)，含失败记录；不是六次诊断正确 |
| YAML 与迟到重注入修复前反例 | [8 失败/1 对照通过](structured-evidence-pre-fix.txt)、[两项竞态失败](watchdog-resume-pre-fix.txt) |

完整私有归档 SHA-256：

eb42d889ab6c6e11877e25be0f4ef97f9ff2f1bf05dacaed3edbee2d6de8b7f9

归档有 92 项（91 个载荷和一个清单），包含逐笔采样、请求、阶段记录、后续诊断快照、截图和源码标识。公开摘要移除根因实体 UID，不上传私有连接配置、环境元数据和任意主机日志。每份公开文件的哈希见 [evidence-sha256.json](evidence-sha256.json)。源码哈希用于版本核对，不是来源签名或诊断正确证明。
