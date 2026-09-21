#!/usr/bin/env bash
# Operator-trusted lifecycle hooks; no workspace code is sourced by the host.
module_compatible() {
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64) return 0 ;;
    Linux:x86_64) command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

comfy_backend() {
  local TORCH_VERSION TORCHVISION_VERSION TORCHAUDIO_VERSION PYTORCH_CUDA_INDEX_URL
  # shellcheck disable=SC1091
  source "${MODULE_DIR}/backend/backend.env"
  export COMFYUI_TORCH_VERSION=${TORCH_VERSION}
  export COMFYUI_TORCHVISION_VERSION=${TORCHVISION_VERSION}
  export COMFYUI_TORCHAUDIO_VERSION=${TORCHAUDIO_VERSION}
  export COMFYUI_CUDA_INDEX_URL=${PYTORCH_CUDA_INDEX_URL}
}

module_configure() {
  # The platform is derived once, by the launcher, and this module reads that conclusion instead of
  # repeating the probe. Both installers get their backend from the same case, so the profile the
  # version picker offers and the installer that later consumes its answer cannot disagree. Plain
  # Linux and WSL2 share one profile: the CUDA path is containerised, so what it installs against is
  # the container's architecture rather than whatever the host kernel calls itself.
  case "${HARNESS_PLATFORM}" in
    darwin-arm64) COMFYUI_BACKEND=mps; COMFYUI_PLATFORM=darwin-arm64 ;;
    linux-x86_64|wsl2-x86_64) COMFYUI_BACKEND=cuda; COMFYUI_PLATFORM=wsl2-x86_64 ;;
    *)
      echo "ComfyUI has no backend profile for host platform ${HARNESS_PLATFORM:-unknown}." >&2
      return 1
      ;;
  esac
  export COMFYUI_PLATFORM COMFYUI_BACKEND
  comfy_backend
  local variable value
  for variable in COMFYUI_MODELS_PATH COMFYUI_CUSTOM_NODES_PATH COMFYUI_INPUT_PATH COMFYUI_OUTPUT_PATH COMFYUI_TEMP_PATH COMFYUI_USER_PATH COMFYUI_MACOS_PYTHON COMFYUI_BRIDGE_MAX_BODY COMFYUI_BRIDGE_MAX_WS_MESSAGE COMFYUI_BRIDGE_MAX_CONNECTIONS COMFYUI_BRIDGE_GRANT_TTL COMFYUI_BRIDGE_SESSION_TTL COMFYUI_BRIDGE_MAX_SESSIONS COMFYUI_OPEN_FRONTEND; do
    value=$(python3 "${HARNESS_ROOT}/scripts/read_env.py" "${HARNESS_RESOLVED_BOOTSTRAP}" "${variable}" 2>/dev/null || true)
    [[ -z "${value}" ]] || export "${variable}=${value}"
  done
}

