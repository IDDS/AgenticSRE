# 架构与诊断原理图

系统介绍图片：[agenticsre-system-overview.png](agenticsre-system-overview.png)。该图使用内置 `imagegen` 生成，用于公开产品介绍；下列可编辑架构图提供更具体的组件与机制说明。

| 图 | Mermaid 源文件 | 文档图片 | 可编辑场景 |
|---|---|---|---|
| 系统架构 | [system-architecture.mmd](system-architecture.mmd) | [SVG](system-architecture.svg) / [PNG](system-architecture.png) | [Excalidraw](system-architecture.excalidraw) |
| 多智能体诊断闭环 | [diagnostic-loop.mmd](diagnostic-loop.mmd) | [SVG](diagnostic-loop.svg) / [PNG](diagnostic-loop.png) | [Excalidraw](diagnostic-loop.excalidraw) |

图与根目录 README 配套。架构图表示主要职责与数据关系，不表示两条链路具备完全相同的审核覆盖；原理图描述多智能体主链路。恢复结果与诊断正确性标签分别保存，合格记忆只能提示后续调查，不能替代本次观测或执行审批。

`.mmd` 是图的源文件，SVG/PNG 是文档展示文件。修改源文件后需要重新渲染对应图片与场景，不能只更新其中一种。PNG 宽度为 1950 像素；SVG 可无损缩放。

本次产物使用已有的离线 Mermaid/Excalidraw 渲染包，在远端 Chromium 中生成并目视检查，不依赖在线绘图服务。`.excalidraw` 可下载后在 Excalidraw 的 File → Open 中编辑；编辑场景不会自动反写 Mermaid 源文件。
