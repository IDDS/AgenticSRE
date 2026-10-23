#!/bin/bash
# AgenticSRE one-command Docker deployment.
# Usage: ./deploy_docker.sh [--build] [--stop] [--verify-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

IMAGE_NAME="agenticsre"
IMAGE_TAG="latest"
IMAGE_TAR="agenticsre-image.tar.gz"
SOURCE_TAR="agenticsre-source.tar.gz"
CONTAINER_NAME="agenticsre"
# Directories / files bind-mounted by docker-compose.yaml — must exist on host.
BIND_PATHS=(agents configs eval hermes-agent memory observability orchestrator paradigms tools skill_runtime web_app vllm_fault_injector main.py mcp_server.py)

# ── Parse arguments ──
ACTION="deploy"
FORCE_BUILD=false
for arg in "$@"; do
    case $arg in
        --stop)  ACTION="stop" ;;
        --build) FORCE_BUILD=true ;;
        --verify-only) ACTION="verify-only" ;;
        --help|-h)
            echo "Usage: $0 [--build] [--stop] [--verify-only]"
            echo "  --build  Force rebuild image (downloads apt/pip dependencies; not for offline release deployment)"
            echo "  --stop   Stop and remove container"
            echo "  --verify-only  Validate local release prerequisites and bundled image without starting the service"
            exit 0
            ;;
    esac
done

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

verify_image_contents() {
    local image_ref="${IMAGE_NAME}:${IMAGE_TAG}"
    [ -d vllm_fault_injector ] || error "vllm_fault_injector directory is missing from the release package."
    [ -f configs/heal_recipes.yaml ] || error "configs/heal_recipes.yaml is missing from the release package."
    [ -f web_app/app.py ] || error "web_app/app.py is missing from the release package."
    info "Verifying bundled runtime tools in ${image_ref}..."
    docker run --rm --entrypoint /bin/sh "${image_ref}" -c '
set -e
python --version
python -c "import fastapi, kubernetes, pandas, openai, chromadb; print(\"python packages ok\")"
command -v kubectl >/dev/null
kubectl version --client=true >/dev/null
'
    info "Image runtime verification passed."
}

# ═══════════════════════════════════════════
# Step 1: Check Docker
# ═══════════════════════════════════════════
info "Checking Docker..."
command -v docker >/dev/null 2>&1 || error "Docker not found. Please install Docker first."

# Check docker compose (plugin or standalone)
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    error "docker compose not found. Please install docker compose."
fi
info "Docker OK. Compose: ${COMPOSE_CMD}"

# ═══════════════════════════════════════════
# Stop action
# ═══════════════════════════════════════════
if [ "$ACTION" = "stop" ]; then
    info "Stopping AgenticSRE container..."
    ${COMPOSE_CMD} down 2>/dev/null || true
    info "Container stopped."
    exit 0
fi

# ═══════════════════════════════════════════
# Step 2: Create directories
# ═══════════════════════════════════════════
info "Creating data directories..."
mkdir -p data/memory logs

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        warn ".env not found; creating it from .env.example. Edit it before production use."
        cp .env.example .env
    else
        warn ".env not found and .env.example is missing."
    fi
fi

# ═══════════════════════════════════════════
# Step 2a: Ensure host-side credential mount points exist
# (docker-compose mounts ~/.kube/config and ~/.ssh into the container).
# ═══════════════════════════════════════════
if [ ! -e "${HOME}/.kube/config" ]; then
    warn "${HOME}/.kube/config not found; creating an empty stub so the mount succeeds."
    warn "  kubectl in container will be inert until you replace this with a real kubeconfig."
    mkdir -p "${HOME}/.kube"
    touch "${HOME}/.kube/config"
fi
if [ ! -d "${HOME}/.ssh" ]; then
    warn "${HOME}/.ssh not found; creating an empty stub so the mount succeeds."
    warn "  SSH-based bare-metal fault injection will fail until you populate it with keys."
    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"
fi

