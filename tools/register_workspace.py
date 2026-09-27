#!/usr/bin/env python3
"""Get a freshly started agent container ready for a session.

Two things, both of them only meaningful from inside the container.

First, register the sandbox workspace through Kimi's authenticated local API. Kimi Code 0.43.1:
POST /api/v1/workspaces is idempotent on root. The web client uses the first visible workspace
unless it restores another selection. Keep the server credential inside the container and out of
diagnostics.

Second, lay out the agent's scratch directory. It is deliberately not in /workspace: the project
tree belongs to whoever checked it out, and working memory that outlives its session becomes a junk
drawer nobody recognises. /tmp is the container's own tmpfs, so the directory is empty at the start
of every launch by construction and disappears entirely when the container does. The layout is the
one the operating contract names, so an agent can append to a known file instead of inventing a
name under pressure.
"""

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Not a mktemp target: this is the fixed path of the tmpfs the agent container already mounts
# at /tmp, so the scratch is per-launch by construction and never visible on a shared host path.
AGENT_STATE_DIR = Path("/tmp/agent-state")  # noqa: S108
STATE_FILE = "STATE.md"
LOGS_DIR = "logs"
LEDGER_FILES = (
    "DEBUG_LEDGER.md",
    "TENSOR_CONTRACTS.md",
    "UPSTREAM_SOURCES.md",
    "BENCHMARKS.jsonl",
)
STATE_TEMPLATE = """# Agent State

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
"""


def prepare_agent_state(directory: Path) -> str:
    """Rebuild the scratch directory from empty, and say what it ended up holding.

    Removal is unconditional because that is the point: whatever a previous session left in here
    is not this session's context, and /tmp survives a container that is restarted without being
    recreated. A path that is not a plain directory - a link, a file - is replaced rather than
    followed.
    """
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        directory.unlink()
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    (directory / STATE_FILE).write_text(STATE_TEMPLATE, encoding="utf-8")
    (directory / "logs").mkdir(mode=0o700, exist_ok=True)
    for name in LEDGER_FILES:
        directory.joinpath(name).touch()
    return str(directory)


def register_workspace():
    home = Path(os.environ.get("KIMI_CODE_HOME", "/home/agent/.kimi-code"))
    token = (home / "server.token").read_text().strip()
    if not token:
        raise ValueError("Missing server credential")
    request = urllib.request.Request(
        "http://127.0.0.1:5494/api/v1/workspaces",
        data=json.dumps({"root": "/workspace"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    # Never send the local credential through an environment-configured proxy
    # or follow a redirect to another endpoint.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("code") != 0:
        raise ValueError("Registration rejected")
    workspace = result.get("data")
    if (
        not isinstance(workspace, dict)
        or workspace.get("root") != "/workspace"
        or not workspace.get("id")
    ):
        raise ValueError("Unexpected registration response")


def main():
    # First and separately tolerated: a scratch directory that cannot be built costs an agent its
    # working memory, not its stack, so this reports and carries on. Registration failing is a
    # different matter - without a registered workspace the UI has nothing to open - so that one
    # still decides the exit status. The variable exists for tests; no launch sets it, and the
    # operating contract names the default path, so changing one without the other is the failure
    # this sentence is here to prevent.
    directory = Path(os.environ.get("KIMI_AGENT_STATE_DIR", str(AGENT_STATE_DIR)))
    try:
        prepared = prepare_agent_state(directory)
    except OSError:
        prepared = ""
        print(f"Could not prepare {directory}; the agent will make its own.", file=sys.stderr)
    try:
        register_workspace()
    except (OSError, ValueError, urllib.error.URLError):
        # Do not print response bodies or exception details: they may contain
        # credentials or private server state.
        print(
            "Could not register /workspace with Kimi. "
            "Check server readiness and API compatibility.",
            file=sys.stderr,
        )
        return 1
    print("Registered /workspace with Kimi.")
    if prepared:
        print(f"Prepared agent scratch in {prepared}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
