#!/usr/bin/env bash
# Shared host orchestration helpers. This file must be sourced.

harness_die() {
  echo "$*" >&2
  return 1
}

harness_traps() {
  # Report the location, never the command: commands can contain credentials.
  set -E
  trap 'printf "Harness failed at %s:%s (exit %s).\n" "${BASH_SOURCE[0]}" "${LINENO}" "$?" >&2' ERR
  trap harness_unlock EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

harness_platform() {
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64) HARNESS_PLATFORM=darwin-arm64; HARNESS_KIMI_ASSET=kimi-code-linux-arm64.tar.gz ;;
    Darwin:x86_64) HARNESS_PLATFORM=darwin-x86_64; HARNESS_KIMI_ASSET=kimi-code-linux-x64.tar.gz ;;
    Linux:x86_64) HARNESS_PLATFORM=linux-x86_64; HARNESS_KIMI_ASSET=kimi-code-linux-x64.tar.gz
      if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then HARNESS_PLATFORM=wsl2-x86_64; fi ;;
    Linux:aarch64|Linux:arm64) HARNESS_PLATFORM=linux-arm64; HARNESS_KIMI_ASSET=kimi-code-linux-arm64.tar.gz ;;
    *) harness_die "Unsupported host architecture; use macOS or Linux/WSL2 on arm64 or x86-64." || return ;;
  esac
  HARNESS_PLATFORM_LABEL=${HARNESS_PLATFORM}
  export HARNESS_PLATFORM HARNESS_PLATFORM_LABEL HARNESS_KIMI_ASSET
}

