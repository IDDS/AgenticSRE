#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-strace-validation"
pod_name="agenticsre-strace-probe"
container_name="probe"
node_name="lsy-3"
manifest_path="${1:-deploy/precision-fault-injection/strace-syscall-probe.yaml}"
namespace_created="false"
trace_file=""

cleanup() {
  if [[ -n "${trace_file}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo rm -f '${trace_file}'" >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if [[ ! -f "${manifest_path}" ]]; then
  printf 'manifest not found: %s\n' "${manifest_path}" >&2
  exit 1
fi

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

kubectl apply -f "${manifest_path}"
namespace_created="true"
kubectl wait --for=condition=Ready "pod/${pod_name}" -n "${namespace}" --timeout=90s

pod_uid_before="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_count_before="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
baseline_line=""
for _ in $(seq 1 30); do
  baseline_line="$(kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" --tail=1 2>/dev/null || true)"
  if [[ "${baseline_line}" == result=ok* ]]; then
    break
  fi
  sleep 1
done
if [[ "${baseline_line}" != result=ok* ]]; then
  printf 'probe baseline is not healthy: %s\n' "${baseline_line}" >&2
  exit 1
fi

container_id_full="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
container_id="${container_id_full#*://}"
if [[ ! "${container_id}" =~ ^[a-f0-9]{64}$ ]]; then
  printf 'unexpected container id: %s\n' "${container_id_full}" >&2
  exit 1
fi

host_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint unix:///var/run/cri-dockerd.sock inspect '${container_id}' | jq -r '.info.pid'")"
if [[ ! "${host_pid}" =~ ^[0-9]+$ ]] || [[ "${host_pid}" == "0" ]]; then
  printf 'failed to resolve host pid for %s: %s\n' "${container_id}" "${host_pid}" >&2
  exit 1
fi

trace_file="$(ssh -o BatchMode=yes "${node_name}" 'sudo mktemp /tmp/agenticsre-strace-probe.XXXXXX')"
process_evidence="$(ssh -o BatchMode=yes "${node_name}" "ps -o pid=,ppid=,user=,comm=,args= -p '${host_pid}'")"
printf 'scope namespace=%s pod=%s node=%s pid=%s syscall=openat error=EACCES when=1 duration=3s\n' \
  "${namespace}" "${pod_name}" "${node_name}" "${host_pid}"
printf 'process_evidence=%s\n' "${process_evidence}"

set +e
ssh -o BatchMode=yes "${node_name}" \
  "sudo timeout --signal=INT 3s strace -f -qq -p '${host_pid}' -e trace=openat -e inject=openat:error=EACCES:when=1 -o '${trace_file}'"
strace_status="$?"
set -e
if [[ "${strace_status}" != "0" && "${strace_status}" != "124" ]]; then
  printf 'strace injection command failed with status %s\n' "${strace_status}" >&2
  exit 1
fi

trace_dump="$(ssh -o BatchMode=yes "${node_name}" "sudo tail -n 40 '${trace_file}'" || true)"

fault_line=""
for _ in $(seq 1 20); do
  fault_line="$(kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" | grep -m1 'result=fault.*errno=13.*name=EACCES' || true)"
  if [[ -n "${fault_line}" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "${fault_line}" ]]; then
  printf 'probe did not observe the injected EACCES syscall failure\n' >&2
  printf 'trace_dump=%s\n' "${trace_dump}" >&2
  exit 1
fi

trace_evidence="$(ssh -o BatchMode=yes "${node_name}" "sudo grep -m1 'INJECTED' '${trace_file}'" || true)"
if [[ -z "${trace_evidence}" ]]; then
  printf 'strace output does not contain INJECTED evidence\n' >&2
  exit 1
fi

sleep 1
recovered_line="$(kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" --tail=1)"
if [[ "${recovered_line}" != result=ok* ]]; then
  printf 'probe did not recover after strace detached: %s\n' "${recovered_line}" >&2
  exit 1
fi

pod_uid_after="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_count_after="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
ready_after="$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].ready}')"
agenticsre_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

[[ "${pod_uid_before}" == "${pod_uid_after}" ]]
[[ "${restart_count_before}" == "${restart_count_after}" ]]
[[ "${ready_after}" == "true" ]]
[[ "${agenticsre_before}" == "${agenticsre_after}" ]]
[[ "${agenticsre_runtime_before}" == "${agenticsre_runtime_after}" ]]

ssh -o BatchMode=yes "${node_name}" "sudo rm -f '${trace_file}'"
trace_file=""
kubectl delete namespace "${namespace}" --wait=true --timeout=90s
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'validation namespace still exists after cleanup: %s\n' "${namespace}" >&2
  exit 1
fi

printf 'fault_evidence=%s\n' "${fault_line}"
printf 'trace_evidence=%s\n' "${trace_evidence}"
printf 'recovery_evidence=%s\n' "${recovered_line}"
printf 'validation=PASS precision=syscall process_scope=single syscall=openat injected_errno=EACCES injection_count=1 pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n'
