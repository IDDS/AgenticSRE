#!/bin/bash
# Package a release from an existing Docker image tarball.
#
# This script does not build an image. Use it after exporting an image from a
# target-compatible host, for example:
#   ssh snail 'docker save agenticsre:latest | gzip > /tmp/agenticsre-image.tar.gz'
#   scp snail:/tmp/agenticsre-image.tar.gz /tmp/agenticsre-image.tar.gz
#   IMAGE_TAR_PATH=/tmp/agenticsre-image.tar.gz ./package_release_from_image.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

IMAGE_NAME="${IMAGE_NAME:-agenticsre}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
IMAGE_TAR_PATH="${IMAGE_TAR_PATH:-/tmp/agenticsre-image.tar.gz}"
IMAGE_META_PATH="${IMAGE_META_PATH:-/tmp/agenticsre-image-meta.txt}"
IMAGE_TAR="agenticsre-image.tar.gz"
SOURCE_TAR="agenticsre-source.tar.gz"
RELEASE_DATE="$(date +%Y%m%d)"
RELEASE_NAME="${RELEASE_NAME:-agenticsre-release-${RELEASE_DATE}}"
STAGING_DIR="${SCRIPT_DIR}/release/${RELEASE_NAME}"
RELEASE_TAR="${SCRIPT_DIR}/${RELEASE_NAME}.tar.gz"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[ -f "${IMAGE_TAR_PATH}" ] || error "Image tar not found: ${IMAGE_TAR_PATH}"

info "Creating release staging directory..."
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

info "Copying exported Docker image..."
cp "${IMAGE_TAR_PATH}" "${STAGING_DIR}/${IMAGE_TAR}"
if [ -f "${IMAGE_META_PATH}" ]; then
    cp "${IMAGE_META_PATH}" "${STAGING_DIR}/IMAGE_META.txt"
fi

