#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
import re
import ssl
import sys
import time
import unicodedata
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
TOTAL_TIMEOUT = float(os.environ.get("COMFYUI_TOTAL_TIMEOUT", "1800"))
MAX_JSON_BYTES = int(os.environ.get("COMFYUI_MAX_JSON_BYTES", str(16 * 1024 * 1024)))
MAX_TRANSFER_BYTES = int(os.environ.get("COMFYUI_MAX_TRANSFER_BYTES", str(16 * 1024**3)))
CA_FILE = os.environ.get("COMFYUI_CA_FILE", "")
#: The origin a browser is meant to open, which is not necessarily the API origin this client
#: talks to: on an MPS host they are two planes of the same ComfyUI.
UI_URL = os.environ.get("COMFYUI_UI_URL", "").rstrip("/")
#: The bridge's frontend routes. The bridge owns these names; they are repeated here because this
#: client runs in another container and cannot import it. tests/test_comfyctl.py keeps them equal.
GRANT_PATH = "/__bridge/grant"
SESSION_PATH = "/__bridge/session"


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
        context = ssl.create_default_context(cafile=CA_FILE or None)
        response = urllib.request.urlopen(
            request,
            timeout=timeout or CONNECT_TIMEOUT,
            context=context,
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
        try:
            body = exc.read(64 * 1024).decode("utf-8", errors="replace")
        finally:
            exc.close()
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
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_JSON_BYTES:
            raise SystemExit(f"JSON response from {url} exceeds the configured limit")
        body = response.read(MAX_JSON_BYTES + 1)
    if len(body) > MAX_JSON_BYTES:
        raise SystemExit(f"JSON response from {url} exceeds the configured limit")
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


def cmd_ui_url(_args: argparse.Namespace) -> None:
    """Print one URL that opens ComfyUI's frontend in a browser, and nothing else.

    A browser cannot attach an ``Authorization`` header to a navigation, so this asks the bridge for
    a single-use grant while holding the bearer token, and hands back a link carrying it in the
    fragment - the one part of a URL that is never transmitted to a server, written to a log, or
    repeated in a Referer. The token itself stays in this process's headers and never reaches an
    address bar, a history entry, or a transcript.
    """
    origin = UI_URL or require_base_url()
    if not AUTH_TOKEN:
        # A frontend that asks for no credential: the origin is the whole answer.
        print(origin)
        return
    link = request_json("POST", GRANT_PATH)
    path = link.get("path") if isinstance(link, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        raise SystemExit("the bridge did not offer a frontend link")
    print(f"{origin}{path}")


def cmd_interrupt(_args: argparse.Namespace) -> None:
    print_json(request_json("POST", "/interrupt", {}))


def multipart_file(path: Path, field_name: str, extra: dict[str, str]):
    boundary = f"----comfyctl-{uuid.uuid4().hex}"
    prefix: list[bytes] = []
    for name, value in extra.items():
        prefix.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    prefix.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'
            ).encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
        ]
    )
    suffix = b"\r\n" + f"--{boundary}--\r\n".encode()
    prefix_bytes = b"".join(prefix)

    def content():
        yield prefix_bytes
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk
        yield suffix

    return content(), boundary, len(prefix_bytes) + path.stat().st_size + len(suffix)


def cmd_upload(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"Input file does not exist: {path}")
    if path.stat().st_size > MAX_TRANSFER_BYTES:
        raise SystemExit("Input file exceeds COMFYUI_MAX_TRANSFER_BYTES")
    body, boundary, content_length = multipart_file(
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
    request_headers["Content-Length"] = str(content_length)
    request = urllib.request.Request(
        f"{require_base_url()}/upload/image",
        data=body,
        headers=request_headers,
        method="POST",
    )
    with open_request(request) as response:
        result = response.read(MAX_JSON_BYTES + 1)
        if len(result) > MAX_JSON_BYTES:
            raise SystemExit("ComfyUI upload response exceeds the configured JSON limit")
        print_json(json.loads(result))


def safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SystemExit(f"ComfyUI returned a non-string {label}")
    normalized = unicodedata.normalize("NFC", value)
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).is_absolute()
        or "/" in normalized
        or "\\" in normalized
        or any(character in normalized for character in '<>:"|?*\u2044\u2215\u29f8\uff0f\uff3c')
        or any(ord(character) < 32 for character in normalized)
        or normalized.endswith((".", " "))
        or normalized.split(".", 1)[0].upper() in reserved
        or not re.fullmatch(r"[^\x00-\x1f/\\]+", normalized)
    ):
        raise SystemExit(f"ComfyUI returned an unsafe {label}")
    return normalized


def collision_free_name(directory_fd: int, name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(10000):
        candidate = name if index == 0 else f"{stem}-{index}{suffix}"
        try:
            os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
    raise SystemExit("Unable to allocate a collision-free output filename")


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
    history = request_json("GET", f"/history/{urllib.parse.quote(args.prompt_id, safe='')}")
    record = history.get(args.prompt_id, history)
    if not isinstance(record, dict) or not record:
        raise SystemExit(f"No history found for prompt {args.prompt_id}")
    failure = workflow_failure(record)
    if failure:
        raise SystemExit(failure)

    destination = Path(args.directory).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise SystemExit("Download destination must be a real directory")
    directory_fd = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    downloaded = []
    try:
        for node_id, output_kind, entry in output_files(record):
            filename = safe_component(entry["filename"], "filename")
            safe_node = safe_component(node_id, "node id")
            safe_kind = safe_component(output_kind, "output kind")
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
            requested_name = f"{safe_node}-{safe_kind}-{filename}"
            temporary_name = f".{uuid.uuid4().hex}.part"
            output_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                try:
                    total = 0
                    with open_request(request) as response:
                        declared = response.headers.get("Content-Length")
                        if declared and int(declared) > MAX_TRANSFER_BYTES:
                            raise SystemExit("ComfyUI download exceeds COMFYUI_MAX_TRANSFER_BYTES")
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_TRANSFER_BYTES:
                                raise SystemExit(
                                    "ComfyUI download exceeds COMFYUI_MAX_TRANSFER_BYTES"
                                )
                            os.write(output_fd, chunk)
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                while True:
                    output_name = collision_free_name(directory_fd, requested_name)
                    try:
                        os.link(
                            temporary_name,
                            output_name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        break
                    except FileExistsError:
                        continue
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            downloaded.append(str(destination / output_name))
    finally:
        os.close(directory_fd)
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
    command.add_argument("--timeout", type=float, default=TOTAL_TIMEOUT)
    command.add_argument("--interval", type=float, default=2)
    command.set_defaults(func=cmd_run)

    command = sub.add_parser("history")
    command.add_argument("prompt_id")
    command.set_defaults(func=cmd_history)

    command = sub.add_parser("wait")
    command.add_argument("prompt_id")
    command.add_argument("--timeout", type=float, default=TOTAL_TIMEOUT)
    command.add_argument("--interval", type=float, default=2)
    command.set_defaults(func=cmd_wait)

    command = sub.add_parser("interrupt")
    command.set_defaults(func=cmd_interrupt)

    command = sub.add_parser("ui-url")
    command.set_defaults(func=cmd_ui_url)

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
