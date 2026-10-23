#!/bin/bash
# Ensure a DeathStarBench service is exposed through a configurable NodePort.
set -euo pipefail

NAMESPACE="${DEATHSTARBENCH_NAMESPACE:-test-social-network}"
SERVICE="${DEATHSTARBENCH_SERVICE:-nginx-thrift}"
PORT="${DEATHSTARBENCH_SERVICE_PORT:-8080}"
TARGET_PORT="${DEATHSTARBENCH_TARGET_PORT:-${PORT}}"
NODE_PORT="${DEATHSTARBENCH_NODE_PORT:-}"
KUBECTL="${KUBECTL:-kubectl}"

if [ -z "${NODE_PORT}" ]; then
    echo "Set DEATHSTARBENCH_NODE_PORT before exposing ${NAMESPACE}/${SERVICE}" >&2
    exit 2
fi

if ! [[ "${NODE_PORT}" =~ ^[0-9]+$ ]]; then
    echo "DEATHSTARBENCH_NODE_PORT must be numeric: ${NODE_PORT}" >&2
    exit 2
fi

"${KUBECTL}" -n "${NAMESPACE}" get svc "${SERVICE}" >/dev/null
"${KUBECTL}" -n "${NAMESPACE}" patch svc "${SERVICE}" --type merge -p \
    "{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"name\":\"http\",\"port\":${PORT},\"protocol\":\"TCP\",\"targetPort\":${TARGET_PORT},\"nodePort\":${NODE_PORT}}]}}"
"${KUBECTL}" -n "${NAMESPACE}" get svc "${SERVICE}"
