# TELLER Request-Level Monitoring and Skill Detail Deployment

This note documents the AgenticSRE integration added for request-level vLLM
monitoring and Skill catalog review. It is intended for deployment and handover,
not as a design draft.

## Scope

The current integration covers two user-visible capabilities:

- AI Dashboard can parse TELLER trace files and show request-level vLLM context
  when events include `step_execute_model[request_ids:...]`.
- Skill Catalog can open a detail modal for each Skill, showing Markdown
  documentation, `skill.yaml`, `skill.py`, metadata, permissions, and confidence
  gates.

The implementation is conservative. It does not mutate the live vLLM Deployment
automatically. The dashboard exposes a non-mutating integration plan first, so
operators can review mounts, commands, and verification before changing the
cluster.

## TELLER Integration Model

TELLER provides request-level context by wrapping vLLM execution and emitting
NVTX/CUPTI trace events. The important event shape is:

```text
step_execute_model[request_ids:<request-id-list>]
```

AgenticSRE parses these events from:

- Chrome trace style JSON with `traceEvents`.
- TELLER native JSON with a top-level `trace` list.
- Existing JSONL/CSV trace exports already supported by the dashboard.

The AI Dashboard groups events by request id and reports:

- request count and context coverage;
- per-request span duration and event count;
- runtime, driver, kernel, and NVTX event counts;
- top events per request;
- process/thread ids observed in the trace.

## Required Runtime Paths

Configure the AI cluster target with:

```yaml
k8s_clusters:
  - id: t4-vllm-k8s
    kind: ai
    default_namespace: llm-k8s
    vllm_deployment: vllm-server
    vllm_container: vllm
    model: qwen2.5-coder-3b-k8s
    teller_trace_dir: /app/data/teller/trace
    teller_code_path: /opt/teller/TELLER_Code
```

`teller_trace_dir` is where AgenticSRE reads generated trace files. In container
deployments this should map to persistent storage under `data/`.

`teller_code_path` is the path visible to the vLLM container or node where the
TELLER package is installed. Do not hardcode local developer paths into cluster
manifests; use cluster target configuration.

## Dashboard APIs

```bash
curl -s http://127.0.0.1:8080/api/ai-clusters/t4-vllm-k8s/dashboard

curl -s \
  http://127.0.0.1:8080/api/ai-clusters/t4-vllm-k8s/teller/integration-plan
```

The integration plan returns:

- target namespace, deployment, container, model, and trace directory;
- the `teller run` wrapper command;
- suggested Kubernetes patch steps;
- verification checks;
- limitations and operator notes.

## Applying TELLER to vLLM

The plan endpoint is intentionally read-only. A reviewed production rollout
should do the equivalent of:

1. Make TELLER code available in the vLLM container image or through a reviewed
   hostPath/PVC mount.
2. Mount a trace output directory that AgenticSRE can read.
3. Change the vLLM command from direct `python -m vllm...` to:

```bash
cd /opt/teller/TELLER_Code
TELLER_DATA_DIR=/app/data/teller \
TELLER_TASK_TYPE=vllm \
TELLER_TAG=agenticsre-vllm \
uv run teller run \
  --run-id agenticsre-vllm \
  --task-type vllm \
  --tag agenticsre-vllm \
  -- python -m vllm.entrypoints.openai.api_server \
       --model <model> \
       --host 0.0.0.0 \
       --port 8000
```

4. Generate inference load against the vLLM OpenAI-compatible API.
5. Confirm trace files contain `step_execute_model[request_ids:...]`.
6. Refresh AI Dashboard > Fine-Grained Trace.

## T4 Cluster Validation Notes

The T4 kubeadm cluster was validated with a temporary `vllm-teller-canary`
Deployment on `dds-128` using the mounted TELLER source tree and Qwen2.5-Coder
3B weights. The canary returned a real OpenAI-compatible completion from
`/v1/completions`, and TELLER wrote CUPTI raw events under:

```text
/opt/teller/data/trace/vllm-teller-canary-<run-id>/output_pid*.tmp.jsonl
```

Observed event types included CUDA `RUNTIME`, `DRIVER`, and `KERNEL` records.
This proves the low-level GPU trace path is active.

