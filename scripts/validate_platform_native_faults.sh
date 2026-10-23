#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-platform-chaos-validation"
scenario_id="${1:-all}"
base_dir="${2:-deploy/precision-fault-injection/platform-native}"
probe_manifest="${base_dir}/platform-chaos-probes.yaml"
network_manifest="${base_dir}/networkchaos-target-loss.yaml"
dns_manifest="${base_dir}/dnschaos-domain-error.yaml"
stress_manifest="${base_dir}/stresschaos-cpu-load.yaml"
io_manifest="${base_dir}/iochaos-path-latency.yaml"
time_manifest="${base_dir}/timechaos-container-offset.yaml"
namespace_created="false"

case "${scenario_id}" in
  all|precise-network-loss|precise-dns-error|precise-container-cpu-stress|precise-path-io-latency|precise-container-time-offset) ;;
  *) printf 'unsupported platform-native precision scenario: %s\n' "${scenario_id}" >&2; exit 1 ;;
esac

cleanup() {
  kubectl delete networkchaos agenticsre-target-packet-loss -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl delete dnschaos agenticsre-domain-resolution-error -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl delete stresschaos agenticsre-container-cpu-load -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl delete iochaos agenticsre-path-io-latency -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl delete timechaos agenticsre-container-time-offset -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=180s >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

for required_file in "${probe_manifest}" "${network_manifest}" "${dns_manifest}" "${stress_manifest}" "${io_manifest}" "${time_manifest}"; do
  if [[ ! -f "${required_file}" ]]; then
    printf 'required file not found: %s\n' "${required_file}" >&2
    exit 1
  fi
done

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

connect_ok() {
  local pod="$1"
  local address="$2"
  kubectl exec -n "${namespace}" "pod/${pod}" -c probe -- python3 -c \
    "import socket; connection=socket.create_connection(('${address}', 18080), 0.8); connection.close()" \
    >/dev/null 2>&1
}

dns_ok() {
  local hostname="$1"
  kubectl exec -n "${namespace}" pod/agenticsre-dns-client -c probe -- python3 -c \
    "import socket; socket.getaddrinfo('${hostname}', 18080)" >/dev/null 2>&1
}

wait_for_connect() {
  local pod="$1"
  local address="$2"
  for _ in $(seq 1 30); do
    if connect_ok "${pod}" "${address}"; then
      return 0
    fi
    sleep 1
  done
  printf 'connection baseline did not become ready: pod=%s address=%s\n' "${pod}" "${address}" >&2
  return 1
}

wait_for_dns() {
  local hostname="$1"
  for _ in $(seq 1 30); do
    if dns_ok "${hostname}"; then
      return 0
    fi
    sleep 1
  done
  printf 'DNS baseline did not become ready: hostname=%s\n' "${hostname}" >&2
  return 1
}

cpu_usage_usec() {
  local pod="$1"
  kubectl exec -n "${namespace}" "pod/${pod}" -c probe -- python3 -c \
    'print(next(line.split()[1] for line in open("/sys/fs/cgroup/cpu.stat") if line.startswith("usage_usec ")))' \
    | tr -d '\r'
}

measure_cpu_delta() {
  local pod="$1"
  local seconds="$2"
  local before
  local after
  before="$(cpu_usage_usec "${pod}")"
  sleep "${seconds}"
  after="$(cpu_usage_usec "${pod}")"
  printf '%s\n' "$((after - before))"
}

io_latency_ms() {
  local path="$1"
  kubectl exec -n "${namespace}" pod/agenticsre-io-target -c probe -- \
    python3 /opt/agenticsre/io_probe.py "${path}" | tr -d '\r'
}

pod_epoch() {
  local pod="$1"
  kubectl logs -n "${namespace}" "pod/${pod}" -c probe --tail=1 \
    | awk -F= '/^epoch=/{print $2}' | tr -d '\r'
}

absolute_delta() {
  local left="$1"
  local right="$2"
  local delta=$((left - right))
  if (( delta < 0 )); then
    delta=$((-delta))
  fi
  printf '%s\n' "${delta}"
}

restart_total() {
  kubectl get pods -n "${namespace}" -l agenticsre.local/validation=platform-native-faults \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
    | awk '{sum += $1} END {print sum + 0}'
}

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre 2>/dev/null || printf unavailable)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime 2>/dev/null || printf unavailable)"

kubectl apply -f "${probe_manifest}"
namespace_created="true"
kubectl wait --for=condition=Ready pod \
  -l agenticsre.local/validation=platform-native-faults \
  -n "${namespace}" --timeout=180s

