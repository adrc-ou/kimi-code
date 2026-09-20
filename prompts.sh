#!/usr/bin/env bash
# Show, choose, and verify the context this harness puts in front of Kimi.
#
# One screen per question, and the same code behind every answer: the launch panel draws the
# graph, so this script never restates it. It exists for the moments outside a launch - "what is
# in my context window right now", "which placeholders may I use", "set my choices from a
# script" - and for the two jobs that need a running stack and cannot wait for the automatic one.
#
# Read-mostly by design. Only --configure and --extract write, and both write inside the
# instance runtime directory. This script never takes the launcher lock, so it is safe to run in
# a second terminal while a session is live.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: ./prompts.sh [--show] [--vars] [--configure [options]] [--live] [--extract]

  --show       the launch panel, drawn once: every prompt, its source file, its token cost
  --vars       every placeholder a prompt file may hold, who resolves it, and its conditions
  --configure  set the launch panel's choices without a screen
                 --enable ID --disable ID    toggle named options
                 --all-on | --all-off        reset the whole list
                 --show                      print the current selection and change nothing
  --live       the prompt actually sent, read from the running stack and measured exactly
  --extract    refresh Kimi's built-in prompt literals from the running stack's bundle

Valid option ids are printed by --configure --show and beside each box in --show.
EOF
}

case "${1:-}" in
  ""|--show) mode=show ;;
  --vars) mode=vars ;;
  --configure) mode=configure ;;
  --live) mode=live ;;
  --extract) mode=extract ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac
[[ "${mode}" == configure ]] || [[ $# -le 1 ]] || { usage; exit 2; }
shift || true

root=$(cd "$(dirname "$0")" && pwd -P)
cd "${root}"
# shellcheck disable=SC1091
source tools/runtime.sh
harness_traps
harness_init_readonly

# The panel reads the resolved policy plan for every figure it draws, and the plan is only written
# by render_runtime.py during a launch. Before that there is nothing honest to show.
[[ -f "${HARNESS_RUNTIME_DIR}/model-policy.json" ]] || {
  echo "No prepared runtime. Run ./start.sh first." >&2
  exit 1
}

# The two container-backed jobs share one precondition, checked by label rather than by reading the
# compose files, so this script never has to open the credential-bearing resolved configuration.
require_running_stack() {
  local container
  container=$(docker ps --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter label=com.docker.compose.service=kimi-agent --format '{{.ID}}')
  [[ -n "${container}" && "${container}" != *$'\n'* ]] || {
    echo "The agent container is not running. Run ./start.sh first." >&2
    exit 1
  }
}

# Same transport as the automatic job in start.sh, so a hand-run refresh and an unattended one
# produce a byte-identical cache file. The bundle only exists inside the image, hence exec.
extract_literals() {
  local cache="${HARNESS_RUNTIME_DIR}/kimi-prompts"
  local tmp status=0 image
  mkdir -p -- "${cache}" && chmod 700 -- "${cache}"
  tmp=$(mktemp) || exit 1
  image=$(harness_prompt_image_id)
  harness_compose exec -T kimi-agent python3 /opt/kimi-runtime/tools/kimi_prompts.py \
    --print --image "${image:-unknown}" >"${tmp}" || status=$?
  if [[ ${status} -eq 0 ]]; then
    cp -- "${tmp}" "${cache}/literals.json" && chmod 600 -- "${cache}/literals.json" || status=$?
  else
    echo "the container did not extract a bundle; see the error above" >&2
  fi
  rm -f -- "${tmp}"
  return "${status}"
}

case "${mode}" in
  extract)
    harness_compose_files
    require_running_stack
    extract_literals
    ;;
  live)
    harness_compose_files
    require_running_stack
    # The copied home holds conversation text, so it is staged under the instance directory,
    # removed on the way in, and removed again whatever the report returns.
    staging="${HARNESS_RUNTIME_DIR}/prompt-sessions"
    status=0
    rm -rf -- "${staging}"
    if harness_compose cp kimi-agent:/home/agent/.kimi-code "${staging}"; then
      python3 tools/prompt_measure.py \
        --report \
        --sessions-dir "${staging}" \
        --plan "${HARNESS_RUNTIME_DIR}/model-policy.json" \
        --history "${HARNESS_RUNTIME_DIR}/prompt-measurements.jsonl" || status=$?
    else
      echo "the container would not give up its agent home; is the session still starting?" >&2
      status=1
    fi
    rm -rf -- "${staging}"
    exit "${status}"
    ;;
  configure)
    python3 tools/prompt_panel.py --runtime-dir "${HARNESS_RUNTIME_DIR}" --root "${root}" \
      --configure "$@"
    ;;
  vars)
    python3 tools/prompt_panel.py --runtime-dir "${HARNESS_RUNTIME_DIR}" --root "${root}" --vars
    ;;
  show)
    python3 tools/prompt_panel.py --runtime-dir "${HARNESS_RUNTIME_DIR}" --root "${root}" \
      --plain --show
    ;;
esac
