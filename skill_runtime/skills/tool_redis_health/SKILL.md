---
name: Redis 健康诊断
description: Diagnose Redis connection pool, memory, eviction, latency, slowlog, and unavailable dependency issues.
---
# Redis 健康诊断

## 适用场景

用于 Redis 连接耗尽、超时、内存压力、eviction、慢查询、主从复制异常、缓存依赖不可用
导致业务错误率或延迟升高。

## 输入信号

- Redis endpoint、db、连接数、blocked clients、used_memory、evicted_keys、slowlog。
- 应用错误：connection refused、timeout、MOVED/ASK、READONLY。
- Prometheus Redis exporter 指标或客户端连接池指标。

## 执行阶段

1. `detect`：识别 Redis 依赖、连接池、内存和慢查询信号。
2. `diagnose`：区分连接耗尽、内存淘汰、慢命令、复制/集群路由和服务不可用。
3. `verify`：验证连接、延迟、内存和业务错误率恢复。

本 Skill 不执行 flush、不重启 Redis，也不终止连接。

## 风险与门禁

Redis 写操作、flush、重启、主从切换和配置修改风险高，必须审批。

## 验证方式

- PING/PONG 正常。
- 连接池 waiting 降低，used_memory/evicted_keys 稳定。
- 业务错误率和 P95 恢复。
