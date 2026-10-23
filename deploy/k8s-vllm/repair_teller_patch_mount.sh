#!/usr/bin/env bash
set -euo pipefail

# Replace the fragile node-local /tmp HostPath used by an existing TELLER
# enabled vLLM Deployment with a ConfigMap file mount. This is idempotent and
# preserves the existing container path and command.
NAMESPACE="${VLLM_NAMESPACE:-llm-k8s}"
DEPLOYMENT="${VLLM_DEPLOYMENT:-vllm-server}"
CONFIGMAP="${VLLM_TELLER_PATCH_CONFIGMAP:-${DEPLOYMENT}-agenticsre-teller-patch}"
PATCH_SOURCE="${VLLM_TELLER_PATCH_SOURCE:-scripts/patch_teller_vllm_request_markers.py}"

if [[ ! -f "${PATCH_SOURCE}" ]]; then
  echo "missing TELLER patch source: ${PATCH_SOURCE}" >&2
  exit 2
fi

sha256="$(sha256sum "${PATCH_SOURCE}" | awk '{print $1}')"

kubectl -n "${NAMESPACE}" create configmap "${CONFIGMAP}" \
  --from-file=patch_teller_vllm_request_markers.py="${PATCH_SOURCE}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" label configmap "${CONFIGMAP}" \
  app="${DEPLOYMENT}" app.kubernetes.io/managed-by=agenticsre --overwrite
kubectl -n "${NAMESPACE}" annotate configmap "${CONFIGMAP}" \
  agenticsre.io/sha256="${sha256}" --overwrite

volume_index="$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for i, item in enumerate(d["spec"]["template"]["spec"].get("volumes", [])):
    if item.get("name") == "agenticsre-teller-patch":
        print(i)
        break
else:
    raise SystemExit("agenticsre-teller-patch volume not found")
')"
mount_index="$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
mounts = d["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", [])
for i, item in enumerate(mounts):
    if item.get("name") == "agenticsre-teller-patch":
        print(i)
        break
else:
    raise SystemExit("agenticsre-teller-patch volumeMount not found")
')"

subpath_op="add"
if [[ -n "$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o jsonpath="{.spec.template.spec.containers[0].volumeMounts[${mount_index}].subPath}")" ]]; then
  subpath_op="replace"
fi
source_annotation_op="add"
if [[ -n "$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o jsonpath='{.spec.template.metadata.annotations.agenticsre\.io/teller-patch-source}')" ]]; then
  source_annotation_op="replace"
fi
sha_annotation_op="add"
if [[ -n "$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o jsonpath='{.spec.template.metadata.annotations.agenticsre\.io/teller-patch-sha256}')" ]]; then
  sha_annotation_op="replace"
fi

patch="$(python3 - "${volume_index}" "${mount_index}" "${CONFIGMAP}" "${sha256}" "${subpath_op}" "${source_annotation_op}" "${sha_annotation_op}" <<'PY'
import json, sys
volume_index, mount_index, configmap, sha256, subpath_op, source_annotation_op, sha_annotation_op = sys.argv[1:]
print(json.dumps([
    {"op": "replace", "path": f"/spec/template/spec/volumes/{volume_index}", "value": {
        "name": "agenticsre-teller-patch",
        "configMap": {"name": configmap, "defaultMode": 420},
    }},
    {"op": subpath_op, "path": f"/spec/template/spec/containers/0/volumeMounts/{mount_index}/subPath", "value": "patch_teller_vllm_request_markers.py"},
    {"op": source_annotation_op, "path": "/spec/template/metadata/annotations/agenticsre.io~1teller-patch-source", "value": f"configmap:{configmap}"},
    {"op": sha_annotation_op, "path": "/spec/template/metadata/annotations/agenticsre.io~1teller-patch-sha256", "value": sha256},
]))
PY
)"
kubectl -n "${NAMESPACE}" patch deployment "${DEPLOYMENT}" --type=json -p="${patch}"
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOYMENT}" --timeout="${VLLM_ROLLOUT_TIMEOUT:-60m}"
