# vLLM 智算被测环境部署指南

本文部署一个可接受持续推理负载、Prometheus 采集、GPU 观测和受控故障注入的
Kubernetes vLLM 环境。优先使用仓库 `deploy/k8s-vllm/` 的实测清单；上游
production stack 适合扩展到多实例路由，不是本项目闭环的必选依赖。

## 1. 已验证边界

仓库清单已在 2026-07-03 的 T4 Kubernetes 环境验证：vLLM 服务、OpenAI 兼容
推理、Prometheus target、请求成功计数、TTFT/E2E P95 和 GPU 指标可以形成闭环。
这是历史 D3-T4 基线；模型路径、GPU UUID、节点名和站点地址都是
运行参数，不是通用默认值。

当前清单仍引用 `vllm/vllm-openai:latest`。新部署必须先解析并固定一个经目标模型
验收的镜像 digest；GPU exporter 也复用该浮动镜像。Prometheus 清单已在
2026-10-15 恢复时固定为 v3.13.4 的 digest，并增加存储上限与健康检查。
模型与 GPU exporter 的镜像仍需固定，不要把浮动标签写进实验基线。
因此当前仓库清单对“新环境直接部署”只计 D1；完成镜像固定并重跑本文
验收后，才能对对应站点重新记为 D3。

## 2. 节点前置条件

```bash
command -v kubectl
command -v jq
kubectl get nodes -o wide
kubectl get nodes \
  -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia.com/gpu
kubectl get pods -A | grep -E 'device-plugin|gpu-operator|dcgm'
```

在 GPU 节点核对驱动和设备：

```bash
nvidia-smi
nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv
```

还要确认：

- 模型目录在选定节点可读，且权重完整；
- 容器运行时允许申请 GPU；
- NodePort `30080`、Prometheus `30090` 未冲突；
- 节点有足够磁盘和主机内存完成模型加载；
- 实验只选择未被其他进程占用的 GPU UUID。

当前 GPU exporter 是特定 T4 集群清单，还有以下硬前提：

- DaemonSet 会调度到所有节点，节点必须都有 `/usr/bin/nvidia-smi`、
  `/dev/nvidiactl`、`/dev/nvidia-uvm*` 和 `/dev/nvidia0` 至 `/dev/nvidia2`；
- 所有节点的 host port `9100` 和 `9400` 必须空闲；
- Prometheus 静态目标列表只渲染两个节点地址。

任一条不满足时，不要原样应用 `gpu-metrics-exporter.yaml` 和
`prometheus.yaml`。应优先复用集群已管理的 GPU/DCGM 和节点采集器；否则需要为
DaemonSet 增加精确 node selector、按实际设备修改挂载，并重新生成 Prometheus 目标。

## 3. 固定镜像和模型

先在目标架构机器拉取候选镜像并记录 digest：

```bash
export VLLM_IMAGE_TAG=validated-version
docker pull "vllm/vllm-openai:${VLLM_IMAGE_TAG}"
docker image inspect "vllm/vllm-openai:${VLLM_IMAGE_TAG}" \
  --format '{{index .RepoDigests 0}}'
```

将 `deploy/k8s-vllm/vllm-k8s.yaml` 和 `gpu-metrics-exporter.yaml` 中的 vLLM image
改为该 digest，并将 `prometheus.yaml` 中的 Prometheus image 固定为已验证
digest，再进行部署评审。模型
同样应记录来源、revision、文件清单和校验和。模型目录的唯一副本不得用于权重
损坏类实验。

