#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init_readonly
[[ -f "${HARNESS_STATE_FILE}" && -f "${HARNESS_RUNTIME_DIR}/runtime.env" ]] || {
  echo "No running runtime for this workspace. Run ./start.sh first." >&2
  exit 1
}
set -a
# shellcheck disable=SC1091
source comfy/backend.env
# shellcheck disable=SC1090
source "${HARNESS_STATE_FILE}"
# shellcheck disable=SC1091
source "${HARNESS_RUNTIME_DIR}/runtime.env"
set +a
python3 tools/verify_bind_paths.py verify "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/binds.json"
bind_assignments=$(python3 tools/verify_bind_paths.py emit "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/binds.json")
while IFS= read -r assignment; do
  variable=${assignment%%=*}
  export "${variable}=${assignment#*=}"
done <<<"${bind_assignments}"
harness_compose_files
harness_compose exec kimi-agent bash
