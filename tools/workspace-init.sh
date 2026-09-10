#!/usr/bin/env bash
set -euo pipefail

workspace="${1:?usage: workspace-init.sh /path/to/workspace}"

mkdir -p "${workspace}/.agent-state/logs"

touch \
  "${workspace}/.agent-state/STATE.md" \
  "${workspace}/.agent-state/DEBUG_LEDGER.md" \
  "${workspace}/.agent-state/TENSOR_CONTRACTS.md" \
  "${workspace}/.agent-state/UPSTREAM_SOURCES.md" \
  "${workspace}/.agent-state/BENCHMARKS.jsonl"

if git -C "${workspace}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exclude="$(git -C "${workspace}" rev-parse --git-path info/exclude)"
    mkdir -p "$(dirname "${exclude}")"

    for pattern in \
        ".agent-state/" \
        ".playwright-cli/" \
        ".serena/"
    do
        grep -Fxq "${pattern}" "${exclude}" 2>/dev/null \
            || echo "${pattern}" >> "${exclude}"
    done
fi

if [[ ! -s "${workspace}/.agent-state/STATE.md" ]]; then
cat > "${workspace}/.agent-state/STATE.md" <<'EOF'
# Agent State

## Objective

## Current failure / work item

## Important requirements

## Last known-good state

## Minimal reproduction

## Current evidence

## Active hypothesis

## Eliminated hypotheses

## Important files

## Upstream references

## Commands/tests already run

## Next three experiments
1.
2.
3.
EOF
fi

echo "Initialized agent state in ${workspace}"
