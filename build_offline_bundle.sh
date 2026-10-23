#!/usr/bin/env bash
# Build a customer-site AgenticSRE offline bundle on a connected build host.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

VERSION="${VERSION:-$(date +%Y%m%d)}"
TARGET_ARCH="${TARGET_ARCH:-amd64}"
DOCKER_VERSION="${DOCKER_VERSION:-29.1.3}"
COMPOSE_VERSION="${COMPOSE_VERSION:-v5.5.0}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.35.0}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-agenticsre-offline}"
PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE:-python:3.12-slim-bookworm}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.org}"
IMAGE_REF="${IMAGE_REPOSITORY}:${VERSION}-${TARGET_ARCH}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/release}"
RUNTIME_CACHE="${RUNTIME_CACHE:-${SCRIPT_DIR}/offline-runtime-cache}"
INCLUDE_SOURCE="${INCLUDE_SOURCE:-0}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-0}"

[[ "${VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "VERSION contains unsafe characters" >&2; exit 2; }
[[ "${DOCKER_VERSION}" =~ ^[0-9][0-9A-Za-z._-]+$ ]] || { echo "DOCKER_VERSION is invalid" >&2; exit 2; }
[[ "${COMPOSE_VERSION}" =~ ^v?[0-9][0-9A-Za-z._-]+$ ]] || { echo "COMPOSE_VERSION is invalid" >&2; exit 2; }
[[ "${KUBECTL_VERSION}" =~ ^v[0-9][0-9A-Za-z._-]+$ ]] || { echo "KUBECTL_VERSION is invalid" >&2; exit 2; }
[[ "${IMAGE_REPOSITORY}" =~ ^[A-Za-z0-9._/:@-]+$ ]] || { echo "IMAGE_REPOSITORY is invalid" >&2; exit 2; }
[ -n "${OUTPUT_DIR}" ] && [ "${OUTPUT_DIR}" != "/" ] || { echo "OUTPUT_DIR must not be empty or /" >&2; exit 2; }
[ -n "${RUNTIME_CACHE}" ] && [ "${RUNTIME_CACHE}" != "/" ] || { echo "RUNTIME_CACHE must not be empty or /" >&2; exit 2; }

case "${TARGET_ARCH}" in
    amd64) DOCKER_ARCH="x86_64"; COMPOSE_ARCH="x86_64" ;;
    arm64) DOCKER_ARCH="aarch64"; COMPOSE_ARCH="aarch64" ;;
    *) echo "TARGET_ARCH must be amd64 or arm64" >&2; exit 2 ;;
esac

info() { printf '[INFO] %s\n' "$*"; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for command_name in docker curl tar gzip; do
    command -v "${command_name}" >/dev/null 2>&1 || die "missing build dependency: ${command_name}"
done
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    die "missing build dependency: sha256sum or shasum"
fi
docker info >/dev/null 2>&1 || die "Docker daemon is not available"

mkdir -p "${OUTPUT_DIR}" "${RUNTIME_CACHE}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd -P)"
RUNTIME_CACHE="$(cd "${RUNTIME_CACHE}" && pwd -P)"
[ "${OUTPUT_DIR}" != "/" ] && [ "${RUNTIME_CACHE}" != "/" ] || die "output/cache directory resolved to /"
BUNDLE_NAME="agenticsre-offline-${VERSION}-linux-${TARGET_ARCH}"
STAGING_DIR="${OUTPUT_DIR}/${BUNDLE_NAME}"
BUNDLE_TAR="${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"
BUNDLE_RUN="${OUTPUT_DIR}/${BUNDLE_NAME}.run"
[ "${STAGING_DIR}" = "${OUTPUT_DIR}/${BUNDLE_NAME}" ] && [ "${STAGING_DIR}" != "${OUTPUT_DIR}" ] \
    || die "unsafe staging directory"
rm -rf -- "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/images" "${STAGING_DIR}/runtime" "${STAGING_DIR}/config" "${STAGING_DIR}/docs"

download_verified() {
    local url="$1"
    local checksum_url="${2:-}"
    local destination="$3"
    local checksum_file="${destination}.sha256"
    if [ ! -f "${destination}" ]; then
        info "Downloading ${url}"
        curl --http1.1 --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
            --output "${destination}.part" "${url}"
        mv "${destination}.part" "${destination}"
    fi
    if [ -n "${checksum_url}" ] && [ ! -f "${checksum_file}" ]; then
        curl --http1.1 --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
            --output "${checksum_file}.part" "${checksum_url}"
        mv "${checksum_file}.part" "${checksum_file}"
    fi
    actual="$(sha256_file "${destination}")"
    if [ -n "${checksum_url}" ]; then
        expected="$(awk '{print $1; exit}' "${checksum_file}")"
        [ "${actual}" = "${expected}" ] || die "checksum mismatch for ${destination}"
    elif [ -n "${DOCKER_SHA256:-}" ]; then
        [ "${actual}" = "${DOCKER_SHA256}" ] || die "DOCKER_SHA256 mismatch for ${destination}"
    else
        # Docker's static download endpoint does not publish a sidecar digest.
        # TLS protects the download; the digest below protects the cached file
        # and is included again in the complete bundle SHA256SUMS manifest.
        info "Docker static archive digest (pin with DOCKER_SHA256 in CI): ${actual}"
    fi
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

DOCKER_TGZ="${RUNTIME_CACHE}/docker-${DOCKER_VERSION}-linux-${DOCKER_ARCH}.tgz"
COMPOSE_BIN="${RUNTIME_CACHE}/docker-compose-${COMPOSE_VERSION}-linux-${COMPOSE_ARCH}"
download_verified \
    "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_VERSION}.tgz" \
    "" \
    "${DOCKER_TGZ}"
download_verified \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}.sha256" \
    "${COMPOSE_BIN}"

if [ "${SKIP_IMAGE_BUILD}" != "1" ]; then
    info "Building self-contained image ${IMAGE_REF} for linux/${TARGET_ARCH}"
    docker build \
        --platform "linux/${TARGET_ARCH}" \
        --build-arg "PYTHON_BASE_IMAGE=${PYTHON_BASE_IMAGE}" \
        --build-arg "KUBECTL_VERSION=${KUBECTL_VERSION}" \
        --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}" \
        --build-arg "PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}" \
        --tag "${IMAGE_REF}" \
        --file Dockerfile.offline .
