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
DEEP_CHECK_PID=""
stack_started=false
# Declared up here rather than beside the flow that sets it, because cleanup is trapped for the
# whole script and `set -u` would turn an early failure into a second one: a trap that errors
# leaves the user holding a borrowed alternate screen with nothing left to hand it back.
screen_held=false
# Sentences the launch has to say but not while the modal owns the window. Printing into a borrowed
# alternate screen scrolls the questions away, and the frame that follows paints over only part of
# what was left behind, so the text waits here and harness_flow_notes reads it out once the window
# is ordinary scrollback again. tools/tui/screen.py's NOTES is this same path from the Python side.
flow_notes=${HARNESS_RUNTIME_DIR}/launch-notes.log

# The agent container keeps a read-only root filesystem, so the checker writes its machine-readable
# report to the container's tmpfs and the launcher carries it out afterwards. The report holds only
# the redacted status lines the checker already printed - never a response body, a credential, or
# a server's own error text.
SERVICE_REPORT_CONTAINER=/tmp/service-check.json
SERVICE_REPORT_NAME=service-check.json
# Long enough for Chromium, the language servers and a first session to finish starting: the whole
# point of the background pass is to judge a settled stack rather than a busy one.
SERVICE_SETTLE_SECONDS=45

cleanup() {
  status=$?
  trap - EXIT INT TERM ERR
  set +e
  # First, and before this function prints anything of its own: a launch that dies mid-question
  # must hand the terminal back so the diagnosis is readable in normal scrollback rather than
  # painted into an alternate screen the user has to guess their way out of. `leave` is silent
  # unless the borrow is still recorded, so the happy path's leave above costs nothing here.
  if [[ "${screen_held:-false}" == true ]]; then
    python3 tools/tui/screen.py --runtime-dir "${HARNESS_RUNTIME_DIR}" leave || true
    screen_held=false
  fi
  # The lines the modal was sitting on, in the case where nothing reached harness_flow_unhold --
  # a signal, or a failure in a step that reports its own trouble and exits through this trap.
  if [[ -s "${flow_notes:-/nonexistent}" ]]; then
    cat "${flow_notes}"
  fi
  for pid in ${MODULE_PIDS[@]+"${MODULE_PIDS[@]}"} "${COMPOSE_PID}" "${PROMPT_MEASURE_PID}" "${DEEP_CHECK_PID}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in ${MODULE_PIDS[@]+"${MODULE_PIDS[@]}"} "${COMPOSE_PID}" "${PROMPT_MEASURE_PID}" "${DEEP_CHECK_PID}"; do
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
  #
  # service-check.json is the same kind of exception: it is the verdict the last launch reached,
  # it stamps itself, and the next launch overwrites it. Its log does not survive, because that one
  # carries the raw output of an exec rather than a redacted conclusion.
  #
  # The two newest entries are here for opposite reasons and both matter. flow-state.json is the
  # interactive sequence's live state machine: while it exists, a step that already answered replays
  # its answer file instead of asking, so a crash partway through a pass must not leave one behind
  # for the next launch to mistake for progress. modules.json is deliberately not in this list, since
  # it is also the record of the last successful selection, which is exactly why the flow that reads
  # it as a replay has to go. module-values.json is the module environment answers, secrets included.
  for file in proxy-token search-token kimi-config.toml runtime.env session.env module.env model-selection.json model-policy.json model.env module-guidance.md flow-state.json module-values.json prompt-measure.log service-check.log launch-notes.log compose/models.json compose/module-environment.json compose/resolved.json ${MODULE_SESSION_FILES[@]+"${MODULE_SESSION_FILES[@]}"}; do
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

# --- the interactive sequence -------------------------------------------------
#
# Eight steps, one fullscreen modal screen each, and the only part of a launch that a Backspace can
# return through. Everything from the commit point down builds or starts something, so the loop ends
# above it and each of those phases runs exactly once.
#
# Model selection precedes the Kimi Code version choice: every downstream number - lane sizes, the
# subagent fan-out, the proxy's enforcement plan - is derived from the two models selected there
# rather than from .env.
flow=(python3 tools/tui/flow.py --runtime-dir "${HARNESS_RUNTIME_DIR}")
# The steps in the order they are asked, and the status a step leaves with when the user asked for
# the one before it. tools/tui/flow.py owns both. A back-request is navigation rather than failure,
# which is why it is the one status the pass does not report as one.
flow_steps=model,subagent,modules,kimi-version,module-version,module-values,context,credentials
flow_back=3

state_file=${HARNESS_STATE_FILE}
session_file=${HARNESS_SESSION_FILE}
state_value() {
  if [[ -f "${state_file}" ]]; then
    python3 scripts/read_env.py "${state_file}" "$1" 2>/dev/null || true
  fi
}

# Everything the launch held back while the modal had the window, said now that it does not.
harness_flow_notes() {
  if [[ -s "${flow_notes}" ]]; then
    cat "${flow_notes}"
    : >"${flow_notes}"
  fi
}

# Hand the window back, then read out whatever waited for it.
#
# The order is the whole point, and it is the reason a failure report cannot simply be printed from
# wherever it was noticed: leaving the alternate screen discards everything painted on it, so a
# sentence written while the modal was still up would be gone by the time anyone could read it.
# Idempotent, because the pass calls this on the way out of a failure and `cleanup` calls it again.
harness_flow_unhold() {
  [[ "${screen_held:-false}" == true ]] || return 0
  unset HARNESS_TUI_SCREEN
  "${screen[@]}" leave || true
  screen_held=false
  harness_flow_notes
}

# One command of a pass, plus the launcher's own diagnostic.
#
# A command whose status is tested never reaches the ERR trap, and testing the status is exactly how
# a back-request is told apart from a failure, so this says the sentence the trap would have said. It
# names the line rather than the command for the same reason the trap does: these commands can carry
# a credential in their arguments.
harness_flow_step() {
  local rc=0 caller=${BASH_SOURCE[1]:-start.sh} line=${BASH_LINENO[0]}
  "$@" || rc=$?
  harness_flow_report "${rc}" "${caller}" "${line}"
  return "${rc}"
}

# The diagnostic itself, shared by both flavours of step so that neither can forget the half of it
# that is not a printf.
harness_flow_report() {
  local rc=$1 caller=$2 line=$3
  if (( rc != 0 && rc != flow_back )); then
    harness_flow_unhold
    printf 'Harness failed at %s:%s (exit %s).\n' "${caller}" "${line}" "${rc}" >&2
  fi
}

# One command of a pass that asks nothing of the user, and so has no screen of its own.
#
# A pass is not only questions. The resolver, the module configure hook, the workspace initializer
# and the capacity check all run between the screens and all of them print, and while the modal holds
# the window ordinary text scrolls the questions away -- the operator then answers a screen with the
# previous step's summary painted through it. So this output waits in the launch notes and is read
# out when the window comes back, which is where a line like it already appeared.
#
# Never route a command that can put a screen up through here. Its interface is stdout, and spooling
# that hides the one question the operator is supposed to be able to see.
harness_flow_work() {
  local rc=0 caller=${BASH_SOURCE[1]:-start.sh} line=${BASH_LINENO[0]}
  if [[ "${screen_held:-false}" == true ]]; then
    "$@" >>"${flow_notes}" 2>&1 || rc=$?
  else
    "$@" || rc=$?
  fi
  harness_flow_report "${rc}" "${caller}" "${line}"
  return "${rc}"
}

# A generated environment file, exported and sourced: what `set -a` around a source does at the top
# level of the script. A file that will not source is a failed step, because every producer here
# writes its output before it returns.
harness_flow_source() {
  local rc=0
  set -a
  # shellcheck disable=SC1090
  source "$1" || rc=$?
  set +a
  return "${rc}"
}

# One pass over every step. The `|| return` on each line is load-bearing rather than decorative:
# inside a function whose own status is tested errexit is switched off, so without it a step that
# failed would be followed by every step after it.
harness_flow_pass() {
  # Both model lanes are answered in one process, so Backspace between them never has to leave it.
  harness_flow_step python3 tools/models.py select ${module_args[@]+"${module_args[@]}"} || return
  harness_flow_work python3 tools/models.py resolve || return
  harness_flow_source "${HARNESS_RUNTIME_DIR}/model.env" || return

  harness_flow_step python3 tools/modules.py select ${module_args[@]+"${module_args[@]}"} || return
  harness_flow_work harness_modules configure || return

  # Probed on every pass rather than remembered: which version is installed is a fact about the disk
  # now, and the row the menu marks installed is only honest if it was read now.
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
  harness_flow_step "${selector[@]}" || return

  harness_flow_step harness_modules select_version || return
  harness_flow_step python3 tools/modules.py environment ${module_args[@]+"${module_args[@]}"} || return
  harness_flow_source "${HARNESS_RUNTIME_DIR}/module.env" || return
  # Generated by scripts/select_versions.py from validated values.
  harness_flow_source "${session_file}" || return

  harness_flow_work python3 tools/safe_workspace_init.py "${workspace}" || return
  harness_flow_work python3 tools/resource_check.py "${workspace}" || return
  harness_flow_work python3 tools/modules.py assemble || return

  # Both halves of the context decision happen here, after the modules are known and before anything
  # is rendered: the panel is the only place the resolved prompt graph is ever visible, and the file
  # it writes is what render_runtime.py composes against. An unattended launch prints the same screen
  # with --plain, so the log records the choices it applied instead of silently applying them.
  panel=(python3 tools/prompt_panel.py --root "${root}" --runtime-dir "${HARNESS_RUNTIME_DIR}")
  [[ "${non_interactive}" == true ]] && panel+=(--plain)
  harness_flow_step "${panel[@]}" || return

  # Asking for the keys the chosen lanes use is the last question of the launch, and it lives here
  # rather than beside the model picker because it needs the resolved plan: which credentials are
  # actually used is a fact about both lanes together.
  harness_flow_step python3 tools/render_runtime.py --root "${root}" --runtime-dir "${HARNESS_RUNTIME_DIR}" --resolved-env "${HARNESS_RESOLVED_BOOTSTRAP}" || return
  harness_flow_source "${HARNESS_RUNTIME_DIR}/runtime.env" || return
}

# A flow is the interactive launch's state machine and nothing else. An unattended launch asks
# nothing, so it has nothing to return to, and starting no flow leaves every step's own numbering,
# its short-circuits, and its side effects exactly as they were.
flow_live=false
if [[ "${non_interactive}" != true ]]; then
  "${flow[@]}" --steps "${flow_steps}" begin
  flow_live=true
  # The rail counts screens and only a step knows how many of them it has, which is after the
  # first one is already drawn. So the launcher asks every step that question up front, read-only,
  # in one process: tools/flow_survey.py answers with the same short-circuits the steps use, and a
  # launch with two questions on it says "1 of 2" on the first screen instead of numbering a step
  # nobody will ever see. A step it cannot answer is left off the line and keeps the default of
  # one screen, and a survey that fails outright is simply a rail with no forecast - hence the
  # suppressed output and the `|| true`, since nothing that only draws a progress bar may be able
  # to stop a launch.
  flow_counts=$(python3 tools/flow_survey.py --root "${root}" --runtime-dir "${HARNESS_RUNTIME_DIR}" \
    --steps "${flow_steps}" 2>/dev/null || true)
  [[ -n "${flow_counts}" ]] && "${flow[@]}" survey --counts "${flow_counts}" || true
fi

# The alternate screen is borrowed once for the whole interactive sequence rather than once per
# step, so the questions read as one modal rather than as a flicker around the launcher's own
# printout. `enter` declines - status 1, nothing written - when stdout is not a terminal, which
# leaves the steps to borrow and return the screen individually exactly as they always did.
# HARNESS_TUI_SCREEN is what tells a step's terminal that the window is already borrowed; see
# tools/tui/screen.py. The leave is owed on every way out of here, so cleanup() runs the same
# command and `screen.py` records the borrow to keep the second one silent.
screen=(python3 tools/tui/screen.py --runtime-dir "${HARNESS_RUNTIME_DIR}")
if [[ "${flow_live}" == true ]] && "${screen[@]}" enter; then
  export HARNESS_TUI_SCREEN=held
  screen_held=true
  # Whatever a killed launch left unsaid is not this launch's news, so the notes start empty.
  : >"${flow_notes}"
fi

pass_status=0
while :; do
  pass_status=0
  harness_flow_pass || pass_status=$?
  [[ "${pass_status}" == 0 ]] && break
  # A back-request cannot come from a launch with no flow to answer it, so any other status is a real
  # failure, and harness_flow_step has already said which.
  [[ "${flow_live}" == true && "${pass_status}" == "${flow_back}" ]] || exit "${pass_status}"
done

# The questions are over, so the terminal is handed back before anything is printed: the modules
# prepare, the image build and the version smoke test all speak in ordinary scrollback lines, and
# the recap belongs beside them rather than inside a screen that no longer has a key to leave it.
harness_flow_unhold

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

# The questions are over: this is the commit point, and the first line of it says so to the
# interactive sequence. Ending the flow here rather than only in cleanup() means a launch that goes
# on to fail during the build leaves no live state machine behind, and every answer below this line
# is durable state owned by state.env and last-*.json instead.
"${flow[@]}" end
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
  # A "--" is only portable in option position: BSD chmod reads one after the mode as a filename.
  mkdir -p -- "${cache}" && chmod 700 "${cache}"
  tmp=$(mktemp) || return 0
  for ((attempt = 1; attempt <= 20; attempt++)); do
    sleep 3
    if harness_compose exec -T kimi-agent python3 /opt/kimi-runtime/tools/kimi_prompts.py \
        --print --image "${PROMPT_IMAGE_ID:-unknown}" >"${tmp}" 2>>"${log}"; then
      if cp -- "${tmp}" "${cache}/literals.json" && chmod 600 "${cache}/literals.json"; then
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

# The staged prompt documents are installed read-only and immutable, so an edit to CONTEXT.md or
# SYSTEM.md cannot reach a session that is already running. Saying so at readiness is what keeps an
# operator from spending a session wondering why their change had no effect. Non-fatal by design:
# a notice about a prompt is never a reason to stop a working stack.
warn_stale_prompts() {
  PYTHONPATH="${root}/tools" python3 - "${root}" "${HARNESS_RUNTIME_DIR}" <<'PY' || true
import pathlib
import sys

import prompt_context

for notice in prompt_context.stale_sources(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])):
    print(notice)
