# Agentic AI 被测环境部署指南

Agentic AI 不是单一软件包。本文把它定义为一套可替换的在线被测栈：

```text
客户端/负载 -> Agent API -> Agent 运行时 -> 模型服务
                              |-> 工具服务
                              |-> 隔离沙箱
                              |-> 会话/记忆存储
                              `-> 指标、日志、Trace
```

本文给出部署合同和验收流程。仓库当前没有绑定某个第三方 Agent 框架的完整镜像，
因此本环境属于“参考栈，待目标集群验收”；不要把本文的接口占位值标记为已实测。

> **可执行性边界**：本文不是从空集群直接部署 Agentic AI 的教程。
> 实施人员必须先自备 Agent 应用源码和镜像、工具网关、沙箱、Secret、
> 状态存储以及 OpenTelemetry/Prometheus 组件。缺少任一必需组件时，
> 只能用本文做架构评审，不应执行第 5 节的 `kubectl apply`。

## 1. 先部署模型服务

按 [vLLM 部署指南](ai-vllm.md) 完成模型服务，至少确认：

```bash
export MODEL_BASE_URL=http://192.0.2.10:30080
curl -fsS "${MODEL_BASE_URL}/health"
curl -fsS "${MODEL_BASE_URL}/v1/models"
```

Agent 运行时应通过 Kubernetes Service 或受控网关访问模型，不要绑定某个 Pod IP。
模型服务、Agent 运行时和工具服务使用不同 Deployment，便于分别注入和定位故障。

## 2. Agent 运行时部署合同

无论选用何种框架，镜像必须提供以下最小接口：

| 接口 | 要求 |
|---|---|
| `GET /healthz` | 仅证明进程存活，不依赖模型和工具 |
| `GET /readyz` | 检查模型、必需工具和状态存储，失败返回非 2xx |
| `POST /v1/agent/runs` | 创建一次带 `run_id` 的 Agent 执行 |
| `GET /v1/agent/runs/{run_id}` | 返回状态、step、tool call 和错误分类 |
| `GET /metrics` | Prometheus 指标 |
| OTLP export | 输出跨 Agent、模型、工具和沙箱的 Trace |

每次执行至少记录：`run_id`、`session_id`、`model`、`agent_version`、`step_index`、
`tool_name`、`tool_call_id`、`latency`、`status`、`retry_count`、token 用量和 Trace ID。
提示词、工具结果和用户数据应按数据分级脱敏，不能默认原文进入日志。

## 3. 构建不可变镜像

在 Agent 应用源代码仓库中固定依赖并构建镜像：

```bash
git rev-parse HEAD
export AGENT_IMAGE_TAG=validated-version
docker build -t "registry.example/agent-runtime:${AGENT_IMAGE_TAG}" .
docker push "registry.example/agent-runtime:${AGENT_IMAGE_TAG}"
docker image inspect "registry.example/agent-runtime:${AGENT_IMAGE_TAG}" \
  --format '{{index .RepoDigests 0}}'
```

部署清单必须使用输出的 digest。镜像内不应在容器启动时 `pip install` 或下载工具
代码；否则一次 Pod 重启可能改变被测版本。

## 4. 创建 namespace 和配置

```bash
export AGENT_NAMESPACE=agentic-ai-testbed
kubectl create namespace "${AGENT_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
```

使用 ConfigMap 保存非敏感参数：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: agent-runtime-config
  namespace: agentic-ai-testbed
data:
  MODEL_BASE_URL: http://vllm-server.llm-k8s.svc.cluster.local:8000/v1
  MODEL_NAME: served-model-name
  TOOL_CATALOG_URL: http://tool-gateway:8080/catalog
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  OTEL_SERVICE_NAME: agent-runtime
  MAX_AGENT_STEPS: "12"
  TOOL_TIMEOUT_SECONDS: "15"
  RUN_TIMEOUT_SECONDS: "120"
```

API key、状态存储凭据和工具凭据放入 Secret，并通过 `secretKeyRef` 注入。不要把
Secret 内容写入本目录、ConfigMap、Pod 参数或实验记录。

## 5. 部署 Agent API

下面清单只展示资源和探针要求。`registry.example`、`tool-gateway`、
`otel-collector` 和所有凭据都是占位值；清单也不含工具服务、沙箱、存储和
NetworkPolicy。必须将它们替换或补齐后再保存为站点清单：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-runtime
  namespace: agentic-ai-testbed
