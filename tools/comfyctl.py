#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("COMFYUI_URL", "").rstrip("/")
AUTH_TOKEN = os.environ.get("COMFYUI_TOKEN", "")
CONNECT_TIMEOUT = float(os.environ.get("COMFYUI_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.environ.get("COMFYUI_READ_TIMEOUT", "60"))


def require_base_url() -> str:
    if not BASE_URL:
        raise SystemExit("COMFYUI_URL is not configured")
    return BASE_URL


def headers() -> dict[str, str]:
    result = {"Accept": "application/json"}
    if AUTH_TOKEN:
        result["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return result


def open_request(request: urllib.request.Request, timeout: float | None = None):
    try:
        response = urllib.request.urlopen(
            request,
            timeout=timeout or CONNECT_TIMEOUT,
        )
        socket = getattr(
            getattr(getattr(response, "fp", None), "raw", None),
            "_sock",
            None,
        )
        if socket is not None:
            socket.settimeout(READ_TIMEOUT)
        return response
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} from {request.full_url}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Unable to reach {request.full_url}: {exc}") from exc


def request_json(method: str, path: str, payload: Any = None) -> Any:
    url = f"{require_base_url()}{path}"
    data = None
    request_headers = headers()
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    with open_request(request) as response:
        body = response.read()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON from {url}: {exc}") from exc


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    print()


def cmd_stats(_args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/system_stats"))


def cmd_schema(args: argparse.Namespace) -> None:
    node_class = urllib.parse.quote(args.node_class, safe="")
    print_json(request_json("GET", f"/object_info/{node_class}"))


def cmd_queue(_args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/queue"))


def load_workflow(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to load workflow {path}: {exc}") from exc
    if isinstance(data, dict) and "prompt" in data:
        return data
    if not isinstance(data, dict):
        raise SystemExit("Workflow must be a JSON object in ComfyUI API format")
    return {"prompt": data, "client_id": str(uuid.uuid4())}


def workflow_failure(record: dict[str, Any]) -> str:
    status = record.get("status", {})
    for message in status.get("messages", []):
        if isinstance(message, list) and message and message[0] == "execution_error":
            details = message[1] if len(message) > 1 else {}
            return f"ComfyUI execution error: {json.dumps(details, sort_keys=True)}"
    if status.get("status_str") == "error":
        return "ComfyUI marked the workflow as failed"
    return ""


def wait_for_prompt(prompt_id: str, timeout: float, interval: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    quoted = urllib.parse.quote(prompt_id, safe="")
    while time.monotonic() < deadline:
        result = request_json("GET", f"/history/{quoted}")
        if result:
            record = result.get(prompt_id, result)
            if isinstance(record, dict):
                failure = workflow_failure(record)
                if failure:
                    raise SystemExit(failure)
            return result
        time.sleep(interval)
    raise SystemExit(f"Timed out after {timeout}s waiting for prompt {prompt_id}")


def cmd_run(args: argparse.Namespace) -> None:
    result = request_json("POST", "/prompt", load_workflow(args.workflow))
    if result.get("error") or result.get("node_errors"):
        raise SystemExit(json.dumps(result, indent=2, sort_keys=True))
    if not args.wait:
        print_json(result)
        return
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise SystemExit(f"ComfyUI did not return a prompt_id: {result}")
    print_json(wait_for_prompt(prompt_id, args.timeout, args.interval))


def cmd_history(args: argparse.Namespace) -> None:
    prompt_id = urllib.parse.quote(args.prompt_id, safe="")
    print_json(request_json("GET", f"/history/{prompt_id}"))


def cmd_wait(args: argparse.Namespace) -> None:
    print_json(wait_for_prompt(args.prompt_id, args.timeout, args.interval))


def cmd_interrupt(_args: argparse.Namespace) -> None:
    print_json(request_json("POST", "/interrupt", {}))


def multipart_file(path: Path, field_name: str, extra: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----comfyctl-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in extra.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{path.name}"\r\n'
            ).encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def cmd_upload(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"Input file does not exist: {path}")
    body, boundary = multipart_file(
        path,
        "image",
        {
            "type": "input",
            "subfolder": args.subfolder,
            "overwrite": "true" if args.overwrite else "false",
        },
    )
    request_headers = headers()
    request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(
        f"{require_base_url()}/upload/image",
        data=body,
        headers=request_headers,
        method="POST",
    )
    with open_request(request) as response:
        print_json(json.loads(response.read()))


def output_files(record: dict[str, Any]):
    for node_id, node_output in record.get("outputs", {}).items():
        if not isinstance(node_output, dict):
            continue
        for output_kind, entries in node_output.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and "filename" in entry:
                    yield node_id, output_kind, entry


def cmd_download(args: argparse.Namespace) -> None:
    history = request_json(
        "GET", f"/history/{urllib.parse.quote(args.prompt_id, safe='')}"
    )
    record = history.get(args.prompt_id, history)
    if not isinstance(record, dict) or not record:
        raise SystemExit(f"No history found for prompt {args.prompt_id}")
    failure = workflow_failure(record)
    if failure:
        raise SystemExit(failure)

    destination = Path(args.directory)
    destination.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for node_id, output_kind, entry in output_files(record):
        filename = Path(str(entry["filename"])).name
        params = urllib.parse.urlencode(
            {
                "filename": entry["filename"],
                "subfolder": entry.get("subfolder", ""),
                "type": entry.get("type", "output"),
            }
        )
        request = urllib.request.Request(
            f"{require_base_url()}/view?{params}", headers=headers(), method="GET"
        )
        output_path = destination / f"{node_id}-{output_kind}-{filename}"
        with open_request(request) as response, output_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        downloaded.append(str(output_path))
    if not downloaded:
        raise SystemExit("Workflow history contains no downloadable outputs")
    print_json({"downloaded": downloaded})


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(required=True)

    command = sub.add_parser("stats")
    command.set_defaults(func=cmd_stats)

    command = sub.add_parser("schema")
    command.add_argument("node_class")
    command.set_defaults(func=cmd_schema)

    command = sub.add_parser("queue")
    command.set_defaults(func=cmd_queue)

    command = sub.add_parser("run")
    command.add_argument("workflow")
    command.add_argument("--wait", action="store_true")
    command.add_argument("--timeout", type=float, default=1800)
    command.add_argument("--interval", type=float, default=2)
    command.set_defaults(func=cmd_run)

    command = sub.add_parser("history")
    command.add_argument("prompt_id")
    command.set_defaults(func=cmd_history)

    command = sub.add_parser("wait")
    command.add_argument("prompt_id")
    command.add_argument("--timeout", type=float, default=1800)
    command.add_argument("--interval", type=float, default=2)
    command.set_defaults(func=cmd_wait)

    command = sub.add_parser("interrupt")
    command.set_defaults(func=cmd_interrupt)

    command = sub.add_parser("upload")
    command.add_argument("file")
    command.add_argument("--subfolder", default="")
    command.add_argument("--overwrite", action="store_true")
    command.set_defaults(func=cmd_upload)

    command = sub.add_parser("download")
    command.add_argument("prompt_id")
    command.add_argument("directory")
    command.set_defaults(func=cmd_download)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
