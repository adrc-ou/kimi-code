#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd -P)
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --runtime ) ]]; then
  echo "usage: tests/compose-config.sh [--runtime]" >&2
  exit 2
fi
# Ignore operator configuration, even when this check runs in a configured clone.
export COMPOSE_ENV_FILES=/dev/null
export COMPOSE_DISABLE_ENV_FILE=1
export LITELLM_UPSTREAM_ORIGIN=https://example.invalid
export LITELLM_MODEL_ID=test-model
export SEARXNG_SECRET=compose-fixture-only
fixture=$(mktemp -d "${TMPDIR:-/tmp}/kimi compose.XXXXXX")
test_project="kimi-cache-test-$(basename "${fixture}" | tr '[:upper:] .' '[:lower:]--')"
runtime_test=false
cleanup() {
  if [[ "${runtime_test}" == true ]]; then
    docker compose -p "${test_project}" -f "${root}/compose.yaml" \
      -f "${root}/compose.search.yaml" down --volumes --remove-orphans
  fi
  find "${fixture}" -depth -delete
}
trap cleanup EXIT
mkdir -p "${fixture}/workspace" "${fixture}/empty"
touch "${fixture}/config.toml" "${fixture}/SYSTEM.md" "${fixture}/secret" "${fixture}/cert.crt"
chmod 600 "${fixture}/"{config.toml,SYSTEM.md,secret,cert.crt}
export HARNESS_RUNTIME_DIR="${fixture}"
export HARNESS_TEST_FIXTURE="${fixture}"
export WORKSPACE_PATH="${fixture}/workspace"
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
export KIMI_CODE_VERSION=0.42.0
export KIMI_CODE_ASSET_URL=https://example.invalid/kimi.tar.gz
export KIMI_CODE_ASSET_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export LOCAL_UID=1000 LOCAL_GID=1000
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" config --quiet
for check in "${root}"/modules/*/tests/compose-config.sh; do
  [[ ! -f "${check}" ]] || bash "${check}"
done
cp "${root}/compose.limits.yaml.example" "${fixture}/limits.yaml"
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${fixture}/limits.yaml" config --quiet
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
docker compose -f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${fixture}/approved.yaml" config --quiet

if [[ "${1:-}" == --runtime ]]; then
  runtime_test=true
  compose=(docker compose -p "${test_project}" -f "${root}/compose.yaml" -f "${root}/compose.search.yaml")
  # Both a fresh cache and one already owned by SearXNG must initialize.
  "${compose[@]}" run --rm --no-deps searxng-init
  # shellcheck disable=SC2016
  "${compose[@]}" run --rm --no-deps --entrypoint /bin/sh searxng -ec \
    'stat -c "%u:%g:%a" /var/cache/searxng; test "$(stat -c "%u:%g:%a" /var/cache/searxng)" = 977:977:700; echo preserved > /var/cache/searxng/test-marker'
  "${compose[@]}" run --rm --no-deps searxng-init
  # shellcheck disable=SC2016
  "${compose[@]}" run --rm --no-deps --entrypoint /bin/sh searxng -ec \
    'test "$(stat -c "%u:%g:%a" /var/cache/searxng)" = 977:977:700; test "$(cat /var/cache/searxng/test-marker)" = preserved'

  # Exercise the actual agent tmpfs configuration without building the agent
  # or mounting operator configuration. Cover default and custom host identities.
  for identity in 1000:1000 1234:2345; do
    export LOCAL_UID=${identity%:*} LOCAL_GID=${identity#*:}
    # shellcheck disable=SC2016
    "${compose[@]}" config --format json | python3 -c '
import json, os, sys
services = json.load(sys.stdin)["services"]
print(json.dumps({"services": {"cache-test": {
    "image": services["searxng"]["image"],
    "user": os.environ["LOCAL_UID"] + ":" + os.environ["LOCAL_GID"],
    "read_only": True,
    "network_mode": "none",
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "tmpfs": services["kimi-agent"]["tmpfs"],
    "entrypoint": ["/bin/sh", "-ec"],
    "command": ["test $(stat -c %u:%g:%a /home/agent/.cache) = $(id -u):$(id -g):700; "
                "mkdir -p /home/agent/.cache/kimi-code/web/test/dist-web/assets; "
                "echo ok > /home/agent/.cache/kimi-code/web/test/dist-web/assets/test"],
}}}))
' >"${fixture}/cache-test.json"
    docker compose -p "${test_project}" -f "${fixture}/cache-test.json" run --rm cache-test
  done
fi
