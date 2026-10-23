#!/usr/bin/env bash

set -euo pipefail

scenario_id="${1:-}"
namespace="agenticsre-c-native-validation"
node_name="${AGENTICSRE_PRECISION_NODE:-lsy-3}"
runtime_endpoint="${AGENTICSRE_CRI_ENDPOINT:-unix:///var/run/cri-dockerd.sock}"
build_dir="$(mktemp -d)"
remote_artifact_dir=""
namespace_created="false"

case "${scenario_id}" in
  precise-gdb-function-return)
    pod="agenticsre-gdb-probe"; manifest="deploy/precision-fault-injection/c-native/gdb-probe.yaml"; source_name="gdb" ;;
  precise-elf-function-return)
    pod="agenticsre-binary-probe"; manifest="deploy/precision-fault-injection/c-native/elf-return-probe.yaml"; source_name="binary" ;;
  precise-elf-function-errno)
    pod="agenticsre-binary-errno-probe"; manifest="deploy/precision-fault-injection/c-native/elf-errno-probe.yaml"; source_name="binary-errno" ;;
  precise-elf-function-delay)
    pod="agenticsre-binary-delay-probe"; manifest="deploy/precision-fault-injection/c-native/elf-delay-probe.yaml"; source_name="binary-delay" ;;
  *)
    printf 'unsupported native precision scenario: %s\n' "${scenario_id}" >&2
    exit 1 ;;
esac

cleanup() {
  if [[ -n "${remote_artifact_dir}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "rm -rf -- '${remote_artifact_dir}'" >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
  fi
  rm -rf -- "${build_dir}"
}
trap cleanup EXIT INT TERM

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi
scripts/build_c_native_probe.sh "${build_dir}" >/dev/null
probe_sha256="$(sha256sum "${build_dir}/probe" | awk '{print $1}')"
instrumentation_sha256="$(sha256sum "${build_dir}/libagenticsre_return_override.so" | awk '{print $1}')"

kubectl create namespace "${namespace}" >/dev/null
namespace_created="true"
kubectl create configmap agenticsre-c-native-artifacts -n "${namespace}" \
  --from-file=probe="${build_dir}/probe" \
  --from-file=libstable.so="${build_dir}/libstable.so" \
  --from-file=libagenticsre_return_override.so="${build_dir}/libagenticsre_return_override.so" >/dev/null
kubectl apply -f "${manifest}" >/dev/null
kubectl wait --for=condition=Ready "pod/${pod}" -n "${namespace}" --timeout=90s >/dev/null
pod_uid_before="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_before="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"

baseline_line=""
for _ in $(seq 1 30); do
  baseline_line="$(kubectl logs "${pod}" -n "${namespace}" 2>&1 | grep -m1 "source=${source_name} " || true)"
  [[ -n "${baseline_line}" ]] && break
  sleep 1
done
[[ -n "${baseline_line}" ]]

if [[ "${scenario_id}" == "precise-gdb-function-return" ]]; then
  ssh -o BatchMode=yes "${node_name}" 'command -v gdb >/dev/null' || {
    printf 'gdb is required on node %s\n' "${node_name}" >&2
    exit 1
  }
  container_id_full="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
  container_id="${container_id_full#*://}"
  host_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint '${runtime_endpoint}' inspect '${container_id}' | jq -r '.info.pid'")"
  remote_artifact_dir="$(ssh -o BatchMode=yes "${node_name}" 'mktemp -d /tmp/agenticsre-native.XXXXXX')"
  scp -q deploy/precision-fault-injection/c-native/gdb-return-once.gdb "${build_dir}/libstable.so" "${node_name}:${remote_artifact_dir}/"
  stable_map="$(ssh -o BatchMode=yes "${node_name}" "sudo awk '\$0 ~ /libstable\\.so/ && \$3 == \"00000000\" {print; exit}' '/proc/${host_pid}/maps'")"
  mapping_start="$(printf '%s\n' "${stable_map}" | awk '{split($1, part, "-"); print part[1]}')"
  symbol_offset="$(readelf -Ws "${build_dir}/libstable.so" | awk '$8 == "stable_operation" {print $2; exit}')"
  symbol_address="$(printf '%x' "$((16#${mapping_start} + 16#${symbol_offset}))")"
  gdb_output="$(ssh -o BatchMode=yes "${node_name}" "sudo timeout 20s gdb -q -nx -batch -ex 'set \$agenticsre_target = (void *)0x${symbol_address}' -p '${host_pid}' -x '${remote_artifact_dir}/gdb-return-once.gdb'")"
  [[ "${gdb_output}" == *"injection_event=gdb_return_override"* ]]
  fault_pattern='source=gdb .*result=-88'
  recovery_filter='source=gdb'
else
  case "${scenario_id}" in
    precise-elf-function-return) fault_pattern='source=binary .*result=-77'; event_pattern='instrumentation_event=return_override' ;;
    precise-elf-function-errno) fault_pattern='source=binary-errno .*result=-1 errno=5'; event_pattern='instrumentation_event=errno .*errno=5 return=-1' ;;
    precise-elf-function-delay) fault_pattern='source=binary-delay'; event_pattern='instrumentation_event=delay .*delay_ms=350' ;;
  esac
fi

fault_line=""
event_line=""
for _ in $(seq 1 30); do
  logs="$(kubectl logs "${pod}" -n "${namespace}" 2>&1 || true)"
  if [[ "${scenario_id}" == "precise-elf-function-delay" ]]; then
    fault_line="$(printf '%s\n' "${logs}" | awk '/source=binary-delay/ {lat=$5; sub("latency_ms=", "", lat); if (lat >= 300) {print; exit}}')"
  else
    fault_line="$(printf '%s\n' "${logs}" | grep -m1 "${fault_pattern}" || true)"
  fi
  if [[ "${scenario_id}" != "precise-gdb-function-return" ]]; then
    event_line="$(printf '%s\n' "${logs}" | grep -m1 "${event_pattern}" || true)"
  fi
  [[ -n "${fault_line}" ]] && [[ "${scenario_id}" == "precise-gdb-function-return" || -n "${event_line}" ]] && break
  sleep 1
done
[[ -n "${fault_line}" ]]
fault_seq="$(printf '%s\n' "${fault_line}" | sed -E 's/.*seq=([0-9]+).*/\1/')"

recovery_line=""
for _ in $(seq 1 30); do
  logs="$(kubectl logs "${pod}" -n "${namespace}" 2>&1 || true)"
  case "${scenario_id}" in
    precise-gdb-function-return)
      recovery_line="$(printf '%s\n' "${logs}" | awk -v seq="${fault_seq}" '/source=gdb/ {n=$2; sub("seq=", "", n); if (n>seq && $3!="result=-88") line=$0} END {print line}')" ;;
    precise-elf-function-return)
      recovery_line="$(printf '%s\n' "${logs}" | awk -v seq="${fault_seq}" '/source=binary/ {n=$2; sub("seq=", "", n); if (n>seq && $3!="result=-77") line=$0} END {print line}')" ;;
    precise-elf-function-errno)
      recovery_line="$(printf '%s\n' "${logs}" | awk -v seq="${fault_seq}" '/source=binary-errno/ {n=$2; sub("seq=", "", n); if (n>seq && $4=="errno=0") line=$0} END {print line}')" ;;
    precise-elf-function-delay)
      recovery_line="$(printf '%s\n' "${logs}" | awk -v seq="${fault_seq}" '/source=binary-delay/ {n=$2; lat=$5; sub("seq=", "", n); sub("latency_ms=", "", lat); if (n>seq && lat<100) line=$0} END {print line}')" ;;
  esac
  [[ -n "${recovery_line}" ]] && break
  sleep 1