fi
docker image inspect "${IMAGE_REF}" >/dev/null 2>&1 || die "image not found: ${IMAGE_REF}"

image_arch="$(docker image inspect "${IMAGE_REF}" --format '{{.Architecture}}')"
[ "${image_arch}" = "${TARGET_ARCH}" ] || die "image architecture is ${image_arch}, expected ${TARGET_ARCH}"

info "Verifying image contents"
docker run --rm --platform "linux/${TARGET_ARCH}" --entrypoint python "${IMAGE_REF}" -c \
    'from pathlib import Path; import fastapi,kubernetes,openai,pandas,sqlite3,chromadb,web_app.app; from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; assert Path("/app/hermes-agent/run_agent.py").is_file(); assert len(DefaultEmbeddingFunction()(["offline verification"])) == 1; print("offline image and embedded database dependencies ok")'
docker run --rm --platform "linux/${TARGET_ARCH}" --entrypoint bash "${IMAGE_REF}" -ec \
    'for tool in curl dig ip jq mysql nc openssl pgrep ping psql redis-cli ssh kubectl; do command -v "$tool" >/dev/null || { echo "missing runtime tool: $tool" >&2; exit 1; }; done'

info "Generating auditable software dependency manifests"
git_commit="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git status --porcelain --untracked-files=all 2>/dev/null)" ]; then
    source_state=dirty
    git_commit="${git_commit}-dirty"
else
    source_state=clean
fi
docker run --rm --platform "linux/${TARGET_ARCH}" --entrypoint python "${IMAGE_REF}" -m pip freeze \
    > "${STAGING_DIR}/docs/python-packages.lock"
docker run --rm --platform "linux/${TARGET_ARCH}" --entrypoint dpkg-query "${IMAGE_REF}" \
    -W '-f=${Package}\t${Version}\n' > "${STAGING_DIR}/docs/os-packages.tsv"
docker image inspect "${IMAGE_REF}" > "${STAGING_DIR}/images/agenticsre-image-metadata.json"
cat > "${STAGING_DIR}/docs/SOFTWARE_MANIFEST.md" <<EOF
# AgenticSRE offline software manifest

- Bundle: ${BUNDLE_NAME}
- Application image: ${IMAGE_REF}
- Target: linux/${TARGET_ARCH}
- Persistence: ChromaDB with embedded SQLite metadata + JSON state
- Docker Engine: ${DOCKER_VERSION}
- Docker Compose: ${COMPOSE_VERSION}
- kubectl: ${KUBECTL_VERSION}
- Source revision: ${git_commit}
- Source state: ${source_state}
- Built at: $(date -u +%Y-%m-%dT%H:%M:%SZ)

