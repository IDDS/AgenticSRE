# 系统复查修复：远端发布与验收

## 本次执行结果

验收主机为 `snail`（`lsy-0`），以下运行结果均来自远端。

- 修复代码提交：`997f860`、`5be6fb6`。修复执行边界、恢复误报、模板证据覆盖
  拒答、OOM 否定句、指标单位/ECC、Prometheus 执行状态、草案门禁和评分字段混用。
- 远端 Python 3.12 容器回归：**508 passed，3 warnings，94.84 秒**；运行时
  `--network none`，测试数据和日志使用临时挂载。
- 生产两个服务已更新至 `agenticsre:review-20260915`，均 healthy；容器内修复文件
  SHA-256 与提交源码一致。`agenticops` 两个容器的 ID 和启动时间保持不变。
- 远端真实模型测试最初返回 401；用户提供新凭据后通过配置中心更新，并确认
  `deepseek-v4-pro` 返回 `AGENTICSRE_LLM_OK`。凭据不进入仓库或安装包。
- 真实多智能体 scale-to-zero 诊断 `rca-077a9d21` 超过 720 秒，验收失败；测试
  命名空间随后清理。任务在截止时间后完成，结论倾向副本为 0，但明确承认没有
  直接读取 Deployment/Endpoints；不能作为根因已验证的成功样本。模型可连接与
  容器健康不等于端到端诊断通过。
- 独立 Agent+Skill 服务故障 `rca-a385e728` 通过：真实读取指定命名空间的
  Deployment JSON、Service 和 Endpoints，模型选择 `k8s.service_unavailable`；
  输出“无可用就绪副本或 endpoints 为空”，分数 0.72，真实 LLM 调用成功。
  恢复由验收脚本显式执行，随后副本和业务探针检查通过；不计为自主自愈成功。
- 独立 Agent+Skill 镜像故障 `rca-392e6ee8` 通过：选择 `k8s.image_pull_error`，
  根据真实 Pod 状态定位不存在的镜像标签及 ImagePullBackOff，分数 0.716，真实
  LLM 调用成功。脚本恢复有效镜像后 rollout 通过；两个场景的最终汇总为 passed，
  测试命名空间清理已验证。只验收这两个 Skill 场景，不外推全库有效性。
- 严格离线 `.run` 在远端 `--network none`、无 systemd、初始无 Docker/Python/Git
  的目标容器内安装通过，验证数据库、备份恢复和重启。Docker 29.1.3，Compose
  5.5.0，overlay2。**本轮未重新验证 systemd 模式。**
- 首次离线验收因缓存目标镜像内旧脚本硬编码 vfs 耗尽空间而失败；修复验收入口，
  始终只读挂载当前脚本，第二次验收通过。未将第一次失败计为通过。

离线交付：`agenticsre-offline-20260915-r1-linux-amd64.run`。
SHA-256：`4d260a3636780005a75e397d0bf054ee62df6edd9f6ab6761a4bc558c457dc60`。
远端持久路径：`/home/ubuntu/agenticsre/release/`；本地取回目录：`release/`。
详细证据归档：`outputs/system-review-20260915/`。

本次修复不等于 61 个 Skill 全部完成真实故障验收。草案发布仍为人工计划，
审批证据尚未绑定内容哈希；评估器是分字段词汇规则，不能称为语义准确率。

## 同机资源清理

业务 Docker 中 6 个容器均在运行，无已停止的闲置容器。本次保留全部业务容器、
镜像和数据卷，仅清理未使用构建缓存 2.533 GB；根盘可用空间由保存新安装包后的
约 1.5 GB 增至约 3.9 GB。未挂载数据卷约 2.62 GB，未获知其数据用途，予以保留。
验收临时容器已自动删除；专用 dockerd/containerd 已停止，确认无残留挂载后删除
本次准确命名的 tmpfs 目录，释放约 5 GB 内存盘。安装包及成功/失败证据先行归档。

## 发布前核实的生产状态

