#!/usr/bin/env python3
"""Native feasibility probe: the blocking walk-before-you-build gate.

This is step 3b of the local-inference plan. It exists because step 3's spike, as written, named no
host: a feasibility check that runs on a Linux development machine can "resolve" the wiring
question by reading source and never once touch Metal, after which eleven steps of schema and
integration work are scheduled against an assumption nobody tested on the hardware that has to
carry it.

So this probe answers three questions on the *actual affected machine*, with the *candidate
dependency set*, in about half an hour, before any of that work is committed:

1. do the wheels import and does generation execute on this chip and this macOS version;
2. does a deliberate wired limit set by the harness survive a generation call, or does the
   high-level path overwrite it;
3. does the context cache grow with consumed tokens rather than being committed at the native
   maximum.

It deliberately runs against a small model of the same family rather than the 27B artifact. The
27B does not exist until the artifact work lands, its download is gigabytes, and question 1 and 2
are properties of MLX and the device rather than of one model's size. Question 3 is answered for the
cache *class*, which is what a small same-family model shares. Full-artifact quality, peak memory
and promotion stay in step 12, which is the only thing that may make a capability claim about the
model that actually ships.

Run it from the operator's machine with the stack otherwise idle. It writes one JSON document; that
document is the input to the step-4 runtime pin, not a report to read afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "tools"), str(ROOT / "proxy")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

RESULT_KIND = "local_native_feasibility_probe"
#: A prompt long enough that a fixed-size cache and a growing one are distinguishable, and short
#: enough that the probe finishes before the operator loses interest. Three hundred tokens is a
#: few hundred milliseconds of prefill on Apple silicon and several kilobytes of cache.
GROWTH_PROMPT_TOKENS = 300
SHORT_PROMPT_TOKENS = 8


class ProbeError(RuntimeError):
    """The probe could not run, or ran and could not answer one of its questions."""


def _require_mac() -> dict[str, Any]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ProbeError(
            "this probe answers questions that only exist on Apple silicon; this host is "
            f"{platform.system()}-{platform.machine()}"
        )
    version = platform.mac_ver()[0] or platform.release()
    return {"macos_version": version, "chip": platform.machine(), "arch": platform.machine()}


def _provision_runtime() -> dict[str, Any]:
    """Build the candidate runtime, or fail saying why. Never silently use the host's Python."""
    import local_mlx_runtime

    record = local_mlx_runtime.provision(install=True)
    return {
        "identity": record["identity"],
        "python": record["python_version"],
        "lock_digest": record["lock_digest"],
        "executable": record["executable"],
        "imports": record.get("imports") or {},
    }


def _run_in_runtime(runtime: dict[str, Any], source: str, *, timeout: int = 900) -> dict[str, Any]:
    """Execute a snippet inside the provisioned interpreter and read one JSON line back.

    A subprocess rather than an import: the whole point is to characterise the runtime the worker
    will use, and this process is the launcher's interpreter, which has no MLX and must not gain
    any.
    """
    environment = dict(os.environ)
    # Anything the operator's shell happens to export would change what the probe measures, and the
    # measurement is the deliverable.
    for key in [name for name in environment if name.startswith(("MLX_", "GGML_", "LLAMA_"))]:
        environment.pop(key, None)
    site = str(Path(runtime["executable"]).parent.parent / "environment" / "site-packages")
    environment["PYTHONPATH"] = site
    try:
        result = subprocess.run(
            [_interpreter(runtime), "-c", source],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"probe step timed out after {timeout}s") from exc
    except OSError as exc:
        raise ProbeError(f"cannot execute the provisioned interpreter: {exc}") from exc
    if result.returncode != 0:
        raise ProbeError(
            f"probe step failed (exit {result.returncode}): {(result.stderr or '')[-3000:]}"
        )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ProbeError(f"probe step returned unreadable output: {result.stdout[-500:]}") from exc


def _interpreter(runtime: dict[str, Any]) -> str:
    """The bare interpreter inside the provisioned runtime, not the worker wrapper."""
    candidate = Path(runtime["executable"]).parent / "environment" / "bin" / "python3"
    if not candidate.is_file():
        raise ProbeError(f"provisioned interpreter is missing at {candidate}")
    return str(candidate)


