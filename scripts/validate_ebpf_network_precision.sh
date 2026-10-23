#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-ebpf-validation"
client_pod="agenticsre-ebpf-client"
server_pod="agenticsre-ebpf-server"
container_name="client"
node_name="lsy-3"
target_port="18080"
control_port="18081"
max_failures="3"
manifest_path="${1:-deploy/precision-fault-injection/ebpf/ebpf-network-probe.yaml}"
object_path="${2:-deploy/precision-fault-injection/ebpf/connect4_deny.o}"
namespace_created="false"
bpf_attached="false"
cgroup_path=""
program_pin=""
map_dir=""
remote_object=""

cleanup() {
  if [[ "${bpf_attached}" == "true" && -n "${cgroup_path}" && -n "${program_pin}" ]]; then
    ssh -o BatchMode=yes "${node_name}" \
      "sudo bpftool cgroup detach '${cgroup_path}' connect4 pinned '${program_pin}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${program_pin}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo rm -f '${program_pin}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${map_dir}" ]]; then
    ssh -o BatchMode=yes "${node_name}" \
      "sudo rm -f '${map_dir}/config_map' '${map_dir}/failure_map'; sudo rmdir '${map_dir}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${remote_object}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo rm -f '${remote_object}'" >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=120s >/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if [[ ! -f "${object_path}" ]]; then
  scripts/build_ebpf_connect4_probe.sh >/dev/null
fi

for required_file in "${manifest_path}" "${object_path}"; do
  if [[ ! -f "${required_file}" ]]; then
    printf 'required file not found: %s\n' "${required_file}" >&2
    exit 1
  fi
done
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

kubectl apply -f "${manifest_path}"
namespace_created="true"
kubectl wait --for=condition=Ready "pod/${server_pod}" -n "${namespace}" --timeout=120s
kubectl wait --for=condition=Ready "pod/${client_pod}" -n "${namespace}" --timeout=120s

baseline_logs=""
for _ in $(seq 1 40); do
  baseline_logs="$(kubectl logs "${client_pod}" -n "${namespace}" -c "${container_name}" --tail=20 2>/dev/null || true)"
  if grep -q "result=ok port=${target_port}" <<<"${baseline_logs}" && \
     grep -q "result=ok port=${control_port}" <<<"${baseline_logs}"; then
    break
  fi
  sleep 1
done
if ! grep -q "result=ok port=${target_port}" <<<"${baseline_logs}" || \
   ! grep -q "result=ok port=${control_port}" <<<"${baseline_logs}"; then
  printf 'client/server baseline did not become healthy\n%s\n' "${baseline_logs}" >&2
  exit 1
fi

service_ip="$(kubectl get service agenticsre-ebpf-server -n "${namespace}" -o jsonpath='{.spec.clusterIP}')"
IFS=. read -r ip_a ip_b ip_c ip_d <<<"${service_ip}"
for octet in "${ip_a}" "${ip_b}" "${ip_c}" "${ip_d}"; do
  if [[ ! "${octet}" =~ ^[0-9]+$ ]] || (( octet < 0 || octet > 255 )); then
    printf 'invalid service IPv4 address: %s\n' "${service_ip}" >&2
    exit 1
  fi
done

container_id_full="$(kubectl get pod "${client_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].containerID}')"
container_id="${container_id_full#*://}"
host_pid="$(ssh -o BatchMode=yes "${node_name}" "sudo crictl --runtime-endpoint unix:///var/run/cri-dockerd.sock inspect '${container_id}' | jq -r '.info.pid'")"
cgroup_relative="$(ssh -o BatchMode=yes "${node_name}" "sudo awk -F: '\$1 == \"0\" {print \$3}' '/proc/${host_pid}/cgroup'")"
cgroup_path="/sys/fs/cgroup${cgroup_relative}"
if ! ssh -o BatchMode=yes "${node_name}" "test -d '${cgroup_path}'"; then
  printf 'client cgroup path not found: %s\n' "${cgroup_path}" >&2
  exit 1
fi

program_pin="/sys/fs/bpf/agenticsre-connect4-${host_pid}"
map_dir="/sys/fs/bpf/agenticsre-connect4-${host_pid}-maps"
remote_object="/tmp/agenticsre-connect4-${host_pid}.o"
scp -q "${object_path}" "${node_name}:${remote_object}"
ssh -o BatchMode=yes "${node_name}" \
  "sudo mkdir '${map_dir}'; sudo bpftool prog load '${remote_object}' '${program_pin}' pinmaps '${map_dir}'"

printf -v ip_a_hex '%02x' "${ip_a}"
printf -v ip_b_hex '%02x' "${ip_b}"
printf -v ip_c_hex '%02x' "${ip_c}"
printf -v ip_d_hex '%02x' "${ip_d}"
port_high=$(( target_port / 256 ))
port_low=$(( target_port % 256 ))
printf -v port_high_hex '%02x' "${port_high}"
printf -v port_low_hex '%02x' "${port_low}"
printf -v max_failures_hex '%02x' "${max_failures}"

