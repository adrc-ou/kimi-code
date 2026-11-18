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
worker accepts one request at a time and the frontend queues them on its channel lock. The worker
never reports "busy", because from its side there is no such moment: it is handed a request only
when it is free. The thread is the worker's main thread; the "one dedicated execution thread"
requirement in the plan is satisfied by there being exactly one thread at all.

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
import threading
from pathlib import Path
from typing import Any

# This file is started as a script by the frontend, which is itself started by the launcher, so it
# cannot rely on anyone else's sys.path. proxy/ holds the shared vocabulary and is not a sibling of
# this package.
_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_ROOT / "proxy"), str(_ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Two statements, not one: isort will not merge an aliased import into the plain group below it, so
# joining them re-introduces I001. The noqa is the same reason proxy/model_proxy.py carries one —
# a module-level path bootstrap has to precede the import it makes resolvable.
from local_engine_contract import (  # noqa: E402
    LOCAL_UNSUPPORTED_REQUEST_FIELDS as _UNSUPPORTED_REQUEST_FIELDS,
)
from local_engine_contract import (  # noqa: E402
    WorkerError,
    status_document,
    unsupported_request_fields,
)

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
    #: The resolved engine document is read from a descriptor, not from argv and not from a path
    #: handed over the command line: it carries the fingerprint and the plan's numeric knobs, and
    #: the process table is world-readable on the operator's machine.
    parser.add_argument("--plan-fd", type=int, default=3)
    #: A second, byte-wide input used only to say "stop". It cannot be the command channel, because
    #: a cancellation that has to wait for the command loop to come back around is not a
    #: cancellation: the loop is exactly what is busy.
    parser.add_argument("--cancel-fd", type=int, default=4)
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
        self.allocator_cache_bytes: int | None = (
            int(profile["allocator_cache_bytes"])
            if profile.get("allocator_cache_bytes") is not None
            else None
        )
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
        # No cache is built here. Each request gets its own from the generator, so nothing outlives
        # the completion that created it; the profile's max_kv_size then actually bounds it, which a
        # process-lifetime cache silently prevented.
        self.cache_allocation = "growing" if self.max_kv_size is None else "bounded"
        #: Set by :func:`watch_cancellation`, which touches nothing else. The pinned MLX rules this
        #: process exists to respect are about stream and cache state, and reading a byte off a
        #: pipe is neither, so this stays the one atom the generation loop consults.
        self.cancel_requested = False
        # The resolver stores its decision as engine["wiring"]["wired_bytes"]
        # (`capacity.py::wiring_targets`), the plan document carries that key through
        # whitelisted as-is, and policy.py reads it back from
        # the same nesting. A top-level read here found nothing, applied zero, and left the guard
        # below permanently unreachable because both sides were consistently absent.
        self.declared_wired_limit_bytes = int(
            (expected.get("wiring") or {}).get("wired_bytes") or 0
        )
        self._apply_wiring()
        self._verify_against_plan()

    # ------------------------------------------------------------------ residency

    def _apply_wiring(self) -> None:
        """Ask the device for the plan's residency, and record only what it can actually tell us.

        mlx 0.32.4 binds ``set_wired_limit`` to return the *previous* limit and offers no getter, so
        there is no observation of the new one to be had. Reporting the request as an observation is
        what made this look verified; the status document now carries the requested number and the
        previous one separately, and leaves "observed" null.
        """
        target = self.declared_wired_limit_bytes
        try:
            recommended = self._mx.device_info().get("max_recommended_working_set_size")
        except Exception:  # noqa: BLE001 - a device that will not say is an unknown, not a crash
            recommended = None
        self.recommended_working_set_bytes = int(recommended) if recommended else None
        self.wired_limit_requested_bytes = target
        self.wired_limit_previous_bytes: int | None = None
        # No mlx 0.32.4 getter exists, so this stays None: unknown, never assumed.
        self.wired_limit_observed_bytes: int | None = None
        if target > 0:
            self.wired_limit_previous_bytes = int(self._mx.set_wired_limit(target))
        # Declared in the profile, hashed into the engine fingerprint, and previously read by
        # nobody — so editing it rotated the engine identity while changing no behaviour. Zero is a
        # meaningful value ("retain no free buffers"), not an absence, which is why the schema
        # allows it; applying it is what makes the fingerprint honest.
        retained = self.allocator_cache_bytes
        if retained is not None:
            self._mx.set_cache_limit(retained)

    def _verify_against_plan(self) -> None:
        expected_context = self.expected.get("context_tokens")
        if expected_context and self.native_context and self.native_context != expected_context:
            raise WorkerFatal(
                f"model native context {self.native_context} differs from the resolved plan "
                f"{expected_context}"
            )
        if self.declared_wired_limit_bytes and self.wired_limit_previous_bytes is None:
            # A declared bound that never reached the setter means the plan and the device disagree
            # about whether residency is controlled at all. Saying so beats reporting ready while
            # paging everything.
            raise WorkerFatal(
                f"plan declares {self.declared_wired_limit_bytes} bytes of deliberate wiring but "
                "the device was never asked for it"
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
        self._mx.reset_peak_memory()

    # ------------------------------------------------------------------ preparation

    def prepare(self, payload: dict[str, Any]) -> tuple[list[int], str]:
        """Run the one chat preparation, returning prompt token ids and the templated text."""
        ids, prompt, template_identity = prepare_prompt(self.tokenizer, payload)
        self.template_identity = template_identity
        return ids, prompt

    def token_count(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids, _prompt = self.prepare(payload)
        return {
            "object": "response.input_tokens",
            "input_tokens": len(ids),
            "template_identity": self.template_identity,
            "tokenizer_identity": self.tokenizer_identity(),
        }

    def tokenizer_identity(self) -> str:
        return digest_of_tokenizer(self.tokenizer)

    # ------------------------------------------------------------------ generation

    def generate(self, payload: dict[str, Any]):
        """Yield one event per produced segment, then a terminal event that says why it stopped.

        Generation goes through ``stream_generate`` rather than the raw step function. The reason
        this file originally gave for bypassing the wrapper — that it raises the device wiring for
        the duration of a request — is the wrapper *saving the previous limit and restoring it on
        exit*, and doing nothing at all when the device reports no recommendation. What bypassing it
        actually cost is everything the wrapper owns and ``generate_step`` does not implement: the
        EOS break, the incremental detokenizer, and the stop-versus-length distinction. A worker
        that cannot tell those apart reports a truncated completion as a finished one.

        No ``prompt_cache`` is passed. The cache the step function builds for itself is per request,
        which is what keeps one conversation's state from being appended to another's, and
        ``max_kv_size`` only bounds a cache built this way — supplying a long-lived one made the
        profile's bound inert.
        """
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        ids, _prompt = self.prepare(payload)
        sampling = _sampling_from(payload, self.defaults)
        limit = _output_limit(payload)
        self.reset_peak()
        kwargs: dict[str, Any] = {"sampler": make_sampler(**sampling)}
        if self.prefill_step_size:
            kwargs["prefill_step_size"] = self.prefill_step_size
        for key in ("max_kv_size", "kv_bits", "kv_group_size", "quantized_kv_start"):
            value = getattr(self, key, None)
            if value is not None:
                kwargs[key] = value
        stop = _StopScanner(_stop_sequences(payload))
        produced = 0
        tool_deltas = 0
        finish_reason = "length"
        peak_memory_bytes: int | None = None
        generation_tokens = 0
        # The prompt arrives one-dimensional because stream_generate normalises it itself; wrapping
        # it in a list made len(prompt) read as one token and skipped the chunked prefill entirely.
        for response in stream_generate(
            self.model, self.tokenizer, ids, max_tokens=limit, **kwargs
        ):
            if response.peak_memory is not None:
                peak_memory_bytes = int(float(response.peak_memory) * 1_000_000_000)
            generation_tokens = int(response.generation_tokens or generation_tokens)
            if response.finish_reason:
                finish_reason = response.finish_reason
            if not response.text:
                continue
            emitted = stop.feed(response.text)
            if emitted:
                produced += len(emitted)
                yield {"kind": "segment", "text": emitted, "token": int(response.token)}
            if self.cancel_requested:
                # Checked between segments rather than inside a step: the segment boundary is the
                # first point where no MLX operation is in flight, so stopping here cannot leave a
                # half-issued kernel call holding device state.
                finish_reason = "cancelled"
                break
            if stop.hit:
                # A stop sequence is a completion that ended, not one that was cut short: reporting
                # "length" here would tell the client to expect more than will ever arrive.
                finish_reason = "stop"
                break
        tail = stop.flush()
        if tail:
            produced += len(tail)
            yield {"kind": "segment", "text": tail, "token": -1}
        yield {
            "kind": "terminal",
            "finish_reason": finish_reason,
            "cancelled": finish_reason == "cancelled",
            "content_chars": produced,
            "generation_tokens": generation_tokens,
            "peak_memory_bytes": peak_memory_bytes,
            "tool_calls": tool_deltas,
        }

    def release(self) -> None:
        """Drop what the device is holding for a conversation that is over.

        There is no long-lived cache to rebuild: :meth:`generate` lets the generator own one per
        request, so the cross-request state this used to clear never exists. ``clear_cache`` still
        matters — it returns buffers the allocator retained — and cancellation and teardown both
        want it. Process exit remains the only guarantee that *wired* memory comes back, which is
        why the frontend treats "worker exited" as the strongest signal rather than an error.
        """
        self._mx.clear_cache()


def _stop_sequences(payload: dict[str, Any]) -> tuple[str, ...]:
    """Validate and normalise ``stop``. A malformed stop list is refused, never ignored."""
    value = payload.get("stop")
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise WorkerRefused(
            WorkerError.MALFORMED_REQUEST, "stop must be a non-empty string or list of strings"
        )
    return tuple(value)


class _StopScanner:
    """Cut the stream at the first stop sequence without emitting what follows it.

    Streaming means the sequence can straddle two segments, so the tail of the emitted text is held
    back for as long as it could still be the beginning of a stop string. Anything else would either
    leak pasted tool output or require buffering the whole completion.
    """

    def __init__(self, sequences: tuple[str, ...]) -> None:
        self.sequences = sequences
        self.holdback = max((len(item) for item in sequences), default=0)
        self.buffer = ""
        self.hit = False

    def feed(self, text: str) -> str:
        if self.hit:
            return ""
        self.buffer += text
        for sequence in self.sequences:
            index = self.buffer.find(sequence)
            if index >= 0:
                emit, self.buffer, self.hit = self.buffer[:index], "", True
                return emit
        keep = 0
        for length in range(min(self.holdback - 1, len(self.buffer)), 0, -1):
            tail = self.buffer[-length:]
            if any(item.startswith(tail) for item in self.sequences):
                keep = length
                break
        cut = len(self.buffer) - keep
        emit, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return emit

    def flush(self) -> str:
        emit, self.buffer = ("" if self.hit else self.buffer), ""
        return emit


def check_request(payload: dict[str, Any]) -> None:
    """Refuse what this engine cannot honour, before any device work or stream state exists.

    Called by both :meth:`Engine.prepare` and the generate branch of :func:`serve`, and by the
    latter *before* it writes ``begin``: a request-level refusal that arrives after the stream has
    started can only reach the client as an in-band error object on a response that already said
    HTTP 200, which is what let one malformed client request look like engine damage.

    The payload's own shape is checked first for the same reason. Every field read below is a
    ``.get``, so a payload that is not an object raises ``AttributeError`` instead, and the command
    loop can only tell ``WorkerRefused`` from device damage — which would retire a healthy engine
    over a request it should simply have refused.
    """
    if not isinstance(payload, dict):
        raise WorkerRefused(WorkerError.MALFORMED_REQUEST, "request payload must be an object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WorkerRefused(WorkerError.MALFORMED_REQUEST, "no messages in request")
    unsupported = unsupported_request_fields(payload)
    if unsupported:
        # Refusing is safer than ignoring: an ignored field still changes what the model sees, and
        # the token count reported for the request would describe a different prompt than served.
        reasons = "; ".join(
            f"{name}: {_UNSUPPORTED_REQUEST_FIELDS[name]}"
            for name in unsupported
            if name in _UNSUPPORTED_REQUEST_FIELDS
        )
        detail = f"unsupported request fields: {', '.join(unsupported)}"
        raise WorkerRefused(
            WorkerError.UNSUPPORTED_SETTING, f"{detail} ({reasons})" if reasons else detail
        )
    _stop_sequences(payload)
    _output_limit(payload)


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


def _config_int(config: Any, *names: str) -> int | None:
    for name in names:
        value = config.get(name) if isinstance(config, dict) else getattr(config, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _digest_of_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def digest_of_tokenizer(tokenizer: Any) -> str:
    """Identify a tokenizer by the bytes that define it, not by its path.

    Shared with the standalone counting process: a count is only meaningful if the identity it is
    reported under is the identity the engine will compute with, and a swapped tokenizer under an
    unchanged model directory must change the digest rather than silently change every count.
    """
    material = json.dumps(
        {
            "vocab": len(getattr(tokenizer, "vocab", {}) or {}),
            "name": type(tokenizer).__name__,
        },
        sort_keys=True,
    )
    return _digest_of_text(material)


def prepare_prompt(tokenizer: Any, payload: dict[str, Any]) -> tuple[list[int], str, str]:
    """Template a chat request and encode it, returning ``(token_ids, prompt, template_digest)``.

    Module-level and shared by the worker *and* the standalone counting process, because the whole
    value of a separate counter is that it reaches the same number the engine will later spend.
    Two code paths that each apply a template are the classic source of an off-by-a-few-token
    count, and with this engine the count decides both admission and the output budget — a
    divergent pair does not merely misreport, it lets through a request that cannot fit.
    """
    check_request(payload)
    messages = payload["messages"]
    tools = payload.get("tools") or None
    chat_template = getattr(tokenizer, "chat_template", None)
    template_identity = _digest_of_text(chat_template or "")
    kwargs: dict[str, Any] = {"add_generation_prompt": True}
    if tools:
        kwargs["tools"] = tools
    thinking = payload.get("chat_template_kwargs") or {}
    if isinstance(thinking, dict):
        kwargs.update(thinking)
    try:
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, **kwargs)
    except TypeError as exc:
        # A template that needs a kwarg this harness does not pass is version skew between plan
        # and engine, not a client error.
        raise WorkerFatal(f"chat template rejected the request: {exc}") from exc
    ids = tokenizer.encode(prompt, add_special_tokens=False)
    return list(ids), prompt, template_identity


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
        # Read back from the loaded model or from the device counters. Everything not listed here
        # is a value the plan told this process, and the document says so rather than letting a
        # health endpoint present an echo as a measurement.
        attested=(
            "active_memory_bytes",
            "peak_memory_bytes",
            "cache_memory_bytes",
            "recommended_working_set_bytes",
            "wired_bytes_previous",
            "tokenizer_identity",
            "template_identity",
            "cache_allocation",
            "model",
            "model_path",
        ),
        # Requested and observed are separate fields on purpose. mlx exposes no wired-limit getter,
        # so the observed value is null rather than a copy of the request; conflating them is what
        # let an applied-zero bug report a bounded engine.
        wired_bytes_requested=engine.declared_wired_limit_bytes,
        wired_bytes_observed=engine.wired_limit_observed_bytes,
        wired_bytes_previous=engine.wired_limit_previous_bytes,
        recommended_working_set_bytes=engine.recommended_working_set_bytes,
        **engine.memory(),
    )


def _write(stream, payload: dict[str, Any]) -> None:
    """One JSON document per line. Newline-delimited rather than length-prefixed: the frontend and
    the worker are both ours, and a framing scheme that a partial write can desynchronise is a
    defect worth avoiding on a channel that carries a device-failure notice."""
    stream.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()


def watch_cancellation(engine: Engine, cancel_fd: int) -> None:
    """Block on ``cancel_fd`` and raise the flag when anything arrives.

    Deliberately a byte channel rather than a command: the frontend should not have to know a
    sequence number, hold a lock, or hope the worker is listening in order to stop work it already
    asked for. A write of one byte is the whole protocol, which is what makes it usable from a
    disconnect handler that is itself already unwinding.
    """
    try:
        with os.fdopen(cancel_fd, "rb", closefd=True) as source:
            arrived = source.read(1)
    except OSError:
        return
    # EOF is not a cancellation. The frontend closes this descriptor on every teardown, so treating
    # an empty read as a stop request would have the last act of a shutting-down parent be a phantom
    # cancel of work that has already finished.
    if arrived:
        engine.cancel_requested = True


def serve(stdin, stdout, expected: dict[str, Any], cancel_fd: int = -1) -> int:
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

    if cancel_fd >= 0:
        # Started before the first request is accepted, so a client that hangs up during the very
        # first prefill is still heard.
        threading.Thread(
            target=watch_cancellation, args=(engine, cancel_fd), daemon=True
        ).start()

    status = build_status(engine, expected, ready=True)
    _write(stdout, {"kind": "ready", "status": status})

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            # Nesting deep enough to exhaust the stack escapes json.loads as a RecursionError, which
            # is not a ValueError; tools/analyze_image.py answers the same hazard the same way.
            request = None
        if not isinstance(request, dict):
            # Valid JSON that is not an object is the same kind of bad request as JSON that is not
            # valid, and gets the same answer.
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
                try:
                    check_request(payload)
                except WorkerRefused as refusal:
                    _write(
                        stdout,
                        {
                            "kind": "refused",
                            "code": refusal.code,
                            "sequence": sequence,
                            "message": str(refusal),
                        },
                    )
                    continue
                _write(stdout, {"kind": "begin", "sequence": sequence})
                # The terminal event carries what the frontend cannot infer: whether the model
                # said stop or the budget ran out. Without it the frontend has nothing honest to
                # put in finish_reason, and a truncation is indistinguishable from a completion.
                for event in engine.generate(payload):
                    if event["kind"] == "segment":
                        _write(
                            stdout,
                            {
                                "kind": "token",
                                "sequence": sequence,
                                "text": event["text"],
                            },
                        )
                        continue
                    _write(
                        stdout,
                        {
                            "kind": "end",
                            "sequence": sequence,
                            "content_chars": event["content_chars"],
                            "finish_reason": event["finish_reason"],
                            "generation_tokens": event["generation_tokens"],
                            "peak_memory_bytes": event["peak_memory_bytes"],
                            # Named with the contract's own code so the frontend can tell "the
                            # caller stopped me" apart from "the device failed". Both end a stream
                            # early; only one of them is engine damage, and labelling the other as
                            # such would latch a healthy engine because the client hung up.
                            "code": (str(WorkerError.CANCELLED) if event["cancelled"] else None),
                        },
                    )
            elif action == "cancel":
                # Reached only when the worker is idle, because a real cancellation arrives on the
                # byte channel while it is busy. It still has a job: releasing retained allocator
                # buffers after an aborted turn, which is what the byte channel cannot do from its
                # own thread (no MLX calls off the main thread).
                engine.release()
                _write(
                    stdout,
                    {
                        "kind": "refused",
                        "code": WorkerError.CANCELLED,
                        "sequence": sequence,
                    },
                )
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
    # The artifact directory arrives inside the plan document, not as an argument. It used to be
    # both, and the argv half could not even be parsed: the frontend emitted
    # "--worker-arg --model-dir", which argparse rejects because the value begins with a dash.
    if not str(expected.get("model_dir") or ""):
        raise SystemExit("the resolved plan document names no artifact directory")
    return serve(sys.stdin, sys.stdout, expected, args.cancel_fd)


if __name__ == "__main__":
    raise SystemExit(main())
