# WorkSwarm 服务诊断远程验证

2026 年 11 月 26 日，在远程服务器的隔离 Dashboard 容器中验证 WorkSwarm 0.2.8、openJiuwen 0.1.20 和官方 Process SDK。Chromium 从控制台提交问题，WorkSwarm 使用平台配置的模型，AgenticSRE 负责执行宿主工具并保留返回值。

| 场景 | 验证结果 |
| --- | --- |
| 已就绪的 Service | 对酒店测试应用的 `frontend` 发起诊断，页面显示 Service 选择器、EndpointSlice 引用的 Pod 及其就绪状态。工具分别读到 1 个就绪端点和 1 个匹配 Pod。 |
| 有选择器但无后端的 Service | 在独立临时命名空间创建 `no-backends`，集群返回的 EndpointSlice 中 `endpoints` 为 `null`。前端显示零端点、零匹配 Pod，结论为服务未就绪。验证后删除了临时命名空间。 |
| 会话续接 | 浏览器在同一会话追问并查看诊断 Skill 目录，在隔离的 Skill Runtime 中实际调用了 `k8s.service_unavailable`。响应为 `dry_run=true`，阶段为 `detect` 和 `diagnose`，三类集群工具再次返回观测。刷新后可重新查看两轮记录。 |
| 异常与隔离 | 定向测试覆盖集群读取失败、空端点、无选择器、同会话并发、切换诊断对象和没有观测的模型回复。对 Skill 请求检查来源、阶段及 dry-run 标志。 |

镜像验证以已有 AgenticSRE 镜像为基础，执行本轮新增的安装脚本并装入候选源码。镜像 ID 为 `sha256:75269205085f2367c9377f3db95ab550b4615caefb3c02a0f15ed2850d046d74`。从镜像启动的 Dashboard 使用内置 SDK 和运行时完成了浏览器验证。390 像素视口下诊断表单宽 364 像素。

远程定向回归共 249 项通过，5 条依赖弃用提示。复现时在测试环境的项目根目录执行以下命令。

```sh
python -m pytest -q tests/test_workswarm.py tests/e2e/test_hermes_skill_runtime.py \
  tests/e2e/test_skill_runtime.py tests/e2e/test_platform_config_persistence.py \
  tests/e2e/test_frontend_refresh_behavior.py tests/test_rca_history_navigation.py
node --check web_app/static/js/workswarm.js
node --check web_app/static/js/app.js
```

浏览器复测需要可用的模型配置及只读集群连接。在“WorkSwarm 诊断”填写命名空间与 Service 名称，提交后展开工具记录。用独立的 `kubectl get service`、`kubectl get endpointslices -l kubernetes.io/service-name=<service>` 和按同一选择器筛选 Pod 的读数逐项核对。会话列表中的“已完成”表示本轮诊断结束，目标服务是否健康需查看诊断结论及证据。
