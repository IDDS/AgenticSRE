# 被测对象部署指南

本目录说明 **AgenticSRE 的被测对象**如何部署、验收和准备故障实验。它与
AgenticSRE 自身的部署文档分层：这里部署的是接受观测、诊断、故障注入和自愈
验证的工作负载，不是 AgenticSRE 服务本身。

## 文档导航

| 环境 | 被测对象 | 推荐用途 | 部署等级 | 文档 |
|---|---|---|---|---|
| 通算 | DeathStarBench Social Network | 微服务调用链、缓存/数据库依赖、级联故障 | D1：上游固定版本命令和必要补丁已核对；仍需自建/固定镜像并完成远端验收 | [部署指南](general-compute-deathstarbench.md) |
| 通算 | Train Ticket | 大规模异构微服务、服务发现、消息与数据库故障 | D1：上游路径已核对；固定版本需补丁且未完整验收 | [部署指南](general-compute-train-ticket.md) |
| 智算 | vLLM | 在线推理、GPU、KV Cache、调度和模型加载 | D3-T4 历史基线 / D1 新部署：特定 T4 拓扑验证过，但清单的浮动镜像必须先固定 | [部署指南](ai-vllm.md) |
| 智算 | Agentic AI | 多轮推理、工具调用、沙箱和请求级 Trace | D0：部署合同，需自备应用源码、镜像、工具和沙箱 | [部署合同](ai-agentic-ai.md) |
| 智算 | Agentic RL | rollout、reward、训练、通信和 checkpoint | D0/D1：可参考单机命令；无可直接部署的 Kubernetes 清单 | [部署合同](ai-agentic-rl.md) |

部署等级的含义：

| 等级 | 含义 | 用户可以期待什么 |
|---|---|---|
| D0 | 架构/接口合同 | 用于设计和验收，不是可直接执行的部署包 |
| D1 | 上游路径已核对 | 命令和文件存在，但需站点补丁、镜像或环境适配 |
| D2 | 可进入集群部署验收 | 具备主体部署流程，仍必须通过本地预检和站点验收 |
| D3 | 已在声明拓扑验证 | 在同类硬件和版本上可复现；不自动保证跨拓扑可移植 |

> 只有标记 D2/D3 的环境才应进入集群部署操作。D0/D1 必须先由
> 实施人员补齐文档声明的缺失物，不能把 namespace 创建成功当作被测环境已部署。

全部环境的详细故障类型、风险等级、观测证据和恢复判据见
[被测对象故障目录](fault-catalog.md)。18 个细粒度场景的逐项完成度、执行前提、
证据和适配器验收门见
[细粒度故障注入实施状态与验收规范](precision-fault-injection-status.md)。

## 术语边界

- **通算环境**：以 Kubernetes 微服务为主，故障重点是 Pod、Service、网络、
  配置、数据库、缓存、消息和节点资源。
- **vLLM 环境**：提供 OpenAI 兼容接口的模型推理服务，重点是 GPU、模型加载、
  KV Cache、连续批处理、请求队列和推理链路。
- **Agentic AI 环境**：在模型服务之上增加 Agent 运行时、工具服务、状态存储和
  沙箱，重点是多轮执行链的正确性、隔离性和可观测性。它是工作负载形态，不是
  某个固定软件名称。
- **Agentic RL 环境**：由训练控制器、actor/critic 或 reference、rollout 引擎、
  reward、环境/工具、分布式通信与 checkpoint 组成。本文用 verl 作为可替换的
  参考实现。

## 建议选型

如果目标是尽快建立可重复的通算闭环，优先部署 DeathStarBench Social Network；
它与仓库现有 `eval/fault_scenarios.yaml` 的服务名和故障路径最接近。Train Ticket
服务更多、启动时间和资源开销更大，适合验证服务发现、消息中间件和数据库依赖
的复杂故障。

如果目标是智算在线推理闭环，先部署仓库自带的 vLLM 环境，再增加 Agentic AI
运行时。Agentic RL 不应作为第一个智算被测对象：它同时占用训练和推理资源，
恢复判据也不能只看 Pod Ready，必须检查训练 step、reward、有效样本和 checkpoint。

## 统一前置检查

在执行任一部署教程前，先确认当前上下文和可用资源。不要在生产集群使用本目录
的故障实验配置。

```bash
kubectl config current-context
kubectl cluster-info
kubectl get nodes -o wide
kubectl get storageclass
kubectl auth can-i create deployments --all-namespaces
kubectl auth can-i create networkpolicies --all-namespaces
helm version
```

