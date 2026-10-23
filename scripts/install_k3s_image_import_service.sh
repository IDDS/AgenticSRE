#!/bin/bash
# Install a systemd oneshot service that imports offline image archives into K3S containerd.
set -euo pipefail

IMAGE_DIR="${K3S_IMAGE_IMPORT_DIR:-}"
SERVICE_NAME="${K3S_IMAGE_IMPORT_SERVICE:-agenticsre-k3s-image-import}"
CTR="${K3S_CTR:-k3s ctr}"
NAMESPACE="${K3S_CONTAINERD_NAMESPACE:-k8s.io}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [ -z "${IMAGE_DIR}" ]; then
    echo "Set K3S_IMAGE_IMPORT_DIR to the directory containing offline image archives" >&2
    exit 2
fi

if [ ! -d "${IMAGE_DIR}" ]; then
    echo "Image archive directory not found: ${IMAGE_DIR}" >&2
    exit 2
fi

install -d "${SYSTEMD_DIR}"

cat > "${SYSTEMD_DIR}/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Import AgenticSRE offline images into K3S containerd
After=k3s.service k3s-agent.service
Wants=k3s.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'set -e; for image in "${IMAGE_DIR}"/*.tar "${IMAGE_DIR}"/*.tar.gz "${IMAGE_DIR}"/*.tgz; do [ -f "$image" ] || continue; ${CTR} -n ${NAMESPACE} images import "$image"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl start "${SERVICE_NAME}.service"
systemctl status "${SERVICE_NAME}.service" --no-pager
