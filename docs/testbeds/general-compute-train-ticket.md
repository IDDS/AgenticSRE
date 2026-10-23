# Train Ticket 部署指南

本文将 Train Ticket 部署为 AgenticSRE 的通算被测对象。该系统包含约 40 个业务
微服务，并依赖 Nacos、RabbitMQ 和 MySQL，适合验证大规模服务发现、消息、数据库
和调用链故障。

> 状态说明：本教程根据固定上游提交核对，尚未在本项目当前远端测试集群完成整套
> 部署验收。固定提交的部署脚本存在本文第 3 节列出的可移植性缺陷，
> 未完成补丁、全量预检和第 9 节验收前，不得进入真实注入。

## 1. 资源与兼容性预检

Train Ticket 的默认 quick start 包含 Nacos 集群、两个三副本 MySQL 集群、
RabbitMQ 和大量业务 Deployment，显著重于 Social Network。建议使用至少 3 个
Linux 节点、32 vCPU、64 GiB 可用内存，并准备默认 StorageClass。带独立数据库、
Prometheus 和 Trace 时需要更多资源。

```bash
kubectl config current-context
kubectl get nodes
kubectl get storageclass
kubectl get pvc -A
helm version
```

上游清单包含较早的 Kubernetes API、镜像和监控组件。部署前必须执行 server-side
dry-run；若集群拒绝某个 API，应在独立分支升级清单并记录差异，不能绕过校验。

## 2. 获取固定版本

```bash
export AGENTICSRE_ROOT=/absolute/path/to/AgenticSRE
export TESTBED_WORKDIR=/absolute/path/to/agenticsre-testbeds
test -f "${AGENTICSRE_ROOT}/docs/testbeds/patches/train-ticket-313886e-deploy-portability.patch"
mkdir -p "${TESTBED_WORKDIR}"
cd "${TESTBED_WORKDIR}"

git clone https://github.com/FudanSELab/train-ticket.git
cd train-ticket
git checkout 313886e99befb94be6cd45f085c98e0019f59829
git rev-parse HEAD
```

预先提取镜像清单并在实验记录中固定 digest：

```bash
rg 'image:' deployment/kubernetes-manifests/quickstart-k8s \
  | sed 's/.*image:[[:space:]]*//' \
  | sort -u
```

若目标机没有 `rg`，可用 `grep -R 'image:' ...` 完成同样检查。上游业务镜像已固定
版本号，但部分基础镜像仍需额外固定 digest。部署脚本还内置了实验数据库
口令；该路径只适用于专用测试环境，不能与其他环境复用凭据。

## 3. 创建 namespace 并渲染预检

先应用本项目为固定提交保存的部署可移植性补丁：

```bash
cd "${TESTBED_WORKDIR}/train-ticket"
git apply --check \
  "${AGENTICSRE_ROOT}/docs/testbeds/patches/train-ticket-313886e-deploy-portability.patch"
git apply \
  "${AGENTICSRE_ROOT}/docs/testbeds/patches/train-ticket-313886e-deploy-portability.patch"
bash -n hack/deploy/deploy.sh
bash -n hack/deploy/gen-mysql-secret.sh
bash -n hack/deploy/utils.sh
git diff --check
```

第一处会导致独立数据库模式生成空 MySQL 主机；后两处会让 Linux
误走 macOS `sed -i` 分支。原脚本还会在首次生成时删除不存在的 Secret 文件；
补丁将其改为幂等删除，让部署器在其他首个失败命令处停止，并在
应用生成的 Secret、Service 和 Deployment 前执行 server-side dry-run。
未完成补丁和语法检查时不要执行 `make deploy`。

