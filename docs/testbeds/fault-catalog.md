# 被测对象详细故障目录

本文统一维护通算、vLLM、Agentic AI 和 Agentic RL 的故障类型。它是实验设计与
验收目录，不表示每个条目都能在任意环境直接执行。

## 1. 状态和风险定义

| 标记 | 含义 |
|---|---|
| `A` | 当前仓库存在对应实现或场景；仍须 dry-run、目标核对和预检 |
| `A*` | 当前仓库有命令入口，但存在已知语义缺口，不能按理想故障名称出具结论 |
| `B` | 有通用实现思路，需要目标适配、额外组件或清单 |
| `C` | 只允许专用实验集群，必须有快照/备份、独占资源和人工审批 |

`A` 只表示仓库有对应命令路径或常规场景，不等于故障语义已在目标业务上生效。
截至 2026-07-30，17 个常规场景已在专用轻量夹具完成真实注入和恢复；18 个细粒度
场景均已接入固定适配器，其中 17 个完成远端真实注入、对照隔离、Undo、恢复和残留
检查，DCGM 场景因没有隔离 GPU 而安全阻塞。完整上游应用和智算硬故障仍须分别验收，证据见
[远端验收记录](../validation/fault_injection_remote_e2e_20260730.md)。

### 1.1 覆盖总览

| 被测对象/机制 | 本目录条目 | 当前可验证范围 |
|---|---:|---|
| Social Network | 17 个现有场景 + 8 个建议扩展 | 17 个已在轻量夹具实注；完整应用需重新验收业务证据 |
| Train Ticket | 15 个故障映射 + 2 个查询链实验 | 两项定向网络故障已完成实际查询链影响与恢复；完整事务链待验收 |
| 细粒度通用合同 | 18 个合同、12 种机制 | 18 个适配器已接入；17 个 F5 通过，1 个 GPU 容量阻塞 |
| vLLM | 40 类：软件 13、网络 9、GPU/驱动 8、OS/资源 10 | 有执行器入口；按 6.5 节逐类验证语义 |
| Agentic AI | 18 类 | 参考故障模型，待选定运行时、工具和沙箱后验收 |
| Agentic RL | 21 类 | 参考故障模型，待选定训练拓扑和 checkpoint 后验收 |

| 风险 | 默认爆炸半径 | 示例 |
|---|---|---|
| L1 低 | 单个临时 Pod、单次请求、最长 30 秒 | 删除故障负载、短时延迟 |
| L2 中 | 单 Deployment/Service、最长 2 分钟 | 单服务 scale 0、Endpoint 为空 |
| L3 高 | 节点、数据层、GPU/通信或多目标 | 磁盘、NCCL、数据库、复合故障 |
| L4 数据/硬件危险 | 权重/checkpoint/数据库内容或驱动/设备状态 | 权重破坏、驱动重置、数据污染 |

真实注入的最低合同字段：`experiment_id`、集群、namespace、目标实体、容器、
基线窗口、注入命令、持续时间、最大影响实体数、停止条件、undo、恢复查询和
验收阈值。任一字段无法确定时，只能 dry-run。

## 2. 统一恢复判据

所有场景至少检查：

1. 注入资源和后台进程已经清理，`tc`/`iptables`/clock/GPU lock 等节点状态还原；
2. 工作负载 Ready/Available、Endpoint 和重启数稳定；
3. 连续业务请求成功率、P95/P99 和吞吐回归基线容差；
4. 指标、日志、事件和 Trace 不再产生新的同类错误；
5. Agentic/训练场景还要检查任务成功率或 global step 继续推进；
6. 数据类场景校验校验和、快照或业务一致性，不能只看 Pod Ready。

建议默认容差：成功率不低于基线 99%，P95 不高于基线 1.2 倍，连续 3 个采样窗口
满足；具体阈值应在实验前固定，而不是观察结果后修改。

## 3. DeathStarBench Social Network

### 3.1 当前仓库场景

下表映射 `eval/fault_scenarios.yaml`。这些命令依赖固定 namespace 和资源名；部署
完整上游后仍需验证容器内是否存在 `sh`、`seq`、`xargs` 等命令。

