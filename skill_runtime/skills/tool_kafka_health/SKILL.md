---
name: Kafka 健康诊断
description: Diagnose Kafka broker, consumer lag, partition, ISR, rebalance, and producer/consumer timeout issues.
---
# Kafka 健康诊断

## 适用场景

用于 Kafka broker 不可用、consumer lag 积压、ISR 不足、partition offline、rebalance 频繁、
producer timeout 或消息队列依赖导致业务延迟/错误。

## 输入信号

- topic、consumer group、lag、under replicated partitions、offline partitions。
- broker、bootstrap server、producer/consumer timeout、rebalance 日志。
- Prometheus Kafka exporter 或客户端指标。

## 执行阶段

1. `detect`：识别 Kafka、lag、broker、partition、ISR 和 timeout 信号。
2. `diagnose`：区分消费积压、broker 不可用、分区副本异常、生产者阻塞和 rebalance。
3. `verify`：验证 lag 下降、ISR 恢复、生产消费成功。

本 Skill 不删除 topic、不重置 offset，也不重启 broker。

## 风险与门禁

重置 offset、删除 topic、重启 broker 或修改副本数风险高，必须审批。

## 验证方式

- broker 可连通。
- consumer lag 持续下降。
- under replicated/offline partitions 为 0。
