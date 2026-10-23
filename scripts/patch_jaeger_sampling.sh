#!/bin/bash
# Patch Jaeger/OpenTelemetry sampling parameter in a configurable ConfigMap.
set -euo pipefail

NAMESPACE="${JAEGER_SAMPLING_NAMESPACE:-observability}"
CONFIGMAP="${JAEGER_SAMPLING_CONFIGMAP:-jaeger-config}"
KEY="${JAEGER_SAMPLING_KEY:-jaeger-config.yml}"
PARAM="${JAEGER_SAMPLING_PARAM:-}"
KUBECTL="${KUBECTL:-kubectl}"

if [ -z "${PARAM}" ]; then
    echo "Set JAEGER_SAMPLING_PARAM before patching sampling config" >&2
    exit 2
fi

case "${PARAM}" in
    0|0.*|1|1.0|1.00) ;;
    *)
        echo "JAEGER_SAMPLING_PARAM should be a probability between 0 and 1: ${PARAM}" >&2
        exit 2
        ;;
esac

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
JSONPATH_KEY="${KEY//./\\.}"

"${KUBECTL}" -n "${NAMESPACE}" get configmap "${CONFIGMAP}" -o "jsonpath={.data.${JSONPATH_KEY}}" > "${tmp}"
if [ ! -s "${tmp}" ]; then
    echo "ConfigMap ${NAMESPACE}/${CONFIGMAP} key ${KEY} is empty or missing" >&2
    exit 1
fi

perl -0pi -e "s/(param:\s*)[0-9.]+/\${1}${PARAM}/g" "${tmp}"
"${KUBECTL}" -n "${NAMESPACE}" create configmap "${CONFIGMAP}" \
    "--from-file=${KEY}=${tmp}" \
    --dry-run=client -o yaml | "${KUBECTL}" apply -f -

echo "Patched ${NAMESPACE}/${CONFIGMAP} ${KEY} sampler.param=${PARAM}"
echo "Restart the affected Jaeger/OpenTelemetry pods if they do not reload ConfigMaps automatically."