| ID | 故障 | 目标/注入 | 主要证据 | Undo/恢复 | 状态/风险 |
|---|---|---|---|---|---|
| `sn-cpu-stress` | namespace CPU 压力 | 临时 `cpu-stress` Pod | node/pod CPU、throttling、入口 P95 | 删除压力 Pod；P95 回归 | A/L1 |
| `sn-mem-pressure` | 内存压力 | tmpfs 写入的临时 Pod | memory、OOM/137、时间线失败 | 删除压力 Pod；无新 OOM | A/L1 |
| `sn-crashloop` | CrashLoop 噪声/应用崩溃 | 独立 `crash-test` Pod | BackOff Event、restart/log | 删除测试 Pod | A/L1 |
| `sn-network-delay` | 入口副本耗尽 | `nginx-thrift` scale 0 | Endpoint 为空、入口 timeout | 恢复原副本数 | A/L2 |
| `sn-svc-scale-down` | 时间线不可用 | `home-timeline-service` scale 0 | 503、Endpoint、Trace 断点 | scale 1；读事务恢复 | A/L2 |
| `sn-db-slow` | 发帖存储依赖中断 | `post-storage-mongodb` scale 0 | 写 P99、连接错误、Trace | scale 1；写入恢复 | A/L3 |
| `sn-redis-down` | 时间线缓存中断 | `home-timeline-redis` scale 0 | 读延迟、connection refused | scale 1；读 P95 回归 | A/L2 |
| `sn-compose-cpu` | 发帖服务 CPU 饱和 | 容器内 CPU 循环 | compose CPU、504、写路径 Trace | kill PID 并删 pidfile | A/L2 |
| `sn-memcached-down` | 用户缓存中断 | `user-memcached` scale 0 | 登录/用户服务延迟、缓存错误 | scale 1；登录恢复 | A/L2 |
| `sn-multi-fault` | 缓存 + CPU 复合故障 | Redis scale 0 + text CPU | 两个独立信号、总体成功率 | 逆序恢复两目标 | A/L3 |
| `sn-network-partition` | URL 路径局部失败 | `url-shorten-service` scale 0 | 含 URL 事务失败、普通事务正常 | scale 1；灰度路径恢复 | A/L2 |
| `sn-staged-write-cascade` | 分阶段级联 | MongoDB 中断，45 秒后 CPU | 两波时序、写路径 Trace | 先停 CPU，再恢复 DB | A/L3 |
| `sn-gray-url-brownout` | URL 路径灰度退化 | `url-shorten-service` CPU | 分事务成功率、P95、CPU | kill 精确 PID | A/L2 |
| `sn-observability-blindspot` | 业务故障 + 观测盲区 | kube-state-metrics 中断 + CPU | target down 与业务错误并存 | 先业务，再采集器 | A/L3 |
| `sn-stale-signal-distractor` | 陈旧/干扰信号 | 无关 CrashLoop + 真 CPU 故障 | Event 时间、业务路径 Trace | 清理两类故障 | A/L2 |
| `sn-transient-cache-flap` | 短暂缓存闪断 | Redis 75 秒后自动恢复 | 短时连接错、滞后告警 | 停后台任务并 scale 1 | A/L2 |
| `sn-scaleout-capacity-failure` | 扩容调度失败 | 不可满足资源请求 + CPU | Pending/Event、现有副本过载 | 删除 Pending Pod、停 CPU | A/L2 |

`sn-network-delay` 和 `sn-db-slow` 的当前实现分别是副本耗尽和数据库不可用，不是
真实 `tc netem delay` 或“慢查询”。实验报告应按实际动作描述，不能只引用场景名。

### 3.2 建议扩展

| 故障 | 精确目标与方式 | 证据 | 恢复 | 状态/风险 |
|---|---|---|---|---|
| 单向网络延迟/丢包 | Chaos Mesh/NetworkChaos 限定源/目的 Pod | dependency span、重传、P99 | 删除 Chaos CR | B/L2 |
| 精确 DNS 失败 | 只匹配目标 FQDN 的 DNSChaos | DNS error、Endpoint 正常但调用失败 | 删除 DNSChaos | B/L2 |
| MongoDB 写延迟 | 数据库 failpoint 或代理延迟，命中发帖库 | 写时延、slow log、Trace | 关闭 failpoint | B/L3 |
| Redis 延迟/只读 | 代理或测试副本配置 | cache timeout、fallback、读写差异 | 恢复配置/切回副本 | B/L3 |
| 配置错误 | 临时 ConfigMap revision + rollout | 启动日志、配置 hash、错误率 | rollout undo | B/L2 |
| 镜像拉取失败 | 单 Deployment 改为不存在的固定 tag | ImagePullBackOff、Event | rollout undo | B/L2 |
| 节点不可调度/驱逐 | cordon + 单目标 Pod 驱逐 | Pending、调度 Event、可用副本 | uncordon/恢复副本 | C/L3 |
| 磁盘/PVC 故障 | 仅 disposable volume 的 I/O error/latency | fs error、DB/应用日志、P99 | 移除故障映射、校验数据 | C/L4 |

