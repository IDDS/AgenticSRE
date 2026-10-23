# AgenticSRE 受控故障注入与事务式恢复

## 边界

本能力只读取 AgenticSRE 自身的场景目录、Skill、固定执行适配器和运行配置。调用方
只能选择已登记的 `scenario_id`，不能提交命令、脚本、kubectl 文本、BPF 程序、
GDB 指令或临时执行器地址。

普通 Kubernetes 场景继续使用通用受控执行链路；细粒度场景使用
`skill_runtime/precision_execution.py` 中的一对一固定适配器。两类能力在同一个
“通算故障实验”中集成，但使用不同的合同与验收口径。

## 当前完成度

`configs/precision_fault_scenarios.yaml` 登记 18 个场景、12 种机制：

| 机制 | 粒度 | 场景 |
|---|---|---|
| `request` | 精确路由、方法和请求头 | 请求延迟 |
| `strace` | 单进程、单 syscall、有限命中 | `openat` errno |
| `seccomp` | 单进程、精确路径 | user-notification errno |
| `ebpf` | 单 cgroup、目标 IP 和端口 | 有限次连接拒绝 |
| `bytecode` | 单 JVM class/method | 方法延迟 |
| `gdb` | 单进程、ABI 和符号 | 函数返回值覆盖 |
| `binary` | 固定 ELF 插桩工件和符号 | 返回值、errno、延迟 |
| `platform_native` | 单 Pod/容器/路径/目标 | 网络、DNS、CPU、I/O、时钟 |
| `failpoint` | 单 failpoint 和请求 ID | 应用定向错误 |
| `cgroup_v2` | 单非根 cgroup 和控制器 | CPU 配额 |
| `dcgm` | 单 GPU 和允许字段 | GPU 指标异常模拟 |
| `dm_flakey` | 单一次性 loop-backed 卷 | 受控写错误 |

截至 2026-07-30：

- 18/18 个固定适配器已实现；
- 17/18 个场景完成远端真实注入、对照隔离、Undo、恢复和残留检查；
- DCGM 场景因 3/3 GPU 已被现有任务预留而安全阻塞，未创建实验 Pod；
- 前端“细粒度注入”页签通过远端浏览器验证；
- 已接入基于服务端真实实验记录的四阶段事务式自愈校验，只能幂等重放原 Undo；
- 独立的“故障仍活跃时由 Agent 决策并执行恢复”F6 验收尚未完成，不能把适配器自动 Undo 或事后幂等校验表述为 F6 通过。

逐项数据见
[细粒度故障注入实施状态与验收规范](testbeds/precision-fault-injection-status.md)
和 `data/validation/precision_fault_injection_remote_20260730.json`。

## 统一事务

细粒度适配器必须完成：

```text
preflight -> baseline -> inject -> effect_verify
          -> undo -> recovery_verify -> residue_check
```

成功判定必须同时满足：

1. 目标身份和环境预检通过；
2. 基线可观测；
3. 目标出现登记的故障效果；
4. 对照组不出现同类影响；
5. Undo 恢复捕获的原值或删除精确登记的故障资源；
6. 恢复指标回到阈值；
7. Namespace、CR、进程、BPF、断点、cgroup、mapper 或临时文件无残留；
8. 适配器输出 `validation=PASS`。

任一条件不满足时返回 blocked/failed，不允许只打印“执行成功”。

## 审批与执行

- Dry-run 不需要审批，不创建故障；
- 真实实验必须同时提交 `confirm=true` 和 `experiment_ack=true`；
- 前端还要求勾选“确认真实执行”并通过二次确认；
- API 根据服务端固定映射选择适配器，不读取调用方提供的命令；
- 清理 API 只处理适配器登记的固定 Namespace 或资源；
- DCGM 和块设备场景必须有独占资源，容量不足时保持 blocked。

## API

- `GET /api/faultlab/precision-scenarios`
- `POST /api/faultlab/precision/plan/{scenario_id}`
- `POST /api/faultlab/precision/preflight/{scenario_id}`
- `POST /api/faultlab/precision/experiment/{scenario_id}`
- `POST /api/faultlab/precision/heal/{scenario_id}`
- `POST /api/faultlab/precision/cleanup/{scenario_id}`
- `POST /api/faultlab/precision/verify/{scenario_id}`

真实闭环示例：

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"dry_run":false,"confirm":true,"experiment_ack":true}' \
  http://127.0.0.1:8080/api/faultlab/precision/experiment/precise-application-failpoint-error
```

## 前端

细粒度注入不是独立系统，而是“通算故障实验”的第四个页签。它与单次实验、连续实验
和场景覆盖共享导航与目标管理，并为每个细粒度场景提供合同、环境预检、预演、
真实闭环、自愈校验、强制清理和残留检查。“自愈校验”要求先有一次成功真实闭环，
页面仅传递该次实验的持久化编号，服务端再次比对全部作用域和 Undo 合同。

## 验证

```bash
pytest -q tests/test_guarded_fault_recovery.py tests/e2e/test_frontend_refresh_behavior.py
node --check web_app/static/js/app.js
python3 scripts/validate_faultlab_frontend_cdp.py \
  --cdp http://127.0.0.1:9223 \
  --url http://127.0.0.1:18084 \
  --real-safe-scenario precise-application-failpoint-error
```

真实场景应使用对应的 `scripts/validate_*precision*.*`，并保存 before、fault、
control、recovery 和 cleanup 证据。完整命令和逐项结果见远端验收文档。
