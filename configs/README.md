# `configs/` 配置目录指南

部署后用户需要关心的文件只有 3 个：

| 文件 | 作用 | 何时改 |
|---|---|---|
| `../.env` | 凭据（API Key、SSH 跳板等） | **必改** — 部署前先 `cp .env.example .env` |
| `config.yaml` | 主配置（LLM / K8s / 观测后端 / Pipeline 行为） | 接入新观测后端或调参时改 |
| `clusters.yaml` | 故障注入目标（K8s 集群 + GPU 主机档案） | 多集群 / 切换 GPU 主机时改；**也可在 UI"管理目标"按钮里在线改** |

其余文件作用说明：

| 文件 | 作用 |
|---|---|
| `config_cluster.yaml` | 备用：跑在 K8s 节点上时的配置模板（默认不用） |
| `config_loader.py` | Python 加载器（dataclass schema），不要手改 |
| `domains/` | 故障域知识库（generic_linux / kubernetes）— 由系统读取，不需手改 |
| `precision_fault_scenarios.yaml` | 系统维护的细粒度场景目录；合同与真实执行都必须由服务端固定适配器重新解析，它本身不是命令开关 |

---

## 加载优先级

```
.env (环境变量)
  ↓ 替换 ${VAR_NAME}
configs/config.yaml   ←  主配置
                          └─ 若 fault_targets: 段缺失
configs/clusters.yaml  ←  fault_targets 段的默认值
                          └─ 若 data/cluster_profiles.json 存在
data/cluster_profiles.json  ←  UI 在线编辑写入的覆盖值（最高优先级）
```

修改 `clusters.yaml` 后**首次重启**才生效。如果 `data/cluster_profiles.json` 已存在，会优先使用 JSON 里的内容；想让 YAML 重新生效需要先删除该 JSON。

> 排障要点：前端"管理目标"保存过一次后，系统实际使用的是
> `data/cluster_profiles.json`，不是 `configs/clusters.yaml`。如果发现
> YAML 里已经补了 kubeconfig 但页面仍显示未配置，先到"管理目标"检查
> 持久化档案，或备份后删除 `data/cluster_profiles.json` 再重启。

---

## 常见配置场景

### 1. 只跑单集群 + 单 GPU 主机

把 `clusters.yaml` 里 `k8s_clusters` 留默认，`llm_hosts` 删到只剩一条即可。

### 2. 多 K8s 集群（dev / staging / prod）

为每个集群挂一个 kubeconfig 到容器，编辑 `docker-compose.yaml`：
```yaml
volumes:
  - ~/.kube/config:/root/.kube/config:ro
  - ~/.kube/staging-config:/root/.kube/staging-config:ro   # 新增
```
然后在 `clusters.yaml` 加一条：
```yaml
- id: staging
  name: "Staging 集群"
  kubeconfig: "/root/.kube/staging-config"
  context: ""              # 或填该 kubeconfig 里的 context 名
  default_namespace: "default"
  description: "staging 环境"
```

### 3. GPU 主机直连（无跳板）

`clusters.yaml > llm_hosts` 把 `jump_host` 留空即可：
```yaml
- id: local-a100
  name: "本地 A100"
  host: "10.0.0.50"
  ssh_user: "ubuntu"
  ssh_key_path: "/root/.ssh/id_ed25519"
  jump_host: ""            # 空 = 直连
  gpu: "NVIDIA A100 80G"
  role: "本地推理"
```

### 4. 换 LLM Provider（OpenAI / Qwen / Claude / 自建 vLLM）

系统调用模型走 **OpenAI 兼容协议**，任何兼容此协议的服务都能直接接。
`configs/config.yaml > llm:` 段头部有详细字段说明，紧随其后是 **4 套
provider 预设**（注释状态），按需取消注释、替换原 `llm:` 段即可。

**操作 3 步**：
1. 打开 `configs/config.yaml`，找到 `# ─── LLM Provider 预设 ───` 注释块
2. 选一个预设（OpenAI / 通义千问 / OpenRouter+Claude / 本地 vLLM），把
   它的 `llm:` 块解开注释并**删掉**默认的 DeepSeek `llm:` 块