PY
}

# Carry the checker's report out of the container's tmpfs. Silent when there is nothing to carry:
# the human-readable results have already been printed or logged by then, and a report is a copy of
# them rather than the only record.
collect_service_report() {
  local host="${HARNESS_RUNTIME_DIR}/${SERVICE_REPORT_NAME}"
  if harness_compose cp kimi-agent:"${SERVICE_REPORT_CONTAINER}" "${host}" >/dev/null 2>&1; then
    chmod 600 "${host}" 2>/dev/null || true
  fi
}

# The quick pass proves that ports answer and enabled servers initialise; the full pass is the one
# that calls a representative tool on each of them. Both run unattended on every launch now, and the
# deep one waits for a settled stack so it is not judging the container while it is still busy
# starting. Its verdict lands in the log and replaces the report; a launch is never failed by it.
deep_service_check() {
  local log="${HARNESS_RUNTIME_DIR}/service-check.log"
  : >"${log}" && chmod 600 "${log}"
  echo "Full service check starts in ${SERVICE_SETTLE_SECONDS}s." >>"${log}"
  sleep "${SERVICE_SETTLE_SECONDS}"
  if harness_compose exec -T kimi-agent /opt/serena/bin/python \
      /opt/kimi-runtime/tools/check_services.py --full \
      --report "${SERVICE_REPORT_CONTAINER}" >>"${log}" 2>&1; then
    echo "Full service check passed." >>"${log}"
  else
    echo "Full service check reported problems; see the results above." >>"${log}"
  fi
  collect_service_report
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
  warn_stale_prompts
  # Checked before the browser opens, on purpose: a readiness probe that competes with a first
  # session booting beside it can time out for reasons that say nothing about the service.
  if ! harness_compose exec -T kimi-agent /opt/serena/bin/python \
      /opt/kimi-runtime/tools/check_services.py --report "${SERVICE_REPORT_CONTAINER}"; then
    echo "Service checks need attention. The stack remains running; the results are above, and the full pass follows." >&2
  fi
  collect_service_report
  echo "Full service check runs in ${SERVICE_SETTLE_SECONDS}s; verdict: ${HARNESS_RUNTIME_DIR}/service-check.log"
  # Detached from the terminal on purpose: the job runs while the operator is already working, and
  # its value is the log and the report, not a block of text that lands in the middle of a reply.
  # Redirecting at the launch site is also what keeps a backgrounded sleep from holding the
  # launcher's own stdout open after the launch has finished.
  deep_service_check >/dev/null 2>&1 &
  DEEP_CHECK_PID=$!
  python3 tools/open_kimi_browser.py docker compose --env-file "${root}/.env" "${HARNESS_COMPOSE_FILES[@]}" || true
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
