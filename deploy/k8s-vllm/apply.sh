#!/usr/bin/env bash
set -euo pipefail

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "missing required environment variable: ${name}" >&2
    exit 2
  fi
}

require_env VLLM_NODE_NAME
require_env VLLM_PROMETHEUS_NODE_NAME
require_env VLLM_GPU_UUID
require_env VLLM_MODEL_PATH
require_env VLLM_SERVED_MODEL_NAME

GPU_EXPORTER_NODE_1="${GPU_EXPORTER_NODE_1:-33.33.33.127}"
GPU_EXPORTER_NODE_1_NAME="${GPU_EXPORTER_NODE_1_NAME:-dds-127}"
GPU_EXPORTER_NODE_2="${GPU_EXPORTER_NODE_2:-33.33.33.128}"
GPU_EXPORTER_NODE_2_NAME="${GPU_EXPORTER_NODE_2_NAME:-dds-128}"

render() {
  local src="$1"
  sed \
    -e "s|[$]{VLLM_NODE_NAME}|${VLLM_NODE_NAME}|g" \
    -e "s|[$]{VLLM_PROMETHEUS_NODE_NAME}|${VLLM_PROMETHEUS_NODE_NAME}|g" \
    -e "s|[$]{VLLM_GPU_UUID}|${VLLM_GPU_UUID}|g" \
    -e "s|[$]{VLLM_MODEL_PATH}|${VLLM_MODEL_PATH}|g" \
    -e "s|[$]{VLLM_SERVED_MODEL_NAME}|${VLLM_SERVED_MODEL_NAME}|g" \
    -e "s|[$]{GPU_EXPORTER_NODE_1}|${GPU_EXPORTER_NODE_1}|g" \
    -e "s|[$]{GPU_EXPORTER_NODE_1_NAME}|${GPU_EXPORTER_NODE_1_NAME}|g" \
    -e "s|[$]{GPU_EXPORTER_NODE_2}|${GPU_EXPORTER_NODE_2}|g" \
    -e "s|[$]{GPU_EXPORTER_NODE_2_NAME}|${GPU_EXPORTER_NODE_2_NAME}|g" \
    "${src}"
}

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

render deploy/k8s-vllm/vllm-k8s.yaml > "${tmp_dir}/vllm-k8s.yaml"
render deploy/k8s-vllm/prometheus.yaml > "${tmp_dir}/prometheus.yaml"
render deploy/k8s-vllm/gpu-metrics-exporter.yaml > "${tmp_dir}/gpu-metrics-exporter.yaml"

kubectl apply -f "${tmp_dir}/vllm-k8s.yaml"
kubectl apply -f "${tmp_dir}/gpu-metrics-exporter.yaml"
kubectl apply -f "${tmp_dir}/prometheus.yaml"