module_select_version() {
  local installed_comfy state_comfy current image_comfy
  installed_comfy=""
  state_comfy=$(state_value COMFYUI_VERSION)
  if [[ -n "${state_comfy}" ]]; then
    if [[ "${COMFYUI_BACKEND}" == mps ]]; then
      current="${HARNESS_RUNTIME_DIR}/module-data/comfyui/app/current"
      [[ -d "${current}" ]] || current="${HARNESS_ROOT}/.local/comfy-macos/${HARNESS_INSTANCE_ID}/current"
      [[ -x "${current}/venv/bin/python" && -f "${current}/VERSION" && "$(<"${current}/VERSION")" == "${state_comfy}" ]] && installed_comfy=${state_comfy}
    else
      image_comfy=$(docker image inspect "adrc-comfyui-cuda:${HARNESS_IMAGE_SUFFIX}" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' 2>/dev/null || true)
      [[ "${image_comfy}" == "${state_comfy}" ]] && installed_comfy=${state_comfy}
    fi
  fi

  export COMFYUI_INSTALLED=${installed_comfy}
  python3 "${MODULE_DIR}/versions.py"
}

comfy_migrate() {
  # Retain existing native installations when upgrading the pre-module harness.
  local legacy="${HARNESS_ROOT}/.local/comfy-macos/${HARNESS_INSTANCE_ID}"
  local data="${HARNESS_RUNTIME_DIR}/module-data/comfyui"
  if [[ -d "${legacy}" && ! -e "${data}/app" ]]; then
    mkdir -p "${data}"
    mv "${legacy}" "${data}/app"
  fi
  if [[ -d "${HARNESS_RUNTIME_DIR}/python" && ! -e "${data}/python" ]]; then
    mkdir -p "${data}"
    mv "${HARNESS_RUNTIME_DIR}/python" "${data}/python"
  fi
}

comfy_resolve() {
  # The shipped lock was compiled from one release's requirements and is installed with
  # `--require-hashes`, so any other release needs a lock of its own before anything installs it.
  # This is the only place that asks for one, which is what keeps the two installers reading a
  # single path and holds the network round trip to once per release per platform.
  local announced="${HARNESS_RUNTIME_DIR}/comfyui/lock-path"
  python3 "${MODULE_DIR}/resolve_locks.py" \
    --platform "${COMFYUI_PLATFORM}" \
    --version "${COMFYUI_VERSION}" \
    --commit "${COMFYUI_COMMIT}" \
    --requirements-sha256 "${COMFYUI_REQUIREMENTS_SHA256}" \
    --runtime-dir "${HARNESS_RUNTIME_DIR}" \
    --output "${announced}" || return 1
  read -r COMFYUI_LOCK_PATH <"${announced}"
  # The CUDA build takes the lock as a named context, and a context is a directory with a fixed
  # file name in it: handing the build the keyed path instead would make the Dockerfile depend on
  # a digest that changes with every release.
  COMFYUI_LOCK_CONTEXT="${HARNESS_RUNTIME_DIR}/comfyui/build"
  mkdir -p "${COMFYUI_LOCK_CONTEXT}" || return 1
  cp -f -- "${COMFYUI_LOCK_PATH}" "${COMFYUI_LOCK_CONTEXT}/requirements.lock" || return 1
  export COMFYUI_LOCK_PATH COMFYUI_LOCK_CONTEXT
  printf 'export COMFYUI_LOCK_CONTEXT=%q\n' "${COMFYUI_LOCK_CONTEXT}" >>"${HARNESS_RUNTIME_DIR}/runtime.env"
}

module_prepare() {
  local bind_manifest bind_assignments assignment variable
  comfy_migrate
  comfy_resolve
  mkdir -p "${HARNESS_RUNTIME_DIR}/module-data/comfyui"
  MODULE_SESSION_FILES+=(module-data/comfyui/bridge.crt module-data/comfyui/bridge.key)
  COMFYUI_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  export COMFYUI_TOKEN
  printf 'export COMFYUI_TOKEN=%q\n' "${COMFYUI_TOKEN}" >>"${HARNESS_RUNTIME_DIR}/runtime.env"
  bind_manifest="${HARNESS_RUNTIME_DIR}/module-data/comfyui/binds.json"
  bind_assignments=$(python3 "${MODULE_DIR}/scripts/verify_bind_paths.py" record "${HARNESS_WORKSPACE}" "${bind_manifest}")
  while IFS= read -r assignment; do
    variable=${assignment%%=*}
    export "${variable}=${assignment#*=}"
    printf 'export %s=%q\n' "${variable}" "${assignment#*=}" >>"${HARNESS_RUNTIME_DIR}/runtime.env"
  done <<<"${bind_assignments}"
  printf 'export COMFYUI_BACKEND=%q\n' "${COMFYUI_BACKEND}" >>"${HARNESS_RUNTIME_DIR}/runtime.env"
  if [[ "${COMFYUI_BACKEND}" == mps ]]; then
    command -v openssl >/dev/null 2>&1 || { echo "Required command not found: openssl" >&2; exit 1; }
    COMFYUI_BRIDGE_CERT="${HARNESS_RUNTIME_DIR}/module-data/comfyui/bridge.crt"
    COMFYUI_BRIDGE_KEY="${HARNESS_RUNTIME_DIR}/module-data/comfyui/bridge.key"
    export COMFYUI_BRIDGE_CERT COMFYUI_BRIDGE_KEY
    # shell.sh and acceptance.sh also need the certificate path for Compose.
    printf 'export COMFYUI_BRIDGE_CERT=%q\n' "${COMFYUI_BRIDGE_CERT}" >>"${HARNESS_RUNTIME_DIR}/runtime.env"
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 2 \
      -config "${MODULE_DIR}/openssl-bridge.cnf" \
      -keyout "${COMFYUI_BRIDGE_KEY}" -out "${COMFYUI_BRIDGE_CERT}" >/dev/null 2>&1
    chmod 600 "${COMFYUI_BRIDGE_KEY}" "${COMFYUI_BRIDGE_CERT}"
  fi
}

module_compose() {
  comfy_backend
  HARNESS_COMPOSE_FILES+=(-f "${MODULE_DIR}/compose.${COMFYUI_BACKEND}.yaml")
}

module_verify() {
  python3 "${MODULE_DIR}/scripts/verify_bind_paths.py" verify "${HARNESS_WORKSPACE}" "${HARNESS_RUNTIME_DIR}/module-data/comfyui/binds.json"
}

module_install() {
  local mac_python
  if [[ "${COMFYUI_BACKEND}" == mps ]]; then
    mac_python=$(bash "${MODULE_DIR}/scripts/select_macos_python.sh")
    bash "${MODULE_DIR}/scripts/install_comfy_macos.sh" "${HARNESS_ROOT}" "${HARNESS_WORKSPACE}" "${COMFYUI_VERSION}" "${COMFYUI_COMMIT}" "${mac_python}" "${HARNESS_INSTANCE_ID}" "${COMFYUI_LOCK_PATH}" "${COMFYUI_REQUIREMENTS_SHA256}"
  fi
}

module_check_build() {
  if [[ "${COMFYUI_BACKEND}" == cuda ]]; then
    # -T for the same reason the launcher puts it on its own one-shot `compose run`: a hook is
    # called with the launcher's terminal on stdin, and without this flag `run` would attach that
    # terminal and allocate a pty for a command whose whole job is to print one line and exit.
    harness_compose run -T --rm --no-deps comfyui python -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name())'
  fi
}

