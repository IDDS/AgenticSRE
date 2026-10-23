# AgenticSRE 生产容器更新记录（2026-08-06）

## 结论

2026-08-06 已在远端 `snail` 部署 GitCode 分支 `feature/skill-runtime` 的提交 `677376e0e73d84bb19a60b66df7dd7f902be14a5`。Dashboard 与 Skill Runtime 均运行镜像 `agenticsre:677376e`，镜像 ID 为 `sha256:661bb876bc350c9d86db6b1eae9d19b49e81c9b8f27bf1ef9aed6b70ab421c9d`，Docker 健康状态均为 `healthy`。

本次容器更新及随后执行的智算链路修复均已验收通过：AgenticSRE 服务正常，智算集群两台节点 Ready，`vllm-large` 和 `vllm-server` 均可用。原 `vllm-server` 的 `FailedMount` 已通过 ConfigMap 文件挂载修复；生产机新增 systemd 管理的 30080 SSH 隧道，使容器内配置的 `127.0.0.1:30080` 可稳定访问集群 NodePort。

## 发布与回滚

- 发布目录：`/home/ubuntu/AgenticSRE-releases/677376e`
- 发布镜像：`agenticsre:677376e`
- Dashboard 回滚镜像：`agenticsre:rollback-dashboard-20260806`，镜像 ID `sha256:ce91338f6f6b6b59b8de29f90cc94022605afdf737a4532ec09e3369787be4b4`
- Skill Runtime 回滚镜像：`agenticsre:rollback-skill-20260806`，镜像 ID `sha256:73c5e3de2a15f2f4d5f14fb61a081de66f73bc4508b15c41de1f006411777a19`
- 生产数据、日志、集群配置和认证配置均沿用原生产目录，未迁移或重建。
- 更新顺序为 Skill Runtime 后 Dashboard；两个服务分别健康后才继续下一步。
- `vllm-server-agenticsre-teller-patch` ConfigMap 使用脚本 SHA-256 `fb094aafef78549cfe513a8f2827a701875e483838f37ad0f90a27892031afbe`，以 `subPath` 挂载到 `/tmp/patch_teller_vllm_request_markers.py`。
- `agenticsre-ai-vllm-tunnel.service` 已启用并运行，负责 `127.0.0.1:30080 -> 33.33.33.128:30080`，断线自动重连。

## 验证证据

| 检查项 | 结果 |
| --- | --- |
| Dashboard `GET /api/health` | `status=ok`，`llm_configured=true` |
| Skill Runtime `GET /api/health` | `status=ok`，加载 61 个 Skill |
| Dashboard RCA 历史 | 更新前后的最近一条记录 ID 均为 `rca-c256afc2`，共享数据未丢失 |
| Dashboard 首页、集群与 Skill 列表 | HTTP 200，生产配置可读取 |
| 诊断融合真实调用 | 排除 `generic.risk_gatekeeper` 辅助候选，选择 `ai.endpoint_timeout`，输出置信度 `0.82` |
| Dashboard / Skill Runtime 镜像 | 均为 `sha256:661bb876bc350c9d86db6b1eae9d19b49e81c9b8f27bf1ef9aed6b70ab421c9d` |
| Docker 健康检查 | 两个容器均为 `healthy` |
| 通用集群节点 | 4/4 Ready；存在四个持续 28 天的演示故障 Pod，不是本次更新产生 |
| T4 智算集群节点 | 2/2 Ready |
| T4 默认目标 `vllm-server`（修复前） | 0/1 Available，Service 无 Endpoint，NodePort 30080 不可达 |
| T4 可用模型服务 `vllm-large` | 1/1 Available，Service 有 Endpoint `10.244.1.109:8000` |
| T4 修复后 `vllm-server` | 1/1 Running，Endpoint `10.244.1.110:8000`，`/health=200` |
| T4 模型 API | `/v1/models` 返回 `qwen2.5-coder-3b-k8s`；真实 `/v1/completions` 返回 HTTP 200 |
| AgenticSRE 容器访问 T4 vLLM | Dashboard 与 Skill Runtime 容器内均能通过 `127.0.0.1:30080/v1/models` 读到目标模型 |
| vLLM 隧道托管 | systemd `enabled/active`，本地端口 30080 监听正常 |

容器内关键文件 SHA-256：

```text
9731661830e0cb16e9f149646873ca26e49d764c2c46a9336e88e1d7b795156a  /app/orchestrator/skill_agent.py
e7257d3d47b7e0460473028c2060d57cc425c836d006921ace2e11abb8e2828c  /app/web_app/app.py
0a284d97ea733b627a180c26af10d9f28974ad95801e05ed4a5b967452b7d295  /app/orchestrator/rca_engine.py
3bde7505b60647481f799d6b94d49a511a963db799d106d32df7e2cb7b7c5ede  /app/remediation_history.py
1b0d223f1ca2ca863b8d0f8541618c4292ca798d444a6c32a391de387000bfdb  /app/confidence_statistics.py
```

## 修复过程与后续门禁

`vllm-server` 的直接失败原因为其 HostPath 卷声明要求 `/tmp/patch_teller_vllm_request_markers.py` 是文件，但 `dds-128` 节点上该文件不存在。已改为 ConfigMap + `subPath` 挂载，删除遗留 Failed Pod，并确认 Deployment 1/1、Service Endpoint 非空、`/health` 可达和真实 completion 成功。首次冷启动从 NFS 读取 5.8 GiB 权重约 17 分钟；这是基础设施存储吞吐限制，不是容器崩溃。

TELLER 仍记录两类非阻断告警：镜像未发现 NVTX injection `libcupti.so`，以及 vLLM usage worker 的 `cpuinfo` 上报 JSON 异常；模型 API 与 AgenticSRE 请求链路均未受影响。后续如需完整 GPU/CUPTI 证据，应单独补齐镜像库路径和 usage worker 配置，不应通过关闭 TELLER 或修改模型命令绕过。

生产机上的 vLLM SSH 隧道现由 systemd 托管；若 `systemctl is-active agenticsre-ai-vllm-tunnel` 非 `active`，不得执行依赖 `vllm-server` 的真实故障注入或自动恢复验收。

远端根分区当前使用率为 94%，剩余约 8.3 GiB。后续构建镜像前应先检查空间，但不得在没有核对回滚标签和发布目录的情况下清理镜像。
