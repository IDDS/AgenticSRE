#!/usr/bin/env bash
set -euo pipefail

namespace="${GEMMA_NAMESPACE:-llm-k8s}"
deployment="${GEMMA_DEPLOYMENT_NAME:-vllm-gemma}"
service="${GEMMA_SERVICE_NAME:-vllm-gemma}"
image="${GEMMA_IMAGE:-vllm/vllm-openai:latest}"
model_source="${GEMMA_MODEL_SOURCE:-hostpath}"
runtime="${GEMMA_RUNTIME:-vllm}"
gpu_count="${GEMMA_GPU_COUNT:-}"
node_port="${GEMMA_NODE_PORT:-30081}"
dtype="${GEMMA_DTYPE:-half}"
max_model_len="${GEMMA_MAX_MODEL_LEN:-1024}"
gpu_memory_utilization="${GEMMA_GPU_MEMORY_UTILIZATION:-0.88}"
extra_args="${GEMMA_EXTRA_ARGS:-}"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "missing required environment variable: ${name}" >&2
    exit 2
  fi
}

require_env GEMMA_NODE_NAME
require_env GEMMA_SERVED_MODEL_NAME

if [[ "${runtime}" != "vllm" && "${runtime}" != "hf-openai" ]]; then
  echo "GEMMA_RUNTIME must be vllm or hf-openai" >&2
  exit 2
fi

if [[ "${runtime}" == "hf-openai" && "${model_source}" != "hostpath" ]]; then
  echo "GEMMA_RUNTIME=hf-openai currently requires GEMMA_MODEL_SOURCE=hostpath" >&2
  exit 2
fi

if [[ -z "${gpu_count}" ]]; then
  if [[ "${runtime}" == "hf-openai" ]]; then
    gpu_count="1"
  else
    gpu_count="3"
  fi
fi

tensor_parallel_size="${GEMMA_TENSOR_PARALLEL_SIZE:-${gpu_count}}"

if [[ -z "${tensor_parallel_size}" ]]; then
  tensor_parallel_size="${gpu_count}"
fi
gpu_resource_yaml=""
if [[ "${gpu_count}" != "0" ]]; then
  gpu_resource_yaml="
          resources:
            limits:
              nvidia.com/gpu: ${gpu_count}"
fi

if [[ "${model_source}" != "hostpath" && "${model_source}" != "hf" ]]; then
  echo "GEMMA_MODEL_SOURCE must be hostpath or hf" >&2
  exit 2
fi

model_arg="/model"
volume_yaml=""
volume_mount_yaml=""
env_yaml=""

if [[ "${model_source}" == "hostpath" ]]; then
  require_env GEMMA_MODEL_PATH
  volume_mount_yaml='
            - name: model
              mountPath: /model
              readOnly: true'
  volume_yaml='
        - name: model
          hostPath:
            path: '"${GEMMA_MODEL_PATH}"'
            type: Directory'
else
  require_env GEMMA_MODEL_ID
  require_env GEMMA_CACHE_PATH
  model_arg="${GEMMA_MODEL_ID}"
  volume_mount_yaml='
            - name: hf-cache
              mountPath: /root/.cache/huggingface'
  volume_yaml='
        - name: hf-cache
          hostPath:
            path: '"${GEMMA_CACHE_PATH}"'
            type: DirectoryOrCreate'
  if [[ -n "${GEMMA_HF_SECRET_NAME:-}" ]]; then
    env_yaml='
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: '"${GEMMA_HF_SECRET_NAME}"'
                  key: token
            - name: HUGGING_FACE_HUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: '"${GEMMA_HF_SECRET_NAME}"'
                  key: token'
  fi
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
rollout_token="${GEMMA_ROLLOUT_TOKEN:-$(date +%s)}"

