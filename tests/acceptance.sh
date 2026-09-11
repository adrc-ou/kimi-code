#!/usr/bin/env bash
set -euo pipefail

backend=${1:?usage: acceptance.sh mps|cuda}
root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "${root}"

case "${backend}" in
  mps) platform_key=darwin-arm64 ;;
  cuda) platform_key=wsl2-x86_64 ;;
  *) echo "backend must be mps or cuda" >&2; exit 2 ;;
esac

# shellcheck disable=SC1091,SC1090
set -a
source comfy/backend.env
source ".local/state/${platform_key}.env"
set +a
workspace_value=$(python3 scripts/read_env.py .env WORKSPACE_PATH)
case "${workspace_value}" in
  /*) WORKSPACE_PATH=${workspace_value} ;;
  *) WORKSPACE_PATH="${root}/${workspace_value}" ;;
esac
export WORKSPACE_PATH
if [[ "${backend}" == "mps" ]]; then
  export LOCAL_UID=1000
  export LOCAL_GID=1000
else
  export LOCAL_UID=${LOCAL_UID:-$(id -u)}
  export LOCAL_GID=${LOCAL_GID:-$(id -g)}
fi

compose_files=(-f compose.yaml -f compose.search.yaml)
if [[ "${backend}" == "cuda" ]]; then
  compose_files+=(-f compose.comfy.cuda.yaml)
fi

compose() {
  docker compose --env-file .env "${compose_files[@]}" "$@"
}

compose exec model-proxy \
  python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

compose exec kimi-agent \
  python /opt/kimi-runtime/tools/comfyctl.py stats

compose exec kimi-agent \
  python /opt/kimi-runtime/tools/comfyctl.py queue

compose exec kimi-agent \
  python /opt/kimi-runtime/tools/comfyctl.py run \
  --wait \
  --timeout 120 \
  /opt/kimi-runtime/tools/comfy-smoke-api.json

compose exec kimi-agent sh -c \
  'test -z "${LITELLM_API_KEY:-}${NRP_API_KEY:-}"' \
  || {
    echo "A model credential is visible inside kimi-agent." >&2
    exit 1
  }

compose exec kimi-agent \
  python -c \
  "import os; from pathlib import Path; paths=[Path('/workspace/comfyui/custom_nodes'), Path('/workspace/comfyui/user/default/workflows')]; assert all(path.is_dir() and os.access(path, os.W_OK) for path in paths)" \
  || {
    echo "Kimi cannot write the ComfyUI development directories." >&2
    exit 1
  }

if [[ "${backend}" == "cuda" ]]; then
  compose exec comfyui python -c \
    'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name())'
else
  .local/comfy-macos/current/venv/bin/python -c \
    'import torch; assert torch.backends.mps.is_available()'
fi

echo "Core acceptance checks passed. Validate enabled MCP servers with /mcp in a fresh Kimi session."
