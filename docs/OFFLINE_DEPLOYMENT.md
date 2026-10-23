# AgenticSRE 客户现场离线一键部署

本文面向无互联网、可能没有 Python、pip、Git、Docker 或 Docker Compose
的 Linux 客户现场。交付流程分为两个安全边界：

1. 在联网构建机生成并验收离线交付包；
2. 将一个可执行 `.run` 文件和客户配置送入现场，只运行一条安装命令。

客户现场脚本不会调用 `apt`、`yum`、`dnf`、`pip`，不会拉取镜像，也不会
访问互联网。应用源码、Python 依赖、kubectl、Hermes Agent 和 Skill Runtime
均已包含在镜像中。数据库运行库也随镜像提供：当前版本使用 ChromaDB（本地 SQLite 元数据）和 JSON 状态文件，不要求现场另行安装 PostgreSQL/MySQL。

## 一、支持范围与现场前置条件

- 操作系统：64 位 Linux；systemd 可选，缺少 systemd 时首次部署使用包内 dockerd 直接启动；
- CPU：本次客户交付为 `x86_64/amd64`，交付包必须与客户机匹配；
- 内核：建议 Linux 5.4 及以上，并启用 cgroup、namespace、overlayfs；
- 权限：安装 Docker、写入运行目录时需要 root；
- 端口：Dashboard 使用 `8080`，Skill Runtime 使用 `8090`；
- 容量：至少预留“镜像压缩包大小 × 3 + 5 GiB”的可用磁盘；
- 客户网络：如需大模型能力，必须能访问客户内网的 OpenAI 兼容模型服务；
  物理隔离环境不能把地址配置成互联网 DeepSeek API。
- 数据库：无需外置数据库；如客户已有 PostgreSQL，`database.dsn` 仅作为后续外部存储扩展点，当前版本不会自动连接或修改它。

如客户禁止安装 Docker Engine，应由客户提前提供可用 Docker Engine。脚本会
复用已有 Docker；检测到 Docker 命令但 daemon 异常时会停止，不覆盖客户安装。

## 二、在联网构建机生成交付包

构建机必须安装 Docker，并能访问基础镜像、Python 软件源、Docker 官方静态
二进制站点、GitHub Docker Compose 发布页和 Kubernetes kubectl 下载站点。
受控网络可通过 `PYTHON_BASE_IMAGE`、`PIP_INDEX_URL`、`PIP_TRUSTED_HOST`
指向华为内网镜像仓和 Python 制品库；这些仅影响联网构建机，客户现场不访问它们。

### x86_64 客户机

```bash
TARGET_ARCH=amd64 VERSION=20260827 ./build_offline_bundle.sh
```

### ARM64 客户机

```bash
TARGET_ARCH=arm64 VERSION=20260827 ./build_offline_bundle.sh
```

输出（主交付物）：

```text
release/agenticsre-offline-20260827-linux-amd64.run
```

`.run` 是自解压单文件安装器，内部已经附带完整 `.tar.gz` 离线包；客户现场
不需要手工解压。`.tar.gz` 仅作为备用介质和审计副本，同时生成各自的 `.sha256`
校验文件。

交付包包括：

- 自包含的 AgenticSRE 镜像；
- Docker Engine 静态二进制；
- Docker Compose 插件；
- 客户现场安装脚本；
- Compose 运行定义与客户配置模板；
- `SHA256SUMS` 完整性清单、整包 `.sha256` 和 `BUNDLE.env` 严格离线元数据。
- 数据库校验器（检查 ChromaDB/SQLite `quick_check` 和 JSON 可解析性）。
- `SOFTWARE_MANIFEST.md`、`python-packages.lock`、`os-packages.tsv` 与镜像元数据，
  用于现场核对所有实际打入镜像的软件版本。

构建脚本使用 Docker 官方静态包和 Compose 官方发布资产；Compose 的官方
`.sha256` 文件会在下载时校验，Docker 静态下载端点不提供侧车摘要，构建输出并记录其 SHA-256。生成整包
SHA-256 清单。Docker、Compose 和 kubectl 版本均可通过同名环境变量冻结。在受控 CI 中可设置 `DOCKER_SHA256` 使用独立摘要固定 Docker 包。

若镜像由受控 CI 或另一台目标架构构建机提供，可设置：

```bash
IMAGE_REPOSITORY=agenticsre-offline \
VERSION=20260827 TARGET_ARCH=amd64 SKIP_IMAGE_BUILD=1 \
./build_offline_bundle.sh
```

此时本机必须已存在与最终 `IMAGE_REF` 完全相同且架构正确的镜像。

## 三、准备客户配置

解压交付包后复制模板：

```bash
cp config/customer.env.example customer.env
chmod 600 customer.env
```

