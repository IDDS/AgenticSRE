#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-seccomp-runtime"
pod="agenticsre-seccomp-probe"
artifact="deploy/precision-fault-injection/seccomp/seccomp-notify-probe"
manifest="deploy/precision-fault-injection/seccomp/seccomp-runtime-probe.yaml"
expected_sha256="45d744950d19ac28880bd3116336344b5b96d51f188566660f531d5e39c3239c"
namespace_created="false"

cleanup() {
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

for path in "${artifact}" "${manifest}"; do
  if [[ ! -f "${path}" ]]; then
    printf 'required reviewed artifact not found: %s\n' "${path}" >&2
    exit 1
  fi
done
artifact_sha256="$(sha256sum "${artifact}" | awk '{print $1}')"
if [[ "${artifact_sha256}" != "${expected_sha256}" ]]; then
  printf 'reviewed seccomp artifact hash mismatch: expected=%s actual=%s\n' \
    "${expected_sha256}" "${artifact_sha256}" >&2
  exit 1
fi
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'reviewed seccomp namespace is already in use: %s\n' "${namespace}" >&2
  exit 1
fi

kubectl create namespace "${namespace}" >/dev/null
namespace_created="true"
kubectl create configmap agenticsre-seccomp-artifacts -n "${namespace}" \
  --from-file=seccomp-notify-probe="${artifact}" >/dev/null
kubectl apply -f "${manifest}" --prune=false >/dev/null

for _ in $(seq 1 90); do
  phase="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "${phase}" == "Succeeded" ]] && break
  if [[ "${phase}" == "Failed" ]]; then
    kubectl logs "${pod}" -n "${namespace}" || true
    printf 'reviewed seccomp Pod failed\n' >&2
    exit 1
  fi
  sleep 1
done
phase="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.phase}')"
if [[ "${phase}" != "Succeeded" ]]; then
  kubectl describe pod "${pod}" -n "${namespace}" >&2 || true
  kubectl logs "${pod}" -n "${namespace}" >&2 || true
  printf 'reviewed seccomp Pod did not complete: %s\n' "${phase}" >&2
  exit 1
fi

pod_uid="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_count="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
logs="$(kubectl logs "${pod}" -n "${namespace}")"
before_summary="$(printf '%s\n' "${logs}" | grep -m1 'before_summary target_failures=0 control_failures=0 filter=absent' || true)"
fault_event="$(printf '%s\n' "${logs}" | grep -m1 'seccomp_event=errno syscall=openat path=/tmp/agenticsre-seccomp-target match=3 injection=1 errno=13' || true)"
during_summary="$(printf '%s\n' "${logs}" | grep -m1 'during_summary target_failures=1 control_failures=0 target_seen=6 notifications=12 injected=1 recovered_after_hit=true child_ok=true filter=process_scoped' || true)"
recovery_summary="$(printf '%s\n' "${logs}" | grep -m1 'recovery_summary target_failures=0 control_failures=0 filter=absent' || true)"
fault_count="$(printf '%s\n' "${logs}" | grep -c 'source=seccomp label=during seq=3 kind=target result=-1 errno=13 path=/tmp/agenticsre-seccomp-target' || true)"
control_failure_count="$(printf '%s\n' "${logs}" | grep 'source=seccomp label=during .*kind=control' | grep -vc 'errno=0' || true)"
post_hit_recovery_count="$(printf '%s\n' "${logs}" | awk '/source=seccomp label=during/ && /kind=target/ {seq=$3; err=$6; sub("seq=", "", seq); sub("errno=", "", err); if (seq > 3 && err == 0) count++} END {print count+0}')"

if [[ -z "${before_summary}" || -z "${fault_event}" || -z "${during_summary}" || -z "${recovery_summary}" ]]; then
  printf 'reviewed seccomp evidence is incomplete\n%s\n' "${logs}" >&2
  exit 1
fi
if [[ "${fault_count}" != "1" || "${control_failure_count}" != "0" || "${post_hit_recovery_count}" != "3" ]]; then
  printf 'reviewed seccomp precision mismatch: fault=%s control_failures=%s post_hit_recovery=%s\n' \
    "${fault_count}" "${control_failure_count}" "${post_hit_recovery_count}" >&2
  exit 1
fi

kubectl delete namespace "${namespace}" --wait=true --timeout=90s >/dev/null
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'reviewed seccomp namespace still exists after cleanup\n' >&2
  exit 1
fi

printf 'executor_profile=agenticsre.local.seccomp_openat_errno.v1\n'
printf 'artifact_sha256=%s\n' "${artifact_sha256}"
printf 'pod_uid=%s\n' "${pod_uid}"
printf 'restart_count=%s\n' "${restart_count}"
printf 'before_evidence=%s\n' "${before_summary}"
printf 'fault_evidence=%s\n' "${fault_event}"
printf 'during_evidence=%s\n' "${during_summary}"
printf 'recovery_evidence=%s\n' "${recovery_summary}"
printf 'validation=PASS precision=seccomp_user_notification exact_path=true target_hit=3 max_hits=1 control_failures=0 post_hit_recovery=3 rollback=restart_without_filter cleanup=complete\n'
