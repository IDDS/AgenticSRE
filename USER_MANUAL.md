# AgenticSRE 用户手册

面向 **部署者 / 运维使用者** 的端到端操作指南。
开发/二次开发请看 `doc/technical_manual.md`，配置字段细节请看 `configs/README.md`。

---

## 目录

1. [系统能做什么](#1-系统能做什么)
2. [5 分钟部署](#2-5-分钟部署)
3. [首次配置](#3-首次配置)
4. [Dashboard 走查](#4-dashboard-走查)
5. [典型工作流](#5-典型工作流)
6. [运行模式](#6-运行模式)
7. [常见问题](#7-常见问题)
8. [进阶链接](#8-进阶链接)

---

## 1. 系统能做什么

AgenticSRE 是一个面向 **K8s 微服务** 与 **GPU 推理集群** 的多智能体运维平台。

| 你的角色 | 它能帮你 |
|---|---|
| **SRE / 值班** | 7×24 检测告警 → 自动 RCA → 出具诊断报告（无需手动跑 kubectl） |
| **架构师** | 跨指标/日志/链路/事件四路证据关联，避免单维度误判 |
| **研究人员** | 在 6 种多智能体协作范式（Chain / ReAct / Reflection / Plan-and-Execute / Debate / Voting）之间对比，跑基准评估 |
| **混沌工程** | 内置"通算 / 智算"两套故障实验室；常规场景支持受控注入与恢复，细粒度场景当前提供强类型 Dry-run 合同 |

部署后只有 **一个 Web Dashboard**（默认 `http://<部署机>:8080`）。命令行模式仅给后台自动化用。

---

## 2. 5 分钟部署

### 2.1 准备一台机器

- Linux x86_64（推荐 Ubuntu 22.04 / CentOS 8+）
- Docker ≥ 20.10、docker-compose plugin
- 4 vCPU / 8 GB RAM 起步
- 能访问 K8s API（`~/.kube/config` 可用）
- 能访问 LLM API（DeepSeek / OpenAI / 通义千问 / 本地 vLLM 任选）

> macOS 也能跑，但 `docker-compose.yaml` 用了 `network_mode: host`，
> macOS 上访问 `http://localhost:8080` 不通 — 这是 Docker Desktop 限制，
> 仅影响访问，容器内部正常。生产请用 Linux。

### 2.2 一键部署

```bash
# 1. 解包
tar xzf agenticsre-release-YYYYMMDD.tar.gz
cd agenticsre-release-YYYYMMDD

# 2. 准备凭据
cp .env.example .env
vim .env                  # 至少填 LLM_API_KEY

# 3. 先验证发布包和内置镜像，再起服务
./deploy_docker.sh --verify-only
./deploy_docker.sh
```

`deploy_docker.sh` 会：
1. 加载随包镜像 `agenticsre:latest`（无需联网构建）
2. 自动展开容器内挂载需要的源码目录
3. 缺 `~/.kube/config` / `~/.ssh` 时建占位避免起不来
4. `docker compose up -d --no-build`

默认部署不会执行 `docker build`，也不会重新下载 apt/pip 依赖。只有明确执行
`./deploy_docker.sh --build` 时才会联网重建镜像。

`--verify-only` 会检查 Docker/Compose、发布包目录、内置 Python 依赖和
`kubectl` 客户端，不启动 Web 服务。该步失败时不应继续部署。
加载后建议记录实际镜像 ID：

```bash
docker image inspect agenticsre:latest --format '{{.Id}}'
```

完成后浏览器打开：`http://<部署机IP>:8080`

### 2.3 可选启动 Skill Runtime

主 Web 容器内置 Agent+Skill fallback，即使不启动独立 Skill Runtime，告警中心的 Agent+Skill 分析也能运行。生产演示建议额外启动独立 Skill Runtime，便于隔离 Skill 执行和查看健康状态：

```bash
docker compose -f docker-compose.skill-runtime.yaml up -d --no-build
curl -sf http://localhost:8090/api/health || echo "SKILL_RUNTIME_FAIL"
```

默认 Web 容器通过 `SKILL_RUNTIME_URL=http://127.0.0.1:8090` 访问独立 runtime。未启动或访问失败时，系统会自动退回本地桥接执行，并在 RCA 事件中标记 `direct_bridge_fallback`。

### 2.4 验证

```bash
docker compose ps                   # 容器是 Up 状态
docker compose logs -f --tail=50    # 看启动日志，无 ERROR
curl -sf http://localhost:8080/api/health || echo "FAIL"
```

UI 上 "集群概览" 页能看到节点和 Pod 列表 = 一切就绪。

---

## 3. 首次配置

需要关心的文件只有 3 个，**都在解包后的目录里**：

| 文件 | 改什么 | 何时改 |
|---|---|---|
| `.env` | LLM API Key、SSH 跳板凭据 | **必改** |
| `configs/config.yaml` | LLM provider、Prometheus/ES/Jaeger 地址、Pipeline 行为 | 接入新观测后端 / 切 LLM 时改 |
| `configs/clusters.yaml` | 故障实验的 K8s 集群清单 + GPU 主机清单 | 多集群 / 切 GPU 主机时改（也能在 UI 上"管理目标"按钮里在线改）|

字段说明详见 `configs/README.md`。三个最常见的场景：

> `configs/precision_fault_scenarios.yaml` 是系统维护的细粒度场景目录，
> 不是部署时打开真实执行的开关，通常不应手工修改。

### 场景 A：换 LLM Provider

打开 `configs/config.yaml`，找到 `# ─── LLM Provider 预设 ───` 注释块，
里面有 OpenAI / 通义千问 / OpenRouter+Claude / 本地 vLLM 四套预设。
取消注释你要的那段，删掉默认 DeepSeek 段，再去 `.env` 填对应 KEY。

> ⚠️ **`base_url` 末尾要带 `/v1`**（DeepSeek 网关例外）。
> 代码用 OpenAI SDK 原样拼 `/chat/completions`，不会自动加。
> 401/404 错误大概率是这个原因。

### 场景 B：接入自己的 K8s 集群

直接把宿主机 `~/.kube/config` 挂进容器即可（`docker-compose.yaml` 已挂）。
多集群时编辑 `configs/clusters.yaml > k8s_clusters` 添加条目，
每条挂一份 kubeconfig 到容器内对应路径。

### 场景 C：接入自己的可观测后端

`configs/config.yaml > observability:` 段改 URL：

```yaml
observability:
  prometheus_url: "http://prometheus.your-domain:9090"
  elasticsearch_url: "http://es.your-domain:9200"
  jaeger_url: "http://jaeger.your-domain:16686"
  grafana_url: "http://grafana.your-domain:3000"
```

改后 `docker compose restart` 生效。

---

## 4. Dashboard 走查

左侧导航有 14 个页面，分 4 大类：

### 4.1 监控类（看现状）

| 页面 | 用途 |
|---|---|
| **集群概览** | 顶部 6 个统计卡 + 工作负载拓扑图（按节点/命名空间分组，异常高亮红色） |
| **指标监控** | Prometheus 自定义查询；右上"刷新"拉最新。配合 `config.yaml > detection.metric_checks` 看阈值 |
| **日志查询** | Elasticsearch 关键字 + 时间窗 + 服务过滤，搜近期日志 |
| **告警中心** | Prometheus AlertManager + K8s 事件合并视图。告警旁"→ RCA"按钮一键转诊 |
| **链路追踪** | Jaeger 链路浏览，按服务/操作/时长筛选 |
| **事件追踪** | K8s `kubectl get events`，按严重程度排序 |

### 4.2 智能分析类（让 Agent 干活）

| 页面 | 用途 |
|---|---|
| **根因分析** | 手动触发 RCA：填一段自然语言"问题描述"（如 *"test-social-network 命名空间 nginx-thrift Pod CrashLoopBackOff"*）→ 选范式 → 跑。出具 5 阶段诊断 + 报告 |
| **知识库** | 系统沉淀的诊断规则、历史故障案例、领域知识。可手动新增规则 |
| **故障报告** | 历史 RCA 的可视化报告，可导出 |
| **演化闭环** | 专家反馈入口：标"对/错"，系统下次同类故障会自学习 |

### 4.3 自动化类

| 页面 | 用途 |
|---|---|
| **守护进程** | 7×24 自动检测开关。开启后无需手动触发 RCA，达到阈值的告警自动跑 Pipeline |
| **Hermes 助手** | 对话式运维助手，自然语言查询集群状态 / 跑诊断 |

### 4.4 故障注入实验室

| 页面 | 用途 |
|---|---|
| **通算故障实验** | 对 K8s 容器/Pod/节点注入故障（CPU 满载、内存挤压、Pod kill、网络丢包等） |
| **智算故障实验** | 对 vLLM/GPU 推理服务开展 GPU、负载、服务和分布式链路故障实验；具体可执行等级见故障目录 |

**常规场景操作流程**：

1. 顶部选目标（通算选"集群"+namespace 覆盖；智算选 K8s 模式或裸机 GPU 模式）
2. 选场景（下拉列表，每个场景旁有描述卡片）
3. 保持 ☑ Dry-run，点 **注入**，核对目标、变更动作、安全预算和清理动作
4. 常规场景 Dry-run 返回 `guarded_plan.ready=true`，且目标预检、审批和基线采集都完成后，才可取消 Dry-run 进行真实注入
5. 观察故障效果和对照组；命中紧急中止条件时立即停止
6. 实验结束后点 **清理/恢复**，并确认无残留且业务回到基线

**细粒度场景操作流程**：

1. 进入 **通算故障实验 → 细粒度注入**，选择专用测试集群和实验 namespace
2. 确认页面显示 `18 场景 / 12 机制 / 18 适配器就绪`
3. 先点 **合同**，核对目标、`scope`、`match`、故障参数、时限、命中上限、Undo 和证据要求
4. 点 **环境预检**；任何 blocker 都必须先解决，不能绕过
5. 点 **预演**，确认交易阶段为 `preflight → baseline → inject → effect_verify → undo → recovery_verify → residue_check`
6. 真实实验前勾选 **确认真实执行**，点击 **真实闭环** 并完成二次确认
7. 只有页面返回 `succeeded`、`validation=PASS` 和 `cleanup_complete=true` 才算真实实验通过
8. 点 **自愈校验** 可复用该次成功实验持久化的原交易 Undo，执行 `detect → diagnose → heal → verify` 合同校验、幂等清理和残留检查；返回 `already_recovered` 表示原交易已恢复且重放校验通过，不表示当时存在仍活跃故障
9. 必要时使用 **强制清理** 并再次点 **残留检查**

| 细粒度返回字段 | 如何判读 |
|---|---|
| `guarded_plan.ready=true` | 强类型合同生成成功 |
| `guarded_plan.real_execution_ready=true` | 场景已绑定服务端固定适配器；仍需运行时预检 |
| `execution_mode=approval_gated_transactional_execution` | 真实执行受双确认和七阶段交易约束 |
| `preflight.ready=false` | 环境、组件、权限或隔离资源不满足，禁止执行 |
| `validation=PASS` / `cleanup_complete=true` | 目标故障、Undo、恢复和清理证据均通过 |
| `already_recovered` / `recovery.passed=true` | 只复用持久化原 Undo 的自愈校验通过，未扩大作用域 |
| `blocked` / `failed` | 目标、安全预算或合同校验未通过，立即停止 |

**目标管理**：右上角"管理目标"按钮 = 增删改集群和 GPU 主机，写入 `data/cluster_profiles.json`，**热加载无需重启**。

> 添加目标档案只负责定位被测环境；真实执行仍必须通过固定适配器、环境预检和双确认。

---

## 5. 典型工作流

### 工作流 1：值班发现告警 → 双链路分析

1. 进 **告警中心**，看到 nginx-thrift CPU > 95% 告警
2. 点告警行右侧 **分析**，弹出"告警分析与自愈"
3. 在 **分析分支** 中选择：
   - **启动智能体分析**：走原多智能体 RCA，展示假设、证据、阶段事件和最终结论
   - **启动 Agent+Skill 分析**：由 Agent 调用 `skill_runtime_workflow`，展示主 Skill、命中数、置信度、detect/diagnose/heal/verify 阶段和 Skill 执行过程
4. 在 **自愈分支** 中可分别生成智能体自愈或 Agent+Skill 自愈方案，默认都是 dry-run，不会直接改集群
5. 若告警已从活跃列表消失，系统只展示历史处理状态，不会再触发新的分析或自愈动作

### 工作流 2：跑一次通算混沌实验

1. 进 **通算故障实验**
2. 选择专用测试集群，namespace 填被测对象的隔离命名空间，例如 `test-social-network`
3. 场景选 `sn-cpu-stress`（社交网络 CPU 压测）
4. ☑ Dry-run → 点注入 → 核对精确目标、动作、安全预算和清理计划
5. 确认该场景来自常规下拉列表、Dry-run 返回 `guarded_plan.ready=true`，并完成审批、基线采集和权限预检后，再取消 Dry-run 注入
6. 同时观察目标、对照组和安全中止指标
7. 实验完点 **清理**，确认负载、业务 SLO 和故障资源均已恢复

### 工作流 3：生成一个细粒度受控合同

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"dry_run":true,"namespace":"agenticsre-faultlab"}' \
  http://localhost:8080/api/faultlab/precision/plan/precise-network-loss \
  | python -m json.tool
```

预期 `status` 为 `planned`，`guarded_plan.ready` 和
`guarded_plan.real_execution_ready` 均为 `true`，并能看到固定适配器、精确 Undo 与
恢复验证字段。这个工作流只检查合同，不会注入真实故障；真实执行还必须通过
preflight 和双确认。

### 工作流 4：验证 Agent+Skill 路径

```bash
# 触发一次 Skill Runtime 工作流，默认 dry-run
curl -s http://localhost:8080/api/skill-workflows/operate \
  -H 'Content-Type: application/json' \
  -d '{
    "domain": "k8s",
    "stages": ["detect", "diagnose", "heal", "verify"],
    "dry_run": true,
    "payload": {
      "namespace": "default",
      "fault_type": "service_unavailable",
      "message": "service frontend returns 503 because no ready endpoints",
      "service": "frontend",
      "deployment": "frontend"
    }
  }' | python -m json.tool
```

预期输出中 `skill_runtime.primary_skill_id` 为 `k8s.service_unavailable`，`diagnosis.fault_type` 为 `ServiceUnavailable`。

### 工作流 5：评估多个范式哪个更好

```bash
# 在宿主机跑评估脚本（不走 UI）
docker exec -it agenticsre bash
cd /app && python -m eval.benchmark_runner --paradigms all --suite social-network
# 输出 eval/results/comparison_report.md
```

---

## 6. 运行模式

通常只用 **Web UI** 模式（默认起）。命令行模式给 CI / 后台任务用：

```bash
# 进容器
docker exec -it agenticsre bash

# 1. 守护模式：7×24 后台轮询，发现就自动 RCA
python main.py --mode daemon

# 2. 单次 Pipeline：跑一次 5 阶段
python main.py --mode pipeline

# 3. 单次 RCA：基于自然语言查询
python main.py --mode rca --query "pod CrashLoopBackOff in namespace default"

# 4. 看守护进程状态
python main.py --mode status
```

行为参数全在 `configs/config.yaml > runtime / daemon / pipeline` 段调。

---

## 7. 常见问题

### Q1: UI 打不开 / 502

```bash
docker compose ps                       # 容器是否 Up
docker compose logs --tail=100          # 看最后报错
docker exec agenticsre curl -sf localhost:8080/api/health   # 容器内自测
```
- macOS 上访问 host 端口不通是 Docker Desktop 限制，换 Linux。
- 容器内自测通但宿主访问不通 = 端口被占用或防火墙。

### Q2: 跑 RCA 报 `401 Unauthorized` 或 `404 Not Found`（LLM 调用失败）

- 检查 `.env` 里 `LLM_API_KEY` 是否填对
- 检查 `configs/config.yaml > llm.base_url` 是否带 `/v1`（DeepSeek 除外）
- 检查 `llm.model` 是否是该 provider 支持的模型 ID

### Q3: 集群查不到 / Pod 列表为空

- 检查 `~/.kube/config` 是否挂进了容器（`docker exec agenticsre kubectl get nodes` 自测）
- 跨集群时确认 `configs/clusters.yaml` 的 kubeconfig 路径在容器内能访问到

### Q4: 故障实验注入失败 `permission denied (publickey)`

- 智算"裸机 GPU 主机"模式需要容器能 SSH 到目标机
- 确认 `docker-compose.yaml` 已挂载 `~/.ssh:/root/.ssh:ro`
- 确认目标机 `~/.ssh/authorized_keys` 含部署机的公钥
- 跳板机模式需保证跳板机也信任部署机

### Q5: 细粒度真实闭环为什么显示 blocked

先查看 **环境预检** 的 blocker。18 个场景都已绑定固定适配器，但真实执行仍可能因
集群不可达、CRD/权限缺失、目标漂移或没有独占资源而阻塞。例如 DCGM 场景在没有
隔离 GPU 时必须保持 blocked。不要通过改 YAML、关闭审批或共享现有 GPU 绕过。
逐项状态见 `docs/testbeds/precision-fault-injection-status.md`。

### Q6: Agent+Skill 一直显示等待工具事件

- 先查 RCA run 是否有 `skill_runtime` 事件：

```bash
curl -s http://localhost:8080/api/rca/replace-with-run-id | python -m json.tool
```

- 如果没有事件，检查 Web 容器日志里是否有 `skill_runtime_workflow` 报错。
- 如果独立 Skill Runtime 没启动，系统会自动走本地 fallback；这不是错误，但需要确认 `skill_runtime/` 目录已被挂载到 Web 容器。
- 如果页面没有刷新，浏览器强制刷新一次，确认 `app.js?v=102` 或更新版本已加载。

### Q7: 配置改了不生效

| 改了什么 | 怎么生效 |
|---|---|
| `.env` | `docker compose restart` |
| `configs/config.yaml` | `docker compose restart` |
| `configs/clusters.yaml` | `docker compose restart`（且 `data/cluster_profiles.json` 不存在；若已存在以 json 优先，需先删它） |
| UI"管理目标"按钮改的 | 立即生效（写入 `data/cluster_profiles.json` 热加载） |

### Q8: 想完全重置

```bash
docker compose down -v
rm -rf data/cluster_profiles.json data/memory data/evolution
./deploy_docker.sh
```

⚠️ 这会清掉所有学习到的规则和历史报告，谨慎。

### Q9: 被测对象教程是否都能从空集群直接部署

不能。`docs/testbeds/README.md` 使用 D0–D3 标识可执行边界：

- D0 是架构/接口合同，不是部署包；
- D1 已核对上游路径，但仍需站点补丁、固定镜像或完整清单；
- D2 可进入专用测试集群部署验收；
- D3 只代表已在文档声明的特定拓扑验证。

只有 D2/D3 且本地预检通过时才能进入集群部署；新站点仍要通过
对应文档的业务、可观测、恢复和清理验收门。

---

## 8. 进阶链接

| 你想 | 看哪里 |
|---|---|
| 看每个配置字段什么意思 | `configs/README.md` |
| 改 Pipeline 行为 / 加新观测后端 | `configs/config.yaml` 内注释 |
| 部署通算或智算被测对象 | `docs/testbeds/README.md` |
| 查看详细故障类型、可执行性和验证方法 | `docs/testbeds/fault-catalog.md` |
| 核对 18 个细粒度场景的实施完成度 | `docs/testbeds/precision-fault-injection-status.md` |
| 了解受控注入、Undo 和自愈门禁 | `docs/guarded_fault_injection_and_recovery.md` |
| 了解系统架构 / 二次开发 | `doc/technical_manual.md` |
| 接入 Claude/Copilot 当 MCP 工具 | `mcp_server.py` 头部 + 技术手册 §11 |

---

> 反馈 / 问题：联系研发团队，或在 `data/expert_feedback/` 留底，
> 演化闭环页可见。
