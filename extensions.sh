#!/usr/bin/env bash
set -euo pipefail

command_name=${1:-list}
case "${command_name}" in list|approve|revoke) ;; *) echo "usage: ./extensions.sh [list|approve|revoke]" >&2; exit 2 ;; esac
root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
# `list` only renders the approval manifest, so it initialises without the launcher lock and stays
# usable while a session is running. `approve` and `revoke` write that manifest and still take the
# lock exclusively, which is what stops a revocation landing under a live container's bind.
if [[ "${command_name}" == list ]]; then
  harness_init_readonly
else
  harness_init
fi
python3 tools/approve_extensions.py "${command_name}" \
  --workspace "${HARNESS_WORKSPACE}" \
  --manifest "${HARNESS_RUNTIME_DIR}/extension-approval.json"