if [[ "${runtime}" == "hf-openai" ]]; then
  require_env GEMMA_MODEL_PATH
  require_env GEMMA_TRANSFORMERS_PATH
  require_env GEMMA_TOKENIZERS_PATH
  require_env GEMMA_HUGGINGFACE_HUB_PATH
  require_env GEMMA_SAFETENSORS_PATH
  extra_dep_mounts=""
  extra_dep_volumes=""
  extra_pythonpath=""
  extra_ld_library_path="${GEMMA_EXTRA_LD_LIBRARY_PATH:-}"
  if [[ -n "${GEMMA_EXTRA_PYTHONPATH:-}" ]]; then
    extra_pythonpath=":${GEMMA_EXTRA_PYTHONPATH}"
  fi
  if [[ -n "${GEMMA_TORCH_PATH:-}" ]]; then
    extra_pythonpath=":/deps/torch${extra_pythonpath}"
    extra_dep_mounts="${extra_dep_mounts}
            - name: deps-torch
              mountPath: /deps/torch
              readOnly: true"
    extra_dep_volumes="${extra_dep_volumes}
        - name: deps-torch
          hostPath:
            path: ${GEMMA_TORCH_PATH}
            type: Directory"
  fi
  if [[ -n "${GEMMA_TORCHVISION_PATH:-}" ]]; then
    extra_pythonpath=":/deps/torchvision${extra_pythonpath}"
    extra_dep_mounts="${extra_dep_mounts}
            - name: deps-torchvision
              mountPath: /deps/torchvision
              readOnly: true"
    extra_dep_volumes="${extra_dep_volumes}
        - name: deps-torchvision
          hostPath:
            path: ${GEMMA_TORCHVISION_PATH}
            type: Directory"
  fi
  if [[ -n "${GEMMA_TRITON_PATH:-}" ]]; then
    extra_pythonpath=":/deps/triton${extra_pythonpath}"
    extra_dep_mounts="${extra_dep_mounts}
            - name: deps-triton
              mountPath: /deps/triton
              readOnly: true"
    extra_dep_volumes="${extra_dep_volumes}
        - name: deps-triton
          hostPath:
            path: ${GEMMA_TRITON_PATH}
            type: Directory"
  fi
  if [[ -n "${GEMMA_DATA_PATH:-}" ]]; then
    extra_dep_mounts="${extra_dep_mounts}
            - name: deps-data
              mountPath: ${GEMMA_DATA_MOUNT_PATH:-/data/dds-data}
              readOnly: true"
    extra_dep_volumes="${extra_dep_volumes}
        - name: deps-data
          hostPath:
            path: ${GEMMA_DATA_PATH}
            type: Directory"
  fi
  kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f -
  cat > "${tmp_dir}/gemma-hf-server.py" <<'PY'
import asyncio
import gc
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Union

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText, AutoProcessor, AutoTokenizer
try:
    from transformers.models.gemma3.configuration_gemma3 import Gemma3TextConfig
    from transformers.models.gemma3.modeling_gemma3 import Gemma3ForCausalLM
except Exception:
    Gemma3TextConfig = None
    Gemma3ForCausalLM = None

MODEL_PATH = os.getenv("MODEL_PATH", "/model")
SERVED_MODEL_NAME = os.getenv("SERVED_MODEL_NAME", "gemma-3-4b-it")
MAX_MODEL_LEN = int(os.getenv("MAX_MODEL_LEN", "1024"))
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "128"))
GEMMA_MODEL_CLASS = os.getenv("GEMMA_MODEL_CLASS", "image_text").strip().lower()
PATCH_TORCH26_MASK_GATE = os.getenv("GEMMA_PATCH_TORCH26_MASK_GATE", "1").strip().lower() in {"1", "true", "yes"}
GEMMA_DEVICE = os.getenv("GEMMA_DEVICE", "cuda").strip().lower()
GEMMA_TORCH_DTYPE = os.getenv("GEMMA_TORCH_DTYPE", "float16").strip().lower()

if os.getenv("GEMMA_PREPEND_NVIDIA_LIBS", "0").strip().lower() in {"1", "true", "yes"}:
    data_root = os.getenv("GEMMA_DATA_ROOT", "/data/dds-data")
    lib_dirs = []
    archive_root = os.path.join(data_root, "uv_cache", "archive-v0")
    for root, dirs, _files in os.walk(archive_root):
        if root.endswith("/lib") and "/nvidia/" in root:
            lib_dirs.append(root)
    if lib_dirs:
        os.environ["LD_LIBRARY_PATH"] = ":".join(lib_dirs + [os.environ.get("LD_LIBRARY_PATH", "")])

app = FastAPI(title="Gemma OpenAI-compatible Server", version="0.1.0")
model = None
tokenizer = None
processor = None
model_mode = "causal_lm"
request_lock = asyncio.Lock()
started_at = int(time.time())
total_requests = 0
total_tokens = 0
total_latency_seconds = 0.0


