#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-dcgm-validation"
pod="agenticsre-dcgm-probe"
context="${AGENTICSRE_GPU_CONTEXT:-kubernetes-admin@kubernetes}"
image="${AGENTICSRE_DCGM_IMAGE:-nvcr.io/nvidia/cloud-native/dcgm:4.3.1-1-ubuntu22.04}"
node_name="${AGENTICSRE_GPU_NODE:-dds-128}"
field_id="202"
injected_value="100"
namespace_created="false"

if [[ -z "${KUBECONFIG:-}" && -f /home/ubuntu/agenticsre/data/t4-admin.conf ]]; then
  export KUBECONFIG=/home/ubuntu/agenticsre/data/t4-admin.conf
fi

kctl() {
  if [[ -n "${context}" ]]; then
    kubectl --context="${context}" "$@"
  else
    kubectl "$@"
  fi
}

cleanup() {
  if [[ "${namespace_created}" == "true" ]]; then
    kctl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if kctl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi
allocatable="$(kctl get node "${node_name}" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')"
reserved="$(kctl get pods -A --field-selector "spec.nodeName=${node_name}" -o json | jq '[.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed") | ([.spec.containers[] | (.resources.requests["nvidia.com/gpu"] // .resources.limits["nvidia.com/gpu"] // 0) | tonumber] | add // 0)] | add // 0')"
if (( allocatable - reserved < 1 )); then
  printf 'no isolated GPU is currently available: node=%s allocatable=%s reserved=%s\n' "${node_name}" "${allocatable}" "${reserved}" >&2
  exit 2
fi

kctl create namespace "${namespace}" >/dev/null
namespace_created="true"
kctl -n "${namespace}" run "${pod}" --image="${image}" --restart=Never \
  --overrides="{\"apiVersion\":\"v1\",\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"${node_name}\"},\"containers\":[{\"name\":\"${pod}\",\"image\":\"${image}\",\"command\":[\"/bin/bash\",\"-lc\",\"nv-hostengine -n; trap : TERM INT; sleep infinity & wait\"],\"resources\":{\"limits\":{\"nvidia.com/gpu\":1}}}]}}" >/dev/null
kctl -n "${namespace}" wait --for=condition=Ready "pod/${pod}" --timeout=180s >/dev/null

gpu_identity="$(kctl -n "${namespace}" exec "${pod}" -- nvidia-smi --query-gpu=index,uuid,name --format=csv,noheader | head -1 | tr -d '\r')"
discovery="$(kctl -n "${namespace}" exec "${pod}" -- dcgmi discovery -l | tr '\n' ' ' | tr -s ' ')"
baseline_dump="$(kctl -n "${namespace}" exec "${pod}" -- dcgmi test --introspect --gpuid 0 --field "${field_id}" | tr '\n' ' ' | tr -s ' ')"

inject_output="$(kctl -n "${namespace}" exec "${pod}" -- dcgmi test --inject --gpuid 0 --field "${field_id}" --value "${injected_value}" --offset 1)"
[[ "${inject_output}" == *"Successfully injected field info"* ]]
sleep 2
active_dump="$(kctl -n "${namespace}" exec "${pod}" -- dcgmi test --introspect --gpuid 0 --field "${field_id}" | tr '\n' ' ' | tr -s ' ')"
if [[ "${active_dump}" != *"${injected_value}"* ]]; then
  printf 'DCGM cache does not expose the injected PCIe replay value\n%s\n' "${active_dump}" >&2
  exit 1
fi

# A newer timestamp with the captured baseline semantics expires the synthetic
# sample.  Zero is the reviewed healthy baseline for this isolated metric test.
kctl -n "${namespace}" exec "${pod}" -- dcgmi test --inject --gpuid 0 --field "${field_id}" --value 0 --offset 0 >/dev/null
sleep 2
recovery_dump="$(kctl -n "${namespace}" exec "${pod}" -- dcgmi test --introspect --gpuid 0 --field "${field_id}" | tr '\n' ' ' | tr -s ' ')"
if [[ "${recovery_dump}" == *"${injected_value}"* ]]; then
  printf 'DCGM injected sample did not expire after baseline override\n%s\n' "${recovery_dump}" >&2
  exit 1
fi

kctl delete namespace "${namespace}" --wait=true --timeout=120s >/dev/null
namespace_created="false"

printf 'before_evidence=gpu:%s field:%s cache:%s discovery:%s\n' "${gpu_identity}" "${field_id}" "${baseline_dump}" "${discovery}"
printf 'fault_evidence=field:%s injected_value:%s cache:%s simulation_only:true\n' "${field_id}" "${injected_value}" "${active_dump}"
printf 'recovery_evidence=baseline_override:0 cache:%s namespace_removed:true\n' "${recovery_dump}"
printf 'validation=PASS precision=dcgm_cache_field field_id=202 single_gpu=true physical_fault=false sample_expired=true cleanup=complete\n'
