"""Run inside an isolated agent image: interrupted tool calls must not execute.

Usage: python3 - < native_stream_recovery.py CONTEXT_WINDOW
The window is resolved from the shipped definitions by the caller; all traffic is loopback.
"""

import http.server
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import threading

home = pathlib.Path("/home/agent/.kimi-code")
if (home / "config.toml").exists():
    raise RuntimeError("This fixture requires an isolated, empty Kimi home")
home.mkdir(parents=True, exist_ok=True)
scratch = tempfile.TemporaryDirectory(prefix="kimi-stream-fixture-")
scratch_path = pathlib.Path(scratch.name)
marker = scratch_path / "tool-count"
requests = []


def frame(delta, reason=None):
    return (
        "data: "
        + json.dumps(
            {
                "id": "fixture-response",
                "object": "chat.completion.chunk",
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
        )
        + "\n\n"
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(data)
        has_result = any(m.get("role") == "tool" for m in data["messages"])
        if has_result:
            body = (
                frame({"content": "Recovery test completed."})
                + frame({}, "stop")
                + "data: [DONE]\n\n"
            ).encode()
        else:
            call = {
                "id": "fixture-tool-call",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": json.dumps(
                        {
                            "command": (
                                f"printf 'ran\\n' >> {shlex.quote(str(marker))}; "
                                "printf 'FIXTURE_TOOL_RAN\\n'"
                            ),
                            "description": "Local recovery fixture",
                        }
                    ),
                },
            }
            body = (
                frame({"tool_calls": [{"index": 0, **call}]}) + frame({}, "tool_calls")
            ).encode()
            if len(requests) > 1:
                body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body) + (100 if len(requests) == 1 else 0)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
(home / "config.toml").write_text(f"""default_model = "fixture"
default_permission_mode = "auto"
telemetry = false
auto_session_title = false
[providers.fixture]
type = "openai"
base_url = "http://127.0.0.1:{server.server_port}/v1"
api_key = "fixture-only"
[models.fixture]
provider = "fixture"
model = "fixture"
max_context_size = {int(sys.argv[1])}
[loop_control]
max_attempts_per_step = 3
""")
env = {
    **os.environ,
    "KIMI_CODE_HOME": str(home),
    "XDG_CACHE_HOME": str(scratch_path / "cache"),
    "XDG_CONFIG_HOME": str(scratch_path / "config"),
    "KIMI_CODE_INFINITE_RETRY": "1",
}
result = subprocess.run(
    [
        "/usr/local/bin/kimi",
        "--prompt",
        "Run the supplied local recovery test tool once and report completion.",
    ],
    cwd="/workspace",
    env=env,
    capture_output=True,
    text=True,
    timeout=90,
)
server.shutdown()
assert result.returncode == 0, result.stderr[-1600:]
assert marker.read_text().splitlines() == ["ran"], "Tool executed more than once"
assert len(requests) == 3, f"Unexpected model request count {len(requests)}"
assert not any(m.get("role") == "tool" for m in requests[1]["messages"]), (
    "Interrupted generation executed a tool"
)
assert sum(m.get("role") == "tool" for m in requests[2]["messages"]) == 1
print(
    json.dumps(
        {
            "unmodified_kimi": subprocess.check_output(
                ["/usr/local/bin/kimi", "--version"], text=True
            ).strip(),
            "interrupted_generation_tool_executions": 0,
            "recovered_generation_tool_executions": 1,
            "model_requests": len(requests),
            "test": "passed",
        }
    )
)
