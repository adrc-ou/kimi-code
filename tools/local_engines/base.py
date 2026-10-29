#!/usr/bin/env python3
"""The adapter interface and the registry that dispatches on declared engine kind.

Dispatch is by the ``engine`` field a serving profile declares, never by provider or model name.
That distinction is load-bearing: two models from one provider may be served by different engines
during a migration, and one model name may be served by the same engine at two different precisions.
Selecting on anything other than the declared kind turns a configuration fact into a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: Engine kinds with an implementation in this package. A profile naming anything else is refused
#: at parse time rather than at launch, because "unknown engine" discovered after a multi-gigabyte
#: download is an expensive way to say "typo".
KNOWN_KINDS = ("llama_cpp", "mlx_lm")


class EngineAdapterError(ValueError):
    """An adapter was asked something its engine cannot answer, or answered inconsistently."""


@dataclass(frozen=True)
class Runtime:
    """A provisioned engine runtime: where it is and what proved it complete."""

    kind: str
    identity: str
    directory: Path
    #: Interpreter or binary the engine actually runs under. Absolute, already verified present.
    executable: Path
    #: Adapter-declared facts that identify this runtime for the plan and for drift checks.
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineIdentity:
    """What the engine software *is*, in fields that do not assume a git build commit.

    llama.cpp reports ``b11846`` plus a commit; a Python library reports a distribution version and
    a source revision, and has no build number at all. The resolved plan therefore carries a
    structured identity rather than a bare ``build_commit`` every engine is pretended to have.
    """

    kind: str
    #: Human-readable one-liner the engine reports about itself, compared as a substring.
    build_info: str
    #: Upstream source revision this runtime was built or pinned from, if it reports one.
    source_revision: str | None = None
    #: Version as the engine itself states it, e.g. a distribution version.
    version: str | None = None
    #: A build commit, for the engines that have one. Optional on purpose: a Python library
    #: resolved from a lock has a version and a source revision, and inventing a commit for it
    #: would make every reader that "compares the build" compare a field that means nothing.
    build_commit: str | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "engine_kind": self.kind,
            "build_info": self.build_info,
            "source_revision": self.source_revision,
            "version": self.version,
            "identity": {
                "build_info": self.build_info,
                "source_revision": self.source_revision,
                "version": self.version,
            },
        }
        if self.build_commit:
            record["build_commit"] = self.build_commit
            record["identity"]["build_commit"] = self.build_commit
        return record


@dataclass(frozen=True)
class Artifact:
    """A verified weight set, described the way this engine needs to load it.

    ``paths`` is every file the engine will consume, not every file present. A loader allowed to
    discover files opportunistically can pick up a shard the lock never digested, which makes the
    digest record a statement about some of the bytes rather than all of them.
    """

    format: str
    variant: str
    #: The file the engine is pointed at: a GGUF for llama.cpp, a directory for MLX.
    primary: Path
    paths: tuple[Path, ...]
    #: Tensor storage only, from verified metadata. Excludes tokenizer, config and unused modality
    # files, because those are not what a memory projection is about.
    tensor_bytes: int | None = None
    total_bytes: int = 0


@dataclass(frozen=True)
class Launch:
    """One engine process: its argv, environment and how to reach it."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]
    host: str
    port: int
    #: Fields the launcher must not echo into a log or an argument list.
    secret_argument_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class EngineStatus:
    """A normalized status document.

    This is the shape the proxy compares against the plan. Each adapter produces it from whatever
    its engine actually serves - ``/props`` for llama.cpp, the worker's own status endpoint for
    MLX - so the comparison is written once and no engine is asked to implement a harness endpoint.
    """

    model_alias: str
    model_path: str
    #: Slots the engine reports as configured, not slots currently busy.
    total_slots: int
    #: Per-sequence context the engine will actually enforce.
    context_tokens: int
    awake: bool
    identity_fragment: str
    #: Anything the engine reported that diagnostics will show but no decision reads.
    observations: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_context_tokens(self) -> int:
        return self.context_tokens * self.total_slots


@dataclass(frozen=True)
class Computed:
    """The result of a compute canary: what the engine produced, not whether it answered."""

    outcome: str
    content_chars: int = 0
    tool_call: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class EngineAdapter(Protocol):
    """The seven operations an engine must support to be servable by this harness."""

    kind: str

    def provision(self, *, install: bool = True) -> Runtime:
        """Return a usable runtime, or raise. ``install=False`` must never touch the network.

        Eligibility probing calls this on every picker keystroke, so a missing runtime with
        ``install=False`` is a normal answer and not an error to surface as a failure.
        """

    def identity(self, runtime: Runtime) -> EngineIdentity:
        """Ask the runtime what it is. Used to bind the plan to these exact bytes."""

    def validate_artifact(self, model: Mapping[str, Any], lock: Mapping[str, Any]) -> Artifact:
        """Check the verified bytes are loadable *by this engine* and describe them."""

    def launch(
        self,
        model: Mapping[str, Any],
        artifact: Artifact,
        runtime: Runtime,
        engine: Mapping[str, Any],
        token: str,
    ) -> Launch:
        """Build the command line from the resolved plan. Reads policy, never sets it."""

    def normalize_status(self, payload: Mapping[str, Any]) -> EngineStatus:
        """Convert one engine's own status document into the canonical fields."""

    def status_request(self, base: str, token: str) -> Any:
        """The request that fetches the status document, or raise if this engine has none."""

    def verify_status(self, status: EngineStatus, engine: Mapping[str, Any]) -> None:
        """Refuse a running engine that differs from the plan it was launched from."""

    def canary(
        self, origin: str, token: str, model: Mapping[str, Any], engine: Mapping[str, Any]
    ) -> Computed:
        """Prove the engine can compute. Metadata agreement is not readiness."""

    def diagnostics(self, model: Mapping[str, Any], engine: Mapping[str, Any]) -> Mapping[str, Any]:
        """Optional adapter-specific evidence. Must never be required for a launch."""


class Registry:
    """Engine kind to adapter, populated by the modules that define them."""

    def __init__(self) -> None:
        self._adapters: dict[str, EngineAdapter] = {}

    def register(self, adapter: EngineAdapter) -> EngineAdapter:
        self._adapters[adapter.kind] = adapter
        return adapter

    def get(self, kind: str) -> EngineAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            known = ", ".join(sorted(self._adapters)) or "none registered"
            raise EngineAdapterError(
                f"engine kind {kind!r} has no adapter; registered: {known}"
            ) from exc

    def kinds(self) -> Sequence[str]:
        return sorted(self._adapters)


#: The process-wide registry. Adapters register themselves at import so a missing optional
#: dependency can disable one engine without breaking the others.
registry = Registry()


def normalize_engine_document(
    payload: Mapping[str, Any], *, require_mapping: str = "status document"
) -> dict[str, Any]:
    """Reject anything that is not a flat JSON object before an adapter reads fields from it.

    Adapters receive parsed JSON from a process the harness started but does not control. A list, a
    scalar, or a nested object where a field is expected must fail closed with a message naming the
    document, not raise a ``TypeError`` three frames later inside a field lookup.
    """
    if not isinstance(payload, Mapping):
        raise EngineAdapterError(f"engine {require_mapping} must be an object")
    return {str(key): value for key, value in payload.items()}


def positive_int(value: Any, name: str) -> int:
    """Read an integer the plan will multiply, refusing the ``True == 1`` class of input."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EngineAdapterError(f"engine {name} must be a positive integer")
    return value
