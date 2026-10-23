#!/usr/bin/env bash

set -euo pipefail

output_dir="${1:-/tmp/agenticsre-seccomp-build}"
source_path="deploy/precision-fault-injection/seccomp/seccomp_notify_probe.c"

if ! command -v musl-gcc >/dev/null 2>&1; then
  printf 'musl-gcc is required to build the static seccomp probe\n' >&2
  exit 1
fi

mkdir -p "${output_dir}"
musl-gcc -static -O2 -Wall -Wextra \
  -idirafter /usr/include \
  -idirafter /usr/include/x86_64-linux-gnu \
  -o "${output_dir}/seccomp-notify-probe" "${source_path}"
chmod 0555 "${output_dir}/seccomp-notify-probe"
file "${output_dir}/seccomp-notify-probe"
sha256sum "${output_dir}/seccomp-notify-probe"
