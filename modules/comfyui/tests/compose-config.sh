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
export COMFYUI_VERSION=v0.35.0
export COMFYUI_COMMIT=40c4fcdf513a4523e39d54a9d391908af8df8171
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