## 4. Train Ticket

Train Ticket 不能直接执行 `sn-*` 命令。应使用同一故障模式，但把目标映射到实际
服务，并保留原副本、ConfigMap 和镜像 revision。

| 故障域 | 推荐目标/方式 | 预期证据 | 最小恢复 | 状态/风险 |
|---|---|---|---|---|
| 入口 Pod 崩溃 | 删除一个 `ts-ui-dashboard` 或 gateway Pod | restart、Endpoint 短抖动、事务失败 | Deployment 自动补副本 | B/L1 |
| gateway scale 0 | `ts-gateway-service` | 全事务 5xx/timeout、Endpoint 空 | 恢复原副本 | B/L2 |
| 预订服务中断 | `ts-preserve-service` scale 0 | 查询正常、预订失败、Trace 断点 | 恢复原副本 | B/L2 |
| 订单服务延迟 | `ts-order-service` 定向延迟 | 订单步骤 P99、上游等待 | 删除 Chaos CR | B/L2 |
| 服务发现异常 | 一个测试实例使用错误 Nacos 地址 | 注册/发现日志、部分实例不可用 | rollout undo | B/L2 |
| Nacos leader/副本故障 | 删除单个 Nacos Pod | leader 变化、注册延迟 | StatefulSet 重建并验证成员 | B/L3 |
| RabbitMQ 中断 | 单副本 Pod 删除或 Service 选择器错误 | queue、连接错误、异步事务失败 | 恢复 Pod/selector | B/L3 |
| MySQL leader 切换 | 业务测试库 leader Pod | SQL error、切换时间、事务失败 | 集群恢复 + 一致性校验 | C/L3 |
| MySQL 慢查询 | 测试库延迟/锁等待 | DB slow log、连接池、事务 P99 | 释放锁/关闭延迟 | C/L3 |
| 连接池耗尽 | 单业务服务受控高并发 | active/wait、timeout、线程数 | 停止负载 | B/L2 |
| 配置错误 | 单服务 ConfigMap/Secret 测试副本 | 启动日志、readiness、配置 hash | rollout undo/恢复 Secret | C/L3 |
| Trace 采集盲区 | 单 agent/exporter 或 collector 中断 | 业务正常、Trace 缺失、target down | 恢复采集器 | B/L2 |
| Prometheus 盲区 | 单 scrape target/采集器短时中断 | target down、指标断点 | 恢复采集器 | B/L2 |
| PVC I/O 延迟/满盘 | 仅一次性 MySQL/PVC 副本 | fs、DB、事务和 Event | 移除故障并校验数据 | C/L4 |
| 复合故障 | 预订延迟 + 无关 Pod crash | Trace 与 Event 冲突 | 逆序最小恢复 | B/L3 |

Train Ticket 的恢复必须跑完整“登录→查询→预订→订单”事务；首页 200 不能作为
RabbitMQ、数据库和后端服务的恢复判据。

2026-10-23 在现有 Train Ticket 上另行完成了下列**查询链局部验收**，不升级上述
完整事务链验收状态，也不表示模型诊断通过：

| 实验 | 实际方式 | 业务影响与恢复 | 证据 |
|---|---|---|---|
| `tt-query-seat-delay` | 从固定 travel Pod 向 seat Service ClusterIP 注入 250 ms 延迟 | 同轮 P95 279→785 ms；撤销后 30 笔全成功且延迟回归 | [查询链实注记录](../validation/train-ticket-business-injection-20261023.md) |
| `tt-query-seat-loss` | 相同精确依赖路径注入 100% 丢包 | 6/6 查询约 10 秒客户端超时、站点对照正常；撤销后 30 笔全成功且延迟回归 | [查询链实注记录](../validation/train-ticket-business-injection-20261023.md) |

首轮按目标 Pod IP 匹配虽然控制器返回已注入，但业务没有达到退化阈值，已保留为
失败实验，不计入有效覆盖。正式执行前应核对实际连接目的地址、当前实体 UID 和
同轮健康基线。模型接口当时返回 HTTP 402，自动根因诊断与学习/修复闭环仍未验收。

## 5. 细粒度通用故障合同

