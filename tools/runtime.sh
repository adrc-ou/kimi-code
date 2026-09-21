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

harness_resolve_bootstrap_env() {
  [[ -f "${HARNESS_ROOT}/.env" ]] || harness_die "Missing .env. Copy .env.example to .env and configure it." || return
  command -v docker >/dev/null 2>&1 || harness_die "Required command not found: docker" || return
  docker compose version >/dev/null
  HARNESS_BOOTSTRAP_ENV=$(docker compose --env-file "${HARNESS_ROOT}/.env" \
    -f "${HARNESS_ROOT}/compose.bootstrap.yaml" config --environment)
  HARNESS_WORKSPACE_VALUE=$(printf '%s\n' "${HARNESS_BOOTSTRAP_ENV}" | \
    python3 "${HARNESS_ROOT}/scripts/read_env.py" /dev/stdin WORKSPACE_PATH)
  [[ -n "${HARNESS_WORKSPACE_VALUE}" ]] || harness_die "WORKSPACE_PATH must not be empty." || return
  case "${HARNESS_WORKSPACE_VALUE}" in
    /*) local candidate=${HARNESS_WORKSPACE_VALUE} ;;
    *) local candidate="${HARNESS_ROOT}/${HARNESS_WORKSPACE_VALUE}" ;;
  esac
  mkdir -p -- "${candidate}"
  HARNESS_WORKSPACE=$(cd "${candidate}" && pwd -P)
  case "${HARNESS_WORKSPACE}" in
    /|"${HARNESS_ROOT}"|"${HOME}") harness_die "Refusing unsafe WORKSPACE_PATH: ${HARNESS_WORKSPACE}" || return ;;
  esac
  [[ "${HARNESS_WORKSPACE}" != *$'\n'* ]] || harness_die "WORKSPACE_PATH contains a newline" || return
  export WORKSPACE_PATH=${HARNESS_WORKSPACE}
  export HARNESS_WORKSPACE
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

harness_validate_compose() {
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
  harness_lock
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
