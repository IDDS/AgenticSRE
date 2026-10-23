#!/usr/bin/env bash
set -euo pipefail

VLLM_URL="${VLLM_URL:-http://127.0.0.1:30080}"
PROM_URL="${PROM_URL:-http://127.0.0.1:30090}"
MODEL="${MODEL:-qwen2.5-coder-3b-k8s}"

echo "== vLLM health =="
curl -fsS "${VLLM_URL}/health"
echo

echo "== vLLM models =="
curl -fsS "${VLLM_URL}/v1/models"
echo

echo "== one inference request =="
curl -fsS -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"用一句中文说明 Kubernetes 自愈机制。\",\"max_tokens\":16,\"temperature\":0}" \
  "${VLLM_URL}/v1/completions"
echo

echo "== Prometheus target =="
curl -fsS "${PROM_URL}/api/v1/targets"
echo

echo "== request_success_total =="
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=vllm:request_success_total'
echo

echo "== E2E P95 =="
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket[2m])) by (le, model_name))'
echo

echo "== TTFT P95 =="
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket[2m])) by (le, model_name))'
echo
