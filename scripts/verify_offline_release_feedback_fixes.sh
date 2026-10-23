#!/bin/bash
# Verify release/package-side fixes required by the deployment feedback review.
set -euo pipefail

RELEASE_DIR="${1:-.}"
REQUIRED_ARCH="${VLLM_ASCEND_REQUIRED_ARCH:-arm64}"
IMAGE_GLOB="${VLLM_ASCEND_IMAGE_GLOB:-${RELEASE_DIR}/images/*vllm*ascend*.tar*}"
DOC_STALE_BEFORE="${DOC_STALE_BEFORE:-}"

required_files=(
    "start-all.sh"
    "scripts/ensure_deathstarbench_nodeport.sh"
    "scripts/patch_jaeger_sampling.sh"
    "scripts/prune_offline_image_archives.sh"
    "scripts/inspect_image_archive_arches.py"
    "scripts/select_ascend_device.sh"
    "scripts/audit_jaeger_ingest.sh"
    "scripts/deploy_jaeger_udp_agent_bridge.sh"
    "scripts/install_k3s_image_import_service.sh"
    "scripts/verify_deathstarbench_hostpath.sh"
)

for file in "${required_files[@]}"; do
    if [ ! -f "${RELEASE_DIR}/${file}" ]; then
        echo "ERROR: missing ${file} in ${RELEASE_DIR}" >&2
        exit 1
    fi
done

if find "${RELEASE_DIR}" -type f \( \
    -name '*coredns*1.14*.tar*' -o \
    -name '*local-path-provisioner*v0.0.36*.tar*' -o \
    -name '*traefik*3.6*.tar*' \
\) | grep -q .; then
    echo "ERROR: obsolete K3S v1.35 image archives found in ${RELEASE_DIR}" >&2
    find "${RELEASE_DIR}" -type f \( \
        -name '*coredns*1.14*.tar*' -o \
        -name '*local-path-provisioner*v0.0.36*.tar*' -o \
        -name '*traefik*3.6*.tar*' \
    \) >&2
    exit 1
fi

shopt -s nullglob
archives=( ${IMAGE_GLOB} )
shopt -u nullglob
if [ "${#archives[@]}" -gt 0 ]; then
    python3 "${RELEASE_DIR}/scripts/inspect_image_archive_arches.py" \
        "${archives[@]}" --match vllm-ascend --require-arch "${REQUIRED_ARCH}"
else
    echo "WARN: no vllm-ascend image archives matched ${IMAGE_GLOB}; architecture check skipped" >&2
fi

if [ -n "${DOC_STALE_BEFORE}" ]; then
    if find "${RELEASE_DIR}" -type f \( -name '*.md' -o -name '*.txt' \) \
        ! -path '*/doc/downloaded_papers/*' \
        ! -path '*/hermes-agent/*' \
        ! -path '*/release/*' \
        ! -path '*/web_app/static/vendor/*' \
        -print0 | xargs -0 grep -nE '20[0-9]{2}[-/][0-9]{2}[-/][0-9]{2}|20[0-9]{6}' | awk -v cutoff="${DOC_STALE_BEFORE}" '
            {
                line=$0
                while (match(line, /20[0-9]{2}[-\/]?[0-9]{2}[-\/]?[0-9]{2}/)) {
                    d=substr(line, RSTART, RLENGTH)
                    gsub(/[-\/]/, "", d)
                    if (d < cutoff) {
                        print
                        stale=1
                        break
                    }
                    line=substr(line, RSTART + RLENGTH)
                }
            }
            END { exit stale ? 0 : 1 }
        '; then
        echo "ERROR: stale document dates found before ${DOC_STALE_BEFORE}" >&2
        exit 1
    fi
fi

echo "offline release feedback-fix verification passed for ${RELEASE_DIR}"
