#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and configure it." >&2
  exit 1
fi
command -v python3 >/dev/null 2>&1 || {
  echo "Required command not found: python3" >&2
  exit 1
}

workspace_value=$(python3 scripts/read_env.py .env WORKSPACE_PATH)
if [[ -z "${workspace_value}" ]]; then
  echo "WORKSPACE_PATH must not be empty." >&2
  exit 1
fi
case "${workspace_value}" in
  /*) workspace_candidate=${workspace_value} ;;
  *) workspace_candidate="${root}/${workspace_value}" ;;
esac

mkdir -p -- "${workspace_candidate}"
workspace=$(cd "${workspace_candidate}" && pwd -P)
if [[ "${workspace}" == "/" \
      || "${workspace}" == "${root}" \
      || "${workspace}" == "${HOME}" ]]; then
  echo "Refusing unsafe WORKSPACE_PATH: ${workspace}" >&2
  exit 1
fi

bash tools/workspace-init.sh "${workspace}"
