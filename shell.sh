#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"

case "$(uname -s):$(uname -m)" in
  Darwin:arm64) platform_key=darwin-arm64 ;;
  Linux:x86_64)
    if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
      platform_key=wsl2-x86_64
    else
      echo "Unsupported host." >&2
      exit 1
    fi
    ;;
  *) echo "Unsupported host." >&2; exit 1 ;;
esac

state_file=".local/state/${platform_key}.env"
if [[ ! -f "${state_file}" ]]; then
  echo "No installed runtime state. Run ./start.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090,SC1091
set -a
source comfy/backend.env
source "${state_file}"
set +a

workspace_value=$(python3 scripts/read_env.py .env WORKSPACE_PATH)
case "${workspace_value}" in
  /*) WORKSPACE_PATH=${workspace_value} ;;
  *) WORKSPACE_PATH="${root}/${workspace_value}" ;;
esac
export WORKSPACE_PATH
if [[ "${platform_key}" == darwin-arm64 ]]; then
  export LOCAL_UID=1000
  export LOCAL_GID=1000
else
  export LOCAL_UID=${LOCAL_UID:-$(id -u)}
  export LOCAL_GID=${LOCAL_GID:-$(id -g)}
fi

compose_files=(-f compose.yaml -f compose.search.yaml)
if [[ "${platform_key}" == wsl2-x86_64 ]]; then
  compose_files+=(-f compose.comfy.cuda.yaml)
fi

docker compose \
  --env-file .env \
  "${compose_files[@]}" \
  exec \
  kimi-agent \
  bash
