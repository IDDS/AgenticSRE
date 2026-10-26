# 业务响应恢复验证

自愈执行与“重新验证恢复”入口共用 `recovery_verification.py`。HTTP 状态成功只能证明请求得到预期状态；启用 JSON 断言后，还必须满足运维声明的响应内容条件，连续全部样本通过才判定恢复。未启用断言的既有 HTTP 配置继续工作，收据明确为 `http_headers`，不能解释为响应内容验证。

## 服务端配置

仅匹配同一 `cluster_id`、`namespace` 和 `service`，不从自愈请求接受 URL 或断言。运维应选择有代表性且无副作用的只读 GET 路径。以下地址为示意，需替换为环境中的实际业务路径：

```yaml
remediation:
  business_recovery_probes:
    - cluster_id: cluster-a
      namespace: demo
      service: search
      url: http://search.demo.svc.cluster.local/business-probe
      expected_status: 200
      samples: 3
      interval_seconds: 2
      timeout_seconds: 3
      max_header_latency_ms: 1000
      max_response_latency_ms: 2000
      max_body_bytes: 65536
      json_assertions:
        - path: [status]
          op: equals
          value: 1
        - path: [data]
          op: non_empty
```

`path` 是逐段访问的列表：字符串选择对象键，非负整数选择数组下标，例如 `[data, 0, available]`。空路径 `[]` 表示整个 JSON。没有表达式执行、脚本、正则、JSONPath 或任意代码求值。

| 断言 | 含义 |
|---|---|
| `equals` | 必须有 `value`，支持字符串、有限数字、布尔值和 null；数字按十进制值精确比较，避免大数小数先转浮点而误判；缺失字段不等于 null；布尔值不与数字混同，JSON 数字 1 与 1.0 可相等 |
| `non_empty` | 值必须为非空字符串、数组或对象，不接受 `value`；不证明数组内每项业务内容正确 |

每个配置最多 16 个断言，路径最多 16 段，字符串键最长 128 字符，期望字符串最长 512 字符。断言、操作名和配置字段严格校验；空断言列表、拼写错误或非法类型在请求前返回未知，不能静默降级为只检查状态码。一次验证开始时固定断言副本，配置编辑不会改变进行中的观察窗口。

样本数为 2–5，间隔 1–10 秒，请求超时 0.1–5 秒。头部及完整响应延迟阈值均为 1–10000 毫秒；完整响应阈值默认继承头部阈值，仅在 JSON 断言模式适用。响应体上限为 1–65536 字节，默认 65536；未启用断言时不能配置响应体参数。

## 结果与资源边界

| 观测 | 判定 |
|---|---|
| 精确 HTTP 状态、头部时延、完整响应时延和所有断言连续通过 | `recovered=true`，`business_validation=json_assertions` |
| 状态不符、超过时延阈值、字段缺失、空结果或断言不符 | `recovered=false`，不被后续好样本覆盖 |
| 完整收到的内容不是合法 JSON、含重复对象键或非标准数值 | 合同不满足，`recovered=false` |
| 连接/读取超时、响应体中断、超体积/结构预算或无法可靠读取 | `recovered=null`，保留未知，不计成功 |
| 基础设施验证不合格、非对应范围或重复配置 | 不发起或不提升业务恢复结论 |

JSON 模式请求 `Accept-Encoding: identity`，支持普通和 chunked 传输，拒绝非 identity 内容编码并保留未知，避免解压造成无界开销。使用底层有界 `read1` 分批读取，最多保留上限加一个判限字节；解析后限制结构深度 32 和节点数 10000。缺少该读取能力的运行环境保持未知。当前已验证环境使用 Requests 2.34.2、urllib3 2.7.0。

完整响应时延从本次请求开始测量。循环检查用于尽早停止慢速响应，Requests 超时仍属于底层连接/读超时；DNS、HTTP 头解析及单次底层读取可能超过协作预算，不能将它解释为进程硬截止时间。[Requests 超时说明](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts)、[urllib3 响应读取接口](https://urllib3.readthedocs.io/en/stable/reference/urllib3.response.html)。

同一观察窗口复用一个 Session，允许完整读取后的连接复用；不跨目标共享。保持禁用环境代理、netrc 凭据和重定向，不新增认证或请求写操作。报告只记录状态、阈值、时延、字节数、断言序号及布尔结果/原因码；不保存 URL、响应体、JSON 路径及断言的期望值或实际值。

`verification_scope` 继续使用 `infrastructure_and_business_http` 以兼容既有消费者，新增 `business_validation` 与检查项的 `validation_level` 区分验证强度。页面成功徽标显示“业务响应断言通过”；无断言时显示原有“业务 HTTP 探测通过”。恢复复验只更新原记录与验证历史，不重放动作，也不因稍后观察到恢复而给原动作记因果成功。

## 验收与限制

业务响应断言阶段（2026-10-26）的冻结候选在远端禁网容器通过 88 项专项和 2156 项完整回归，保留 40 项现有弃用警告。专项包含该阶段新增 48 项与已有 40 项探针/复验用例。真实 loopback HTTP 覆盖内容错误、空结果、布尔/数字混同、缺失与 null、重复 JSON 键、分块与慢速响应、截断、超限及配置编辑；恢复复验 API 使用真实 HTTP 探针，动作和基础设施验证使用受控替身，没有重放动作或给稍后恢复记因果成功。后续历史标准隔离阶段结果见 [按恢复验证标准隔离历史](recovery-verification-history.md#2026-10-26-验证结果)。

原实现的 24 个响应合同反例先失败；还保留了镜像漏复制恢复模块的一项失败，以及中间版本非法配置掩盖基础设施失败的三项失败、三个健康对照。修正后实际应用 COPY 层镜像中的模块哈希与测试源码一致，无源码挂载时完整 Web 应用导入与既有恢复复验路由通过；镜像内四种真实 HTTP 响应验证及六项实际前端徽标函数检查通过。镜像内 HTTP/1.1 正常路径的两次采样复用一条 TCP 连接；这验证连接复用机制，不代表已测得生产时延收益。构建复用已有依赖，不等于从头重建依赖。

仓库内公开验收见 [恢复验证与历史隔离验收](validation/recovery-verification-20261026.md)，包含最终收据与修复前反例。本次仓库发布没有启用生产探针或替换生产服务。

配置的断言只能验证声明的业务条件，不等于全量业务成功率、P95、完整交易、多依赖一致性、长期恢复稳定性或根因正确性。本阶段没有真实故障注入、模型诊断准确率或 MTTR 收益验收。观察样本仍是短窗口；后续增量已按 [恢复验证标准](recovery-verification-history.md) 隔离新历史查询，未标记的旧成功率不作为当前 JSON 合同依据，既有自动执行与权限门禁没有扩大。
