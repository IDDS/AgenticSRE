#!/usr/bin/env bash
set -euo pipefail

namespace="${VLLM_NAMESPACE:-llm-k8s}"
service="${VLLM_SERVICE_NAME:-vllm-server}"
model="${VLLM_SERVED_MODEL_NAME:-qwen2.5-coder-3b-k8s}"
image="${LOADGEN_IMAGE:-vllm/vllm-openai:latest}"
duration_s="${LOADGEN_DURATION_S:-180}"
concurrency="${LOADGEN_CONCURRENCY:-8}"
prompt_len="${LOADGEN_PROMPT_LEN:-2048}"
max_tokens="${LOADGEN_MAX_TOKENS:-128}"
timeout_s="${LOADGEN_TIMEOUT_S:-60}"
qps="${LOADGEN_QPS:-0}"
endpoint="${LOADGEN_ENDPOINT:-http://${service}.${namespace}.svc.cluster.local:8000/v1/completions}"
node_name="${LOADGEN_NODE_NAME:-}"
name="agenticsre-vllm-load-$(date +%s)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

cat > "${tmp_dir}/loadgen.py" <<'PY'
import argparse
import json
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description="vLLM OpenAI-compatible load generator")
parser.add_argument("--endpoint", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--duration-s", type=int, default=180)
parser.add_argument("--concurrency", type=int, default=8)
parser.add_argument("--prompt-len", type=int, default=2048)
parser.add_argument("--max-tokens", type=int, default=128)
parser.add_argument("--timeout-s", type=int, default=60)
parser.add_argument("--qps", type=float, default=0.0)
args = parser.parse_args()

deadline = time.time() + max(1, args.duration_s)
results = queue.Queue()
payload = json.dumps({
    "model": args.model,
    "prompt": "A" * max(1, args.prompt_len),
    "max_tokens": max(1, args.max_tokens),
}).encode("utf-8")
headers = {"Content-Type": "application/json"}
interval = (args.concurrency / args.qps) if args.qps and args.qps > 0 else 0

def worker(worker_id):
    count = 0
    while time.time() < deadline:
        started = time.time()
        status = 0
        err = ""
        try:
            req = urllib.request.Request(args.endpoint, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=max(1, args.timeout_s)) as resp:
                status = getattr(resp, "status", 200)
                resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = str(exc)
        except Exception as exc:
            err = str(exc)
        results.put({"ok": 200 <= int(status or 0) < 300, "status": status, "latency": time.time() - started, "error": err[:160]})
        count += 1
        if interval > 0:
            time.sleep(min(interval, max(0, deadline - time.time())))
    results.put({"worker_done": worker_id, "requests": count})

threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(max(1, args.concurrency))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

items = []
workers = []
while not results.empty():
    item = results.get()
    if item.get("worker_done") is not None:
        workers.append(item)
    else:
        items.append(item)
latencies = [x["latency"] for x in items if x.get("ok")]
errors = [x for x in items if not x.get("ok")]
print(json.dumps({
    "endpoint": args.endpoint,
    "model": args.model,
    "duration_s": args.duration_s,
    "concurrency": args.concurrency,
    "prompt_len": args.prompt_len,
    "max_tokens": args.max_tokens,
    "requests": len(items),
    "success": len(items) - len(errors),
    "errors": len(errors),
    "rps": round(len(items) / max(1, args.duration_s), 3),
    "latency_p50_s": round(statistics.median(latencies), 4) if latencies else None,
    "latency_p95_s": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 4) if latencies else None,
    "sample_errors": errors[:5],
    "workers": workers,
}, ensure_ascii=False, sort_keys=True))
PY

kubectl -n "${namespace}" delete pod,configmap -l app=agenticsre-vllm-loadgen --ignore-not-found=true
kubectl -n "${namespace}" create configmap "${name}-script" \
  --from-file=loadgen.py="${tmp_dir}/loadgen.py" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${namespace}" label configmap "${name}-script" app=agenticsre-vllm-loadgen --overwrite

cat > "${tmp_dir}/pod.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${namespace}
  labels:
    app: agenticsre-vllm-loadgen
    agenticsre.io/service: ${service}
spec:
  restartPolicy: Never
$(if [[ -n "${node_name}" ]]; then cat <<NODE
  nodeSelector:
    kubernetes.io/hostname: ${node_name}
NODE
fi)
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
    - key: node.kubernetes.io/disk-pressure
      operator: Exists
      effect: NoSchedule
  containers:
    - name: loadgen
      image: ${image}
      imagePullPolicy: IfNotPresent
      command: ["python3", "/opt/loadgen/loadgen.py"]
      args:
        - "--endpoint=${endpoint}"
        - "--model=${model}"
        - "--duration-s=${duration_s}"
        - "--concurrency=${concurrency}"
        - "--prompt-len=${prompt_len}"
        - "--max-tokens=${max_tokens}"
        - "--timeout-s=${timeout_s}"
        - "--qps=${qps}"
      volumeMounts:
        - name: script
          mountPath: /opt/loadgen
  volumes:
    - name: script
      configMap:
        name: ${name}-script
EOF

kubectl apply -f "${tmp_dir}/pod.yaml"
echo "loadgen pod: ${name}"
echo "logs: kubectl -n ${namespace} logs -f ${name}"