3. 打开 `.env`，按预设里 `${XXX_API_KEY}` 占位的变量名填实际 key

示例：切到通义千问
```yaml
# configs/config.yaml
llm:
  api_key: "${DASHSCOPE_API_KEY}"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen3-max"
  temperature: 0.1
  max_tokens: 32768
  timeout: 300
```
```bash
# .env
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
```

重启容器：`./deploy_docker.sh` 或 `docker compose restart`。

也可以在 Dashboard 的“配置中心 → 大模型”填写上述参数并保存。保存时：

- `api_key` 仅写入服务器端持久化配置，不会通过接口明文回显；页面留空表示保持原值；
- 其余模型参数写入 `data/platform_config.json`，并立即重建后续请求使用的运行时配置；
- 启动时先读取 `configs/config.yaml` 与 `.env`，再应用 `platform_config.json` 的覆盖值；
- 点击“测试大模型”会使用当前有效配置发起真实请求，并回显模型名、耗时和响应；
- RCA/多智能体诊断、告警分析、故障模式总结分类及自愈建议均通过同一个 `LLMClient` 使用该配置。

生产或版本化发布应在 `.env` 中配置稳定的数据目录，避免切换 release
目录时丢失配置和运行记录：

```bash
AGENTICSRE_ENV_FILE=/var/lib/agenticsre/agenticsre.env
AGENTICSRE_DATA_DIR=/var/lib/agenticsre/data
AGENTICSRE_LOG_DIR=/var/lib/agenticsre/logs
```

`AGENTICSRE_ENV_FILE` 用于保存 API Key 等环境凭据；不要提交到 Git。其中
`platform_config.json` 采用原子替换写入且文件权限为 `0600`。故障模式
快照、激活版本和审计记录保存在同一数据目录下的 `fault_mode_versions/`。

> ⚠️ **`base_url` 末尾要带 `/v1`**（DeepSeek 除外，它的网关同时接受
> 带与不带）。代码用 OpenAI SDK 原样拼接 `/chat/completions`，不会
> 自动加 `/v1`。如果 401/404 多半就是这个原因。

### 5. 调整 RCA 六因子置信度

`config.yaml > confidence_calibration` 可调整以下六个因子的权重，以及证据不足、反证、人工复核等场景的硬上限：

| 配置字段 | 中文名称 | English |
|---|---|---|
| `raw_weight` | 校准前置信度 | Raw |
| `hypothesis_weight` | 假设分支稳定性 | Hypothesis |
| `evidence_weight` | 多源证据充分性 | Evidence |
| `quality_weight` | 诊断质量评审 | Quality |
| `specificity_weight` | 根因具体性 | Specificity |
| `history_weight` | 历史诊断正确性 | History |

示例：在保留默认总体权重的前提下，提高多源证据充分性（Evidence）权重并收紧单证据源上限：

```yaml
confidence_calibration:
  raw_weight: 0.15
  hypothesis_weight: 0.17
  evidence_weight: 0.32
  quality_weight: 0.16
  specificity_weight: 0.10
  history_weight: 0.10
  single_evidence_cap: 0.68
```

保护机制：

- 六因子权重与多源证据充分性（Evidence）子权重会自动归一化，总和填错不会导致置信度越界。
- 负数、NaN 或全 0 权重会被修正或回退到默认值。
- 实际权重、加权贡献、配置来源与 cap 触发项会写入 `confidence_explanation`。
- 建议先在回放数据上对比根因准确率、误拒绝率和自愈门禁命中率，再在生产环境修改。

修改后重启服务或执行配置重载才会生效。完整字段和默认值见 `configs/config.yaml`。

---

## 通过 UI 配置（推荐）

部署后打开 `http://<host>:8080`，**通算 / 智算故障实验** 页面顶部都有
"管理目标"按钮 — 增删改集群 / GPU 主机不需要重启容器，编辑保存后立即
写入 `data/cluster_profiles.json` 并热加载到内存。

适合：临时加一个测试目标 / 切换默认集群 / 一次性试验。

