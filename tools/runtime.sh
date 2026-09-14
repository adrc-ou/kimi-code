#!/usr/bin/env bash
# Shared host orchestration helpers. This file must be sourced.

harness_die() {
  echo "$*" >&2
  return 1
}

harness_platform() {
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64)
      HARNESS_PLATFORM=darwin-arm64
      HARNESS_PLATFORM_LABEL="macOS / Apple Silicon / MPS"
      HARNESS_BACKEND=mps
      HARNESS_KIMI_ASSET=kimi-code-linux-arm64.tar.gz
      ;;
    Linux:x86_64)
      if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
        HARNESS_PLATFORM=wsl2-x86_64
        HARNESS_PLATFORM_LABEL="Windows / WSL2 / NVIDIA CUDA"
        HARNESS_BACKEND=cuda
        HARNESS_KIMI_ASSET=kimi-code-linux-x64.tar.gz
      else
        harness_die "Unsupported host. This project supports Apple Silicon macOS and Windows/WSL2 x86-64." || return
      fi
      ;;
    *) harness_die "Unsupported host. On Windows, run inside WSL2, not Git Bash." || return ;;
  esac
  export HARNESS_PLATFORM HARNESS_PLATFORM_LABEL HARNESS_BACKEND HARNESS_KIMI_ASSET
}

harness_resolve_bootstrap_env() {
  [[ -f "${HARNESS_ROOT}/.env" ]] || harness_die "Missing .env. Copy .env.example to .env and configure it." || return
  command -v docker >/dev/null 2>&1 || harness_die "Required command not found: docker" || return
  docker compose version >/dev/null
  local output
  output=$(docker compose --env-file "${HARNESS_ROOT}/.env" \
    -f "${HARNESS_ROOT}/compose.bootstrap.yaml" config --environment)
  HARNESS_RESOLVED_BOOTSTRAP=$(mktemp "${TMPDIR:-/tmp}/kimi-env.XXXXXX")
  chmod 600 "${HARNESS_RESOLVED_BOOTSTRAP}"
  printf '%s\n' "${output}" >"${HARNESS_RESOLVED_BOOTSTRAP}"
  HARNESS_WORKSPACE_VALUE=$(python3 "${HARNESS_ROOT}/scripts/read_env.py" \
    "${HARNESS_RESOLVED_BOOTSTRAP}" WORKSPACE_PATH)
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
  local variable value
  for variable in COMFYUI_MODELS_PATH COMFYUI_CUSTOM_NODES_PATH COMFYUI_INPUT_PATH COMFYUI_OUTPUT_PATH COMFYUI_TEMP_PATH COMFYUI_USER_PATH; do
    value=$(python3 "${HARNESS_ROOT}/scripts/read_env.py" "${HARNESS_RESOLVED_BOOTSTRAP}" "${variable}" 2>/dev/null || true)
    [[ -n "${value}" ]] && export "${variable}=${value}"
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
  mkdir -p "${HARNESS_COMPOSE_DIR}"
  chmod 700 "${HARNESS_RUNTIME_DIR}" "${HARNESS_COMPOSE_DIR}"
  : "${COMPOSE_PROJECT_NAME:=kimi_code_${HARNESS_INSTANCE_ID}}"
  [[ "${COMPOSE_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || harness_die "Invalid COMPOSE_PROJECT_NAME" || return
  export HARNESS_INSTANCE_ID HARNESS_RUNTIME_DIR HARNESS_COMPOSE_DIR
  export HARNESS_STATE_FILE HARNESS_SESSION_FILE HARNESS_IMAGE_SUFFIX COMPOSE_PROJECT_NAME
}

harness_lock() {
  HARNESS_LOCK_PATH="${HARNESS_RUNTIME_DIR}/launcher.lock"
  if command -v flock >/dev/null 2>&1; then
    exec {HARNESS_LOCK_FD}>"${HARNESS_LOCK_PATH}"
    if ! flock -n "${HARNESS_LOCK_FD}"; then
      harness_die "Another launcher owns ${HARNESS_LOCK_PATH}" || return
    fi
    printf 'pid=%s\nstarted=%s\n' "$$" "$(date -u +%FT%TZ)" 1>&"${HARNESS_LOCK_FD}"
    export HARNESS_LOCK_FD HARNESS_LOCK_PATH
    return
  fi
  HARNESS_LOCK_DIRECTORY="${HARNESS_LOCK_PATH}.d"
  if ! mkdir "${HARNESS_LOCK_DIRECTORY}" 2>/dev/null; then
    local owner=""
    [[ -f "${HARNESS_LOCK_DIRECTORY}/pid" ]] && owner=$(sed -n '1p' "${HARNESS_LOCK_DIRECTORY}/pid")
    if [[ "${owner}" =~ ^[0-9]+$ ]] && ! kill -0 "${owner}" 2>/dev/null; then
      find "${HARNESS_LOCK_DIRECTORY}" -depth -delete
      mkdir "${HARNESS_LOCK_DIRECTORY}"
    else
      harness_die "Another launcher owns ${HARNESS_LOCK_DIRECTORY} (pid ${owner:-unknown})" || return
    fi
  fi
  printf '%s\n' "$$" >"${HARNESS_LOCK_DIRECTORY}/pid"
  chmod 700 "${HARNESS_LOCK_DIRECTORY}"
  export HARNESS_LOCK_DIRECTORY HARNESS_LOCK_PATH
}

harness_unlock() {
  if [[ -n "${HARNESS_LOCK_DIRECTORY:-}" && -d "${HARNESS_LOCK_DIRECTORY}" ]]; then
    find "${HARNESS_LOCK_DIRECTORY}" -depth -delete
  fi
  if [[ -n "${HARNESS_LOCK_FD:-}" ]]; then
    flock -u "${HARNESS_LOCK_FD}" 2>/dev/null || true
    eval "exec ${HARNESS_LOCK_FD}>&-"
  fi
  [[ -n "${HARNESS_RESOLVED_BOOTSTRAP:-}" ]] && find "${HARNESS_RESOLVED_BOOTSTRAP}" -delete 2>/dev/null || true
}

harness_compose_files() {
  HARNESS_COMPOSE_FILES=(-f "${HARNESS_ROOT}/compose.yaml" -f "${HARNESS_ROOT}/compose.search.yaml")
  if [[ "${HARNESS_BACKEND}" == cuda ]]; then
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_ROOT}/compose.comfy.cuda.yaml")
  else
    HARNESS_COMPOSE_FILES+=(-f "${HARNESS_ROOT}/compose.comfy.mps.yaml")
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

harness_validate_compose() {
  harness_compose config --format json >"${HARNESS_COMPOSE_DIR}/resolved.json"
  chmod 600 "${HARNESS_COMPOSE_DIR}/resolved.json"
}

harness_init() {
  HARNESS_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  export HARNESS_ROOT
  harness_platform
  harness_resolve_bootstrap_env
  harness_instance
  harness_lock
}

harness_init_readonly() {
  HARNESS_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  export HARNESS_ROOT
  harness_platform
  harness_resolve_bootstrap_env
  harness_instance
}
