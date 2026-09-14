#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd -P)
exec python3 "${root}/tools/safe_workspace_init.py" \
  "${1:?usage: workspace-init.sh /path/to/workspace}"
