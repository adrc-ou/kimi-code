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
# Same floor the launcher applies: below it `config --format json` omits an explicit
# create_host_path: false, so the hygiene gate below cannot read the option it enforces and
# refuses a confined launch. Ask for the upgrade here, where the cause is named, rather than
# leaving it as an unexplained bind violation two thirds of the way through this script.
# shellcheck disable=SC1091
source "${root}/tools/runtime.sh"
harness_require_compose_version || exit 1
export SEARXNG_SECRET=compose-fixture-only
fixture=$(mktemp -d "${TMPDIR:-/tmp}/kimi compose.XXXXXX")
test_project="kimi-cache-test-$(basename "${fixture}" | tr '[:upper:] .' '[:lower:]--')"
# Stand-in for the fragment tools/models.py resolve writes from the selected definitions: the
# launcher appends it, so credential secret names are never spelled in compose.yaml and these
# checks exercise a real mount rather than an imagined one.
compose_files=(-f "${root}/compose.yaml" -f "${root}/compose.search.yaml" -f "${fixture}/models.json")
runtime_test=false
cleanup() {
  local status=0
  if [[ "${runtime_test}" == true ]]; then
    # Release the initializer's pins before asking the daemon to destroy the volumes: the removal
    # runs as root, but unlinking an immutable file is EPERM for root too, so kimi-state only
    # becomes removable once a container still holding CAP_LINUX_IMMUTABLE has cleared them.
    # Best effort, because a teardown problem must never mask the check that actually failed.
    if [[ -f "${fixture}/state-test.json" ]]; then
      docker compose -p "${test_project}" -f "${fixture}/state-test.json" run --rm --no-deps \
        state-unpin-test || true
    fi
    docker compose -p "${test_project}" "${compose_files[@]}" down --volumes --remove-orphans ||
      status=$?
  fi
  # Unconditional: under errexit an EXIT trap aborted by a failing command never reached this line,
  # so a refused volume removal also leaked the whole fixture directory.
  find "${fixture}" -depth -delete
  # `return`, never `exit`: an EXIT trap that calls `exit` overwrites the body's status, which
  # would turn every failed runtime check green.
  return "${status}"
}
trap cleanup EXIT
mkdir -p "${fixture}/workspace" "${fixture}/empty" "${fixture}/assets/skills" \
  "${fixture}/assets/agents" "${fixture}/assets/tools" "${fixture}/credentials" \
  "${fixture}/prompt-log"
touch "${fixture}/config.toml" "${fixture}/SYSTEM.md" "${fixture}/AGENTS.md" "${fixture}/secret" \
  "${fixture}/cert.crt" \
  "${fixture}/assets/mcp.json" "${fixture}/model-policy.json" \
  "${fixture}/credentials/nrp__default"
chmod 600 "${fixture}/"{config.toml,SYSTEM.md,AGENTS.md,secret,cert.crt,model-policy.json}
chmod 600 "${fixture}/credentials/nrp__default"
export HARNESS_RUNTIME_DIR="${fixture}"
export HARNESS_TEST_FIXTURE="${fixture}"
export WORKSPACE_PATH="${fixture}/workspace"
export HARNESS_IMAGE_SUFFIX=test
export KIMI_RENDERED_CONFIG="${fixture}/config.toml"
export KIMI_RENDERED_AGENTS_MD="${fixture}/AGENTS.md"
export KIMI_SYSTEM_MD="${fixture}/SYSTEM.md"
export MODEL_PROXY_POLICY_FILE="${fixture}/model-policy.json"
export MODEL_PROXY_INTERNAL_TOKEN_FILE="${fixture}/secret"
export MODEL_PROXY_CACHE_SALT_FILE="${fixture}/secret"
export KIMI_SUBAGENT_CONCURRENCY=5
export KIMI_CODE_PERMISSION_MODE_REMINDER=true
export KIMI_BACKGROUND_TASK_SLOTS=8
export KIMI_BACKGROUND_BASH_TASK_TIMEOUT_S=0
export SEARCH_ADAPTER_TOKEN=test-search-token-with-at-least-32-characters
export KIMI_CODE_VERSION=0.42.0
export KIMI_CODE_ASSET_URL=https://example.invalid/kimi.tar.gz
export KIMI_CODE_ASSET_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export LOCAL_UID=1000 LOCAL_GID=1000
printf '%s\n' \
  'services:' \
  '  model-proxy:' \
  '    secrets:' \
  '      - proxy_internal_token' \
  '      - proxy_cache_salt' \
  '      - nrp__default' \
  'secrets:' \
  "  nrp__default:" \
  "    file: ${fixture}/credentials/nrp__default" >"${fixture}/models.json"
