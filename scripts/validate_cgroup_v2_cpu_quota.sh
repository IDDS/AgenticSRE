#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-cgroup-validation"
target_pod="agenticsre-cgroup-probe"
control_pod="agenticsre-cgroup-control"
node_name="${AGENTICSRE_PRECISION_NODE:-lsy-3}"
runtime_endpoint="${AGENTICSRE_CRI_ENDPOINT:-unix:///var/run/cri-dockerd.sock}"
namespace_created="false"
target_cgroup=""
baseline_value=""

cleanup() {
  if [[ -n "${target_cgroup}" && -n "${baseline_value}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "printf '%s\\n' '${baseline_value}' | sudo tee '${target_cgroup}/cpu.max' >/dev/null" >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi
ssh -o BatchMode=yes "${node_name}" 'test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs' || {
  printf 'target node does not use cgroup v2: %s\n' "${node_name}" >&2
  exit 1
}

kubectl create namespace "${namespace}" >/dev/null
namespace_created="true"
for pod in "${target_pod}" "${control_pod}"; do
  kubectl run "${pod}" -n "${namespace}" \
    --image=alpine:3.18 --image-pull-policy=IfNotPresent --restart=Never \
    --labels="app=${pod},agenticsre.local/validation=cgroup-v2" \
    --overrides="{\"apiVersion\":\"v1\",\"spec\":{\"nodeName\":\"${node_name}\",\"containers\":[{\"name\":\"${pod}\",\"image\":\"alpine:3.18\",\"imagePullPolicy\":\"IfNotPresent\",\"command\":[\"/bin/sh\",\"-c\",\"while :; do :; done\"],\"resources\":{\"requests\":{\"cpu\":\"10m\",\"memory\":\"16Mi\"},\"limits\":{\"cpu\":\"1\",\"memory\":\"64Mi\"}}}]}}" >/dev/null
done
kubectl wait --for=condition=Ready "pod/${target_pod}" "pod/${control_pod}" -n "${namespace}" --timeout=90s >/dev/null

container_id_full="$(kubectl get pod "${target_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
container_id="${container_id_full#*://}"
host_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint '${runtime_endpoint}' inspect '${container_id}' | jq -r '.info.pid'")"
if [[ ! "${host_pid}" =~ ^[0-9]+$ ]] || [[ "${host_pid}" == "0" ]]; then
  printf 'failed to resolve target host PID: %s\n' "${host_pid}" >&2
  exit 1
fi
relative_cgroup="$(ssh -o BatchMode=yes "${node_name}" "sudo awk -F: '\$1 == \"0\" {print \$3}' '/proc/${host_pid}/cgroup'")"
target_cgroup="/sys/fs/cgroup${relative_cgroup}"
baseline_value="$(ssh -o BatchMode=yes "${node_name}" "sudo cat '${target_cgroup}/cpu.max'")"
baseline_snapshot="${baseline_value}"
control_container_id_full="$(kubectl get pod "${control_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
control_container_id="${control_container_id_full#*://}"
control_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint '${runtime_endpoint}' inspect '${control_container_id}' | jq -r '.info.pid'")"
control_relative="$(ssh -o BatchMode=yes "${node_name}" "sudo awk -F: '\$1 == \"0\" {print \$3}' '/proc/${control_pid}/cgroup'")"
control_cgroup="/sys/fs/cgroup${control_relative}"
control_before="$(ssh -o BatchMode=yes "${node_name}" "sudo cat '${control_cgroup}/cpu.max'")"
throttled_before="$(ssh -o BatchMode=yes "${node_name}" "sudo awk '\$1 == \"nr_throttled\" {print \$2}' '${target_cgroup}/cpu.stat'")"

applied_value="20000 100000"
ssh -o BatchMode=yes "${node_name}" "printf '%s\\n' '${applied_value}' | sudo tee '${target_cgroup}/cpu.max' >/dev/null"
readback="$(ssh -o BatchMode=yes "${node_name}" "sudo cat '${target_cgroup}/cpu.max'")"
[[ "${readback}" == "${applied_value}" ]]
sleep 4
throttled_after="$(ssh -o BatchMode=yes "${node_name}" "sudo awk '\$1 == \"nr_throttled\" {print \$2}' '${target_cgroup}/cpu.stat'")"
control_during="$(ssh -o BatchMode=yes "${node_name}" "sudo cat '${control_cgroup}/cpu.max'")"
if (( throttled_after <= throttled_before )); then
  printf 'target cgroup did not record throttling: before=%s after=%s\n' "${throttled_before}" "${throttled_after}" >&2
  exit 1
fi
[[ "${control_before}" == "${control_during}" ]]

ssh -o BatchMode=yes "${node_name}" "printf '%s\\n' '${baseline_value}' | sudo tee '${target_cgroup}/cpu.max' >/dev/null"
restored_value="$(ssh -o BatchMode=yes "${node_name}" "sudo cat '${target_cgroup}/cpu.max'")"
[[ "${restored_value}" == "${baseline_value}" ]]
baseline_value=""

kubectl delete namespace "${namespace}" --wait=true --timeout=90s >/dev/null
namespace_created="false"

printf 'before_evidence=cgroup_path:%s cpu.max:%s nr_throttled:%s\n' "${target_cgroup}" "${baseline_snapshot}" "${throttled_before}"
printf 'fault_evidence=cpu.max:%s nr_throttled:%s control_cpu.max:%s\n' "${applied_value}" "${throttled_after}" "${control_during}"
printf 'recovery_evidence=restored_cpu.max:%s namespace_removed:true\n' "${restored_value}"
printf 'validation=PASS precision=cgroup_v2 controller=cpu exact_cgroup=true control_unchanged=true value_restored=true cleanup=complete\n'
