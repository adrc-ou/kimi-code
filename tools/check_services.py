#!/usr/bin/env python3
"""Bounded, credential-redacted service/MCP probes, run inside kimi-agent.

Use /opt/serena/bin/python: its pinned environment already contains the MCP SDK.
No model inference, GPU jobs, or arbitrary discovered tool calls are performed.
``--report PATH`` records the same conclusions as JSON for the launcher to keep.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import signal
import subprocess
import sys
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path("/home/agent/.kimi-code/mcp.json")
# One retry, and only for a timeout. See run_probe.
PROBE_ATTEMPTS = 2
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


class LanguageServerUnavailable(Exception):
    """Serena answered, but none of the language servers behind its symbol tools are running."""


LANGUAGE_SERVER_STATUS = re.compile(r"^Language server status: (.*)$", re.MULTILINE)


class UnmountedBearerVariable(Exception):
    """Kimi will not mount a server whose declared bearer variable is empty.

    Only the variable's name is carried, never its value, so the verdict stays reportable.
    """

    def __init__(self, variable):
        super().__init__(variable)
        self.variable = variable


def unmounted_bearer_variable(config, env):
    """Return the bearer variable that Kimi fails closed on, or ``None`` if there is none.

    Kimi's remote MCP client rejects a server whose ``bearerTokenEnvVar`` names a variable that is
    unset or empty, so that server never reaches a session at all. A probe cannot see this on its
    own: connecting without the header still succeeds against services that accept anonymous calls,
    which is how a server nobody can call gets reported as working.
    """
    variable = config.get("bearerTokenEnvVar")
    if variable and not env.get(variable, "").strip():
        return variable
    return None


def serena_language_server(text):
    """Return Serena's language server status, refusing to report success without one.

    ``get_current_config`` is a successful call whether or not any language server started, so
    the status line inside its text is the only part of it that a symbol tool depends on. The
    status is ``ready``, ``not initialized``, or ``error (<reason>)``; the reason is Serena's own
    exception text, which is never surfaced here.
    """
    match = LANGUAGE_SERVER_STATUS.search(text)
    if match is None:
        raise NeedsSetup("activate a coding project in Serena before symbol tools")
    status = match.group(1).strip()
    if status.startswith("error"):
        raise LanguageServerUnavailable(status)
    if status != "ready":
        raise NeedsSetup(f"Serena language server is {status}")
    return status


def classify_error(error):
    # Async MCP transports wrap tool errors in ExceptionGroup on context exit.
    if isinstance(error, BaseExceptionGroup):
        results = [classify_error(child) for child in error.exceptions]
        if not results:  # an empty group reports no cause, which is a failure, not a setup
            return "FAIL", "MCP transport failed without reporting a cause"
        return next((result for result in results if result[0] == "SETUP"), results[0])
    if isinstance(error, LanguageServerUnavailable):
        return "FAIL", (
            "no language server running, so symbol tools are unavailable; check that the image "
            "builds pyright-langserver and that ls_path in runtime/serena-config.yml names it"
        )
    if isinstance(error, UnmountedBearerVariable):
        return "SETUP", (
            f"reachable, but Kimi will not mount it: bearer variable {error.variable} is unset or "
            "empty, so a session gets no tools from it; give it a value or remove "
            "bearerTokenEnvVar and use the server anonymously"
        )
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
        # Discovery above is anonymous, so it only proves the server answers. Kimi applies its own
        # gate when loading the session, and a server it refuses to mount has no tools to report.
        unmounted = unmounted_bearer_variable(config, os.environ)
        if unmounted:
            raise UnmountedBearerVariable(unmounted)
        # A server that answers its own tool calls is not yet one that can do its job: Chromium
        # has to actually start, and Serena's language servers have to have come up.
        call = SMOKE_CALLS.get(name) if full or name in {"chrome-devtools", "serena"} else None
        if call and call[0] in names:
            result = await session.call_tool(*call)
            if result.isError:
                if name == "serena" and any(
                    "No active project" in getattr(item, "text", "") for item in result.content
                ):
                    raise NeedsSetup("activate a coding project in Serena before symbol tools")
                raise ValueError("smoke tool returned an error")
            if name == "serena":
                # get_current_config answers normally even when no language server ever started,
                # and it is the language servers that every symbol tool depends on.
                detail += "; language server " + serena_language_server(
                    "\n".join(getattr(item, "text", "") for item in result.content)
                )
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
    probe = Path(__file__).parent / f"service_{name}.py"
    if probe.is_file():
        import runpy
        return runpy.run_path(str(probe))["probe"](full)
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
    if name == "model-proxy":
        # A 200 alone would hide a proxy that has stopped enforcing policy.
        report = json.loads(payload)
        if not report.get("policy_enforced"):
            raise ValueError(f"policy not enforced: {report.get('policy_error', 'unknown')}")
        budgets = sorted(
            {
                counter["context_budget"]
                for counter in (report.get("counters") or {}).values()
                if isinstance(counter.get("context_budget"), int)
            }
        )
        rates = sorted(
            {
                rate["capacity"]
                for rate in (report.get("rates") or {}).values()
                if isinstance(rate.get("capacity"), int)
            }
        )
        summary = (
            f"{len(report.get('lanes', {}))} lanes; "
            f"{report.get('subagent_limit')} subagent permits; "
            f"context budget {'/'.join(map(str, budgets)) or 'none'}; "
            f"rate {len(rates)} counter(s)"
        )
        return "policy enforced (" + summary + ")" + (
            "; upstream inference not tested" if full else ""
        )
    return "HTTP ready"


def run_probe(kind, name, full, config_path, timeout):
    """Run one probe in its own process group, retrying once if it never answers.

    A timeout is the one result that cannot be told apart from a cold start: while the stack comes
    up, the agent container is also starting Chromium, the language servers and the first session,
    and a probe that could not get a child scheduled has not yet shown anything about its server.
    Only that case pays for a second bounded window. A probe that answers with a failure, a bad
    status or an unusable response is reported the moment it speaks.
    """
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", kind,
               "--name", name, "--config", str(config_path)]
    if full:
        command.append("--full")
    for attempt in range(PROBE_ATTEMPTS):
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
            if process.returncode:
                return "FAIL", "probe process failed; see verification guide"
            status, detail = json.loads(output)
            if attempt:
                detail = f"{detail}; retried after a timeout"
            return status, detail
        except subprocess.TimeoutExpired:
            continue
        except (ValueError, TypeError):
            return "FAIL", "invalid probe response"
        finally:
            # Also reap browser/language-server descendants left behind by a probe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    return "FAIL", (
        f"timed out after {timeout}s on each of {PROBE_ATTEMPTS} attempts; "
        "check /mcp and service logs"
    )


def write_report(path, mode, results, code):
    """Record what this pass concluded, so the verdict outlives the terminal that printed it.

    The launcher copies this file to the host, which is the only way a result stays readable after
    the stack is down; nobody has to rerun a check to find out what the last launch concluded. The
    detail strings are the redacted ones the probes and classify_error already produce: never a
    response body, a credential, or a path from inside a server's own error text.
    """
    if __package__:
        from .private_file import write_private_json
    else:
        from private_file import write_private_json
    write_private_json(path, {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "exit_code": code,
        "counts": dict(Counter(result["status"] for result in results)),
        "checks": sorted(results, key=lambda result: (result["kind"], result["name"])),
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--report", type=Path,
                        help="write a machine-readable summary of this pass to PATH")
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
        "kimi-web", "model-proxy", "search", "local-tools"
    )]
    jobs.extend(("service", path.stem.removeprefix("service_"))
                for path in Path(__file__).parent.glob("service_*.py"))
    print("Service check (full)" if args.full else "Service check (quick)", flush=True)
    manual = False
    results = []
    for name, server in config.items():
        if not server.get("enabled", True):
            print(f"SKIP  MCP {name}: disabled", flush=True)
            results.append({"kind": "mcp", "name": name, "status": "SKIP",
                            "detail": "server is disabled"})
        elif server.get("auth") == "oauth" or name == "nvidia-cuda-docs":
            print(f"MANUAL MCP {name}: verify OAuth connection in Kimi /mcp", flush=True)
            manual = True
            results.append({"kind": "mcp", "name": name, "status": "MANUAL",
                            "detail": "OAuth connection is checked in Kimi"})
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
            results.append({"kind": kind, "name": name, "status": status, "detail": detail})
            print(f"{status:5} {kind} {name}: {detail}", flush=True)
    print("Scope: harness MCP config only; project/plugin tools + model routing need Kimi checks.")
    code = 1 if failed else 2 if manual else 0
    if args.report:
        write_report(args.report, "full" if args.full else "quick", results, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
