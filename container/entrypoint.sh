#!/usr/bin/env bash
set -euo pipefail

: "${KIMI_MODEL_NAME:?KIMI_MODEL_NAME is required}"
: "${KIMI_MODEL_API_KEY:?KIMI_MODEL_API_KEY is required}"
: "${KIMI_MODEL_BASE_URL:?KIMI_MODEL_BASE_URL is required}"
: "${KIMI_MODEL_MAX_CONTEXT_SIZE:?KIMI_MODEL_MAX_CONTEXT_SIZE is required}"

case "${KIMI_MODEL_MAX_CONTEXT_SIZE}" in
  ''|*[!0-9]*)
    echo "KIMI_MODEL_MAX_CONTEXT_SIZE must be an integer" >&2
    exit 1
    ;;
esac

if (( KIMI_MODEL_MAX_CONTEXT_SIZE < 8192 )); then
    echo "KIMI_MODEL_MAX_CONTEXT_SIZE is implausibly small" >&2
    exit 1
fi

mkdir -p "${KIMI_CODE_HOME}"

exec "$@"
