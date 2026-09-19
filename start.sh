#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"

non_interactive=false
if [[ $# -eq 1 && "$1" == "--non-interactive" ]]; then
  non_interactive=true
elif [[ $# -ne 0 ]]; then
  echo "usage: ./start.sh [--non-interactive]" >&2
  exit 2
fi

# LOCAL_UID becomes the container user, so a root launch would build the agent image with
# USER 0:0 and leave every host artifact owned by root. The in-container initializer refuses
# root too, but it runs inside Compose and cannot cover the build or the version probe.
if [[ "$(id -u)" == 0 ]]; then
  echo "./start.sh must not run as root; run it as the account that owns the workspace." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${root}/tools/runtime.sh"
# Before the lock and the runtime directory exist, so a host missing docker fails with this
# sentence rather than with a compose error after a bind source has already been created.
harness_require_commands || exit 1
harness_traps
echo "Preparing Kimi workspace..."
harness_init
harness_warn_env_migration
docker info >/dev/null || { echo "Docker is not ready. Start Docker Desktop and retry." >&2; exit 1; }

MODULE_PIDS=()
MODULE_SESSION_FILES=()
COMPOSE_PID=""
PROMPT_MEASURE_PID=""
stack_started=false

cleanup() {
  status=$?
  trap - EXIT INT TERM ERR
  set +e
  for pid in ${MODULE_PIDS[@]+"${MODULE_PIDS[@]}"} "${COMPOSE_PID}" "${PROMPT_MEASURE_PID}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in ${MODULE_PIDS[@]+"${MODULE_PIDS[@]}"} "${COMPOSE_PID}" "${PROMPT_MEASURE_PID}"; do
    if [[ -n "${pid}" ]]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done
  if [[ "${stack_started}" == true ]]; then
    harness_compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  # Session material is deleted on exit. The module guidance is regenerated before every render,
  # and a stale extension snapshot must never outlive the approval that produced it.
  #
  # The two composed prompt documents are the deliberate exception, and sit alongside each other
  # for the same reason: they carry no secret, they are rewritten from scratch on every launch,
  # and once the stack is down they are the only surviving evidence of what this launcher actually
  # put in front of the model.
  #
  # prompt-context.json and prompt-measurements.jsonl persist too and must never join this list:
  # the first is the startup panel's remembered choices and the second is the history the panel
  # reads its token counts from. Extending the list by analogy with a neighbour would wipe the
  # operator's settings on every exit, which is the inverse of the feature.
  for file in proxy-token search-token kimi-config.toml runtime.env session.env module.env model-selection.json model-policy.json model.env module-guidance.md prompt-measure.log compose/models.json compose/module-environment.json compose/resolved.json ${MODULE_SESSION_FILES[@]+"${MODULE_SESSION_FILES[@]}"}; do
    [[ -f "${HARNESS_RUNTIME_DIR}/${file}" ]] && find "${HARNESS_RUNTIME_DIR}/${file}" -delete
  done
  [[ -d "${HARNESS_RUNTIME_DIR}/extension-snapshot" ]] && rm -rf -- "${HARNESS_RUNTIME_DIR}/extension-snapshot"
  # The measurement job copies the agent's home out of the container to read it. Being killed
  # between the copy and its own cleanup must not leave conversation text on the host, and a job
  # that exits early takes its pid down with it, so this directory is cleared unconditionally.
  [[ -d "${HARNESS_RUNTIME_DIR}/prompt-sessions" ]] && rm -rf -- "${HARNESS_RUNTIME_DIR}/prompt-sessions"
  # Provider keys are session material: empty the directory, including any file a
  # renamed credential left behind.
  [[ -d "${HARNESS_RUNTIME_DIR}/credentials" ]] && find "${HARNESS_RUNTIME_DIR}/credentials" -mindepth 1 -delete
  harness_unlock
  exit "${status}"
}
trap cleanup EXIT

platform_label=${HARNESS_PLATFORM_LABEL}
kimi_asset=${HARNESS_KIMI_ASSET}
workspace=${HARNESS_WORKSPACE}

export WORKSPACE_PATH=${workspace}
if [[ "$(uname -s)" == Darwin ]]; then
  export LOCAL_UID=1000 LOCAL_GID=1000
else
  export LOCAL_UID=${LOCAL_UID:-$(id -u)}
  export LOCAL_GID=${LOCAL_GID:-$(id -g)}
fi

export MODULE_NON_INTERACTIVE=${non_interactive}
module_args=()
[[ "${non_interactive}" == true ]] && module_args+=(--non-interactive)

# Model selection precedes the Kimi Code version choice: every downstream number -
# lane sizes, the subagent fan-out, the proxy's enforcement plan - is derived from
# the two models selected here rather than from .env.
python3 tools/models.py select ${module_args[@]+"${module_args[@]}"}
python3 tools/models.py resolve
set -a
# shellcheck disable=SC1091
source "${HARNESS_RUNTIME_DIR}/model.env"
set +a

python3 tools/modules.py select ${module_args[@]+"${module_args[@]}"}
harness_modules configure

state_file=${HARNESS_STATE_FILE}
session_file=${HARNESS_SESSION_FILE}
state_value() {
  if [[ -f "${state_file}" ]]; then
    python3 scripts/read_env.py "${state_file}" "$1" 2>/dev/null || true
  fi
}

installed_kimi=""
state_kimi=$(state_value KIMI_CODE_VERSION)
if [[ -n "${state_kimi}" ]]; then
  image_kimi=$(docker image inspect "adrc-kimi-agent:${HARNESS_IMAGE_SUFFIX}" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' 2>/dev/null || true)
  [[ "${image_kimi}" == "${state_kimi}" ]] && installed_kimi=${state_kimi}
fi

GITHUB_RELEASES_TOKEN=$(python3 scripts/read_env.py "${HARNESS_RESOLVED_BOOTSTRAP}" GITHUB_RELEASES_TOKEN 2>/dev/null || true)
export GITHUB_RELEASES_TOKEN
selector=(python3 scripts/select_versions.py --platform-label "${platform_label}" --kimi-asset "${kimi_asset}" --state "${state_file}" --output "${session_file}" --installed-kimi "${installed_kimi}")
[[ "${non_interactive}" == true ]] && selector+=(--non-interactive)
"${selector[@]}"

harness_modules select_version
python3 tools/modules.py environment ${module_args[@]+"${module_args[@]}"}
set -a
# shellcheck disable=SC1091
source "${HARNESS_RUNTIME_DIR}/module.env"
set +a

# Generated by scripts/select_versions.py from validated values.
set -a
# shellcheck disable=SC1090
source "${session_file}"
set +a

python3 tools/safe_workspace_init.py "${workspace}"
python3 tools/resource_check.py "${workspace}"
python3 tools/modules.py assemble

# Both halves of the context decision happen here, after the modules are known and before anything
# is rendered: the panel is the only place the resolved prompt graph is ever visible, and the file it
# writes is what render_runtime.py composes against. An unattended launch prints the same screen with
# --plain, so the log records the choices it applied instead of silently applying them.
panel=(python3 tools/prompt_panel.py --root "${root}" --runtime-dir "${HARNESS_RUNTIME_DIR}")
[[ "${non_interactive}" == true ]] && panel+=(--plain)
"${panel[@]}"

python3 tools/render_runtime.py --root "${root}" --runtime-dir "${HARNESS_RUNTIME_DIR}" --resolved-env "${HARNESS_RESOLVED_BOOTSTRAP}"
set -a
# shellcheck disable=SC1091
source "${HARNESS_RUNTIME_DIR}/runtime.env"
set +a

approval_manifest="${HARNESS_RUNTIME_DIR}/extension-approval.json"
python3 tools/approve_extensions.py prepare --workspace "${workspace}" --manifest "${approval_manifest}" --state-dir "${HARNESS_RUNTIME_DIR}" --output "${HARNESS_COMPOSE_DIR}/approved-extensions.yaml"

harness_modules prepare
harness_compose_files
harness_validate_compose
# First of two verifications by design: this one fails before the image build, the second runs
# immediately before Compose starts, because a bind source can be replaced during the build.
harness_modules verify

harness_modules install

harness_compose build
PROMPT_IMAGE_ID=$(harness_prompt_image_id)
actual_kimi=$(harness_compose run -T --rm --no-deps kimi-agent kimi --version)
[[ "${actual_kimi}" == *"${KIMI_CODE_VERSION}"* ]] || { echo "Built Kimi version mismatch: ${actual_kimi}" >&2; exit 1; }
harness_modules check_build

cp -- "${session_file}" "${state_file}"
chmod 600 "${state_file}"
cp "${HARNESS_RUNTIME_DIR}/modules.json" "${HARNESS_RUNTIME_DIR}/last-modules.json"
cp "${HARNESS_RUNTIME_DIR}/model-selection.json" "${HARNESS_RUNTIME_DIR}/last-model-selection.json"
find "${session_file}" -delete

wait_for_url() {
  local url=$1 token=${2:-} cafile=${3:-} attempts=${4:-120}
  local attempt
  for ((attempt=1; attempt<=attempts; attempt++)); do
    if [[ -n "${COMPOSE_PID:-}" ]] && ! kill -0 "${COMPOSE_PID}" 2>/dev/null; then
      return 1
    fi
    if URL_TO_CHECK="${url}" TOKEN_TO_CHECK="${token}" CA_TO_CHECK="${cafile}" python3 - <<'PY' >/dev/null 2>&1
import os, ssl, urllib.request
headers = {}
if os.environ.get("TOKEN_TO_CHECK"): headers["Authorization"] = f"Bearer {os.environ['TOKEN_TO_CHECK']}"
context = ssl.create_default_context(cafile=os.environ.get("CA_TO_CHECK") or None)
urllib.request.urlopen(urllib.request.Request(os.environ["URL_TO_CHECK"], headers=headers), timeout=3, context=context).read(1)
PY
    then return 0; fi
    sleep 1
  done
  return 1
}

# Kimi's own prompt literals live in the 182 MB bundle, which exists at a known path only inside
# the agent image. An operator's SYSTEM.md is allowed to quote one (${kimi.coder_role} and friends)
# so that a custom prompt inherits upstream edits, and that promise has to be kept without the
# operator doing anything: the stack that already has the bundle extracts it. Same transport as
# ./prompts.sh --extract, so a hand-run refresh and an automatic one produce the same file.
#
# Bounded and silent on failure. A cold cache only matters to a prompt that quotes a literal, and
# render_runtime reports that case by name rather than substituting anything wrong.
cache_kimi_literals() {
  local log="${HARNESS_RUNTIME_DIR}/prompt-measure.log"
  local cache="${HARNESS_RUNTIME_DIR}/kimi-prompts"
  local attempt tmp
  mkdir -p -- "${cache}" && chmod 700 -- "${cache}"
  tmp=$(mktemp) || return 0
  for ((attempt = 1; attempt <= 20; attempt++)); do
    sleep 3
    if harness_compose exec -T kimi-agent python3 /opt/kimi-runtime/tools/kimi_prompts.py \
        --print --image "${PROMPT_IMAGE_ID:-unknown}" >"${tmp}" 2>>"${log}"; then
      if cp -- "${tmp}" "${cache}/literals.json" && chmod 600 -- "${cache}/literals.json"; then
        rm -f -- "${tmp}"
        return 0
      fi
    fi
  done
  rm -f -- "${tmp}"
  echo "no Kimi prompt literals this session: the container never returned them" >>"${log}"
  return 0
}

# Token accounting has to wait for the stack: a profile.bind record only exists once the operator
# has sent a first prompt, and that record carries the finished system prompt, which is the only
# honest measurement of what this session costs. Probing the rendered text ourselves would mean
# reimplementing Kimi's placeholder renderer a second time.
#
# Best-effort by construction. Every failure is written to the log and dropped, because a number
# that never landed must never cost a working stack; a launch with no history simply shows ~.
measure_prompts_after_first_request() {
  local log="${HARNESS_RUNTIME_DIR}/prompt-measure.log"
  local staging="${HARNESS_RUNTIME_DIR}/prompt-sessions"
  local prefs="${HARNESS_RUNTIME_DIR}/prompt-context.json"
  local attempt options
  # The first prompt is the only moment a profile.bind record exists, and nobody knows when the
  # operator will send it, so this waits on a deadline rather than assuming. Three seconds times
  # sixty attempts, and a number that never lands costs a log line and nothing else.
  local poll_seconds=3 poll_attempts=60
  : >"${log}" && chmod 600 "${log}"
  # Concurrent by intent: the measurement loop has its own patience, so waiting for the bundle
  # never costs the token figures their chance to land in this session.
  cache_kimi_literals &
  for ((attempt = 1; attempt <= poll_attempts; attempt++)); do
    sleep "${poll_seconds}"
    # Poll with exec rather than by copying: the copy carries conversation content and is only
    # worth making once a record actually exists.
    harness_compose exec -T kimi-agent /bin/sh -c \
      "grep -lq '\"profile.bind\"' /home/agent/.kimi-code/sessions/*/*/agents/*/wire.jsonl 2>/dev/null" \
      >>"${log}" 2>&1 || continue
    options=$(shasum -a 256 "${prefs}" 2>/dev/null | cut -c1-12)
    rm -rf -- "${staging}"
    if harness_compose cp kimi-agent:/home/agent/.kimi-code "${staging}" >>"${log}" 2>&1; then
      python3 tools/prompt_measure.py \
        --sessions-dir "${staging}" \
        --plan "${HARNESS_RUNTIME_DIR}/model-policy.json" \
        --out "${HARNESS_RUNTIME_DIR}/prompt-measurements.jsonl" \
        --image "${PROMPT_IMAGE_ID:-unknown}" --prefs "${options:-default}" >>"${log}" 2>&1
    fi
    # The tree holds conversation text, so it leaves the host filesystem the same way it arrived.
    rm -rf -- "${staging}"
    return 0
  done
  echo "no prompt measurement this session: no first request within $((poll_seconds * poll_attempts))s" >>"${log}"
  return 0
}

harness_modules start
echo "Instance:  ${HARNESS_INSTANCE_ID}"
echo "Press Ctrl-C to stop everything."
echo

harness_modules verify
# searxng-init is expected to exit successfully during startup.
harness_compose up --remove-orphans --abort-on-container-failure &
COMPOSE_PID=$!
stack_started=true
measure_prompts_after_first_request &
PROMPT_MEASURE_PID=$!
if wait_for_url http://127.0.0.1:5494/api/v1/healthz "" "" 90; then
  harness_compose exec -T kimi-agent python3 /opt/kimi-runtime/tools/register_workspace.py
  echo "Kimi Code: http://127.0.0.1:5494 (workspace ready)"
  python3 tools/open_kimi_browser.py docker compose --env-file "${root}/.env" "${HARNESS_COMPOSE_FILES[@]}" || true
  if ! harness_compose exec -T kimi-agent /opt/serena/bin/python /opt/kimi-runtime/tools/check_services.py; then
    echo "Service checks need attention. The stack remains running; see docs/verification.md and rerun ./doctor.sh." >&2
  fi
else
  echo "Kimi web did not become ready for workspace registration; inspect the startup logs." >&2
  exit 1
fi
while kill -0 "${COMPOSE_PID}" 2>/dev/null; do
  for pid in ${MODULE_PIDS[@]+"${MODULE_PIDS[@]}"}; do
    kill -0 "${pid}" 2>/dev/null || { echo "A module process exited; inspect ${HARNESS_RUNTIME_DIR} logs" >&2; exit 1; }
  done
  sleep 1
done
wait "${COMPOSE_PID}"
