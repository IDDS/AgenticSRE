# K8S vLLM Fault Lab

This directory captures the reproducible Kubernetes vLLM setup used for the
T4 intelligent-computing fault lab.

Validated on 2026-07-03:

- Control plane: `33.33.33.127`
- Worker/GPU node: `33.33.33.128`
- GPU: Tesla T4 x6 (`dds-127` x3, `dds-128` x3)
- vLLM model: `/data/dds-data/jyw/Qwen2.5-Coder-3B`
- vLLM NodePort: `30080`
- Prometheus NodePort: `30090`
- GPU exporter: host-network DaemonSet on port `9400`, with per-node
  `Hostname`/`node` labels from the Kubernetes Downward API.

Sensitive SSH passwords are intentionally not stored here. Use your SSH config,
agent, or runtime-only environment variables when applying these manifests.

## Apply

```bash
export VLLM_NODE_NAME=dds-128
export VLLM_PROMETHEUS_NODE_NAME=dds-127
export VLLM_GPU_UUID=GPU-af2dab55-3548-f9c3-3934-08adc92becfa
export VLLM_MODEL_PATH=/data/dds-data/jyw/Qwen2.5-Coder-3B
export VLLM_SERVED_MODEL_NAME=qwen2.5-coder-3b-k8s
export GPU_EXPORTER_NODE_1=33.33.33.127
export GPU_EXPORTER_NODE_1_NAME=dds-127
export GPU_EXPORTER_NODE_2=33.33.33.128
export GPU_EXPORTER_NODE_2_NAME=dds-128
export KUBECONFIG=/etc/kubernetes/admin.conf
bash deploy/k8s-vllm/apply.sh
```

On 2026-10-15, monitoring was restored on `dds-127` because `dds-128` had
95% root filesystem usage and historical disk-pressure evictions. The manifest
pins Prometheus v3.13.4 by digest, limits TSDB retention to 6 hours / 512 MB,
and adds resource bounds and health probes. The `emptyDir` remains ephemeral;
this recovery does not restore historical metrics. Choose a node with adequate
disk space and preload the pinned image if its registry is unreachable.

The validated environment used GPU UUID
`GPU-af2dab55-3548-f9c3-3934-08adc92becfa` on `dds-128` because GPU0 was used
by an existing Docker vLLM service. These values are runtime parameters, not
stored credentials or fixed application defaults.

## Verify

```bash
bash deploy/k8s-vllm/verify_vllm_k8s.sh
```

The verifier checks vLLM health, model API, Prometheus target health, request
success counters, TTFT P95, and E2E latency P95.

## Deploy a Gemma 3 Service

Gemma should be deployed as a separate service so the validated Qwen fault-lab
service remains available. The cached `google/gemma-4-12B-it` directory on the
T4 lab cluster only contains `config.json`; no model weights were present, so it
cannot be deployed. Use local Gemma 3 4B as the current Gemma target. The cached
native `vllm/vllm-openai:latest` image is too old for Gemma 3, so the
reproducible path uses `GEMMA_RUNTIME=hf-openai`: it keeps the OpenAI-compatible
`/v1/completions` and `/v1/chat/completions` surface while loading Gemma 3
through cached Transformers 4.57.1.

The deployer supports two model sources:

- `hostpath`: use an already downloaded model directory on the target node.
- `hf`: let vLLM download from Hugging Face into a host cache directory. If the
  model is gated, create a Kubernetes Secret at runtime; do not store tokens in
  this repository.

Gemma 3 4B example with local weights and cached dependencies:

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
export GEMMA_RUNTIME=hf-openai
export GEMMA_NODE_NAME=dds-128
export GEMMA_MODEL_SOURCE=hostpath
export GEMMA_MODEL_PATH=/data/dds-data/cxw/models/Gemma-3/4B/LLM-Research/gemma-3-4b-it
export GEMMA_TRANSFORMERS_PATH=/data/dds-data/uv_cache/archive-v0/NEGM8QtqHS7pRJ8SAJx2A
export GEMMA_TOKENIZERS_PATH=/data/dds-data/uv_cache/archive-v0/tWA7doKTOVVlYTN7l9Zf7
export GEMMA_HUGGINGFACE_HUB_PATH=/data/dds-data/uv_cache/archive-v0/4Bd5q3w_TUvTFE4G0_iZo
export GEMMA_SAFETENSORS_PATH=/data/dds-data/uv_cache/archive-v0/k8FmJHZz1xVjAbAjUa-S5
export GEMMA_SERVED_MODEL_NAME=gemma-3-4b-it
export GEMMA_GPU_COUNT=1
export GEMMA_NODE_PORT=30081
bash deploy/k8s-vllm/apply_gemma.sh
```

Native vLLM remains supported when the image has Gemma 3 support:

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
export GEMMA_RUNTIME=vllm
export GEMMA_NODE_NAME=dds-128
export GEMMA_MODEL_SOURCE=hostpath
export GEMMA_MODEL_PATH=/data/dds-data/cxw/models/Gemma-3/4B/LLM-Research/gemma-3-4b-it
export GEMMA_SERVED_MODEL_NAME=gemma-3-4b-it
export GEMMA_IMAGE=<gemma3-capable-vllm-image>
export GEMMA_GPU_COUNT=1
export GEMMA_TENSOR_PARALLEL_SIZE=1
export GEMMA_NODE_PORT=30081
bash deploy/k8s-vllm/apply_gemma.sh
```

