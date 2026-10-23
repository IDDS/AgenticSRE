# 现网 Job 告警指标标签匹配修复

## 问题与改动

上一版现网 14 个 Prometheus 分组没有合格指标证据。其中三个 KubeJobFailed 分组对应的 kube_job_failed 序列分别包含 condition=true、false、unknown；原逻辑只匹配命名空间和 Job 名称，因此正确地拒绝了三条候选，但遗漏了告警自身已经保留的 condition 标签。

本轮仅对单指标选择器比较规则追加输入序列标签验证。将所有原告警保留的标签与返回序列匹配，排除 alertname 和规则添加/覆盖的标签。每个分组内所有告警必须匹配同一序列，仍不允许多条候选；不平均不同状态、不任意选择采集实例。显式测量注解与复杂表达式不使用这条标签保留假设。

新增测试覆盖 condition 三种状态、不同 exporter 实例及规则覆盖标签导致的歧义；原有告警成员、统计阈值和生命周期逻辑不变。

## 真实远端验证

在 snail 隔离候选容器中读取生产配置和现网 Prometheus，使用独立候选数据目录。只读采集返回 16 条告警，14 个 Prometheus 分组中有 3 个获得合格指标证据，每个 11 点，均准确匹配 Job 名称及 condition=true。可用分组从 0 增加为 3。

三个 Job 为 telemetry-pipeline-probe、source-matched-components-verification 和 source-matched-components-verification-v2，均位于 rca-semantic-tt。两个 Job 的历史数值均恒定，统计检查返回 unknown / constant series，未把共同失败状态误判为统计相关。证据可用并不等同于统计关联通过，更不代表确认根因。

剩余 10 个分组为复杂规则：TargetDown 四组、KubePodNotReady 四组，以及两种 etcd 告警各一组；另一个分组缺少支持的资源身份。后续需要保留聚合、向量匹配及动态阈值的含义，明确配置测量查询或使用结构化表达式解析，不能凭告警名称猜测指标。

## 回归验证

远端完整回归 **719 passed**，108.70 秒。新增两项测试均纳入完整套件；测试未在本地执行。候选接口验证确认异步压缩、分页、旧版读取和 Job Skill 可用，导入操作不改变生命周期记录。

## 产物

源代码提交 `5dfc84c`。远端目录 `/home/ubuntu/AgenticSRE-releases/metriclabels-20260916/`，本地证据目录 `outputs/metriclabels-20260916/`。

## 部署验收

已部署 `agenticsre:metriclabels-20260916`，镜像 ID 为 `sha256:308827bc95b20e29ce3ac1bb5912af923d5d5980dddb59418d5aebad03bdacc6`。两个修改文件与正式容器逐项哈希一致。主服务和 Skill 服务健康，25 条原有生命周期记录与部署前完全一致；旧接口、异步压缩、分页和 Job Skill 检查通过。远端 Chromium 页面 HTTP 200，加载 545 毫秒，无浏览器错误。

回退镜像 `agenticsre:livemetrics-20260916` 保留，本轮未触发回退。结论限于已验证范围，不宣称所有现网复杂规则已完成适配。

正式容器再次通过三个现网 Job 的只读指标验收，结果保存在 `live-job-production.json`。两个本轮临时候选容器均已停止并删除，未操作其他业务容器。验证产物归档为 `verification-artifacts.tar.gz`。