spec:
  replicas: 2
  selector:
    matchLabels: {app: agent-runtime}
  template:
    metadata:
      labels: {app: agent-runtime, testbed.agenticsre.io/component: agent}
    spec:
      containers:
        - name: agent
          image: registry.example/agent-runtime:validated-version
          ports:
            - {name: http, containerPort: 8080}
            - {name: metrics, containerPort: 9464}
          envFrom:
            - configMapRef: {name: agent-runtime-config}
          readinessProbe:
            httpGet: {path: /readyz, port: http}
            periodSeconds: 5
          livenessProbe:
            httpGet: {path: /healthz, port: http}
            periodSeconds: 10
          resources:
            requests: {cpu: 500m, memory: 512Mi}
            limits: {cpu: "2", memory: 2Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: agent-runtime
  namespace: agentic-ai-testbed
spec:
  selector: {app: agent-runtime}
  ports:
    - {name: http, port: 8080, targetPort: http}
    - {name: metrics, port: 9464, targetPort: metrics}
```

只有下列预检全部通过后，才保存为目标环境自己的清单并执行：

1. 镜像可拉取且使用 digest，容器内实际监听文档声明的端口；
2. 第 4 节 ConfigMap 已与 Deployment/Service 合并进同一站点清单，
   Secret、ServiceAccount、NetworkPolicy 和所有 PVC 均已定义；
3. 模型、工具网关、状态存储和采集端点从该 namespace 可达；
4. `/healthz`、`/readyz`、`/metrics` 和 run API 已在镜像本地验证。

```bash
kubectl apply --dry-run=server -f agent-runtime.yaml
kubectl apply -f agent-runtime.yaml
kubectl -n "${AGENT_NAMESPACE}" rollout status deploy/agent-runtime --timeout=5m
```

## 6. 工具服务和沙箱

工具服务必须独立部署，并为每个工具定义：输入 JSON Schema、幂等性、超时、允许
目标、最小权限、审计字段和错误分类。优先选择只读工具建立基线。

执行生成代码、Shell、浏览器或文件操作的工具必须进入独立沙箱，至少使用：

- 非 root 用户、只读根文件系统和 seccomp；
- 独立 ServiceAccount，不挂载集群管理 kubeconfig；
- 默认拒绝网络，只放行模型/工具所需目标；
- CPU、内存、进程数、临时盘、运行时长和输出大小限制；
- 每条轨迹独立实例或工作目录，结束后强制清理；
- 工具参数和结果带 `run_id/tool_call_id`，但敏感数据脱敏。

不要把演示用的进程内代码执行器作为真实故障实验沙箱。

## 7. 状态存储

无状态问答可以先不部署共享记忆；需要多轮会话时，将 session/checkpoint 存入独立
数据库或缓存。数据键必须至少包含 tenant、session 和 run，设置 TTL，并禁止两个
实验共享相同 session。

状态存储的 Ready 探针只做轻量读写；故障实验使用专用 keyspace，不能删除共享库。

## 8. 负载和验收请求

通过 port-forward 验证：

```bash
kubectl -n "${AGENT_NAMESPACE}" port-forward svc/agent-runtime 18082:8080
curl -fsS http://127.0.0.1:18082/healthz
curl -fsS http://127.0.0.1:18082/readyz
```

创建一次可审计运行：

```bash
curl -fsS -H 'Content-Type: application/json' \
  -H 'X-Run-ID: baseline-001' \
  -d '{"session_id":"baseline-session","input":"调用只读工具查询测试对象状态，并给出证据。"}' \
  http://127.0.0.1:18082/v1/agent/runs
```

持续负载应同时包含三类轨迹：无工具单轮、单工具多轮、多工具长链路。固定数据集、
并发、最大 step、工具超时和模型采样参数，记录任务成功率，不要只统计 HTTP 200。

## 9. 可观测性

建议指标：

- `agent_runs_total{status}`、`agent_run_duration_seconds`；
- `agent_steps_total`、`agent_active_runs`、`agent_retry_total{reason}`；
- `agent_tool_calls_total{tool,status}`、`agent_tool_latency_seconds{tool}`；
- `agent_model_requests_total{status}`、首 token/总时延和 token 用量；
- `agent_loop_limit_total`、`agent_output_validation_failures_total`；
- 沙箱创建/销毁/超时/OOM 和状态存储错误。

一条 Trace 应形成：

```text
agent.run -> agent.step -> model.generate -> tool.call -> sandbox.execute
```

模型 API 只有指标、没有请求级 Trace 时，应明确标记跨层 Trace 缺口。Agent 日志的
tool call 与模型日志可通过 `run_id` 关联，但这不是完整分布式 Trace 的替代品。

## 10. AgenticSRE 接入

Agent 运行时和工具服务仍作为 Kubernetes 通算资源接入；其底层 vLLM/GPU 同时作为
智算目标接入。两个档案可以指向同一集群，但 namespace、资源名和故障合同必须
隔离。可在 Web“管理目标”新建档案；如果改 `configs/clusters.yaml`，将列表项
合并到已有 `fault_targets.k8s_clusters` 列表，不要替换整个文件：

```yaml
fault_targets:
  k8s_clusters:
    - id: agentic-ai-runtime
      name: Agentic AI 运行时
      kind: general
      kubectl_path: kubectl
      kubeconfig: /root/.kube/ai-testbed-config
      context: ai-testbed
      default_namespace: agentic-ai-testbed
      prometheus_url: http://prometheus.example:9090
      description: Agent、工具、沙箱和状态存储被测环境
```

详细故障类型见 [故障目录](fault-catalog.md#7-agentic-ai)。低风险顺序为：单个 Agent
Pod 删除、只读工具短时延迟、模型请求限流、沙箱超时。跨 session 污染、凭据错误、
沙箱策略和数据损坏只能在专用数据与隔离集群执行。

## 11. 上线验收

- 两个 Agent 副本 Ready，Service Endpoint 非空；
- 三类基线轨迹连续成功，tool call 参数和结果可审计；
- 指标、日志和 Trace 都带同一 `run_id`；
- tool timeout、run timeout、max steps 和 retry budget 生效；
- 沙箱在正常、超时和异常退出后均无残留；
- 单 Pod 和单工具故障恢复后，任务成功率与 P95 回归基线；
- 敏感提示词、工具凭据和工具结果未以明文进入普通日志。

## 12. 清理

停止负载，导出实验结果，再删除该被测环境自己的资源：

```bash
kubectl -n "${AGENT_NAMESPACE}" delete -f agent-runtime.yaml
kubectl -n "${AGENT_NAMESPACE}" get all,configmap,secret,pvc
```

只有确认状态存储无保留价值时才删除 PVC 或 namespace。模型服务在 `llm-k8s`
namespace，不能随 Agent 运行时一起误删。
