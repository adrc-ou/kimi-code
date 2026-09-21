#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../../.." && pwd -P)
fixture=${HARNESS_TEST_FIXTURE:?Run tests/compose-config.sh}
mkdir -p "${fixture}/workspace/comfyui/"{models,custom_nodes,input,output,temp,user}
for kind in models custom_nodes input output temp user; do
  variable=$(printf 'COMFYUI_%s_PATH' "${kind}" | tr '[:lower:]' '[:upper:]')
  export "${variable}=${fixture}/workspace/comfyui/${kind}"
done
export COMFYUI_TOKEN=test-bridge-token-with-at-least-32-characters
export COMFYUI_BRIDGE_CERT="${fixture}/cert.crt"
# The build now takes its dependency lock from a named context, and Compose interpolates that
# reference while resolving the configuration, so these checks have to stand in for what the
# launcher's prepare hook stages. The values come from the reviewed document rather than from here,
# so this cannot keep passing after the baseline moves and the digests stop agreeing.
{
  read -r COMFYUI_VERSION
  read -r COMFYUI_COMMIT
  read -r COMFYUI_REQUIREMENTS_SHA256
} < <(python3 - "${root}" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(
    (Path(sys.argv[1]) / "modules/comfyui/backend/compatibility.json").read_text()
)
baseline = next(p["baseline"] for p in document["platforms"] if p["platform"] == "wsl2-x86_64")
print(baseline["comfyui_version"])
print(baseline["comfyui_commit"])
print(baseline["requirements_sha256"])
PY
)
export COMFYUI_VERSION COMFYUI_COMMIT COMFYUI_REQUIREMENTS_SHA256
COMFYUI_LOCK_CONTEXT="${fixture}/comfyui/build"
mkdir -p "${COMFYUI_LOCK_CONTEXT}"
cp "${root}/modules/comfyui/backend/requirements-linux.lock" "${COMFYUI_LOCK_CONTEXT}/requirements.lock"
export COMFYUI_LOCK_CONTEXT
MODULE_DIR="${root}/modules/comfyui"
# shellcheck disable=SC1091
source "${MODULE_DIR}/module.sh"
comfy_backend
# The shared assertions, over this module's overlay: a module must not add a host bind to
# kimi-agent, loosen a root filesystem, or publish a port beyond loopback.
for backend in cuda mps; do
  docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" \
    -f "${root}/modules/comfyui/compose.${backend}.yaml" config --format json |
    python3 "${root}/tools/compose_hygiene.py" \
      --workspace "${WORKSPACE_PATH}" --runtime-dir "${HARNESS_RUNTIME_DIR}" \
      --label "comfyui ${backend}"
done