The same run also exposed one important integration boundary: some vLLM images
ship NVTX as `libnvToolsExt.so.1` without the unversioned
`libnvToolsExt.so` name. The generated wrapper command now discovers
`libnvToolsExt.so*`, creates `/tmp/teller-nvtx/libnvToolsExt.so`, exports
`TELLER_NVTX_LIB`, and prepends that directory to `LD_LIBRARY_PATH`. This keeps
the fix image-agnostic.

Request-level context is considered complete only when the raw trace contains
`step_execute_model[request_ids:...]` or equivalent request-id-bearing NVTX
events. If raw JSONL contains only `RUNTIME` / `DRIVER` / `KERNEL`, the dashboard
will still show kernel-level profiling, but the request execution path remains
incomplete. In that case the TELLER CUPTI injection library must be rebuilt or
patched so NVTX range text is emitted into the trace export.

For vLLM images where CUPTI records kernels but does not export NVTX marker
text, use the AgenticSRE sidecar-marker patch:

```bash
python3 scripts/patch_teller_vllm_request_markers.py /opt/teller/TELLER_Code
```

The patch keeps the existing NVTX calls, and also writes
`request_markers_pid*.jsonl` into the same `TELLER_TRACE_DIR`. Each file contains
`NVTX_MARKER` push/pop records for `step_execute_model[request_ids:...]`.
AgenticSRE scans these sidecar files together with `output_pid*.tmp.jsonl`, then
joins request windows with CUDA runtime/kernel events. This is the recommended
fallback when kernel-level CUPTI tracing works but request IDs are missing.

The AI Dashboard performs a best-effort synchronization before summarizing
trace data. It uses the configured Kubernetes cluster, `vllm_deployment`,
`vllm_container`, and `teller_runtime_trace_root` to copy
`<runtime_trace_root>/trace` from running vLLM pods into the local
`teller_trace_dir`. The response includes `teller_trace_sync` so operators can
see whether trace files were copied, which Pod was used, and where the local
copy was written.

## Verification

Local code checks:

```bash
node --check web_app/static/js/app.js
PYTHONPYCACHEPREFIX=/private/tmp/agenticsre_pycache python3 -m py_compile web_app/app.py
pytest -q \
  tests/e2e/test_frontend_refresh_behavior.py::test_ai_dashboard_exposes_teller_fine_grained_trace_tab \
  tests/e2e/test_skill_runtime.py::test_ai_cluster_dashboard_includes_teller_trace_summary \
  tests/e2e/test_skill_runtime.py::test_ai_teller_integration_plan_endpoint
```

Remote service checks:

```bash
curl -s http://127.0.0.1:8080/api/health
curl -s http://127.0.0.1:8080/api/skill-workflows/skills/llm.vllm_process_crash
curl -s http://127.0.0.1:8080/api/ai-clusters/t4-vllm-k8s/teller/integration-plan
```

If the web process was started as root, restart with sudo:

```bash
sudo pkill -f "uvicorn web_app.app:app"
cd /home/ubuntu/AgenticSRE
sudo nohup python -m uvicorn web_app.app:app --host 0.0.0.0 --port 8080 \
  > logs/web.log 2>&1 &
```

## Skill Documentation Model

AgenticSRE executable Skills are currently:

```text
skill_runtime/skills/<skill_name>/
  skill.yaml
  skill.py
  SKILL.md        # optional
```

`SKILL.md` is the preferred human-readable document when it exists. If a Skill
does not have `SKILL.md`, the dashboard generates Markdown from `skill.yaml` so
operators can still review:

- purpose and domain;
- execution stages;
- risk and approval policy;
- confidence thresholds;
- permissions and tool dependencies;
- tags and source directory.

The detail modal also exposes the full `skill.yaml` and `skill.py` text for
audit. This avoids hiding the executable logic behind a short catalog card.

## Skill Detail APIs

```bash
curl -s http://127.0.0.1:8080/api/skill-workflows/skills

curl -s \
  http://127.0.0.1:8080/api/skill-workflows/skills/llm.vllm_process_crash
```

The detail response includes:

- `metadata`;
- `markdown`;
- `markdown_source`, either `SKILL.md` or `generated_from_skill_yaml`;
- `yaml`;
- `python`;
- `files`.

## Operational Notes

- The TELLER probe button in the UI is only a quick profiler probe. It is not a
  substitute for wrapping the real vLLM service with TELLER.
- Real request-level context requires live inference traffic.
- Network or process fault experiments should keep SSH and Kubernetes API access
  out of the fault blast radius.
- Use dry-run and approval gates before executing any fault injection or
  recovery action.
