#!/bin/bash
# ═══════════════════════════════════════════════════════════
# AgenticSRE — One-Click Cluster Deploy + E2E Evaluation
# Uploads code, sets up port-forward, runs evaluation, downloads results
# ═══════════════════════════════════════════════════════════
set -e

# ── Config ──
JUMP_HOST="${AGENTICSRE_DEPLOY_JUMP_HOST:-${K8S_SSH_JUMP:-}}"
TARGET="${AGENTICSRE_DEPLOY_TARGET:-${K8S_SSH_TARGET:-}}"
REMOTE_DIR="${AGENTICSRE_REMOTE_DIR:-/home/ubuntu/AgenticSRE}"
if [ -z "${JUMP_HOST}" ] || [ -z "${TARGET}" ]; then
    echo "Set AGENTICSRE_DEPLOY_JUMP_HOST/K8S_SSH_JUMP and AGENTICSRE_DEPLOY_TARGET/K8S_SSH_TARGET before running run_cluster_eval.sh" >&2
    exit 2
fi
SSH_CMD="ssh -J ${JUMP_HOST} ${TARGET}"
SCP_CMD="scp -o ProxyJump=${JUMP_HOST}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse arguments ──
PARADIGM=""
SCENARIO=""
SKIP_WORKLOAD=""
MODE=""
VERBOSE=""
SKIP_DEPLOY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --paradigm)   PARADIGM="--paradigm $2"; shift 2 ;;
        --scenario)   SCENARIO="--scenario $2"; shift 2 ;;
        --skip-workload) SKIP_WORKLOAD="--skip-workload"; shift ;;
        --mode)       MODE="--mode $2"; shift 2 ;;
        --verbose|-v) VERBOSE="-v"; shift ;;
        --skip-deploy) SKIP_DEPLOY="1"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "========================================================"
echo "  AgenticSRE Cluster Evaluation"
echo "  Target: ${TARGET} (via ${JUMP_HOST})"
echo "========================================================"
echo ""

# ── Step 1: Deploy latest code ──
if [ -z "${SKIP_DEPLOY}" ]; then
    echo "[Step 1/4] Deploying latest code to cluster..."
    bash "${SCRIPT_DIR}/deploy.sh" &
    DEPLOY_PID=$!
    # Wait for deploy to finish (it ends with SSH tunnel, so we kill it after upload)
    sleep 30
    kill ${DEPLOY_PID} 2>/dev/null || true
    wait ${DEPLOY_PID} 2>/dev/null || true
    echo "  Code deployed."
else
    echo "[Step 1/4] Skipping deploy (--skip-deploy)"
fi

# ── Step 2: Set up port-forward for social-network ──
echo "[Step 2/4] Setting up port-forward for social-network..."
# Kill any existing port-forward
${SSH_CMD} "pkill -f 'kubectl.*port-forward.*social-network' 2>/dev/null || true"
sleep 1

# Start port-forward in background on remote
${SSH_CMD} "nohup kubectl -n social-network port-forward svc/nginx-thrift 8080:8080 \
    > /tmp/pf-social-network.log 2>&1 &"
sleep 3
echo "  Port-forward established (nginx-thrift:8080)"

# ── Step 3: Run evaluation ──
echo "[Step 3/4] Running E2E evaluation on cluster..."
echo "  Args: ${PARADIGM} ${SCENARIO} ${SKIP_WORKLOAD} ${MODE} ${VERBOSE}"
echo ""

${SSH_CMD} "cd ${REMOTE_DIR} && \
    source .venv/bin/activate && \
    export PYTHONPATH=${REMOTE_DIR}:\$PYTHONPATH && \
    python -m eval.e2e_cluster_eval ${PARADIGM} ${SCENARIO} ${SKIP_WORKLOAD} ${MODE} ${VERBOSE}"

echo ""
echo "  Evaluation complete."

# ── Step 4: Download results ──
echo "[Step 4/4] Downloading results..."
mkdir -p "${SCRIPT_DIR}/eval/results"

# Find the latest result file
LATEST=$(${SSH_CMD} "ls -t ${REMOTE_DIR}/eval/results/cluster_eval_*.json 2>/dev/null | head -1")
if [ -n "${LATEST}" ]; then
    LOCAL_RESULT="${SCRIPT_DIR}/eval/results/$(basename ${LATEST})"
    ${SCP_CMD} "${TARGET}:${LATEST}" "${LOCAL_RESULT}"
    echo "  Results saved to: ${LOCAL_RESULT}"
else
    echo "  No result files found on cluster."
fi

# Cleanup port-forward
${SSH_CMD} "pkill -f 'kubectl.*port-forward.*social-network' 2>/dev/null || true"

echo ""
echo "========================================================"
echo "  Evaluation Complete!"
if [ -n "${LOCAL_RESULT}" ]; then
    echo "  Report: ${LOCAL_RESULT}"
fi
echo "========================================================"