ssh -o BatchMode=yes "${node_name}" \
  "sudo bpftool map update pinned '${map_dir}/config_map' key hex 00 00 00 00 value hex '${ip_a_hex}' '${ip_b_hex}' '${ip_c_hex}' '${ip_d_hex}' '${port_high_hex}' '${port_low_hex}' 00 00 '${max_failures_hex}' 00 00 00"

client_uid_before="$(kubectl get pod "${client_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
client_restarts_before="$(kubectl get pod "${client_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
injection_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ssh -o BatchMode=yes "${node_name}" \
  "sudo bpftool cgroup attach '${cgroup_path}' connect4 pinned '${program_pin}'"
bpf_attached="true"

active_logs=""
for _ in $(seq 1 30); do
  active_logs="$(kubectl logs "${client_pod}" -n "${namespace}" -c "${container_name}" --since-time="${injection_started_at}" 2>/dev/null || true)"
  target_fault_count="$(grep -c "result=fault port=${target_port} .*errno=1 name=EPERM" <<<"${active_logs}" || true)"
  target_ok_count="$(grep -c "result=ok port=${target_port}" <<<"${active_logs}" || true)"
  control_ok_count="$(grep -c "result=ok port=${control_port}" <<<"${active_logs}" || true)"
  if (( target_fault_count >= max_failures && target_ok_count >= 1 && control_ok_count >= 1 )); then
    break
  fi
  sleep 1
done

target_fault_count="$(grep -c "result=fault port=${target_port} .*errno=1 name=EPERM" <<<"${active_logs}" || true)"
control_fault_count="$(grep -c "result=fault port=${control_port}" <<<"${active_logs}" || true)"
target_ok_count="$(grep -c "result=ok port=${target_port}" <<<"${active_logs}" || true)"
control_ok_count="$(grep -c "result=ok port=${control_port}" <<<"${active_logs}" || true)"
if [[ "${target_fault_count}" != "${max_failures}" ]] || [[ "${control_fault_count}" != "0" ]] || \
   (( target_ok_count < 1 || control_ok_count < 1 )); then
  printf 'unexpected eBPF scope/count result: target_fault=%s control_fault=%s target_ok=%s control_ok=%s\n%s\n' \
    "${target_fault_count}" "${control_fault_count}" "${target_ok_count}" "${control_ok_count}" "${active_logs}" >&2
  exit 1
fi

attachment_evidence="$(ssh -o BatchMode=yes "${node_name}" "sudo bpftool cgroup show '${cgroup_path}' | grep connect4")"
map_evidence="$(ssh -o BatchMode=yes "${node_name}" "sudo bpftool -j map dump pinned '${map_dir}/failure_map'")"

ssh -o BatchMode=yes "${node_name}" \
  "sudo bpftool cgroup detach '${cgroup_path}' connect4 pinned '${program_pin}'"
bpf_attached="false"
recovery_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sleep 2
recovery_logs="$(kubectl logs "${client_pod}" -n "${namespace}" -c "${container_name}" --since-time="${recovery_started_at}" 2>/dev/null || true)"
if ! grep -q "result=ok port=${target_port}" <<<"${recovery_logs}" || \
   ! grep -q "result=ok port=${control_port}" <<<"${recovery_logs}" || \
   grep -q "result=fault" <<<"${recovery_logs}"; then
  printf 'network probe did not recover cleanly after eBPF detach\n%s\n' "${recovery_logs}" >&2
  exit 1
fi

client_uid_after="$(kubectl get pod "${client_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
client_restarts_after="$(kubectl get pod "${client_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
agenticsre_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"
[[ "${client_uid_before}" == "${client_uid_after}" ]]
[[ "${client_restarts_before}" == "${client_restarts_after}" ]]
[[ "${agenticsre_before}" == "${agenticsre_after}" ]]
[[ "${agenticsre_runtime_before}" == "${agenticsre_runtime_after}" ]]

ssh -o BatchMode=yes "${node_name}" \
  "sudo rm -f '${program_pin}' '${map_dir}/config_map' '${map_dir}/failure_map' '${remote_object}'; sudo rmdir '${map_dir}'"
program_pin=""
map_dir=""
remote_object=""
kubectl delete namespace "${namespace}" --wait=true --timeout=120s
namespace_created="false"

printf 'scope service_ip=%s target_port=%s control_port=%s cgroup=%s max_failures=%s\n' \
  "${service_ip}" "${target_port}" "${control_port}" "${cgroup_path}" "${max_failures}"
printf 'attachment_evidence=%s\n' "${attachment_evidence}"
printf 'map_evidence=%s\n' "${map_evidence}"
printf 'active_summary target_fault=%s control_fault=%s target_ok_after_limit=%s control_ok=%s\n' \
  "${target_fault_count}" "${control_fault_count}" "${target_ok_count}" "${control_ok_count}"
printf 'validation=PASS precision=ebpf_network scope=cgroup_ip_port max_failures=%s recovery=complete pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n' "${max_failures}"
