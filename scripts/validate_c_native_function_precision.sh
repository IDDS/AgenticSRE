#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-c-native-validation"
node_name="lsy-3"
gdb_pod="agenticsre-gdb-probe"
binary_pod="agenticsre-binary-probe"
binary_errno_pod="agenticsre-binary-errno-probe"
binary_delay_pod="agenticsre-binary-delay-probe"
manifest_path="${1:-deploy/precision-fault-injection/c-native/c-native-probe-pods.yaml}"
gdb_script="${2:-deploy/precision-fault-injection/c-native/gdb-return-once.gdb}"
build_dir="$(mktemp -d)"
remote_artifact_dir=""
namespace_created="false"

cleanup() {
  if [[ -n "${remote_artifact_dir}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "rm -rf -- '${remote_artifact_dir}'" >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null || true
  fi
  rm -rf "${build_dir}"
}

trap cleanup EXIT INT TERM

for path in "${manifest_path}" "${gdb_script}"; do
  if [[ ! -f "${path}" ]]; then
    printf 'required artifact not found: %s\n' "${path}" >&2
    exit 1
  fi
done
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi
if ! ssh -o BatchMode=yes "${node_name}" 'command -v gdb >/dev/null 2>&1'; then
  printf 'gdb is required on node %s\n' "${node_name}" >&2
  exit 1
fi

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

scripts/build_c_native_probe.sh "${build_dir}"
probe_sha256="$(sha256sum "${build_dir}/probe" | awk '{print $1}')"
override_sha256="$(sha256sum "${build_dir}/libagenticsre_return_override.so" | awk '{print $1}')"

kubectl create namespace "${namespace}"
namespace_created="true"
kubectl create configmap agenticsre-c-native-artifacts -n "${namespace}" \
  --from-file=probe="${build_dir}/probe" \
  --from-file=libstable.so="${build_dir}/libstable.so" \
  --from-file=libagenticsre_return_override.so="${build_dir}/libagenticsre_return_override.so"
kubectl apply -f "${manifest_path}" --prune=false
kubectl wait --for=condition=Ready \
  "pod/${gdb_pod}" \
  "pod/${binary_pod}" \
  "pod/${binary_errno_pod}" \
  "pod/${binary_delay_pod}" \
  -n "${namespace}" --timeout=90s

gdb_uid_before="$(kubectl get pod "${gdb_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
gdb_restart_before="$(kubectl get pod "${gdb_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_uid_before="$(kubectl get pod "${binary_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_restart_before="$(kubectl get pod "${binary_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_errno_uid_before="$(kubectl get pod "${binary_errno_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_errno_restart_before="$(kubectl get pod "${binary_errno_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_delay_uid_before="$(kubectl get pod "${binary_delay_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_delay_restart_before="$(kubectl get pod "${binary_delay_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"

baseline_line=""
for _ in $(seq 1 40); do
  baseline_line="$(kubectl logs "${gdb_pod}" -n "${namespace}" --tail=1 2>/dev/null || true)"
  if [[ "${baseline_line}" == source=gdb* ]] && [[ "${baseline_line}" != *result=-88* ]]; then
    break
  fi
  sleep 1
done
if [[ "${baseline_line}" != source=gdb* ]]; then
  printf 'GDB probe baseline is not healthy: %s\n' "${baseline_line}" >&2
  exit 1
fi

container_id_full="$(kubectl get pod "${gdb_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
container_id="${container_id_full#*://}"
host_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint unix:///var/run/cri-dockerd.sock inspect '${container_id}' | jq -r '.info.pid'")"
if [[ ! "${host_pid}" =~ ^[0-9]+$ ]] || [[ "${host_pid}" == "0" ]]; then
  printf 'failed to resolve GDB target host PID: %s\n' "${host_pid}" >&2
  exit 1
fi

remote_artifact_dir="$(ssh -o BatchMode=yes "${node_name}" 'mktemp -d /tmp/agenticsre-c-native.XXXXXX')"
scp -q "${gdb_script}" "${build_dir}/libstable.so" "${node_name}:${remote_artifact_dir}/"
stable_map="$(ssh -o BatchMode=yes "${node_name}" "sudo awk '\$0 ~ /libstable\\.so/ && \$3 == \"00000000\" {print; exit}' '/proc/${host_pid}/maps'")"
mapping_start="$(printf '%s\n' "${stable_map}" | awk '{split($1, part, "-"); print part[1]}')"
symbol_offset="$(readelf -Ws "${build_dir}/libstable.so" | awk '$8 == "stable_operation" {print $2; exit}')"
if [[ ! "${mapping_start}" =~ ^[0-9a-fA-F]+$ ]] || [[ ! "${symbol_offset}" =~ ^[0-9a-fA-F]+$ ]]; then
  printf 'failed to resolve reviewed ELF symbol address: map=%s mapping=%s symbol=%s\n' "${stable_map}" "${mapping_start}" "${symbol_offset}" >&2
  exit 1
fi
symbol_address="$(printf '%x' "$((16#${mapping_start} + 16#${symbol_offset}))")"
gdb_output="$(ssh -o BatchMode=yes "${node_name}" "sudo timeout 20s gdb -q -nx -batch -ex 'set \$agenticsre_target = (void *)0x${symbol_address}' -p '${host_pid}' -x '${remote_artifact_dir}/gdb-return-once.gdb'")"
if [[ "${gdb_output}" != *"injection_event=gdb_return_override"* ]]; then
  printf 'GDB did not report the reviewed return override: %s\n' "${gdb_output}" >&2
  exit 1
fi

gdb_fault_line=""
for _ in $(seq 1 20); do
  gdb_fault_line="$(kubectl logs "${gdb_pod}" -n "${namespace}" | grep -m1 'source=gdb .*result=-88' || true)"
  [[ -n "${gdb_fault_line}" ]] && break
  sleep 1
done
if [[ -z "${gdb_fault_line}" ]]; then
  printf 'GDB probe did not observe result=-88\n' >&2
  exit 1
fi
gdb_fault_seq="$(printf '%s\n' "${gdb_fault_line}" | sed -E 's/.*seq=([0-9]+).*/\1/')"
gdb_recovery_line=""
for _ in $(seq 1 20); do
  gdb_recovery_line="$(kubectl logs "${gdb_pod}" -n "${namespace}" | awk -v fault_seq="${gdb_fault_seq}" '/source=gdb/ {seq=$2; sub("seq=", "", seq); if (seq > fault_seq && $3 != "result=-88") line=$0} END {print line}')"
  [[ -n "${gdb_recovery_line}" ]] && break
  sleep 1
done
if [[ -z "${gdb_recovery_line}" ]]; then
  printf 'GDB probe did not recover after debugger detach\n' >&2
  exit 1
fi

binary_fault_line=""
binary_event_line=""
for _ in $(seq 1 30); do
  binary_logs="$(kubectl logs "${binary_pod}" -n "${namespace}" 2>&1 || true)"
  binary_fault_line="$(printf '%s\n' "${binary_logs}" | grep -m1 'source=binary .*result=-77' || true)"
  binary_event_line="$(printf '%s\n' "${binary_logs}" | grep -m1 'instrumentation_event=return_override' || true)"
  [[ -n "${binary_fault_line}" && -n "${binary_event_line}" ]] && break
  sleep 1
done
if [[ -z "${binary_fault_line}" || -z "${binary_event_line}" ]]; then
  printf 'ELF dynamic instrumentation evidence is incomplete\n' >&2
  exit 1
fi
binary_fault_count="$(kubectl logs "${binary_pod}" -n "${namespace}" 2>&1 | grep -c 'source=binary .*result=-77' || true)"
if [[ "${binary_fault_count}" != "1" ]]; then
  printf 'ELF injection count mismatch: %s\n' "${binary_fault_count}" >&2
  exit 1
fi
binary_fault_seq="$(printf '%s\n' "${binary_fault_line}" | sed -E 's/.*seq=([0-9]+).*/\1/')"
binary_recovery_line="$(kubectl logs "${binary_pod}" -n "${namespace}" 2>&1 | awk -v fault_seq="${binary_fault_seq}" '/source=binary/ {seq=$2; sub("seq=", "", seq); if (seq > fault_seq && $3 != "result=-77") line=$0} END {print line}')"
if [[ -z "${binary_recovery_line}" ]]; then
  printf 'ELF probe did not recover after bounded hit count\n' >&2
  exit 1
fi

binary_errno_fault_line=""
binary_errno_event_line=""
for _ in $(seq 1 30); do
  binary_errno_logs="$(kubectl logs "${binary_errno_pod}" -n "${namespace}" 2>&1 || true)"
  binary_errno_fault_line="$(printf '%s\n' "${binary_errno_logs}" | grep -m1 'source=binary-errno .*result=-1 errno=5' || true)"
  binary_errno_event_line="$(printf '%s\n' "${binary_errno_logs}" | grep -m1 'instrumentation_event=errno .*errno=5 return=-1' || true)"
  [[ -n "${binary_errno_fault_line}" && -n "${binary_errno_event_line}" ]] && break
  sleep 1
done
if [[ -z "${binary_errno_fault_line}" || -z "${binary_errno_event_line}" ]]; then
  printf 'ELF errno instrumentation evidence is incomplete\n' >&2
  exit 1
fi
binary_errno_fault_count="$(kubectl logs "${binary_errno_pod}" -n "${namespace}" 2>&1 | grep -c 'source=binary-errno .*result=-1 errno=5' || true)"
binary_errno_event_count="$(kubectl logs "${binary_errno_pod}" -n "${namespace}" 2>&1 | grep -c 'instrumentation_event=errno' || true)"
if [[ "${binary_errno_fault_count}" != "1" || "${binary_errno_event_count}" != "1" ]]; then
  printf 'ELF errno injection count mismatch: fault=%s event=%s\n' "${binary_errno_fault_count}" "${binary_errno_event_count}" >&2
  exit 1
fi
binary_errno_fault_seq="$(printf '%s\n' "${binary_errno_fault_line}" | sed -E 's/.*seq=([0-9]+).*/\1/')"
binary_errno_recovery_line="$(kubectl logs "${binary_errno_pod}" -n "${namespace}" 2>&1 | awk -v fault_seq="${binary_errno_fault_seq}" '/source=binary-errno/ {seq=$2; err=$4; sub("seq=", "", seq); if (seq > fault_seq && err == "errno=0") line=$0} END {print line}')"
if [[ -z "${binary_errno_recovery_line}" ]]; then
  printf 'ELF errno probe did not recover after bounded hit count\n' >&2
  exit 1
fi

binary_delay_fault_line=""
binary_delay_event_line=""
for _ in $(seq 1 30); do
  binary_delay_logs="$(kubectl logs "${binary_delay_pod}" -n "${namespace}" 2>&1 || true)"
  binary_delay_fault_line="$(printf '%s\n' "${binary_delay_logs}" | awk '/source=binary-delay/ {lat=$5; sub("latency_ms=", "", lat); if (lat >= 300) {print; exit}}')"
  binary_delay_event_line="$(printf '%s\n' "${binary_delay_logs}" | grep -m1 'instrumentation_event=delay .*delay_ms=350' || true)"
  [[ -n "${binary_delay_fault_line}" && -n "${binary_delay_event_line}" ]] && break
  sleep 1
done
if [[ -z "${binary_delay_fault_line}" || -z "${binary_delay_event_line}" ]]; then
  printf 'ELF delay instrumentation evidence is incomplete\n' >&2
  exit 1
fi
binary_delay_event_count="$(kubectl logs "${binary_delay_pod}" -n "${namespace}" 2>&1 | grep -c 'instrumentation_event=delay' || true)"
if [[ "${binary_delay_event_count}" != "1" ]]; then
  printf 'ELF delay injection count mismatch: %s\n' "${binary_delay_event_count}" >&2
  exit 1
fi
binary_delay_fault_seq="$(printf '%s\n' "${binary_delay_fault_line}" | sed -E 's/.*seq=([0-9]+).*/\1/')"
binary_delay_recovery_line="$(kubectl logs "${binary_delay_pod}" -n "${namespace}" 2>&1 | awk -v fault_seq="${binary_delay_fault_seq}" '/source=binary-delay/ {seq=$2; lat=$5; sub("seq=", "", seq); sub("latency_ms=", "", lat); if (seq > fault_seq && lat < 100) line=$0} END {print line}')"
if [[ -z "${binary_delay_recovery_line}" ]]; then
  printf 'ELF delay probe did not recover after bounded hit count\n' >&2
  exit 1
fi

gdb_uid_after="$(kubectl get pod "${gdb_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
gdb_restart_after="$(kubectl get pod "${gdb_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_uid_after="$(kubectl get pod "${binary_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_restart_after="$(kubectl get pod "${binary_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_errno_uid_after="$(kubectl get pod "${binary_errno_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_errno_restart_after="$(kubectl get pod "${binary_errno_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
binary_delay_uid_after="$(kubectl get pod "${binary_delay_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
binary_delay_restart_after="$(kubectl get pod "${binary_delay_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
agenticsre_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

[[ "${gdb_uid_before}" == "${gdb_uid_after}" ]]
[[ "${gdb_restart_before}" == "${gdb_restart_after}" ]]
[[ "${binary_uid_before}" == "${binary_uid_after}" ]]
[[ "${binary_restart_before}" == "${binary_restart_after}" ]]
[[ "${binary_errno_uid_before}" == "${binary_errno_uid_after}" ]]
[[ "${binary_errno_restart_before}" == "${binary_errno_restart_after}" ]]
[[ "${binary_delay_uid_before}" == "${binary_delay_uid_after}" ]]
[[ "${binary_delay_restart_before}" == "${binary_delay_restart_after}" ]]
[[ "${agenticsre_before}" == "${agenticsre_after}" ]]
[[ "${agenticsre_runtime_before}" == "${agenticsre_runtime_after}" ]]

ssh -o BatchMode=yes "${node_name}" "rm -rf -- '${remote_artifact_dir}'"
remote_artifact_dir=""
kubectl delete namespace "${namespace}" --wait=true --timeout=90s
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'validation namespace still exists after cleanup\n' >&2
  exit 1
fi

printf 'gdb_scope=node:%s pid:%s binary_sha256:%s symbol:stable_operation hit_count:1 return:-88\n' \
  "${node_name}" "${host_pid}" "${probe_sha256}"
printf 'gdb_fault_evidence=%s\n' "${gdb_fault_line}"
printf 'gdb_recovery_evidence=%s\n' "${gdb_recovery_line}"
printf 'binary_scope=provider:ld_preload artifact:agenticsre.cfi.int32.v2 artifact_sha256:%s abi_profile:int32_int32 symbol:stable_operation hit_count:3 max_hits:1 return:-77\n' \
  "${override_sha256}"
printf 'binary_fault_evidence=%s\n' "${binary_fault_line}"
printf 'binary_event_evidence=%s\n' "${binary_event_line}"
printf 'binary_recovery_evidence=%s\n' "${binary_recovery_line}"
printf 'binary_errno_fault_evidence=%s\n' "${binary_errno_fault_line}"
printf 'binary_errno_event_evidence=%s\n' "${binary_errno_event_line}"
printf 'binary_errno_recovery_evidence=%s\n' "${binary_errno_recovery_line}"
printf 'binary_delay_fault_evidence=%s\n' "${binary_delay_fault_line}"
printf 'binary_delay_event_evidence=%s\n' "${binary_delay_event_line}"
printf 'binary_delay_recovery_evidence=%s\n' "${binary_delay_recovery_line}"
printf 'validation=PASS precision=c_native methods=gdb,binary fault_types=gdb_function_return,elf_function_return,elf_function_errno,elf_function_delay target_processes=4 exact_symbol=stable_operation abi_profile=int32_int32 max_hits=1 pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n'
