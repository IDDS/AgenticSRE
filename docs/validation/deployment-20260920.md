# 2026-09-20 远端部署记录

## 结果与边界

按用户本次部署指令，将提交 `91d0936` 对应的运行源码部署到 snail 的
`agenticsre` 和 `agenticsre-skill-runtime`。两个正式容器均 healthy，接口与
前端发布检查通过。使用既有 Docker Compose 部署方式，未执行 GitCode 推送。

镜像固定为 `sha256:48db82e03f3fe154d247a5272bf5ade1392e66fe96014b70129dc92675c118a8`。
候选及两个正式容器中的 27 个变更源码文件逐项 SHA-256 匹配本地提交，包含
前端 JavaScript。沿用该镜像此前远端 **890 passed / 108.93 秒** 的回归结果，
本次未重复运行全量测试，未执行新的完整 LLM RCA 故障实验。

**部署成功不代表可信 RCA 已验收。** 最近完整 RCA 质量分仍为 0.510，低于
0.65 门槛；最终日志补丁仍需完整链路复验。本次没有降低门槛或绕过诊断人工
复核逻辑。独立标注、竞争解释及真实业务准确率验收仍待完成。

## 发布与实际检查

- 发布前无在途 RCA；先检查隔离候选，再依次更新 Skill Runtime 和主服务。
- 正式告警生命周期的 25 条事件在更新及接口验证前后完全一致。
- 实际调用非语义压缩任务接口，完成两条人工测试输入的处理；分页及旧接口
  返回正常，未改变正式事件生命周期。这是接口冒烟，不是压缩效果实验。
- 8080、8090 健康接口均返回 ok，Job Skill 查询成功。
- 远端浏览器访问正式前端 HTTP 200，DOM 加载 504 毫秒，无控制台错误。
- 告警窗口桌面/移动端展示通过；根因候选展示、过期隐藏和移动端无溢出通过。
  这些展示检查使用既有真实实验数据回放，不冒充新产生的告警。
- 四个其他项目容器 ID 和镜像保持不变。

## 数据和回滚

远端发布目录：`/home/ubuntu/AgenticSRE-releases/deploy-20260920/`。
继承旧版配置、挂载和数据卷，只替换镜像，Compose 使用不可变镜像 ID。
脚本在服务更新或验证失败时自动恢复旧版两个服务镜像；本次无需回滚。

首次全目录备份发现数据约 41 GB，主要是本次不涉及的 Teller 历史数据。
在更新服务前主动停止该备份，退出 143；原日志保存为
`deploy-full-backup-stopped.log`，不完整包为 `data-full-backup.incomplete.tar.gz`，
不能用于恢复。随后成功生成 `data-before.tar.gz`，约 47 MB，排除 `data/teller`。
备份以 0600 权限留在远端，未取回或提交；Teller 数据原卷保持不变。
这是在线应用状态备份，不声称跨数据库事务一致快照。

回滚使用旧发布目录
`/home/ubuntu/AgenticSRE-releases/sampletime-20260918/` 的两个生产 Compose 文件，
项目名 `agenticsre`，恢复镜像 `agenticsre:sampletime-20260918`。
旧镜像和数据备份保留。

## 前次收尾与证据

远端连接恢复后，前次实验归档已取回，118 个清单文件哈希全部匹配。
归档 SHA-256：`c4ab1d4b5280acbec96b6bc3b92a472c60363317e3b8c501e1a170cdc4552a35`。
其中的旧清理记录属于归档时状态；最新清理结果另保存在本次部署证据中。

两个隔离容器已校验 Compose 项目和镜像归属后删除：
`agenticsre-rca-runtime-canary`、`agenticsre-skill-runtime-rca-runtime-canary`。
正式两个服务及四个其他项目容器均保留。

本地证据位于 `outputs/deploy-20260920/` 和
`outputs/rca-runtime-20260918/verification-artifacts.tar.gz`。
部署归档按白名单包含检查结果、脚本、日志和截图，排除配置及数据备份。
归档已取回，35 个清单文件全部通过哈希校验；归档 SHA-256 为
`81dd294f6b0817ccb29c577ffa3e2de8bac20a8ae8f5544f57381bfae8dbcd43`。
