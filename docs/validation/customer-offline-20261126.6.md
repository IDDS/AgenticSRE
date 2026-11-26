# AgenticSRE 20261126.6 · WorkSwarm 服务诊断交付说明

本版适用于 Linux x86_64。进入「高级工具 → 独立根因分析」，选择「openJiuwen / WorkSwarm 服务诊断」，填写命名空间、Service 名称和问题即可发起诊断。指定集群配置 ID 时，工具仅访问该配置对应的 Service、EndpointSlice 和 Pod。可从 RCA 历史按 Service 或 WorkSwarm 搜索，并重新展开原始工具观测。独立 WorkSwarm 页面支持在同一目标上继续提问。

WorkSwarm 负责本次服务取证的推理和工具调用。完整、部分成功和失败均会记录。此模式不会触发自动恢复。既有多智能体 RCA 和 Agent+Skill 继续使用各自的执行路径。

## 接收、安装和升级

将 ZIP 和同名 SHA-256 文件放入一个目录，在目标机执行以下命令。

```bash
sha256sum -c AgenticSRE-20261126.6-linux-amd64.zip.sha256
unzip -q AgenticSRE-20261126.6-linux-amd64.zip
cd AgenticSRE-20261126.6-linux-amd64
sha256sum -c SHA256SUMS
```

已有安装的主机先备份，保留旧版安装器及备份摘要。

```bash
sudo install -d -m 0700 /var/backups/agenticsre
sudo agenticsrectl --backup "/var/backups/agenticsre/before-20261126.6-$(date +%Y%m%d-%H%M%S).tar.gz"
```

在解压目录运行安装器，并检查两项服务的健康状态及数据一致性。

```bash
chmod +x agenticsre-offline-20261126.6-linux-amd64.run
sudo ./agenticsre-offline-20261126.6-linux-amd64.run
sudo agenticsrectl --status
sudo agenticsrectl --data-check
```

目标机需要 root 权限、可用的 cgroup 和 namespace，以及可分配的 8080、8090 端口。安装不依赖互联网。发起真实诊断需要可用的模型服务和能读取目标资源的集群配置。可选配置模板为 `customer.env.example`。现有安装的升级请安排在维护窗口。

## 验收记录

- 全量 pytest 共 2,914 项通过，0 失败、0 错误、0 跳过，40 条警告。WorkSwarm 与 RCA 历史专项 39 项通过。Node 回归 5 项通过。
- Chromium 从真实表单对正常及缺失 Service 发起诊断。正常目标的 1 个端点和 1 个 Pod 与独立 kubectl 查询一致。缺失目标显示证据不全。刷新页面后，两条历史均可重新打开。独立 WorkSwarm 会话也通过正常和缺失目标复验。逐个打开 22 个导航入口未出现脚本异常。
- 在断网的 Linux 容器靶机上验证正式安装器的全新安装、双服务健康、备份恢复及服务重启。另一靶机从 20261126.5 原地升级，并验证持久化标记及数据一致性。验收记录位于包内 `evidence/`。
- 浏览器复验可在已配置 Playwright、Chromium 和 kubectl 的远程机器运行 `python tests/browser_workswarm_rca.py <Dashboard地址> <命名空间> <带选择器的Service名称>`。kubectl 和 Dashboard 默认集群应一致。

应用镜像为 `agenticsre-offline:20261126.6-amd64`。对应代码提交为 GitHub `8aa18db`、GitCode `99c8710`。安装器共 1,304,652,854 字节，SHA-256 为 `2866fb98e67215861a2f8666c441a652db3f9e79ad7ae00137aa3fc29eac3fd3`。
