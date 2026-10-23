#!/usr/bin/env bash
# Runs inside an ephemeral, network-isolated systemd Linux target.

set -Eeuo pipefail
umask 077

BUNDLE_DIR="${BUNDLE_DIR:-/bundle}"
RESULT_DIR="${RESULT_DIR:-/results}"
ARTIFACT_MODE="${E2E_ARTIFACT_MODE:-bundle}"
INSTALLER_FILENAME="${E2E_INSTALLER_FILENAME:-install-offline.sh}"
INSTALLER="${BUNDLE_DIR}/${INSTALLER_FILENAME}"
NO_SYSTEMD="${E2E_NO_SYSTEMD:-0}"
DOCKER_STORAGE_DRIVER="${E2E_DOCKER_STORAGE_DRIVER:-vfs}"
CUSTOMER_ENV="/tmp/agenticsre-customer.env"
BACKUP_FILE="/var/lib/agenticsre/backups/offline-e2e.tar.gz"
MARKER_FILE="/var/lib/agenticsre/data/offline-e2e-marker.json"
OFFLINE_AUDIT_FILE="/var/lib/agenticsre/offline-install-audit.env"

mkdir -p "${RESULT_DIR}"
exec > >(tee "${RESULT_DIR}/install-e2e.log") 2>&1

collect_diagnostics() {
    local exit_code=$?
    trap - ERR
    if [ "${exit_code}" -ne 0 ]; then
        {
            printf '\n[E2E DIAGNOSTICS] systemd state\n'
            if command -v systemctl >/dev/null 2>&1; then
                systemctl is-system-running || true
                systemctl status docker --no-pager || true
            fi
            if command -v journalctl >/dev/null 2>&1; then journalctl -u docker --no-pager -n 200 || true; fi
            cat /var/log/agenticsre-docker.log || true
            if command -v systemctl >/dev/null 2>&1; then systemctl stop docker || true; fi
            printf '\n[E2E DIAGNOSTICS] dockerd foreground debug output\n'
            timeout 20 /usr/local/bin/dockerd --debug --host=unix:///var/run/agenticsre-e2e-docker.sock || true
            printf '\n[E2E DIAGNOSTICS] cgroup and mounts\n'
            mount | grep -E 'cgroup| /tmp | /run ' || true
            cat /proc/self/cgroup || true
        } > "${RESULT_DIR}/failure-diagnostics.log" 2>&1
    fi
    return "${exit_code}"
}
trap collect_diagnostics ERR

fail() {
    printf '[E2E ERROR] %s\n' "$*" >&2
    exit 1
}

printf '[E2E] Confirming the target has no external network\n'
if curl --fail --silent --show-error --connect-timeout 2 https://example.com >/dev/null 2>&1; then
    fail "the supposedly isolated target reached the Internet"
fi

if [ "${NO_SYSTEMD}" = "1" ]; then
    # Keep PID 1 running for the container, but remove the systemctl command
    # from the customer user space before invoking the real installer. This
    # exercises the installer branch that has no systemd dependency.
    systemctl_path="$(command -v systemctl || true)"
    [ -n "${systemctl_path}" ] || fail "cannot prepare no-systemd test: systemctl was not found"
    mv "${systemctl_path}" "${systemctl_path}.e2e-disabled"
    hash -r 2>/dev/null || true
    command -v systemctl >/dev/null 2>&1 && fail "systemctl is still visible in no-systemd test"
fi

[ -x "${INSTALLER}" ] || fail "installer is missing or not executable"
INSTALL_ARGS=()
if [ "${ARTIFACT_MODE}" = "bundle" ]; then
    cp "${BUNDLE_DIR}/config/customer.env.example" "${CUSTOMER_ENV}"
    sed -i \
        -e 's/^LLM_API_KEY=.*/LLM_API_KEY=/' \
        -e 's/^LLM_BASE_URL=.*/LLM_BASE_URL=http:\/\/127.0.0.1:8000\/v1/' \
        -e 's/^AGENTICSRE_AUTH_PASSWORD=.*/AGENTICSRE_AUTH_PASSWORD=offline-e2e-only/' \
        -e 's/^AGENTICSRE_AUTH_TOKEN=.*/AGENTICSRE_AUTH_TOKEN=offline-e2e-token/' \
        "${CUSTOMER_ENV}"
    chmod 0600 "${CUSTOMER_ENV}"
    INSTALL_ARGS=(--config "${CUSTOMER_ENV}")
