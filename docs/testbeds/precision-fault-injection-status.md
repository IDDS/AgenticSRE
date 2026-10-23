# 细粒度故障注入实施状态与验收规范

本文说明 AgenticSRE 细粒度故障注入的实际完成度、执行边界、逐项证据和验收门。
它是实施与验收说明，不是面向生产环境的无审批操作手册。

## 1. 当前结论

截至 2026-07-30：

| 能力层 | 完成度 | 说明 |
|---|---:|---|
| 场景登记 | 18/18 | 18 个场景、12 种机制均进入版本化目录 |
| 强类型合同 | 18/18 | 目标、匹配条件、时限、命中上限、爆炸半径和证据字段均由服务端校验 |
| 受审执行适配器 | 18/18 | 每个场景固定映射到一个仓库内适配器，不接受命令、脚本或临时执行地址 |
| 远端真实注入 | 17/18 | 17 个场景完成目标命中、对照隔离、Undo、恢复和残留检查 |
| 资源阻塞 | 1/18 | DCGM 场景因隔离 GPU 为 0（3 张可分配 GPU 已全部预留）未执行 |
| 自动事务恢复 | 17/18 | 通过场景均在同一次实验内完成 Undo 和恢复验证 |
| 事务式自愈合同 | 18/18 | 全部场景可基于精确持久化记录生成 `detect → diagnose → heal → verify` 合同，仅允许原 Undo |
| 远端自愈校验 | 代表链路已通过 | 一次性应用 failpoint 完成真实闭环后，原 Undo 幂等重放和残留验证返回 `already_recovered` |
| Agent 自愈 F6 | 0/18 | 尚未逐项验收“故障仍活跃时诊断—决策—原交易 Undo—恢复验证”的独立 Agent 闭环 |
| 前端闭环 | 已通过 | “通算故障实验”已集成“细粒度注入”页签，18 张卡片和 7 类动作均通过远端浏览器验证 |

目录状态为：

```yaml
execution_mode: approval_gated_transactional_execution
validation_status: remote_real_validation_17_passed_1_gpu_capacity_blocked
```

真实执行必须同时提交 `confirm=true` 和 `experiment_ack=true`，并通过运行时预检。
适配器只有在输出 `validation=PASS` 且完成残留检查时才会被 API 记为成功。

机器可读证据见
`data/validation/precision_fault_injection_remote_20260730.json`。

## 2. 完成等级

| 等级 | 名称 | 通过条件 |
|---|---|---|
| F0 | 已登记 | 场景 ID、目标、参数和安全预算进入版本化目录 |
| F1 | 合同已验证 | 拒绝命令、通配符、越界目标和关闭自动清理 |
| F2 | 适配器已接入 | 固定适配器可生成确定动作、证据和幂等 Undo |
| F3 | 环境预检通过 | 集群、内核、权限、组件、目标与对照均满足执行条件 |
| F4 | 真实注入通过 | 目标出现预期故障，对照不受影响 |
| F5 | 清理恢复通过 | Undo 成功、基线恢复、无临时资源残留 |
| F6 | Agent 自愈闭环通过 | 诊断、审批、原交易 Undo、自愈执行和恢复验证均可审计 |

当前 18 个场景均达到 F2；其中 17 个达到 F5。DCGM 场景达到 F2，运行时 F3
因隔离 GPU 容量不足而阻塞。18 个场景的事务式自愈合同已通过自动化测试，一条远端代表链路
已通过真实页面验证；但这仅证明“已恢复交易的原 Undo 可追溯、可幂等重放”。F6 仍需要
在故障仍活跃的状态下单独验收，不能用适配器自动 Undo 或事后校验代替。

## 3. 统一安全边界

1. 只允许选择服务端登记的 `scenario_id`，请求体不能携带 shell、脚本、kubectl、
   GDB 指令或 BPF 源码。
2. 所有真实实验要求双确认，并在隔离 canary、一次性 Namespace、一次性进程、
   单 cgroup、单 GPU 或一次性块设备上执行。
