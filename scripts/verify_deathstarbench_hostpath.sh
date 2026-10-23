#!/bin/bash
# Verify DeathStarBench hostPath prerequisites without hardcoding a site-specific path.
set -euo pipefail

HOST_PATH="${DSB_NGINX_HOSTPATH:-}"
REQUIRED_FILES_CSV="${DSB_NGINX_REQUIRED_FILES:-}"

if [ -z "${HOST_PATH}" ]; then
    echo "Set DSB_NGINX_HOSTPATH before verifying DeathStarBench nginx hostPath" >&2
    exit 2
fi

if [ ! -d "${HOST_PATH}" ]; then
    echo "Missing DeathStarBench nginx hostPath directory: ${HOST_PATH}" >&2
    exit 1
fi

if [ -n "${REQUIRED_FILES_CSV}" ]; then
    IFS=',' read -r -a required_files <<< "${REQUIRED_FILES_CSV}"
    for rel in "${required_files[@]}"; do
        if [ ! -e "${HOST_PATH}/${rel}" ]; then
            echo "Missing required hostPath file: ${HOST_PATH}/${rel}" >&2
            exit 1
        fi
    done
fi

echo "DeathStarBench nginx hostPath verified: ${HOST_PATH}"
