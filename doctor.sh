#!/usr/bin/env bash
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --full ) ]]; then
  echo "usage: ./doctor.sh [--full]" >&2
  exit 2
fi
root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init_readonly
[[ -f "${HARNESS_RUNTIME_DIR}/compose/resolved.json" ]] || {
  echo "No prepared runtime. Run ./start.sh first." >&2
  exit 1
}
# Locate by labels without loading the credential-bearing resolved Compose file.
container=$(docker ps --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
  --filter label=com.docker.compose.service=kimi-agent --format '{{.ID}}')
[[ -n "${container}" && "${container}" != *$'\n'* ]] || {
  echo "Expected one running agent container. Run ./start.sh first." >&2
  exit 1
}
# Host-side and deliberately non-fatal. The staged documents are installed read-only and immutable,
# so an edit to CONTEXT.md or SYSTEM.md cannot reach a running session; this says so in words instead
# of leaving the operator to guess why their change had no effect.
PYTHONPATH="${root}/tools" python3 - "${root}" "${HARNESS_RUNTIME_DIR}" <<'PY' || true
import pathlib
import sys

import prompt_context

root = pathlib.Path(sys.argv[1])
runtime_dir = pathlib.Path(sys.argv[2])
for notice in prompt_context.stale_sources(root, runtime_dir):
    print(notice)
PY

docker exec "${container}" \
  /opt/serena/bin/python /opt/kimi-runtime/tools/check_services.py "$@"
