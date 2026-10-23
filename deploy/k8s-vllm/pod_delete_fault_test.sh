#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-llm-k8s}"
DEPLOYMENT="${DEPLOYMENT:-vllm-server}"
VLLM_URL="${VLLM_URL:-http://127.0.0.1:30080}"
PROM_URL="${PROM_URL:-http://127.0.0.1:30090}"
MODEL="${MODEL:-qwen2.5-coder-3b-k8s}"
KUBECTL="${KUBECTL:-kubectl}"

echo "== before =="
"${KUBECTL}" -n "${NS}" get pods -l "app=${DEPLOYMENT}" -o wide
curl -sG --max-time 5 "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:request_success_total'
echo

old_pod="$("${KUBECTL}" -n "${NS}" get pod -l "app=${DEPLOYMENT}" -o jsonpath='{.items[0].metadata.name}')"
echo "delete ${old_pod}"
"${KUBECTL}" -n "${NS}" delete pod "${old_pod}" --wait=false

for i in $(seq 1 45); do
  code="$(curl -sS --max-time 3 -o /tmp/vllm_fault_probe.out -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${MODEL}\",\"prompt\":\"健康检查\",\"max_tokens\":4,\"temperature\":0}" \
    "${VLLM_URL}/v1/completions" || true)"
  pod_line="$("${KUBECTL}" -n "${NS}" get pods -l "app=${DEPLOYMENT}" --no-headers 2>/dev/null | tr '\n' ';')"
  echo "t=${i}s http=${code} pods=${pod_line}"
  sleep 1
done

echo "== after =="
"${KUBECTL}" -n "${NS}" get pods -l "app=${DEPLOYMENT}" -o wide
curl -sG --max-time 5 "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:num_requests_running'
echo
curl -sG --max-time 5 "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:num_requests_waiting'
echo
curl -sG --max-time 5 "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:request_success_total'
echo