Validation note from the T4 lab on 2026-07-01: Qwen2.5-Coder-3B is the current
validated GPU closed-loop model on the T4 cluster. Use Gemma only as an
experimental target until the precision/runtime issue below is fixed. The cached
`vllm/vllm-openai:latest` image is vLLM 0.6.5 with Transformers 4.47.1 and
cannot load Gemma 3 natively. The Gemma 4 12B cache under
`/data/dds-data/hf-cache/hub/models--google--gemma-4-12B-it` has no local
weights. Gemma 3 4B weights are complete under
`/data/dds-data/cxw/models/Gemma-3/4B/LLM-Research/gemma-3-4b-it`; use that
path for Gemma experiments until Gemma 4 weights are imported. Gemma 3 4B CPU
float32 produced finite logits, but the T4 GPU fp16 path produced `NaN` logits,
so it is not yet a reliable fault-injection closure target on this hardware.

The helper Dockerfile below is kept for building a refreshed native image when
a network-enabled build host is available:

```bash
docker build \
  -f deploy/k8s-vllm/images/Dockerfile.gemma4 \
  -t registry.local/vllm-openai:gemma4 \
  .
export GEMMA_IMAGE=registry.local/vllm-openai:gemma4
```

Verify the new service:

```bash
export VLLM_URL=http://127.0.0.1:30081
export PROM_URL=http://127.0.0.1:30090
export MODEL=gemma-3-4b-it
bash deploy/k8s-vllm/verify_gemma_vllm.sh
```

If the API server is not running on the same machine as the NodePort, either use
the node IP, for example `http://33.33.33.127:30081`, or create an SSH tunnel /
`kubectl port-forward` before running the verifier.

### Production vLLM tunnel

The production AgenticSRE host uses `http://127.0.0.1:30080/v1/completions` in
its AI cluster profile. The tunnel must therefore be supervised by systemd,
not started as an untracked shell background process. Install the supplied
unit on the AgenticSRE host after the existing
`/etc/agenticsre/ai-prometheus-tunnel.env` has been configured:

```bash
sudo install -m 0755 deploy/remote/agenticsre-ai-vllm-tunnel \
  /usr/local/bin/agenticsre-ai-vllm-tunnel
sudo install -m 0644 deploy/remote/agenticsre-ai-vllm-tunnel.service \
  /etc/systemd/system/agenticsre-ai-vllm-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable --now agenticsre-ai-vllm-tunnel.service
curl -fsS http://127.0.0.1:30080/health
```

The unit forwards the vLLM NodePort through the same jump host as the
Prometheus tunnel and automatically reconnects after a network or jump-host
failure.

If an existing TELLER-enabled Deployment still mounts
`/tmp/patch_teller_vllm_request_markers.py` from the node filesystem, repair it
with the idempotent ConfigMap migration script before restarting the workload:

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
bash deploy/k8s-vllm/repair_teller_patch_mount.sh
```

The script records the source SHA-256 on both the ConfigMap and Pod template,
uses `subPath` so the historical file path remains unchanged, and waits for
the Deployment rollout to finish.

## Generate vLLM Business Load

Fault propagation requires real inference traffic. The web console can now
start a bounded in-cluster load Pod when `背景负载` is enabled. For manual
experiments, run the same load path with:

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
export VLLM_NAMESPACE=llm-k8s
export VLLM_SERVICE_NAME=vllm-server
export VLLM_SERVED_MODEL_NAME=qwen2.5-coder-3b-k8s
export LOADGEN_DURATION_S=180
export LOADGEN_CONCURRENCY=1
export LOADGEN_PROMPT_LEN=128
export LOADGEN_MAX_TOKENS=24
bash deploy/k8s-vllm/apply_loadgen.sh
```

The load generator runs inside the cluster and calls
`http://<service>.<namespace>.svc.cluster.local:8000/v1/completions`, so it does
not depend on NodePort or local SSH tunnels. It prints request count, success
count, RPS, and latency percentiles to Pod logs. Clean it with:

```bash
kubectl -n llm-k8s delete pod,configmap -l app=agenticsre-vllm-loadgen --ignore-not-found=true
```

## Enable Request-Level TELLER Trace

AgenticSRE can display request-level vLLM context when the target vLLM service
is started through TELLER. The dashboard does not rewrite the live Deployment
automatically. It first exposes a non-mutating integration plan:

```bash
curl -s \
  http://127.0.0.1:8080/api/ai-clusters/t4-vllm-k8s/teller/integration-plan
```

The plan uses the AI cluster fields below. Set them in `configs/clusters.yaml`
or the Web target manager:

```yaml
teller_trace_dir: /app/data/teller/trace
teller_code_path: /opt/teller/TELLER_Code
vllm_deployment: vllm-server
vllm_container: vllm
```

The reviewed wrapper command should be equivalent to:

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
       --model "${VLLM_MODEL_PATH}" \
       --host 0.0.0.0 \
       --port 8000
```

After applying the reviewed Deployment change, generate inference load and
confirm the trace includes `step_execute_model[request_ids:...]`. Then refresh
AI Dashboard > Fine-Grained Trace. See
`docs/integrations/teller_skill_runtime_deployment.md` for the full deployment
and verification checklist.