`configs/precision_fault_scenarios.yaml` 当前定义 18 个细粒度合同，默认
`duration_s=15`、`max_injections=1`、自动清理、单实体和最长 30 秒。声明随机请求
采样的场景还受 5% 上限约束；当前 HTTP 场景使用精确实验请求头，不声明随机采样。
18 个固定适配器均已实现，17 个场景完成 F5 远端验收，DCGM 因隔离 GPU 容量不足
停在 F2/F3 阻塞。逐项完成等级、适配器前提和验收要求见
[细粒度故障注入实施状态与验收规范](precision-fault-injection-status.md)。

| ID | 粒度 | 必要前提 | 观测/恢复 | 状态/风险 |
|---|---|---|---|---|
| `precise-http-header-delay` | 指定 HTTP header 的单请求延迟 | 代理/网格支持 header match | Trace 延迟；删规则 | B/L1 |
| `precise-strace-openat-error` | 进程 syscall 观测/错误 | ptrace 权限、PID/路径明确 | syscall 证据；detach | B/L2 |
| `precise-seccomp-openat-errno` | 精确路径 `openat` errno | 预装 seccomp profile，不能运行中热改 | errno/log；恢复 profile | B/L3 |
| `precise-ebpf-connect-denial` | 目的端口连接拒绝 | 内核/eBPF allowlist | connect error；detach program | B/L3 |
| `precise-jvm-method-latency` | 指定 Java 方法延迟 | 类/方法签名、bytecode agent | method span；卸载规则 | B/L2 |
| `precise-gdb-function-return` | native 函数返回值覆盖 | 符号、PID、gdb 权限 | 函数证据；detach | C/L3 |
| `precise-elf-function-return` | ELF 函数返回覆盖 | 已审核 instrumentation artifact | 函数/业务结果；卸载 | C/L3 |
| `precise-elf-function-errno` | ELF 函数 errno | 同上 | errno/log；卸载 | C/L3 |
| `precise-elf-function-delay` | ELF 函数延迟 | 同上 | function latency；卸载 | C/L3 |
| `precise-network-loss` | 单 Pod 定向丢包 | Chaos Mesh | packet/Trace；删 CR | B/L2 |
| `precise-dns-error` | 单 DNS 名失败 | DNSChaos | DNS error；删 CR | B/L2 |
| `precise-container-cpu-stress` | 单容器 CPU | Chaos Mesh/stress helper | CPU/throttle；删 CR/进程 | B/L2 |
| `precise-path-io-latency` | 单路径 I/O 延迟 | disposable path/Chaos I/O | I/O latency；删 CR | C/L3 |
| `precise-container-time-offset` | 单容器时间偏移 | TimeChaos、时间隔离 | clock/log/TLS；删 CR | C/L3 |
| `precise-application-failpoint-error` | 指定 request ID、单 hit | 应用内 reviewed failpoint | 业务错误/Trace；disable | B/L1 |
| `precise-cgroup-v2-cpu-quota` | 单 cgroup CPU quota | cgroup v2、原值快照 | throttling；恢复原值 | C/L3 |
| `precise-dcgm-pcie-replay` | 单 GPU 指标模拟 | DCGM injection 能力 | 指标告警；清除模拟 | B/L2 |
| `precise-dm-flakey-write-error` | disposable volume 写错误 | loop/dm-flakey、快照 | I/O error；还原 mapping | C/L4 |

## 6. vLLM

### 6.1 软件和模型层

| fault type | 实际动作/目标 | 主要证据 | Undo/恢复 | 状态/风险 |
|---|---|---|---|---|
| `process_crash` | kill serving 进程，找不到则删一个 Pod | restart、503、throughput 0 | Deployment 自动恢复/rollout | A/L2 |
| `process_hang` | 对 serving PID 发 SIGSTOP | health timeout、无 restart、吞吐 0 | SIGCONT，失败则 rollout | A/L2 |
| `model_load_failure` | 当前修改固定索引的模型参数为不存在路径 | CrashLoop、model not found | 恢复捕获的原参数 | A*/L3 |
| `model_permission_error` | 当前把模型参数改到预设不可用路径 | permission denied、load fail | 恢复捕获的原参数 | A*/L3 |
| `tokenizer_error` | 当前只写临时 tokenizer 覆盖文件 | 必须证明服务读取该文件并报解析错 | 删除覆盖或恢复备份 | A*/L3 |
| `kv_cache_exhaustion` | 独立负载 Pod 发长上下文并发请求 | KV%、waiting、TTFT、GPU memory | 删除精确负载 Pod | A/L2 |
| `scheduler_contention` | 当前从服务容器发固定模型名并发请求 | 必须证明请求有效且 waiting 上升 | 停负载/重建实例 | A*/L2 |
| `request_queue_overflow` | 当前设置 `VLLM_MAX_NUM_SEQS` 环境变量 | 启动参数生效、waiting/timeout | 恢复参数 | A*/L2 |
| `continuous_batch_disruption` | 当前设置 `VLLM_MAX_NUM_BATCHED_TOKENS` | 参数生效、tokens/s、TPOT | 恢复参数 | A*/L2 |
| `attention_backend_mismatch` | 设置 attention backend 环境变量 | 启动日志、kernel error、throughput | rollout undo | A*/L3 |
| `prefill_decode_misclassification` | 设置显存利用率/序列数环境变量 | 参数生效、request marker、kernel/输出 | 恢复参数，重启实例 | A*/L3 |
| `state_contamination` | 当前新进程只写自己的 NaN GPU tensor | 不能据此认定 serving KV Cache 污染 | 删除测试实例并重建 | A*/L4 |
| `weight_corruption` | 当前只把模型参数改到损坏模型占位路径 | 不能据此认定权重字节已损坏 | 恢复捕获的原参数 | A*/L4 |

