# DeathStarBench Social Network 部署指南

本文将 DeathStarBench Social Network 部署为 AgenticSRE 的通算被测对象。默认
namespace 为 `test-social-network`，与仓库现有通算故障场景保持一致。

> 状态说明：AgenticSRE 已针对同名服务集合完成远端故障闭环验证，但
> `deploy/faultlab/remote-e2e-workloads.yaml` 是轻量验证夹具，不是完整业务系统。
> 需要业务请求、缓存/数据库依赖和 Trace 时，必须按本文部署完整上游应用。
> 当前等级为 D1：未完成镜像固定和本文验收前不是可直接部署包。

## 1. 拓扑和资源规划

Social Network 包含入口 `nginx-thrift`、发帖/时间线/用户等业务服务，以及
MongoDB、Redis、Memcached 和 Jaeger。固定提交的 chart 会向大多数容器套用
`100m CPU/128Mi` 的统一 requests/limits，对数据库和部分业务服务可能过小。
部署前应另存站点 values，逐类调整业务服务、数据库、缓存和初始化容器
的资源边界，不能把统一默认值当作容量评估。

实验集群建议从 3 个 Linux 节点、16 vCPU、32 GiB 可用内存起步。资源不足时
宁可降低并发，不要删掉数据库或缓存依赖，否则级联故障和调用链实验失去意义。

## 2. 获取固定版本

```bash
export AGENTICSRE_ROOT=/absolute/path/to/AgenticSRE
export TESTBED_WORKDIR=/absolute/path/to/agenticsre-testbeds
test -f "${AGENTICSRE_ROOT}/scripts/ensure_deathstarbench_nodeport.sh"
test -f "${AGENTICSRE_ROOT}/docs/testbeds/patches/deathstarbench-6ecb097-init-source.patch"
mkdir -p "${TESTBED_WORKDIR}"
cd "${TESTBED_WORKDIR}"

git clone https://github.com/delimitrou/DeathStarBench.git
cd DeathStarBench
git checkout 6ecb09706140f8730b5385c08f1386c654c3c526
git submodule update --init --recursive
git apply --check \
  "${AGENTICSRE_ROOT}/docs/testbeds/patches/deathstarbench-6ecb097-init-source.patch"
git apply \
  "${AGENTICSRE_ROOT}/docs/testbeds/patches/deathstarbench-6ecb097-init-source.patch"
git diff --check
```

`AGENTICSRE_ROOT` 必须是本项目的绝对路径，后文调用项目审计脚本时依赖它。
若 `DeathStarBench` 目录已存在，不要重复 clone；先确认它没有未保存的修改。

记录版本：

```bash
git rev-parse HEAD
helm version
kubectl config current-context
```

## 3. 部署 Kubernetes 版本

上游 Helm chart 位于 `socialNetwork/helm-chart/socialnetwork`。先创建 namespace，
再更新依赖并渲染清单。`helm template` 是必要的预检，可提前发现 API、镜像和
资源配置问题。

```bash
export DSB_NAMESPACE=test-social-network
export DSB_RELEASE=dsb-social
export DSB_CHART=./socialNetwork/helm-chart/socialnetwork
export DSB_SITE_VALUES="${TESTBED_WORKDIR}/dsb-social-site-values.yaml"

test -f "${DSB_SITE_VALUES}" || cp "${DSB_CHART}/values.yaml" "${DSB_SITE_VALUES}"
# 先编辑该文件：固定镜像，调整资源，并根据第 3.1 节核对 DNS。
if grep -En 'defaultImageVersion:[[:space:]]*latest' "${DSB_SITE_VALUES}"; then
  echo "ERROR: 请先在 DSB_SITE_VALUES 中固定 defaultImageVersion" >&2
  exit 1
fi

kubectl create namespace "${DSB_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
helm dependency build "${DSB_CHART}"
helm lint "${DSB_CHART}" -f "${DSB_SITE_VALUES}"
helm template "${DSB_RELEASE}" "${DSB_CHART}" \
  --namespace "${DSB_NAMESPACE}" \
  -f "${DSB_SITE_VALUES}" > /tmp/dsb-social-rendered.yaml
if grep -En 'image:[[:space:]]*[^[:space:]]*:latest' /tmp/dsb-social-rendered.yaml; then
  echo "ERROR: 渲染清单仍有 latest 镜像，请在站点 values 中固定" >&2
  exit 1
fi
kubectl apply --dry-run=server -f /tmp/dsb-social-rendered.yaml
helm upgrade --install "${DSB_RELEASE}" "${DSB_CHART}" \
  --namespace "${DSB_NAMESPACE}" \
  -f "${DSB_SITE_VALUES}" \
  --wait --timeout 15m
```

