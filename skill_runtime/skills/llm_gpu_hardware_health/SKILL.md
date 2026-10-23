---
name: GPU 硬件健康诊断
description: Diagnose GPU ECC, Xid, temperature, power throttle, retired pages, NVLink/PCIe errors, and hardware degradation.
---
# GPU 硬件健康诊断

## 适用场景

用于智算集群 GPU 硬件健康问题：ECC 错误、Xid、温度过高、功耗/时钟降频、retired pages、
NVLink/PCIe 错误、GPU 掉卡、单卡异常导致 vLLM 请求失败或性能退化。

## 输入信号

- DCGM/NVIDIA 指标：ECC、Xid、温度、功耗、时钟、retired pages、NVLink/PCIe replay。
- 节点、GPU UUID、卡号、vLLM Pod、请求时延和错误率。
- `nvidia-smi`、dmesg、kubelet/node event。

## 执行阶段

1. `detect`：识别 GPU 硬件健康、温度、功耗、ECC/Xid 和链路错误。
2. `diagnose`：区分可恢复降频、单卡硬件异常、驱动/Xid、链路错误和节点级问题。
3. `heal`：默认只读；隔离节点、迁移负载、重启驱动必须审批。
4. `verify`：验证错误计数不再增长，GPU 指标和业务延迟恢复。

## 风险与门禁

节点隔离、驱动重启、GPU reset、迁移生产推理负载风险高，必须审批。

## 验证方式

- ECC/Xid/retired pages 不再增长。
- 温度/功耗/时钟回到可接受范围。
- vLLM 请求成功率和 P95 恢复。