elif [ "${ARTIFACT_MODE}" != "run" ]; then
    fail "unknown artifact mode: ${ARTIFACT_MODE}"
fi

printf '[E2E] Running the exact customer one-command installation\n'
AGENTICSRE_HEALTH_TIMEOUT=300 AGENTICSRE_DOCKER_STORAGE_DRIVER="${DOCKER_STORAGE_DRIVER}" \
    "${INSTALLER}" "${INSTALL_ARGS[@]}"

printf '[E2E] Checking installed runtime, services, and persistent database\n'
if [ "${NO_SYSTEMD}" != "1" ]; then systemctl is-enabled docker; fi
docker info >/dev/null
docker compose version
agenticsrectl --status
agenticsrectl --data-check
[ -f "${OFFLINE_AUDIT_FILE}" ] || fail "offline installation audit file is missing"
grep -q '^RESULT=PASS$' "${OFFLINE_AUDIT_FILE}" || fail "offline installation audit did not pass"
grep -q '^DEPLOYMENT_MODE=strict-offline$' "${OFFLINE_AUDIT_FILE}" || fail "bundle was not strict-offline"
grep -q '^REQUIRES_INTERNET=0$' "${OFFLINE_AUDIT_FILE}" || fail "bundle requires Internet"
grep -q '^IMAGE_LOAD_METHOD=docker-load$' "${OFFLINE_AUDIT_FILE}" || fail "image was not loaded locally"
grep -q '^COMPOSE_PULL_POLICY=never$' "${OFFLINE_AUDIT_FILE}" || fail "Compose pull policy is not never"

printf '{"value":"before-backup"}\n' > "${MARKER_FILE}"
printf '[E2E] Exercising consistent backup and verified restore\n'
agenticsrectl --backup "${BACKUP_FILE}"
printf '{"value":"after-backup"}\n' > "${MARKER_FILE}"
agenticsrectl --restore "${BACKUP_FILE}"
grep -q 'before-backup' "${MARKER_FILE}" || fail "restored marker does not match backup"

printf '[E2E] Exercising restart and final health checks\n'
agenticsrectl --restart
agenticsrectl --data-check
for service in agenticsre agenticsre-skill-runtime; do
    healthy=0
    for attempt in $(seq 1 60); do
        state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${service}")"
        if [ "${state}" = healthy ]; then healthy=1; break; fi
        sleep 2
    done
    [ "${healthy}" = 1 ] || fail "${service} did not become healthy after restart"
done

{
    printf 'result=PASS\n'
    printf 'network=none\n'
    printf 'offline_contract=strict-offline\n'
    printf 'installer_network_fetches=0\n'
    printf 'image_load_method=docker-load\n'
    printf 'compose_pull_policy=never\n'
    printf 'host_arch=%s\n' "$(uname -m)"
    printf 'docker_version=%s\n' "$(docker version --format '{{.Server.Version}}')"
    printf 'docker_storage_driver=%s\n' "$(docker info --format '{{.Driver}}')"
    if [ "${NO_SYSTEMD}" = "1" ]; then
        printf 'docker_start_mode=direct-no-systemd\n'
    elif systemctl is-active docker >/dev/null 2>&1; then
        printf 'docker_start_mode=systemd\n'
    else
        printf 'docker_start_mode=direct-fallback\n'
    fi
    printf 'compose_version=%s\n' "$(docker compose version --short)"
    printf 'dashboard=%s\n' "$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' agenticsre)"
    printf 'skill_runtime=%s\n' "$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' agenticsre-skill-runtime)"
    printf 'backup_sha256=%s\n' "$(sha256sum "${BACKUP_FILE}" | awk '{print $1}')"
    printf 'marker=%s\n' "$(tr -d '\n' < "${MARKER_FILE}")"
} > "${RESULT_DIR}/summary.txt"

cat "${RESULT_DIR}/summary.txt"
