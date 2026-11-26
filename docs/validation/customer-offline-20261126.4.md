# AgenticSRE 20261126.4 恢复验证修正版交付说明

本版适用于 Linux x86_64，内含 Dashboard、Skill Runtime、Hermes、openJiuwen WorkSwarm 及离线运行依赖。已有 20261126.3 安装的现场应先备份数据，保留旧版安装器与备份摘要，再安排升级窗口。

## 接收和安装

在接收机将 ZIP 与同名校验文件放在同一目录，执行下列命令。目录中包含安装器、可选配置模板和本版验收记录。

```bash
sha256sum -c AgenticSRE-20261126.4-linux-amd64.zip.sha256
unzip -q AgenticSRE-20261126.4-linux-amd64.zip
cd AgenticSRE-20261126.4-linux-amd64
sha256sum -c SHA256SUMS
```

已有安装的主机先备份。

```bash
sudo install -d -m 0700 /var/backups/agenticsre
sudo agenticsrectl --backup "/var/backups/agenticsre/before-20261126.4-$(date +%Y%m%d-%H%M%S).tar.gz"
```

在目标机的解压目录执行安装，并检查服务与持久化数据。目标机需要 root 权限、可用的 cgroup 和 namespace，以及 8080、8090 端口。

```bash
chmod +x agenticsre-offline-20261126.4-linux-amd64.run
sudo ./agenticsre-offline-20261126.4-linux-amd64.run
sudo agenticsrectl --status
sudo agenticsrectl --data-check
```

初装使用随机生成的管理员密码，管理员可在目标机的 `/var/lib/agenticsre/config/agenticsre.env` 中查看。通过管理网络访问目标机的 8080 端口登录。需要预置参数时，复制 `customer.env.example` 为 `customer.env`，设为 0600、填入现场配置并在安装命令中追加 `--config ./customer.env`。WorkSwarm 真实诊断需要现场模型连通性和有效的 kubeconfig。

## 恢复验证变更

| 观测情况 | 系统行为 |
| --- | --- |
| 未能读取目标 Pod、Service、Node 或 Deployment，或未能确认清理结果 | 保留原始检查记录，标记待验证。该次读取不会计为恢复失败样本。 |
| 目标 Pod 无法读取，同一诊断还列有健康 Service | 结束本次 Pod 验证。其他对象的就绪状态不会代替原目标的证据。 |
| Deployment 的控制器尚未观测当前 generation | 等待当前版本的状态。期望副本为 0 时，遗留的就绪副本不能证明服务已经恢复。 |
| 读取命令成功退出，但输出为空、无效 JSON 或并非 Kubernetes 对象 | 记录读取错误，保留复核入口。 |
| 运维人员在自愈中心重新验证、刷新页面 | 页面分别显示“集群状态读取未完成，可重新验证”或“等待 Deployment 控制器观测当前版本”。重新验证沿用原目标且不重放修复命令。 |

## 远程验收

在修复前的 20261126.3 镜像上，修正测试夹具后复现 16 个反例。专项回归覆盖读取异常、正反就绪证据、自愈复核、业务探针、历史记录与执行边界，180 项通过。完整 pytest 2,905 项通过，0 失败、0 错误、0 跳过，40 条警告。逐项结果见包内 `evidence/pytest.xml`。

使用新版镜像启动独立 Dashboard，通过 Chromium 登录后点击“重新验证”。验证目标为实际集群中不存在的 Pod，同条预置待验证记录中还包含有就绪端点的 Service。复核只执行一次 Pod 查询，保留待验证状态，未写入恢复成败样本。刷新页面后仍然显示对应提示和复核按钮。浏览器对 22 个导航入口进行只读切换，核对激活视图和页面标题，未出现未捕获的 JavaScript 错误。

另在临时 Kubernetes 命名空间内通过 Dashboard 实际执行带审批的扩容，将 Nginx 副本从 0 调到 1。业务探针不可达时保持待验证，经 Service 端口转发连续三次 HTTP 探测成功后通过复核。回滚至 0 后再提交返回同一回执，Deployment generation 保持不变，测试资源已清理。

安装介质在与外网隔离的无 systemd Linux amd64 靶机上验收。首装检查覆盖管理员登录、WorkSwarm 模型未配置提示、SDK 宿主协议、备份恢复及重启后的双服务健康。另一靶机先安装 20261126.3 并写入持久化标记，经备份后原地升级到 20261126.4，核对镜像版本、标记内容和数据校验结果。物理机断电重启、现场模型和现场权限需要在目标环境验收。

## 版本核对

- 应用镜像为 `agenticsre-offline:20261126.4-amd64`，复用 20261126.3 离线依赖。修复代码对应 GitHub `c3231fe` 和 GitCode `bfeb1e4`。
- 安装器为 `agenticsre-offline-20261126.4-linux-amd64.run`，共 1,303,729,035 字节，SHA-256 为 `127e340c3d4b24093a0610bc2f64463f8122af24752984db761e932e90ff02a7`。
- `evidence/` 包含构建元数据、修复前反例、专项和完整回归结果、浏览器与集群验收、断网首装和升级记录。