def _log(message: str) -> None:
    print(f"[gemma3-hf] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class CompletionRequest(BaseModel):
    model: Optional[str] = None
    prompt: Union[str, List[str]]
    max_tokens: int = Field(default=64, ge=1, le=MAX_NEW_TOKENS)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    max_tokens: int = Field(default=64, ge=1, le=MAX_NEW_TOKENS)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)


def _load_model() -> None:
    global model, tokenizer, processor, model_mode
    _log(f"loading config from {MODEL_PATH}")
    config = AutoConfig.from_pretrained(MODEL_PATH, trust_remote_code=True, local_files_only=True)
    config._attn_implementation = "eager"
    text_config = getattr(config, "text_config", None)
    if text_config is not None:
        text_config._attn_implementation = "eager"
    model_type = getattr(config, "model_type", "")
    torch.cuda.empty_cache()
    gc.collect()
    _log(f"config loaded model_type={model_type or 'unknown'} served_model={SERVED_MODEL_NAME}")
    if model_type == "gemma3":
        model_mode = "gemma3_text" if GEMMA_MODEL_CLASS == "text" else "image_text"
        if model_mode == "image_text" and PATCH_TORCH26_MASK_GATE:
            try:
                import transformers.masking_utils as masking_utils
                masking_utils._is_torch_greater_or_equal_than_2_6 = True
                _log("patched Transformers torch>=2.6 mask gate for Gemma3 processor path")
            except Exception as exc:
                _log(f"failed to patch torch>=2.6 mask gate: {exc}")
        _log("loading Gemma3 processor")
        processor = AutoProcessor.from_pretrained(MODEL_PATH, trust_remote_code=True, local_files_only=True)
        tokenizer = getattr(processor, "tokenizer", None)
        if model_mode == "gemma3_text":
            if Gemma3ForCausalLM is None or Gemma3TextConfig is None:
                raise RuntimeError("Gemma3ForCausalLM is unavailable in this Transformers build")
            text_model_config = Gemma3TextConfig(**(text_config.to_dict() if text_config is not None else {}))
            text_model_config._attn_implementation = "eager"
            _log("loading Gemma3 text-only causal weights")
            model = Gemma3ForCausalLM.from_pretrained(
                MODEL_PATH,
                config=text_model_config,
                dtype=torch.float16,
                device_map="cuda",
                attn_implementation="eager",
                trust_remote_code=True,
                low_cpu_mem_usage=True,
                local_files_only=True,
            )
        else:
            _log("loading Gemma3 image-text weights")
            dtype = {"float32": torch.float32, "fp32": torch.float32, "float16": torch.float16, "fp16": torch.float16}.get(GEMMA_TORCH_DTYPE, torch.float16)
            device_map = "cpu" if GEMMA_DEVICE == "cpu" else "cuda"
            model = AutoModelForImageTextToText.from_pretrained(
                MODEL_PATH,
                config=config,
                dtype=dtype,
                device_map=device_map,
                attn_implementation="eager",
                trust_remote_code=True,
                low_cpu_mem_usage=True,
                local_files_only=True,
            )
    else:
        model_mode = "causal_lm"
        _log("loading tokenizer")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True, local_files_only=True)
        _log("loading causal LM weights")
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_PATH,
            config=config,
            dtype=torch.float16,
            device_map="cuda",
            attn_implementation="eager",
            trust_remote_code=True,
            low_cpu_mem_usage=True,
            local_files_only=True,
        )
    if hasattr(model, "set_attn_implementation"):
        model.set_attn_implementation("eager")
    model.eval()
    allocated = torch.cuda.memory_allocated() / 1024 / 1024 if torch.cuda.is_available() else 0.0
    reserved = torch.cuda.memory_reserved() / 1024 / 1024 if torch.cuda.is_available() else 0.0
    _log(f"model ready mode={model_mode} device={GEMMA_DEVICE} dtype={GEMMA_TORCH_DTYPE} cuda_allocated_mib={allocated:.1f} cuda_reserved_mib={reserved:.1f}")


@app.on_event("startup")
async def startup_event() -> None:
    await asyncio.to_thread(_load_model)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "model": SERVED_MODEL_NAME, "mode": model_mode, "cuda": torch.cuda.is_available()}


@app.get("/v1/models")
async def models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": SERVED_MODEL_NAME, "object": "model", "created": started_at, "owned_by": "agenticsre"}],
    }


