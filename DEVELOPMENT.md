# 开发者工作流

本仓的 Git 远端是 **远端机自建裸仓**（不依赖 GitHub/GitLab）：

```
本地  /Users/cpf/.../AgenticSRE     ──push──►  <target-user>@<target-host>:~/agenticsre.git  (bare)
                                                          │
                                                          │ git clone / pull
                                                          ▼
远端  /home/ubuntu/agenticsre  ──docker compose──►  agenticsre 容器
```

`docker-compose.yaml` 把源码目录 **bind-mount** 进容器，所以多数代码改动 **无需重新打镜像、甚至无需重启容器**。

---

## 日常修改 (Python / 前端 / 配置 90% 的情况)

```bash
# 本地
vim agents/xxx.py
git add -A && git commit -m "fix: ..."
git push                  # → 推到远端 bare repo

# 远端
ssh -J "${AGENTICSRE_DEPLOY_JUMP_HOST}" "${AGENTICSRE_DEPLOY_TARGET}"
cd ~/agenticsre
git pull --ff-only
```

| 改了什么 | 怎么生效 |
|---|---|
| `web_app/templates/*.html`、`static/*` | **无需操作**（请求时 Jinja2 + 静态文件直接读盘） |
| `agents/`、`tools/`、`orchestrator/`、`paradigms/`、`memory/`、`observability/` | `docker compose restart agenticsre`（uvicorn 不开 reload，进程级重启） |
| `configs/config.yaml`、`configs/clusters.yaml` | `docker compose restart agenticsre` |
| `.env` | `docker compose restart agenticsre` |
| `configs/clusters.yaml` 但 `data/cluster_profiles.json` 已存在 | JSON 优先，需先删 JSON 再 restart |

> `web_app/app.py` 也属于 bind-mount，改完 restart 就行。

---

## 需要重新打镜像的情况 (10%)

只有动到 **容器本身** 时需要：

- `requirements.txt`（加新 pip 包）
- `Dockerfile`（换 Python 版本、改基础镜像、加 apt 包）
- `docker-deps/kubectl`（升级 kubectl）
- 根目录 `diagnostic_strategy.py`、`operational_ontology.py`、`operational_lineage.py`、`operational_provenance.py`（默认开发 Compose 未挂载这些文件，须更新镜像）

流程：

```bash
# 远端构建机（编译、测试、构建与部署均在远端执行）
bash build_release.sh                   # 产 agenticsre-release-YYYYMMDD.tar.gz

# 构建机与部署机不同时，传输镜像 + 源码包
scp -J "${AGENTICSRE_DEPLOY_JUMP_HOST}" \
    agenticsre-release-YYYYMMDD.tar.gz \
    "${AGENTICSRE_DEPLOY_TARGET}:~/"

# 远端
ssh -J "${AGENTICSRE_DEPLOY_JUMP_HOST}" "${AGENTICSRE_DEPLOY_TARGET}"
tar xzf agenticsre-release-YYYYMMDD.tar.gz
cd ~/agenticsre
# 复用镜像 tarball
cp ~/agenticsre-release-YYYYMMDD/agenticsre-image.tar.gz .
docker load -i agenticsre-image.tar.gz
docker compose up -d --force-recreate
rm agenticsre-image.tar.gz              # 不要 commit 进 git
```

> **永远不要** 把 `*.tar.gz` `agenticsre-release-*/` 提交到 git —— `.gitignore` 已经拦了，但每次注意一下。

---

## 不进 git 的东西

详见 `.gitignore`。**核心 3 类**：

1. **凭据**：`.env`、`**/kubeconfig*`、`**/.ssh/`
2. **运行时数据**：`data/`（含 memory/、evolution/、rca_history.json 等学习产物，每台机独立）、`logs/`、`report/`、`backups/`
3. **构建产物**：`*.tar.gz`、`release/`、`docker-deps/`、`agenticsre-release-*/`

文档类 `doc/`、`测试/`、`*.docx`、`*.pdf`、`hermes-agent/` 子项目也排除。

---

## 多人协作 (可选)

裸仓在 `/home/ubuntu/agenticsre.git`。其他开发者通过同一跳板机访问：

```bash
git clone "${AGENTICSRE_DEPLOY_TARGET}:agenticsre.git"
git config core.sshCommand "ssh -J ${AGENTICSRE_DEPLOY_JUMP_HOST}"
```

或者把跳板配进 `~/.ssh/config`：

```sshconfig
Host agenticsre-jump
    HostName <jump-host>
    User <jump-user>

Host agenticsre-deploy
    HostName <target-host>
    User <target-user>
    ProxyJump agenticsre-jump
```

之后 `git clone agenticsre-deploy:agenticsre.git` 即可。

---

## 备份与同步策略

- **代码**：本地 push + 远端裸仓 = 双副本
- **运行时数据 `data/`**：仅远端有；如需备份，`tar czf data-backup-$(date +%F).tgz data/` 单独存
- **镜像**：发布时 `agenticsre-release-*.tar.gz` 离线归档；老的可删

---

## 故障恢复

**本地仓挂了**：

```bash
cd /tmp && git clone "${AGENTICSRE_DEPLOY_TARGET}:agenticsre.git"
```

**远端工作目录挂了**：

```bash
# 在远端
mv ~/agenticsre ~/agenticsre.broken
git clone ~/agenticsre.git agenticsre
cd agenticsre
cp ~/agenticsre.broken/.env .
cp -r ~/agenticsre.broken/data .
docker compose up -d
```

**裸仓也挂了**：本地仓推到任何新 remote 即可重建。
