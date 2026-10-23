#!/bin/bash
# Backward-compatible one-command entrypoint for offline deployments.
# Older deployment runbooks referenced start-all.sh; keep it as a thin wrapper
# around the maintained Docker deployment script so fixes stay in one place.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

RUN_POST_STEPS=1
for arg in "$@"; do
    case "${arg}" in
        --help|-h|--stop|--verify-only)
            RUN_POST_STEPS=0
            ;;
    esac
done

if [ ! -x ./deploy_docker.sh ]; then
    echo "[ERROR] deploy_docker.sh is missing or not executable" >&2
    exit 1
fi

./deploy_docker.sh "$@"

if [ "${RUN_POST_STEPS}" != "1" ]; then
    exit 0
fi

if [ "${AGENTICSRE_EXPOSE_DEATHSTARBENCH:-0}" = "1" ]; then
    ./scripts/ensure_deathstarbench_nodeport.sh
fi

if [ -n "${JAEGER_SAMPLING_PARAM:-}" ]; then
    ./scripts/patch_jaeger_sampling.sh
fi
