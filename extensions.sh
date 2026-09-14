#!/usr/bin/env bash
set -euo pipefail

command_name=${1:-list}
case "${command_name}" in list|approve|revoke) ;; *) echo "usage: ./extensions.sh [list|approve|revoke]" >&2; exit 2 ;; esac
root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init
python3 tools/approve_extensions.py "${command_name}" \
  --workspace "${HARNESS_WORKSPACE}" \
  --manifest "${HARNESS_RUNTIME_DIR}/extension-approval.json"