# ------------------------------------------------------------------------ questions

def question_one(runtime: dict[str, Any], model: str) -> dict[str, Any]:
    """Does generation actually execute here?

    A wheel that imports is not the same fact as a wheel that computes on the GPU, and an
    attention-head dimension this build cannot kernel is discovered by the first real prompt.
    """
    source = f"""
import json, mlx.core as mx
from mlx_lm import load, generate
info = mx.device_info()
model, tokenizer = load({model!r})
out = generate.generate(
    model, tokenizer, prompt="Reply with one word.", max_tokens=4, verbose=False
)
print(json.dumps({{
    "metal_available": bool(mx.metal.is_available()),
    "device_name": str(info.get("device_name", "")),
    "recommended_working_set_bytes": int(info.get("max_recommended_working_set_size") or 0),
    "generated_characters": len(out or ""),
    "generated": bool((out or "").strip()),
}}))
"""
    answer = _run_in_runtime(runtime, source)
    if not answer.get("generated"):
        raise ProbeError(f"the model produced no text: {answer}")
    return answer


def question_two(runtime: dict[str, Any], model: str) -> dict[str, Any]:
    """Does a harness-chosen wired limit survive the generation call?

    This is U2, and it is the question the whole bounded-wiring design rests on. The high-level
    wrapper raises the wired limit to the device recommendation on entry and restores it on exit,
    so a limit set before calling it is overwritten for the duration of the request. The probe
    therefore measures both paths: if the wrapper clobbers the setting and the lower-level step
    preserves it, the answer is "use generate_step", which is a design input rather than a failure.
    """
    source = f"""
import json, mlx.core as mx
from mlx_lm import load
from mlx_lm.generate import stream_generate, generate_step

model, tokenizer = load({model!r})
requested = 256 * 1024 * 1024
observed = {{}}

def probe(name, run):
    mx.set_wired_limit(requested)
    before = int(mx.get_wired_limit())
    run()
    after = int(mx.get_wired_limit())
    observed[name] = {{"before": before, "after_call": after, "honoured": before == requested}}

prompt = tokenizer.encode("Count from one to thirty.", add_special_tokens=False)

def use_wrapper():
    for _ in stream_generate(model, tokenizer, prompt=prompt, max_tokens=8):
        pass

def use_step():
    cache = model.make_cache() if hasattr(model, "make_cache") else None
    kwargs = {{}} if cache is None else {{"cache": cache}}
    for _ in generate_step(iter(prompt), model, max_tokens=8, **kwargs):
        pass

try:
    probe("stream_generate", use_wrapper)
except Exception as exc:
    observed["stream_generate"] = {{"error": str(exc)[:200]}}
try:
    probe("generate_step", use_step)
except Exception as exc:
    observed["generate_step"] = {{"error": str(exc)[:200]}}
mx.set_wired_limit(0)
print(json.dumps(observed))
"""
    observed = _run_in_runtime(runtime, source)
    honoured = [name for name, value in observed.items() if value.get("honoured")]
    return {
        "observed": observed,
        "honoured_by": sorted(honoured),
        # Answering "no" is not a failure of the probe; it is the finding that decides the API path.
        "wrapper_overwrites": not observed.get("stream_generate", {}).get("honoured", False),
        "usable_path": sorted(honoured)[0] if honoured else None,
    }


