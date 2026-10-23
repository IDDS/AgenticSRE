#!/usr/bin/env bash
# Self-extracting customer-site installer. The build script appends one
# complete, checksummed bundle archive below the marker.

set -Eeuo pipefail
umask 077

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
PAYLOAD_MARKER="__AGENTICSRE_OFFLINE_PAYLOAD_BELOW__"
PAYLOAD_LINE="$(awk -v marker="${PAYLOAD_MARKER}" '$0 == marker {print NR + 1; exit}' "${SELF}")"
[ -n "${PAYLOAD_LINE}" ] || { printf '[ERROR] invalid offline installer: payload marker is missing\n' >&2; exit 2; }

EXTRACT_ROOT="$(mktemp -d /tmp/agenticsre-oneclick.XXXXXX)"
cleanup() { rm -rf -- "${EXTRACT_ROOT}"; }
trap cleanup EXIT

# tail preserves every byte of the gzip stream after the shell stub.
tail -n +"${PAYLOAD_LINE}" "${SELF}" | tar -xzf - -C "${EXTRACT_ROOT}"
BUNDLE_ROOT=""
for candidate in "${EXTRACT_ROOT}"/*; do
    if [ -d "${candidate}" ]; then BUNDLE_ROOT="${candidate}"; break; fi
done
[ -n "${BUNDLE_ROOT}" ] || { printf '[ERROR] invalid offline installer: bundle directory is missing\n' >&2; exit 2; }
[ -x "${BUNDLE_ROOT}/install-offline.sh" ] || { printf '[ERROR] invalid offline installer: install-offline.sh is missing\n' >&2; exit 2; }

"${BUNDLE_ROOT}/install-offline.sh" "$@"
exit $?

__AGENTICSRE_OFFLINE_PAYLOAD_BELOW__