`DSB_SITE_VALUES` 中至少要审核以下键；具体标签必须来自固定提交的
自建镜像，或已在目标架构拉取并记录 digest 的镜像：

```yaml
global:
  # 用实际推送的业务镜像仓库和不可变版本替换。
  dockerRegistry: registry.example/testbeds
  defaultImageVersion: validated-social-network-version
  resources:
    requests: {cpu: 200m, memory: 256Mi}
    limits: {cpu: "1", memory: 1Gi}
jaeger:
  container:
    image: registry.example/testbeds/jaeger-all-in-one
    imageVersion: validated-jaeger-version
nginx-thrift:
  container:
    # 该子 chart 原本有独立镜像，必须显式覆盖完整地址。
    image: registry.example/testbeds/openresty-thrift
    imageVersion: validated-openresty-version
  initContainer:
    image: registry.example/testbeds/git
    imageVersion: validated-git-version
media-frontend:
  container:
    image: registry.example/testbeds/media-frontend
    imageVersion: validated-frontend-version
  initContainer:
    image: registry.example/testbeds/git
    imageVersion: validated-git-version
```

这些资源值只是结构示例，不是容量建议。渲染后用下列命令审核所有
实际镜像，不要只修复脚本检出的第一个浮动标签：

```bash
awk '/^[[:space:]]*image:/ {print $2}' /tmp/dsb-social-rendered.yaml | sort -u
```

如果环境不能访问 chart 依赖仓库，在联网构建机完成 `helm dependency build`，
连同 chart 的 `charts/` 目录和所需镜像一起进入离线环境。不要在离线目标机临时
改回浮动依赖。

本项目补丁会把两个 init container 的源码下载固定到同一上游提交，
但启动时仍需要访问源码仓库。离线或长期基线应将这些 Lua/页面/密钥文件
预置进经审核的不可变镜像，再移除启动时 clone。

### DNS 兼容性

上游 values 默认 resolver 为 `kube-dns.kube-system.svc.cluster.local`。若集群只有
CoreDNS，先检查该 Service 名是否仍为 `kube-dns`：

```bash
kubectl -n kube-system get svc kube-dns
```

若名称不同，在 `${DSB_SITE_VALUES}` 中用目标集群的 DNS Service FQDN
覆盖 `global.nginx.resolverName`，然后重新执行已带 `-f` 的
`helm lint/template/upgrade` 命令和 server-side dry-run。

## 4. 等待和排障

```bash
kubectl -n "${DSB_NAMESPACE}" get deploy,statefulset,pod,svc -o wide
kubectl -n "${DSB_NAMESPACE}" wait \
  --for=condition=Available deployment --all --timeout=15m
kubectl -n "${DSB_NAMESPACE}" get pods \
  --field-selector=status.phase!=Running
kubectl -n "${DSB_NAMESPACE}" get events --sort-by=.lastTimestamp
```

若 init container 拉取脚本失败，检查目标节点是否能访问其固定来源；生产化实验
建议把这些文件构建进不可变镜像，而不是在 Pod 启动时联网下载。

## 5. 初始化业务数据

初始化脚本依赖 `aiohttp`。先在上游目录内创建独立环境，不要安装到
AgenticSRE 的 Python 环境：

```bash
cd "${TESTBED_WORKDIR}/DeathStarBench"
python3 -m venv .venv-init
. .venv-init/bin/activate
python -m pip install 'aiohttp>=3.9,<4'
python -c 'import aiohttp; print(aiohttp.__version__)'
```

入口先在集群内保持 `ClusterIP`，通过 port-forward 初始化小型社会图：

```bash
kubectl -n "${DSB_NAMESPACE}" port-forward svc/nginx-thrift 18080:8080
```

保持这个前台进程运行，再在第二个终端执行后续命令。

另开终端执行：

```bash
cd "${TESTBED_WORKDIR}/DeathStarBench/socialNetwork"
python3 scripts/init_social_graph.py \
  --graph=socfb-Reed98 \
  --ip=127.0.0.1 \
  --port=18080
```

初始化成功后访问 `http://127.0.0.1:18080`，或请求一个只读时间线接口：

```bash
curl -fsS 'http://127.0.0.1:18080/wrk2-api/home-timeline/read?user_id=1&start=0&stop=10'
```

## 6. 建立持续负载

上游 wrk2 仅支持 x86 构建路径。负载机需有 `make`、`luajit`、LuaSocket 和 SSL/zlib
开发包：

```bash
cd "${TESTBED_WORKDIR}/DeathStarBench/wrk2"
make
cd ../socialNetwork
../wrk2/wrk -D exp -t 4 -c 32 -d 10m -L \
  -s ./wrk2/scripts/social-network/read-home-timeline.lua \
  http://127.0.0.1:18080/wrk2-api/home-timeline/read -R 20
```

