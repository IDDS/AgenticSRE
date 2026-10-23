# AgenticSRE 客户现场部署说明

本文是交付给客户现场工程师的操作手册。交付物是一个 Linux x86_64 单文件安装器，
客户只需要执行一条命令即可完成安装。安装器不需要客户预装 Python、pip、Git、
Docker、Docker Compose 或 PostgreSQL。

## 1. 交付物与一键安装

交付目录中使用以下两个文件：

```text
agenticsre-offline-20260827-r3-linux-amd64.run
agenticsre-offline-20260827-r3-linux-amd64.run.sha256
```

现场安装命令只有一条：

```bash
chmod 0755 agenticsre-offline-20260827-r3-linux-amd64.run
sha256sum -c agenticsre-offline-20260827-r3-linux-amd64.run.sha256
sudo ./agenticsre-offline-20260827-r3-linux-amd64.run
```

其中 `chmod` 和 `sha256sum` 是介质校验与权限准备，不是安装步骤；真正的安装动作
就是最后一条 `sudo` 命令。`.run` 会自动解压内部包、校验文件、安装包内 Docker
Engine 与 Compose、加载应用镜像、创建持久化目录并启动服务，不需要手工解压或运行
第二个安装脚本。

如果客户配置文件已经准备好，可以在同一条安装命令中传入：

```bash
sudo ./agenticsre-offline-20260827-r3-linux-amd64.run --config ./customer.env
```

不传 `--config` 时，安装器会生成随机 Web 管理密码并打印一次；LLM 保持关闭，系统
仍然可以启动和使用本地故障注入、观测、数据管理等功能。

## 2. 客户主机最低条件

- 64 位 Linux x86_64/amd64；
- root 或 `sudo` 权限；
- 能运行 Bash、解压 gzip/tar，并允许 Docker 所需的 namespace、cgroup 和 overlayfs；
- 预留至少“应用镜像压缩包大小 × 3 + 5 GiB”的磁盘空间；
- 8080（Dashboard）和 8090（Skill Runtime）未被其他服务占用，或由客户先确认端口调整方案。

systemd 不是首次安装的硬依赖。存在可用 systemd 时，安装器会注册 Docker 服务；没有
systemd 时，安装器使用包内 `dockerd` 直接启动，首次安装仍然是一条命令完成。客户重启
后如没有 systemd，需要由客户自己的启动编排托管 `/usr/local/bin/dockerd`。

## 3. 大模型配置

### 3.1 配置文件

先复制交付包中的模板：

```bash
cp config/customer.env.example customer.env
chmod 600 customer.env
```

编辑以下变量：

```dotenv
# OpenAI 兼容 API 地址。不要在完全断网现场填写公网地址。
LLM_BASE_URL=http://<客户内网模型网关>:8000/v1

# 真实模型 ID。按客户网关的 /v1/models 返回值填写。
LLM_MODEL=deepseek-v4-pro

# 客户内网模型服务的密钥；不使用大模型时保持为空。
LLM_API_KEY=<客户内网模型服务密钥>

# 可选：模型慢时增大超时时间，单位秒。
LLM_TIMEOUT=300
```

项目默认模型为 `deepseek-v4-pro`，但 `LLM_MODEL` 可覆盖为客户部署的实际模型 ID。
`LLM_BASE_URL` 必须指向 OpenAI 兼容接口的根路径，例如 `http://10.10.20.15:8000/v1`。
安装器不会把 API Key 打印到日志中。

### 3.2 完全无网络现场的模型要求

安装阶段和 AgenticSRE 的故障注入、观测、数据管理功能都不访问互联网。若要启用大模型
诊断，客户必须在现场内网另行提供可访问的 DeepSeek-V4-Pro 或兼容模型服务，并将
`LLM_BASE_URL` 指向该内网地址。模型服务本身不包含在 AgenticSRE `.run` 包中。

如果现场没有模型服务：

```dotenv
LLM_API_KEY=
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=deepseek-v4-pro
```

安装器会保持 LLM 禁用，不影响系统启动；后续补充模型服务后，修改持久化配置并重启
即可启用。

### 3.3 Web 认证配置

建议交付前同时填写：