# The directory this checkout works in, from whatever was asked of it.
#
# `./start.sh` puts a screen in front of the operator and exports the answer; every other entry point
# (`./shell.sh`, `./extensions.sh`, `./prompts.sh`) and an unattended launch have no screen to ask
# with, and take the directory the last interactive launch chose. That remembered list is the only
# place the answer can live outside an instance, because the instance identity digests the workspace:
# a file under `.local/runtime/<instance>/` would be orphaned by the very choice it recorded.
#
# The checks are the launcher's own and run on the answer however it arrived. The registry
# canonicalises what it stores, but `WORKSPACE_PATH` in the environment is a hand-written thing, and
# a hand-written `/` is a workspace that mounts the host.
harness_resolve_workspace() {
  local value=${WORKSPACE_PATH:-}
  if [[ -z "${value}" ]]; then
    value=$(python3 "${HARNESS_ROOT}/tools/workspace_registry.py" --root "${HARNESS_ROOT}" newest) ||
      value=""
  fi
  [[ -n "${value}" ]] ||
    harness_die "No workspace has been chosen for this checkout. Run ./start.sh to pick one." ||
    return
  case "${value}" in
    /*) local candidate=${value} ;;
    *) local candidate="${HARNESS_ROOT}/${value}" ;;
  esac
  mkdir -p -- "${candidate}"
  HARNESS_WORKSPACE=$(cd "${candidate}" && pwd -P)
  [[ "${HARNESS_WORKSPACE}" != "/" ]] ||
    harness_die "Refusing unsafe workspace: the filesystem root" || return
  # A workspace that *holds* the checkout also holds its .env and every generated key under
  # .local/runtime, and one that holds the account's home holds everything the account keeps. The
  # trailing slash is what makes this containment rather than a string prefix: /work/proj must not
  # be refused for sitting inside a workspace at /work/thing that merely shares three characters.
  for protected in "${HARNESS_ROOT}" "${HOME}"; do
    if [[ "${protected}/" == "${HARNESS_WORKSPACE}/"* ]]; then
      harness_die "Refusing unsafe workspace: ${HARNESS_WORKSPACE} holds ${protected}" || return
    fi
  done
  [[ "${HARNESS_WORKSPACE}" != *$'\n'* ]] || harness_die "The workspace path contains a newline" || return
  export WORKSPACE_PATH=${HARNESS_WORKSPACE}
  export HARNESS_WORKSPACE
}

# Ask the operator which workspace this launch should use, and hand the answer to the rest of the
# launcher through the environment. Only `./start.sh` calls this, and it calls it before
# `harness_init`, because the instance identity is built from the answer: nothing that needs a runtime
# directory, an image tag, or a Compose project can have been worked out yet.
#
# The screen prints the path on standard output and nothing else, which is what makes it capturable.
# Its status is this function's, so a Ctrl-C inside it still reaches the caller's trap as 130.
harness_choose_workspace() {
  local chosen
  chosen=$(python3 "${HARNESS_ROOT}/tools/workspace_choice.py" --root "${HARNESS_ROOT}") || return
  [[ -n "${chosen}" ]] || harness_die "The workspace choice came back empty." || return
  export WORKSPACE_PATH=${chosen}
}

harness_resolve_bootstrap_env() {
  [[ -f "${HARNESS_ROOT}/.env" ]] || harness_die "Missing .env. Copy .env.example to .env and configure it." || return
  command -v docker >/dev/null 2>&1 || harness_die "Required command not found: docker" || return
  docker compose version >/dev/null
  harness_resolve_workspace || return
  HARNESS_BOOTSTRAP_ENV=$(docker compose --env-file "${HARNESS_ROOT}/.env" \
    -f "${HARNESS_ROOT}/compose.bootstrap.yaml" config --environment)
  local variable value
  for variable in COMPOSE_PROJECT_NAME LOCAL_UID LOCAL_GID; do
    value=$(printf '%s\n' "${HARNESS_BOOTSTRAP_ENV}" | \
      python3 "${HARNESS_ROOT}/scripts/read_env.py" /dev/stdin "${variable}" 2>/dev/null || true)
    if [[ -n "${value}" ]]; then
      export "${variable}=${value}"
    fi
  done
}

harness_instance() {
  local digest
  digest=$(printf '%s\0%s\0%s' "${HARNESS_ROOT}" "${HARNESS_WORKSPACE}" "${HARNESS_PLATFORM}" | shasum -a 256 | awk '{print $1}')
  HARNESS_INSTANCE_ID=${digest:0:16}
  HARNESS_RUNTIME_DIR="${HARNESS_ROOT}/.local/runtime/${HARNESS_INSTANCE_ID}"
  HARNESS_COMPOSE_DIR="${HARNESS_RUNTIME_DIR}/compose"
  HARNESS_STATE_FILE="${HARNESS_RUNTIME_DIR}/state.env"
  HARNESS_SESSION_FILE="${HARNESS_RUNTIME_DIR}/session.env"
  HARNESS_IMAGE_SUFFIX=${HARNESS_INSTANCE_ID}
  mkdir -p "${HARNESS_COMPOSE_DIR}" "${HARNESS_RUNTIME_DIR}/prompt-log"
  chmod 700 "${HARNESS_RUNTIME_DIR}" "${HARNESS_COMPOSE_DIR}"
  # The proxy is the only writer here and it runs unprivileged, so this is the one directory
  # under the instance directory that other accounts may write to. Its parent stays 0700, so
  # reaching it already requires being the operator or root.
  chmod 777 "${HARNESS_RUNTIME_DIR}/prompt-log"
  : "${COMPOSE_PROJECT_NAME:=kimi_code_${HARNESS_INSTANCE_ID}}"
  [[ "${COMPOSE_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || harness_die "Invalid COMPOSE_PROJECT_NAME" || return
  export HARNESS_INSTANCE_ID HARNESS_RUNTIME_DIR HARNESS_COMPOSE_DIR
  export HARNESS_STATE_FILE HARNESS_SESSION_FILE HARNESS_IMAGE_SUFFIX COMPOSE_PROJECT_NAME
}

# Delete everything the instance directory may be holding that could carry a secret.
#
# A launch that is killed outright runs no cleanup at all: no EXIT trap, no unlock, nothing. So the
# next launch has to assume the directory is dirty, and it has to say so before it renders anything,
# or a provider key survives every reboot until someone happens to stop the stack politely.
#
# Three families, by how they got there:
#
#   credentials/       today's layout - one file per credential the selection uses (tools/models.py)
#   <a>__<b>           the same secret names written flat. Two underscores are reserved for exactly
#                      this by tools/definitions.py, which refuses them in a provider id or a
#                      credential id, and no other artifact in this directory has one, so the shape
#                      alone identifies a key without naming a provider.
#   the ledger below   flat names older revisions wrote at the instance root. No current code
#                      produces any of them, which is precisely why no current cleanup knows them.
#
# `bootstrap.?*` is the resolved bootstrap environment, which is a copy of .env with every default
# filled in, and is the one file here that can hold a key the operator persisted rather than typed.
#
# Never fatal: residue that will not go is worth a warning, not a launch that cannot start.
HARNESS_SECRET_RESIDUE=(nrp-api-key bridge-token)

harness_sweep_secrets() {
  local directory=${1:-${HARNESS_RUNTIME_DIR:-}}
  local name
  [[ -n "${directory}" && -d "${directory}" ]] || return 0
  # This function deletes by name without looking; the instance directory is the only thing it is
  # ever allowed to point at, and a path that does not say so is a bug worth shouting about.
  if [[ "${directory}" != *"/.local/runtime/"* ]]; then
    echo "Refusing to sweep ${directory}: not a harness instance directory." >&2
    return 0
  fi
  for name in ${HARNESS_SECRET_RESIDUE[@]+"${HARNESS_SECRET_RESIDUE[@]}"}; do
    if [[ -e "${directory}/${name}" || -L "${directory}/${name}" ]]; then
      find "${directory}/${name}" -delete 2>/dev/null ||
        echo "Could not remove ${name} from the instance directory." >&2
    fi
  done
  find "${directory}" -maxdepth 1 -type f \( -name '*__*' -o -name 'bootstrap.?*' \) \
    -delete 2>/dev/null ||
    echo "Could not sweep secret-shaped residue from ${directory}." >&2
  if [[ -d "${directory}/credentials" ]]; then
    # The directory itself stays; materialise_credentials() owns its mode and recreates the files.
    find "${directory}/credentials" -mindepth 1 -delete 2>/dev/null ||
      echo "Could not empty ${directory}/credentials." >&2
  fi
  return 0
}

# Sweep every instance directory in this checkout *except* the one we are launching.
#
# The instance id digests the checkout path, the workspace path and the platform, so moving the
# repository, retargeting its workspace, or switching platform orphans the previous instance
# directory completely: nothing in a later launch ever names it, and a provider key inside it
# outlives every cleanup that knows about. Orphans are also what a checkout that has been copied
# around accumulates. Every sibling whose lock no live process holds gets the same sweep the live
# directory gets, and one that is still held is left exactly as it is, so a second instance
# sharing this checkout keeps its own secrets.
#
# Heldness comes from the lock itself rather than the pid written in it: a pid outliving its
# process is the ordinary state of a file left by a kill -9, and after a reboot some unrelated
# process owns that number, which would guard the leftover key forever. The kernel drops an flock
# when its holder dies by any means, so trying the lock non-blockingly answers the real question.
# Anything the probe cannot settle counts as held — skipping a stale directory costs one leftover,
# sweeping a live one costs the operator their running session.
harness_sweep_stale_instances() {
  local root=${1:-}
  local current=${HARNESS_RUNTIME_DIR:-}
  [[ -n "${root}" && -d "${root}" ]] || return 0
  local directory
  for directory in "${root}"/*/; do
    [[ -d "${directory}" ]] || continue
    directory=${directory%/}
    [[ "${directory}" == "${current}" ]] && continue
    python3 "${HARNESS_ROOT}/tools/instance_lock.py" --free "${directory}/launcher.lock" ||
      continue
    harness_sweep_secrets "${directory}"
  done
  return 0
}