pod_count="$(kubectl get pods -n "${namespace}" -l agenticsre.local/validation=platform-native-faults -o name | wc -l | tr -d ' ')"
if [[ "${pod_count}" != "10" ]]; then
  printf 'expected 10 isolated probe pods, found %s\n' "${pod_count}" >&2
  exit 1
fi

uids_before="$(kubectl get pods -n "${namespace}" -l agenticsre.local/validation=platform-native-faults \
  -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
restarts_before="$(restart_total)"

if [[ "${scenario_id}" == "all" || "${scenario_id}" == "precise-network-loss" ]]; then
network_target_ip="$(kubectl get pod agenticsre-network-target -n "${namespace}" -o jsonpath='{.status.podIP}')"
network_control_ip="$(kubectl get pod agenticsre-network-control -n "${namespace}" -o jsonpath='{.status.podIP}')"
wait_for_connect agenticsre-network-client "${network_target_ip}"
wait_for_connect agenticsre-network-client "${network_control_ip}"
kubectl apply -f "${network_manifest}"
kubectl wait --for=condition=AllInjected networkchaos/agenticsre-target-packet-loss -n "${namespace}" --timeout=60s
read -r network_target_faults network_control_faults < <(
  kubectl exec -n "${namespace}" pod/agenticsre-network-client -c probe -- python3 -c \
    "import socket
target_faults = 0
control_faults = 0
for _ in range(4):
    for kind, address in (('target', '${network_target_ip}'), ('control', '${network_control_ip}')):
        try:
            connection = socket.create_connection((address, 18080), 0.8)
            connection.close()
        except OSError:
            if kind == 'target':
                target_faults += 1
            else:
                control_faults += 1
print(target_faults, control_faults)" | tr -d '\r'
)
printf 'network_loss_active target_failures=%s control_failures=%s\n' "${network_target_faults}" "${network_control_faults}"
if [[ "${network_target_faults}" != "4" || "${network_control_faults}" != "0" ]]; then
  kubectl describe networkchaos agenticsre-target-packet-loss -n "${namespace}" >&2 || true
  printf 'network loss scope assertion failed\n' >&2
  exit 1
fi
kubectl delete networkchaos agenticsre-target-packet-loss -n "${namespace}" --wait=true
sleep 3
connect_ok agenticsre-network-client "${network_target_ip}"
printf 'network_loss=PASS target_failures=%s control_failures=%s recovery=success scope=client_to_target_pod\n' \
  "${network_target_faults}" "${network_control_faults}"
fi

if [[ "${scenario_id}" == "all" || "${scenario_id}" == "precise-dns-error" ]]; then
dns_target="agenticsre-dns-target.${namespace}.svc.cluster.local"
dns_control="kubernetes.default.svc.cluster.local"
wait_for_dns "${dns_target}"
wait_for_dns "${dns_control}"
kubectl apply -f "${dns_manifest}"
kubectl wait --for=condition=AllInjected dnschaos/agenticsre-domain-resolution-error -n "${namespace}" --timeout=60s
if dns_ok "${dns_target}"; then
  printf 'target DNS lookup unexpectedly succeeded during DNSChaos\n' >&2
  exit 1
fi
dns_ok "${dns_control}"
kubectl delete dnschaos agenticsre-domain-resolution-error -n "${namespace}" --wait=true
sleep 3
dns_ok "${dns_target}"
printf 'dns_error=PASS target_domain_failed=true control_domain_ok=true recovery=success scope=pod_and_fqdn\n'
fi

if [[ "${scenario_id}" == "all" || "${scenario_id}" == "precise-container-cpu-stress" ]]; then
stress_baseline_target="$(measure_cpu_delta agenticsre-stress-target 2)"
stress_baseline_control="$(measure_cpu_delta agenticsre-stress-control 2)"
kubectl apply -f "${stress_manifest}"
kubectl wait --for=condition=AllInjected stresschaos/agenticsre-container-cpu-load -n "${namespace}" --timeout=60s
stress_active_target="$(measure_cpu_delta agenticsre-stress-target 4)"
stress_active_control="$(measure_cpu_delta agenticsre-stress-control 4)"
if (( stress_active_target < 500000 || stress_active_target < stress_active_control * 3 )); then
  printf 'CPU stress evidence too weak: target=%s control=%s\n' "${stress_active_target}" "${stress_active_control}" >&2
  exit 1
fi
kubectl delete stresschaos agenticsre-container-cpu-load -n "${namespace}" --wait=true
sleep 3
stress_recovered_target="$(measure_cpu_delta agenticsre-stress-target 3)"
if (( stress_recovered_target >= stress_active_target / 2 )); then
  printf 'CPU usage did not recover: active=%s recovered=%s\n' "${stress_active_target}" "${stress_recovered_target}" >&2
  exit 1