def question_three(runtime: dict[str, Any], model: str) -> dict[str, Any]:
    """Does cache storage grow with the conversation instead of being committed upfront?

    Measured as a difference between a short prompt and a long one on the same model, because an
    absolute number conflates weights with cache. If the short-request figure already contains the
    whole native maximum, the growing-cache premise is false and steps 6 and 7 change shape.
    """
    source = f"""
import json, mlx.core as mx
from mlx_lm import load
from mlx_lm.generate import generate_step

model, tokenizer = load({model!r})

def measure(tokens):
    mx.reset_peak_memory_stats()
    cache = model.make_cache() if hasattr(model, "make_cache") else None
    kwargs = {{}} if cache is None else {{"cache": cache}}
    prompt = [17] * tokens
    for _ in generate_step(iter(prompt), model, max_tokens=2, **kwargs):
        pass
    nbytes = getattr(cache, "nbytes", None)
    return {{
        "cache_nbytes": int(nbytes) if isinstance(nbytes, int) else None,
        "active_memory_bytes": int(mx.get_active_memory()),
        "peak_memory_bytes": int(mx.get_peak_memory()),
    }}

short = measure({SHORT_PROMPT_TOKENS})
long_ = measure({GROWTH_PROMPT_TOKENS})
print(json.dumps({{"short": short, "long": long_}}))
"""
    answer = _run_in_runtime(runtime, source)
    short, long_ = answer["short"], answer["long"]
    reference = "cache_nbytes" if short.get("cache_nbytes") else "peak_memory_bytes"
    growth = int(long_[reference]) - int(short[reference])
    span = GROWTH_PROMPT_TOKENS - SHORT_PROMPT_TOKENS
    return {
        "short": short,
        "long": long_,
        "measured_by": reference,
        "growth_bytes": growth,
        "bytes_per_token": round(growth / span, 1) if span and growth > 0 else None,
        # Growing is the premise. A zero delta at three hundred tokens means the allocation was
        # already made at eight, which is the behaviour being migrated away from.
        "grows_incrementally": growth > 0,
    }


# ------------------------------------------------------------------------ entry point

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="mlx-community/Qwen2.5-0.5B-Instruct-4bit",
        help="a small model of the same family as the candidate artifact",
    )
    parser.add_argument("--output", default="", help="where to write the JSON result")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=("imports", "wiring", "growth"),
        help="omit a question (only when it is known to be irrelevant)",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    started = time.monotonic()
    host = _require_mac()
    runtime = _provision_runtime()
    result: dict[str, Any] = {
        "kind": RESULT_KIND,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": host,
        "runtime": runtime,
        "probed_model": args.model,
        "answers": {},
        "blocking_findings": [],
    }
    if "imports" not in args.skip:
        result["answers"]["executes"] = question_one(runtime, args.model)
        if not result["answers"]["executes"]["metal_available"]:
            result["blocking_findings"].append("the provisioned runtime reports no Metal device")
    if "wiring" not in args.skip:
        result["answers"]["wiring"] = question_two(runtime, args.model)
        if result["answers"]["wiring"]["usable_path"] is None:
            result["blocking_findings"].append(
                "no supported generation path preserves a harness-set wired limit"
            )
    if "growth" not in args.skip:
        result["answers"]["growth"] = question_three(runtime, args.model)
        if not result["answers"]["growth"]["grows_incrementally"]:
            result["blocking_findings"].append(
                "cache storage did not grow between short and long prompts: the "
                "growing-cache premise does not hold on this build"
            )
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    result["passes"] = not result["blocking_findings"]
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        destination.chmod(0o600)
    return result


def metal_probe() -> int:
    """Emit the Metal device facts ``host_caps.metal_observations`` reads.

    A separate entry point because the two run in different interpreters: the launcher's ``python3``
    has no MLX and never will, while this must be executed by the provisioned runtime and its JSON
    handed back. Keeping it here rather than in ``host_caps`` is what lets the launcher stay free of
    any engine dependency.
    """
    source = (
        "import json, mlx.core as mx;"
        "info = mx.device_info();"
        "print(json.dumps({"
        "'device_count': 1,"
        "'recommended_max_working_set_size': int("
        "info.get('max_recommended_working_set_size') or 0),"
        "'presentation_model': str(info.get('recommended_max_working_set_size') and 'unified'),"
        "'low_power_mode': None}))"
    )
    try:
        import local_mlx_runtime

        record = local_mlx_runtime.provision(install=False)
        print(_run_in_runtime(record, source, timeout=60).get("device_count"))
    except Exception:  # noqa: BLE001 - an unavailable observation must stay an unavailable answer
        print("{}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] == "metal-probe":
        return metal_probe()
    try:
        result = run(argv)
    except (ProbeError, OSError) as error:
        print(f"probe refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
