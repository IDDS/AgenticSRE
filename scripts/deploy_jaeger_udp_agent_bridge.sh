#!/bin/bash
# Deploy a Jaeger v1 agent bridge for legacy UDP clients.
#
# Legacy services send spans to UDP 6831. This DaemonSet receives UDP locally
# and forwards to an existing Jaeger collector endpoint.
set -euo pipefail

NAMESPACE="${JAEGER_AGENT_NAMESPACE:-observability}"
NAME="${JAEGER_AGENT_NAME:-jaeger-agent-udp-bridge}"
IMAGE="${JAEGER_AGENT_IMAGE:-jaegertracing/jaeger-agent:1.64.0}"
REPORTER_PROTOCOL="${JAEGER_AGENT_REPORTER_PROTOCOL:-grpc}"
COLLECTOR_HOST_PORT="${JAEGER_COLLECTOR_HOST_PORT:-jaeger:14250}"
KUBECTL="${KUBECTL:-kubectl}"
DRY_RUN="${JAEGER_AGENT_DRY_RUN:-0}"

case "${REPORTER_PROTOCOL}" in
    grpc)
        REPORTER_ARG="--reporter.grpc.host-port=${COLLECTOR_HOST_PORT}"
        ;;
    tchannel)
        REPORTER_ARG="--reporter.tchannel.host-port=${COLLECTOR_HOST_PORT}"
        ;;
    *)
        echo "JAEGER_AGENT_REPORTER_PROTOCOL must be grpc or tchannel" >&2
        exit 2
        ;;
esac

render_manifest() {
    cat <<YAML
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${NAME}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: ${NAME}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${NAME}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: jaeger-agent
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          args:
            - ${REPORTER_ARG}
          ports:
            - name: thrift-compact
              containerPort: 6831
              hostPort: 6831
              protocol: UDP
            - name: thrift-binary
              containerPort: 6832
              hostPort: 6832
              protocol: UDP
YAML
}

if [ "${DRY_RUN}" = "1" ]; then
    render_manifest
    exit 0
fi

"${KUBECTL}" create namespace "${NAMESPACE}" --dry-run=client -o yaml | "${KUBECTL}" apply -f -
render_manifest | "${KUBECTL}" apply -f -

"${KUBECTL}" -n "${NAMESPACE}" rollout status "daemonset/${NAME}" --timeout=120s
