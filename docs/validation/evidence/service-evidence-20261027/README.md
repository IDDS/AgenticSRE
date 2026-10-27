# Service 定向取证与身份绑定收据

说明与限制见[本轮验收报告](../../service-evidence-binding-20261027.md)。以下均为远端实际收据；公开摘要不含对象 UID、工具原文或私有连接配置。

| 内容 | 收据 |
|---|---|
| 实际故障与阶段结果 | [summary.json](summary.json) |
| 预算内状态与后续快照分别保存 | [diagnosis-snapshots.json](diagnosis-snapshots.json) |
| 冷启动原始失败计数与恢复窗口 | [business-readiness.json](business-readiness.json)、[recovery-windows.json](recovery-windows.json) |
| 根因 UID 匹配、原对象恢复、无原生自愈 | [cleanup-verification.json](cleanup-verification.json) |
| 310 专项及 2292 全量回归 | [执行收据](packaged-verification.json)、[专项日志](targeted.txt)、[完整日志](full.txt) |
| 镜像导入及全部 425 个 COPY 源文件核对 | [packaging-verification.json](packaging-verification.json)、[copy-bytes-verification.json](copy-bytes-verification.json) |
| 两条过程记录、策略及报告只读可访问 | [browser.json](browser.json) |
| 冻结源码及实际控制脚本标识 | [source-version-receipt.json](source-version-receipt.json) |
| 本轮临时资源清理、生产服务状态 | [own-resources-cleanup.json](own-resources-cleanup.json) |

选择性私有归档包含 36 项（35 个载荷及一个清单），SHA-256：

`30da9449caebdb09217bef589ea0e2dc6ac6ed481db9f712cad2eb6fc2ea5ec9`

归档保留逐笔业务样本、盲诊请求、控制契约、恢复与源码收据，不包含私有配置或任意工具原文。公开文件哈希见 [evidence-sha256.json](evidence-sha256.json)；后续临时容器清理单独记录，不改写原验收归档。
