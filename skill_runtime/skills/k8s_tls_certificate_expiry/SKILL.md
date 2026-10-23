---
name: TLS 证书与握手诊断
description: Diagnose Kubernetes TLS certificate expiration, secret mismatch, SNI, and handshake failures.
---
# TLS 证书与握手诊断

## 适用场景

用于证书过期、TLS Secret 引用错误、SNI/Host 不匹配、HTTPS 握手失败、
入口网关返回证书错误或 mTLS 证书异常。

## 输入信号

- TLS Secret、证书 NotAfter、issuer、SAN、SNI、Host。
- Ingress/Gateway TLS 配置、HTTPRoute、证书管理器事件。
- 客户端错误：certificate expired、unknown authority、handshake failure。

## 执行阶段

1. `detect`：识别证书、TLS、SNI、握手失败信号。
2. `diagnose`：区分证书过期、Secret 缺失、域名不匹配、CA 不可信和 mTLS 配置错误。
3. `heal`：生成续签、Secret 引用修复和灰度验证建议；真实变更需审批。
4. `verify`：验证证书有效期、SAN、入口 TLS 和业务探测。

## 风险与门禁

证书替换会影响所有 HTTPS 流量。必须审批并保留旧 Secret 或回滚路径。

## 验证方式

- `openssl s_client` 可完成握手。
- 证书未过期，SAN 覆盖目标域名。
- Ingress/Gateway 绑定正确 Secret。
