# 智算监控恢复与集群路由修复（2026-10-15）

## 已验证结果

dds-127 的 NVIDIA 设备插件已恢复，节点可分配 GPU 从 0 恢复到 3。
重建前后，原有 `VLLM::EngineCore` 进程 PID 3580198 和 GPU 1 的
12947 MiB 显存占用保持一致。仅重建该节点的设备插件 Pod，未重启主机。

Prometheus 已恢复在 dds-127，沿用现有 ConfigMap、Service、NodePort 30090
和部署服务器的 19090 隧道。当前采集结果为：

| Target | 状态 |
| --- | --- |
| dds-127 node-exporter | up |
| dds-128 node-exporter | up |
| dds-127 GPU exporter | up，3 张 T4 |
| dds-128 GPU exporter | down，镜像未就绪 |
| vllm-server、vllm-large | down，工作节点 GPU 不可分配 |

看板已恢复 `mode=prometheus`，展示真实节点和 GPU 数据，并保留 vLLM
指标缺失提示。恢复前使用 `emptyDir` 的历史时序数据没有恢复；新增指标从
本次启动开始积累。

## 根因与处理

### dds-127 设备插件失去 GPU 访问能力

主机 `nvidia-smi` 正常，驱动为 535.309.01，但长期运行的设备插件容器内
NVML 初始化失败，并不断将设备标为 unhealthy。新建检查容器能正常读取
主机 GPU，限定重建故障设备插件后，容器 NVML 和节点 allocatable 均恢复。

这一现象与 [NVIDIA 关于容器丢失 GPU 访问权限的说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/troubleshooting.html)
一致；本次没有证据确定最初触发它的是哪一次 cgroup 或服务配置变化。

### Prometheus 镜像缺失与历史驱逐

原实例位于根分区使用率约 95% 的 dds-128，处于 ImagePullBackOff；集群中
仍有大量历史 DiskPressure 驱逐记录。本次没有批量删除这些记录。

从官方仓库拉取并验证 Prometheus v3.13.4，固定引用：

```text
quay.io/prometheus/prometheus@sha256:87861b8cf91579109319ebc300f3f1060e6da9c05d6ae8ad15a20c879e84e32e
```

版本依据为 [Prometheus 官方下载页的 LTS 条目](https://prometheus.io/download/)。
通过 `promtool check config /dev/stdin` 验证当前配置，将保留原始 manifest
摘要的 106758656 字节镜像归档导入 dds-127，再恢复 Deployment。
新增 6 小时 / 512 MB TSDB 保留上限、1 GiB emptyDir 限额、资源请求与限制，
以及 readiness/liveness 探针。仓库清单已同步；服务端 dry-run 通过。

### 管道命令未继承选定集群配置

原 `_apply_cluster_to_cmd` 返回 `KUBECONFIG=... printf ... | kubectl ...`。
Shell 仅将环境赋值应用于第一个进程，后面的 kubectl 使用了默认集群配置。
集群 context 名称相同，无法阻止这一错误。本次临时探针曾误建到默认集群；
发现后已核对无遗留，再改用显式 kubeconfig/context 参数执行维护。

修复使完整复合命令运行在带选定 KUBECONFIG 的 `/bin/sh -c` 内，保留原有
kubectl 路径与 context 设置。新增远程隔离回归覆盖管道、`&&`、分号、子 shell、
含空格/引号的配置路径、stdin 和非零退出码。修复前 4 项失败、1 项通过；
修复后相关 59 项通过。真实集群只读验证中，管道和连续命令均返回
`dds-127`、`dds-128`，未访问默认集群。

完整远程回归结果：

```text
1072 passed, 5 warnings in 215.17s (0:03:35)
```

浏览器验证覆盖自愈记录筛选（56 条、每页 10 条）、62 项技能就绪、真实
Kubernetes 节点清单、3 张 GPU 指标、3 个健康监控 target 与模型缺失提示。

## dds-128 待维护项

目前没有重载其驱动、重启主机、删除镜像或模型文件。

| 检查项 | 实测结果 |
| --- | --- |
| 正在运行的内核模块 | NVIDIA 595.84 |
| NVML 与磁盘上的内核模块 | 595.99.02 |
| 内核 ABI | 6.8.0-136-generic，与已安装模块 vermagic 一致 |
| NVML | Driver/library version mismatch |
| GPU 设备文件持有者 | PID 5549，旧 NVIDIA 设备插件 |
| 根分区 | 879 GiB，总使用约 789 GiB，可用约 45 GiB |
| kubelet 驱逐阈值 | imagefs/nodefs.available 均为 3% |
| containerd | 约 1.1 GiB，缺失模型镜像 |
| Docker | 约 29 GiB，仍缓存已验证的 vLLM 0.6.5 镜像 |
| 本项目本地模型目录 | `/var/lib/agenticsre/models/Qwen2.5-14B-Instruct`，约 28 GiB |
| 共享模型盘 | `/data/dds-data` 为 NFS，可用约 41 TiB |

Docker 中的 vLLM 镜像 config digest 为
`sha256:56aa649484bfa110f8c50a296233c07426e1952c523ed5e456e470ceb9841452`，
与 dds-127 已验证镜像一致。仍需向 Kubernetes 使用的 containerd 导入；
导入同时占用镜像内容和解包空间，当前距 3% 驱逐阈值仅约 18 GiB，不宜直接
加载后再次触发驱逐。28 GiB 项目目录是模型权重，不能作为普通缓存清理。

建议维护顺序：

1. 确认维护窗口与存储方案，优先扩容或由数据所有者指定可归档的数据范围。
   不以降低 kubelet 驱逐阈值解决空间不足。
2. 再次核对 GPU 设备持有者和正在运行的工作负载。存在业务 GPU 进程时停止。
3. 仅重建 dds-128 的旧设备插件，尝试无强制参数的驱动模块卸载、重新加载。
   模块仍被使用或正常卸载失败时停止；整机重启另行确认。
4. 验证主机及新容器的 NVML 版本一致、3 张 T4 健康、节点 allocatable=3。
5. 从本机 Docker 缓存导入已验证镜像到 containerd，固定摘要并恢复 exporter
   与模型 Pod，保留 TELLER hooks、模型路径及服务端口。
6. 运行模型请求、Prometheus target、GPU 指标及 TELLER 请求链路验收。

## 发布与证据

应用修复镜像：`agenticsre:ai-recovery-20261015-r2`。
镜像 ID：`sha256:20316917ca8f8f9432c2eb9806ee0ff52268f2437f5bcaab2b7a36913e6fbfea`。
回滚镜像：`agenticsre:usability-20261015-r6`。

远程验证目录：`/home/ubuntu/AgenticSRE-releases/ai-recovery-20261015/`。
本地证据与操作脚本：`outputs/ai-recovery-20261015/`。
发布脚本校验源文件摘要、容器配置与数据指纹，失败自动回滚。
两个线上容器均已使用该镜像且 healthy；16 个源文件摘要一致，配置与数据
指纹保持一致。线上浏览器验证通过，无页面脚本错误。
`cluster-final.json` 确认本轮 5 个临时检查 Pod 已全部移除，两个候选容器也已清理。
最终线上证据为 `deployment-result.json`、`production-result.json`。
本轮未执行 git commit 或 push。
