#!/usr/bin/env bash
set -euo pipefail

expected_backend=${1:?usage: acceptance.sh mps|cuda}
root=$(cd "$(dirname "$0")/../../.." && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init_readonly
set -a
# shellcheck disable=SC1091
source modules/comfyui/backend/backend.env
# shellcheck disable=SC1090
source "${HARNESS_STATE_FILE}"
# shellcheck disable=SC1091
source "${HARNESS_RUNTIME_DIR}/runtime.env"
set +a
[[ "${COMFYUI_BACKEND:-}" == "${expected_backend}" ]] || { echo "Start a session with ComfyUI enabled for ${expected_backend}" >&2; exit 2; }
python3 modules/comfyui/scripts/verify_bind_paths.py verify "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/module-data/comfyui/binds.json"
bind_assignments=$(python3 modules/comfyui/scripts/verify_bind_paths.py emit "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/module-data/comfyui/binds.json")
while IFS= read -r assignment; do
  variable=${assignment%%=*}
  export "${variable}=${assignment#*=}"
done <<<"${bind_assignments}"
[[ "${expected_backend}" == mps ]] && export COMFYUI_BRIDGE_CERT="${HARNESS_RUNTIME_DIR}/module-data/comfyui/bridge.crt"
harness_compose_files
harness_validate_compose

harness_compose exec model-proxy python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py stats
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py queue
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py schema EmptyImage
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py upload /opt/kimi-runtime/tools/comfy-smoke.ppm
prompt_result=$(harness_compose exec -T kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py run /opt/kimi-runtime/tools/comfy-smoke-api.json)
prompt_id=$(printf '%s' "${prompt_result}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["prompt_id"])')
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py wait --timeout 120 "${prompt_id}"
harness_compose exec kimi-agent python3 /opt/kimi-runtime/tools/comfyctl.py download "${prompt_id}" /workspace/comfyui/output/acceptance-download

# Expansion must occur inside the container.
# shellcheck disable=SC2016
harness_compose exec kimi-agent sh -c 'test -z "${NRP_API_KEY:-}"'
# shellcheck disable=SC2016
harness_compose exec kimi-agent sh -c 'test -z "${QWEN3_API_KEY:-}"'
# Credentials are mounted only into model-proxy, under the secret name the selected
# definitions generate, so no other service may read any file below /run/secrets.
harness_compose exec kimi-agent sh -c '! ls -A /run/secrets/* >/dev/null 2>&1'
harness_compose exec search-adapter sh -c '! ls -A /run/secrets/* >/dev/null 2>&1 && ! getent hosts model-proxy'
if [[ "${expected_backend}" == cuda ]]; then
  harness_compose exec comfyui sh -c '! ls -A /run/secrets/* >/dev/null 2>&1 && ! getent hosts model-proxy'
  harness_compose exec comfyui python -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name())'
else
  "${HARNESS_RUNTIME_DIR}/module-data/comfyui/app/current"/venv/bin/python -c 'import torch; assert torch.backends.mps.is_available()'
fi

harness_compose exec kimi-agent sh -c \
  'test ! -w /home/agent/.kimi-code/SYSTEM.md && test ! -w /home/agent/.kimi-code/agents && test ! -w /workspace/.kimi-code/skills'
harness_compose exec kimi-agent sh -c \
  '! ps -ef | grep "[c]hrome.*--no-sandbox" && ! ps -ef | grep "[c]hrome.*--disable-setuid-sandbox"'
harness_compose exec kimi-agent python3 -c \
  "import os; from pathlib import Path; paths=[Path('/workspace/comfyui/custom_nodes'), Path('/workspace/comfyui/user/default/workflows')]; assert all(path.is_dir() and os.access(path, os.W_OK) for path in paths)"

echo "Core acceptance checks passed. Validate enabled MCP servers with /mcp in a fresh Kimi session."
