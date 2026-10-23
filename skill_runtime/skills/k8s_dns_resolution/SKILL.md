---
name: K8s DNS 解析故障
description: 诊断 CoreDNS/kube-dns 解析失败，并生成受控恢复建议。
---
# K8s DNS 解析故障诊断与守护式修复

本 Skill 用于诊断 CoreDNS/kube-dns 解析失败，并提出受控恢复建议。

## 适用场景

- Pod 内无法解析 Service 名称或外部域名。
- CoreDNS Pod 崩溃、重启或副本不足。
- CoreDNS ConfigMap 配置错误导致解析异常。
- 上游 DNS 转发失败或 kube-dns Service Endpoint 缺失。

## 输入信号

- 应用容器内 `nslookup` 或 `dig` 的解析结果与错误。
- kube-system 中 CoreDNS Pod 的状态与重启计数。
- CoreDNS 日志中的 `SERVFAIL`、上游超时记录。
- CoreDNS ConfigMap、Service 和 Endpoint 状态。

## 执行阶段

- `detect`: 确认解析失败范围，区分单命名空间、全集群、内部或外部域名。
- `diagnose`: 结合 CoreDNS 状态、日志、Corefile 与 Endpoint 定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如滚动重启 CoreDNS 或修正 Corefile 的候选方案。
- `verify`: 批准执行后从 Pod 侧复测解析。

## 风险与门禁

- 风险等级 medium，CoreDNS 重启或配置变更影响全集群解析，必须审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.82，全自动执行门槛为 0.90；两者均不表示恢复成功概率。

## 验证方式

- 从业务 Pod 解析 `<service>.<namespace>.svc.cluster.local` 正常返回。
- CoreDNS Pod 全部就绪，日志无 `SERVFAIL` 或上游超时。
- 内外部域名解析恢复正常。
