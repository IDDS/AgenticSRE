# AgenticSRE 20261126.2 客户交付说明

本包用于 Linux x86_64 主机，包含 Dashboard、Skill Runtime、Hermes、openJiuwen WorkSwarm、ChromaDB 嵌入模型、Docker Engine 及 Compose。现场可断开互联网完成安装。使用 WorkSwarm 诊断时，Dashboard 需要能够访问现场 Kubernetes API 及 OpenAI 兼容模型服务。

## 接收交付包

交付文件为 `AgenticSRE-20261126.2-linux-amd64.zip`，附同名 `.zip.sha256`。在有 `unzip` 和 `sha256sum` 的接收机上，将两者放在同一目录，执行以下命令。也可以在接收机解压、校验后，把完整目录转移到断网目标机。

```bash
sha256sum -c AgenticSRE-20261126.2-linux-amd64.zip.sha256
unzip -q AgenticSRE-20261126.2-linux-amd64.zip
cd AgenticSRE-20261126.2-linux-amd64
sha256sum -c SHA256SUMS
```

`README.md` 是本说明，`customer.env.example` 是可选配置模板，`evidence/` 提供离线安装、回归、页面与真实诊断的验收记录。安装器与模板均可直接从解压目录使用。

## 安装与验收

在目标机的解压目录执行安装。主机需提供 root 权限、cgroup、namespace 及可用的 8080、8090 端口。已有安装的现场先执行以下备份命令，保留生成的数据备份、侧车摘要及原版安装器。

```bash
sudo install -d -m 0700 /var/backups/agenticsre
sudo agenticsrectl --backup "/var/backups/agenticsre/pre-upgrade-$(date +%Y%m%d-%H%M%S).tar.gz"
```

首次安装可直接执行以下命令。

```bash
sha256sum -c agenticsre-offline-20261126.2-linux-amd64.run.sha256
chmod +x agenticsre-offline-20261126.2-linux-amd64.run
sudo ./agenticsre-offline-20261126.2-linux-amd64.run
sudo agenticsrectl --status
sudo agenticsrectl --data-check
```

初装的管理员账号为 `admin`。管理员在 `/var/lib/agenticsre/config/agenticsre.env` 中查看随机生成的初始密码。使用随附的 `customer.env.example` 时，请先复制为 `customer.env`，设置现场账号和模型参数并将权限设为 `0600`。预先准备现场配置时，安装命令可加 `--config ./customer.env`。配置文件按现场要求单独保管。

1. 在管理网络通过 Dashboard 主机的 8080 端口登录，确认首页可打开。没有集群配置时应提示资源读取失败。将现场提供的 kubeconfig 安装到 `/var/lib/agenticsre/kube/config`，在配置中心设置现场模型服务，运行 `sudo agenticsrectl --restart` 后重新登录。
2. 打开“WorkSwarm 诊断”，输入有读取权限的命名空间和 Service 名。展开诊断证据，与该 Service 的 selector、EndpointSlice 就绪数和匹配 Pod 数核对。
3. 输入一个不存在的 Service，确认页面说明缺失证据。刷新页面后可重新打开当前服务进程内的会话；Dashboard 重启后应新建 WorkSwarm 会话。
4. 运维数据可按上面的备份命令保存。还原时，将备份的实际路径作为 `sudo agenticsrectl --restore` 的参数，并将配套的 `.sha256` 放在备份旁边。升级期间新版本启动失败时，安装器执行旧版本和升级前数据恢复。主动回到旧版前，请确认维护窗口并备份当前数据。

## 交付物识别

| 项目 | 实际值 |
| --- | --- |
| 文件 | `agenticsre-offline-20261126.2-linux-amd64.run` |
| 字节数 | 1,303,075,851 |
| 安装器 SHA-256 | `8448c6a88e1af4460e8c2a59885922f76be60061ef71d59b82afaa28620dd050` |
| Docker 镜像 | `agenticsre-offline:20261126.2-amd64` |
| 镜像索引摘要 | `sha256:3d676208b3d889464c2f4a6dc55f2ef2f21be4e1b6ded3a655e54effb0694441` |
| 代码修订 | GitHub `8939795ad81512dd0a9d06fc66b17a94b31fb0a0`，GitCode `e6bd88de20783b478acd6dd698543d651018f245` |
| 依赖版本 | WorkSwarm 0.2.8，宿主 MCP SDK 1.28.1，Docker Engine 29.1.3，Compose 5.5.0 |
| 构建来源 | 干净源码、Hermes 提交 `3065e69dc5f4f2a6aec44cc93ed969ad9878e35f`、WorkSwarm SDK 提交 `068f148b59e140a179e31c9fe58eed8ea1700c1e`。最终镜像由同批候选镜像修正 MCP 依赖并补入启动脚本生成。 |

包内 `docs/python-packages.lock`、`docs/workswarm-packages.lock`、`docs/os-packages.tsv` 记录实际安装版本。`config/customer.env.example` 为配置模板。

## 发布验收记录

2026-11-26 在远程 Linux amd64 服务器执行。最终安装器在远程发布目录和接收工作区的 SHA-256 一致。

| 验收项 | 实测结果 |
| --- | --- |
| 断网初装 | 靶机与公网隔离，初始没有 Docker、Compose、Python 或 Git。执行最终 `.run`，从包内安装 Docker 和 Compose，通过 `docker load` 加载镜像，两个服务均达到 `running/healthy`。 |
| 登录和模型状态 | 匿名访问受保护 API 返回 401。默认管理员登录后可打开首页及 WorkSwarm；未配置模型时正确提示“模型未配置”。离线 SDK 成功与本地 WorkSwarm 进程协商宿主工具协议。 |
| 恢复和重启 | 执行 ChromaDB/SQLite 与 JSON 校验；备份后修改持久化测试标记，再恢复并核对到备份值。重启后两项服务均健康。 |
| 仓库回归 | 在最终镜像的依赖环境中运行完整 pytest，2,868 项通过，0 失败、0 错误、0 跳过，40 条警告。5 项 MCP 专项测试另行复测通过。 |
| 页面与真实诊断 | 22 个导航入口均可打开，无未捕获的 JavaScript 错误或 API 错误。独立实例接入测试集群和模型，真实 Service 的 selector、就绪端点与 Pod 数均与 kubectl 一致；缺失 Service 显示证据不全。另验证了 503、30 秒超时、并发点击、自动刷新和恢复。 |
| 完整性 | 两个 Python 环境分别通过 pip 依赖检查。完整介质通过校验；替换一份配置模板后，安装器在校验阶段拒绝继续。 |

本版验证的是断网 Linux amd64 靶机上的无 systemd 首装和应用服务重启。ARM64、客户物理机整机重启自启，以及客户自己的模型连通性与集群权限，需要在对应环境另行验收。
