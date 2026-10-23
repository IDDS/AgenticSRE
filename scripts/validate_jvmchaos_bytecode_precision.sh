#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-bytecode-validation"
pod_name="agenticsre-bytecode-probe"
container_name="probe"
chaos_name="agenticsre-method-latency"
configmap_name="agenticsre-bytecode-probe-class"
class_file="${1:-deploy/precision-fault-injection/bytecode/classes/AgenticSREBytecodeProbe.class}"
pod_manifest="${2:-deploy/precision-fault-injection/bytecode/bytecode-probe-pod.yaml}"
chaos_manifest="${3:-deploy/precision-fault-injection/bytecode/jvmchaos-method-latency.yaml}"
namespace_created="false"
chaos_created="false"

if [[ ! -f "${class_file}" ]]; then
  scripts/build_jvm_precision_probe.sh \
    deploy/precision-fault-injection/bytecode/AgenticSREBytecodeProbe.java \
    deploy/precision-fault-injection/bytecode/classes >/dev/null
fi

cleanup() {
  if [[ "${chaos_created}" == "true" ]]; then
    kubectl delete jvmchaos "${chaos_name}" -n "${namespace}" --ignore-not-found --wait=true >/dev/null || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=120s >/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

for required_file in "${class_file}" "${pod_manifest}" "${chaos_manifest}"; do
  if [[ ! -f "${required_file}" ]]; then
    printf 'required file not found: %s\n' "${required_file}" >&2
    exit 1
  fi
done

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

extract_latency() {
  local line="$1"
  printf '%s\n' "${line}" | awk '{for (i=1; i<=NF; i++) if ($i ~ /^latency_ms=/) {split($i, value, "="); print value[2]; exit}}'
}

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

kubectl create namespace "${namespace}"
namespace_created="true"
kubectl label namespace "${namespace}" \
  app.kubernetes.io/managed-by=agenticsre \
  agenticsre.local/validation=bytecode-method-precision
kubectl create configmap "${configmap_name}" -n "${namespace}" \
  --from-file="AgenticSREBytecodeProbe.class=${class_file}"
kubectl apply -f "${pod_manifest}"
kubectl wait --for=condition=Ready "pod/${pod_name}" -n "${namespace}" --timeout=120s

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
  printf 'JVM probe baseline is not healthy: %s\n' "${baseline_line}" >&2
  exit 1
fi

baseline_latency="$(extract_latency "${baseline_line}")"
if [[ -z "${baseline_latency}" ]] || (( baseline_latency >= 500 )); then
  printf 'unexpected JVM baseline latency: %s\n' "${baseline_line}" >&2
  exit 1
fi

printf 'scope namespace=%s pod=%s container=%s class=AgenticSREBytecodeProbe method=stableOperation action=latency latency_ms=2000 duration=25s\n' \
  "${namespace}" "${pod_name}" "${container_name}"
printf 'baseline_evidence=%s\n' "${baseline_line}"

injection_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl apply -f "${chaos_manifest}"
chaos_created="true"
if ! kubectl wait --for=condition=AllInjected "jvmchaos/${chaos_name}" -n "${namespace}" --timeout=90s; then
  kubectl describe jvmchaos "${chaos_name}" -n "${namespace}" >&2 || true
  exit 1
fi
kubectl get jvmchaos "${chaos_name}" -n "${namespace}" -o jsonpath='injected desiredPhase={.status.experiment.desiredPhase} conditions={range .status.conditions[*]}{.type}:{.status} {end}{"\n"}'

injected_line=""
for _ in $(seq 1 40); do
  injected_line="$(kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" --since-time="${injection_started_at}" 2>/dev/null | \
    awk '{for (i=1; i<=NF; i++) if ($i ~ /^latency_ms=/) {split($i, value, "="); if (value[2] >= 1500) {print; exit}}}' || true)"
  if [[ -n "${injected_line}" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "${injected_line}" ]]; then
  printf 'JVM probe did not observe method-level latency injection\n' >&2
  kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" --tail=40 >&2 || true
  kubectl describe jvmchaos "${chaos_name}" -n "${namespace}" >&2 || true
  exit 1
fi

kubectl wait --for=condition=AllRecovered "jvmchaos/${chaos_name}" -n "${namespace}" --timeout=90s
kubectl get jvmchaos "${chaos_name}" -n "${namespace}" -o jsonpath='recovered desiredPhase={.status.experiment.desiredPhase} conditions={range .status.conditions[*]}{.type}:{.status} {end}{"\n"}'

recovery_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
recovered_line=""
for _ in $(seq 1 20); do
  recovered_line="$(kubectl logs "${pod_name}" -n "${namespace}" -c "${container_name}" --since-time="${recovery_started_at}" 2>/dev/null | tail -1 || true)"
  recovered_latency="$(extract_latency "${recovered_line}")"
  if [[ "${recovered_line}" == result=ok* && -n "${recovered_latency}" ]] && (( recovered_latency < 500 )); then
    break
  fi
  recovered_line=""
  sleep 1
done
if [[ -z "${recovered_line}" ]]; then
  printf 'JVM probe did not recover to sub-500ms method latency\n' >&2
  exit 1
fi

kubectl delete jvmchaos "${chaos_name}" -n "${namespace}" --wait=true
chaos_created="false"

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
if kubectl get jvmchaos "${chaos_name}" -n "${namespace}" >/dev/null 2>&1; then
  printf 'JVMChaos resource still exists after cleanup\n' >&2
  exit 1
fi

kubectl delete namespace "${namespace}" --wait=true --timeout=120s
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'validation namespace still exists after cleanup: %s\n' "${namespace}" >&2
  exit 1
fi

printf 'injection_evidence=%s\n' "${injected_line}"
printf 'recovery_evidence=%s\n' "${recovered_line}"
printf 'validation=PASS precision=bytecode target_scope=class_method class=AgenticSREBytecodeProbe method=stableOperation action=latency pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n'