注意：Kubernetes 的模型路径恢复不能硬编码为 `/model`。执行前必须保存 Deployment
完整 revision 或原 args；否则所谓 undo 可能把服务恢复到错误路径。权重、tokenizer
和权限实验只对模型副本执行，模型目录应只挂载给专用 canary。

### 6.2 网络层

| fault type | 目标/动作 | 证据 | Undo | 状态/风险 |
|---|---|---|---|---|
| `endpoint_timeout` | 当前把 Service selector 改为空目标 | Endpoint 空、connect timeout/refused | 恢复原 selector | A*/L2 |
| `http_503` | 当前移空 Service Endpoint | 实际多为拒绝/超时，不保证 HTTP 503 | 恢复原 selector | A*/L2 |
| `grpc_failure` | 容器内阻断 gRPC 端口 | gRPC unavailable、规则前后快照 | 删除精确规则 | A*/L2 |
| `dns_failure` | 尝试覆写容器 DNS 配置 | DNS error、Service 健康 | 恢复原配置 | A*/L2 |
| `packet_loss` | 容器 `eth0` 注入随机丢包 | retransmit、stream 中断、P99 | 删除 qdisc | A*/L3 |
| `bandwidth_throttle` | 容器 `eth0` 出口限带宽 | inter-token latency、throughput | 删除 qdisc | A*/L3 |
| `tp_communication_loss` | 当前阻断预设 TP 端口范围 | 真实通信端口上的 NCCL timeout | 删除规则并重建实例 | A*/L3 |
| `rdma_failure` | 当前阻断预设 RDMA/IB 端口 | 真实 RDMA 失效或可证 fallback | 删除规则并验证链路 | A*/L3 |
| `service_mesh_fault` | 错误路由/熔断 | route/sidecar 指标、5xx | 删除路由规则 | B/L2 |

节点原生 `tc/iptables` 会影响同节点其他工作负载，必须限定 interface、方向、端口、
目的地址和持续时间；注入前后保存 `tc qdisc show` 与相关 iptables rule。

### 6.3 GPU/驱动层

| fault type | 目标/动作 | 证据 | Undo | 状态/风险 |
|---|---|---|---|---|
| `cuda_error` | 隐藏/指定错误 CUDA device | CUDA log、GPU util 0、请求失败 | 恢复 env/revision | A/L3 |
| `nccl_failure` | 当前尝试阻断预设 NCCL 端口 | 真实 collective timeout、TP stall | 恢复网络并重建 Pod | A*/L3 |
| `pcie_throttle` | host-device copy 压力 | copy time、TTFT/TPOT、tokens/s | 停止压力进程 | A/L3 |
| `gpu_clock_throttle` | 当前尝试锁定低 GPU clock | 必须证明 clock 变化及 tokens/s 下降 | `nvidia-smi -rgc` | A*/L3 |
| `cupti_trace_overhead` | 当前尝试启动 `nsys` profiling | 必须证明 profiler 存活及 TTFT/TPOT 变化 | 关闭 profiler/restart | A*/L2 |
| `cuda_ctx_corruption` | 当前新进程尝试销毁自己的 context | 不能据此认定 serving context 已损坏 | 重建 serving Pod | A*/L4 |
| `ecc_memory_error` | 当前尝试 GPU reset，失败时写新进程 tensor | 必须区分 reset、指标模拟和真实 ECC | 清除模拟/隔离 GPU | A*/L4 |
| `gpu_driver_crash` | 当前 debug Pod 只输出模拟消息 | 不能宣称驱动已崩溃 | 节点级恢复 | A*/L4 |