fi
printf 'cpu_stress=PASS baseline_target_usec=%s baseline_control_usec=%s active_target_usec=%s active_control_usec=%s recovered_target_usec=%s scope=single_container\n' \
  "${stress_baseline_target}" "${stress_baseline_control}" "${stress_active_target}" "${stress_active_control}" "${stress_recovered_target}"
fi

if [[ "${scenario_id}" == "all" || "${scenario_id}" == "precise-path-io-latency" ]]; then
io_baseline_fault_ms="$(io_latency_ms /data/fault-probe)"
io_baseline_control_ms="$(io_latency_ms /data/control-probe)"
kubectl apply -f "${io_manifest}"
kubectl wait --for=condition=AllInjected iochaos/agenticsre-path-io-latency -n "${namespace}" --timeout=90s
io_active_fault_ms="$(io_latency_ms /data/fault-probe)"
io_active_control_ms="$(io_latency_ms /data/control-probe)"
if (( io_active_fault_ms < 200 || io_active_fault_ms < io_active_control_ms + 150 )); then
  printf 'I/O latency evidence too weak: fault=%s control=%s\n' "${io_active_fault_ms}" "${io_active_control_ms}" >&2
  exit 1
fi
kubectl delete iochaos agenticsre-path-io-latency -n "${namespace}" --wait=true
sleep 3
io_recovered_fault_ms="$(io_latency_ms /data/fault-probe)"
if (( io_recovered_fault_ms >= 150 )); then
  printf 'I/O path did not recover: %sms\n' "${io_recovered_fault_ms}" >&2
  exit 1
fi
printf 'io_latency=PASS baseline_fault_ms=%s baseline_control_ms=%s active_fault_ms=%s active_control_ms=%s recovered_fault_ms=%s scope=container_path\n' \
  "${io_baseline_fault_ms}" "${io_baseline_control_ms}" "${io_active_fault_ms}" "${io_active_control_ms}" "${io_recovered_fault_ms}"
fi

if [[ "${scenario_id}" == "all" || "${scenario_id}" == "precise-container-time-offset" ]]; then
time_baseline_target="$(pod_epoch agenticsre-time-target)"
time_baseline_control="$(pod_epoch agenticsre-time-control)"
time_baseline_delta="$(absolute_delta "${time_baseline_target}" "${time_baseline_control}")"
(( time_baseline_delta <= 5 ))
kubectl apply -f "${time_manifest}"
kubectl wait --for=condition=AllInjected timechaos/agenticsre-container-time-offset -n "${namespace}" --timeout=60s
sleep 3
time_active_target="$(pod_epoch agenticsre-time-target)"
time_active_control="$(pod_epoch agenticsre-time-control)"
time_active_delta="$(absolute_delta "${time_active_target}" "${time_active_control}")"
if (( time_active_delta < 295 || time_active_delta > 305 )); then
  printf 'unexpected active clock delta: %ss\n' "${time_active_delta}" >&2
  exit 1
fi
kubectl delete timechaos agenticsre-container-time-offset -n "${namespace}" --wait=true
sleep 3
time_recovered_target="$(pod_epoch agenticsre-time-target)"
time_recovered_control="$(pod_epoch agenticsre-time-control)"
time_recovered_delta="$(absolute_delta "${time_recovered_target}" "${time_recovered_control}")"
(( time_recovered_delta <= 5 ))
printf 'time_offset=PASS baseline_delta_s=%s active_delta_s=%s recovered_delta_s=%s scope=single_container_clock\n' \
  "${time_baseline_delta}" "${time_active_delta}" "${time_recovered_delta}"
fi

uids_after="$(kubectl get pods -n "${namespace}" -l agenticsre.local/validation=platform-native-faults \
  -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
restarts_after="$(restart_total)"
agenticsre_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre 2>/dev/null || printf unavailable)"
agenticsre_runtime_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime 2>/dev/null || printf unavailable)"

[[ "${uids_before}" == "${uids_after}" ]]
[[ "${restarts_before}" == "${restarts_after}" ]]
[[ "${agenticsre_before}" == "${agenticsre_after}" ]]
[[ "${agenticsre_runtime_before}" == "${agenticsre_runtime_after}" ]]

kubectl delete namespace "${namespace}" --wait=true --timeout=180s
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'validation namespace still exists after cleanup: %s\n' "${namespace}" >&2
  exit 1
fi

printf 'validation=PASS precision=platform_native scenario=%s pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n' "${scenario_id}"