3. 目标与对照必须同时观测；对照出现同类影响时判定失败并进入清理。
4. 原值、原工件哈希、目标 UID/PID/cgroup、设备映射或 GPU 身份必须在写入前捕获。
5. 每个适配器强制有限时长、有限命中和固定清理范围，不允许调用方扩大作用域。
6. 成功条件统一为：基线成立、目标故障成立、对照正常、Undo 成功、恢复成立、残留为零。
7. GDB、ELF、seccomp、eBPF、cgroup、DCGM 和 dm-flakey 仅允许在专用验证节点执行。
8. DCGM 只模拟 DCGM 缓存字段，不表示制造了物理 PCIe 或 GPU 硬件故障。
9. dm-flakey 只操作动态创建的一次性 loop-backed 设备，禁止使用业务 PVC、模型权重或
   唯一 checkpoint。

## 4. 逐项远端验证结果

### 4.1 请求、系统调用和运行时

| 场景 | 实际验证证据 | Undo/恢复 | 等级 |
|---|---|---|---|
| `precise-http-header-delay` | 精确 `GET /healthz` 和请求头命中；基线 34 ms，500 ms 配置实际 537 ms，对照 33 ms | 删除 HTTPChaos 后恢复 31 ms，Pod 未重建 | F5 |
| `precise-strace-openat-error` | 单 PID 的一次 `openat` 返回 `EACCES`，trace 标记 `INJECTED` | tracer 退出，后续迭代恢复正常，Namespace 删除 | F5 |
| `precise-seccomp-openat-errno` | 精确路径第 3 次命中，注入 1 次 errno 13；对照失败 0 | 无过滤器恢复，后续目标调用正常 | F5 |
| `precise-ebpf-connect-denial` | 单 cgroup 到目标 IP:18080 失败 3 次；对照端口失败 0 | 达到上限后目标恢复，BPF detach，Namespace 删除 | F5 |
| `precise-jvm-method-latency` | `AgenticSREBytecodeProbe.stableOperation` 从 0 ms 增至 2020 ms | 规则停止后恢复 0 ms，Pod 未重建 | F5 |

HTTP 场景不再声明适配器未实现的随机采样。隔离由精确路由、方法、实验请求头和
独立 canary 共同保证；所有匹配实验请求均注入 500 ms 延迟。

### 4.2 Native 函数

| 场景 | 实际验证证据 | Undo/恢复 | 等级 |
|---|---|---|---|
| `precise-gdb-function-return` | `stable_operation` 单次返回 `-88` | detach 后返回序列恢复，进程未重建 | F5 |
| `precise-elf-function-return` | 第 3 次调用返回 `-77`，注入 1 次 | 后续返回正常，工件哈希留证 | F5 |
| `precise-elf-function-errno` | 第 4 次调用返回 `-1` 且 errno=5 | 后续 result/errno 恢复 | F5 |
| `precise-elf-function-delay` | 第 5 次调用配置 350 ms，实际 361 ms | 后续恢复至 10 ms | F5 |

Native 适配器固定 ABI、符号和构建工件，并记录探针与插桩库 SHA-256。只匹配函数名
而没有 ABI/工件校验不能通过预检。

### 4.3 平台原生故障

| 场景 | 实际验证证据 | Undo/恢复 | 等级 |
|---|---|---|---|
| `precise-network-loss` | 目标连接失败 4 次，对照失败 0 | 删除规则后恢复成功 | F5 |
| `precise-dns-error` | 精确 FQDN 解析失败，对照域名正常 | 删除规则后目标解析恢复 | F5 |
| `precise-container-cpu-stress` | 目标 CPU 时间 46951→2609583 μs，对照仅 40112→59122 μs | 目标恢复至 69568 μs | F5 |
| `precise-path-io-latency` | 目标路径 2→2353 ms，对照路径 2→7 ms | 目标恢复至 2 ms | F5 |
| `precise-container-time-offset` | 单容器时差 0→300 s | 时差恢复为 0 s | F5 |

### 4.4 应用、资源、GPU 和存储

