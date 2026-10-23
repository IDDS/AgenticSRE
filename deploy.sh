#!/bin/bash
# ═══════════════════════════════════════════
# AgenticSRE — Deploy to K8S Cluster Node
# Uploads code, installs deps, starts web dashboard
# ═══════════════════════════════════════════
set -e

# ── Config ──
JUMP_HOST="${AGENTICSRE_DEPLOY_JUMP_HOST:-${K8S_SSH_JUMP:-}}"
TARGET="${AGENTICSRE_DEPLOY_TARGET:-${K8S_SSH_TARGET:-}}"
REMOTE_DIR="${AGENTICSRE_REMOTE_DIR:-/home/ubuntu/AgenticSRE}"
if [ -z "${JUMP_HOST}" ] || [ -z "${TARGET}" ]; then
    echo "Set AGENTICSRE_DEPLOY_JUMP_HOST/K8S_SSH_JUMP and AGENTICSRE_DEPLOY_TARGET/K8S_SSH_TARGET before running deploy.sh" >&2
    exit 2
fi
SSH_CMD="ssh -J ${JUMP_HOST} ${TARGET}"
SCP_CMD="scp -o ProxyJump=${JUMP_HOST}"
LOCAL_PORT=8080
REMOTE_PORT=8080

echo "🚀 AgenticSRE Cluster Deployment"
echo "   Target: ${TARGET} (via ${JUMP_HOST})"
echo ""

# ── Step 1: Create remote directory ──
echo "📁 Step 1: Preparing remote directory..."
${SSH_CMD} "mkdir -p ${REMOTE_DIR}"

# ── Step 2: Upload code ──
echo "📦 Step 2: Uploading code to cluster..."
# Use rsync if available, fallback to tar+scp
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Create a tar excluding unnecessary files
cd "${SCRIPT_DIR}"
tar czf /tmp/agenticsre.tar.gz \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='data' \
    --exclude='*.pyc' \
    --exclude='.env' \
    .

${SCP_CMD} /tmp/agenticsre.tar.gz ${TARGET}:/tmp/agenticsre.tar.gz
${SSH_CMD} "cd ${REMOTE_DIR} && tar xzf /tmp/agenticsre.tar.gz && rm /tmp/agenticsre.tar.gz"
rm /tmp/agenticsre.tar.gz
echo "   ✅ Code uploaded"

# ── Step 3: Use cluster config ──
echo "🔧 Step 3: Applying cluster configuration..."
${SSH_CMD} "cd ${REMOTE_DIR} && cp configs/config_cluster.yaml configs/config.yaml"

# ── Step 4: Install dependencies ──
echo "📥 Step 4: Installing Python dependencies..."
${SSH_CMD} "cd ${REMOTE_DIR} && python3 -m venv .venv 2>/dev/null || true && source .venv/bin/activate && pip install --upgrade pip -q && pip install pyyaml python-dotenv openai pydantic fastapi uvicorn jinja2 aiofiles numpy requests 2>&1 | tail -5"
echo "   ✅ Dependencies installed"

# ── Step 5: Set API key ──
echo "🔑 Step 5: Setting up environment..."
if [ -n "${LLM_API_KEY}" ]; then
    ${SSH_CMD} "echo 'LLM_API_KEY=${LLM_API_KEY}' > ${REMOTE_DIR}/.env"
    echo "   ✅ API key configured from local env"
elif [ -f "${SCRIPT_DIR}/.env" ]; then
    ${SCP_CMD} "${SCRIPT_DIR}/.env" ${TARGET}:${REMOTE_DIR}/.env
    echo "   ✅ .env file uploaded"
else
    echo "   ⚠️  No API key found. Set LLM_API_KEY env var or create .env file"
fi

# ── Step 6: Create data directories ──
${SSH_CMD} "mkdir -p ${REMOTE_DIR}/data/memory ${REMOTE_DIR}/logs"

# ── Step 7: Start web app ──
echo "🌐 Step 6: Starting web dashboard on cluster..."
${SSH_CMD} "cd ${REMOTE_DIR} && source .venv/bin/activate && \
    export PYTHONPATH=${REMOTE_DIR}:\$PYTHONPATH && \
    pkill -f 'uvicorn web_app.app:app' 2>/dev/null || true && \
    sleep 1 && \
    nohup python -m uvicorn web_app.app:app --host 0.0.0.0 --port ${REMOTE_PORT} \
    > logs/web.log 2>&1 &"

echo "   ✅ Web dashboard started on cluster port ${REMOTE_PORT}"

# ── Step 8: Setup SSH tunnel ──
echo ""
echo "═══════════════════════════════════════════"
echo "✅ Deployment complete!"
echo ""
echo "📡 Starting SSH tunnel: localhost:${LOCAL_PORT} → cluster:${REMOTE_PORT}"
echo "   Access dashboard at: http://localhost:${LOCAL_PORT}"
echo "   Press Ctrl+C to stop the tunnel"
echo "═══════════════════════════════════════════"
echo ""

# SSH tunnel (foreground, Ctrl+C to stop)
ssh -J ${JUMP_HOST} -N -L ${LOCAL_PORT}:localhost:${REMOTE_PORT} ${TARGET}