YAML 适合：版本化的基线默认值（部署到新环境时直接生效）。

---

## 细粒度故障目录不是执行开关

`precision_fault_scenarios.yaml` 登记 18 个场景和 12 种注入机制。它的作用是让
服务端根据版本化白名单生成强类型合同，而不是接收 shell、脚本、
`kubectl` 文本或临时执行器地址。顶层状态必须保持：

```yaml
execution_mode: approval_gated_transactional_execution
validation_status: remote_real_validation_17_passed_1_gpu_capacity_blocked
```

管理员不应绕过目录手工加入 `real`、`execute` 或命令字段。真实能力来自
`skill_runtime/precision_execution.py` 中服务端固定的场景—适配器映射，而不是某个
配置开关。真实实验必须使用 experiment API，并同时提交 `confirm=true` 和
`experiment_ack=true`；plan API 只生成合同，不执行故障。

场景目录与合同共同使用以下字段；`scope` 和 `match` 是否必填由注入机制决定：

| 字段 | 约束 |
|---|---|
| `id` / `target_entity_ids` | 只能使用服务端登记的场景和带类型精确目标 |
| `precision.method` | 必须是合同构建器允许的机制 |
| `precision.scope` / `precision.match` | 限定容器、进程、路径、请求或网络元组，不允许通配符扩散 |
| `precision.fault` | 只保留对应机制的强类型字段，不允许命令或脚本 |
| `safety` | 限制持续时间和命中次数，`auto_cleanup` 不能关闭 |
| `blast_radius_budget` | 限制最大影响实体、时间、命中次数和请求采样率 |

页面返回 `ready=true` 只表示合同校验成功。还必须同时看到
`real_execution_ready=true` 并通过适配器、环境、真实效果、Undo 和恢复验收，
才能将某个场景标记为可真实执行。当前 18 个场景均已绑定固定适配器；17 个完成
远端 F5 验收，DCGM 场景因无隔离 GPU 在运行时预检阶段阻塞。实施状态与 F0–F6 验收门见
`../docs/testbeds/precision-fault-injection-status.md`。

自愈校验不从目录或请求体生成新命令。它只接受服务端持久化的成功真实实验
`fault_run_id`，校验场景、集群、Namespace、目标、逐目标作用域和原
`agenticsre.precision.cleanup` 完全一致后，才允许幂等重放 Undo 并执行残留检查。

`clusters.yaml` 和 `data/cluster_profiles.json` 只负责目标定位。新增集群或 GPU 主机
不会改变细粒度场景的执行等级。

---

## 智算事件中心的 K8s Events 接入

智算事件中心会从三类来源生成事件：

1. vLLM / GPU / TELLER / Prometheus 指标；
2. 智算故障实验运行记录；
3. K8s Warning Events。

要让第 3 类生效，智算集群档案必须满足：

```yaml
- id: t4-vllm-k8s
  kind: "ai"
  kubeconfig: "/app/data/t4-admin.conf"      # 或通过 ${LLM_K8S_T4_KUBECONFIG} 注入
  context: "kubernetes-admin@kubernetes"    # 可为空，使用 kubeconfig 当前 context
  default_namespace: "llm-k8s"
```

页面上的"智算事件中心"会显示当前事件源状态：

- `K8s 未配置`：集群档案没有 kubeconfig/context；
- `K8s 连接失败`：kubectl 无法访问 API Server 或权限不足；
- `K8s 已连接无 Warning`：接入正常，但当前 namespace 没有近期 Warning；
- `K8s Warning N`：已读取到 Warning，会生成 `k8s_event` 智算事件。

只看到"无事件"不一定是故障。先看事件中心顶部的 K8s 状态，再用：

```bash
KUBECONFIG_PATH=/path/to/kubeconfig
KUBE_CONTEXT=context-name
TARGET_NAMESPACE=namespace-name
kubectl --kubeconfig "${KUBECONFIG_PATH}" --context "${KUBE_CONTEXT}" \
  -n "${TARGET_NAMESPACE}" get events --sort-by=.lastTimestamp
```

确认目标 namespace 是否真的有 Warning。