2026-09-15 通过只读 SSH 核实：snail 根盘剩余约 2.2 GB；`agenticsre` 和
`agenticsre-skill-runtime` 使用 `agenticsre:677376e`，镜像约 1.08 GB。
Compose project 均为 `agenticsre`，配置位于
`/home/ubuntu/AgenticSRE-releases/677376e/`。

旧 release 的 `data`、`logs`、`hermes-agent` 是符号链接，分别指向
`/home/ubuntu/agenticsre/data`、`/home/ubuntu/agenticsre/logs`、
`/home/ubuntu/agenticsre/hermes-agent`。应用代码还有大量 bind mount。
发布时应保留生产配置、凭据和这些稳定数据位置。

## 发布步骤

1. 将最终提交的源码传到新的 release 目录。在远端现有依赖镜像中，以
   `--network none`、临时 `/app/data` 和新源码运行回归测试；不挂生产数据。
2. 若依赖没有变化，可基于已验证的生产镜像添加应用源码层，生成带提交号的
   不可变镜像。确认所有新根模块均进入镜像，且生产 bind mount 不会覆盖修复。
   不要将旧版生产 `configs` 或凭据复制进可交付镜像。
3. 生成两个服务的 Compose 配置，显式使用新镜像、生产 `.env`、稳定 data/logs
   和原有凭据挂载。保留旧镜像和旧 release 作为回退点。
4. 仅对 `agenticsre` project 中的两个明确服务执行 `up --no-build --pull never`。
   不执行全局 prune、`down` 或 `--remove-orphans`；不修改 `agenticops` 实例。
5. 检查两个服务健康、修复模块哈希、保存的业务配置和只读 API。失败时恢复旧
   Compose/镜像，而不替换或回滚共享业务数据。

远端代码和健康验收脚本：

```bash
python3 scripts/verify_deployed_review_fixes.py \
  --baseline outputs/system-review-20260915/deployment-before.json \
  --output outputs/system-review-20260915/deployment-after.json
```

该脚本通过 SSH 在两个运行容器内计算 SHA-256，并核对健康状态及所有名称含
`agenticops` 的容器 ID、启动时间。默认检查四个核心修复文件；`--file` 可重复传入
完整发布文件清单（传入后替代默认清单）。脚本不执行业务写操作或读取环境变量。

## 低磁盘空间下的严格离线包

旧生产镜像加源码层不自动等于严格离线镜像。交付镜像仍需通过
`build_offline_bundle.sh` 的工具、Hermes、ChromaDB 模型缓存检查，并通过实际 `.run`
安装器的断网安装验收；普通 `package_release_from_image.sh` 不能代替此验收。

远端独立 Docker daemon 可将 `data-root` 放到专用 `/dev/shm` 子目录，且分别指定
socket、pidfile、exec-root，禁用 bridge、iptables 和 IP masquerade。不可共用生产
containerd socket。先验证存储驱动支持：`vfs` 会重复复制镜像层，可能超过 tmpfs
容量；发布前应按实际镜像大小、构建缓存和内层 daemon 存储计算峰值。

外层构建命令使用明确的 `DOCKER_HOST`，不得将其传入内层安装目标。优先通过
`E2E_NO_SYSTEMD=1` 和 `AGENTICSRE_E2E_DOCKER_DATA_DIR` 运行现有隔离测试，避免
默认 systemd 测试对宿主 `/sys/fs/cgroup` 的读写挂载。完整两模式验收是否通过
必须分别记录，不得以单一模式结果代替。

E2E 的目标镜像 Dockerfile 有 apt 安装步骤；独立 daemon 禁用 bridge 后需要预载
目标镜像并复用，或仅构建阶段明确使用可联网配置。目标运行阶段仍为
`--network none`。临时目录、镜像和 daemon 只能按本次任务的准确标识清理。

禁止在根盘先落地完整镜像 archive 再复制。`build_release_from_remote_image.sh`
当前会这样做；低空间环境应将 `docker save` 输出流直接发送到有空间的位置，或
全部在已核算容量的 tmpfs 工作区完成，并及时将最终产物、校验和与验收证据取回。