部分“硬故障”实现可能只模拟指标或对注入器进程生效，不能等同于物理设备故障。
验收报告必须记录实际命令、作用进程/GPU UUID 和可观测结果。

### 6.4 OS/资源层

| fault type | 目标/动作 | 证据 | Undo | 状态/风险 |
|---|---|---|---|---|
| `gpu_oom` | 专用 GPU 压力 Pod/进程 | GPU memory、CUDA OOM、restart | 删除压力 Pod | A/L3 |
| `cpu_stress` | 同节点受控 CPU 压力 | CPU、queue、TTFT | 删除压力 Pod | A/L2 |
| `memory_pressure` | 同节点内存压力 | memory、OOM/Event | 删除压力 Pod | A/L3 |
| `disk_pressure` | 隔离临时路径写压力 | disk、load time/log latency | 删除压力文件/Pod | A/L3 |
| `cgroup_limit` | 临时降低内存 limit | OOM/restart、Pod revision | rollout undo | A/L3 |
| `numa_error` | 无效/次优 NUMA 绑定 | CPU/memory locality、latency | 恢复 affinity/restart | C/L3 |
| `hugepages_exhaustion` | 消耗 HugePage | allocation/fallback、latency | 停进程/restart | C/L3 |
| `fd_exhaustion` | 当前独立 Pod 打开自己的 fd | 不能据此认定 serving fd 已耗尽 | 删除压力 Pod/进程 | A*/L2 |
| `io_scheduler_interference` | 当前独立 Pod 临时路径连续 I/O | 必须证明共享设备 iowait 与服务时延变化 | 删除压力 Pod | A*/L3 |
| `clock_skew` | 当前尝试改容器时钟，失败时只打印时间 | 必须比较注入前后时钟及超时/TLS | 删除 TimeChaos/rebuild Pod | A*/L3 |

宿主机 `date -s`/`hwclock` 不作为推荐实现，因为容器可能共享节点时间。优先使用
TimeChaos 或独立虚拟机，避免影响控制面与证书。

### 6.5 当前执行器的语义限制

下表用于区分“命令返回成功”和“被测服务真的出现目标故障”。在这些限制消除前，
实验结论必须按实际动作命名，不能直接使用理想化 fault type。

| fault type/组 | 当前动作 | 不能直接宣称的结果 | 进入真实验收前的补强 |
|---|---|---|---|
| `model_load_failure` | 修改容器 args 固定索引为缺失路径 | 任意清单都能正确定位模型参数 | 从 Deployment revision 解析参数并保存原值 |
| `model_permission_error` | 把模型路径改成预设不可用路径 | 真实文件权限回归 | 在只读 canary 模型副本精确修改权限并校验 errno |
| `weight_corruption` | 把模型路径改成损坏模型占位路径 | 权重字节已经损坏 | 创建校验和已知的模型副本，限定分片和偏移，原子恢复 |
| `tokenizer_error` | 写 `/tmp/tokenizer_override.json` | vLLM 已读取该文件并初始化失败 | 将 canary 启动参数显式指向覆盖文件 |
| `scheduler_contention` | 从服务容器内用固定模型名发并发请求 | 真实模型调度器已争用 | 改用独立负载 Pod、从 `/v1/models` 发现模型并验证 waiting 指标 |
| queue/batch/prefill | 设置 `VLLM_*` 环境变量 | 当前 vLLM 版本一定采用这些变量 | 用该版本支持的 CLI/config 参数，比较生效前后启动参数与指标 |
| `endpoint_timeout`/`http_503` | Service selector 改为空目标 | 已返回指定 HTTP 状态码 | 按实际结果记录 timeout/refused；Undo 恢复捕获的原 selector |
| `grpc_failure`/`packet_loss`/`bandwidth_throttle` | 容器内 `iptables`/`tc`，部分分支失败后仍打印模拟成功 | 规则已经写入网络栈 | 检查 capability、规则前后快照和目标流量；失败必须非零退出 |
| `tp_communication_loss`/`nccl_failure`/`rdma_failure` | 端口规则和子 shell 环境变量 | 多节点 collective 或 RDMA 已中断 | 先确认真实端口/网卡/通信域，再用最小 collective 验证 |
| `state_contamination` | 新 Python 进程创建并写入自己的 GPU tensor | 在线服务 KV Cache 被污染 | 需要服务进程内受审 failpoint 或专用插桩 |
| `cuda_ctx_corruption` | 新 Python 进程尝试销毁自身 current context | 在线 serving context 被销毁 | 需要服务内插桩；默认只允许专用 canary |
| `ecc_memory_error` | 尝试 GPU reset，失败时改写新进程自己的 tensor | 物理 ECC 已发生 | 区分 DCGM 指标模拟、GPU reset 与真实硬件事件 |
| `gpu_driver_crash` | debug Pod 输出模拟消息 | 驱动已卸载或节点失去 GPU | 当前只能作为计划占位；节点级真实动作须单独审批 |
| `fd_exhaustion` | 独立 Pod 打开自己的文件描述符 | vLLM 进程 fd 已耗尽 | 使用服务内受控 helper，或改名为节点文件表压力并量化影响 |
| `clock_skew` | 尝试改容器时钟，失败时只打印时间 | 时钟已经偏移 | 使用 TimeChaos/独立虚拟机并比较注入前后时钟 |

