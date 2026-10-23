# 告警异步任务、分页与部署验收

## 本次发布内容

源码版本 `0f90dd7`，已部署远端镜像 `agenticsre:alert-jobs-20260916`。包含前两轮的全量组检索、取消默认 200 对候选截断、每批 20 对的完整关系处理，以及本轮新增的持久化异步任务和分页 API。

异步任务使用独立 SQLite 数据库，位于 `data/alert_compression/jobs/jobs.sqlite3`。准备后的固定输入快照单独落盘；每批的状态、关系结果和已记录调用次数原子写入数据库。工作进程持有文件锁，防止共享同一目录的多个应用进程重复消费。重启后将未结束任务重新入队，跳过已成功的批次。失败批次可恢复重试；取消会阻止下一批启动，当前已在执行的请求可能完成。

检查点保证成功且已经落盘的批次不重复执行；**不保证外部模型调用严格一次**。进程若在请求发出后、检查点落盘前中断，该批可能重放。调用计数只覆盖已记录尝试，不能当作服务商账单总数。没有引入费用或 Token 总预算截断。

准备文件落盘和 SQL 总数更新之间的崩溃窗口已专门处理：恢复时从固定输入重新校正候选总数，无需重新检索。部分失败的任务保留检查点，在全部成功前不发布最终结果链接，避免把不完整结果误认为完成。

## API 使用

现有同步接口及完整结果接口保留兼容行为。新增能力通过以下 API 使用；本轮没有将现有网页扫描按钮改成异步模式。

| 操作 | 接口 | 返回含义 |
| --- | --- | --- |
| 提交任务 | `POST /api/alerts/compression-jobs` | HTTP 202，持久化后的 job_id；请求体与 `/api/alerts/compress` 相同 |
| 查询进度 | `GET /api/alerts/compression-jobs/{job_id}` | queued/running/completed/partial/failed/cancelled、候选总数、已完成/失败批次及候选数 |
| 恢复 | `POST /api/alerts/compression-jobs/{job_id}/resume` | 仅 partial/failed/cancelled 可恢复，已成功批次跳过 |
| 取消 | `POST /api/alerts/compression-jobs/{job_id}/cancel` | 仅 queued/running 可取消 |
| 结果摘要 | `GET /api/alerts/compression/{scan_id}/summary` | 扫描版本、统计、关系验证状态和各类记录总数 |
| 分页 | `GET /api/alerts/compression/{scan_id}/page?section=candidate_pairs&offset=0&limit=200` | 当前页、total、revision、next_offset；limit 为 1–200 |

分页 section 支持 groups、raw_alerts、candidate_pairs、relations、scag_edges。客户端使用 next_offset 继续读取，并检查 revision 是否变化；next_offset=null 表示末页。完整结果仍可由原 GET 接口读取。分页减少网络响应，当前实现仍需在后台读取完整扫描文件，不等于已完成数据库级分段读取。

示例请求体：

```json
{
  "cluster_id": "default",
  "namespace": "example",
  "semantic": true,
  "alerts": [
    {"name": "ConnectionRefused", "source": "logs", "message": "connection refused", "timestamp": 1000, "namespace": "example", "pod": "consumer-0"}
  ]
}
```

semantic=false 时任务完成表示压缩、检索及结果保存完成，已完成模型候选数为零；不能将其理解为已进行模型验证。semantic=true 且模型不可用时任务明确失败，配置恢复后可重试。

## 远端验收

所有服务测试和浏览器检查均在 `snail` 执行。最终源码完整回归 **645 passed，108.41 秒**。新增任务测试覆盖失败恢复、调用尝试记录、取消、单工作进程、重启入队、分页尾部和准备阶段崩溃窗口。既有 pytest/依赖弃用警告仍存在。

真实模型重启测试：20 个独立对象生成 79 对候选。首批成功落盘后，对独立测试容器执行强制终止，再启动容器。任务自动恢复并完成四个批次；数据库对比确认终止前成功批次没有重新执行。原文保留、摘要和分页检查通过。此样本没有证明共同根因，模型接受零关系，不用于宣称因果识别准确率。

万条异步压力测试关闭 LLM，10,000 组、49,985 对候选全部保留。HTTP 202 受理耗时 1.319 秒、响应 255 字节；完整任务约 30.259 秒。摘要 855 字节，首个 200 对候选页 38,357 字节，末页 353 字节。25 次并发健康检查均成功，最大 0.289 秒。逐条核对原文一致，每条记录仅属于一个独立组。上述为单次受控测量，不是生产 P95 或并发 SLA。

候选页面在远端 Chromium 中 HTTP 200、DOMContentLoaded 572 毫秒、无控制台错误；部署前原生产页面 532 毫秒、无控制台错误。六个核心源码文件 SHA-256 已核对候选容器与本地审阅版本一致。

## 部署与回滚

发布沿用现有生产 Docker Compose、环境和数据目录，仅替换主服务及 Skill runtime 镜像；不修改其他服务。回滚目标 `agenticsre:job-skill-20260916` 及原 Compose 文件保留。服务健康检查或部署后功能检查失败时执行回滚。

部署后的核查包括源码哈希、主服务与 Skill runtime 健康、Job Skill 存在、原有告警生命周期完全一致、新异步任务完成、分页和旧完整结果接口兼容，以及远端 Chromium 控制台和页面加载检查。

部署结果：主服务和 Skill runtime 均运行上述镜像，状态 healthy，镜像 revision 标签为 `0f90dd7`。部署镜像 ID 为 `sha256:486b8e40a62de89a9fac33fa78d98f324b7b8a294a49b7dee1bec7a8d4ead700`。完整回归后的最终构建仅增加源码版本标签；六个核心文件与通过测试的候选及本地审阅源码哈希一致。

线上复核通过：25 条原有告警生命周期记录与部署前完全一致；Job Skill 可用；新增异步导入任务正常完成，摘要、分页和旧完整结果读取均通过；导入没有进入线上告警生命周期。远端 Chromium 页面 HTTP 200、DOMContentLoaded 507 毫秒、无控制台错误。本轮隔离主服务与 Skill runtime 容器已清理，回滚镜像、原生产配置及验收记录保留。

## 剩余边界

- 全量处理指全部已经召回的候选；大范围 ANN 是近似检索，不承诺穷尽所有关系。
- 单工作进程串行消费任务；网页原有扫描流程仍同步。后续可在页面接入任务进度、取消与分页展示。
- Trace/变更采集仍有上游可用性和覆盖限制；完整多源因果路径、生产标注集和置信度校准需要进一步验收。
- 本轮没有声称整张原设计图的所有能力已完成。

完整远端证据保存在 `/home/ubuntu/AgenticSRE-releases/alert-jobs-20260916/`；审阅副本在 `outputs/alert-jobs-20260916/`。