| 场景 | 实际验证证据 | Undo/恢复 | 等级 |
|---|---|---|---|
| `precise-application-failpoint-error` | 精确 request ID 返回登记错误，命中上限 1；对照和耗尽后请求正常 | token 禁用与过期后均恢复 | F5 |
| `precise-cgroup-v2-cpu-quota` | `cpu.max` 100000/100000→20000/100000，节流计数 1→50；对照不变 | 原值精确恢复，Namespace 删除 | F5 |
| `precise-dcgm-pcie-replay` | 预检发现节点可分配 3、已预留 3，无隔离 GPU | 未创建 Pod、未注入字段 | F2 / F3 阻塞 |
| `precise-dm-flakey-write-error` | 动态 loop 设备出现受控写错误，对照写入正常 | mapper 移除，恢复前后 64 MiB SHA-256 完全一致 | F5 |

GPU 容量不足是安全阻塞，不是适配器失败。只有获得独占空闲 GPU 后，才能执行 field
202 注入、缓存读取、基线覆盖和清理验证。

## 5. 事务执行与 API

统一事务阶段为：

```text
preflight -> baseline -> inject -> effect_verify
          -> undo -> recovery_verify -> residue_check
```

主要 API：

- `GET /api/faultlab/precision-scenarios`：场景、机制和适配器状态；
- `POST /api/faultlab/precision/plan/{scenario_id}`：生成受控合同；
- `POST /api/faultlab/precision/preflight/{scenario_id}`：只读环境预检；
- `POST /api/faultlab/precision/experiment/{scenario_id}`：预演或真实七阶段闭环；
- `POST /api/faultlab/precision/heal/{scenario_id}`：只基于成功真实实验的持久化编号生成四阶段合同，幂等重放原 Undo 并验证残留；
- `POST /api/faultlab/precision/cleanup/{scenario_id}`：仅清理适配器登记的固定资源；
- `POST /api/faultlab/precision/verify/{scenario_id}`：残留检查。

真实实验示例：

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"dry_run":false,"confirm":true,"experiment_ack":true,"namespace":"agenticsre-faultlab"}' \
  http://127.0.0.1:8080/api/faultlab/precision/experiment/precise-network-loss
```

执行前应先调用 plan 与 preflight。任何预检 blocker、目标漂移、效果证据缺失、
Undo 失败或残留检查失败都会阻止成功状态。

自愈校验必须传入原真实实验的 `fault_run_id`。服务端要求该记录状态为
`completed`、适配器结果为 `succeeded`、`validation=PASS`、`cleanup_complete=true`，并且场景、集群、
Namespace、目标、逐目标作用域和 `agenticsre.precision.cleanup` 必须完全匹配。返回
`already_recovered` 表示原交易已恢复且幂等重放验证通过，不表示接口发现并修复了一个仍活跃故障。

## 6. 前端验收

细粒度能力集成在“通算故障实验”的“细粒度注入”页签，不另建一套孤立控制台。
每个场景提供：

- 合同；
- 环境预检；
- 预演；
- 真实闭环；
- 自愈校验；
- 强制清理；
- 残留检查。

远端浏览器验收确认 18 张场景卡、126 个动作按钮、合同就绪、环境预检就绪、
七阶段预演和残留检查均正常；一次性应用 failpoint 还通过页面完成真实闭环，返回
`succeeded` 和 `cleanup_complete=true`。随后页面使用该次持久化记录执行“自愈校验”，
四阶段合同均为成功状态，原 `agenticsre.precision.cleanup` 幂等重放后返回
`already_recovered` 和 `recovery.passed=true`。真实故障执行仍由确认复选框和浏览器二次确认共同保护。

## 7. 复现与证据索引

- 场景目录：`configs/precision_fault_scenarios.yaml`
- 合同校验：`skill_runtime/precision_injection.py`
- 固定适配器：`skill_runtime/precision_execution.py`
- 探针、清单与工件：`deploy/precision-fault-injection/`
- 真实验证脚本：`scripts/validate_*precision*.*`、
  `scripts/validate_platform_native_faults.sh`、
  `scripts/validate_native_function_scenario.sh`
- API：`web_app/app.py`
- 前端验证：`scripts/validate_faultlab_frontend_cdp.py`
- 机器可读结果：`data/validation/precision_fault_injection_remote_20260730.json`
- 全流程记录：`docs/validation/fault_injection_remote_e2e_20260730.md`

只有 F4 才能写“真实注入通过”，只有 F5 才能写“已恢复且无残留”，只有 F6
才能写“Agent 自愈闭环通过”。合同测试、HTTP 200、按钮可见或组件已安装都不能
替代这些结论。