# Retire the scratch directory this harness used to keep inside the project.
#
# The workspace belongs to whatever project is checked out there, so harness leftovers get removed
# at both ends of a launch rather than accumulating between them. Removal is deliberately not
# unconditional: a project that tracks its own directory of that name owns it, and Git is the
# arbiter. The function it calls also refuses a symlink and a directory it does not own.
#
# Silent by construction. This runs from the exit trap, where a complaint about a cleanup would
# bury the reason the launch is ending.
harness_retire_workspace_state() {
  local workspace=${1:-${HARNESS_WORKSPACE:-}}
  [[ -n "${workspace}" && -d "${workspace}" ]] || return 0
  python3 "${HARNESS_ROOT}/tools/safe_workspace_init.py" --retire-only "${workspace}" \
    >/dev/null 2>&1 || true
  return 0
}

harness_lock() {
  HARNESS_LOCK_PATH="${HARNESS_RUNTIME_DIR}/launcher.lock"
  # FD 9 is reserved for the launcher. Python's flock works on both macOS and
  # Linux, including Apple's Bash 3.2, without racy PID-directory reclamation.
  # Do not truncate the current owner's metadata before acquiring the lock.
  exec 9>>"${HARNESS_LOCK_PATH}"
  if ! python3 - "${HARNESS_LOCK_PATH}" "$$" <<'PY'
import fcntl
import os
import sys
from datetime import datetime, timezone

try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(f"Another launcher owns {sys.argv[1]}") from None
os.ftruncate(9, 0)
os.write(9, f"pid={sys.argv[2]}\nstarted={datetime.now(timezone.utc).isoformat()}\n".encode())
PY
  then
    exec 9>&-
    return 1
  fi
  HARNESS_LOCK_HELD=true
}

