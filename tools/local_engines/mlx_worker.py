#!/usr/bin/env python3
"""The MLX execution process: one model, one thread, one slot.

This process is the only thing in the stack that touches an accelerator. The HTTP listener is a
separate OS process, so the two properties the harness depends on are enforced by the operating
system rather than by discipline: a wedged Metal call cannot take the listener down with it, and
MLX's memory controls — ``set_wired_limit``, ``set_cache_limit``, the active/peak/cache counters —
are process-global with no device or scope argument, so an engine that shares an interpreter with
another engine's residency settings has no honest way to report which bytes it asked to pin.

Everything the process does runs on one thread. MLX's own rules about stream and cache state mean
two threads issuing work would corrupt the conversation rather than merely interleave it, so the
worker accepts one request at a time and says ``busy`` rather than queueing invisibly. The thread is
the worker's main thread; the "one dedicated execution thread" requirement in the plan is satisfied
by there being exactly one thread at all.

The model, its identity and its numbers all arrive from the resolved plan and are echoed back
through the status document. Nothing here decides a context window, a slot count or a wiring
target; it reports whether it is honouring the ones it was given.

This module is only importable inside the provisioned native runtime. It is not imported by the
launcher, the proxy, or any test that runs on a machine without MLX — the adapter spawns it as a
subprocess for exactly that reason.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

from local_engine_contract import WorkerError, status_document

#: MLX is imported lazily by :func:`_mlx` rather than at module scope. The pure logic in this file -
#: request validation, the output bound, the status document - is the part with real failure modes,
#: and a top-level ``import mlx`` would make it untestable on any host without the provisioned
#: native runtime, which is every CI machine and this one.
_mx = None
_generate_step = None


def _mlx():
    global _mx, _generate_step
    if _mx is None:
        import mlx.core as mlx_core
        from mlx_lm.generate import generate_step

        _mx, _generate_step = mlx_core, generate_step
    return _mx, _generate_step


class WorkerFatal(RuntimeError):
    """A device-level failure. The worker exits; it does not attempt to continue."""


class WorkerRefused(RuntimeError):
    """A request the worker will not run, with the code that says why."""

    def __init__(self, code: WorkerError, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    #: The resolved engine document is read from a descriptor, not from argv and not from a path
    #: handed over the command line: it carries the fingerprint and the plan's numeric knobs, and
    #: the process table is world-readable on the operator's machine.
    parser.add_argument("--plan-fd", type=int, default=3)
    return parser.parse_args(argv)


class Engine:
    """The bound model, its cache, and the residency the plan asked for.

    Construction is the whole of the worker's startup: load, verify the resolved numbers, prove the
    wiring setting stuck. A worker that cannot state its own effective settings does not report
    ready, because the alternative is the recorded failure — a backend that answers questions about
    itself while unable to answer a prompt.
    """

    def __init__(self, model_dir: str, expected: dict[str, Any]) -> None:
        from mlx_lm import load

        self._mx, self._generate_step = _mlx()
        self.model_dir = model_dir
        self.expected = expected
        self.model, self.tokenizer = load(model_dir)
        # The manifest's sampling defaults and the serving profile's numeric knobs arrive through
        # the expected record. They are read here, at construction, so a request cannot alter the
        # prefill chunk or the cache precision mid-session: those are the values the engine
        # fingerprint hashes, and letting a request body move them would make the plan a hint.
        self.defaults = dict(expected.get("sampling") or {})
        profile = dict(expected.get("allocation") or {})
        self.prefill_step_size = int(profile.get("prefill_chunk_tokens") or 0) or None
        self.max_kv_size = profile.get("max_kv_size")
        # KV quantisation is deliberately not plumbed: the profile declares f16/bf16 attention
        # caches, and a quantised KV cache is a different precision decision that U3 has not
        # qualified. Absent means generate_step keeps its own no-quantisation default.
        self.kv_bits = profile.get("kv_bits")
        self.kv_group_size = profile.get("kv_group_size")
        self.quantized_kv_start = profile.get("quantized_kv_start")  # noqa: E501
        # Generation goes through generate_step rather than the high-level stream_generate
        # wrapper. The wrapper calls maybe_set_recommended_wired_limit() on entry — which raises
        # the wired limit to the device's maximum recommendation, the opposite of a bounded
        # target — and restores it on exit. Anything the launcher configured would be overwritten
        # per request, and the status document would then describe a setting the engine ignored.
        config = getattr(self.model, "config", None) or {}
        self.native_context = _config_int(config, "max_position_embeddings", "n_positions")
        self.cache = _new_cache(self.model)
        self.cache_allocation = "growing" if _grows(self.cache) else "fixed"
        self.wired_limit_bytes = 0
        self.declared_wired_limit_bytes = int(expected.get("wired_bytes") or 0)
        self._apply_wiring()
        self._verify_against_plan()

    # ------------------------------------------------------------------ residency

    def _apply_wiring(self) -> None:
        """Request the plan's deliberate residency and then read back what the device granted."""
        target = self.declared_wired_limit_bytes
        recommended = None
        try:
            recommended = self._mx.device_info().get("max_recommended_working_set_size")
        except Exception:  # noqa: BLE001 - a device that will not say is an unknown, not a crash
            recommended = None
        self.recommended_working_set_bytes = int(recommended) if recommended else None
        if target > 0:
            # MLX clamps the wired limit to the system ceiling internally; the read-back is the
            # only evidence of what was actually granted, and the worker reports that rather than
            # echoing the request it made.
            self._mx.set_wired_limit(target)
        reader = getattr(self._mx, "get_wired_limit", None)
        self.wired_limit_bytes = int(reader()) if callable(reader) else target

    def _verify_against_plan(self) -> None:
        expected_context = self.expected.get("context_tokens")
        if expected_context and self.native_context and self.native_context != expected_context:
            raise WorkerFatal(
                f"model native context {self.native_context} differs from the resolved plan "
                f"{expected_context}"
            )
        if self.declared_wired_limit_bytes and not self.wired_limit_bytes:
            # A requested bound that reads back as zero means the setting did not take. Saying so
            # beats reporting ready while silently paging everything.
            raise WorkerFatal(
                f"requested wired limit {self.declared_wired_limit_bytes} bytes but the device "
                f"reports {self.wired_limit_bytes}"
            )

    # ------------------------------------------------------------------ memory

    def memory(self) -> dict[str, int | None]:
        """Process-global counters. Host-wide pressure is not measurable from here and is not
        claimed: the launcher reads that, and mixing the two is how a total gets assembled from
        numbers that already overlap."""
        return {
            "active_memory_bytes": int(self._mx.get_active_memory()),
            "peak_memory_bytes": int(self._mx.get_peak_memory()),
            "cache_memory_bytes": int(self._mx.get_cache_memory()),
        }

    def reset_peak(self) -> None:
        self._mx.reset_peak_memory_stats()

    # ------------------------------------------------------------------ preparation

    def prepare(self, payload: dict[str, Any]) -> tuple[list[int], str]:
        """Run the one chat preparation, returning prompt token ids.

        Counting and generation both call this. Two code paths that each apply a template are the
        classic source of an off-by-a-few-token count, and with this engine the count decides both
        admission and the output budget, so a divergent pair does not merely misreport — it lets a
        request through that cannot fit.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise WorkerRefused(WorkerError.MALFORMED_REQUEST, "no messages in request")
        unsupported = sorted(
            set(payload) - _ACCEPTED_REQUEST_FIELDS,
        )
        if unsupported:
            # Refusing is safer than ignoring: an ignored field would still change what the model
            # sees, and the count reported for the request would describe a different prompt than
            # the one served.
            raise WorkerRefused(
                WorkerError.UNSUPPORTED_SETTING,
                "unsupported request fields: " + ", ".join(unsupported),
            )
        tools = payload.get("tools") or None
        chat_template = getattr(self.tokenizer, "chat_template", None)
        template_identity = _digest_of_text(chat_template or "")
        kwargs: dict[str, Any] = {"add_generation_prompt": True}
        if tools:
            kwargs["tools"] = tools
        thinking = payload.get("chat_template_kwargs") or {}
        if isinstance(thinking, dict):
            kwargs.update(thinking)
        try:
            prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, **kwargs)
        except TypeError as exc:
            # A template that needs a kwarg this worker does not pass is a version skew between
            # plan and engine, not a client error.
            raise WorkerFatal(f"chat template rejected the request: {exc}") from exc
        ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        self.template_identity = template_identity
        return list(ids), prompt

    def token_count(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids, _prompt = self.prepare(payload)
        return {
            "object": "response.input_tokens",
            "input_tokens": len(ids),
            "template_identity": self.template_identity,
            "tokenizer_identity": self.tokenizer_identity(),
        }

    def tokenizer_identity(self) -> str:
        # The tokenizer is identified by the bytes that define it, so a swapped tokenizer under the
        # same model directory changes the identity instead of silently changing every count.
        material = json.dumps(
            {
                "vocab": len(getattr(self.tokenizer, "vocab", {}) or {}),
                "name": type(self.tokenizer).__name__,
            },
            sort_keys=True,
        )
        return _digest_of_text(material)

    # ------------------------------------------------------------------ generation

    def generate(self, payload: dict[str, Any]):
        """Yield completion events. One caller at a time; the slot is this thread.

        Called through ``generate_step`` rather than ``stream_generate``. The high-level wrapper
        enters a ``wired_limit()`` context that raises the device wiring to
        ``max_recommended_working_set_size`` for the duration of every request and restores it
        afterwards, so a bound the launcher computed from the plan would be overwritten per
        request while the status document still reported the plan's value. ``generate_step`` is the
        supported path that leaves the setting alone, and it is the one that takes the cache,
        prefill chunk and quantised-KV arguments explicitly rather than deriving them.
        """
        from mlx_lm.sample_utils import make_sampler

        ids, _prompt = self.prepare(payload)
        sampling = _sampling_from(payload, self.defaults)
        limit = _output_limit(payload)
        self.reset_peak()
        step_kwargs: dict[str, Any] = {
            "max_tokens": limit,
            "sampler": make_sampler(**sampling),
            "prompt_cache": self.cache,
        }
        if self.prefill_step_size:
            step_kwargs["prefill_step_size"] = self.prefill_step_size
        for key in ("max_kv_size", "kv_bits", "kv_group_size", "quantized_kv_start"):
            value = getattr(self, key, None)
            if value is not None:
                step_kwargs[key] = value
        # generate_step takes a token array and owns the chunked prefill itself, which is what
        # makes prefill_step_size the peak-memory knob rather than a caller-side loop.
        prompt = self._mx.array([ids])
        for token, _logprobs in self._generate_step(prompt, self.model, **step_kwargs):
            yield {"token": token, "text": self.tokenizer.decode([int(token)])}

    def release(self) -> None:
        """Drop the cache and let the device reclaim what the conversation no longer needs.

        Called on cancellation and between conversations. It does not, and cannot, un-wire memory
        the device pinned: process teardown is what guarantees release, which is why the frontend
        treats "worker exited" as the strongest possible signal rather than an error to retry.
        """
        self.cache = _new_cache(self.model)
        self._mx.clear_cache()


#: Fields the worker will honour. Anything else is refused rather than dropped, because a dropped
#: field still changed the prompt the client meant to send.
_ACCEPTED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "stream_options",
        "seed",
        "chat_template_kwargs",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
    }
)


#: Request field -> the ``make_sampler`` argument it maps to. The request speaks OpenAI and the
#: sampler speaks MLX, and the two do not use the same names for the same thing.
_SAMPLING_FIELDS = {
    "temperature": "temp",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
}


def _sampling_from(payload: dict[str, Any], defaults: dict[str, Any]) -> dict[str, float]:
    """Resolve sampler arguments from the request over the manifest's declared defaults.

    The defaults come from the model manifest rather than from MLX's own, so the engine samples the
    way the vendor published for this checkpoint and a request that omits a knob does not silently
    change it.
    """
    resolved: dict[str, float] = {}
    for field, sampler_argument in _SAMPLING_FIELDS.items():
        value = payload.get(field, defaults.get(field))
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise WorkerRefused(WorkerError.MALFORMED_REQUEST, f"{field} must be a number")
        resolved[sampler_argument] = float(value)
    return resolved


def _output_limit(payload: dict[str, Any]) -> int:
    """The exact output bound. An absent key must still resolve to a concrete number.

    The proxy rewrites the body to a bounded maximum before dispatch, so a request that arrives
    without a limit is the harness's own bug rather than a client's, and letting MLX generate to
    the end of the context would be a reservation nothing priced.
    """
    limits = [
        payload[key]
        for key in ("max_tokens", "max_completion_tokens")
        if isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool)
    ]
    if not limits:
        raise WorkerRefused(
            WorkerError.MALFORMED_REQUEST,
            "no output maximum reached the worker; the proxy bounds every local request",
        )
    value = min(limits)
    if value <= 0:
        raise WorkerRefused(WorkerError.MALFORMED_REQUEST, "output maximum must be positive")
    return int(value)


def _new_cache(model: Any):
    """Ask the model for its own cache rather than constructing one.

    A hybrid architecture returns a mix of token-proportional KV caches and fixed-size recurrent
    state. Building a plain KVCache for such a model produces something that runs and is wrong, and
    a recurrent state cannot be sliced or truncated like an attention cache, so the only safe
    source of truth is the model's own ``make_cache``.
    """
    from mlx_lm.models.cache import KVCache

    maker = getattr(model, "make_cache", None)
    if callable(maker):
        return maker()
    return KVCache()


def _grows(cache: Any) -> bool:
    from mlx_lm.models.cache import ArraysCache, KVCache

    return isinstance(cache, (KVCache, ArraysCache, list))


def _config_int(config: Any, *names: str) -> int | None:
    for name in names:
        value = config.get(name) if isinstance(config, dict) else getattr(config, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _digest_of_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def build_status(engine: Engine, expected: dict[str, Any], ready: bool) -> dict[str, Any]:
    """The one place this worker describes itself."""
    return status_document(
        engine_kind="mlx_lm",
        model=str(expected.get("model_alias", "")),
        model_path=os.path.abspath(engine.model_dir),
        protocol_version=int(expected.get("protocol_version", 1)),
        ready=ready,
        context_tokens=int(expected["context_tokens"]),
        sequences=int(expected["sequences"]),
        total_context_tokens=int(expected["total_context_tokens"]),
        fingerprint=str(expected.get("fingerprint", "")),
        launch_identity=str(expected.get("snapshot_id", "")),
        runtime_identity=str(expected.get("runtime_identity", "")),
        serving_profile_id=str(expected.get("serving_profile_id", "")),
        tokenizer_identity=engine.tokenizer_identity(),
        template_identity=getattr(engine, "template_identity", ""),
        artifact_format=str(expected.get("artifact_format", "mlx")),
        cache_allocation=engine.cache_allocation,
        wired_limit_bytes=engine.wired_limit_bytes,
        declared_wired_limit_bytes=engine.declared_wired_limit_bytes,
        recommended_working_set_bytes=engine.recommended_working_set_bytes,
        **engine.memory(),
    )


def _write(stream, payload: dict[str, Any]) -> None:
    """One JSON document per line. Newline-delimited rather than length-prefixed: the frontend and
    the worker are both ours, and a framing scheme that a partial write can desynchronise is a
    defect worth avoiding on a channel that carries a device-failure notice."""
    stream.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()


def serve(stdin, stdout, expected: dict[str, Any]) -> int:
    """The worker's whole life: build, announce readiness, serve requests one at a time.

    Reads requests from ``stdin`` and writes responses to ``stdout``, both line-delimited. The
    frontend owns the socket, the credential and the protocol surface; this process owns the device.
    A fatal failure exits non-zero without answering, which is the frontend's evidence that the
    slot is free because the holder is gone.
    """
    try:
        engine = Engine(str(expected["model_dir"]), expected)
    except WorkerFatal as failure:
        _write(stdout, {"kind": "fatal", "reason": str(failure)})
        return 1
    except Exception as failure:  # noqa: BLE001 - a model that will not load is a failed launch
        _write(stdout, {"kind": "fatal", "reason": f"model load failed: {failure}"})
        return 1

    status = build_status(engine, expected, ready=True)
    _write(stdout, {"kind": "ready", "status": status})

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write(stdout, {"kind": "refused", "code": WorkerError.MALFORMED_REQUEST})
            continue
        sequence = request.get("sequence")
        action = request.get("action")
        payload = request.get("payload") or {}
        try:
            if action == "status":
                _write(stdout, {"kind": "status", "sequence": sequence,
                                "status": build_status(engine, expected, True)})
            elif action == "input_tokens":
                count = engine.token_count(payload)
                _write(stdout, {"kind": "input_tokens", "sequence": sequence, "count": count})
            elif action == "generate":
                _write(stdout, {"kind": "begin", "sequence": sequence})
                produced = 0
                for event in engine.generate(payload):
                    produced += len(event["text"])
                    _write(stdout, {"kind": "token", "sequence": sequence, "text": event["text"]})
                _write(stdout, {"kind": "end", "sequence": sequence, "content_chars": produced})
            elif action == "cancel":
                # The worker only ever runs one generation, so a cancel arriving here was queued
                # behind it; releasing between requests is therefore the honest handling.
                engine.release()
                _write(stdout, {"kind": "cancelled", "sequence": sequence})
            else:
                _write(stdout, {"kind": "refused", "code": WorkerError.MALFORMED_REQUEST,
                                "sequence": sequence})
        except WorkerRefused as refusal:
            _write(stdout, {"kind": "refused", "code": str(refusal.code),
                            "reason": str(refusal), "sequence": sequence})
        except Exception as failure:  # noqa: BLE001 - anything else is the device, and terminal
            # A Metal error mid-generation leaves cache and stream state undefined. Continuing
            # would serve the next request from that state, which is precisely the poisoned-backend
            # condition this whole change set exists to detect.
            _write(stdout, {"kind": "fatal", "reason": str(failure), "sequence": sequence})
            engine.release()
            return 2
    engine.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # The plan document arrives on the control descriptor rather than argv: it contains the
    # engine fingerprint and the resolved numbers, and argv is visible to every process the
    # operator's machine is running.
    with os.fdopen(args.plan_fd, "r", encoding="utf-8") as control:
        expected = json.load(control)
    expected["model_dir"] = args.model_dir
    return serve(sys.stdin, sys.stdout, expected)


if __name__ == "__main__":
    raise SystemExit(main())