至少修改：

```dotenv
LLM_API_KEY=<客户内网模型服务凭据；不使用模型时留空>
LLM_BASE_URL=http://<客户内网模型网关>:8000/v1
LLM_MODEL=<该网关真实模型 ID>

AGENTICSRE_AUTH_ENABLED=true
AGENTICSRE_AUTH_USERNAME=<现场管理员账号>
AGENTICSRE_AUTH_PASSWORD=<强密码>
AGENTICSRE_AUTH_TOKEN=<长随机 API Token>
```

安装器会覆盖配置中的镜像名和持久化路径，防止配置文件引用错误 release。
模型配置、Web 登录配置和现场凭据会保留。配置文件不会明文回显 API Key。

将客户 Kubeconfig 放入安装后的：

```text
/var/lib/agenticsre/kube/config
```

将访问裸机/GPU 目标所需的 SSH 私钥和 `known_hosts` 放入：

```text
/var/lib/agenticsre/ssh/
```

## 四、客户现场一键部署

```bash
sudo ./agenticsre-offline-20260827-linux-amd64.run
```

这就是完整的一键安装命令：`.run` 自动解包并调用包内安装器，安装结束后自动
清理临时目录。不传配置时会生成随机管理密码并保持 LLM 禁用。如已准备
`customer.env`，直接追加参数：

```bash
sudo ./agenticsre-offline-20260827-linux-amd64.run --config ./customer.env
```

若不提供 `--config`，安装器会生成随机 Web 管理密码并打印一次，但 LLM 功能
保持未启用。建议交付前准备客户配置，避免现场手工录入。

安装器执行以下步骤：

1. 检查 Linux/CPU 架构，验证全部 SHA-256；
2. 复用现有 Docker，或从包内静态二进制安装 Docker 与 Compose；
3. 创建 `/opt/agenticsre` 与 `/var/lib/agenticsre` 稳定目录；
4. 加载镜像并再次校验镜像架构；
5. Compose 文件固定 `pull_policy: never`，命令行再次使用 `--no-build --pull never`
   启动 Dashboard 和 Skill Runtime；
6. 在容器内部检查 `8080/api/health` 与 `8090/api/health`；
7. 安装 `agenticsrectl` 管理命令；
8. 对 `/var/lib/agenticsre/data` 执行一次只读数据库与 JSON 完整性检查；
9. 写入 `/var/lib/agenticsre/offline-install-audit.env`，记录包版本、镜像来源、
   拉取策略、镜像摘要、健康检查和持久化检查结果。

包内 Docker 在存在可用 systemd 时会注册并启用开机自启；如现场没有 systemd 或其处于
降级状态，但 Linux 内核仍允许运行 Docker，安装器会使用包内 dockerd 直接完成首次部署，
日志落到 `/var/log/agenticsre-docker.log`。无 systemd 主机需要由客户自己的启动编排在
重启后托管 dockerd；这不影响断网首次安装。

## 五、持久化与升级

默认目录：

| 内容 | 路径 |
|---|---|
| 运行配置与 API Key | `/var/lib/agenticsre/config/agenticsre.env` |
| 配置中心、记忆、实验与故障模式版本 | `/var/lib/agenticsre/data` |
| 日志 | `/var/lib/agenticsre/logs` |
| Kubeconfig | `/var/lib/agenticsre/kube/config` |
| SSH 凭据 | `/var/lib/agenticsre/ssh` |
| 版本元数据与 Compose 文件 | `/opt/agenticsre/releases` |

`data/memory/chromadb/chroma.sqlite3` 是记忆库的 SQLite 文件；其余 `*.json`
保存 RCA、专家反馈、故障实验、自愈和演化记录。镜像升级不会覆盖这些目录。

### 数据库校验、备份与恢复

```bash
# 只读检查 SQLite quick_check 与所有 JSON 文件
sudo agenticsrectl --data-check

# 停止服务写入，生成 data + config 的一致性备份及 .sha256 侧车文件
sudo agenticsrectl --backup /secure-backup/agenticsre-20260827.tar.gz

# 校验备份路径、解压内容和数据后再替换；失败会自动回滚
sudo agenticsrectl --restore /secure-backup/agenticsre-20260827.tar.gz
```

备份只包含 `config/` 和 `data/`，不包含镜像层、日志、Kubeconfig 或 SSH 私钥。
恢复前脚本会在 `backups/pre-restore-<时间>.tar.gz` 保存当前数据；恢复后的
校验或健康检查失败时，原数据自动恢复，失败版本另存为
`failed-restore-*` 供审计。建议把备份文件和 `.sha256` 一并转移到受控介质。