harness_unlock() {
  if [[ "${HARNESS_LOCK_HELD:-false}" == true ]]; then
    python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_UN)' || true
    exec 9>&-
    HARNESS_LOCK_HELD=false
  fi
  if [[ -n "${HARNESS_RESOLVED_BOOTSTRAP:-}" ]]; then
    find "${HARNESS_RESOLVED_BOOTSTRAP}" -delete 2>/dev/null || true
  fi
}

harness_compose_files() {
  HARNESS_COMPOSE_FILES=(-f "${HARNESS_ROOT}/compose.yaml" -f "${HARNESS_ROOT}/compose.search.yaml")
  harness_modules compose
  if [[ -f "${HARNESS_COMPOSE_DIR}/module-environment.json" ]]; then
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_COMPOSE_DIR}/module-environment.json")
  fi
  if [[ -f "${HARNESS_COMPOSE_DIR}/models.json" ]]; then
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_COMPOSE_DIR}/models.json")
  fi
  if [[ -f "${HARNESS_ROOT}/compose.limits.yaml" ]]; then
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_ROOT}/compose.limits.yaml")
  fi
  if [[ -f "${HARNESS_COMPOSE_DIR}/approved-extensions.yaml" ]]; then
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_COMPOSE_DIR}/approved-extensions.yaml")
  fi
}

harness_compose() {
  docker compose --env-file "${HARNESS_ROOT}/.env" "${HARNESS_COMPOSE_FILES[@]}" "$@"
}

# The digest of the agent image this instance built, or empty when there is no such image yet.
# Kimi's cached prompt literals are keyed on this exact string, so every producer has to call this
# one function: if the launcher and ./prompts.sh derived it differently, a hand-run refresh would
# write a file that no launch ever reads back.
harness_prompt_image_id() {
  docker image inspect "adrc-kimi-agent:${HARNESS_IMAGE_SUFFIX}" --format '{{.Id}}' 2>/dev/null || true
}

# The floor below is the minimum Docker Compose whose `config --format json` output the
# confinement gate can read honestly. Releases before v5.0.2 serialise an explicit
# `create_host_path: false` as a missing key (the field was a Go bool carrying `omitempty`),
# so compose_hygiene.py cannot see the option it exists to enforce and refuses a confined
# launch as a bind violation. v5.0.0 renders the value; v5.0.2 also re-materialises the
# default on load, which is what makes an empty `bind` section mean `true` unambiguously.
# Fail here, naming the version found, rather than letting the gate report someone else's
# config for a Compose that cannot state this one.
HARNESS_MIN_COMPOSE_VERSION=5.0.2

