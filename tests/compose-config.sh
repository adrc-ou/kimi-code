#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd -P)
# Ignore operator configuration, even when this check runs in a configured clone.
export COMPOSE_ENV_FILES=/dev/null
export COMPOSE_DISABLE_ENV_FILE=1
export LITELLM_UPSTREAM_ORIGIN=https://example.invalid
export LITELLM_MODEL_ID=test-model
export SEARXNG_SECRET=compose-fixture-only
fixture=$(mktemp -d "${TMPDIR:-/tmp}/kimi compose.XXXXXX")
trap 'find "${fixture}" -depth -delete' EXIT
mkdir -p "${fixture}/workspace/comfyui/"{models,custom_nodes,input,output,temp,user} "${fixture}/empty"
touch "${fixture}/config.toml" "${fixture}/SYSTEM.md" "${fixture}/secret" "${fixture}/cert.crt"
chmod 600 "${fixture}/"{config.toml,SYSTEM.md,secret,cert.crt}
export WORKSPACE_PATH="${fixture}/workspace"
export COMFYUI_MODELS_PATH="${fixture}/workspace/comfyui/models"
export COMFYUI_CUSTOM_NODES_PATH="${fixture}/workspace/comfyui/custom_nodes"
export COMFYUI_INPUT_PATH="${fixture}/workspace/comfyui/input"
export COMFYUI_OUTPUT_PATH="${fixture}/workspace/comfyui/output"
export COMFYUI_TEMP_PATH="${fixture}/workspace/comfyui/temp"
export COMFYUI_USER_PATH="${fixture}/workspace/comfyui/user"
export HARNESS_IMAGE_SUFFIX=test
export KIMI_RENDERED_CONFIG="${fixture}/config.toml"
export KIMI_EMPTY_SYSTEM="${fixture}/SYSTEM.md"
export KIMI_EMPTY_USER_AGENTS="${fixture}/empty"
export KIMI_EMPTY_USER_SKILLS="${fixture}/empty"
export KIMI_EMPTY_USER_PLUGINS="${fixture}/empty"
export NRP_API_KEY_FILE="${fixture}/secret"
export NRP_INTERNAL_TOKEN_FILE="${fixture}/secret"
export NRP_CACHE_SALT_FILE="${fixture}/secret"
export SEARCH_ADAPTER_TOKEN=test-search-token-with-at-least-32-characters
export COMFYUI_TOKEN=test-bridge-token-with-at-least-32-characters
export COMFYUI_BRIDGE_CERT="${fixture}/cert.crt"
export KIMI_CODE_VERSION=0.42.0
export KIMI_CODE_ASSET_URL=https://example.invalid/kimi.tar.gz
export KIMI_CODE_ASSET_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export COMFYUI_VERSION=v0.35.0
export COMFYUI_COMMIT=40c4fcdf513a4523e39d54a9d391908af8df8171
export LOCAL_UID=1000 LOCAL_GID=1000
set -a
# shellcheck disable=SC1091
source "${root}/comfy/backend.env"
set +a
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${root}/compose.comfy.cuda.yaml" config --quiet
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${root}/compose.comfy.mps.yaml" config --quiet
cp "${root}/compose.limits.yaml.example" "${fixture}/limits.yaml"
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${root}/compose.comfy.cuda.yaml" -f "${fixture}/limits.yaml" config --quiet
mkdir -p "${fixture}/approved-skills"
printf '%s\n' \
  'services:' \
  '  kimi-agent:' \
  '    volumes:' \
  '      - type: bind' \
  "        source: ${fixture}/approved-skills" \
  '        target: /workspace/.kimi-code/skills' \
  '        read_only: true' \
  '        bind:' \
  '          create_host_path: false' >"${fixture}/approved.yaml"
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${root}/compose.comfy.cuda.yaml" -f "${fixture}/approved.yaml" config --quiet