Exact Python versions are in \`python-packages.lock\`; exact Debian packages are in
\`os-packages.tsv\`. The image metadata and complete bundle are covered by
\`SHA256SUMS\`.
EOF

info "Exporting image"
docker save "${IMAGE_REF}" | gzip -1 > "${STAGING_DIR}/images/agenticsre-image.tar.gz"
cp "${DOCKER_TGZ}" "${STAGING_DIR}/runtime/docker-${DOCKER_VERSION}.tgz"
cp "${COMPOSE_BIN}" "${STAGING_DIR}/runtime/docker-compose-linux-${TARGET_ARCH}"
chmod 0755 "${STAGING_DIR}/runtime/docker-compose-linux-${TARGET_ARCH}"

cp install-offline.sh docker-compose.offline.yaml "${STAGING_DIR}/"
cp docs/OFFLINE_DEPLOYMENT.md "${STAGING_DIR}/docs/"
[ ! -f docs/OFFLINE_DEPLOYMENT_GUIDE.md ] || cp docs/OFFLINE_DEPLOYMENT_GUIDE.md "${STAGING_DIR}/docs/"
[ ! -f docs/OFFLINE_E2E_ACCEPTANCE.md ] || cp docs/OFFLINE_E2E_ACCEPTANCE.md "${STAGING_DIR}/docs/"
cp .env.offline.example "${STAGING_DIR}/config/customer.env.example"
chmod 0755 "${STAGING_DIR}/install-offline.sh"

cat > "${STAGING_DIR}/BUNDLE.env" <<EOF
BUNDLE_VERSION=${VERSION}
DEPLOYMENT_MODE=strict-offline
REQUIRES_INTERNET=0
CONTAINS_APPLICATION_IMAGE=1
CONTAINS_DOCKER_RUNTIME=1
CONTAINS_COMPOSE=1
CONTAINS_EMBEDDING_MODEL=1
TARGET_OS=linux
TARGET_ARCH=${TARGET_ARCH}
IMAGE_REF=${IMAGE_REF}
DOCKER_VERSION=${DOCKER_VERSION}
COMPOSE_VERSION=${COMPOSE_VERSION}
KUBECTL_VERSION=${KUBECTL_VERSION}
PERSISTENCE_BACKEND=chromadb-sqlite-json
GIT_COMMIT=${git_commit}
SOURCE_STATE=${source_state}
BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

if [ "${INCLUDE_SOURCE}" = "1" ]; then
    info "Adding auditable source snapshot"
    tar --exclude='.git' --exclude='.venv' --exclude='data' --exclude='logs' \
        --exclude='release' --exclude='offline-runtime-cache' --exclude='.env' \
        --exclude='*.tar' --exclude='*.tar.gz' \
        --exclude='demo_recordings' --exclude='outputs' \
        -czf "${STAGING_DIR}/agenticsre-source.tar.gz" .
fi

(
    cd "${STAGING_DIR}"
    find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while IFS= read -r file; do
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum "${file}"
        else
            shasum -a 256 "${file}"
        fi
    done > SHA256SUMS
)

(cd "${STAGING_DIR}" && if command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHA256SUMS; else shasum -a 256 -c SHA256SUMS; fi)

rm -f "${BUNDLE_TAR}"
tar -czf "${BUNDLE_TAR}" -C "${OUTPUT_DIR}" "${BUNDLE_NAME}"
bundle_digest="$(sha256_file "${BUNDLE_TAR}")"
printf '%s  %s\n' "${bundle_digest}" "$(basename "${BUNDLE_TAR}")" > "${BUNDLE_TAR}.sha256"
info "Creating single-file self-extracting installer"
[ -f offline-oneclick-installer.sh ] || die "offline-oneclick-installer.sh is missing"
cp offline-oneclick-installer.sh "${BUNDLE_RUN}"
cat "${BUNDLE_TAR}" >> "${BUNDLE_RUN}"
chmod 0755 "${BUNDLE_RUN}"
run_digest="$(sha256_file "${BUNDLE_RUN}")"
printf '%s  %s\n' "${run_digest}" "$(basename "${BUNDLE_RUN}")" > "${BUNDLE_RUN}.sha256"
info "Offline bundle created: ${BUNDLE_TAR}"
info "Offline bundle SHA-256: ${bundle_digest}"
info "Single-file installer created: ${BUNDLE_RUN}"
info "Single-file installer SHA-256: ${run_digest}"
info "Customer command: sudo ./${BUNDLE_NAME}.run"
