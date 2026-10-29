#!/usr/bin/env python3
"""Operator-only capacity evidence for already-running, harness-owned local engines.

This does not install, launch, stop, reconfigure or qualify an engine. Run with
Kimi idle: synthetic prompts deliberately occupy every resolved sequence slot.
After normal stack shutdown, compare an isolated structured allocation event.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from . import capacity, host_caps, local_llm
    from .private_file import write_private_json
else:
    import capacity
    import host_caps
    import local_llm
    from private_file import write_private_json

METADATA_BYTES = 65536
RESPONSE_BYTES = 8 * 1024**2
RECORD_DIRECTORY = "capacity-smoke"

#: Mirrored from ``proxy/model_proxy.py``, which cannot be imported here: the proxy needs aiohttp
#: and this tool runs on the host's own interpreter. A drift between the two is caught by
#: ``tests/test_local_capacity_smoke``, which feeds both the same stale plan.
PLAN_SCHEMA_VERSION_LOCAL = 4
STALE_LOCAL_PLAN_SCHEMAS = frozenset({2, 3})


class SmokeError(ValueError):
    """Qualification preparation or synthetic execution failed safely."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SmokeError("engine returned duplicate JSON keys")
        result[key] = value
    return result


class EngineClient:
    def __init__(self, origin: str, token: str, timeout: float):
        self.origin, self.token, self.timeout = origin, token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            self.origin + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        maximum = RESPONSE_BYTES if path == "/v1/chat/completions" else METADATA_BYTES
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SmokeError(f"engine {path} returned HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SmokeError("engine returned compressed metadata")
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise SmokeError("engine response exceeds the evidence byte limit")
                return json.loads(
                    raw,
                    object_pairs_hook=strict_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                )
        except urllib.error.HTTPError as exc:
            # Never archive response bodies or request headers: either may contain secrets.
            raise SmokeError(f"engine {path} returned HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            if isinstance(exc, SmokeError):
                raise
            raise SmokeError(f"engine {path} failed or returned malformed JSON") from None


def token_count(client: Any, payload: dict) -> int:
    result = client.request("/v1/chat/completions/input_tokens", payload)
    count = result.get("input_tokens") if isinstance(result, dict) else None
    if type(count) is not int or count < 0:
        raise SmokeError("engine input_tokens result is unavailable or malformed")
    return count


def synthetic_payload(alias: str, repeats: int) -> dict:
    # Include Unicode, code, a tool schema and thinking in the final templated count.
    return {
        "model": alias,
        "messages": [
            {
                "role": "user",
                "content": "Synthetic capacity test; continue the sequence.\n"
                + "λ = [i * i for i in range(17)]  # café 日本語\n" * repeats,
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "capacity_fixture",
                    "description": "Synthetic evidence only",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
        "chat_template_kwargs": {"enable_thinking": True},
        "stream": False,
    }


def near_window_prompt(client: Any, engine: dict, output_tokens: int) -> tuple[dict, int]:
    target = engine["context_tokens"] - output_tokens
    if target <= 0:
        raise SmokeError("synthetic output budget must be smaller than the engine window")
    low, high = 0, 1
    payload = synthetic_payload(engine["model_alias"], 0)
    count = token_count(client, payload)
    if count > target:
        raise SmokeError("engine window cannot fit the synthetic template and output budget")
    # Tokenizer-measured bounded search, never a bytes-per-token sizing fallback.
    while high < engine["context_tokens"]:
        candidate = synthetic_payload(engine["model_alias"], high)
        if token_count(client, candidate) > target:
            break
        low, high = high, min(high * 2, engine["context_tokens"])
    while low < high:
        middle = (low + high + 1) // 2
        candidate = synthetic_payload(engine["model_alias"], middle)
        if token_count(client, candidate) <= target:
            low = middle
        else:
            high = middle - 1
    payload = synthetic_payload(engine["model_alias"], low)
    count = token_count(client, payload)
    # A tokenizer that ignores payload growth must not produce misleading near-window evidence.
    if count * 100 < target * 95:
        raise SmokeError("synthetic prompt could not approach the planned context boundary")
    payload.update(max_tokens=output_tokens, ignore_eos=True)
    return payload, count


def occupied_slots(client: Any, sequences: int) -> int:
    slots = client.request("/slots")
    if (
        not isinstance(slots, list)
        or len(slots) != sequences
        or any(
            not isinstance(slot, dict) or type(slot.get("is_processing")) is not bool
            for slot in slots
        )
    ):
        raise SmokeError("engine /slots does not attest the planned execution slots")
    return sum(slot["is_processing"] for slot in slots)


def exercise_engine(
    client: Any,
    engine: dict,
    pid: int,
    output_tokens: int,
    timeout: float,
    rss: Callable[[int], int | None] = host_caps.process_memory_bytes,
) -> dict:
    """Observe all full slots plus preparation; a missed overlap is failed evidence."""
    try:
        local_llm.validate_properties(client.request("/props"), engine)
    except local_llm.EngineError as exc:
        raise SmokeError(str(exc)) from None
    slots = engine["sequences"]
    if occupied_slots(client, slots):
        raise SmokeError("engine is busy; pause Kimi work before running synthetic evidence")
    payload, count = near_window_prompt(client, engine, output_tokens)
    stop = threading.Event()
    peaks = {"rss_bytes": 0, "samples": 0, "measurement_failed": False}

    def sample():
        while not stop.is_set():
            amount = rss(pid)
            if type(amount) is not int or amount <= 0:
                peaks["measurement_failed"] = True
            else:
                peaks["rss_bytes"] = max(peaks["rss_bytes"], amount)
                peaks["samples"] += 1
            stop.wait(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    preparation_count = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=slots) as pool:
            futures = [
                pool.submit(client.request, "/v1/chat/completions", dict(payload, id_slot=index))
                for index in range(slots)
            ]
            deadline = started + timeout
            while time.monotonic() < deadline:
                if occupied_slots(client, slots) == slots:
                    # Count the full templating payload while all executions own their slots.
                    preparation_count = token_count(client, payload)
                    if occupied_slots(client, slots) != slots:
                        raise SmokeError("execution ended before saturated preparation completed")
                    break
                if any(future.done() for future in futures):
                    raise SmokeError(
                        "requests ended before simultaneous slot occupancy was observed"
                    )
                time.sleep(0.05)
            if preparation_count is None:
                raise SmokeError("timed out waiting for simultaneous full-slot occupancy")
            results = [
                future.result(timeout=max(0.01, deadline - time.monotonic())) for future in futures
            ]
        usages = []
        for result in results:
            usage = result.get("usage") if isinstance(result, dict) else None
            if (
                not isinstance(usage, dict)
                or type(usage.get("prompt_tokens")) is not int
                or usage["prompt_tokens"] != count
                or type(usage.get("completion_tokens")) is not int
                or usage["completion_tokens"] != output_tokens
            ):
                raise SmokeError("synthetic execution did not attest the requested token workload")
            usages.append({key: usage[key] for key in ("prompt_tokens", "completion_tokens")})
        if preparation_count != count:
            raise SmokeError("engine token count changed during saturated preparation")
    finally:
        stop.set()
        sampler.join()
    if peaks["measurement_failed"] or not peaks["samples"]:
        raise SmokeError("owned-engine RSS could not be sampled throughout execution")
    return {
        "status": "synthetic_smoke_recorded",
        "input_tokens": count,
        "requested_output_tokens": output_tokens,
        "execution_usages": usages,
        "simultaneously_occupied_slots": slots,
        "preparation_while_all_slots_occupied": True,
        "peak_owned_engine_rss_bytes": peaks["rss_bytes"],
        "rss_samples": peaks["samples"],
        "memory_management": "os_managed",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def owned_receipt(runtime: Path, engine_id: str, engine: dict) -> dict:
    try:
        receipt = json.loads(local_llm.readiness_path(runtime, engine_id).read_text())
        pid, supervisor = receipt["engine_pid"], receipt["supervisor_pid"]
        if type(pid) is not int or type(supervisor) is not int or min(pid, supervisor) <= 0:
            raise SmokeError("readiness receipt has invalid owned process identities")
        if (
            receipt.get("fingerprint") != engine["fingerprint"]
            or receipt.get("snapshot_id") != engine["snapshot_id"]
        ):
            raise SmokeError("readiness receipt differs from the resolved plan")
        os.kill(supervisor, 0)
        parent = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        if int(parent.stdout.strip()) != supervisor:
            raise SmokeError("engine is no longer a child of its recorded supervisor")
        return receipt
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if isinstance(exc, SmokeError):
            raise
        raise SmokeError(
            "cannot attest the running owned engine from its readiness receipt"
        ) from None


def engine_evidence(engine: dict) -> dict:
    # Explicit allowlist: no provider credentials, headers, requests or generated text.
    keys = (
        "fingerprint",
        "snapshot_id",
        "build_info",
        "build_commit",
        "model_alias",
        "model_lock_digest",
        "model_manifest_digest",
        "provider_manifest_digest",
        "source_commit",
        "context_tokens",
        "sequences",
        "total_context_tokens",
        "allocation",
        "capacity_strategy",
        "memory_management",
        "native_context_tokens",
        "placement",
        "serving_profile_id",
        "validation_status",
    )
    return {key: engine[key] for key in keys if key in engine}


def checked_runtime(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path.parent.name != "runtime" or path.parent.parent.name != ".local" or not path.is_dir():
        raise SmokeError(
            "--runtime must name an existing harness .local/runtime/<instance> directory"
        )
    return path


def run_smoke(runtime: Path, output_tokens: int, timeout: float) -> Path:
    plan_bytes = (runtime / "model-policy.json").read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get("schema_version") in STALE_LOCAL_PLAN_SCHEMAS:
        raise SmokeError(
            f"stale schema {plan['schema_version']} local policy; "
            "relaunch the harness for the current engine contract"
        )
    engines = plan.get("engines")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION_LOCAL or (
        not isinstance(engines, dict) or not engines
    ):
        raise SmokeError("the resolved plan has no managed local engines")
    record = {
        "schema_version": 1,
        "kind": "local_capacity_native_smoke",
        "created_at": datetime.now(UTC).isoformat(),
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "memory_management": "os_managed",
        "engines": {},
        "native_qualification_complete": False,
        "status": "shutdown_comparison_pending",
        "qualification_note": "Synthetic smoke is evidence only; both pinned quants, native "
        "peak-workload review and attributed shutdown comparisons are required.",
    }
    path = runtime / RECORD_DIRECTORY / f"smoke-{uuid.uuid4().hex}.json"
    try:
        for engine_id, engine in sorted(engines.items()):
            result = {"identity": engine_evidence(engine), "shutdown_comparison": None}
            record["engines"][engine_id] = result
            receipt = owned_receipt(runtime, engine_id, engine)
            lanes = [lane for lane in plan["lanes"].values() if lane.get("engine_id") == engine_id]
            if not lanes:
                raise SmokeError("engine has no bound route in the resolved plan")
            lane = lanes[0]
            secret_name = f"{lane['provider']}__{lane['credential']}"
            if not all(character.isalnum() or character == "_" for character in secret_name):
                raise SmokeError("resolved credential identity is malformed")
            token = (runtime / "credentials" / secret_name).read_text().strip()
            if not token:
                raise SmokeError("resolved engine credential is unavailable")
            port = urllib.parse.urlsplit(lane["base_url"]).port
            if not port:
                raise SmokeError("resolved local route omits its explicit host engine port")
            # Host-side evidence only: never send the credential to a redirected/remote origin.
            client = EngineClient(f"http://127.0.0.1:{port}", token, timeout)
            result["execution"] = exercise_engine(
                client, engine, receipt["engine_pid"], output_tokens, timeout
            )
            owned_receipt(runtime, engine_id, engine)
            if (runtime / "model-policy.json").read_bytes() != plan_bytes:
                raise SmokeError("resolved plan changed during synthetic evidence collection")
    except BaseException as exc:
        record["status"] = "failed"
        record["failure"] = str(exc) if isinstance(exc, SmokeError) else type(exc).__name__
        raise
    finally:
        write_private_json(path, record)
    return path


def compare_shutdown(record: dict, engine_id: str, event_text: str) -> dict:
    if record.get("kind") != "local_capacity_native_smoke" or engine_id not in record.get(
        "engines", {}
    ):
        raise SmokeError("smoke record does not contain that engine")
    try:
        actual = capacity.parse_projection(event_text)
    except capacity.CapacityError as exc:
        raise SmokeError(str(exc)) from None
    engine = record["engines"][engine_id]
    engine["shutdown_comparison"] = {
        "event_sha256": hashlib.sha256(event_text.encode()).hexdigest(),
        "attribution": "operator_provided_isolated_event",
        "shutdown_host_bytes": actual["host_bytes"],
        "shutdown_device_bytes": actual["devices"],
        "memory_management": "os_managed",
    }
    record["native_qualification_complete"] = False
    record["status"] = "shutdown_compared_review_required"
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "compare-shutdown"))
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument(
        "--record", help="smoke record basename under the instance capacity-smoke directory"
    )
    parser.add_argument("--engine", help="qualified engine identity in the record")
    parser.add_argument(
        "--event", help="isolated shutdown JSONL event file under this instance directory"
    )
    args = parser.parse_args(argv)
    try:
        runtime = checked_runtime(args.runtime)
        if (
            args.output_tokens <= 0
            or args.timeout_seconds <= 0
            or not math.isfinite(args.timeout_seconds)
        ):
            raise SmokeError("output token budget and request timeout must be positive")
        if args.action == "run":
            path = run_smoke(runtime, args.output_tokens, args.timeout_seconds)
        else:
            if (
                not args.record
                or Path(args.record).name != args.record
                or not args.engine
                or not args.event
            ):
                raise SmokeError("comparison requires --record basename, --engine and --event")
            path = runtime / RECORD_DIRECTORY / args.record
            source = Path(args.event).resolve()
            if not source.is_relative_to(runtime):
                raise SmokeError("shutdown event must remain instance-scoped")
            record = json.loads(path.read_text())
            compare_shutdown(record, args.engine, source.read_text())
            write_private_json(path, record)
        print(path)
        print("Evidence recorded; native qualification remains pending review.")
        return 0
    except (SmokeError, OSError, ValueError, concurrent.futures.TimeoutError) as exc:
        reason = str(exc) if isinstance(exc, SmokeError) else type(exc).__name__
        print(f"Capacity evidence failed: {reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
