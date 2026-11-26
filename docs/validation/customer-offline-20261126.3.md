# AgenticSRE 20261126.3 自愈修正版交付说明

本版适用于 Linux x86_64，内含 Dashboard、Skill Runtime、Hermes、openJiuwen WorkSwarm 及离线运行依赖。已有 20261126.2 安装的现场应先备份数据，保留旧版安装器与备份摘要，再安排升级窗口。

## 接收和安装

在接收机将 ZIP 与同名校验文件放在同一目录，执行下列命令。目录中包含安装器、可选配置模板和本版验收记录。

```bash
sha256sum -c AgenticSRE-20261126.3-linux-amd64.zip.sha256
unzip -q AgenticSRE-20261126.3-linux-amd64.zip
cd AgenticSRE-20261126.3-linux-amd64
sha256sum -c SHA256SUMS
```

已有安装的主机先备份。

```bash
sudo install -d -m 0700 /var/backups/agenticsre
sudo agenticsrectl --backup "/var/backups/agenticsre/before-20261126.3-$(date +%Y%m%d-%H%M%S).tar.gz"
```

在目标机的解压目录执行安装，并检查服务与持久化数据。目标机需要 root 权限、可用的 cgroup 和 namespace，以及 8080、8090 端口。

```bash
chmod +x agenticsre-offline-20261126.3-linux-amd64.run
sudo ./agenticsre-offline-20261126.3-linux-amd64.run
sudo agenticsrectl --status
sudo agenticsrectl --data-check
```

初装使用随机生成的管理员密码，管理员可在目标机的 `/var/lib/agenticsre/config/agenticsre.env` 中查看。通过管理网络访问目标机的 8080 端口登录。需要预置参数时，复制 `customer.env.example` 为 `customer.env`，设为 0600、填入现场配置并在安装命令中追加 `--config ./customer.env`。WorkSwarm 真实诊断需要现场模型连通性和有效的 kubeconfig。

## 自愈行为和验收结果

| 场景 | 验收结果 |
| --- | --- |
| 真实回滚资格 | 仅预演、执行结果未知、缺少步骤、成功记录不完整或无法一一对应回滚命令的请求返回 409，交由人工核对。完整成功的正向多步操作按逆序回滚。 |
| 审批与重试 | 执行和回滚接口校验 JSON 布尔值。已完成的真实回滚再次提交会返回原回执。执行中或结果未知的回滚仍需人工核对。 |
| 业务恢复复核 | 动作成功、业务探针不可达时保留待验证。复核发现失败状态与成功标志矛盾时继续待验证。诊断页面刷新后可读取关联自愈记录的最新复核结果。 |
| 远程回归 | 修复前 14 个反例复现。修复后 78 项专项通过，完整 pytest 2,886 项通过、0 失败、0 跳过，40 条警告。 |
| 真实 Kubernetes 验收 | 在临时命名空间中通过修正版 Dashboard HTTP 接口操作。预演记录请求真实回滚返回 409，副本保持 0。经批准将 Nginx 从 0 扩到 1，探针不可达时为待验证。经 Kubernetes Service 端口转发对实际 Nginx 完成 3 次 HTTP 探测后，复核成功。回滚至 0 后重复提交，回执相同，Deployment 的 generation 保持不变。测试命名空间已清理。 |
| 断网部署 | 在无 systemd 的 Linux amd64 隔离靶机用最终安装器完成首装，验证登录、WorkSwarm 宿主协议、数据备份与恢复，以及服务重启后 Dashboard 和 Skill Runtime 均健康。 |
| 断网升级 | 在另一台隔离靶机安装 20261126.2，写入持久化标记并备份，再用最终安装器原地升至 20261126.3。双服务健康，标记内容保持一致，数据校验通过。 |

真实集群用例覆盖带审批的执行与回滚。无人审批的策略编排另有受控专项回归。本轮未实测客户业务、客户现场配置或物理机断电重启。现场应配置能代表实际业务的只读探针，并用本站点的审批策略和权限完成验收。

## 版本核对

- 安装器为 `agenticsre-offline-20261126.3-linux-amd64.run`，共 1,303,293,359 字节，SHA-256 为 `b0dadc78a3207fd4dfa4c93f8f72b8a5aedda26244f72f6f38e4e962070fa2af`。
- 镜像为 `agenticsre-offline:20261126.3-amd64`，复用 20261126.2 的依赖，替换了已验证的 Dashboard 自愈实现。构建源码提交为 GitHub `89e843b`，GitCode `0612d82`。
- `evidence/` 提供完整 pytest XML、自愈专项及修复前反例摘要、真实集群记录、断网安装与升级摘要和构建元数据。
