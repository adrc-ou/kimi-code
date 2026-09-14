#!/usr/bin/env python3
"""Bounded, credential-redacted service/MCP probes, run inside kimi-agent.

Use /opt/serena/bin/python: its pinned environment already contains the MCP SDK.
No model inference, GPU jobs, or arbitrary discovered tool calls are performed.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path

CONFIG = Path("/home/agent/.kimi-code/mcp.json")
SMOKE_CALLS = {
    "chrome-devtools": ("list_pages", {}),
    "serena": ("get_current_config", {}),
    "github": ("get_me", {}),
    "deepwiki": ("read_wiki_structure", {"repoName": "python/cpython"}),
    "context7": (
        "resolve-library-id", {"libraryName": "python", "query": "Python standard library"}
    ),
    "huggingface": (
        "hf_fs", {"operations": [
            {"cmd": "stat", "args": ["hf://models/openai-community/gpt2/README.md"]}
        ]}
    ),
}


class NeedsSetup(Exception):
    """A reachable server needs operator configuration before it is useful."""


def classify_error(error):
    # Async MCP transports wrap tool errors in ExceptionGroup on context exit.
    if isinstance(error, BaseExceptionGroup):
        results = [classify_error(child) for child in error.exceptions]
        return next((result for result in results if result[0] == "SETUP"), results[0])
    if isinstance(error, NeedsSetup):
        return "SETUP", "Serena needs an active coding project; see verification guide"
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return "FAIL", f"HTTP {status}; check credentials, rate limits and /mcp"
    return "FAIL", "connection, credentials, schema or tool check failed; see /mcp"


def selected_tools(config, tools):
    names = {tool.name for tool in tools}
    allow = config.get("enabledTools")
    if allow is not None and set(allow) - names:
        raise ValueError("configured tool allowlist is missing from server")
    return (names if allow is None else names & set(allow)) - set(config.get("disabledTools", []))


async def mcp_probe(name, config, full):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    # Never print server logs, responses, URLs, headers, or exception messages.
    logging.disable(logging.CRITICAL)
    async with AsyncExitStack() as stack:
        if "command" in config:
            errlog = stack.enter_context(open(os.devnull, "w"))
            params = StdioServerParameters(
                command=config["command"], args=config.get("args", []),
                cwd=config.get("cwd", "/workspace"),
                env={**os.environ, **config.get("env", {})},
            )
            streams = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        else:
            headers = dict(config.get("headers", {}))
            token = os.environ.get(config.get("bearerTokenEnvVar", ""), "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            transport = sse_client if config.get("transport") == "sse" else streamablehttp_client
            streams = await stack.enter_async_context(transport(config["url"], headers=headers))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        tools = []
        cursor = None
        for _ in range(50):
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        else:
            raise ValueError("tool catalog pagination exceeded limit")
        names = selected_tools(config, tools)
        if not names:
            raise ValueError("no usable tools")
        detail = f"MCP initialized; {len(names)} configured tools discovered"
        # Starting Chromium catches failures that MCP discovery alone cannot.
        call = SMOKE_CALLS.get(name) if full or name in {"chrome-devtools", "serena"} else None
        if call and call[0] in names:
            result = await session.call_tool(*call)
            if result.isError:
                if name == "serena" and any(
                    "No active project" in getattr(item, "text", "") for item in result.content
                ):
                    raise NeedsSetup("activate a coding project in Serena before symbol tools")
                raise ValueError("smoke tool returned an error")
            if name == "huggingface":
                records = (result.structuredContent or {}).get("results", [])
                if not records or not all(
                    record.get("status") == "success" and record.get("result", {}).get("exists")
                    for record in records
                ):
                    raise ValueError("public Hugging Face fixture not found")
            detail += f"; {call[0]} succeeded"
        elif full:
            detail += "; no automatic tool-call recipe (verify in Kimi)"
        return detail


def service_probe(name, full):
    import urllib.request

    if name == "local-tools":
        import shutil

        for command in ("bash", "git", "rg", "python3", "kimi", "playwright-cli"):
            if not shutil.which(command):
                raise ValueError("required local executable is missing")
        subprocess.run(["playwright-cli", "--version"], check=True, capture_output=True, timeout=10)
        return "shell, Git, ripgrep, Python and Kimi present; Playwright CLI starts"
    if name == "comfyui":
        # Exercise the same TLS, bearer auth and API helper used by the agent.
        import comfyctl

        stats = comfyctl.request_json("GET", "/system_stats")
        if not isinstance(stats, dict) or "system" not in stats:
            raise ValueError("invalid ComfyUI stats")
        if full:
            schema = comfyctl.request_json("GET", "/object_info/EmptyImage")
            if "EmptyImage" not in schema:
                raise ValueError("missing EmptyImage node")
        return "authenticated stats" + (" and node schema" if full else "")
    urls = {
        "kimi-web": "http://127.0.0.1:5494/",
        "model-proxy": "http://model-proxy:8080/healthz",
        "search": "http://search-adapter:8080/healthz",
    }
    request = urllib.request.Request(urls[name])
    if name == "search" and full:
        request = urllib.request.Request(
            os.environ["KIMI_WEB_SEARCH_BASE_URL"],
            data=json.dumps({"text_query": "Python official documentation"}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ['KIMI_WEB_SEARCH_API_KEY']}"},
        )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(8 * 1024 * 1024)
    if name == "search" and full:
        if not json.loads(payload).get("search_results"):
            raise ValueError("search returned no results")
        return "authenticated search returned results through SearXNG"
    return "HTTP ready" + ("; upstream inference not tested" if name == "model-proxy" else "")


def run_probe(kind, name, full, config_path, timeout):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", kind,
               "--name", name, "--config", str(config_path)]
    if full:
        command.append("--full")
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode:
            return "FAIL", "probe process failed; see verification guide"
        return tuple(json.loads(output))
    except subprocess.TimeoutExpired:
        return "FAIL", f"timed out after {timeout}s; check /mcp and service logs"
    except (ValueError, TypeError):
        return "FAIL", "invalid probe response"
    finally:
        # Also reap browser/language-server descendants left behind by a probe.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--worker", choices=("mcp", "service"), help=argparse.SUPPRESS)
    parser.add_argument("--name", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            if args.worker == "mcp":
                config = json.loads(args.config.read_text())["mcpServers"][args.name]
                detail = asyncio.run(mcp_probe(args.name, config, args.full))
            else:
                detail = service_probe(args.name, args.full)
            result = ("PASS", detail)
        except (Exception, SystemExit) as error:
            result = classify_error(error)
        print(json.dumps(result))
        return 0

    config = json.loads(args.config.read_text())["mcpServers"]
    jobs = [("service", name) for name in (
        "kimi-web", "model-proxy", "search", "comfyui", "local-tools"
    )]
    print("Service check (full)" if args.full else "Service check (quick)", flush=True)
    manual = False
    for name, server in config.items():
        if not server.get("enabled", True):
            print(f"SKIP  MCP {name}: disabled", flush=True)
        elif server.get("auth") == "oauth" or name == "nvidia-cuda-docs":
            print(f"MANUAL MCP {name}: verify OAuth connection in Kimi /mcp", flush=True)
            manual = True
        else:
            jobs.append(("mcp", name))
    failed = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        pending = {
            pool.submit(run_probe, kind, name, args.full, args.config, 60 if args.full else 25):
            (kind, name) for kind, name in jobs
        }
        for future in concurrent.futures.as_completed(pending):
            kind, name = pending[future]
            status, detail = future.result()
            failed |= status == "FAIL"
            manual |= status == "SETUP"
            print(f"{status:5} {kind} {name}: {detail}", flush=True)
    print("Scope: harness MCP config only; project/plugin tools and model routing need Kimi checks.")
    return 1 if failed else 2 if manual else 0


if __name__ == "__main__":
    raise SystemExit(main())