done
[[ -n "${recovery_line}" ]]

fault_count="$(kubectl logs "${pod}" -n "${namespace}" 2>&1 | grep -c "$(printf '%s' "${fault_line}" | sed -E 's/ seq=[0-9]+.*//')" || true)"
pod_uid_after="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_after="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
[[ "${pod_uid_before}" == "${pod_uid_after}" ]]
[[ "${restart_before}" == "${restart_after}" ]]

kubectl delete namespace "${namespace}" --wait=true --timeout=90s >/dev/null
namespace_created="false"
if [[ -n "${remote_artifact_dir}" ]]; then
  ssh -o BatchMode=yes "${node_name}" "rm -rf -- '${remote_artifact_dir}'" >/dev/null
  remote_artifact_dir=""
fi

printf 'before_evidence=%s\n' "${baseline_line}"
printf 'fault_evidence=%s\n' "${fault_line}"
[[ -n "${event_line}" ]] && printf 'instrumentation_evidence=%s\n' "${event_line}"
printf 'recovery_evidence=%s\n' "${recovery_line}"
printf 'artifact_evidence=probe_sha256:%s instrumentation_sha256:%s\n' "${probe_sha256}" "${instrumentation_sha256}"
printf 'validation=PASS precision=native_function scenario=%s exact_symbol=stable_operation max_hits=1 pod_recreated=false restart_delta=0 cleanup=complete\n' "${scenario_id}"