```dotenv
AGENTICSRE_AUTH_ENABLED=true
AGENTICSRE_AUTH_USERNAME=admin
AGENTICSRE_AUTH_PASSWORD=<强密码>
AGENTICSRE_AUTH_TOKEN=<长随机 API Token>
```

认证配置会持久化到 `/var/lib/agenticsre/config/agenticsre.env`，升级镜像不会覆盖。

## 4. 安装后检查

安装成功后，安装器会打印 Dashboard 地址和管理命令。现场工程师执行：

```bash
sudo agenticsrectl --status
sudo agenticsrectl --data-check
docker compose -f /opt/agenticsre/current/docker-compose.offline.yaml ps
```

预期结果：Dashboard 与 Skill Runtime 均为 `running/healthy`，数据校验返回无错误。
浏览器访问：

```text
http://<客户主机地址>:8080
```

如启用了认证，使用 `AGENTICSRE_AUTH_USERNAME` 和 `AGENTICSRE_AUTH_PASSWORD` 登录。

## 5. 配置和数据持久化

| 内容 | 持久化路径 |
|---|---|
| 模型、认证、运行参数 | `/var/lib/agenticsre/config/agenticsre.env` |
| 记忆库 ChromaDB/SQLite | `/var/lib/agenticsre/data/memory` |
| RCA、故障实验、自愈、演化记录 | `/var/lib/agenticsre/data` |
| 日志 | `/var/lib/agenticsre/logs` |
| Kubeconfig | `/var/lib/agenticsre/kube/config` |
| SSH 私钥与 known_hosts | `/var/lib/agenticsre/ssh` |
| 发布版本与 Compose 文件 | `/opt/agenticsre/releases` |

修改模型或认证配置后执行：

```bash
sudo agenticsrectl --restart
sudo agenticsrectl --status
```

## 6. 备份、恢复和升级

```bash
sudo agenticsrectl --backup /secure-backup/agenticsre.tar.gz
sudo agenticsrectl --restore /secure-backup/agenticsre.tar.gz
```

备份包含配置和业务数据，不包含日志、镜像层、Kubeconfig 和 SSH 私钥。重复执行新版本
`.run` 是原地升级，配置和数据目录保留；升级失败时安装器会恢复旧版本指针和旧数据。

## 7. 严格离线保证

该交付包包含应用镜像、Docker Engine 静态二进制、Docker Compose 插件、Python 依赖、
kubectl、Hermes Agent、Skill Runtime 和嵌入式数据库运行库。现场安装流程：

- 不执行 `apt`、`yum`、`dnf` 或 `pip`；
- 不执行 `docker pull`、镜像构建或 Compose 拉取；
- Compose 固定 `pull_policy: never`；
- 应用镜像通过 `docker load` 从包内归档加载；
- 安装审计写入 `/var/lib/agenticsre/offline-install-audit.env`。

验收时可以检查：

```bash
grep -E '^(RESULT|DEPLOYMENT_MODE|REQUIRES_INTERNET|IMAGE_LOAD_METHOD|COMPOSE_PULL_POLICY)=' \
  /var/lib/agenticsre/offline-install-audit.env
```

应分别看到 `RESULT=PASS`、`DEPLOYMENT_MODE=strict-offline`、`REQUIRES_INTERNET=0`、
`IMAGE_LOAD_METHOD=docker-load` 和 `COMPOSE_PULL_POLICY=never`。

## 8. 常见问题

### 端口冲突

安装器不会自动终止未知进程。先确认：

```bash
ss -lntp | grep -E ':(8080|8090)\\b'
```

由客户决定释放端口或调整部署方案。

### 磁盘空间不足

镜像加载需要额外临时空间。清理客户确认过的无用 Docker 数据后再重试；不要删除
现有业务容器或未知卷。最低建议预留“镜像压缩包大小 × 3 + 5 GiB”。

### 大模型调用失败

检查容器是否能访问客户内网模型网关、`LLM_BASE_URL` 是否包含 `/v1`、`LLM_MODEL` 是否
与模型服务实际 ID 一致，以及 API Key 是否有效：

```bash
docker logs --tail 200 agenticsre
sudo grep -E '^(LLM_BASE_URL|LLM_MODEL|LLM_API_KEY)=' \
  /var/lib/agenticsre/config/agenticsre.env
```

日志不会回显 API Key 的完整内容。
