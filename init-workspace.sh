#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init
python3 tools/safe_workspace_init.py "${HARNESS_WORKSPACE}"
python3 tools/verify_bind_paths.py record "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/binds.json" >/dev/null
