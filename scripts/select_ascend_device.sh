#!/bin/bash
# Select an available Ascend/NPU device and optionally patch a Kubernetes deployment.
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NPU_SMI="${NPU_SMI:-npu-smi}"
PREFERRED="${ASCEND_PREFERRED_DEVICE:-}"
EXCLUDE_CSV="${ASCEND_EXCLUDE_DEVICES:-}"
PATCH_NAMESPACE="${ASCEND_PATCH_NAMESPACE:-}"
PATCH_DEPLOYMENT="${ASCEND_PATCH_DEPLOYMENT:-}"
CONTAINER_NAME="${ASCEND_PATCH_CONTAINER:-}"
ENV_NAME="${ASCEND_VISIBLE_ENV_NAME:-ASCEND_RT_VISIBLE_DEVICES}"
SKIP_BUSY="${ASCEND_SKIP_BUSY_DEVICES:-1}"

device_excluded() {
    local id="$1"
    IFS=',' read -r -a excluded <<< "${EXCLUDE_CSV}"
    for item in "${excluded[@]}"; do
        [ "${item}" = "${id}" ] && return 0
    done
    return 1
}

discover_devices() {
    if command -v "${NPU_SMI}" >/dev/null 2>&1; then
        "${NPU_SMI}" info 2>/dev/null | awk '
            /[0-9]+[[:space:]]+[0-9]+[[:space:]]+[A-Za-z0-9]/ {
                if ($1 ~ /^[0-9]+$/) print $1
            }
        ' | sort -n | uniq
    else
        seq 0 "${ASCEND_MAX_DEVICE_INDEX:-7}"
    fi
}

device_busy() {
    local id="$1"
    [ "${SKIP_BUSY}" = "1" ] || return 1
    command -v "${NPU_SMI}" >/dev/null 2>&1 || return 1
    "${NPU_SMI}" info 2>/dev/null | awk -v target="${id}" '
        BEGIN { in_proc=0; busy=0 }
        /Process id|Process name|No running processes/ { in_proc=1 }
        in_proc && $0 ~ target && $0 ~ /[0-9]/ { busy=1 }
        END { exit busy ? 0 : 1 }
    '
}

choose_device() {
    if [ -n "${PREFERRED}" ] && ! device_excluded "${PREFERRED}" && ! device_busy "${PREFERRED}"; then
        echo "${PREFERRED}"
        return 0
    fi
    while IFS= read -r id; do
        [ -n "${id}" ] || continue
        if ! device_excluded "${id}" && ! device_busy "${id}"; then
            echo "${id}"
            return 0
        fi
    done < <(discover_devices)
    return 1
}

DEVICE="$(choose_device)" || {
    echo "No available Ascend device found. Check ASCEND_EXCLUDE_DEVICES and npu-smi output." >&2
    exit 1
}

echo "${ENV_NAME}=${DEVICE}"

if [ -n "${PATCH_NAMESPACE}" ] && [ -n "${PATCH_DEPLOYMENT}" ]; then
    if [ -n "${CONTAINER_NAME}" ]; then
        "${KUBECTL}" -n "${PATCH_NAMESPACE}" set env "deployment/${PATCH_DEPLOYMENT}" -c "${CONTAINER_NAME}" "${ENV_NAME}=${DEVICE}"
    else
        "${KUBECTL}" -n "${PATCH_NAMESPACE}" set env "deployment/${PATCH_DEPLOYMENT}" "${ENV_NAME}=${DEVICE}"
    fi
fi
