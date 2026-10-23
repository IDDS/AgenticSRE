# AgenticSRE 故障注入全流程远端验收

## 验收结论

2026-07-30 在远端 Kubernetes 测试集群完成两层验收。17 个常规场景均完成真实注入、故障效果确认、基于服务端注入记录的事务式自愈规划、精确清理和基线恢复。18 个细粒度场景覆盖 12 种机制，18 个固定适配器全部接入；其中 17 个完成目标故障、对照隔离、Undo、恢复和残留检查，DCGM 场景因隔离 GPU 容量为 0 而安全阻塞。后续补充的细粒度事务式自愈合同已对 18/18 场景通过自动化测试，并使用一次性应用 failpoint 在远端页面验证了原 Undo 幂等重放和恢复校验。没有场景在效果证据缺失时被记为通过。

## 范围与隔离

- 主实验 Namespace：`test-social-network`。
- 跨 Namespace 可观测性实验：`agenticsre-faultlab-observe`。
- 远端验证服务使用独立进程和独立数据目录；既有 AgenticSRE 服务未重启。
- 实验只操作随 `deploy/faultlab/remote-e2e-workloads.yaml` 创建的工作负载，不修改集群既有业务 Namespace。
- 每个场景串行执行；下一场景开始前必须满足 10 个业务 Deployment 和 1 个可观测性 Deployment 全部 Ready、10 个 Service 全部存在 Endpoint、辅助 Pod 和 CPU 故障进程均已清理。

## 常规场景真实结果

| 类别 | 场景 | 真实效果证据 | 结果 |
| --- | --- | --- | --- |
| 资源 | `sn-cpu-stress` | CPU 辅助 Pod 创建并运行 | 通过 |
| 资源 | `sn-mem-pressure` | 内存辅助 Pod 创建并执行 | 通过 |
| 应用 | `sn-crashloop` | Pod 重启或进入 CrashLoopBackOff | 通过 |
| 网络/入口 | `sn-network-delay` | 入口 Deployment 副本降为 0 | 通过 |
| 可用性 | `sn-svc-scale-down` | 服务副本和 Endpoint 降为 0 | 通过 |
| 数据库 | `sn-db-slow` | 数据库依赖副本降为 0 | 通过 |
| 缓存 | `sn-redis-down` | 缓存依赖副本降为 0 | 通过 |
| 资源 | `sn-compose-cpu` | 目标容器 CPU 故障进程存活 | 通过 |
| 缓存 | `sn-memcached-down` | 缓存依赖副本降为 0 | 通过 |
| 复合 | `sn-multi-fault` | 缓存下线与另一容器 CPU 故障同时成立 | 通过 |
| 网络 | `sn-network-partition` | 指定依赖副本降为 0 | 通过 |
| 分阶段级联 | `sn-staged-write-cascade` | 第一阶段依赖下线，45 秒后第二阶段 CPU 故障触发 | 通过 |
| 灰度 | `sn-gray-url-brownout` | 指定容器 CPU 故障进程存活 | 通过 |
| 可观测盲点 | `sn-observability-blindspot` | 独立可观测 Deployment 下线，业务容器 CPU 故障同时成立 | 通过 |
| 干扰信号 | `sn-stale-signal-distractor` | 干扰 CrashLoop 与业务 CPU 故障同时成立 | 通过 |
| 瞬态 | `sn-transient-cache-flap` | 缓存副本短暂降为 0，清理取消延迟恢复任务 | 通过 |
| 调度容量 | `sn-scaleout-capacity-failure` | 超额资源 Pod 保持 Pending，目标容器 CPU 故障同时成立 | 通过 |

完整首轮记录为 `data/validation/remote_faultlab_e2e_initial.json`。首轮 16 个场景通过，调度容量场景暴露兼容性问题；修复后的单场景复验记录为 `data/validation/remote_faultlab_e2e_retry4.json`，结果通过。两份证据合并构成 17/17 最终结论，且两次验收结束时均记录了全量基线状态。

## 细粒度场景

### 验收口径

每个场景按同一交易执行：`preflight -> baseline -> inject -> effect_verify -> undo -> recovery_verify -> residue_check`。只有目标故障成立、对照不受影响、Undo 成功、恢复成立、清理完成且适配器输出 `validation=PASS` 时才通过。

### 真实结果