```bash
export TT_NAMESPACE=train-ticket
kubectl create namespace "${TT_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

helm lint deployment/kubernetes-manifests/quickstart-k8s/charts/mysql
helm lint deployment/kubernetes-manifests/quickstart-k8s/charts/nacos
helm lint deployment/kubernetes-manifests/quickstart-k8s/charts/rabbitmq
helm template nacosdb deployment/kubernetes-manifests/quickstart-k8s/charts/mysql \
  --namespace "${TT_NAMESPACE}" \
  --set mysql.mysqlUser=nacos \
  --set mysql.mysqlPassword='Abcd1234#' \
  --set mysql.mysqlDatabase=nacos \
  | kubectl apply --dry-run=server -n "${TT_NAMESPACE}" -f -
helm template nacos deployment/kubernetes-manifests/quickstart-k8s/charts/nacos \
  --namespace "${TT_NAMESPACE}" \
  --set nacos.db.host=nacosdb-mysql-leader \
  --set nacos.db.username=nacos \
  --set nacos.db.name=nacos \
  --set nacos.db.password='Abcd1234#' \
  | kubectl apply --dry-run=server -n "${TT_NAMESPACE}" -f -
helm template rabbitmq deployment/kubernetes-manifests/quickstart-k8s/charts/rabbitmq \
  --namespace "${TT_NAMESPACE}" \
  | kubectl apply --dry-run=server -n "${TT_NAMESPACE}" -f -
helm template tsdb deployment/kubernetes-manifests/quickstart-k8s/charts/mysql \
  --namespace "${TT_NAMESPACE}" \
  --set mysql.mysqlUser=ts \
  --set mysql.mysqlPassword=Ts_123456 \
  --set mysql.mysqlDatabase=ts \
  | kubectl apply --dry-run=server -n "${TT_NAMESPACE}" -f -
kubectl apply --dry-run=server \
  -n "${TT_NAMESPACE}" \
  -f deployment/kubernetes-manifests/quickstart-k8s/yamls/svc.yaml
```

全新检出的固定版本只有 `svc.yaml` 和 Deployment 模板。部署前先用同一
上游函数生成默认共享数据库路径所需的 `secret.yaml` 和 `deploy.yaml`：

```bash
/bin/bash -c '
  set -euo pipefail
  source hack/deploy/gen-mysql-secret.sh
  gen_secret_for_services ts Ts_123456 ts tsdb-mysql-leader
  update_tt_dp_cm nacos rabbitmq
'

kubectl apply --dry-run=server -n "${TT_NAMESPACE}" \
  -f deployment/kubernetes-manifests/quickstart-k8s/yamls/secret.yaml
kubectl apply --dry-run=server -n "${TT_NAMESPACE}" \
  -f deployment/kubernetes-manifests/quickstart-k8s/yamls/deploy.yaml
```

如果计划启用 `--with-tracing`，还要生成并检查 Trace 版 Deployment，
同时检查上游 Trace 清单：

```bash
/bin/bash -c '
  set -euo pipefail
  source hack/deploy/gen-mysql-secret.sh
  update_tt_sw_dp_cm nacos rabbitmq
'
kubectl apply --dry-run=server -n "${TT_NAMESPACE}" \
  -f deployment/kubernetes-manifests/quickstart-k8s/yamls/sw_deploy.yaml
kubectl apply --dry-run=server -n "${TT_NAMESPACE}" \
  -f deployment/kubernetes-manifests/skywalking
```

生成文件包含数据库连接 Secret，不得提交或进入普通实验日志。
此预检只针对第 4.1 节默认共享数据库路径；独立数据库模式要按每个实际
release 另行渲染。即使全部 dry-run 通过，首次部署仍只能进入专用测试集群。

检查 NodePort `32677` 未被占用：

```bash
kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}' \
  | grep 32677 || true
```

如有冲突，在上游 `svc.yaml` 中为 `ts-ui-dashboard` 选择实验网段内的空闲 NodePort，
并保存补丁；不要临时修改后不留记录。

## 4. 部署路径

### 4.1 最小通算环境

```bash
make deploy Namespace="${TT_NAMESPACE}"
```

