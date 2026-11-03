# Optuna MCP 调优建议服务（试验性）

本服务提供有限搜索空间中的 `create_study`、`suggest_trial`、`report_trial`、`study_summary` 和 `close_study`。它只生成建议并记录调用者上报的指标，不连接集群、不执行压测、不验证证据、不修改配置，不能独立证明优化效果；须先完成或失败所有待处理 trial，才能关闭 study。

服务依赖 Python 3.10+、`mcp` 和 Optuna 4.5.0；请在隔离环境固定和审核依赖后，以 `python integrations/optuna_mcp/server.py` 启动 MCP stdio 服务。调用方提供的参数取值必须由可信执行器另行校验白名单、目标身份、风险、资源约束及回滚方案。记录结果前应先完成实际变更、独立性能测量和正确性检查；受保护系统必须遵守既有审批与门禁。

所有 study 仅驻留单个进程内，重启后消失；最多 16 个 study、每个最多 64 个 trial。调用方必须保存自身实验记录和证据。失败 trial 应报告 `failed`，不得将失败、缺测或超时伪造为数值。`evidence_ref` 仅是调用方声明，服务不验证其内容或真实性。

`workflow.run_experiment` 提供主机侧编程接口，以 MCP 调用建议服务并调用应用注入的可信异步执行器；执行器必须自行保障目标范围、审批、真实测量、正确性与回滚，返回 `value`、`evidence_ref`、`correctness_verified` 和 `cleanup_verified`。编排器拒绝超出搜索空间的建议，对正确性未确认的试验记失败，清理未确认或执行器抛错时立即停止；它不能独立核验执行器声明，不能将其视为自动调优闭环。

`build_tool_registry` 可由可信宿主显式注入 `optuna_policy` 与 `optuna_executor` 注册 `optuna_experiment`；默认不注册。策略由宿主预先固定，工具调用不能传入目标、命令或参数。同步调用使用 `registry.execute`；在已有事件循环中调用方须通过工具的 `run_async` 直接异步调用。当前还没有将其接入诊断 Agent 或生产执行器。

远端环境验证可使用仓库提供的 `protocol_check.py`；测试需在安装了项目 MCP 依赖与 Optuna 的隔离环境运行。用户侧模型不得自行授予执行权限，服务不接受 shell 命令或运行路径。
