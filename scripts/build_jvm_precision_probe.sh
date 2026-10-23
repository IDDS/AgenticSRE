#!/usr/bin/env bash

set -euo pipefail

source_file="${1:-deploy/precision-fault-injection/bytecode/AgenticSREBytecodeProbe.java}"
output_dir="${2:-deploy/precision-fault-injection/bytecode/classes}"

if [[ ! -f "${source_file}" ]]; then
  printf 'JVM probe source not found: %s\n' "${source_file}" >&2
  exit 1
fi
command -v javac >/dev/null 2>&1 || {
  printf 'javac is required to build the JVM precision probe\n' >&2
  exit 1
}
mkdir -p "${output_dir}"
javac -g:none -d "${output_dir}" "${source_file}"
sha256sum "${output_dir}/AgenticSREBytecodeProbe.class"