该路径使用共享业务 MySQL，不包含 Prometheus 和 Trace，适合先做镜像、资源和业务
可用性检查，但不满足完整 SRE 被测环境要求。

### 4.2 推荐实验环境

推荐先部署最小业务环境，再接入集群已维护的 Prometheus 和
Jaeger/OpenTelemetry。这样可以避免旧监控清单覆盖集群公共组件。

如果需要先验证上游 SkyWalking 插桩，可在专用集群中单独启用：

```bash
make deploy \
  Namespace="${TT_NAMESPACE}" \
  DeployArgs='--with-tracing'
```

`--with-tracing` 会让业务 Deployment 使用 SkyWalking agent 并部署其后端；
AgenticSRE 还需要兼容查询入口或向已支持的 Trace 后端导出。

不建议直接启用固定提交的 `--with-monitoring`。该路径会将旧版
Prometheus、Grafana、kube-state-metrics 和相关 ClusterRole 写入 `kube-system`，
不受 `TT_NAMESPACE` 隔离。只能在可重建的专用集群中做兼容性测试，不应用于
已有共享监控系统的集群。

### 4.3 独立数据库实验

只有在容量充足、确实需要服务级数据库隔离故障时才使用：

```bash
make deploy \
  Namespace="${TT_NAMESPACE}" \
  DeployArgs='--independent-db --with-tracing'
```

独立数据库会显著增加 StatefulSet、PVC 和启动时间。不要仅为了增加 Pod 数量启用。

## 5. 等待就绪

```bash
kubectl -n "${TT_NAMESPACE}" get deploy,statefulset,pod,svc,pvc -o wide
kubectl -n "${TT_NAMESPACE}" wait \
  --for=condition=Available deployment --all --timeout=30m
kubectl -n "${TT_NAMESPACE}" rollout status statefulset/nacos --timeout=15m
kubectl -n "${TT_NAMESPACE}" get events --sort-by=.lastTimestamp
```

重点检查：

```bash
kubectl -n "${TT_NAMESPACE}" get endpoints \
  ts-ui-dashboard ts-gateway-service ts-preserve-service ts-order-service
kubectl -n "${TT_NAMESPACE}" get pod \
  -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount
```

如果 Nacos、MySQL 或 RabbitMQ 未稳定，业务 Deployment 即使 Ready 也可能持续报错；
应先修复基础依赖，再做入口测试。

## 6. 业务验证和负载

默认入口为节点 `32677`：

```bash
export TT_NODE_IP=192.0.2.10  # 替换为可访问 NodePort 的节点地址
curl -fsS "http://${TT_NODE_IP}:32677/"
```

也可以避免暴露节点端口：

```bash
kubectl -n "${TT_NAMESPACE}" port-forward svc/ts-ui-dashboard 18081:8080
```

保持该前台进程运行，再在第二个终端执行：

```bash
curl -fsS http://127.0.0.1:18081/
```

上游提供独立自动查询工具仓库：
<https://github.com/FudanSELab/train-ticket-auto-query>。其版本和依赖应像业务系统一样
固定；先以单用户、低频查询验证登录、车次查询、预订和订单查询，再提升并发。

若暂不接入该工具，至少用已有接口测试脚本形成以下固定事务链：

```text
登录 -> 查询车次 -> 查询余票 -> 预订 -> 查询订单
```

负载记录必须包含成功率、各步骤 P95/P99、事务总时延和目标服务。只轮询首页不能
覆盖 `ts-preserve-service`、`ts-order-service`、RabbitMQ 和 MySQL 的依赖路径。

## 7. 可观测性验收

### 指标

定位 Prometheus Service，并验证 target：

```bash
kubectl get svc -A | grep -i prometheus
kubectl get pod -A | grep -E 'prometheus|grafana|kube-state-metrics|node-exporter'
```