harness_require_compose_version() {
  local raw version
  raw=$(docker compose version --format json 2>/dev/null) || raw=""
  [[ -n "${raw}" ]] || raw=$(docker compose version 2>/dev/null || true)
  version=$(printf '%s' "${raw}" | python3 -c '
import re
import sys

match = re.search(r"v?(\d+\.\d+\.\d+)", sys.stdin.read())
print(match.group(1) if match else "")
') || version=""
  if [[ -z "${version}" ]]; then
    harness_die "Cannot determine the Docker Compose version; v${HARNESS_MIN_COMPOSE_VERSION} or newer is required." || return
  fi
  if ! python3 - "${version}" "${HARNESS_MIN_COMPOSE_VERSION}" <<'PY'
import sys


def parts(value):
    return [int(part) for part in value.split("-")[0].split(".")]


raise SystemExit(0 if parts(sys.argv[1]) >= parts(sys.argv[2]) else 1)
PY
  then
    harness_die "Docker Compose v${version} is too old: it omits an explicit create_host_path: false from the resolved configuration, so launch hygiene cannot read it. Upgrade Docker Compose to v${HARNESS_MIN_COMPOSE_VERSION} or newer." || return
  fi
}

harness_validate_compose() {
  harness_require_compose_version || return
  harness_compose config --format json >"${HARNESS_COMPOSE_DIR}/resolved.json"
  chmod 600 "${HARNESS_COMPOSE_DIR}/resolved.json"
  # Check the launch as resolved rather than the files in the checkout: this is the only
  # moment when every module overlay and approved-extension bind is known.
  python3 "${HARNESS_ROOT}/tools/compose_hygiene.py" \
    --workspace "${WORKSPACE_PATH}" --runtime-dir "${HARNESS_RUNTIME_DIR}" \
    --label launch <"${HARNESS_COMPOSE_DIR}/resolved.json"
}

# The host tools every entry point shells out for. Checked by both initialisation paths, so a
# read-only command like ./prompts.sh --live notices a missing docker instead of failing
# somewhere far less legible inside compose.
harness_require_commands() {
  local command
  for command in docker git python3 shasum; do
    command -v "${command}" >/dev/null 2>&1 ||
      harness_die "Required command not found: ${command}" || return
  done
}

harness_init() {
  HARNESS_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  export HARNESS_ROOT
  umask 077
  harness_require_commands || return
  harness_platform
  harness_resolve_bootstrap_env
  harness_instance
  # Behind the lock, so this only ever clears residue: a launcher that is actually running would
  # have refused us the lock and never got here.
  harness_lock
  harness_sweep_secrets
  # The lock above only proves nothing is launching *this* instance id, so the siblings need their
  # own liveness test before the same sweep reaches them.
  harness_sweep_stale_instances "${HARNESS_RUNTIME_DIR%/*}"
  HARNESS_RESOLVED_BOOTSTRAP=$(mktemp "${HARNESS_RUNTIME_DIR}/bootstrap.XXXXXX")
  printf '%s\n' "${HARNESS_BOOTSTRAP_ENV}" >"${HARNESS_RESOLVED_BOOTSTRAP}"
  unset HARNESS_BOOTSTRAP_ENV
  export HARNESS_RESOLVED_BOOTSTRAP
}

harness_init_readonly() {
  HARNESS_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  export HARNESS_ROOT
  umask 077
  harness_require_commands || return
  harness_platform
  harness_resolve_bootstrap_env
  harness_instance
  unset HARNESS_BOOTSTRAP_ENV
}

# Migration notices for .env keys that stopped meaning something: model facts moved into ./models
# and provider rules into ./providers, and the session context is now chosen on the launch panel
# rather than by a variable. Setting a key from the first group has no
# effect: the equivalent numbers are derived from the selected definitions. A key from the
# second group still works but answers to a MODEL_PROXY_* name. Printed, never fatal: an
# untidy .env must not stop a workspace from starting.
harness_retired_env=(
  KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE
  LITELLM_API_KEY
  LITELLM_UPSTREAM_ORIGIN
  LITELLM_MODEL_ID
  NRP_MODEL_CONTEXT
  NRP_FAIR_USE_PERCENT
  NRP_PARALLEL_CONTEXT_BUDGET
  NRP_OUTPUT_TOKENS_PER_MINUTE
  NRP_OUTPUT_RATE_HEADROOM_PERCENT
  NRP_MODEL_MAX_CONCURRENCY
  NRP_PRIMARY_MAX_OUTPUT_TOKENS
  NRP_LONG_MAX_OUTPUT_TOKENS
  NRP_SUBAGENT_MAX_OUTPUT_TOKENS
  KIMI_SUBAGENT_CONCURRENCY
)
harness_renamed_env=(
  NRP_UPSTREAM_SOCK_READ_TIMEOUT=MODEL_PROXY_SOCK_READ_TIMEOUT
  NRP_MAX_REQUEST_SECONDS=MODEL_PROXY_MAX_REQUEST_SECONDS
  NRP_INPUT_GUARD_PERCENT=MODEL_PROXY_INPUT_GUARD_PERCENT
  NRP_MEDIA_TOKEN_ESTIMATE=MODEL_PROXY_MEDIA_TOKEN_ESTIMATE
  NRP_REQUEST_USAGE=MODEL_PROXY_REQUEST_USAGE
  NRP_MAX_REQUEST_BYTES=MODEL_PROXY_MAX_REQUEST_BYTES
  NRP_MAX_RESPONSE_BYTES=MODEL_PROXY_MAX_RESPONSE_BYTES
  NRP_MAX_ERROR_BYTES=MODEL_PROXY_MAX_ERROR_BYTES
  NRP_MAX_QUEUED=MODEL_PROXY_MAX_QUEUED
)

harness_env_declares() {
  local file=$1 key=$2
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "${file}"
}

harness_warn_env_migration() {
  local file=${HARNESS_ROOT}/.env entry key retired=() renamed=()
  [[ -f "${file}" ]] || return 0
  for key in "${harness_retired_env[@]}"; do
    if harness_env_declares "${file}" "${key}"; then
      retired+=("${key}")
    fi
  done
  for entry in "${harness_renamed_env[@]}"; do
    key=${entry%%=*}
    if harness_env_declares "${file}" "${key}"; then
      renamed+=("${entry}")
    fi
  done
  if [[ ${#retired[@]} -gt 0 ]]; then
    echo ".env keys now derived from ./models and ./providers - delete them:" >&2
    for key in ${retired[@]+"${retired[@]}"}; do
      echo "  ${key}" >&2
    done
  fi
  if [[ ${#renamed[@]} -gt 0 ]]; then
    echo ".env keys renamed with the provider-neutral proxy - use the new name:" >&2
    for entry in ${renamed[@]+"${renamed[@]}"}; do
      echo "  ${entry%%=*} -> ${entry#*=}" >&2
    done
  fi
}

# Hooks execute only operator-owned code installed in this harness, never workspace extensions.
#
# The identifiers are read into an array before any hook runs, rather than by redirecting the file
# into the loop that calls them, because a hook inherits this shell's stdin and one of them puts a
# screen on it: the version menu that module_select_version draws takes its keystrokes from stdin
# (tools/tui/term.py). Redirecting modules.list into the loop body replaces that with the rest of a
# text file, and the selector's answer is to refuse a menu it cannot get input from -- which left an
# interactive launch that had selected a module unable to finish asking for its version.
harness_modules() {
  local phase=$1 module hook
  local modules=()
  [[ -f "${HARNESS_RUNTIME_DIR}/modules.list" ]] || return 0
  while IFS= read -r module; do
    modules+=("${module}")
  done <"${HARNESS_RUNTIME_DIR}/modules.list"
  # `read` fills the variable and reports failure together on a final line with no newline, so the
  # loop above drops that module unless it is appended here.
  [[ -z "${module:-}" ]] || modules+=("${module}")
  # The `${array[@]+...}` spelling is the launcher's idiom for a possibly empty array, which bare
  # `set -u` on Apple's bash 3.2 treats as an error; a launch with no module selected is one.
  for module in ${modules[@]+"${modules[@]}"}; do
    [[ "${module}" =~ ^[a-z][a-z0-9_]*$ ]] || harness_die "Invalid module identifier" || return
    MODULE_DIR="${HARNESS_ROOT}/modules/${module}"
    [[ -f "${MODULE_DIR}/module.sh" ]] || harness_die "Session module removed; stop and restart the stack" || return
    for hook in configure select_version prepare compose verify install check_build start; do
      unset -f "module_${hook}" 2>/dev/null || true
    done
    # shellcheck disable=SC1091
    source "${MODULE_DIR}/module.sh"
    if declare -F "module_${phase}" >/dev/null; then "module_${phase}"; fi
  done
}
