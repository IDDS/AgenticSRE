#!/bin/bash
# Build an offline release package from a Docker image on a remote host.
#
# Typical usage:
#   ./build_release_from_remote_image.sh
#   REMOTE_HOST=snail IMAGE_NAME=agenticsre IMAGE_TAG=latest ./build_release_from_remote_image.sh
#
# The generated release contains:
#   - agenticsre-image.tar.gz exported from the remote Docker daemon
#   - current local source snapshot and deployment materials
#   - one-command offline container deployment script

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

REMOTE_HOST="${REMOTE_HOST:-snail}"
REMOTE_TMP_DIR="${REMOTE_TMP_DIR:-/tmp}"
IMAGE_NAME="${IMAGE_NAME:-agenticsre}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
RELEASE_DATE="$(date +%Y%m%d)"
RELEASE_NAME="${RELEASE_NAME:-agenticsre-release-${RELEASE_DATE}-remote-image}"
LOCAL_IMAGE_TAR="${LOCAL_IMAGE_TAR:-/tmp/agenticsre-image-${RELEASE_DATE}.tar.gz}"
LOCAL_IMAGE_META="${LOCAL_IMAGE_META:-/tmp/agenticsre-image-meta-${RELEASE_DATE}.txt}"
REMOTE_IMAGE_TAR="${REMOTE_TMP_DIR}/agenticsre-image-${RELEASE_DATE}.tar.gz"
REMOTE_IMAGE_META="${REMOTE_TMP_DIR}/agenticsre-image-meta-${RELEASE_DATE}.txt"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

command -v ssh >/dev/null 2>&1 || error "ssh not found."
command -v scp >/dev/null 2>&1 || error "scp not found."

info "Checking local git workspace..."
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"
    if [ -n "$(git status --porcelain)" ]; then
        warn "Local workspace has uncommitted changes. They will be included in the source snapshot."
    fi
    info "Local source: ${GIT_BRANCH:-unknown}@${GIT_COMMIT:-unknown}"
fi

info "Exporting remote Docker image ${IMAGE_REF} from ${REMOTE_HOST}..."
ssh "${REMOTE_HOST}" "set -euo pipefail
docker image inspect '${IMAGE_REF}' >/dev/null
{
  echo 'image=${IMAGE_REF}'
  echo 'host='\"\$(hostname)\"
  echo 'exported_at='\"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  echo 'docker_image_id='\"\$(docker image inspect '${IMAGE_REF}' --format '{{.Id}}')\"
  echo 'docker_image_size='\"\$(docker image inspect '${IMAGE_REF}' --format '{{.Size}}')\"
  echo 'docker_repo_tags='\"\$(docker image inspect '${IMAGE_REF}' --format '{{json .RepoTags}}')\"
  echo 'container='\"\$(docker ps --filter name=agenticsre --format '{{.ID}} {{.Image}} {{.Names}} {{.Status}}' | head -1)\"
  echo 'health='\"\$(curl -fsS http://127.0.0.1:8080/api/health 2>/dev/null || true)\"
} > '${REMOTE_IMAGE_META}'
docker save '${IMAGE_REF}' | gzip > '${REMOTE_IMAGE_TAR}'
ls -lh '${REMOTE_IMAGE_TAR}' '${REMOTE_IMAGE_META}'"

info "Copying remote image archive to local machine..."
scp "${REMOTE_HOST}:${REMOTE_IMAGE_TAR}" "${LOCAL_IMAGE_TAR}"
scp "${REMOTE_HOST}:${REMOTE_IMAGE_META}" "${LOCAL_IMAGE_META}"

info "Building offline release package from exported image..."
IMAGE_NAME="${IMAGE_NAME}" \
IMAGE_TAG="${IMAGE_TAG}" \
IMAGE_TAR_PATH="${LOCAL_IMAGE_TAR}" \
IMAGE_META_PATH="${LOCAL_IMAGE_META}" \
RELEASE_NAME="${RELEASE_NAME}" \
./package_release_from_image.sh

info "Release package is ready: ${SCRIPT_DIR}/${RELEASE_NAME}.tar.gz"