先用 `20 RPS` 建立 10 分钟基线，再按 CPU、P95 和错误率逐步提升。故障注入期间
保持相同脚本、连接数和目标 RPS，否则无法区分故障影响与负载变化。

若必须通过节点访问入口，使用仓库的受控脚本，显式指定端口：

```bash
export DEATHSTARBENCH_NAMESPACE=test-social-network
export DEATHSTARBENCH_SERVICE=nginx-thrift
export DEATHSTARBENCH_NODE_PORT=30087
bash "${AGENTICSRE_ROOT}/scripts/ensure_deathstarbench_nodeport.sh"
```

## 7. 可观测性接入

### Kubernetes 指标和事件

至少为集群 Prometheus 接入 kube-state-metrics 和节点/容器指标，验证：

```bash
kubectl top node
kubectl -n "${DSB_NAMESPACE}" top pod
kubectl -n "${DSB_NAMESPACE}" get events --sort-by=.lastTimestamp
```

AgenticSRE 的通算场景会使用 `nginx-thrift`、`compose-post-service`、
`home-timeline-service`、`post-storage-mongodb`、`home-timeline-redis`、
`user-memcached`、`social-graph-redis`、`text-service` 和
`url-shorten-service` 等稳定资源名。部署后应核对名称，不要依赖 Pod 随机后缀。

### Jaeger Trace

上游应用使用 Jaeger 6831/UDP。先确认 Jaeger 镜像不是浮动标签，并运行仓库审计：

```bash
export JAEGER_NAMESPACE=test-social-network
export JAEGER_SERVICE=jaeger
export JAEGER_APP_SELECTOR='app=jaeger'
bash "${AGENTICSRE_ROOT}/scripts/audit_jaeger_ingest.sh"
```

固定提交的 Jaeger 和部分业务/初始化镜像仍使用 `latest`。它们可用于
首次兼容性摸底，但在建立可复现基线前，必须通过 `site-values.yaml`
固定成已验证标签或 digest，然后重新部署和验收。

如果目标 Jaeger 不再提供 6831/UDP，可部署兼容 agent bridge，或重新插桩为 OTLP；
不能只看到 Jaeger UI 就认为 Trace 已接入。发出业务请求后必须能查询到
`nginx-thrift` 及下游服务的同一 Trace。

## 8. AgenticSRE 目标配置

可在 Web“管理目标”新建档案。如果改 `configs/clusters.yaml`，将下面的
列表项合并到已有 `fault_targets.k8s_clusters` 列表；不要用这段替换整个文件：

```yaml
fault_targets:
  k8s_clusters:
    - id: social-network
      name: Social Network 测试集群
      kind: general
      kubectl_path: kubectl
      kubeconfig: /root/.kube/testbed-config
      context: testbed
      default_namespace: test-social-network
      prometheus_url: http://prometheus.example:9090
      description: DeathStarBench Social Network 完整被测环境
```

先在“通算故障实验”选择该集群，执行 dry-run。确认目标和 undo 后再按
[故障目录](fault-catalog.md#3-deathstarbench-social-network) 的低风险顺序实注。

## 9. 上线验收

```bash
kubectl -n "${DSB_NAMESPACE}" get pod
kubectl -n "${DSB_NAMESPACE}" get endpoints nginx-thrift compose-post-service home-timeline-service
curl -fsS http://127.0.0.1:18080/
```

验收必须同时满足：

- 所有关键 Deployment Available，数据库/缓存无持续重启；
- `nginx-thrift` Endpoint 非空，连续业务请求成功；
- 负载下能看到入口 QPS、错误率、P95/P99 和容器资源基线；
- 至少一条请求具有跨入口、业务服务和数据层的 Trace；
- 低风险 Pod 删除或单服务 scale 故障可恢复，入口重新成功。

## 10. 清理

先停止负载和 port-forward，再卸载 chart：

```bash
helm uninstall "${DSB_RELEASE}" --namespace "${DSB_NAMESPACE}"
kubectl -n "${DSB_NAMESPACE}" get pvc
kubectl delete namespace "${DSB_NAMESPACE}"
```

删除 namespace 会删除其中业务数据。只有确认这是一次性实验环境、且无需保留
证据或数据库快照时才执行最后一条命令。

## 11. 参考来源

- 上游仓库：<https://github.com/delimitrou/DeathStarBench>
- Social Network 说明：<https://github.com/delimitrou/DeathStarBench/blob/master/socialNetwork/README.md>
- Kubernetes Helm chart：<https://github.com/delimitrou/DeathStarBench/tree/master/socialNetwork/helm-chart>
- 本项目场景：`eval/fault_scenarios.yaml`
- Jaeger 检查：`scripts/audit_jaeger_ingest.sh`
