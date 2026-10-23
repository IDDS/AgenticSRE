#!/bin/bash
# Remove obsolete image archives from an offline bundle directory.
set -euo pipefail

IMAGE_DIR="${OFFLINE_IMAGE_DIR:-./images}"
DRY_RUN="${OFFLINE_IMAGE_PRUNE_DRY_RUN:-1}"
PATTERNS="${OFFLINE_IMAGE_PRUNE_PATTERNS:-coredns:*1.14*,local-path-provisioner:*v0.0.36*,traefik:*3.6*}"

if [ ! -d "${IMAGE_DIR}" ]; then
    echo "Image directory not found: ${IMAGE_DIR}" >&2
    exit 2
fi

IFS=',' read -r -a pattern_list <<< "${PATTERNS}"
matched=0

for pattern in "${pattern_list[@]}"; do
    pattern="${pattern//:/-}"
    while IFS= read -r file; do
        [ -n "${file}" ] || continue
        matched=$((matched + 1))
        if [ "${DRY_RUN}" = "0" ]; then
            rm -f "${file}"
            echo "removed ${file}"
        else
            echo "would remove ${file}"
        fi
    done < <(find "${IMAGE_DIR}" -type f \( -name "*${pattern}*.tar" -o -name "*${pattern}*.tar.gz" -o -name "*${pattern}*.tgz" \) | sort)
done

if [ "${matched}" -eq 0 ]; then
    echo "No obsolete image archives matched in ${IMAGE_DIR}"
elif [ "${DRY_RUN}" != "0" ]; then
    echo "Dry-run only. Set OFFLINE_IMAGE_PRUNE_DRY_RUN=0 to remove matched files."
fi