info "Creating source snapshot from current workspace..."
tar --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.claude' \
    --exclude='.pycache_tmp' \
    --exclude='.DS_Store' \
    --exclude='*/.DS_Store' \
    --exclude='data' \
    --exclude='demo_recordings' \
    --exclude='logs' \
    --exclude='.env' \
    --exclude='docker-compose.yaml' \
    --exclude='*.tar' \
    --exclude='*.tar.gz' \
    --exclude='*.tgz' \
    --exclude='release' \
    --exclude='hermes-agent' \
    --exclude='运维多智能体协作技术研究项目SOW.docx' \
    --exclude='*SOW*' \
    --exclude='*sow*' \
    --exclude='eval/results/*' \
    --exclude='agenticsre-release-*.tar.gz' \
    -czf "${STAGING_DIR}/${SOURCE_TAR}" .

info "Copying deployment files..."
cp Dockerfile .dockerignore docker-compose.release.yaml docker-compose.skill-runtime.yaml deploy_docker.sh start-all.sh build_release.sh build_release_from_remote_image.sh package_release_from_image.sh README.md USER_MANUAL.md AGENTS.md requirements.txt .env.example "${STAGING_DIR}/"
cp docker-compose.release.yaml "${STAGING_DIR}/docker-compose.yaml"
cp main.py mcp_server.py "${STAGING_DIR}/"
cp -R agents "${STAGING_DIR}/agents"
cp -R configs "${STAGING_DIR}/configs"
cp -R eval "${STAGING_DIR}/eval"
cp -R memory "${STAGING_DIR}/memory"
cp -R observability "${STAGING_DIR}/observability"
cp -R orchestrator "${STAGING_DIR}/orchestrator"
cp -R paradigms "${STAGING_DIR}/paradigms"
cp -R tools "${STAGING_DIR}/tools"
cp -R scripts "${STAGING_DIR}/scripts"
cp -R skill_runtime "${STAGING_DIR}/skill_runtime"
cp -R web_app "${STAGING_DIR}/web_app"
cp -R tests "${STAGING_DIR}/tests"
cp -R doc "${STAGING_DIR}/doc"
cp -R report "${STAGING_DIR}/report"
cp -R 测试 "${STAGING_DIR}/测试"
cp -R vllm_fault_injector "${STAGING_DIR}/vllm_fault_injector"
find "${STAGING_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} +
rm -rf "${STAGING_DIR}/eval/results"
mkdir -p "${STAGING_DIR}/eval/results"
touch "${STAGING_DIR}/eval/results/.gitkeep"
find "${STAGING_DIR}" -name .DS_Store -delete
find "${STAGING_DIR}" -name '._*' -delete
chmod +x "${STAGING_DIR}/deploy_docker.sh" "${STAGING_DIR}/start-all.sh" "${STAGING_DIR}/build_release.sh" "${STAGING_DIR}/build_release_from_remote_image.sh" "${STAGING_DIR}/package_release_from_image.sh"
find "${STAGING_DIR}/scripts" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} +
chmod +x "${STAGING_DIR}/skill_runtime/run.sh"

cat > "${STAGING_DIR}/RELEASE_NOTES.md" <<EOF
# AgenticSRE Release ${RELEASE_DATE}

This package was created from an exported Docker image and the current source snapshot.

## Image

- Image reference: \`${IMAGE_REF}\`
- Image tar: \`${IMAGE_TAR}\`
- Image source: \`${IMAGE_TAR_PATH}\`
- Image metadata: \`IMAGE_META.txt\` when available
- Source snapshot: \`${GIT_BRANCH}@${GIT_COMMIT}\`

## Contents

- \`${IMAGE_TAR}\`: prebuilt Docker image
- \`${SOURCE_TAR}\`: source snapshot from the current workspace
- \`docker-compose.yaml\`: offline runtime Compose definition (no build section)
- \`docker-compose.skill-runtime.yaml\`: optional standalone Skill Runtime deployment on port 8090
- \`deploy_docker.sh\`: one-command offline deployment helper
- \`start-all.sh\` and \`scripts/\`: compatibility entrypoint and optional offline post-deploy helpers
- \`skill_runtime/\`: lightweight skill registry, executor, API, sample skills, and draft generator
- Runtime source directories are included at package top level for bind mounts
- \`configs/\`: runtime configuration templates
- \`doc/\`, \`report/\`, \`测试/\`, \`tests/\`: local project documentation and test materials
- \`.env.example\`: environment variable template
- \`eval/\`: fault scenarios and evaluation utilities required by Web APIs
- \`SHA256SUMS\`: checksum file for package verification
- \`MANIFEST.txt\`: file list inside this release package

## Deploy

\`\`\`bash
tar xzf ${RELEASE_NAME}.tar.gz
cd ${RELEASE_NAME}
./deploy_docker.sh
\`\`\`

The release deploy script creates \`.env\` from \`.env.example\` when needed,
loads \`${IMAGE_TAR}\`, verifies the bundled runtime, and starts Compose with
\`--no-build\`. It should not download apt/pip dependencies during deployment.
Use \`./deploy_docker.sh --build\` only when an online rebuild is intentional.

Dashboard: http://localhost:8080
Health: http://localhost:8080/api/health
EOF

info "Writing manifest and checksums..."
(
    cd "${STAGING_DIR}"
    find . -type f | sort > MANIFEST.txt
    : > SHA256SUMS
    if command -v shasum >/dev/null 2>&1; then
        find . -type f ! -name SHA256SUMS | sort | while IFS= read -r file; do
            shasum -a 256 "$file" >> SHA256SUMS
        done
    else
        find . -type f ! -name SHA256SUMS | sort | while IFS= read -r file; do
            sha256sum "$file" >> SHA256SUMS
        done
    fi
)

info "Creating release tarball..."
rm -f "${RELEASE_TAR}"
tar -czf "${RELEASE_TAR}" -C "${SCRIPT_DIR}/release" "${RELEASE_NAME}"

RELEASE_SIZE="$(du -h "${RELEASE_TAR}" | awk '{print $1}')"
IMAGE_SIZE="$(du -h "${STAGING_DIR}/${IMAGE_TAR}" | awk '{print $1}')"
SOURCE_SIZE="$(du -h "${STAGING_DIR}/${SOURCE_TAR}" | awk '{print $1}')"

echo ""
echo "═══════════════════════════════════════════"
info "Release package built from exported image."
echo "  File:   ${RELEASE_TAR}"
echo "  Size:   ${RELEASE_SIZE}"
echo "  Image:  ${IMAGE_SIZE}"
echo "  Source: ${SOURCE_SIZE}"
echo "═══════════════════════════════════════════"