必须能查询 Pod/容器 CPU、内存、重启、Deployment Available、Endpoint 和 PVC；
若使用自带旧版 kube-state-metrics，应先验证它与目标 Kubernetes 版本兼容。

### 日志

```bash
kubectl -n "${TT_NAMESPACE}" logs deploy/ts-gateway-service --tail=100
kubectl -n "${TT_NAMESPACE}" logs deploy/ts-preserve-service --tail=100
kubectl -n "${TT_NAMESPACE}" logs deploy/ts-order-service --tail=100
```

生产化评测应把容器 stdout/stderr 送入集中日志系统，并为 `namespace`、`pod`、
`container`、`service` 和 `trace_id` 建立字段。

### Trace

```bash
kubectl -n "${TT_NAMESPACE}" get pod,svc | grep -i skywalking
```

执行完整事务链后，应能在 Trace 后端看到入口、预订、订单和依赖服务。当前
AgenticSRE 原生工具面向 Jaeger/OpenTelemetry；若保留 SkyWalking，需要在项目配置
中提供兼容查询入口，或将 Trace 导出到已支持的后端。未完成这一层时，将状态记录
为“应用已部署、Trace 尚未接入”，不要宣称端到端 Trace 已验收。

## 8. AgenticSRE 配置

可在 Web“管理目标”新建档案。如果改 `configs/clusters.yaml`，将下面的
列表项合并到已有 `fault_targets.k8s_clusters` 列表；不要替换整个文件：

```yaml
fault_targets:
  k8s_clusters:
    - id: train-ticket
      name: Train Ticket 测试集群
      kind: general
      kubectl_path: kubectl
      kubeconfig: /root/.kube/testbed-config
      context: testbed
      default_namespace: train-ticket
      prometheus_url: http://prometheus.example:9090
      description: Train Ticket 通算被测环境
```

Train Ticket 的服务名不同于 Social Network。当前仓库的 17 个常规通算场景
均以 `sn-` 开头并绑定另一套资源名，不能仅通过 UI 切换 namespace 或
手工选服务名就用于 Train Ticket。未将第 4 节故障映射实现为服务端登记场景前，
本环境只能用于监控、RCA 和手工验收，不得从现有常规场景发起真实注入。

## 9. 上线验收

进入真实注入前，至少完成一次 15 分钟稳定性窗口：

```bash
kubectl -n "${TT_NAMESPACE}" get pod
kubectl -n "${TT_NAMESPACE}" get endpoints | awk 'NR==1 || $3=="<none>"'
kubectl -n "${TT_NAMESPACE}" get events --sort-by=.lastTimestamp
```

通过标准：

- 核心 Deployment Available，Nacos/RabbitMQ/MySQL 稳定，无持续重启；
- 登录、查询、预订、订单事务连续成功；
- Prometheus 能查询资源和 Kubernetes 状态；
- 日志可按服务和时间定位；
- Trace 已接入支持的后端，或明确标记为未完成；
- 在专用工具或完成 Train Ticket 场景登记后，单 Pod 删除、单服务短时
  scale、单向网络延迟三类低风险故障均可恢复。

详细故障类型见 [故障目录](fault-catalog.md#4-train-ticket)。

## 10. 清理

```bash
make reset-deploy Namespace="${TT_NAMESPACE}"
kubectl -n "${TT_NAMESPACE}" get all,pvc
```

上游 reset 脚本不会自动证明所有监控资源、ClusterRole 或 PVC 已删除。根据部署快照
逐项核对后，再决定是否删除 namespace：

```bash
kubectl delete namespace "${TT_NAMESPACE}"
```

该命令会删除业务数据，只能用于一次性实验环境。

## 11. 参考来源

- 上游仓库：<https://github.com/FudanSELab/train-ticket>
- 自动查询负载：<https://github.com/FudanSELab/train-ticket-auto-query>
- 固定提交的部署入口：`Makefile`、`hack/deploy/deploy.sh`
