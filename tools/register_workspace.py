#!/usr/bin/env python3
"""Register the sandbox workspace through Kimi's authenticated local API.

Kimi Code 0.43.1: POST /api/v1/workspaces is idempotent on root. The web
client uses the first visible workspace unless it restores another selection.
Keep the server credential inside the container and out of diagnostics.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


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
    if not isinstance(workspace, dict) or workspace.get("root") != "/workspace" or not workspace.get("id"):
        raise ValueError("Unexpected registration response")


def main():
    try:
        register_workspace()
    except (OSError, ValueError, urllib.error.URLError):
        # Do not print response bodies or exception details: they may contain
        # credentials or private server state.
        print("Could not register /workspace with Kimi. Check server readiness and API compatibility.", file=sys.stderr)
        return 1
    print("Registered /workspace with Kimi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
