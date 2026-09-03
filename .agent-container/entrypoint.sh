#!/usr/bin/env bash
set -euo pipefail

: "${LITELLM_API_KEY:?LITELLM_API_KEY is required}"
: "${LITELLM_BASE_URL:?LITELLM_BASE_URL is required}"
: "${LITELLM_MODEL_ID:?LITELLM_MODEL_ID is required}"
: "${SAFE_CONTEXT:?SAFE_CONTEXT is required}"

mkdir -p "${HOME}/.kimi"
chmod 700 "${HOME}/.kimi"

# Generate the credential-bearing config INSIDE the isolated container.
# Nothing containing the API key is written into the repository.
python3 - <<'PY'
import os
from pathlib import Path

def toml_string(value: str) -> str:
    """Escape a string safely enough for a TOML basic string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

base_url = os.environ["LITELLM_BASE_URL"]
api_key = os.environ["LITELLM_API_KEY"]
model_id = os.environ["LITELLM_MODEL_ID"]

try:
    safe_context = int(os.environ["SAFE_CONTEXT"])
except ValueError as exc:
    raise SystemExit("SAFE_CONTEXT must be an integer") from exc

if safe_context < 8192:
    raise SystemExit("SAFE_CONTEXT looks implausibly small")

config = f"""
[providers.ou_litellm]
type = "openai_legacy"
base_url = {toml_string(base_url)}
api_key = {toml_string(api_key)}

[models.primary]
provider = "ou_litellm"
model = {toml_string(model_id)}
max_context_size = {safe_context}

[loop_control]
max_steps_per_turn = 1000
max_retries_per_step = 3

# Keep some generation headroom.
reserved_context_size = 4096

# Because max_context_size is already a deliberately reduced logical
# window, this triggers at about 30.4% of the REAL model context when
# SAFE_CONTEXT was calculated as 32% of the physical context.
compaction_trigger_ratio = 0.95

[background]
# Root + 5 possible background agents = no more than 6 model requests.
max_running_tasks = 5

# Allow a bounded subagent to work for up to an hour.
agent_task_timeout_s = 3600
"""

path = Path.home() / ".kimi" / "config.toml"
path.write_text(config, encoding="utf-8")
path.chmod(0o600)
PY

exec "$@"