docker compose "${compose_files[@]}" config --quiet

# Approved project extensions legitimately bind the snapshot the launcher staged for them, so
# the hygiene assertions below have to cover that generated fragment as well as the checked-in
# files. The path mirrors what tools/approve_extensions.py actually emits.
mkdir -p "${fixture}/extension-snapshot/.kimi-code/skills"
printf '%s\n' \
  'services:' \
  '  kimi-agent:' \
  '    volumes:' \
  '      - type: bind' \
  "        source: ${fixture}/extension-snapshot/.kimi-code/skills" \
  '        target: /workspace/.kimi-code/skills' \
  '        read_only: true' \
  '        bind:' \
  '          create_host_path: false' >"${fixture}/approved.yaml"

# Mount, credential and port hygiene of the resolved configuration: the agent container must not
# be able to read harness or host layout out of its own mount table, a provider credential must
# reach model-proxy alone, and every published port must stay on loopback. Modules run the same
# assertions over their own overlays from their own compose-config.sh.
for fragment in "" "${fixture}/approved.yaml"; do
  files=("${compose_files[@]}")
  label="core configuration"
  [[ -z "${fragment}" ]] || { files+=(-f "${fragment}"); label="with approved extensions"; }
  docker compose "${files[@]}" config --format json |
    python3 "${root}/tools/compose_hygiene.py" \
      --workspace "${WORKSPACE_PATH}" --runtime-dir "${HARNESS_RUNTIME_DIR}" \
      --expect-secret nrp__default --label "${label}"
