# 集群概览与 WorkSwarm 发布验证

2026-11-26 在远程服务器 `snail` 验证。测试从已发布的主分支代码和正式 Dashboard 镜像分别进入。正式镜像沿用已有部署布局，主分支多一个本体图谱导航入口。

| 验证对象 | 方法和结果 |
| --- | --- |
| Python 仓库回归 | 在无网络、无生产数据和集群凭据的容器中运行 `python -m pytest --continue-on-collection-errors -q tests`，通过 2,868 项，0 失败、0 错误、0 跳过。安装在独立测试环境的 Semantica 依赖用于图谱用例。40 条依赖与框架警告。 |
| Kubernetes 读取边界 | 36 项新 API 用例覆盖概览、节点、事件、拓扑。正常的空列表返回 200。任一依赖资源返回查询错误、Status、非列表或无响应数据，相关接口返回 503。另在没有集群连接的容器中直接请求四个接口，四项均返回 503。 |
| 前端刷新 | Chromium 验证节点 NotReady、所选命名空间消失、连续点击、事件延迟、切换视图、503、无效响应、真实 30 秒超时、恢复、空集群、快速变更计数、10 秒自动刷新与隐藏页暂停。另运行仓库的四个 Node 测试脚本以及桌面、移动端键盘导航测试。 |
| 页面走查 | 主分支 22 个导航入口、正式布局候选镜像 21 个入口均可打开，无未捕获 JS 错误。主分支隔离实例允许事件刷新写入临时数据。正式布局走查拦截该 POST，其余观测到的 API 请求均成功。 |
| WorkSwarm 诊断 | 浏览器对真实 Service 执行只读诊断，分别与 kubectl 核对 Service selector、EndpointSlice 就绪数和 Pod 数。就绪端点、Pod 均为 1。刷新页面可重开会话。缺失 Service 返回部分证据并列出未完成读取。 |
| 性能 | 同机 Chromium 对同一集群交替刷新，每版 5 次。自触发刷新至拓扑 DOM 更新的中位耗时从 3,269 ms 降至 1,146 ms。常规集群请求从 4 个降至 2 个。此组样本均为 4 个节点、178 个 Pod。 |

## 复测入口

在远程 QA 环境运行以下命令，将 `DASHBOARD_URL` 指向隔离 Dashboard。浏览器用例在浏览器内替换集群 API 响应，不会改变集群状态。

```sh
python -m pytest -q tests
node tests/test_overview_refresh.js
python tests/browser_overview_failures.py "$DASHBOARD_URL"
```

本轮覆盖仓库中 pytest 自动收集的用例。独立运行的真实集群故障注入、自愈写操作和客户现场离线安装演练不在这次 Dashboard 发布验收中。

## 正式部署结果

正式镜像为 `agenticsre:overview-20261126-r2`。在 `snail:8080` 使用 Chromium 点击刷新 3 次，拓扑更新时间分别为 1,472、1,223、1,137 ms，每次各发一次拓扑请求和事件请求。节点卡片为 4 张，Pod 状态图显示 176 个 Running、2 个 Pending，与当轮拓扑一致。Dashboard 与 Skill Runtime 健康，部署后核对到 200 条 RCA 和 125 条自愈历史记录。