@app.get("/metrics")
async def metrics() -> str:
    avg = total_latency_seconds / total_requests if total_requests else 0.0
    return "\n".join([
        f'gemma3_requests_total{{model="{SERVED_MODEL_NAME}"}} {total_requests}',
        f'gemma3_generated_tokens_total{{model="{SERVED_MODEL_NAME}"}} {total_tokens}',
        f'gemma3_request_latency_seconds_avg{{model="{SERVED_MODEL_NAME}"}} {avg}',
        "",
    ])


def _build_prompt(messages: List[ChatMessage]) -> str:
    lines = [f"{msg.role.strip() or 'user'}: {msg.content}" for msg in messages]
    lines.append("assistant:")
    return "\n".join(lines)


def _generate(prompt: str, max_tokens: int, temperature: float, top_p: float) -> Dict[str, Any]:
    global total_requests, total_tokens, total_latency_seconds
    if model is None or tokenizer is None:
        raise RuntimeError("model is not ready")
    if model_mode in {"image_text", "gemma3_text"}:
        target_device = "cpu" if GEMMA_DEVICE == "cpu" else "cuda"
        if model_mode == "image_text" and processor is not None:
            messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            encoded = processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to(target_device)
        else:
            if processor is not None and hasattr(processor, "apply_chat_template"):
                messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
                rendered = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            else:
                rendered = prompt
            encoded = tokenizer(rendered, return_tensors="pt", truncation=True, max_length=MAX_MODEL_LEN)
            encoded.pop("token_type_ids", None)
            encoded = encoded.to(target_device)
    else:
        encoded = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=MAX_MODEL_LEN).to("cpu" if GEMMA_DEVICE == "cpu" else "cuda")
    do_sample = temperature > 0
    kwargs = {"max_new_tokens": max_tokens, "do_sample": do_sample, "pad_token_id": tokenizer.eos_token_id}
    if do_sample:
        kwargs["temperature"] = temperature
        kwargs["top_p"] = top_p
    started = time.time()
    with torch.inference_mode():
        output = model.generate(**encoded, **kwargs)
    latency = time.time() - started
    input_tokens = int(encoded["input_ids"].shape[-1])
    output_tokens = int(output[0].shape[-1])
    if output_tokens > input_tokens:
        generated_ids = output[0][input_tokens:]
    else:
        generated_ids = output[0]
    text = tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
    raw_preview = tokenizer.decode(generated_ids[: min(16, int(generated_ids.shape[-1]))], skip_special_tokens=False)
    _log(
        "generation done "
        f"input_tokens={input_tokens} output_tokens={output_tokens} "
        f"generated_tokens={int(generated_ids.shape[-1])} "
        f"ids={generated_ids[: min(8, int(generated_ids.shape[-1]))].detach().cpu().tolist()} "
        f"raw_preview={raw_preview!r} text_preview={text[:80]!r}"
    )
    generated_tokens = int(generated_ids.shape[-1])
    total_requests += 1
    total_tokens += generated_tokens
    total_latency_seconds += latency
    return {"text": text, "generated_tokens": generated_tokens}


@app.post("/v1/completions")
async def completions(req: CompletionRequest) -> Dict[str, Any]:
    prompts = req.prompt if isinstance(req.prompt, list) else [req.prompt]
    choices = []
    async with request_lock:
        try:
            for idx, prompt in enumerate(prompts):
                result = await asyncio.to_thread(_generate, prompt, req.max_tokens, req.temperature, req.top_p)
                choices.append({"index": idx, "text": result["text"], "finish_reason": "stop"})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "id": f"cmpl-{uuid.uuid4().hex}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": SERVED_MODEL_NAME,
        "choices": choices,
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Dict[str, Any]:
    prompt = _build_prompt(req.messages)
    async with request_lock:
        try:
            result = await asyncio.to_thread(_generate, prompt, req.max_tokens, req.temperature, req.top_p)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": SERVED_MODEL_NAME,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": "stop"}],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