# ═══════════════════════════════════════════
# Step 2b: Ensure bind-mount sources exist (extract from SOURCE_TAR if needed)
# ═══════════════════════════════════════════
MISSING=()
for p in "${BIND_PATHS[@]}"; do
    [ -e "$p" ] || MISSING+=("$p")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    if [ -f "${SOURCE_TAR}" ]; then
        info "Bind-mount sources missing (${#MISSING[@]}): ${MISSING[*]}"
        info "Extracting ${SOURCE_TAR} into current directory..."
        tar -xzf "${SOURCE_TAR}"
        # Re-verify
        STILL_MISSING=()
        for p in "${BIND_PATHS[@]}"; do
            [ -e "$p" ] || STILL_MISSING+=("$p")
        done
        if [ ${#STILL_MISSING[@]} -gt 0 ]; then
            warn "Still missing after extraction: ${STILL_MISSING[*]}"
            warn "Creating empty stubs so Docker bind-mounts succeed (features depending on them will be inert):"
            for p in "${STILL_MISSING[@]}"; do
                if [[ "$p" == *.py ]]; then
                    warn "  - $p (empty file)"
                    touch "$p"
                else
                    warn "  - $p/ (empty directory)"
                    mkdir -p "$p"
                fi
            done
        else
            info "Source files extracted."
        fi
    else
        warn "Bind-mount sources missing and ${SOURCE_TAR} not found."
        warn "Creating empty stubs (container will run but with limited functionality):"
        for p in "${MISSING[@]}"; do
            if [[ "$p" == *.py ]]; then
                touch "$p"
            else
                mkdir -p "$p"
            fi
        done
    fi
fi

# ═══════════════════════════════════════════
# Step 3: Load image, or build only when explicitly requested
# ═══════════════════════════════════════════
if [ "$FORCE_BUILD" = true ]; then
    [ -f Dockerfile ] || error "Dockerfile not found; cannot build. Use bundled ${IMAGE_TAR} or unpack ${SOURCE_TAR}."
    warn "Force building image. This may download apt/pip dependencies."
    docker build -t ${IMAGE_NAME}:${IMAGE_TAG} .
elif [ -f "${IMAGE_TAR}" ]; then
    info "Found ${IMAGE_TAR}, loading image..."
    docker load -i "${IMAGE_TAR}"
    info "Image loaded."
elif docker image inspect ${IMAGE_NAME}:${IMAGE_TAG} >/dev/null 2>&1; then
    info "Image ${IMAGE_NAME}:${IMAGE_TAG} already exists."
else
    error "No ${IMAGE_TAR} and no local ${IMAGE_NAME}:${IMAGE_TAG} image found. Refusing to build/download dependencies during release deployment. Provide ${IMAGE_TAR}, run 'docker load -i ${IMAGE_TAR}', or rerun with --build if online build is intended."
fi

docker image inspect ${IMAGE_NAME}:${IMAGE_TAG} >/dev/null 2>&1 || error "Image ${IMAGE_NAME}:${IMAGE_TAG} is not available after load/build."
verify_image_contents

if [ "$ACTION" = "verify-only" ]; then
    info "Verify-only mode completed."
    exit 0
fi

# ═══════════════════════════════════════════
# Step 4: Stop old container → Start new
# ═══════════════════════════════════════════
info "Stopping old container (if any)..."
${COMPOSE_CMD} down 2>/dev/null || true

info "Starting AgenticSRE container without building..."
${COMPOSE_CMD} up -d --no-build

# ═══════════════════════════════════════════
# Step 5: Wait for health check
# ═══════════════════════════════════════════
info "Waiting for health check..."
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf http://localhost:8080/api/health >/dev/null 2>&1; then
        echo ""
        info "Health check passed!"
        break
    fi
    printf "."
    sleep 2
    WAITED=$((WAITED + 2))
done

if [ $WAITED -ge $MAX_WAIT ]; then
    warn "Health check timed out after ${MAX_WAIT}s. Check logs:"
    warn "  docker logs ${CONTAINER_NAME}"
fi

# ═══════════════════════════════════════════
# Step 6: Print access info
# ═══════════════════════════════════════════
echo ""
echo "═══════════════════════════════════════════"
info "AgenticSRE is running!"
echo ""
echo "  Dashboard:  http://localhost:8080"
echo "  Health:     http://localhost:8080/api/health"
echo "  Logs:       docker logs -f ${CONTAINER_NAME}"
echo "  Stop:       $0 --stop"
echo "═══════════════════════════════════════════"
