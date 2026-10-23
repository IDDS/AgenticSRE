#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-http-validation"
target_pod="agenticsre-http-probe"
client_pod="agenticsre-http-client"
chaos_name="agenticsre-request-header-delay"
probe_manifest="${1:-deploy/precision-fault-injection/http/http-probe.yaml}"
chaos_manifest="${2:-deploy/precision-fault-injection/httpchaos-request-header-delay.yaml}"
namespace_created="false"
chaos_created="false"

cleanup() {
  if [[ "${chaos_created}" == "true" ]]; then
    kubectl delete httpchaos "${chaos_name}" -n "${namespace}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  fi
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

measure() {
  local header_value="$1"
  kubectl exec -n "${namespace}" "pod/${client_pod}" -c client -- \
    python3 -c 'import sys,time,urllib.request; h=sys.argv[1]; req=urllib.request.Request("http://agenticsre-http-probe:18080/healthz", headers=({"x-agenticsre-fault":h} if h else {})); t=time.monotonic(); print("%.6f" % (urllib.request.urlopen(req, timeout=5).read() and time.monotonic()-t))' "${header_value}"
}

kubectl apply -f "${probe_manifest}" >/dev/null
namespace_created="true"
kubectl wait --for=condition=Ready "pod/${target_pod}" "pod/${client_pod}" -n "${namespace}" --timeout=120s >/dev/null
target_uid_before="$(kubectl get pod "${target_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_before="$(kubectl get pod "${target_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
baseline_target="$(measure delay)"
baseline_control="$(measure '')"

kubectl apply -f "${chaos_manifest}" >/dev/null
chaos_created="true"
kubectl wait --for=condition=AllInjected "httpchaos/${chaos_name}" -n "${namespace}" --timeout=60s >/dev/null
active_warmup="$(measure delay)"
active_target="$(measure delay)"
active_control="$(measure '')"
awk -v target="${active_target}" -v control="${active_control}" 'BEGIN {if (target < 0.40 || target > 1.50 || target-control < 0.30 || control >= 0.25) exit 1}'

kubectl delete httpchaos "${chaos_name}" -n "${namespace}" --wait=true >/dev/null
chaos_created="false"
sleep 2
recovered_target="$(measure delay)"
awk -v recovered="${recovered_target}" 'BEGIN {if (recovered >= 0.25) exit 1}'
target_uid_after="$(kubectl get pod "${target_pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_after="$(kubectl get pod "${target_pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
[[ "${target_uid_before}" == "${target_uid_after}" ]]
[[ "${restart_before}" == "${restart_after}" ]]

kubectl delete namespace "${namespace}" --wait=true --timeout=120s >/dev/null
namespace_created="false"

printf 'before_evidence=matching_header_latency_s:%s control_latency_s:%s\n' "${baseline_target}" "${baseline_control}"
printf 'fault_evidence=matching_header_latency_s:%s warmup_latency_s:%s configured_delay_ms:500 control_latency_s:%s exact_route:/healthz exact_header:x-agenticsre-fault=delay\n' "${active_target}" "${active_warmup}" "${active_control}"
printf 'recovery_evidence=matching_header_latency_s:%s pod_recreated:false restart_delta:0\n' "${recovered_target}"
printf 'validation=PASS precision=request_header_delay target_affected=true control_unaffected=true recovery=true cleanup=complete\n'
