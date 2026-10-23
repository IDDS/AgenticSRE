#!/usr/bin/env bash
set -euo pipefail

VLLM_URL="${VLLM_URL:-http://127.0.0.1:30081}"
PROM_URL="${PROM_URL:-http://127.0.0.1:30090}"
MODEL="${MODEL:-${GEMMA_SERVED_MODEL_NAME:-gemma-12b}}"

echo "== Gemma vLLM health =="
curl -fsS "${VLLM_URL}/health"
echo

echo "== Gemma vLLM models =="
curl -fsS "${VLLM_URL}/v1/models"
echo

echo "== one Chinese inference request =="
curl -fsS -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"用一句中文说明 GPU 推理服务如何做故障自愈。\",\"max_tokens\":32,\"temperature\":0}" \
  "${VLLM_URL}/v1/completions"
echo

echo "== Gemma Prometheus target =="
curl -fsSG "${PROM_URL}/api/v1/targets" | grep -E 'vllm-gemma|gemma|health' || true
echo

echo "== Gemma request_success_total =="
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode "query=vllm:request_success_total{model_name=\"${MODEL}\"}"
echo

echo "== Gemma E2E P95 =="
curl -fsSG "${PROM_URL}/api/v1/query" \
  --data-urlencode "query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket{model_name=\"${MODEL}\"}[2m])) by (le, model_name))"
echo
