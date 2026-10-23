# AgenticSRE 离线一键部署验收记录

- 验收日期：2026-08-27
- 交付架构：Linux x86_64
- 网络条件：安装靶机使用 `--network none`，并主动确认不能访问公网
- 靶机初始状态：无 Docker、Docker Compose、Python、pip、Git 和外置数据库
- 验收结论：**PASS**

## 实际执行范围

1. 校验单文件安装器及其内置交付包的 SHA-256；
2. 从离线包安装 Docker Engine 29.1.3 和 Docker Compose 5.5.0；
3. 在无网络条件下加载 AgenticSRE 应用镜像；
4. 使用交付 Compose 文件启动 Dashboard 与 Skill Runtime；
5. 验证两个容器均为 `running/healthy`；
6. 对 ChromaDB/SQLite 执行 `quick_check`，并校验所有 JSON 状态文件；
7. 停止写入后创建数据与配置的一致性备份及 SHA-256 侧车文件；
8. 修改备份后的持久化标记，再执行预校验、恢复、健康检查与标记对比；
9. 重启全部服务，再次执行双服务健康检查和持久化数据校验。

## 关键结果

```text
result=PASS
network=none
offline_contract=strict-offline
installer_network_fetches=0
image_load_method=docker-load
compose_pull_policy=never
host_arch=x86_64
docker_version=29.1.3
docker_start_mode=direct-fallback
compose_version=5.5.0
dashboard=running/healthy
skill_runtime=running/healthy
marker={"value":"before-backup"}
```

`docker_start_mode=direct-fallback` 是因为 macOS Docker Desktop 内的嵌套 systemd 本身处于
degraded 状态，无法正常托管内层 dockerd。验收使用安装器的直接守护进程
兼容路径，但 Docker 二进制安装、镜像加载、Compose 运行、持久化、备份恢复和
应用重启都是真实执行，未使用 mock。客户现场正式验收仍应在代表性物理
Linux/VM 上补做 `systemctl is-active docker` 和整机重启后的自启检查。

发布门禁还会在同一断网靶机中隐藏 `systemctl` 命令，验证没有 systemd 时仍能使用包内
Docker 直接完成首次部署，结果记录为 `docker_start_mode=direct-no-systemd`。

## 重复验收

```bash
tests/run_offline_install_e2e.sh \
  release/agenticsre-offline-<version>-linux-amd64.run
```

正式发布应运行门禁脚本，它会构建交付包并自动调用上述验收，只有摘要同时满足
`result=PASS`、`network=none`、严格离线契约和双服务健康才返回成功：

```bash
VERSION=<version> TARGET_ARCH=amd64 ./release_offline_bundle.sh
```

测试实现：

- `tests/run_offline_install_e2e.sh`
- `tests/offline_install_target/Dockerfile`
- `tests/offline_install_target/run_inside_target.sh`