此外，软件层恢复逻辑中的 `/model`、网络层恢复逻辑中的 `app=deployment-name` 仅匹配
仓库当前 vLLM 清单。对其他清单执行前必须捕获原 args、env、selector、resource 和
revision，并从这些快照生成 Undo；不能依赖硬编码默认值。

## 7. Agentic AI

| 故障 | 精确目标/注入 | 关键证据 | 恢复判据 | 状态/风险 |
|---|---|---|---|---|
| Agent Pod 崩溃 | 删除一个非唯一副本 | restart、active run、retry | Endpoint/任务成功率恢复 | B/L1 |
| Agent event-loop hang | 单 canary 进程 SIGSTOP | health/ready 分离、run 卡住 | SIGCONT/recreate，卡住 run 终止 | B/L2 |
| 模型超时/503 | 只对 Agent→模型流量 | model span、retry、fallback | 路由清理，run 成功率恢复 | B/L2 |
| 模型慢响应 | 目标 request header 延迟 | TTFT、run step、timeout budget | 删除规则 | B/L1 |
| 工具 5xx | 单只读工具、单 run ID | tool span/status、Agent 决策 | 关闭 failpoint | B/L1 |
| 工具超时 | 单工具定向延迟 | timeout/retry/circuit breaker | 删除延迟，轨迹完成 | B/L1 |
| 工具返回畸形 JSON | failpoint 单 hit | schema validation error | 关闭 failpoint | B/L1 |
| 工具权限拒绝 | 专用测试凭据 | 401/403、无越权执行 | 恢复测试 Secret revision | C/L3 |
| 沙箱启动失败 | 单轨迹 sandbox create fail | sandbox metric/trace | 恢复调度，清理实例 | B/L2 |
| 沙箱超时/OOM | 单轨迹资源 limit | timeout/OOM、tool result | 强制销毁，无残留 | B/L2 |
| 沙箱网络隔离 | 单 sandbox egress deny | connect error、policy log | 删除 NetworkPolicy | B/L2 |
| 会话存储超时 | Agent→专用 keyspace | state span、retry、run error | 恢复存储，读写一致 | B/L2 |
| 跨 session 污染 | 专用合成数据校验 | tenant/session key 冲突 | 删除实验数据、修复隔离 | C/L4 |
| Agent 无限循环 | 构造循环工具结果 | step count、loop limit | max steps 自动终止 | B/L1 |
| 重试风暴 | 工具失败 + 错误 retry 配置 | retry/QPS/queue、下游过载 | 熔断/恢复配置 | B/L2 |
| 输出解析失败 | 单模型/模板 canary | schema/parse error、task fail | 恢复模板/解析器 revision | B/L2 |
| Trace 断链 | 单 exporter/collector 中断 | run 正常但 span 缺失 | 恢复采集，Trace 完整 | B/L2 |
| 记忆陈旧/冲突 | 专用 session 注入旧版本 | evidence/version、错误决策 | 清理 key、恢复版本 | C/L3 |

Agentic AI 的恢复指标必须包含任务成功率、平均 step 和 tool-call 正确率；HTTP 200
或 Agent Pod Ready 不能证明多轮链路恢复。

## 8. Agentic RL

