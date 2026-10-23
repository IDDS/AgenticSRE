---
name: K8s 旧知识库桥接
description: 在专用 Skill 迁移完成前，将尚未覆盖的 Kubernetes 故障类型桥接到已评审的自愈知识库。
---
# K8s 遗留 Recipe 桥接

本 Skill 作为迁移期桥接层，将尚未拥有专用 Skill 覆盖的 Kubernetes 故障类型对接到已评审的自愈 recipe 库。

## 适用场景

- 故障类型尚无专用 K8s Skill 覆盖。
- 需临时复用已评审的历史自愈 recipe 给出处置建议。
- 迁移期内保证覆盖面，但要求更强的人工把关。

## 输入信号

- 故障分类结果显示未命中任何专用 Skill。
- 匹配到的遗留 recipe 标识与适用条件。
- `kubectl` 采集的基础事件和状态。
- recipe 库中该条目的评审状态与风险标注。

## 执行阶段

- `detect`: 确认故障未被专用 Skill 覆盖，标记为需桥接处理。
- `diagnose`: 匹配最相关的已评审 recipe，核对其适用条件与当前证据。
- `heal`: 仅按 recipe 生成 dry-run 或建议命令，明确标注来源 recipe，供人工审批。
- `verify`: 批准执行后按 recipe 验证项复检。

## 风险与门禁

- 风险等级 high，桥接路径缺乏专用 Skill 的精细约束，任何变更必须严格人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 最小证据来源为 1，但应尽量补充多源证据。
- 需记录所用 recipe 标识，便于迁移到专用 Skill 后回溯。

## 验证方式

- 按所选 recipe 的验证清单逐项复检。
- `kubectl get all` 和事件确认目标资源恢复正常且无新增异常。
- 记录桥接处置结果，作为专用 Skill 迁移输入。
