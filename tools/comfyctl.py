#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


BASE_URL = os.environ.get("COMFYUI_URL", "").rstrip("/")


def require_base_url() -> str:
    if not BASE_URL:
        raise SystemExit("COMFYUI_URL is not configured")
    return BASE_URL


def request(method: str, path: str, payload=None):
    base = require_base_url()
    url = f"{base}{path}"

    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"HTTP {exc.code} from {url}\n{body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Unable to reach {url}: {exc}") from exc

    if not body:
        return {}

    return json.loads(body)


def print_json(value):
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    print()


def cmd_stats(_args):
    print_json(request("GET", "/system_stats"))


def cmd_schema(args):
    node_class = urllib.parse.quote(args.node_class, safe="")
    print_json(request("GET", f"/object_info/{node_class}"))


def load_workflow(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # Accept either raw API-format workflow or a wrapper containing "prompt".
    if isinstance(data, dict) and "prompt" in data:
        return data

    return {
        "prompt": data,
        "client_id": str(uuid.uuid4()),
    }


def cmd_run(args):
    payload = load_workflow(args.workflow)
    result = request("POST", "/prompt", payload)
    print_json(result)


def cmd_history(args):
    prompt_id = urllib.parse.quote(args.prompt_id, safe="")
    print_json(request("GET", f"/history/{prompt_id}"))


def cmd_wait(args):
    prompt_id = args.prompt_id
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        quoted = urllib.parse.quote(prompt_id, safe="")
        result = request("GET", f"/history/{quoted}")

        if result:
            print_json(result)
            return

        time.sleep(args.interval)

    raise SystemExit(
        f"Timed out after {args.timeout}s waiting for prompt {prompt_id}"
    )


def cmd_interrupt(_args):
    print_json(request("POST", "/interrupt", {}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("schema")
    p.add_argument("node_class")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("run")
    p.add_argument("workflow")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("history")
    p.add_argument("prompt_id")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("wait")
    p.add_argument("prompt_id")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--interval", type=float, default=2)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("interrupt")
    p.set_defaults(func=cmd_interrupt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