PY
  kubectl -n "${namespace}" create configmap gemma3-openai-server \
    --from-file=gemma3_server.py="${tmp_dir}/gemma-hf-server.py" \
    --dry-run=client -o yaml | kubectl apply -f -
  cat > "${tmp_dir}/gemma-vllm.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${namespace}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${deployment}
  namespace: ${namespace}
  labels:
    app: ${deployment}
    model: ${GEMMA_SERVED_MODEL_NAME}
    runtime: hf-openai-compatible
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ${deployment}
  template:
    metadata:
      labels:
        app: ${deployment}
        model: ${GEMMA_SERVED_MODEL_NAME}
        runtime: hf-openai-compatible
      annotations:
        agenticsre.io/rollout-token: "${rollout_token}"
    spec:
      nodeSelector:
        kubernetes.io/hostname: ${GEMMA_NODE_NAME}
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
        - key: node.kubernetes.io/disk-pressure
          operator: Exists
          effect: NoSchedule
      containers:
        - name: vllm
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: ["bash", "-lc"]
          args:
            - |
              if [ "${GEMMA_PREPEND_NVIDIA_LIBS:-0}" = "1" ] || [ "${GEMMA_PREPEND_NVIDIA_LIBS:-}" = "true" ]; then
                export LD_LIBRARY_PATH="\$(find "\${GEMMA_DATA_ROOT:-/data/dds-data}/uv_cache/archive-v0" -path '*/nvidia/*/lib' -type d 2>/dev/null | tr '\n' ':')\${LD_LIBRARY_PATH:-}"
              fi
              exec python3 /app/gemma3_server.py
          env:
            - name: PYTHONPATH
              value: /deps/transformers:/deps/tokenizers:/deps/huggingface_hub:/deps/safetensors${extra_pythonpath}
            - name: LD_LIBRARY_PATH
              value: "${extra_ld_library_path}"
            - name: HF_HUB_OFFLINE
              value: "1"
            - name: MODEL_PATH
              value: /model
            - name: SERVED_MODEL_NAME
              value: ${GEMMA_SERVED_MODEL_NAME}
            - name: MAX_MODEL_LEN
              value: "${max_model_len}"
            - name: MAX_NEW_TOKENS
              value: "${GEMMA_MAX_NEW_TOKENS:-128}"
            - name: GEMMA_DEVICE
              value: "${GEMMA_DEVICE:-cuda}"
            - name: GEMMA_TORCH_DTYPE
              value: "${GEMMA_TORCH_DTYPE:-float16}"
            - name: GEMMA_MODEL_CLASS
              value: "${GEMMA_MODEL_CLASS:-image_text}"
            - name: GEMMA_PREPEND_NVIDIA_LIBS
              value: "${GEMMA_PREPEND_NVIDIA_LIBS:-0}"
            - name: GEMMA_DATA_ROOT
              value: "${GEMMA_DATA_MOUNT_PATH:-/data/dds-data}"
          ports:
            - containerPort: 8000
              name: http
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            failureThreshold: 120
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
            timeoutSeconds: 2
${gpu_resource_yaml}
          volumeMounts:
            - name: server
              mountPath: /app
              readOnly: true
            - name: model
              mountPath: /model
              readOnly: true
            - name: deps-transformers
              mountPath: /deps/transformers
              readOnly: true
            - name: deps-tokenizers
              mountPath: /deps/tokenizers
              readOnly: true
            - name: deps-hub
              mountPath: /deps/huggingface_hub
              readOnly: true
            - name: deps-safetensors
              mountPath: /deps/safetensors
              readOnly: true${extra_dep_mounts}
      volumes:
        - name: server
          configMap:
            name: gemma3-openai-server
        - name: model
          hostPath:
            path: ${GEMMA_MODEL_PATH}
            type: Directory
        - name: deps-transformers
          hostPath:
            path: ${GEMMA_TRANSFORMERS_PATH}
            type: Directory
        - name: deps-tokenizers
          hostPath:
            path: ${GEMMA_TOKENIZERS_PATH}
            type: Directory
        - name: deps-hub
          hostPath:
            path: ${GEMMA_HUGGINGFACE_HUB_PATH}
            type: Directory
        - name: deps-safetensors
          hostPath:
            path: ${GEMMA_SAFETENSORS_PATH}
            type: Directory${extra_dep_volumes}
---
apiVersion: v1
kind: Service
metadata:
  name: ${service}
  namespace: ${namespace}
  labels:
    app: ${deployment}
    model: ${GEMMA_SERVED_MODEL_NAME}
spec:
  selector:
    app: ${deployment}
  ports:
    - name: http
      port: 8000
      targetPort: 8000
      nodePort: ${node_port}
  type: NodePort
EOF
else

