# Agent＋Skill 诊断优化系统更新（2026-10-13）

## 发布结果

按用户“提交和更新系统”的指令，本轮代码已提交，并部署到远程 `snail` 的
正式 Web 与 Skill Runtime。两个服务均健康，接口与浏览器检查通过，未触发回滚。

- 代码提交：`1adaf7b34c22f94a107a74574214765bd98b7339`，分支 `feature/skill-runtime`。
- 正式镜像标签：`agenticsre:skill-owned-20261013-r7`。
- 实际镜像：`sha256:b07ab1499f8baddc694fc67c034feb3d52f4d3c8e604fec578398af434074362`。
- 发布目录：`/home/ubuntu/AgenticSRE-releases/skill-owned-release-20261013`。
- Skill Runtime 启动：2026-10-13 15:53:09（北京时间）。
- Web 启动：2026-10-13 15:53:29（北京时间）。
- 含上线前后检查的发布脚本耗时：61.242 秒。

本次只提交本轮源码、测试和验证文档；工作区既有录屏及实验产物未加入 Git。
未执行远程仓库推送。

## 依据与上线验证

复用[诊断优化复验](skill-owned-diagnosis-20261013.md)中通过远程 **1059 项全量
测试**、镜像故障与三服务传播门禁的同一镜像，未重新构建其他算法版本。
提交前核验工作区与候选镜像的源码哈希一致。此前纯多智能体对照的 Judge
失败仍保留在复验报告中，没有把它记为通过。

上线前确认没有活动 RCA，记录数据与配置哈希，并检查生产页面。沿用原正式
Compose 文件及既有配置挂载，只追加镜像与版本标签覆盖项。依次更新 Runtime
和 Web，每个容器通过健康检查后核验 13 个源码及测试文件的 SHA-256。

| 项目 | 验证结果 |
|---|---|
| Web `8080/api/health` | `ok`，LLM 已配置、持久化正常 |
| Runtime `8090/api/health` | `ok`，62 个 Skill |
| 浏览器页面 | HTTP 200，标题 `AgenticSRE Console`，加载 357 毫秒，无捕获到的页面或控制台错误 |
| Runtime 镜像故障负例 | 仅提供告警、无真实取证时 detect 与 diagnose 均 skipped，没有模板根因 |
| 知识规则 | 114 条，部署前后内容哈希一致 |
| 修复配方 | 34 条，部署前后内容哈希一致 |
| 故障记忆 | 82 条，计数一致 |
| 事件生命周期 | 25 条，内容哈希一致 |
| 运行配置 | 环境、启动命令、挂载、网络、重启策略及配置文件哈希一致 |
| 其他容器 | ID 与镜像一致 |

本次上线后检查没有执行故障注入、自愈写操作或新的模型诊断任务。
实际故障诊断效果由同一镜像的发布前隔离现场验证支持，不能将页面健康检查
解释为所有生产故障诊断都已验证。

## 回滚与证据

旧正式镜像保留：
`sha256:807569a268af4f74f7b8502ad687b97bba3eb988ae0751ee3814b67612aaf201`。

发布脚本在替换后检查失败时自动使用原 Compose 恢复两个服务；本次未触发。
手工回滚可在远程执行原 Compose 配置：

```bash
docker compose -p agenticsre \
  --project-directory /home/ubuntu/AgenticSRE-releases/pure-agent-release-20260924-r4 \
  -f /home/ubuntu/AgenticSRE-releases/pure-agent-release-20260924-r4/docker-compose.production.yaml \
  -f /home/ubuntu/AgenticSRE-releases/pure-agent-release-20260924-r4/docker-compose.skill-production.yaml \
  up -d --no-deps --no-build --pull never agenticsre-skill-runtime agenticsre
```

首次预检遇到受保护配置文件读取权限与旧 Playwright 缓存缺失；均在替换生产
容器前处理。受保护配置只计算哈希，浏览器依赖仅安装在远程发布目录，配置
内容与凭据未写入发布产物。

证据包：`outputs/skill-owned-release-20261013/deployment-evidence.tar.gz`。
包含 12 个验证文件和哈希清单，已在本地逐项核验。SHA-256：
`4bc91a8ad343a2ffdb18246aaa9fe175ed0100ce21fb6ca0e60630b74c6e0c47`。

主要文件为 `deployment-result.json`、`before.json`、`source-sha256.json`、
`skill-negative-check.json` 和上线前后浏览器结果、截图。不包含生产配置原文、
环境变量值、凭据或数据备份。
