#!/usr/bin/env bash

set -euo pipefail

output_dir="${1:-/tmp/agenticsre-c-native-build}"
source_dir="deploy/precision-fault-injection/c-native"

if ! command -v musl-gcc >/dev/null 2>&1; then
  printf 'musl-gcc is required to build Alpine-compatible C probes\n' >&2
  exit 1
fi

mkdir -p "${output_dir}"
musl-gcc -g -O0 -fPIC -shared -o "${output_dir}/libstable.so" "${source_dir}/libstable.c"
musl-gcc -g -O0 -fno-omit-frame-pointer -o "${output_dir}/probe" \
  "${source_dir}/probe.c" -L"${output_dir}" -lstable -Wl,-rpath,/opt/agenticsre
musl-gcc -g -O0 -fPIC -shared -o "${output_dir}/libagenticsre_return_override.so" \
  "${source_dir}/return_override.c" -ldl

readelf -Ws "${output_dir}/libstable.so" | grep -q 'stable_operation'
readelf -Ws "${output_dir}/libagenticsre_return_override.so" | grep -q 'stable_operation'
chmod 0555 "${output_dir}/probe" "${output_dir}/libstable.so" "${output_dir}/libagenticsre_return_override.so"
sha256sum "${output_dir}/probe" "${output_dir}/libstable.so" "${output_dir}/libagenticsre_return_override.so"
