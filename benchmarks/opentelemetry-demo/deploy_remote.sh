#!/bin/bash
# Deploy OpenTelemetry Demo on a remote Kubernetes cluster for AgenticSRE tests.
#
# Defaults target the existing remote alias used by this workspace:
#   REMOTE_HOST=snail ./benchmarks/opentelemetry-demo/deploy_remote.sh
#
# Actions:
#   deploy  Install/upgrade the benchmark
#   status  Show rollout, pods, services, and access URLs
#   stop    Uninstall the benchmark and delete the namespace

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-snail}"
NAMESPACE="${NAMESPACE:-otel-demo}"
RELEASE="${RELEASE:-otel-demo}"
CHART_REPO_NAME="${CHART_REPO_NAME:-open-telemetry}"
CHART_REPO_URL="${CHART_REPO_URL:-https://open-telemetry.github.io/opentelemetry-helm-charts}"
CHART="${CHART:-open-telemetry/opentelemetry-demo}"
CHART_VERSION="${CHART_VERSION:-0.40.9}"
NODE_PORT="${NODE_PORT:-30088}"
PROMETHEUS_NODE_PORT="${PROMETHEUS_NODE_PORT:-30091}"
REMOTE_DIR="${REMOTE_DIR:-/tmp/agenticsre-otel-demo}"
VALUES_FILE="${SCRIPT_DIR}/values-agenticsre.yaml"
ACTION="${1:-deploy}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [deploy|status|stop]

Environment:
  REMOTE_HOST=${REMOTE_HOST}
  NAMESPACE=${NAMESPACE}
  RELEASE=${RELEASE}
  CHART_VERSION=${CHART_VERSION}
  NODE_PORT=${NODE_PORT}
  PROMETHEUS_NODE_PORT=${PROMETHEUS_NODE_PORT}
EOF
}

remote() {
    ssh -o ConnectTimeout=20 "${REMOTE_HOST}" "$@"
}

case "${ACTION}" in
    deploy|status|stop|--help|-h) ;;
    *) usage; error "Unknown action: ${ACTION}" ;;
esac

if [ "${ACTION}" = "--help" ] || [ "${ACTION}" = "-h" ]; then
    usage
    exit 0
fi

if [ "${ACTION}" = "stop" ]; then
    info "Uninstalling ${RELEASE} from ${REMOTE_HOST}/${NAMESPACE}..."
    remote "helm uninstall '${RELEASE}' -n '${NAMESPACE}' 2>/dev/null || true; kubectl delete ns '${NAMESPACE}' --ignore-not-found=true"
    exit 0
fi

if [ "${ACTION}" = "deploy" ]; then
    [ -f "${VALUES_FILE}" ] || error "Missing values file: ${VALUES_FILE}"
    info "Copying values file to ${REMOTE_HOST}:${REMOTE_DIR}/..."
    remote "mkdir -p '${REMOTE_DIR}'"
    scp -o ConnectTimeout=20 "${VALUES_FILE}" "${REMOTE_HOST}:${REMOTE_DIR}/values-agenticsre.yaml" >/dev/null

    info "Installing/upgrading OpenTelemetry Demo ${CHART_VERSION} in namespace ${NAMESPACE}..."
    remote "set -euo pipefail
helm repo add '${CHART_REPO_NAME}' '${CHART_REPO_URL}' 2>/dev/null || true
helm repo update '${CHART_REPO_NAME}'
kubectl create namespace '${NAMESPACE}' --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install '${RELEASE}' '${CHART}' \
  --version '${CHART_VERSION}' \
  --namespace '${NAMESPACE}' \
  --values '${REMOTE_DIR}/values-agenticsre.yaml' \
  --wait \
  --timeout 15m
kubectl -n '${NAMESPACE}' patch svc prometheus --type merge -p '{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"name\":\"http\",\"port\":9090,\"protocol\":\"TCP\",\"targetPort\":9090,\"nodePort\":${PROMETHEUS_NODE_PORT}}]}}'
"
fi

info "Checking benchmark status..."
remote "set -e
kubectl -n '${NAMESPACE}' get pods -o wide
kubectl -n '${NAMESPACE}' get svc frontend-proxy grafana jaeger prometheus 2>/dev/null || kubectl -n '${NAMESPACE}' get svc
kubectl -n '${NAMESPACE}' rollout status deployment/frontend-proxy --timeout=120s
NODE_IP=\$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}')
echo ''
echo 'OpenTelemetry Demo access:'
echo \"  Frontend proxy: http://\${NODE_IP}:${NODE_PORT}\"
echo \"  Prometheus:     http://\${NODE_IP}:${PROMETHEUS_NODE_PORT}\"
echo \"  Jaeger path:    http://\${NODE_IP}:${NODE_PORT}/jaeger/ui\"
echo \"  Load generator: http://\${NODE_IP}:${NODE_PORT}/loadgen/\"
echo \"  Feature flags:  http://\${NODE_IP}:${NODE_PORT}/feature/\"
echo ''
curl -fsS \"http://\${NODE_IP}:${NODE_PORT}\" >/dev/null && echo 'Frontend HTTP check: OK' || echo 'Frontend HTTP check: FAILED'
"

info "Done."
