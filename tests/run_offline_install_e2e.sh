#!/usr/bin/env bash
# End-to-end customer-site installation test. The target starts with systemd
# but without Docker, Python, pip, Git, a database, or network connectivity.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT="${1:-}"
RESULT_DIR="${2:-}"
TARGET_IMAGE="${AGENTICSRE_E2E_TARGET_IMAGE:-agenticsre-offline-install-target:bookworm-amd64}"
TARGET_BASE_IMAGE="${AGENTICSRE_E2E_TARGET_BASE_IMAGE:-agenticsre-offline:validation-amd64}"
TARGET_NAME="agenticsre-offline-install-e2e-$$"
NO_SYSTEMD="${E2E_NO_SYSTEMD:-0}"
DOCKER_DATA_DIR="${AGENTICSRE_E2E_DOCKER_DATA_DIR:-}"
INNER_STORAGE_DRIVER="${AGENTICSRE_E2E_INNER_STORAGE_DRIVER:-vfs}"

[ -n "${ARTIFACT}" ] || { printf 'usage: %s BUNDLE_DIR|INSTALLER.run [RESULT_DIR]\n' "$0" >&2; exit 2; }
ARTIFACT="$(cd "$(dirname "${ARTIFACT}")" && pwd -P)/$(basename "${ARTIFACT}")"
if [ -d "${ARTIFACT}" ]; then
    [ -f "${ARTIFACT}/install-offline.sh" ] || { printf 'invalid bundle: %s\n' "${ARTIFACT}" >&2; exit 2; }
    ARTIFACT_MODE=bundle
    BUNDLE_MOUNT_DIR="${ARTIFACT}"
    INSTALLER_FILENAME=install-offline.sh
else
    [ -x "${ARTIFACT}" ] || { printf 'invalid single-file installer: %s\n' "${ARTIFACT}" >&2; exit 2; }
    ARTIFACT_MODE=run
    BUNDLE_MOUNT_DIR="$(dirname "${ARTIFACT}")"
    INSTALLER_FILENAME="$(basename "${ARTIFACT}")"
fi

if [ -z "${RESULT_DIR}" ]; then
    RESULT_DIR="$(mktemp -d /tmp/agenticsre-offline-e2e-results.XXXXXX)"
else
    mkdir -p "${RESULT_DIR}"
    RESULT_DIR="$(cd "${RESULT_DIR}" && pwd -P)"
fi

cleanup() {
    docker stop --time 30 "${TARGET_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ "${AGENTICSRE_E2E_SKIP_TARGET_BUILD:-0}" = "1" ]; then
    # A prebuilt target is useful on isolated/space-constrained validation hosts.
    # The actual installer still runs with --network none below.
    docker image inspect "${TARGET_IMAGE}" >/dev/null
else
    docker build --platform linux/amd64 \
        --build-arg "TARGET_BASE_IMAGE=${TARGET_BASE_IMAGE}" \
        --tag "${TARGET_IMAGE}" \
        --file "${ROOT_DIR}/tests/offline_install_target/Dockerfile" \
        "${ROOT_DIR}"
fi

docker_run_args=(
    --rm --detach
    --platform linux/amd64
    --name "${TARGET_NAME}"
    --hostname agenticsre-offline-target
    --privileged
    --cgroupns private
    --network none
    --tmpfs /run
    --tmpfs /run/lock
    --tmpfs /tmp:exec,mode=1777
    --volume "${BUNDLE_MOUNT_DIR}:/bundle:ro"
    --volume "${RESULT_DIR}:/results"
    --volume "${ROOT_DIR}/tests/offline_install_target/run_inside_target.sh:/usr/local/sbin/run-agenticsre-offline-e2e:ro"
    --env "E2E_ARTIFACT_MODE=${ARTIFACT_MODE}"
    --env "E2E_INSTALLER_FILENAME=${INSTALLER_FILENAME}"
    --env "E2E_NO_SYSTEMD=${NO_SYSTEMD}"
    --env "E2E_DOCKER_STORAGE_DRIVER=${INNER_STORAGE_DRIVER}"
)
if [ -n "${DOCKER_DATA_DIR}" ]; then
    [ "${DOCKER_DATA_DIR#/}" != "${DOCKER_DATA_DIR}" ] || {
        printf 'AGENTICSRE_E2E_DOCKER_DATA_DIR must be an absolute path: %s\n' "${DOCKER_DATA_DIR}" >&2
        exit 2
    }
    mkdir -p "${DOCKER_DATA_DIR}"
    DOCKER_DATA_DIR="$(cd "${DOCKER_DATA_DIR}" && pwd -P)"
    docker_run_args+=(--volume "${DOCKER_DATA_DIR}:/var/lib/docker")
fi
if [ "${NO_SYSTEMD}" = "1" ]; then
    # Safer remote-host mode: do not bind the host cgroup filesystem and do not
    # boot nested systemd. The bundled dockerd runs only inside this ephemeral
    # privileged container.
    docker run "${docker_run_args[@]}" "${TARGET_IMAGE}" sleep infinity >/dev/null
else
    docker run "${docker_run_args[@]}" \
        --stop-signal SIGRTMIN+3 \
        --volume /sys/fs/cgroup:/sys/fs/cgroup:rw \
        "${TARGET_IMAGE}" >/dev/null
fi

if [ "${NO_SYSTEMD}" != "1" ]; then
    for _attempt in $(seq 1 60); do
        if docker exec "${TARGET_NAME}" systemctl is-system-running >/dev/null 2>&1; then
            break
        fi
        state="$(docker exec "${TARGET_NAME}" systemctl is-system-running 2>/dev/null || true)"
        [ "${state}" != "degraded" ] || break
        sleep 1
    done
    docker exec "${TARGET_NAME}" systemctl list-units --state=failed --no-legend || true
fi
docker exec "${TARGET_NAME}" /usr/local/sbin/run-agenticsre-offline-e2e
printf 'E2E evidence: %s\n' "${RESULT_DIR}"
