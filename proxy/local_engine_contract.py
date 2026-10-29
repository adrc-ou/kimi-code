#!/usr/bin/env python3
"""The shared vocabulary for what a local generation actually amounted to.

An engine that answers ``/props`` and returns HTTP 200 has not necessarily produced an answer.
The recorded failure this exists because of had a Metal backend that could no longer compute while
its metadata endpoint stayed polite, so every observer that judged the response by its status line
called it a success and retried it forever. The fix cannot be a boolean: "not usable" covers a
refusal, a thinking block with no answer behind it, a stream cut off at the output bound, and a
device that has stopped working, and those want opposite responses from a proxy that holds a scarce
execution slot and must not clear a dead engine just because it answered a metadata call.

So this module owns one thing - the names - and deliberately owns no I/O.

## Why this file is separate from ``model_proxy.py``

``model_proxy.py`` imports aiohttp at module scope, so anything defined inside it is unreachable
from the host side: ``tools/local_llm.py`` runs the launcher, and the engine worker runs the model,
and neither has the proxy's dependencies. A vocabulary invented in step 2 of the implementation and
re-invented in step 3's contract module drifts, and this repository already demonstrates the cost:
``tools/local_llm.py`` ``validate_properties()`` is a hand copy of ``verify()`` in the proxy, same
fields, same strictness, different exception type. This file is the standard-library-only place both
sides state the same fact, which is the only reason both sides can state it at all.

## Two observers, and which question each may answer

The worker sees the device. The proxy sees the bytes the client received. Neither can substitute
for the other, and R3 was precisely one observer being trusted about a signal it was not positioned
to see, so authority is split by kind of judgment rather than crowned:

* the **proxy** is authoritative on whether the client received a usable completion, because it owns
  the wire and the retry loop, and the worker cannot know what arrived;
* the **worker** is authoritative on latching a fatal failure, and on clearing one, because only it
  sees an allocator or driver give up. A healthy metadata observation is a readiness signal and must
  never reset a latched failure - that is the bug, not a trade-off.

Where the two disagree, :func:`reconcile` refuses to call it success and also refuses to condemn the
engine, because a contract violation between two observers is not evidence the device is broken.

Pure standard library. No engine imports, no MLX, no wire details.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "DIVERGENT",
    "OUTCOMES",
    "STATUS_REQUIRED_FIELDS",
    "FailureClass",
    "WorkerError",
    "status_document",
    "validate_status_document",
    "Outcome",
    "Reconciliation",
    "StreamEvidence",
    "classify",
    "implies_engine_damage",
    "is_usable_completion",
    "partition_for_outcome",
    "partition_for_status",
    "reconcile",
]

#: The value :func:`reconcile` reports when the worker declared one outcome and the wire showed
#: another. It is not a member of :class:`Outcome` because no stream ever produced it: it names a
#: disagreement between observers, not something that arrived.
DIVERGENT = "divergent"


class Outcome(StrEnum):
    """How one local completion ended, as far as an observer can tell.

    The members split along the line that actually matters downstream - whether the caller received
    something it can act on - rather than along transport status, which is the distinction that
    failed in the first place.
    """

    #: Text arrived. The ordinary good case.
    CONTENT = "content"
    #: The model called a tool and wrote no prose. Valid, and the single most common reason an
    #: agent turn looks empty to a log that only counts characters.
    TOOL_ONLY = "tool_only"
    #: A thinking block arrived and nothing after it. The engine computed; the caller still has no
    #: answer. Not usable, and not damage either - see :func:`implies_engine_damage`.
    REASONING_ONLY = "reasoning_only"
    #: Generation stopped on the output bound with real text behind it. The request was well formed
    #: and the engine was working; the budget ran out.
    TRUNCATED = "truncated"
    #: The stream completed cleanly and produced nothing at all. An immediate end of stream is
    #: commonly a sampling or template artifact rather than a broken device, so this is reported
    #: apart from failure and deliberately not fatal on its own.
    EMPTY = "empty"
    #: The engine sent an error object inside a response it had already begun.
    ERROR_OBJECT = "error_object"
    #: The framing itself was unusable: a partial event, a non-JSON payload, a stream that ended
    #: mid-event without a terminating blank line.
    FRAMING_FAILURE = "framing_failure"
    #: An observer could not place the response in this vocabulary. Treated as not usable.
    UNKNOWN = "unknown"


#: Every outcome a classifier may legitimately report.
OUTCOMES = frozenset(Outcome)


#: Which transport-shaped failure this is, which decides whether a retry could ever help.
#:
#: Kept separate from :class:`Outcome` because the same outcome can arrive by different routes, and
#: because "the caller got nothing" and "the engine is broken" are different statements that were
#: previously being collapsed into one 503.
class FailureClass(StrEnum):
    #: The engine cannot serve until it is restarted. Retrying is a loop, not a recovery.
    FATAL = "fatal"
    #: The request was wrong. Retrying the same bytes is a loop for a different reason.
    INVALID = "invalid"
    #: The transport or the deadline gave up. The next attempt may genuinely succeed.
    TRANSIENT = "transient"


class WorkerError(StrEnum):
    """Why a worker refused to do something, as a stable machine-readable label.

    These are the codes a worker reports; they are *not* a client-facing error taxonomy. What Kimi
    parses is settled by U1 and stays on the standard OpenAI shapes, so a code added here must never
    be forwarded verbatim to a session. AIP-193's rule applies to the label itself: once any
    consumer has seen a code it keeps meaning the same thing, so codes are added and never reused.
    """

    #: The request asked for something this engine cannot do.
    UNSUPPORTED_SETTING = "unsupported_setting"
    #: The body was not parseable, too large, or carried a field this worker does not accept.
    MALFORMED_REQUEST = "malformed_request"
    #: The bound model was not the one requested. A worker serves exactly one model and says so.
    MODEL_MISMATCH = "model_mismatch"
    #: A device or allocator failure the worker cannot recover from.
    COMPUTE_FAILED = "compute_failed"
    #: The caller cancelled. Not an error the engine caused.
    CANCELLED = "cancelled"
    #: The worker is executing and will not take a second request.
    BUSY = "busy"


#: The fields a harness status document must carry, and the type each one has.
#:
#: This is the contract that replaces "trust that /props looks right": the worker builds its status
#: through :func:`status_document` and every reader validates against the same table, so a missing
#: field is a construction error rather than a ``None`` that reaches a comparison three layers away.
STATUS_REQUIRED_FIELDS: dict[str, type] = {
    "engine_kind": str,
    "model": str,
    "model_path": str,
    "protocol_version": int,
    "ready": bool,
    "context_tokens": int,
    "sequences": int,
    "total_context_tokens": int,
    "fingerprint": str,
    "launch_identity": str,
    "runtime_identity": str,
    "serving_profile_id": str,
    "tokenizer_identity": str,
    "template_identity": str,
    "artifact_format": str,
    "cache_allocation": str,
}


def status_document(**fields: Any) -> dict[str, Any]:
    """Assemble a worker status document, refusing an incomplete one.

    An engine cannot be attested to partially: a document missing its template identity would read
    as healthy to the proxy and then contradict the plan at the first count. Failing at
    construction moves that from a confusing runtime rejection to a worker that will not start.
    """
    missing = sorted(name for name in STATUS_REQUIRED_FIELDS if name not in fields)
    if missing:
        raise ValueError(f"status document omits required fields: {', '.join(missing)}")
    wrong = sorted(
        name
        for name, kind in STATUS_REQUIRED_FIELDS.items()
        if type(fields[name]) is not kind
    )
    if wrong:
        raise ValueError(f"status document fields have the wrong type: {', '.join(wrong)}")
    declared = fields["context_tokens"] * fields["sequences"]
    if declared != fields["total_context_tokens"]:
        raise ValueError(
            f"status total_context_tokens {fields['total_context_tokens']} != "
            f"{fields['context_tokens']} x {fields['sequences']}"
        )
    optional = {
        key: value
        for key, value in fields.items()
        if key not in STATUS_REQUIRED_FIELDS and key not in {"fatal", "observations", "settings"}
    }
    document = {
        **fields,
        "settings": {
            "context_tokens": fields["context_tokens"],
            "sequences": fields["sequences"],
        },
        "identity": {
            "build_info": str(fields.get("build_info", "")),
            "source_revision": fields.get("source_revision"),
            "version": fields.get("version"),
        },
        "fatal": fields.get("fatal"),
    }
    observations = {**optional, **(fields.get("observations") or {})}
    if observations:
        document["observations"] = observations
    return document


def validate_status_document(payload: Any) -> dict[str, Any]:
    """Check a document received over the wire against the same table that built it.

    Returns the payload as a dict. Raises on anything that would make a later comparison read a
    missing field as agreement - which is how a ``None`` context length becomes a plan mismatch
    three frames away and an operator spends an afternoon finding it.
    """
    if not isinstance(payload, dict):
        raise ValueError("engine status document must be an object")
    missing = sorted(name for name in STATUS_REQUIRED_FIELDS if name not in payload)
    if missing:
        raise ValueError(f"engine status document omits: {', '.join(missing)}")
    wrong = sorted(
        name
        for name, kind in STATUS_REQUIRED_FIELDS.items()
        if type(payload[name]) is not kind
    )
    if wrong:
        raise ValueError(f"engine status document has wrong field types: {', '.join(wrong)}")
    return payload


def is_usable_completion(outcome: Outcome) -> bool:
    """Whether the caller received something an agent can act on.

    Deliberately narrower than "the request succeeded". Reasoning without an answer and truncation
    without a decision are both cases where retrying or surfacing beats pretending.
    """
    return outcome in (Outcome.CONTENT, Outcome.TOOL_ONLY)


def implies_engine_damage(outcome: Outcome) -> bool:
    """Whether this outcome is evidence the backend itself has stopped working.

    Only framing and in-band errors say that. An empty completion says the sampler produced nothing,
    which is a model behavior, and a truncated one says the budget was spent, which is arithmetic.
    Latching on those would turn a bad prompt into a dead session.
    """
    return outcome in (Outcome.ERROR_OBJECT, Outcome.FRAMING_FAILURE)


@dataclass(frozen=True)
class StreamEvidence:
    """What one completion stream showed, as facts rather than as a verdict.

    Every field is a count or a flag a reader can check against a capture. The classification rule
    lives in :func:`classify` so a disagreement about policy is one function, not a scattered set of
    conditionals in the proxy's forwarding path.
    """

    content_chars: int = 0
    reasoning_chars: int = 0
    tool_calls: int = 0
    #: The ``finish_reason`` of the last choice that reported one, if any.
    finish_reason: str | None = None
    #: True when the stream reached a terminating event (``[DONE]`` or a complete final event).
    terminated: bool = False
    #: True when an error object arrived, including inside an otherwise successful response.
    saw_error_object: bool = False
    #: True when bytes arrived that could not be framed into an event at all.
    saw_framing_failure: bool = False

    def damage_observed(self) -> bool:
        return implies_engine_damage(classify(self))


def classify(evidence: StreamEvidence) -> Outcome:
    """Reduce one stream's evidence to a single :class:`Outcome`.

    Ordered most-specific first: a framing failure outranks content that arrived before it, because
    a response the client cannot parse is not a response. Clean termination is checked before the
    content counts for the same reason as the reverse - a stream that never ended has not produced a
    completion however much arrived.
    """
    if evidence.saw_framing_failure:
        return Outcome.FRAMING_FAILURE
    if evidence.saw_error_object:
        return Outcome.ERROR_OBJECT
    if not evidence.terminated:
        return Outcome.TRUNCATED if _produced_anything(evidence) else Outcome.FRAMING_FAILURE
    if evidence.finish_reason == "length":
        return Outcome.TRUNCATED
    if evidence.content_chars > 0:
        return Outcome.CONTENT
    if evidence.tool_calls > 0:
        return Outcome.TOOL_ONLY
    if evidence.reasoning_chars > 0:
        return Outcome.REASONING_ONLY
    return Outcome.EMPTY


def _produced_anything(evidence: StreamEvidence) -> bool:
    return bool(
        evidence.content_chars or evidence.reasoning_chars or evidence.tool_calls
    )


def partition_for_outcome(outcome: Outcome) -> FailureClass | None:
    """The retry consequence of a completion that ended this way, or ``None`` if it is no failure.

    Unusable-but-intact outcomes return ``None`` on purpose. The proxy must stop treating them as
    successes, which is what :func:`is_usable_completion` is for, but classifying them as a failure
    class invites the fatal latch that this design keeps under the worker.
    """
    if outcome in (Outcome.ERROR_OBJECT, Outcome.FRAMING_FAILURE, Outcome.UNKNOWN):
        return FailureClass.FATAL
    return None


def partition_for_status(status: int, *, body_rejects: bool = False) -> FailureClass:
    """Classify a non-2xx response the engine returned outright.

    A 4xx is the engine refusing the request, which no amount of re-dispatching fixes, and which
    must never be allowed to look like a dead device. Everything else is treated as transport
    until recovery is demonstrated.
    """
    if body_rejects or 400 <= status < 500 and status != 429:
        return FailureClass.INVALID
    return FailureClass.TRANSIENT


@dataclass(frozen=True)
class Reconciliation:
    """The verdict after both observers have reported.

    ``divergent`` is a field rather than an outcome because the two facts are independent: the proxy
    still has to tell the client whether it received an answer, and the operator still has to be
    able to see that the engine contradicted itself.
    """

    outcome: Outcome
    divergent: bool

    @property
    def usable(self) -> bool:
        return is_usable_completion(self.outcome)

    @property
    def failure_class(self) -> FailureClass | None:
        """What this verdict implies about the engine, as far as the proxy may claim.

        ``None`` when the observers disagreed. Divergence is a defect in the contract between two
        reporters, and the proxy has no authority to declare a device dead because of it - latching
        belongs to the worker alone. The verdict still reports as not usable, so nothing is smuggled
        past the client.
        """
        if self.divergent:
            return None
        return partition_for_outcome(self.outcome)


def reconcile(declared: Outcome | None, inferred: Outcome) -> Reconciliation:
    """Combine a worker's self-report with the proxy's reading of the wire.

    A worker that does not declare an outcome is not a contradiction, it is a backend predating this
    contract, so the proxy's inference simply governs. When both report and disagree, the result is
    :attr:`Outcome.UNKNOWN`: never a success, and never a fatal latch, because the two observers
    disagreeing is a defect in the contract and not proof that the device stopped working.
    """
    if declared is None:
        return Reconciliation(outcome=inferred, divergent=False)
    if declared == inferred:
        return Reconciliation(outcome=inferred, divergent=False)
    return Reconciliation(outcome=Outcome.UNKNOWN, divergent=True)
