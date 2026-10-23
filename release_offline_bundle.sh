#!/usr/bin/env bash
# Release gate for a customer-site offline bundle. A bundle is releasable only
# after the exact staging directory passes the network-none installation E2E.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

VERSION="${VERSION:-$(date +%Y%m%d)}"
TARGET_ARCH="${TARGET_ARCH:-amd64}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/release}"
BUNDLE_NAME="agenticsre-offline-${VERSION}-linux-${TARGET_ARCH}"
BUNDLE_DIR="${OUTPUT_DIR}/${BUNDLE_NAME}"
BUNDLE_TAR="${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"
BUNDLE_RUN="${OUTPUT_DIR}/${BUNDLE_NAME}.run"
EVIDENCE_DIR="${OUTPUT_DIR}/${BUNDLE_NAME}-e2e"
NO_SYSTEMD_EVIDENCE_DIR="${OUTPUT_DIR}/${BUNDLE_NAME}-e2e-no-systemd"

info() { printf '[RELEASE] %s\n' "$*"; }
die() { printf '[RELEASE ERROR] %s\n' "$*" >&2; exit 1; }

[[ "${VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || die "VERSION contains unsafe characters"
[ "${TARGET_ARCH}" = "amd64" ] || die "the current network-none release target supports amd64 only"
[ -n "${OUTPUT_DIR}" ] && [ "${OUTPUT_DIR}" != "/" ] || die "OUTPUT_DIR must not be empty or /"

info "Building checksummed strict-offline bundle"
VERSION="${VERSION}" TARGET_ARCH="${TARGET_ARCH}" OUTPUT_DIR="${OUTPUT_DIR}" \
    "${SCRIPT_DIR}/build_offline_bundle.sh"

[ -f "${BUNDLE_TAR}" ] || die "bundle archive was not created"
[ -f "${BUNDLE_TAR}.sha256" ] || die "bundle archive checksum was not created"
[ -x "${BUNDLE_RUN}" ] || die "single-file installer was not created or is not executable"
[ -f "${BUNDLE_RUN}.sha256" ] || die "single-file installer checksum was not created"
(cd "${OUTPUT_DIR}" && if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "$(basename "${BUNDLE_TAR}").sha256"
else
    shasum -a 256 -c "$(basename "${BUNDLE_TAR}").sha256"
fi)
(cd "${OUTPUT_DIR}" && if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "$(basename "${BUNDLE_RUN}").sha256"
else
    shasum -a 256 -c "$(basename "${BUNDLE_RUN}").sha256"
fi)

info "Running the exact single-file installer in a Linux target with --network none"
"${SCRIPT_DIR}/tests/run_offline_install_e2e.sh" "${BUNDLE_RUN}" "${EVIDENCE_DIR}"
grep -q '^result=PASS$' "${EVIDENCE_DIR}/summary.txt" || die "offline E2E did not produce PASS"
grep -q '^network=none$' "${EVIDENCE_DIR}/summary.txt" || die "offline E2E was not network-isolated"
grep -q '^offline_contract=strict-offline$' "${EVIDENCE_DIR}/summary.txt" || die "strict-offline contract was not verified"
grep -q '^dashboard=running/healthy$' "${EVIDENCE_DIR}/summary.txt" || die "dashboard did not pass health verification"
grep -q '^skill_runtime=running/healthy$' "${EVIDENCE_DIR}/summary.txt" || die "skill runtime did not pass health verification"

info "Running the same single-file installer with systemctl absent"
E2E_NO_SYSTEMD=1 "${SCRIPT_DIR}/tests/run_offline_install_e2e.sh" "${BUNDLE_RUN}" "${NO_SYSTEMD_EVIDENCE_DIR}"
grep -q '^result=PASS$' "${NO_SYSTEMD_EVIDENCE_DIR}/summary.txt" || die "no-systemd offline E2E did not produce PASS"
grep -q '^network=none$' "${NO_SYSTEMD_EVIDENCE_DIR}/summary.txt" || die "no-systemd E2E was not network-isolated"
grep -q '^docker_start_mode=direct-no-systemd$' "${NO_SYSTEMD_EVIDENCE_DIR}/summary.txt" || die "no-systemd direct Docker startup was not verified"
grep -q '^dashboard=running/healthy$' "${NO_SYSTEMD_EVIDENCE_DIR}/summary.txt" || die "no-systemd dashboard did not pass health verification"
grep -q '^skill_runtime=running/healthy$' "${NO_SYSTEMD_EVIDENCE_DIR}/summary.txt" || die "no-systemd skill runtime did not pass health verification"

info "PASS: release artifact ${BUNDLE_TAR}"
info "SHA-256 file: ${BUNDLE_TAR}.sha256"
info "PASS: single-file installer ${BUNDLE_RUN}"
info "SHA-256 file: ${BUNDLE_RUN}.sha256"
info "E2E evidence: ${EVIDENCE_DIR}"
info "No-systemd E2E evidence: ${NO_SYSTEMD_EVIDENCE_DIR}"