cat > "${tmp_dir}/gemma-vllm.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${namespace}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${deployment}
  namespace: ${namespace}
  labels:
    app: ${deployment}
    model: ${GEMMA_SERVED_MODEL_NAME}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${deployment}
  template:
    metadata:
      labels:
        app: ${deployment}
        model: ${GEMMA_SERVED_MODEL_NAME}
      annotations:
        agenticsre.io/rollout-token: "${rollout_token}"
    spec:
      nodeSelector:
        kubernetes.io/hostname: ${GEMMA_NODE_NAME}
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
        - key: node.kubernetes.io/disk-pressure
          operator: Exists
          effect: NoSchedule
      containers:
        - name: vllm
          image: ${image}
          imagePullPolicy: IfNotPresent
          env:
            - name: HF_HOME
              value: /root/.cache/huggingface${env_yaml}
          args:
            - --model
            - ${model_arg}
            - --served-model-name
            - ${GEMMA_SERVED_MODEL_NAME}
            - --host
            - 0.0.0.0
            - --port
            - "8000"
            - --dtype
            - ${dtype}
            - --max-model-len
            - "${max_model_len}"
            - --gpu-memory-utilization
            - "${gpu_memory_utilization}"
            - --tensor-parallel-size
            - "${tensor_parallel_size}"
            - --enforce-eager
            - --trust-remote-code
$(if [[ -n "${extra_args}" ]]; then for arg in ${extra_args}; do printf '            - %s\n' "${arg}"; done; fi)
          ports:
            - containerPort: 8000
              name: http
${gpu_resource_yaml}
          volumeMounts:${volume_mount_yaml}
      volumes:${volume_yaml}
---
apiVersion: v1
kind: Service
metadata:
  name: ${service}
  namespace: ${namespace}
  labels:
    app: ${deployment}
    model: ${GEMMA_SERVED_MODEL_NAME}
spec:
  selector:
    app: ${deployment}
  ports:
    - name: http
      port: 8000
      targetPort: 8000
      nodePort: ${node_port}
  type: NodePort
EOF
fi

kubectl apply -f "${tmp_dir}/gemma-vllm.yaml"

if [[ "${GEMMA_UPDATE_PROMETHEUS:-true}" == "true" ]]; then
  prometheus_target="${service}.${namespace}.svc.cluster.local:8000"
  kubectl -n "${namespace}" get configmap vllm-prometheus-config -o yaml > "${tmp_dir}/prometheus-config.before.yaml" 2>/dev/null || true
  kubectl -n "${namespace}" get configmap vllm-prometheus-config -o jsonpath='{.data.prometheus\.yml}' > "${tmp_dir}/prometheus.yml" 2>/dev/null || true
  if ! grep -q "${prometheus_target}" "${tmp_dir}/prometheus.yml" 2>/dev/null; then
    if [[ -s "${tmp_dir}/prometheus.yml" ]]; then
      cat >> "${tmp_dir}/prometheus.yml" <<EOF

  - job_name: ${deployment}
    metrics_path: /metrics
    static_configs:
      - targets:
          - ${prometheus_target}
        labels:
          cluster: ${GEMMA_CLUSTER_LABEL:-t4-vllm-k8s}
          service: vllm
          instance: ${deployment}
          model: ${GEMMA_SERVED_MODEL_NAME}
EOF
    else
      cat > "${tmp_dir}/prometheus.yml" <<EOF
global:
  scrape_interval: 5s
  evaluation_interval: 5s
scrape_configs:
  - job_name: ${deployment}
    metrics_path: /metrics
    static_configs:
      - targets:
          - ${prometheus_target}
        labels:
          cluster: ${GEMMA_CLUSTER_LABEL:-t4-vllm-k8s}
          service: vllm
          instance: ${deployment}
          model: ${GEMMA_SERVED_MODEL_NAME}
EOF
    fi
    kubectl -n "${namespace}" create configmap vllm-prometheus-config \
      --from-file=prometheus.yml="${tmp_dir}/prometheus.yml" \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n "${namespace}" rollout restart deploy/vllm-prometheus >/dev/null 2>&1 || true
  fi
fi

if [[ "${GEMMA_WAIT_ROLLOUT:-true}" == "true" ]]; then
  kubectl -n "${namespace}" rollout status "deploy/${deployment}" --timeout="${GEMMA_ROLLOUT_TIMEOUT:-900s}"
fi
