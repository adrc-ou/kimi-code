#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose \
  --env-file .env \
  run \
  --rm \
  --no-deps \
  kimi-agent \
  bash /opt/kimi-runtime/tools/workspace-init.sh /workspace