## 4. 设置站点参数

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
export VLLM_NODE_NAME=gpu-node-name
export VLLM_PROMETHEUS_NODE_NAME=prometheus-node-name
export VLLM_GPU_UUID=GPU-UUID
export VLLM_MODEL_PATH=/absolute/path/to/model
export VLLM_SERVED_MODEL_NAME=served-model-name
export GPU_EXPORTER_NODE_1=192.0.2.11
export GPU_EXPORTER_NODE_1_NAME=gpu-node-1
export GPU_EXPORTER_NODE_2=192.0.2.12
export GPU_EXPORTER_NODE_2_NAME=gpu-node-2
```

仓库实测示例值见 `deploy/k8s-vllm/README.md`。不要原样复制其中节点和 GPU UUID
到另一套集群。

## 5. 渲染、部署和首次限制

`apply.sh` 会把环境变量渲染到临时清单，然后直接依次执行
`kubectl apply`。`bash -n` 只检查 shell 语法，脚本当前没有 server-side dry-run
模式。因此它只能在专用测试集群首次执行，不得在共享或生产集群试跑。
执行前至少确认上文设备、端口、镜像和两节点拓扑前提：

```bash
bash -n deploy/k8s-vllm/apply.sh
kubectl config current-context
kubectl get node "${VLLM_NODE_NAME}" "${VLLM_PROMETHEUS_NODE_NAME}"
bash deploy/k8s-vllm/apply.sh
```

部署后等待：

```bash
kubectl -n llm-k8s get deploy,daemonset,pod,svc -o wide
kubectl -n llm-k8s rollout status deployment/vllm-server --timeout=15m
kubectl -n llm-k8s rollout status deployment/vllm-prometheus --timeout=5m
kubectl -n llm-k8s get events --sort-by=.lastTimestamp
```

如果目标节点模型目录不同，当前 `hostPath` 会导致 Pod 启动失败。不要通过复制一份
不受版本管理的模型到随机路径规避，应修正 `VLLM_MODEL_PATH` 并记录模型校验和。

## 6. 端到端验证

默认外部入口：

```bash
export VLLM_URL=http://192.0.2.10:30080
export PROM_URL=http://192.0.2.10:30090
export MODEL=served-model-name
bash deploy/k8s-vllm/verify_vllm_k8s.sh
```

验证器会对 vLLM HTTP 和一次推理请求使用 `curl -f`，但对
Prometheus targets 和 PromQL 只打印 JSON，不会因 target 为 down 或查询结果为空而失败。
因此脚本返回 0 不能单独作为验收结论。还必须执行：

```bash
curl -fsS "${PROM_URL}/api/v1/targets" \
  | jq -e '[.data.activeTargets[] | select(.health != "up")] | length == 0'
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:request_success_total' \
  | jq -e '.status == "success" and (.data.result | length > 0)'
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=DCGM_FI_DEV_GPU_UTIL' \
  | jq -e '.status == "success" and (.data.result | length > 0)'
```

若指标为空，先持续发出请求再重试，不要把“HTTP 健康”当作
“指标接入完成”。

## 7. 持续负载

```bash
export VLLM_NAMESPACE=llm-k8s
export VLLM_SERVICE_NAME=vllm-server
export VLLM_SERVED_MODEL_NAME=served-model-name
export LOADGEN_IMAGE=vllm/vllm-openai@sha256:replace-with-validated-digest
export LOADGEN_DURATION_S=300
export LOADGEN_CONCURRENCY=2
export LOADGEN_PROMPT_LEN=256
export LOADGEN_MAX_TOKENS=32
bash deploy/k8s-vllm/apply_loadgen.sh
```

先以低并发建立 10 分钟基线，记录 request success、running/waiting、TTFT、TPOT、
E2E、KV Cache、GPU utilization/memory 和错误率。KV Cache 故障实验再逐步提高 prompt
长度和并发；不要一开始就使用最大上下文和全部 GPU 容量。

停止负载：

```bash
kubectl -n llm-k8s delete pod,configmap \
  -l app=agenticsre-vllm-loadgen --ignore-not-found=true