| 类别 | 场景 | 故障效果 | 恢复与清理 | 结果 |
|---|---|---|---|---|
| 请求 | `precise-http-header-delay` | 500 ms 配置实际 537 ms，对照 33 ms | 恢复 31 ms，Pod 未重建 | 通过 |
| syscall | `precise-strace-openat-error` | 单 PID 的一次 `openat` 返回 `EACCES` | 后续迭代正常，tracer 与 Namespace 清理 | 通过 |
| syscall | `precise-seccomp-openat-errno` | 精确路径第 3 次命中，注入 errno 13 一次 | 无过滤器恢复，对照失败 0 | 通过 |
| eBPF | `precise-ebpf-connect-denial` | 单 cgroup 到目标端口失败 3 次 | 达上限后恢复，程序 detach | 通过 |
| JVM | `precise-jvm-method-latency` | 精确方法 0→2020 ms | 恢复 0 ms，Pod 未重建 | 通过 |
| GDB | `precise-gdb-function-return` | 指定符号单次返回 `-88` | detach 后返回正常 | 通过 |
| ELF | `precise-elf-function-return` | 第 3 次调用返回 `-77` | 后续调用正常 | 通过 |
| ELF | `precise-elf-function-errno` | 第 4 次调用返回 `-1`/errno 5 | result/errno 恢复 | 通过 |
| ELF | `precise-elf-function-delay` | 第 5 次调用配置 350 ms，实际 361 ms | 恢复 10 ms | 通过 |
| 网络 | `precise-network-loss` | 目标失败 4 次，对照失败 0 | 网络恢复，规则清理 | 通过 |
| DNS | `precise-dns-error` | 精确域名失败，对照域名正常 | 目标解析恢复 | 通过 |
| CPU | `precise-container-cpu-stress` | 目标 CPU 时间显著增长，对照变化有限 | 目标回落并清理 | 通过 |
| I/O | `precise-path-io-latency` | 目标路径 2→2353 ms，对照 2→7 ms | 目标恢复 2 ms | 通过 |
| 时间 | `precise-container-time-offset` | 单容器时差 0→300 s | 恢复 0 s | 通过 |
| 应用 | `precise-application-failpoint-error` | 精确 request ID 命中一次登记错误 | 禁用/过期后恢复，对照正常 | 通过 |
| cgroup | `precise-cgroup-v2-cpu-quota` | `cpu.max` 降至 20000/100000，节流 1→50 | 原值精确恢复 | 通过 |
| GPU | `precise-dcgm-pcie-replay` | 可分配 3、已预留 3，无隔离 GPU | 未创建实验资源 | 资源阻塞 |
| 存储 | `precise-dm-flakey-write-error` | 一次性 mapper 出现写错误，对照写入正常 | mapper 删除，64 MiB 哈希一致 | 通过 |

机器可读证据保存在 `data/validation/precision_fault_injection_remote_20260730.json`。DCGM 的阻塞不算失败，也不算通过；待有独占 GPU 后必须重新执行真实注入。

## 前端验证

通过一次性 Chrome 调试实例进入“通算故障实验”的“细粒度注入”页签，确认 18 张场景卡、126 个动作按钮和 7 类动作标签完整。随后实际点击合同、环境预检、预演和残留检查，验证适配器可执行、预检就绪、预演包含 7 个交易阶段且残留检查通过。最后勾选真实执行确认，对一次性应用 failpoint 探针点击“真实闭环”，页面返回 `succeeded` 且 `cleanup_complete=true`。页面随后点击“自愈校验”，服务端仅根据已持久化的 `fault_run_id` 生成四阶段合同：`detect=matched`、`diagnose=ok`、`heal=ok`、`verify=ok`，原 `agenticsre.precision.cleanup` 幂等重放后返回 `already_recovered` 和 `recovery.passed=true`。这证明前端、API、固定适配器、效果验证、Undo、恢复与事后自愈校验链路真实贯通。细粒度能力与普通通算实验共享入口和目标管理，没有拆成孤立系统。

需要保留口径边界：该“自愈校验”发生在适配器已自动 Undo 之后，证明的是原交易审批、作用域和 Undo 可追溯且可安全重放，不能替代“故障仍活跃时由 Agent 完成恢复”的 F6 逐项验收。

## 验收中发现并修复的问题

- 交易目标新增逐目标 `target_scopes`，跨 Namespace 注入、Undo 和自愈必须完全一致。
- 注入或清理任一命令失败时，API 与持久化记录状态改为 `failed`，不再误报 `executed`。
- CPU 和延迟任务隔离标准输入输出，并把后台 PID 纳入 Undo，避免 API 超时或清理后再次触发。
- 调度容量 Pod 使用兼容旧版 `kubectl` 的资源覆盖，并禁止将该 Pod 固定到节点，确保由调度器产生 Pending 证据。
- 可观测盲点场景改用 AgenticSRE 专用 Namespace，不再影响集群既有监控工作负载。
- 细粒度真实执行由服务端固定映射到 18 个适配器，并要求双确认和 `validation=PASS`。
- cgroup 证据修正为注入前原值，避免把注入后 readback 误标为基线。
- dm-flakey 恢复校验统一为 64 MiB 全盘 SHA-256，避免基线与恢复哈希口径不一致。
- GPU 容量统计兼容 Kubernetes JSON 中的字符串资源值，并在 3/3 GPU 已预留时阻止实验。
- HTTP 请求级验证增加代理预热，稳定观测 500 ms 配置实际 537 ms；删除未实现的随机采样声明。
- 前端把细粒度能力集成为“通算故障实验”的第四个页签，并保留普通场景快捷跳转。
- 新增细粒度自愈校验接口；只接受成功真实实验的精确持久化编号，原记录的场景、集群、Namespace、目标、逐目标作用域或 Undo 任一不一致即阻断。
- 事务式自愈 Skill 扩展到细粒度 `agenticsre.precision.cleanup`，并支持已登记的单 GPU 和一次性卷目标；仍禁止调用方提交命令或扩大作用域。

## 回归命令

```bash
python3 -B -m pytest tests -q
python3 -B -m pytest tests/test_guarded_fault_recovery.py -q
python3 -B -m pytest tests/e2e/test_frontend_refresh_behavior.py -q
node --check web_app/static/js/app.js
python3 scripts/validate_faultlab_frontend_cdp.py \
  --cdp http://127.0.0.1:9223 \
  --url http://127.0.0.1:18084 \
  --real-safe-scenario precise-application-failpoint-error
git diff --check
```
