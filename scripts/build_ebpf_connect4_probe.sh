#!/usr/bin/env bash

set -euo pipefail

source_file="deploy/precision-fault-injection/ebpf/connect4_deny.c"
output_file="deploy/precision-fault-injection/ebpf/connect4_deny.o"
bpf_clang="${BPF_CLANG:-}"

if [[ -z "${bpf_clang}" ]]; then
  if [[ -x /opt/homebrew/opt/llvm/bin/clang ]]; then
    bpf_clang="/opt/homebrew/opt/llvm/bin/clang"
  else
    bpf_clang="$(command -v clang || true)"
  fi
fi
if [[ -z "${bpf_clang}" ]]; then
  printf 'BPF-capable clang not found\n' >&2
  exit 1
fi
if ! "${bpf_clang}" --print-targets 2>/dev/null | grep -q 'bpf'; then
  printf 'clang does not provide a BPF target: %s\n' "${bpf_clang}" >&2
  exit 1
fi

"${bpf_clang}" -target bpfel -O2 -g -c "${source_file}" -o "${output_file}"
printf 'built=%s compiler=%s\n' "${output_file}" "${bpf_clang}"