module_start() {
  local comfy_home comfy_log bridge_log COMFY_PID COMFY_BRIDGE_PID
  if [[ "${COMFYUI_BACKEND}" == mps ]]; then
    comfy_home="${HARNESS_RUNTIME_DIR}/module-data/comfyui/app/current"
    comfy_log="${HARNESS_RUNTIME_DIR}/module-data/comfyui/comfyui.log"
    bridge_log="${HARNESS_RUNTIME_DIR}/module-data/comfyui/bridge.log"
    (
      cd "${comfy_home}/app" || exit
      exec env PYTORCH_ENABLE_MPS_FALLBACK=1 "${comfy_home}/venv/bin/python" main.py \
        --listen 127.0.0.1 --port 8188 --disable-auto-launch \
        --user-directory "${COMFYUI_USER_PATH}" --input-directory "${COMFYUI_INPUT_PATH}" \
        --output-directory "${COMFYUI_OUTPUT_PATH}" --temp-directory "${COMFYUI_TEMP_PATH}"
    ) >"${comfy_log}" 2>&1 &
    COMFY_PID=$!
    MODULE_PIDS+=("${COMFY_PID}")
    wait_for_url http://127.0.0.1:8188/system_stats "" "" 180 || { echo "ComfyUI did not become ready. See ${comfy_log}" >&2; exit 1; }
    COMFYUI_TOKEN=${COMFYUI_TOKEN} "${comfy_home}/venv/bin/python" "${MODULE_DIR}/scripts/comfy_bridge.py" \
      --host 0.0.0.0 --port 8190 --upstream http://127.0.0.1:8188 \
      --tls-cert "${COMFYUI_BRIDGE_CERT}" --tls-key "${COMFYUI_BRIDGE_KEY}" \
      --max-body "${COMFYUI_BRIDGE_MAX_BODY:-536870912}" \
      --max-ws-message "${COMFYUI_BRIDGE_MAX_WS_MESSAGE:-67108864}" \
      --max-connections "${COMFYUI_BRIDGE_MAX_CONNECTIONS:-16}" \
      --grant-ttl "${COMFYUI_BRIDGE_GRANT_TTL:-60}" \
      --session-ttl "${COMFYUI_BRIDGE_SESSION_TTL:-43200}" \
      --max-sessions "${COMFYUI_BRIDGE_MAX_SESSIONS:-8}" >"${bridge_log}" 2>&1 &
    COMFY_BRIDGE_PID=$!
    MODULE_PIDS+=("${COMFY_BRIDGE_PID}")
    wait_for_url https://127.0.0.1:8190/system_stats "${COMFYUI_TOKEN}" "${COMFYUI_BRIDGE_CERT}" 30 || { echo "ComfyUI bridge did not become ready. See ${bridge_log}" >&2; exit 1; }
    # ComfyUI starts with --disable-auto-launch, so its own opener never fires here and the
    # launcher opens the tab instead - once readiness is proven rather than hoped for. The URL is
    # the host's loopback frontend, the same page this module has always told the operator to open,
    # and it carries no bridge credential at all. A host with no desktop just fails this one line.
    if [[ "${COMFYUI_OPEN_FRONTEND:-true}" != false ]]; then
      python3 "${MODULE_DIR}/scripts/open_comfy_frontend.py" --url http://127.0.0.1:8188 || true
    fi
  fi

  echo "ComfyUI: http://127.0.0.1:8188 (${COMFYUI_BACKEND})"
  if [[ "${COMFYUI_BACKEND}" == mps ]]; then
    # The origin the agent's own browser is meant to use. Both planes are the same ComfyUI and
    # both still authenticate at the bridge; only the shape of the credential differs.
    echo "ComfyUI frontend in the sandbox: http://comfyui-ui:8188"
  fi
}

if [[ "${1:-}" == compatible ]]; then module_compatible; fi
