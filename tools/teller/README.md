# AgenticSRE TELLER vLLM Hooks

这组 hook 用于补齐 TELLER/CUPTI 难以直接看到的业务入口层：

- HTTP 入站 span：记录 vLLM OpenAI API 的请求入口、状态码和 request_id。
- PyTorch 前端 span：可选记录 `torch.nn.Module` 调用，用于把模型前端开销纳入请求路径。
- CUDA Runtime/Driver/GPU Kernel：继续由 TELLER 的 CUPTI/NVTX 注入库采集。

典型链路为：

`HTTP 入站 -> vLLM Engine/Scheduler -> PyTorch/LibTorch -> CUDA Runtime/Driver -> GPU Kernel`

## K8S vLLM 接入

将本目录挂载到 vLLM 容器，例如 `/opt/agenticsre/tools/teller`，并在 vLLM 启动命令中增加：

```bash
export AGENTICSRE_TELLER_HOOKS=1
export AGENTICSRE_TELLER_TRACE_DIR=${TELLER_DATA_DIR:-/opt/teller/data}/trace
export AGENTICSRE_TELLER_PYTORCH_HOOK=1
export AGENTICSRE_TELLER_PYTORCH_SAMPLE=20
export PYTHONPATH=/opt/agenticsre/tools/teller:${PYTHONPATH:-}
```

随后继续使用 TELLER wrapper 启动 vLLM。hook 会写入：

- `agenticsre_http_spans_pid*.jsonl`
- `agenticsre_pytorch_spans_pid*.jsonl`

AgenticSRE Dashboard 会自动把这些 JSONL 与 TELLER 的 NVTX/CUPTI trace 合并。

## 注意

- HTTP hook 基于 FastAPI middleware，适用于 vLLM OpenAI API server。
- PyTorch hook 有额外开销，默认按 `AGENTICSRE_TELLER_PYTORCH_SAMPLE` 采样。
- hook 不会中断推理请求；写 trace 失败时会静默跳过。