| 故障域 | 故障 | 精确目标/动作 | 关键证据 | 恢复 | 状态/风险 |
|---|---|---|---|---|---|
| 控制面 | controller crash | 非保存窗口删除 controller Pod | Ray actor/job、最后 step | 从状态/ckpt 恢复，step 继续 | B/L3 |
| 控制面 | Ray head 失联 | 专用集群短时隔离 | node/actor/task、progress heartbeat | 恢复 head 或显式失败重提 | C/L3 |
| worker | 单 trainer worker crash | 删除非 leader/rank Pod | rank error、collective、step 停止 | worker/job 重建，状态一致 | B/L2 |
| worker | straggler | 单 rank CPU/GPU/网络延迟 | rank timing、barrier、step P99 | 清除压力，step 回归 | B/L2 |
| rollout | rollout server crash/hang | 单 DP group Pod | rollout pending、QPS、GPU | 重建 worker，batch 继续/重试 | B/L2 |
| rollout | queue overflow | 受控并发/长轨迹 | pending、rollout P99、timeout | 停止负载/调小并发 | B/L2 |
| Agent | tool timeout/5xx | 单工具/轨迹 | tool trace、有效 rollout 比例 | 关闭规则，轨迹成功 | B/L1 |
| Agent | sandbox leak | 故意终止轨迹 | sandbox count、PID/Pod/临时盘 | janitor 清零 | B/L2 |
| reward | reward timeout | 单 reward service | reward latency、actor idle | 恢复服务，batch 继续 | B/L2 |
| reward | NaN/Inf reward | 单 batch failpoint | finite check、loss/advantage | 丢弃 batch/恢复函数 | C/L3 |
| reward | reward skew | 专用合成样本偏置 | reward distribution、KL/entropy | 恢复版本并回滚坏 ckpt | C/L4 |
| 数据 | dataset shard missing | 单副本路径错误 | loader error、sample count | 恢复 manifest/路径 | B/L2 |
| 数据 | schema/样本损坏 | 单合成 shard | parse/reject/effective batch | 隔离 shard、恢复校验和 | C/L3 |
| GPU | actor/rollout OOM | 单 worker 调小余量/加长序列 | CUDA OOM、memory、restart | 降 batch/恢复配置、重提 | B/L3 |
| 通信 | NCCL timeout | 单链路短时 loss/delay | collective timeout、rank stall | 清规则，重建 group | C/L3 |
| 通信 | RDMA fallback | 单节点/链路 | NCCL log、throughput/latency | 恢复链路，重建 group | C/L3 |
| 存储 | checkpoint 写失败 | 临时目录/PVC 配额或 I/O fail | save error、bytes、manifest | 清临时文件，恢复空间重存 | C/L3 |
| 存储 | checkpoint 分片缺失/损坏 | 只破坏快照副本 | checksum/load error | 切回已验证 checkpoint | C/L4 |
| 一致性 | 部分 worker 参数版本漂移 | 专用 canary worker | model/checkpoint hash、loss 异常 | 重建 worker/统一 revision | C/L4 |
| 可观测 | progress/Trace 盲区 | 单 exporter 中断 | GPU 忙但 step/Trace 缺失 | 恢复 exporter，证据连续 | B/L2 |
| 复合 | straggler + 陈旧告警 | 单 rank 延迟 + 无关 crash | rank/Trace 与 Event 冲突 | 先真故障再噪声 | B/L3 |

训练故障的停止条件至少包括：连续两个采样窗 global step 不推进、出现 NaN/Inf、
GPU XID/ECC、checkpoint 正式路径被写坏、影响实体数超预算、控制面失去清理能力。

## 9. 执行前后检查清单

执行前：

```bash
kubectl config current-context
TESTBED_NS=your-testbed-namespace
kubectl -n "${TESTBED_NS}" get deploy,statefulset,job,pod,svc,endpoints,pvc -o wide
kubectl -n "${TESTBED_NS}" get events --sort-by=.lastTimestamp
```

执行后必须查询带实验标签的残留：

```bash
EXPERIMENT_ID=your-experiment-id
kubectl get pod,job,configmap,networkpolicy -A \
  -l "agenticsre.io/experiment-id=${EXPERIMENT_ID}"
```

节点故障还要逐节点检查 `tc qdisc`、相关防火墙规则、压力 PID、磁盘临时文件、GPU
clock、cordon/taint 和设备健康。发现任一残留时，实验状态应标为 `cleanup_failed`，
不能标为恢复成功。

## 10. 实现来源

- 通算场景：`eval/fault_scenarios.yaml`
- 细粒度合同：`configs/precision_fault_scenarios.yaml`
- 细粒度实施状态：`docs/testbeds/precision-fault-injection-status.md`
- vLLM 场景说明：`vllm_fault_injector/scenarios.yaml`、`eval/llm_fault_scenarios.yaml`
- vLLM 执行器：`vllm_fault_injector/`
- 安全与自愈约束：`docs/guarded_fault_injection_and_recovery.md`