```

## 8. 可观测性验收

```bash
curl -fsS "${PROM_URL}/api/v1/targets"
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:num_requests_running'
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:num_requests_waiting'
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=DCGM_FI_DEV_GPU_UTIL'
```

同时核对：

```bash
kubectl -n llm-k8s logs deploy/vllm-server --tail=100
kubectl -n llm-k8s get endpoints vllm-server
kubectl -n llm-k8s get events --sort-by=.lastTimestamp
```

需要请求级 GPU/Kernel Trace 时，再按
`docs/integrations/teller_skill_runtime_deployment.md` 接入。该能力不能由普通
Prometheus 指标替代。

## 9. AgenticSRE 配置

可在 Web“管理目标”新建档案。如果改 `configs/clusters.yaml`，将下面的
列表项合并到已有 `fault_targets.k8s_clusters` 列表；不要替换整个文件：

```yaml
fault_targets:
  k8s_clusters:
    - id: vllm-k8s
      name: vLLM 智算集群
      kind: ai
      kubectl_path: kubectl
      kubeconfig: /root/.kube/ai-testbed-config
      context: ai-testbed
      default_namespace: llm-k8s
      prometheus_url: http://192.0.2.10:30090
      vllm_endpoint: http://192.0.2.10:30080/v1/completions
      model: served-model-name
      model_family: model-family
      description: vLLM 推理、GPU 和 Prometheus 被测环境
```

如果 vLLM 运行在裸机而非 Kubernetes，使用 `llm_hosts` 档案并通过 SSH 受控执行；
凭据只通过密钥或运行时环境变量提供，不能写入 YAML。

## 10. 故障准备与验收

先检查场景目录：

```bash
python3 -m vllm_fault_injector.cli inject process_crash \
  --target k8s \
  --namespace llm-k8s \
  --deployment vllm-server \
  --dry-run
```

也可以通过 AgenticSRE Web 的 dry-run/预检生成合同。真实注入必须按照
[故障目录](fault-catalog.md#6-vllm) 从请求负载、单 Pod 和 Service 类开始。

上线门包括：

- vLLM Pod Ready，模型 API 连续成功；
- Prometheus vLLM/GPU targets 为 up，关键指标已有非空样本；
- 负载可按标签停止；
- 单 Pod 删除后 Deployment 恢复，Endpoint 和推理成功率回归；
- 没有遗留 `tc`、`iptables`、GPU clock lock、压力进程或损坏副本。

## 11. 可选：上游 production stack

需要多 vLLM 实例、路由和统一观测时，可参考：

```bash
git clone https://github.com/vllm-project/production-stack.git
cd production-stack
git checkout 01eaf92cb895ececa44ee86083f11522adafbbc9
helm repo add vllm https://vllm-project.github.io/production-stack
helm repo update
helm upgrade --install vllm vllm/vllm-stack \
  --namespace llm-k8s --create-namespace \
  -f tutorials/assets/values-01-minimal-example.yaml
```

上游最小 values 使用示例模型和浮动镜像，不能直接作为项目验收配置。必须替换为
目标模型、固定镜像、资源、存储和 endpoint，并将 AgenticSRE 的 `deployment`、
`service`、指标 label 与实际 chart 输出对齐。

## 12. 清理

仓库清单没有一键卸载脚本。先停止负载，再按部署文件删除资源：

```bash
kubectl -n llm-k8s delete pod,configmap \
  -l app=agenticsre-vllm-loadgen --ignore-not-found=true
kubectl delete -f deploy/k8s-vllm/prometheus.yaml --ignore-not-found=true
kubectl delete -f deploy/k8s-vllm/gpu-metrics-exporter.yaml --ignore-not-found=true
kubectl delete -f deploy/k8s-vllm/vllm-k8s.yaml --ignore-not-found=true
```

原始 YAML 包含模板变量，若直接删除无法匹配已渲染对象，可按资源名删除或保存部署
时的渲染清单用于卸载。删除 namespace 前确认其中没有共享模型服务或采集组件。

## 13. 参考来源

- 仓库实测部署：`deploy/k8s-vllm/README.md`
- vLLM 文档：<https://docs.vllm.ai/>
- vLLM production stack：<https://github.com/vllm-project/production-stack>
- 项目故障实现：`vllm_fault_injector/`
