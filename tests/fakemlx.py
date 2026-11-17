"""A stand-in for the pinned MLX runtime, so the real worker and frontend can execute on Linux.

``tools/local_engines/mlx_worker.py`` imports ``mlx`` lazily for exactly the reason
this file exists:
its logic — request validation, cache lifecycle, residency, the status document, the transport
framing — has the failure modes worth testing, and none of them need a GPU. Before this fixture the
only tests that touched those two files read them as *text* and regex-matched the result, which
cannot see argument order, key nesting, buffer semantics, or whether an attribute exists at all.

The fixture is deliberately a mirror rather than a mock:

* :data:`MLX_CORE_NAMES` is the export list of ``mlx`` 0.32.4's ``python/src/memory.cpp`` at that
  tag, so ``mx.reset_peak_memory_stats`` and ``mx.get_wired_limit`` raise ``AttributeError`` here
  exactly as they do on the operator's Mac. A stub that supplied them would launder the bug.
* ``generate_step`` has no EOS handling and no detokenizer, because the pinned one does not. That
  asymmetry — the stop condition living in ``stream_generate`` instead — is the fact the worker most
  needs to get right.
* a supplied ``prompt_cache`` is updated **in place** and ``max_kv_size`` is then ignored, which is
  the pinned contract and the reason a long-lived cache silently defeats the profile's bound.

Update :data:`MLX_CORE_NAMES`, :data:`GENERATE_STEP_PARAMETERS` and
:data:`STREAM_GENERATE_PARAMETERS` from the tag when ``requirements-mlx.lock`` moves, not from
whichever version happens to be installed.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

#: Pinned dependency versions this fixture mirrors (providers/local_mps/requirements-mlx.in).
MLX_VERSION = "0.32.4"
MLX_LM_VERSION = "0.32.2"

#: ``mlx.core`` names the worker or the probe reaches for, checked against
#: ``mlx/python/src/memory.cpp`` at tag v0.32.4. ``set_wired_limit`` returns the *previous* limit;
#: there is no getter, so an "observed wired limit" is not a thing the platform can report.
MLX_CORE_NAMES = frozenset(
    {
        "array",
        "clear_cache",
        "device_info",
        "eval",
        "evaluate",
        "get_active_memory",
        "get_array_buffer_size",
        "get_cache_memory",
        "get_memory_limit",
        "get_peak_memory",
        "reset_peak_memory",
        "set_cache_limit",
        "set_memory_limit",
        "set_wired_limit",
        "synchronize",
    }
)

#: Names callers must *not* find on ``mlx.core``. Kept separate from the positive list because the
#: point is that a stub inherits nothing: absent means absent.
MLX_CORE_ABSENT_NAMES = frozenset(
    {
        "get_wired_limit",  # no wired getter exists in 0.32.4
        "reset_peak_memory_stats",  # the PyTorch spelling
    }
)

#: Keyword-only parameters of ``mlx_lm.generate.generate_step`` at tag v0.32.2.
GENERATE_STEP_PARAMETERS = (
    "max_tokens",
    "sampler",
    "logits_processors",
    "max_kv_size",
    "prompt_cache",
    "prefill_step_size",
    "kv_bits",
    "kv_group_size",
    "quantized_kv_start",
    "prompt_progress_callback",
    "input_embeddings",
)

#: Parameters of ``mlx_lm.generate.stream_generate`` at tag v0.32.2. Everything not in
#: :data:`GENERATE_STEP_PARAMETERS` is consumed by the wrapper itself rather than forwarded.
STREAM_GENERATE_PARAMETERS = (
    "model",
    "tokenizer",
    "prompt",
    "max_tokens",
    "draft_model",
    "stream",
)

#: Parameters of ``generate_step`` that a caller may pass by keyword — the positional names
#: included, because naming them is legal and a check that forgot them reports a false defect.
GENERATE_STEP_ACCEPTED_KEYWORDS = ("prompt", "model", "stream") + GENERATE_STEP_PARAMETERS

#: ``stream_generate``'s own parameters are consumed by the wrapper; the rest are forwarded to
#: ``generate_step``, so both sets are legal on a call to it.
STREAM_GENERATE_ACCEPTED_KEYWORDS = STREAM_GENERATE_PARAMETERS + GENERATE_STEP_PARAMETERS


SAMPLER_PARAMETERS = (
    "temp",
    "top_p",
    "min_p",
    "min_tokens_to_keep",
    "top_k",
    "xtc_probability",
    "xtc_threshold",
    "xtc_special_tokens",
)


class Recording:
    """Every device-level call the code under test made, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.cache_created: list[int] = []
        self.wired_limit = 0
        self.cache_limit = 0

    def note(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [entry[0] for entry in self.calls]

    def with_name(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [entry for entry in self.calls if entry[0] == name]

    def kwargs_of(self, name: str) -> list[dict]:
        return [entry[2] for entry in self.calls if entry[0] == name]


class FakeArray:
    """Just enough of ``mx.array`` to expose the shape error the real API would raise.

    ``generate_step`` calls ``len(prompt)`` and ``prompt[None]``; handing it a 2-D batch therefore
    reads a prompt of length one and feeds the model a rank-3 input. Both are reproduced here rather
    than hidden.
    """

    def __init__(self, data: Any) -> None:
        self.data = data
        nested = (
            isinstance(data, (list, tuple)) and bool(data) and isinstance(data[0], (list, tuple))
        )
        self.ndim = 2 if nested else 1
        self.shape = (len(data[0]),) if nested else (len(data),)
        self.size = int(self.shape[0])

    def __len__(self) -> int:
        # The pinned failure mode: a 2-D prompt reports one token long, so chunked prefill skips.
        return self.shape[0] if self.ndim == 2 else len(self.data)

    def __getitem__(self, key: Any) -> FakeArray:
        if isinstance(key, list) and key and key[0] is None:
            return FakeArray([self.data])
        return self

    def __bool__(self) -> bool:
        return True


class FakeCache:
    """A per-layer cache with the offset semantics ``generate_step`` relies on."""

    def __init__(self, kind: str = "kv") -> None:
        self.kind = kind
        self.offset = 0
        self.state = None

    def update(self, *args: Any, **kwargs: Any) -> None:
        self.offset += 1

    def makes_empty(self) -> FakeCache:
        return FakeCache(self.kind)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"FakeCache({self.kind}, offset={self.offset})"


class BareCache(FakeCache):
    """What a single ``KVCache`` behaves like: not indexable, not iterable.

    Upstream's ``generate_step`` does ``mx.eval([c.state for c in prompt_cache])`` and models index
    ``cache[0]``, so a bare cache object rather than the model's per-layer list fails exactly here.
    """

    def __getitem__(self, key: int) -> Any:
        raise TypeError("'KVCache' object is not subscriptable")

    def __iter__(self) -> Iterator[Any]:
        raise TypeError("'KVCache' object is not iterable")


class Detokenizer:
    """The pinned ``NaiveStreamingDetokenizer`` contract: segments, not per-token strings.

    Modelled as byte-level BPE, because that is what the shipped chat templates use: a token id maps
    to one byte, so a lone token is frequently not valid UTF-8 and decoding it by itself yields a
    replacement character. That is the corruption a per-token ``decode([id])`` loop introduces. The
    detokenizer holds a prefix that cannot yet decode and emits it once a later byte completes the
    character, which is what upstream does.

    Passing ``words`` switches to opaque string tokens, for cases where the point is sequencing
    rather than UTF-8 boundaries.
    """

    def __init__(self, words: dict[int, str] | None = None) -> None:
        self.words = words
        self.pending: list[int] = []
        self.last_segment = ""

    def add_token(self, token: int) -> None:
        if self.words is not None:
            self.pending.append(token)
            text = "".join(self.words.get(piece, f"<{piece}>") for piece in self.pending)
            # Hold back anything a following token could still complete.
            if text.endswith((">", "\uFFFD")):
                self.last_segment = ""
            else:
                self.last_segment = text
                self.pending = []
            return
        self.pending.append(int(token) & 0xFF)
        if self._decodable():
            self.last_segment = bytes(self.pending).decode("utf-8")
            self.pending = []
        else:
            self.last_segment = ""

    def _decodable(self) -> bool:
        if not self.pending:
            return False
        try:
            bytes(self.pending).decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def finalize(self) -> None:
        if not self.pending:
            # Upstream leaves nothing to emit; not clearing this would make the wrapper's final
            # yield repeat the segment it already delivered.
            self.last_segment = ""
            return
        if self.words is not None:
            self.last_segment = "".join(self.words.get(p, f"<{p}>") for p in self.pending)
        else:
            # Upstream emits the tail with errors="replace"; so does this, so a stranded byte is
            # visible to the caller rather than silently dropped.
            self.last_segment = bytes(self.pending).decode("utf-8", errors="replace")
        self.pending = []


@dataclass
class GenerationResponse:
    text: str
    token: Any
    logprobs: Any
    from_draft: bool = False
    prompt_tokens: int = 0
    prompt_tps: float = 0.0
    generation_tokens: int = 0
    generation_tps: float = 0.0
    peak_memory: float = 0.0
    finish_reason: str | None = None


@dataclass
class FakeTokenizer:
    """A ``TokenizerWrapper``-shaped tokenizer.

    ``eos_token_ids`` is a set because that is what upstream tests with ``token in
    tokenizer.eos_token_ids``; a single ``eos_token_id`` is not the interface.
    """

    eos_token_ids: frozenset = frozenset({99})
    bos_token: str | None = None
    #: Opaque token->string map. Empty means byte-level: id 233 is the byte 0xe9.
    words: dict[int, str] = field(default_factory=dict)
    templated: list[Any] = field(default_factory=list)
    #: The ids a generation will be fed, so a test can drive a scripted token sequence.
    prompt_ids: list[int] = field(default_factory=lambda: [1, 2, 3])

    def apply_chat_template(self, messages: Any, **kwargs: Any) -> str:
        self.templated.append(messages)
        return f"PROMPT<{len(messages)}>"

    def encode(self, text: str, **kwargs: Any) -> list[int]:
        return list(self.prompt_ids)

    def decode(self, ids: Any, **kwargs: Any) -> str:
        """Byte-level, like the shipped tokenizers — which is what makes per-token decode lossy.

        ``decode([0xc3])`` cannot be valid UTF-8 on its own and yields a replacement character,
        while ``decode([0xc3, 0xa9])`` yields ``é``. A loop that decodes one id at a time therefore
        corrupts every character a tokenizer split across tokens.
        """
        if self.words:
            return "".join(self.words.get(int(i), f"<{int(i)}>") for i in ids)
        return bytes(int(i) & 0xFF for i in ids).decode("utf-8", errors="replace")

    @property
    def detokenizer(self) -> Detokenizer:
        return Detokenizer(self.words or None)


class FakeModel:
    """``num_layers`` + ``make_cache`` + ``config``, the three things the worker reads."""

    def __init__(self, num_layers: int = 3, native_context: int = 4096) -> None:
        self.num_layers = num_layers
        self.config = {"max_position_embeddings": native_context}

    def make_cache(self) -> list[FakeCache]:
        return [FakeCache() for _ in range(self.num_layers)]


class ModelWithoutMakeCache:
    """A model that does not build its own cache, so upstream must supply the per-layer list.

    Qwen3 in mlx-lm v0.32.2 defines no ``make_cache`` and indexes ``cache[0]``, so the serving
    target lands here — which is precisely why a hand-rolled fallback to a single bare cache object
    is fatal rather than merely untidy. It is a separate class rather than a flag on
    :class:`FakeModel` because ``hasattr`` must genuinely be False.
    """

    def __init__(self, num_layers: int = 3, native_context: int = 4096) -> None:
        self.num_layers = num_layers
        self.config = {"max_position_embeddings": native_context}


def make_prompt_cache(model: Any, max_kv_size: int | None = None) -> list[Any]:
    """Pinned ``mlx_lm.models.cache.make_prompt_cache`` semantics: always a per-layer list."""
    if hasattr(model, "make_cache"):
        cache = model.make_cache()
    else:
        cache = [BareCache() for _ in range(getattr(model, "num_layers", 1))]
    return list(cache)


def install(
    *,
    words: dict[int, str] | None = None,
    script: list[int] | None = None,
    model: FakeModel | None = None,
    tokenizer: FakeTokenizer | None = None,
    device_info: dict | None = None,
) -> Recording:
    """Put the fake packages in ``sys.modules`` and return the call recorder.

    ``script`` is the token-id sequence a generation yields, so a test controls EOS and budget
    exhaustion without a model. ``device_info`` defaults to a Metal-sized device so the wired-limit
    path is exercised rather than skipped.
    """
    recording = Recording()
    # Byte-level by default, because that is what the shipped tokenizers are; a test that
    # wants opaque pieces passes words= explicitly.
    words = words or {}
    script = script if script is not None else [1, 2, 3]
    model = model or FakeModel()
    tokenizer = tokenizer or FakeTokenizer(words=words)
    device = {
        "deviceType": "MLX1",
        "max_recommended_working_set_size": 64 * 1024**3,
        "recommendedMaxWorkingSetSize": 64 * 1024**3,
    } | (device_info or {})

    mx = types.ModuleType("mlx.core")
    mx.__version__ = MLX_VERSION
    mx.array = lambda data: FakeArray(data)
    mx.eval = lambda *a, **k: recording.note("eval")
    mx.evaluate = lambda *a, **k: None
    mx.synchronize = lambda *a, **k: recording.note("synchronize")
    mx.device_info = lambda *a, **k: dict(device)
    mx.clear_cache = lambda *a, **k: recording.note("clear_cache")
    def set_wired_limit(limit: int) -> int:
        """Returns the previous limit, as the pinned binding does — never the one requested."""
        recording.note("set_wired_limit", int(limit))
        previous = recording.wired_limit
        recording.wired_limit = int(limit)
        return previous

    def set_cache_limit(limit: int) -> None:
        recording.note("set_cache_limit", int(limit))
        recording.cache_limit = int(limit)

    mx.set_wired_limit = set_wired_limit
    mx.set_cache_limit = set_cache_limit
    mx.set_memory_limit = lambda n: None
    mx.get_memory_limit = lambda: 0
    mx.get_active_memory = lambda: 1_000_000
    mx.get_peak_memory = lambda: 2_000_000
    mx.get_cache_memory = lambda: 3_000_000
    mx.reset_peak_memory = lambda: recording.note("reset_peak_memory")
    mx.metal = types.SimpleNamespace(is_available=lambda: True)

    class _Stream:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    mx.Stream = _Stream
    mx.thread_local_stream = lambda *a, **k: _Stream()

    def generate_step(
        prompt: Any,
        model_arg: Any,
        stream: Any = None,
        **kwargs: Any,
    ) -> Iterator[tuple[int, list]]:
        unknown = set(kwargs) - set(GENERATE_STEP_PARAMETERS)
        if unknown:
            raise TypeError(f"generate_step got unexpected keywords {sorted(unknown)}")
        if isinstance(prompt, FakeArray) and prompt.ndim != 1:
            # Pinned behaviour, made loud: len(prompt) becomes 1, so the chunked prefill the
            # profile configured never runs and the model is handed a rank-3 input.
            recording.note("generate_step_bad_prompt_ndim", prompt.ndim)
        else:
            recording.note("generate_step", sorted(kwargs))
        cache = kwargs.get("prompt_cache")
        if cache is None:
            cache = make_prompt_cache(model_arg, kwargs.get("max_kv_size"))
        recording.cache_created.append(id(cache))
        # No EOS handling here, because the pinned generate_step has none either.
        for token in script:
            yield token, []

    def stream_generate(
        model_arg: Any,
        tokenizer_arg: Any,
        prompt: Any,
        max_tokens: int = 256,
        draft_model: Any = None,
        stream: Any = None,
        **kwargs: Any,
    ) -> Iterator[GenerationResponse]:
        unknown = set(kwargs) - set(GENERATE_STEP_PARAMETERS)
        if unknown:
            raise TypeError(f"stream_generate forwarded unexpected keywords {sorted(unknown)}")
        recording.note("stream_generate", max_tokens, sorted(kwargs))
        if not isinstance(prompt, FakeArray):
            prompt = mx.array(prompt if isinstance(prompt, list) else [1, 2, 3])
        # The pinned wrapper raises the wired limit to the device's recommendation and restores it
        # afterwards; reproducing that is the point of preferring it over the raw step function.
        previous = mx.set_wired_limit(device["max_recommended_working_set_size"])
        detokenizer = tokenizer_arg.detokenizer
        eos_hit = False
        produced = 0
        try:
            for token, logprobs in generate_step(prompt, model_arg, stream, **kwargs):
                if token in tokenizer_arg.eos_token_ids:
                    eos_hit = True
                    break
                detokenizer.add_token(token)
                produced += 1
                if produced >= max_tokens:
                    break
                if detokenizer.last_segment:
                    yield GenerationResponse(
                        text=detokenizer.last_segment,
                        token=token,
                        logprobs=logprobs,
                        prompt_tokens=len(prompt),
                        generation_tokens=produced,
                        peak_memory=0.002,
                        finish_reason=None,
                    )
            detokenizer.finalize()
            yield GenerationResponse(
                text=detokenizer.last_segment,
                token=token,
                logprobs=[],
                prompt_tokens=len(prompt),
                generation_tokens=produced,
                peak_memory=0.002,
                finish_reason="stop" if eos_hit else "length",
            )
        finally:
            mx.set_wired_limit(previous)

    def load(directory: Any) -> tuple[Any, Any]:
        recording.note("load", str(directory))
        return model, tokenizer

    gen = types.ModuleType("mlx_lm.generate")
    gen.generate_step = generate_step
    gen.stream_generate = stream_generate
    gen.generate = lambda *a, **k: list(stream_generate(*a, **k))
    su = types.ModuleType("mlx_lm.sample_utils")

    def make_sampler(**kwargs: Any) -> Callable:
        unknown = set(kwargs) - set(SAMPLER_PARAMETERS)
        if unknown:
            raise TypeError(f"make_sampler got unexpected keywords {sorted(unknown)}")
        recording.note("make_sampler", sorted(kwargs))
        return lambda logits, mx_sample: 0

    su.make_sampler = make_sampler
    su.make_logits_processors = lambda **kwargs: []
    cache_mod = types.ModuleType("mlx_lm.models.cache")
    cache_mod.KVCache = BareCache
    cache_mod.ArraysCache = FakeCache
    cache_mod.make_prompt_cache = make_prompt_cache
    cache_mod.can_trim_prompt_cache = lambda cache: isinstance(cache, list)
    cache_mod.trim_prompt_cache = lambda cache, tokens: cache
    models_mod = types.ModuleType("mlx_lm.models")
    models_mod.cache = cache_mod
    utils_mod = types.ModuleType("mlx_lm.utils")
    utils_mod.maybe_set_recommended_wired_limit = lambda: mx.set_wired_limit(
        device["max_recommended_working_set_size"]
    )
    lm = types.ModuleType("mlx_lm")
    lm.__version__ = MLX_LM_VERSION
    lm.load = load
    # The standalone counter loads only the tokenizer: no weights, no device work.
    lm.load_tokenizer = lambda path, **kw: (recording.note("load_tokenizer", str(path)),
                                            tokenizer)[-1]
    # Upstream's package does ``from .generate import batch_generate, generate, stream_generate``,
    # so the attribute mlx_lm.generate is the *function*, shadowing the module of the same name.
    # Reproducing that matters: the probe calls generate.generate(...), which is an AttributeError
    # on a real host and must be one here too rather than silently resolving to the module.
    lm.generate = gen.generate
    lm.stream_generate = stream_generate
    lm.batch_generate = lambda *a, **k: []
    lm.sample_utils = su
    lm.models = models_mod
    lm.utils = utils_mod

    mlx_package = types.ModuleType("mlx")
    mlx_package.core = mx

    modules = {
        "mlx": mlx_package,
        "mlx.core": mx,
        "mlx_lm": lm,
        "mlx_lm.generate": gen,
        "mlx_lm.sample_utils": su,
        "mlx_lm.models": models_mod,
        "mlx_lm.models.cache": cache_mod,
        "mlx_lm.utils": utils_mod,
    }
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    for module in modules.values():
        module._fakemlx_saved = saved  # type: ignore[attr-defined]
    return recording


def uninstall() -> None:
    """Restore whatever ``sys.modules`` held before :func:`install`."""
    for name in (
        "mlx",
        "mlx.core",
        "mlx_lm",
        "mlx_lm.generate",
        "mlx_lm.sample_utils",
        "mlx_lm.models",
        "mlx_lm.models.cache",
        "mlx_lm.utils",
    ):
        module = sys.modules.get(name)
        saved = getattr(module, "_fakemlx_saved", {}) if module is not None else {}
        if name in saved and saved[name] is not None:
            sys.modules[name] = saved[name]
        else:
            sys.modules.pop(name, None)
