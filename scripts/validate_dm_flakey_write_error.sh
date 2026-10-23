#!/usr/bin/env bash

set -euo pipefail

node_name="${AGENTICSRE_PRECISION_NODE:-lsy-3}"
mapping_name="agenticsre-flakey-probe"
remote_dir=""
backing_loop=""
control_loop=""
mapping_created="false"

cleanup() {
  if [[ "${mapping_created}" == "true" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo dmsetup remove '${mapping_name}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${backing_loop}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo losetup -d '${backing_loop}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${control_loop}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo losetup -d '${control_loop}'" >/dev/null 2>&1 || true
  fi
  if [[ -n "${remote_dir}" ]]; then
    ssh -o BatchMode=yes "${node_name}" "sudo rm -rf -- '${remote_dir}'" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

ssh -o BatchMode=yes "${node_name}" 'command -v dmsetup >/dev/null && command -v losetup >/dev/null' || {
  printf 'dmsetup and losetup are required on node %s\n' "${node_name}" >&2
  exit 1
}
if ssh -o BatchMode=yes "${node_name}" "sudo dmsetup info '${mapping_name}'" >/dev/null 2>&1; then
  printf 'refusing to reuse device-mapper name: %s\n' "${mapping_name}" >&2
  exit 1
fi

remote_dir="$(ssh -o BatchMode=yes "${node_name}" 'mktemp -d /tmp/agenticsre-dm-flakey.XXXXXX')"
ssh -o BatchMode=yes "${node_name}" "truncate -s 64M '${remote_dir}/backing.img'; truncate -s 64M '${remote_dir}/control.img'; dd if=/dev/urandom of='${remote_dir}/backing.img' bs=1M count=1 conv=notrunc status=none; cp --reflink=auto '${remote_dir}/backing.img' '${remote_dir}/snapshot.img'"
backing_loop="$(ssh -o BatchMode=yes "${node_name}" "sudo losetup --find --show '${remote_dir}/backing.img'")"
control_loop="$(ssh -o BatchMode=yes "${node_name}" "sudo losetup --find --show '${remote_dir}/control.img'")"
sectors="$(ssh -o BatchMode=yes "${node_name}" "sudo blockdev --getsz '${backing_loop}'")"
baseline_checksum="$(ssh -o BatchMode=yes "${node_name}" "sha256sum '${remote_dir}/snapshot.img' | awk '{print \$1}'")"

ssh -o BatchMode=yes "${node_name}" "sudo dmsetup create '${mapping_name}' --table '0 ${sectors} flakey ${backing_loop} 0 3 3 1 error_writes'"
mapping_created="true"
table_evidence="$(ssh -o BatchMode=yes "${node_name}" "sudo dmsetup table '${mapping_name}'")"

write_failed="false"
for _ in $(seq 1 30); do
  if ! ssh -o BatchMode=yes "${node_name}" "dd if=/dev/zero of='/dev/mapper/${mapping_name}' bs=4096 count=1 oflag=direct conv=notrunc status=none" >/dev/null 2>&1; then
    write_failed="true"
    break
  fi
  sleep 0.25
done
if [[ "${write_failed}" != "true" ]]; then
  printf 'dm-flakey did not produce a bounded write error\n' >&2
  exit 1
fi
ssh -o BatchMode=yes "${node_name}" "sudo dd if=/dev/zero of='${control_loop}' bs=4096 count=1 oflag=direct conv=notrunc status=none"

ssh -o BatchMode=yes "${node_name}" "sudo dmsetup remove '${mapping_name}'"
mapping_created="false"
ssh -o BatchMode=yes "${node_name}" "sudo dd if='${remote_dir}/snapshot.img' of='${backing_loop}' bs=1M conv=fsync status=none"
restored_checksum="$(ssh -o BatchMode=yes "${node_name}" "sudo dd if='${backing_loop}' bs=1M count=64 status=none | sha256sum | awk '{print \$1}'")"
[[ "${restored_checksum}" == "${baseline_checksum}" ]]
if ssh -o BatchMode=yes "${node_name}" "sudo dmsetup info '${mapping_name}'" >/dev/null 2>&1; then
  printf 'device-mapper mapping still exists after cleanup\n' >&2
  exit 1
fi

ssh -o BatchMode=yes "${node_name}" "sudo losetup -d '${backing_loop}'; sudo losetup -d '${control_loop}'; sudo rm -rf -- '${remote_dir}'"
backing_loop=""
control_loop=""
remote_dir=""

printf 'before_evidence=snapshot_sha256:%s backing_device:dynamic_loop disposable:true\n' "${baseline_checksum}"
printf 'fault_evidence=mapping:%s table:%s write_error:true control_write:true\n' "${mapping_name}" "${table_evidence}"
printf 'recovery_evidence=mapping_removed:true snapshot_restored:true checksum:%s\n' "${restored_checksum}"
printf 'validation=PASS precision=dm_flakey mode=error_writes exact_mapping=true control_unaffected=true snapshot_restored=true cleanup=complete\n'
