#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose \
  --env-file .env \
  run \
  --rm \
  kimi-agent \
  bash
