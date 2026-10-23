#!/bin/bash
# Audit Jaeger trace ingestion readiness for legacy DeathStarBench UDP clients.
set -euo pipefail

NAMESPACE="${JAEGER_NAMESPACE:-observability}"
APP_SELECTOR="${JAEGER_APP_SELECTOR:-app.kubernetes.io/name=jaeger}"
SERVICE="${JAEGER_SERVICE:-jaeger}"
KUBECTL="${KUBECTL:-kubectl}"

echo "== Jaeger pods =="
"${KUBECTL}" -n "${NAMESPACE}" get pods -l "${APP_SELECTOR}" -o wide

echo ""
echo "== Jaeger images =="
"${KUBECTL}" -n "${NAMESPACE}" get pods -l "${APP_SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.containers[*]}{.image}{" "}{end}{"\n"}{end}' |
while IFS= read -r line; do
    echo "${line}"
    image="${line#*$'\t'}"
    if printf '%s\n' "${image}" | grep -Eq '(^|:)latest([[:space:]]|$)'; then
        echo "ERROR: Jaeger image uses mutable latest tag: ${image}" >&2
        exit 1
    fi
done

echo ""
echo "== Jaeger service ports =="
"${KUBECTL}" -n "${NAMESPACE}" get svc "${SERVICE}" -o wide
"${KUBECTL}" -n "${NAMESPACE}" get svc "${SERVICE}" \
    -o jsonpath='{range .spec.ports[*]}{.name}{"\t"}{.protocol}{"\t"}{.port}{"\t"}{.targetPort}{"\n"}{end}'

echo ""
echo "== Legacy UDP 6831 readiness =="
if "${KUBECTL}" -n "${NAMESPACE}" get svc "${SERVICE}" \
    -o jsonpath='{range .spec.ports[*]}{.protocol}:{.port}{"\n"}{end}' | grep -qx 'UDP:6831'; then
    echo "UDP 6831 service port present"
else
    echo "WARN: UDP 6831 service port is missing. Legacy Jaeger clients will not send spans unless they use OTLP/HTTP or an agent bridge." >&2
fi

echo ""
echo "== Jaeger API services =="
pod="$("${KUBECTL}" -n "${NAMESPACE}" get pod -l "${APP_SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
if [ -n "${pod}" ]; then
    "${KUBECTL}" -n "${NAMESPACE}" exec "${pod}" -- sh -c \
        'command -v wget >/dev/null 2>&1 && wget -q -O - http://127.0.0.1:16686/api/services || true'
fi