重复运行新版本安装包属于原地升级：新镜像和 release 元数据单独保存，配置与
数据不覆盖。成功启动后 `/opt/agenticsre/current` 指向新版本；新版本健康检查
失败时，安装器会在停止旧版写入后自动生成数据快照，并同时恢复旧 release 指针、
升级前运行配置和数据库。新版本产生的失败数据会移入
`/var/lib/agenticsre/failed-upgrade-*`，供现场审计；快照和失败 release 不会自动删除。
升级前仍建议另行执行 `sudo agenticsrectl --backup /secure-backup/<file>.tar.gz`，并保存配套的 `.sha256` 文件。

## 六、管理与排障

```bash
sudo agenticsrectl --status
sudo agenticsrectl --restart
sudo agenticsrectl --stop
docker logs --tail 200 agenticsre
docker logs --tail 200 agenticsre-skill-runtime
```

只校验交付包而不修改现场系统：

```bash
./install-offline.sh --verify-only
```

`--verify-only` 只校验交付包，不需要 Docker daemon；`--data-check`、`--backup`
和 `--restore` 是已安装服务的数据操作，需要 root 与可用 Docker。

如端口冲突，先由客户确认占用者，禁止现场脚本直接终止未知服务：

```bash
ss -lntp | grep -E ':(8080|8090)\b'
```

正式交付统一使用发布门禁脚本。它先生成 `.run` 单文件安装器，再把精简 Linux/systemd 靶机
切换为 `--network none`，执行真实安装、Docker 启动、双服务健康检查、数据库
校验、备份恢复与重启；任何一步失败都不会输出 PASS：

```bash
VERSION=20260827 TARGET_ARCH=amd64 ./release_offline_bundle.sh
```

该测试需要验收机 Docker 支持特权容器和 cgroup/systemd，不应在客户生产主机
运行。验收记录输出到单独结果目录，测试靶机退出后自动销毁。由于
Docker-in-Docker 不能再次使用宿主 overlayfs，验收脚本会设置
`AGENTICSRE_DOCKER_STORAGE_DRIVER=vfs`；物理 Linux 客户机不应设置该变量，由 Docker
自动选择 overlay2。

## 七、安全边界

- 不自动关闭 SELinux、防火墙或系统安全策略；
- 不覆盖客户已有 `/etc/docker/daemon.json`；
- 不覆盖异常的已有 Docker 安装；
- 不自动执行真实故障注入或自愈；
- 默认开启 Web 登录认证；
- 配置文件权限为 `0600`、SSH 目录权限为 `0700`；
- 默认 Docker 配置关闭容器桥接和 iptables 修改，因为服务使用 host network；
- 升级失败时保留容器、日志、镜像和全部持久化数据供审计排障。

Docker 静态二进制安装仍要求客户 Linux 内核提供可用的 namespace、cgroup、
overlayfs 和 iptables/nftables 等容器能力。若客户主机安全基线禁止这些能力，
应改为客户预装并验收容器运行时，而不是在现场绕过安全策略。

## 八、依赖清单（边界说明）

### 已随离线包提供

- AgenticSRE 源码和 Python 依赖安装结果（含 ChromaDB、SQLite Python 标准库、FastAPI、Kubernetes client、Hermes Agent）；
- ChromaDB 默认 ONNX 向量模型缓存；否则它会在现场首次写入记忆时尝试联网下载；
- 常用只读诊断客户端：`psql`、`mysql`、`redis-cli`、`curl`、`dig`、`ip`、`ping`、`jq`、`openssl`、`ssh`；
- kubectl、Docker Engine 静态二进制和 Docker Compose 插件；
- Compose 文件、配置模板、数据库校验器、备份/恢复工具和 SHA-256 清单。

### 仍需宿主机提供

- 64 位 Linux 内核（namespace、cgroup、overlayfs 等容器能力）；
- 能执行 `bash` 并解压 `.tar.gz` 的最小用户空间，以及 root 权限；systemd 不是首次安装硬依赖。
- 标准 Linux 基础命令：`awk`、`grep`、`sed`、`tar`、`gzip`、`install`、`mktemp`、
  `od`、`tr` 等；安装器会在修改系统前一次性列出缺失项。

这些是容器本身无法替代的宿主机边界。现场禁止安装 Docker 时，客户只需预装
并验收 Docker daemon，安装器会复用它，不覆盖其安全策略。

Prometheus、Elasticsearch、Jaeger、Grafana、客户业务的 Redis/MySQL/MongoDB，
以及 GPU 节点上的 NVIDIA 驱动/DCGM/TELLER 属于“被观测或被诊断环境”，不是
AgenticSRE 自身数据库，交付包不会在客户主机偷偷部署这些服务。常用数据库
只读客户端已装入应用镜像；专用服务端、GPU 驱动和目标侧采集器应按客户现网
版本单独验收。
