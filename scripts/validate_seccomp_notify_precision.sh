#!/usr/bin/env bash

set -euo pipefail

namespace="agenticsre-seccomp-validation"
pod="agenticsre-seccomp-probe"
manifest_path="${1:-deploy/precision-fault-injection/seccomp/seccomp-notify-probe.yaml}"
build_dir="$(mktemp -d)"
namespace_created="false"

cleanup() {
  if [[ "${namespace_created}" == "true" ]]; then
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=true --timeout=90s >/dev/null || true
  fi
  rm -rf "${build_dir}"
}

trap cleanup EXIT INT TERM

if [[ ! -f "${manifest_path}" ]]; then
  printf 'required manifest not found: %s\n' "${manifest_path}" >&2
  exit 1
fi
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'refusing to reuse existing namespace: %s\n' "${namespace}" >&2
  exit 1
fi

agenticsre_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_before="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

scripts/build_seccomp_notify_probe.sh "${build_dir}"
probe_sha256="$(sha256sum "${build_dir}/seccomp-notify-probe" | awk '{print $1}')"

kubectl create namespace "${namespace}"
namespace_created="true"
kubectl create configmap agenticsre-seccomp-artifacts -n "${namespace}" \
  --from-file=seccomp-notify-probe="${build_dir}/seccomp-notify-probe"
kubectl apply -f "${manifest_path}" --prune=false

for _ in $(seq 1 90); do
  phase="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "${phase}" == "Succeeded" ]] && break
  if [[ "${phase}" == "Failed" ]]; then
    kubectl logs "${pod}" -n "${namespace}" || true
    printf 'seccomp validation Pod failed\n' >&2
    exit 1
  fi
  sleep 1
done
phase="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.phase}')"
if [[ "${phase}" != "Succeeded" ]]; then
  kubectl describe pod "${pod}" -n "${namespace}" || true
  kubectl logs "${pod}" -n "${namespace}" || true
  printf 'seccomp validation Pod did not complete: %s\n' "${phase}" >&2
  exit 1
fi

pod_uid_before="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_before="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
logs="$(kubectl logs "${pod}" -n "${namespace}")"

before_summary="$(printf '%s\n' "${logs}" | grep -m1 'before_summary target_failures=0 control_failures=0 filter=absent' || true)"
fault_event="$(printf '%s\n' "${logs}" | grep -m1 'seccomp_event=errno syscall=openat path=/tmp/agenticsre-seccomp-target match=3 injection=1 errno=13' || true)"
during_summary="$(printf '%s\n' "${logs}" | grep -m1 'during_summary target_failures=1 control_failures=0 target_seen=6 notifications=12 injected=1 recovered_after_hit=true child_ok=true filter=process_scoped' || true)"
recovery_summary="$(printf '%s\n' "${logs}" | grep -m1 'recovery_summary target_failures=0 control_failures=0 filter=absent' || true)"
fault_count="$(printf '%s\n' "${logs}" | grep -c 'source=seccomp label=during seq=3 kind=target result=-1 errno=13 path=/tmp/agenticsre-seccomp-target' || true)"
control_failure_count="$(printf '%s\n' "${logs}" | grep 'source=seccomp label=during .*kind=control' | grep -vc 'errno=0' || true)"
post_hit_recovery_count="$(printf '%s\n' "${logs}" | awk '/source=seccomp label=during/ && /kind=target/ {seq=$3; err=$6; sub("seq=", "", seq); sub("errno=", "", err); if (seq > 3 && err == 0) count++} END {print count+0}')"

if [[ -z "${before_summary}" || -z "${fault_event}" || -z "${during_summary}" || -z "${recovery_summary}" ]]; then
  printf 'seccomp before/during/recovery evidence is incomplete\n%s\n' "${logs}" >&2
  exit 1
fi
if [[ "${fault_count}" != "1" || "${control_failure_count}" != "0" || "${post_hit_recovery_count}" != "3" ]]; then
  printf 'seccomp precision mismatch: fault=%s control_failures=%s post_hit_recovery=%s\n' \
    "${fault_count}" "${control_failure_count}" "${post_hit_recovery_count}" >&2
  exit 1
fi

pod_uid_after="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.metadata.uid}')"
restart_after="$(kubectl get pod "${pod}" -n "${namespace}" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
agenticsre_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre)"
agenticsre_runtime_after="$(docker inspect -f '{{.Id}}|{{.State.StartedAt}}' agenticsre-skill-runtime)"

[[ "${pod_uid_before}" == "${pod_uid_after}" ]]
[[ "${restart_before}" == "${restart_after}" ]]
[[ "${agenticsre_before}" == "${agenticsre_after}" ]]
[[ "${agenticsre_runtime_before}" == "${agenticsre_runtime_after}" ]]

kubectl delete namespace "${namespace}" --wait=true --timeout=90s
namespace_created="false"
if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
  printf 'validation namespace still exists after cleanup\n' >&2
  exit 1
fi

printf 'seccomp_scope=pod:%s child_process:single syscall:openat exact_path:/tmp/agenticsre-seccomp-target hit_count:3 max_hits:1 errno:13 artifact_sha256:%s\n' \
  "${pod}" "${probe_sha256}"
printf 'seccomp_before_evidence=%s\n' "${before_summary}"
printf 'seccomp_fault_evidence=%s\n' "${fault_event}"
printf 'seccomp_during_evidence=%s\n' "${during_summary}"
printf 'seccomp_recovery_evidence=%s\n' "${recovery_summary}"
printf 'validation=PASS precision=seccomp_user_notification fault_types=seccomp_openat_errno exact_path=true target_hit=3 max_hits=1 control_failures=0 post_hit_recovery=3 rollback=restart_without_filter pod_recreated=false restart_delta=0 cleanup=complete agenticsre_unchanged=true\n'