done
for check in "${root}"/modules/*/tests/compose-config.sh; do
  [[ ! -f "${check}" ]] || bash "${check}"
done
cp "${root}/compose.limits.yaml.example" "${fixture}/limits.yaml"
docker compose "${compose_files[@]}" -f "${fixture}/limits.yaml" config --quiet

if [[ "${1:-}" == --runtime ]]; then
  runtime_test=true
  compose=(docker compose -p "${test_project}" "${compose_files[@]}")
  # Every container started here needs the daemon, so unlike the checks above this one cannot be
  # reproduced by reading the rendered configuration. Each announces itself before it starts: the
  # script fails fast, so the last label on stdout names the check that died rather than leaving
  # an unexplained exit 1 at the end of a run nobody else can replay.
  runtime_check() {
    local label=$1
    shift
    printf 'runtime %s\n' "${label}"
    "$@"
  }
  # Both a fresh cache and one already owned by SearXNG must initialize.
  runtime_check "searxng cache initialises when empty" "${compose[@]}" run --rm --no-deps searxng-init
  # shellcheck disable=SC2016
  runtime_check "searxng cache is owned and writable by SearXNG" "${compose[@]}" run --rm --no-deps --entrypoint /bin/sh searxng -ec \
    'stat -c "%u:%g:%a" /var/cache/searxng; test "$(stat -c "%u:%g:%a" /var/cache/searxng)" = 977:977:700; echo preserved > /var/cache/searxng/test-marker'
  runtime_check "searxng cache initialises when already owned" "${compose[@]}" run --rm --no-deps searxng-init
  # shellcheck disable=SC2016
  runtime_check "searxng cache keeps owner and contents across restarts" "${compose[@]}" run --rm --no-deps --entrypoint /bin/sh searxng -ec \
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
    runtime_check "agent cache tmpfs belongs to ${identity}" \
      docker compose -p "${test_project}" -f "${fixture}/cache-test.json" run --rm cache-test
    # Exercise the real initializer on fresh volumes, then migrate 1000:1000
    # state to 1234:2345. Use the cached Python image without building Kimi.
    # shellcheck disable=SC2016
    "${compose[@]}" config --format json | python3 -c '
import json, os, sys
services = json.load(sys.stdin)["services"]
initializer = services["agent-state-init"]
initializer["image"] = services["searxng"]["image"]
probe = {
    "image": initializer["image"],
    "user": os.environ["LOCAL_UID"] + ":" + os.environ["LOCAL_GID"],
    "read_only": True, "network_mode": "none", "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "volumes": [v for v in initializer["volumes"] if v["type"] == "volume"],
    "entrypoint": ["python", "-c"],
    "command": ["import os; from pathlib import Path; "
                "roots=[Path(\"/state/kimi\"),Path(\"/state/serena\")]; "
                "assert all(p.stat().st_uid == os.getuid() and p.stat().st_mode & 511 == 448 for p in roots); "
                "p=roots[0]/\"server/instances\"; p.mkdir(parents=True,exist_ok=True); "
                "marker=p/\"preserved\"; "
                "assert not marker.exists() or marker.read_text() == \"keep\"; "
                "marker.write_text(\"keep\"); marker.chmod(384); "
                "link=roots[0]/\"outside-link\"; "
                "link.is_symlink() or link.symlink_to(\"/etc/passwd\"); "
                "(roots[1]/\"writable\").write_text(\"ok\")"],
}
# The initializer pins its managed files, and unlinking an immutable file is EPERM even for root,
# so nothing can remove the kimi-state volume until a container that still holds CAP_LINUX_IMMUTABLE
# has cleared those pins. Derived from the initializer service itself, and calling its own
# clear_immutable, so the mounts and capabilities that release the pins cannot drift away from the
# ones that set them.
unpin = "\n".join([
    "import importlib.util, os",
    "from pathlib import Path",
    "spec = importlib.util.spec_from_file_location(\"kimi_staging\", \"/initialize-agent-state.py\")",
    "module = importlib.util.module_from_spec(spec)",
    "spec.loader.exec_module(module)",
    "targets = [Path(base) / name",
    "           for root in (\"/state/kimi\", \"/state/serena\", \"/state/assets\", \"/state/managed\")",
    "           for base, dirs, files in os.walk(root)",
    "           for name in dirs + files]",
    "for path in targets:",
    "    module.clear_immutable(path)",
    "print(\"cleared immutable pins on \" + str(len(targets)) + \" state volume entries\")",
])
unpin_service = dict(initializer, entrypoint=["python", "-c"], command=[unpin])
print(json.dumps({"services": {"state-init-test": initializer, "state-write-test": probe,
                               "state-unpin-test": unpin_service},
                  "volumes": {"kimi-state": {}, "serena-state": {}, "kimi-assets": {},
                              "kimi_user_agents": {}, "kimi_user_skills": {},
                              "kimi_user_plugins": {}}}))
' >"${fixture}/state-test.json"
    # Two passes, because the second one re-stages over the state the first left -- which is what
    # restarting an existing workspace does, and the only way to catch a staging step that is not
    # actually idempotent against its own output.
    for pass in 1 2; do
      runtime_check "agent state initialises for ${identity} (pass ${pass})" \
        docker compose -p "${test_project}" -f "${fixture}/state-test.json" run --rm state-init-test
      runtime_check "agent writes initialised state for ${identity} (pass ${pass})" \
        docker compose -p "${test_project}" -f "${fixture}/state-test.json" run --rm state-write-test
    done
  done
fi
