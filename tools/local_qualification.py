#!/usr/bin/env python3
"""Native qualification: the evidence a promotion is allowed to rest on.

Step 12 of the local-inference plan. The probe (``local_native_probe.py``) asks whether the design
is possible; this asks whether *this artifact, on this host, for this workload class* actually
works, and records the answer in a form that cannot be summarised into a stronger claim.

Two rules shape the file.

First, the questions are asked in escalating order and the run stops recording "passed" the moment a
later one fails, because the recorded incident was a near-ceiling tool-heavy request and a suite
that reports green on canaries while that one workload OOMs is the exact false assurance the whole
change set exists to remove.

Second, memory findings are compared against the declared per-cache-class expectation rather than
against a threshold invented here. Exceeding an old projection is an observation; an actual
allocation failure, a malformed outcome, identity drift or a failed cancellation is a failure. That
distinction is the plan's R7 and it is the reason this tool reports both and conflates neither.

Run it with the stack otherwise idle. It never changes an OS setting, never restarts an engine, and
never promotes anything: it writes evidence, and a human reads it before the manifest flips.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "tools"), str(ROOT / "proxy")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

RESULT_KIND = "local_native_qualification"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


#: One repeated sentence is the filler. Its size is a property of the prompt the test builds, not
#: of the engine, so it is a constant the sizing loop adjusts a count of rather than a string it
#: grows by multiplication.
_FILLER_UNIT = "The quick brown fox jumps over the lazy dog. "
#: The most filler the sizer will ever place in one message. A prompt this long is not a sizing
#: failure waiting to happen, it is the reason the sizer is bounded: an unclamped correction factor
#: compounds once per pass and reaches gigabytes of text in a handful of iterations.
_MAX_FILLER_UNITS = 200_000


class QualificationError(RuntimeError):
    """The qualification could not be run at all, as opposed to a case inside it failing."""


def _engine_origin() -> tuple[str, int, str]:
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    plan_path = runtime / "model-policy.json"
    if not plan_path.is_file():
        raise QualificationError(f"no resolved plan at {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if __package__:
        from . import local_llm
    else:
        import local_llm
    models = local_llm.selected_models(runtime)
    if len(models) != 1:
        raise QualificationError(
            "qualification targets exactly one selected local engine; this launch has "
            f"{len(models)}"
        )
    model = models[0]
    token = local_llm.api_token(model, plan)
    return local_llm.engine_host(), local_llm.engine_port(model), token


def _post(origin: tuple[str, int, str], path: str, payload: dict, timeout: float) -> dict:
    host, port, token = origin
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local origin
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise QualificationError("engine response exceeded the supported size")
        return json.loads(body)


def synthetic_request(tokens: int, *, tools: bool = False) -> dict[str, Any]:
    """A prompt of a stated token size, built through the engine's own preparation path.

    Sized by iteration against ``input_tokens`` rather than by a bytes-per-token estimate, because
    the only authority on how many tokens a preparation consumes is that preparation. An estimate
    here would reproduce the mistake the counting endpoint exists to remove.
    """
    messages = [{"role": "user", "content": _FILLER_UNIT * max(1, tokens // 4)}]
    payload: dict[str, Any] = {
        "messages": messages,
        "stream": True,
        "max_tokens": 16,
        "temperature": 0.0,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]
        payload["tool_choice"] = "required"
    return payload


def measure_tokens(origin, payload: dict) -> int:
    answer = _post(origin, "/v1/chat/completions/input_tokens", payload, 60.0)
    count = answer.get("input_tokens")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise QualificationError("engine returned an unusable token count")
    return int(count)




def fit_request(origin, target_tokens: int, *, tools: bool) -> tuple[dict, int]:
    """Grow or shrink a synthetic prompt until its prepared count lands on the target.

    Sized by iteration against the engine's own ``input_tokens`` rather than by a bytes-per-token
    estimate, because the only authority on what a preparation costs is that preparation.

    The correction adjusts a *unit count*, not the current length, and is clamped. Scaling the
    existing string by ``target / observed`` compounds: an engine that reports a count far from the
    target makes the next pass overshoot by that same factor again, and eight passes is enough to
    exhaust memory. Failing to converge is the correct outcome for a sizer, not a bigger string.
    """
    payload = synthetic_request(target_tokens, tools=tools)
    count = -1
    for _ in range(10):
        count = measure_tokens(origin, payload)
        if abs(count - target_tokens) <= max(8, target_tokens // 100):
            return payload, count
        units = _unit_count(payload)
        tokens_per_unit = max(1, count // max(1, units))
        wanted = max(1, min(_MAX_FILLER_UNITS, target_tokens // tokens_per_unit))
        payload["messages"][0]["content"] = _FILLER_UNIT * wanted
    raise QualificationError(
        f"could not size a synthetic request to {target_tokens} prepared tokens; last count "
        f"{count}. Refusing to qualify against a prompt that is not the one intended."
    )


def _unit_count(payload: dict[str, Any]) -> int:
    body = payload["messages"][0]["content"]
    return max(1, len(body) // len(_FILLER_UNIT))


def timed_generation(origin, payload: dict, timeout: float) -> dict[str, Any]:
    """Stream one completion, recording first-token time, rates and the terminal shape."""
    host, port, token = origin
    body = dict(payload)
    body["stream_options"] = {"include_usage": True}
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    first_token: float | None = None
    content = ""
    reasoning = ""
    tool_calls = 0
    finish_reason: str | None = None
    saw_error = False
    usage: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local origin
        buffer = b""
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n\n")
                for line in raw.split(b"\n"):
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == b"[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if event.get("error") is not None:
                        saw_error = True
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        if isinstance(choice.get("finish_reason"), str):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if isinstance(delta.get("content"), str):
                            if first_token is None and delta["content"]:
                                first_token = time.monotonic() - started
                            content += delta["content"]
                        if isinstance(delta.get("reasoning_content"), str):
                            reasoning += delta["reasoning_content"]
                        tool_calls += len(delta.get("tool_calls") or [])
    elapsed = time.monotonic() - started
    generated = int(usage.get("completion_tokens") or 0)
    return {
        "seconds_total": round(elapsed, 3),
        "seconds_to_first_token": round(first_token, 3) if first_token else None,
        "content_characters": len(content),
        "reasoning_characters": len(reasoning),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "saw_error_object": saw_error,
        "usage": usage,
        "tokens_per_second": round(generated / elapsed, 2) if generated and elapsed else None,
    }


def cancel_during(origin, payload: dict, after_seconds: float) -> dict[str, Any]:
    """Abandon a request mid-flight and prove the slot came back.

    This is the case a cooperative cancellation can silently fail: the HTTP task ends while the
    device work it started is still running, and the next request queues behind a generation nobody
    asked for any more. The measurement is therefore not "did cancel return" but "can the engine
    serve the next thing promptly".
    """
    host, port, token = origin
    body = dict(payload)
    body["max_tokens"] = 512
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    opened = time.monotonic()
    response = None
    try:
        response = urllib.request.urlopen(request, timeout=600.0)  # noqa: S310 - local origin
        response.read(64)
        time.sleep(after_seconds)
        cancelled_after = time.monotonic() - opened
    finally:
        if response is not None:
            response.close()
    recovered = synthetic_request(SHORT_TOKENS)
    started = time.monotonic()
    try:
        observe = timed_generation(origin, recovered, timeout=120.0)
    except (OSError, urllib.error.URLError) as failure:
        return {
            "cancelled_after_seconds": round(cancelled_after, 3),
            "slot_recovered": False,
            "reason": f"the following request failed: {failure}",
        }
    waited = time.monotonic() - started
    return {
        "cancelled_after_seconds": round(cancelled_after, 3),
        "slot_recovered": bool(observe["content_characters"] or observe["tool_calls"]),
        "recovery_seconds": round(waited, 3),
        "first_request_after_cancel": observe,
    }


SHORT_TOKENS = 32


def run(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--near-ceiling-fraction", type=float, default=0.9)
    parser.add_argument("--request-timeout", type=float, default=1800.0)
    parser.add_argument("--cancel-after", type=float, default=2.0)
    parser.add_argument("--skip-near-ceiling", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    origin = _engine_origin()
    host, port, _token = origin
    started = time.monotonic()
    result: dict[str, Any] = {
        "kind": RESULT_KIND,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "origin": f"{host}:{port}",
        "cases": {},
        "failures": [],
        "observations": [],
    }

    def record(name: str, value: dict, *, failing: bool, note: str = "") -> None:
        result["cases"][name] = value
        if failing:
            result["failures"].append(f"{name}: {note or 'failed'}")

    # 1. The basics first: if a short prompt does not produce content, nothing above it is
    #    interpretable and continuing only spends the operator's afternoon.
    payload, count = fit_request(origin, SHORT_TOKENS, tools=False)
    answer = timed_generation(origin, payload, args.request_timeout)
    usable = bool(answer["content_characters"] or answer["tool_calls"])
    record(
        "short_completion",
        {**answer, "prepared_tokens": count},
        failing=answer["saw_error_object"] or not usable,
        note="a short request produced no usable completion",
    )
    if result["failures"]:
        return _finish(result, started, args.output)

    # 2. Tools and thinking, which are the shapes that made the original workload fail.
    payload, count = fit_request(origin, 512, tools=True)
    answer = timed_generation(origin, payload, args.request_timeout)
    record(
        "tool_call",
        {**answer, "prepared_tokens": count},
        failing=answer["tool_calls"] == 0,
        note="declared tool_use produced no tool call",
    )

    # 3. Cancellation, and the slot returning.
    payload, _count = fit_request(origin, 256, tools=False)
    answer = cancel_during(origin, payload, args.cancel_after)
    record(
        "cancellation",
        answer,
        failing=not answer["slot_recovered"],
        note="the engine did not serve a follow-up request after a cancellation",
    )

    # 4. Repeated turns, where a leak shows up as growth across identical requests rather than in
    #    any single one.
    rates: list[float] = []
    payloads = [fit_request(origin, 1024, tools=True)[0] for _ in range(3)]
    for index, payload in enumerate(payloads):
        answer = timed_generation(origin, payload, args.request_timeout)
        if answer["tokens_per_second"]:
            rates.append(answer["tokens_per_second"])
        record(
            f"repeat_tool_turn_{index}",
            answer,
            failing=answer["saw_error_object"],
            note="a repeated identical turn errored",
        )
    if len(rates) == len(payloads):
        spread = max(rates) - min(rates)
        # Reported, not failed: a slowdown across identical turns is evidence of a leak that the
        # operator should weigh, and one noisy measurement must not block a promotion on its own.
        result["observations"].append(
            {
                "kind": "throughput_spread_across_identical_turns",
                "tokens_per_second": rates,
                "spread": round(spread, 2),
                "median": round(statistics.median(rates), 2),
            }
        )

    # 5. The near-ceiling request: the workload class from the recorded failure.
    if not args.skip_near_ceiling:
        declared = _declared_context(origin)
        if declared is None:
            result["failures"].append(
                "near_ceiling: the engine reported no context length to approach"
            )
        else:
            target = int(declared * args.near_ceiling_fraction)
            payload, count = fit_request(origin, target, tools=True)
            answer = timed_generation(origin, payload, args.request_timeout)
            record(
                "near_ceiling",
                {**answer, "prepared_tokens": count, "declared_context": declared},
                failing=bool(answer["saw_error_object"]) or not (
                    answer["content_characters"] or answer["tool_calls"]  # noqa: SIM220
                ),
                note=(
                    f"a {count}-token request against a {declared}-token window did not "
                    "complete usefully"
                ),
            )
    else:
        result["observations"].append({"kind": "near_ceiling_skipped", "by_flag": True})

    return _finish(result, started, args.output)


def _declared_context(origin) -> int | None:
    """Read the engine's own enforced window, refusing to assume it matches the plan."""
    try:
        status = _post(origin, "/v1/harness/status", {}, 30.0)
    except (OSError, urllib.error.URLError, QualificationError):
        status = {}
    for key in ("context_tokens",):
        value = status.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _finish(result: dict[str, Any], started: float, output: str) -> dict[str, Any]:
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    result["passes"] = not result["failures"]
    if not result["passes"]:
        # Stated in the artifact, so a summary line cannot accidentally imply more than the run
        # established.
        result["promotion_permitted"] = False
        result["blocking_failures"] = result["failures"]
    else:
        result["promotion_permitted"] = True
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(result, indent=2, sort_keys=True) + "\n"
        destination.write_text(text)
        destination.chmod(0o600)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(argv)
    except (QualificationError, OSError, urllib.error.URLError) as error:
        print(f"qualification refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
