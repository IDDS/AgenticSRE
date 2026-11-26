# AgenticSRE 20261126.5 恢复验证修正版交付说明

本版适用于 Linux x86_64，内含 Dashboard、Skill Runtime、Hermes、openJiuwen WorkSwarm 及离线运行依赖。已有 20261126.4 安装的现场应先备份数据，保留旧版安装器与备份摘要，再安排升级窗口。

## 接收和安装

在接收机将 ZIP 与同名校验文件放在同一目录，执行下列命令。目录中包含安装器、可选配置模板和本版验收记录。

```bash
sha256sum -c AgenticSRE-20261126.5-linux-amd64.zip.sha256
unzip -q AgenticSRE-20261126.5-linux-amd64.zip
cd AgenticSRE-20261126.5-linux-amd64
sha256sum -c SHA256SUMS
```

已有安装的主机先备份。

```bash
sudo install -d -m 0700 /var/backups/agenticsre
sudo agenticsrectl --backup "/var/backups/agenticsre/before-20261126.5-$(date +%Y%m%d-%H%M%S).tar.gz"
```

在目标机的解压目录执行安装，并检查服务与持久化数据。目标机需要 root 权限、可用的 cgroup 和 namespace，以及 8080、8090 端口。

```bash
chmod +x agenticsre-offline-20261126.5-linux-amd64.run
sudo ./agenticsre-offline-20261126.5-linux-amd64.run
sudo agenticsrectl --status
sudo agenticsrectl --data-check
```

初装使用随机生成的管理员密码，管理员可在目标机的 `/var/lib/agenticsre/config/agenticsre.env` 中查看。通过管理网络访问目标机的 8080 端口登录。需要预置参数时，复制 `customer.env.example` 为 `customer.env`，设为 0600、填入现场配置并在安装命令中追加 `--config ./customer.env`。WorkSwarm 真实诊断需要现场模型连通性和有效的 kubeconfig。

## 本次修复

仅要求基础设施就绪的原方案，现在可以在稍后复核时按相同条件结案。系统校验原始记录、复核目标以及验证标准的版本、完整性和指纹。原始记录缺少接受标准、标准已变化，或原方案要求 HTTP/业务断言时，一次基础设施就绪观测无法替代业务验证。重复复核也不能补写缺失的原始依据。

独立自愈中心和诊断页的自动恢复记录都会注明“基础设施就绪，业务未验证”。复核不重放动作，也不给原动作增加成功样本。

## 验收记录

- 旧版镜像复现了“原始标准完整、目标已就绪却一直待验证”。另覆盖原标准缺失、不完整、不同目标、不同验证层次、版本错误及重复复核。
- 远程专项测试 140 项通过，覆盖复核、验证标准一致性、自动恢复编排与业务探针。完整 pytest 2,911 项通过，0 失败、0 错误、0 跳过，40 条警告。逐项记录位于包内 `evidence/pytest.xml`。
- Chromium 通过修正版 Dashboard 复核预置记录。Kubernetes Service 的真实就绪端点得到确认，两处界面均明确业务未验证，历史成功样本没有增加。缺失 Pod 的对照用例保持待验证。另完成 22 个导航入口的只读切换检查。
- 在独立临时命名空间验证经批准的 Kubernetes 扩容、业务探针暂不可达、三次 HTTP 探测后复核成功，以及回滚重复提交返回同一回执。
- 在断网 Linux x86_64 靶机验证首装、管理员登录、WorkSwarm 离线运行协议、备份恢复和重启。另一靶机从 20261126.4 原地升级，核对双服务健康、持久化标记和数据一致性。客户现场权限、业务探针与物理机重启仍需现场验收。

## 版本核对

应用镜像为 `agenticsre-offline:20261126.5-amd64`，修复代码对应 GitHub `930112f` 和 GitCode `dcaa7cc`。安装器共 1,304,169,292 字节，SHA-256 为 `a009a5b740be7c08298a75e57f58c2865c50b37ba4b21275402f4e6d5197172f`。ZIP 校验文件随交付包单独发放。