智算环境还应检查设备插件和共享内存：

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia.com/gpu
kubectl get pods -A | grep -E 'device-plugin|gpu-operator|dcgm'
```

若使用其他加速设备，将资源键、设备插件和指标采集器替换为目标环境的实现，
不要继续使用 `nvidia.com/gpu` 作为验收条件。

## 统一部署原则

1. 每类被测对象使用独立 namespace；不要与 AgenticSRE 控制面共用 namespace。
2. 固定上游提交、容器镜像 digest 或不可变版本号，保存实际值；禁止将 `latest`
   当作可复现实验版本。
3. 部署前记录基线：节点、Pod、Service、Endpoint、PVC、镜像、资源限额和版本。
4. 先建立持续业务负载，再接入指标、日志和 Trace；没有负载的 Ready Pod 不能
   证明故障对业务有影响。
5. 每次真实注入必须有唯一 `experiment_id`、单一目标、持续时间、爆炸半径、
   undo 动作和恢复验证；默认从低风险场景开始。
6. 数据破坏类实验只对一次性副本、快照或临时卷执行，不能直接修改唯一模型权重、
   业务数据库或唯一 checkpoint。

## 统一验收门

被测对象进入 AgenticSRE 故障实验前，应同时满足以下条件。

| 验收门 | 最低要求 | 证据示例 |
|---|---|---|
| G1 版本可追溯 | 上游提交、镜像、配置和模型版本已记录 | `git rev-parse HEAD`、`kubectl get pod -o jsonpath=...imageID` |
| G2 资源稳定 | 目标 Pod Ready，重启数不再持续增长 | `kubectl get pod -w`、`kubectl get events` |
| G3 业务可用 | 连续请求成功，已有延迟和错误率基线 | 负载生成器结果、HTTP 状态、P95/P99 |
| G4 可观测 | 指标、日志、事件至少三类可查询；复杂链路还要有 Trace | Prometheus target、日志查询、Trace ID |
| G5 目标稳定 | namespace、workload、container、service 和标签已固定 | `kubectl get ... --show-labels` |
| G6 可恢复 | undo 已演练，恢复后业务和关键指标回归基线 | rollout/scale 验证、恢复窗口对比 |
| G7 可清理 | 负载生成器和故障资源可按标签删除，不残留后台进程 | label 查询结果为空 |

建议保存一次部署快照：

```bash
TESTBED_NS=your-testbed-namespace
kubectl -n "${TESTBED_NS}" get deploy,statefulset,daemonset,job,pod,svc,endpoints,pvc -o wide
kubectl -n "${TESTBED_NS}" get events --sort-by=.lastTimestamp
```

## 接入 AgenticSRE

将目标集群写入 `configs/clusters.yaml`，或在 Web 页面“管理目标”中创建同等配置。
示例只展示结构，不包含站点地址和凭据：

```yaml
fault_targets:
  k8s_clusters:
    - id: testbed-general
      name: 通算被测集群
      kind: general
      kubectl_path: kubectl
      kubeconfig: /root/.kube/testbed-config
      context: testbed
      default_namespace: test-social-network
      prometheus_url: http://prometheus.example:9090

    - id: testbed-ai
      name: 智算被测集群
      kind: ai
      kubectl_path: kubectl
      kubeconfig: /root/.kube/testbed-config
      context: testbed
      default_namespace: llm-k8s
      prometheus_url: http://prometheus.example:9090
      vllm_endpoint: http://inference.example:8000/v1/completions
      model: served-model-name
```

如果 AgenticSRE 运行在容器中，`kubeconfig` 必须是容器内可见路径；其中引用的
证书文件也必须可见。已有 `data/cluster_profiles.json` 时，运行配置会覆盖
`configs/clusters.yaml` 默认值，详见 `configs/README.md`。

接入后先做只读校验：

```bash
curl -fsS http://127.0.0.1:8080/api/health
kubectl --kubeconfig /path/to/testbed-kubeconfig --context testbed-context get namespace
```

随后在前端选择目标集群，只运行 dry-run 或预检；确认解析出的 namespace、
workload、container、Service、undo 和恢复判据全部正确后，才允许真实注入。
通算细粒度能力位于“通算故障实验”的“细粒度注入”页签；它与普通场景共享目标
管理和审批体系，但使用固定适配器完成独立的七阶段效果验证。

## 版本基线

本轮文档核对的上游提交如下。部署时应重新确认提交仍可获取，并把实际镜像 digest
写入实验记录。

| 被测对象 | 上游提交 |
|---|---|
| DeathStarBench | `6ecb09706140f8730b5385c08f1386c654c3c526` |
| Train Ticket | `313886e99befb94be6cd45f085c98e0019f59829` |
| vLLM Production Stack（可选参考） | `01eaf92cb895ececa44ee86083f11522adafbbc9` |
| verl | `668baad7455453eac8ab863f1b2d6fecaec746ed` |

这些提交的取证日期为 2026-07-30，只用于固定教程输入，不代表所有目标集群已经
完成兼容性验收。仓库自带的
vLLM 清单及其 T4 验证记录见 `deploy/k8s-vllm/README.md`。
