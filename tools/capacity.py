#!/usr/bin/env python3
"""Resolve managed local engines into the same immutable policy plan as remote limits.

Native windows and explicit sequence slots are policy facts, independent of physical
memory. The OS and backend own allocation and paging. Offline projection helpers remain
available for diagnostics only and never approve or resize a launch.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__:
    from . import host_caps, local_llm
    from .local_engines import registry as engine_registry
    from .private_file import write_private_json
else:
    import host_caps
    import local_llm
    from local_engines import registry as engine_registry
    from private_file import write_private_json

MIB = 1024**2
GIB = 1024**3

#: Adapters register here rather than as a side effect of their own import, so "which engines can
#: this process serve" is one readable list. An engine whose module cannot load on this platform
#: simply stays unregistered, and the profile that selects it is then refused at resolution instead
#: of failing after a download.
for _adapter_name in ("llama_cpp", "mlx_lm"):
    try:
        _prefix = __name__.rsplit(".", 1)[0] + "." if __package__ else ""
        _module = __import__(_prefix + "local_engines." + _adapter_name, fromlist=["register"])
    except ImportError:
        continue
    _module.register(engine_registry)


def engine_kinds() -> tuple[str, ...]:
    """Engine kinds with a registered adapter. Nothing is offered that cannot be launched."""
    return tuple(engine_registry.kinds())


def adapter_for(kind: str):
    """The adapter for one declared engine kind, or an error naming what is registered."""
    return engine_registry.get(kind)


#: The wire contract between the proxy and a local engine, published in every engine record so a
#: reader knows which normalization a status document needs before asking for one. Bumped when the
#: meaning of an existing field changes, not when a field is added.
PROTOCOL_VERSION = 1


class CapacityError(ValueError):
    """A native engine contract or an explicitly requested diagnostic is invalid."""


def managed(provider: dict[str, Any]) -> bool:
    return (provider.get("execution") or {}).get("kind") == "local"


def selected(
    providers: dict[str, Any], models: dict[str, Any], selection: dict[str, str]
) -> list[dict[str, Any]]:
    result = []
    for role in ("primary", "subagent"):
        model = models.get(selection.get(role, ""))
        if model and managed(providers[model["provider"]]) and model not in result:
            result.append(model)
    return result


def parse_projection(output: str) -> dict[str, Any]:
    """Read the pinned structured event, never the adjacent human-readable table."""
    events = []
    for line in output.splitlines():
        if not line.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise CapacityError("the engine emitted malformed projection JSON") from exc
        if isinstance(event, dict) and event.get("type") == "fit_memory_breakdown":
            events.append(event.get("data"))
    if len(events) != 1 or not isinstance(events[0], dict):
        raise CapacityError("the engine must emit exactly one memory projection")
    data = events[0]
    if data.get("unit") != "MiB" or not isinstance(data.get("rows"), list):
        raise CapacityError("unsupported memory projection format")
    host = 0
    devices: dict[str, int] = {}
    totals: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for row in data["rows"]:
        if not isinstance(row, dict):
            raise CapacityError("invalid memory projection row")
        kind, name = row.get("kind"), row.get("name")
        amount = row.get("self")
        if (
            kind not in {"device", "host", "buffer_type"}
            or not isinstance(name, str)
            or not name
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount < 0
            or (kind, name) in seen
        ):
            raise CapacityError("invalid or duplicate memory projection row")
        seen.add((kind, name))
        # The engine truncates self to MiB. Charge one whole additional MiB per row.
        charged = (amount + 1) * MIB
        host += charged
        if kind == "device":
            total = row.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
                raise CapacityError(f"device {name} has no measurable memory pool")
            if name in devices:
                raise CapacityError(f"duplicate device {name}")
            devices[name] = charged
            totals[name] = total * MIB
    if not seen or not any(kind == "host" for kind, _ in seen):
        raise CapacityError("the engine projection omits host memory")
    return {"host_bytes": host, "devices": devices, "device_totals": totals}


def engine_identity(binary: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=local_llm.engine_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityError("cannot identify the local inference engine") from exc
    output = result.stdout + result.stderr
    match = re.search(r"build\s+(\d+),\s+commit\s+([0-9a-f]{7,40})", output)
    if match is None:
        raise CapacityError("the local engine has an unsupported version response")
    expected = str(local_llm.engine_plan().get("version", ""))
    if expected != f"b{match[1]}":
        raise CapacityError(f"local engine build differs from the pinned release {expected}")
    commit = str(local_llm.engine_plan().get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not commit.startswith(match[2]):
        raise CapacityError("local engine commit differs from the pinned serving profile")
    return {
        "build_info": output.strip().splitlines()[0],
        "build_commit": match[2],
        "source_commit": commit,
    }


def project(
    model: dict[str, Any],
    profile: dict[str, Any],
    context: int,
    sequences: int,
    estimator: Path,
    record_path: Path | None = None,
) -> dict[str, Any]:
    args = [str(estimator), "--model", str(local_llm.primary_weights_path(model))]
    args += local_llm.allocation_args(profile, context * sequences, sequences, server=False)
    args += ["--fit-print", "on", "--log-jsonl"]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            env=local_llm.engine_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if record_path:
            detail = str(getattr(exc, "stderr", "") or "")[-65536:]
            with record_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "model_id": model["id"],
                            "context": context,
                            "sequences": sequences,
                            "failure": detail,
                        }
                    )
                    + "\n"
                )
        hint = "; inspect capacity-projections.jsonl" if record_path else ""
        raise CapacityError(f"cannot project {model['id']} with the pinned engine{hint}") from exc
    output = result.stdout + "\n" + result.stderr
    try:
        estimate = parse_projection(output)
        if not estimate["devices"] or any(
            not re.fullmatch(r"MTL\d+", device) for device in estimate["devices"]
        ):
            raise CapacityError(
                "the pinned Metal adapter requires measured Metal device allocations"
            )
    except CapacityError as exc:
        if record_path:
            with record_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "model_id": model["id"],
                            "context": context,
                            "sequences": sequences,
                            "failure": str(exc),
                            "output": output[-65536:],
                        }
                    )
                    + "\n"
                )
        raise
    if record_path:
        with record_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "model_id": model["id"],
                        "context": context,
                        "sequences": sequences,
                        "projection": estimate,
                    }
                )
                + "\n"
            )
    return estimate


def wiring_targets(
    engines: dict[str, Any], providers: dict[str, Any], measured: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Resolve the provider's declared residency strategy into one deliberate-wiring target.

    This bounds *how much an engine asks to be pinned in memory*. It is not an admission test and it
    cannot resize a context window or a slot count: those are decided in :func:`allocate`, from the
    model's declared native context, before this function is ever reached. Nothing here reads a
    number back into a capacity decision.

    Returns ``engine_id -> {"wired_bytes", "reason", "controllable"}``. A zero is a real answer
    meaning "wire nothing deliberately, let the OS page", and is reported as such rather than being
    confused with "no opinion".
    """
    result: dict[str, dict[str, Any]] = {}
    if not engines:
        return result
    memory = _memory_policy(engines, providers)
    strategy = memory.get("strategy", "bounded_wiring")

    # An engine that pins memory however it likes defeats the aggregate for everyone sharing the
    # device, and unified memory means everyone does. llama.cpp is such an engine today: its Metal
    # residency is set inside the library, not by this harness, so the only honest aggregate with
    # one selected is "nothing else gets wired deliberately".
    uncontrolled = sorted(
        engine_id
        for engine_id in engines
        if engines[engine_id]["engine_kind"] not in _WIRING_CONTROLLABLE
    )
    if strategy == "uncontrolled":
        uncontrolled = sorted(engines)

    available = _as_positive(measured.get("available_memory_bytes"))
    recommended = _as_positive((measured.get("metal") or {}).get("recommended_working_set_bytes"))
    unknown_policy = memory.get("unknown_measurements", "unwired")

    if uncontrolled:
        for engine_id in engines:
            result[engine_id] = {
                "wired_bytes": 0,
                "controllable": engine_id not in uncontrolled,
                "reason": (
                    "residency is set by this engine"
                    if engine_id in uncontrolled
                    else "another selected engine sets its own residency: "
                    + ", ".join(uncontrolled)
                ),
            }
        return result

    if strategy != "bounded_wiring":  # pragma: no cover - the parser refuses other values
        raise CapacityError(f"unknown memory strategy {strategy!r}")

    # min(), never sum(): available memory already accounts for what is resident now, and adding a
    # device recommendation to it would double-count the same physical pages.
    candidates = [value for value in (available, recommended) if value is not None]
    if not candidates:
        if unknown_policy == "refuse":
            raise CapacityError(
                "memory measurements unavailable and unknown_measurements = refuse"
            )
        for engine_id in engines:
            result[engine_id] = {
                "wired_bytes": 0,
                "controllable": True,
                "reason": "measurements unavailable: deliberate wiring disabled, not zero-sized",
            }
        return result
    aggregate = min(candidates)

    weights = {
        engine_id: _as_positive(engines[engine_id].get("tensor_bytes")) for engine_id in engines
    }
    if any(value is None or value <= 0 for value in weights.values()):
        # Proportional division needs a real denominator. An estimate invented here would become the
        # justification for a residency decision, which is the failure mode the whole memory
        # refactor exists to remove.
        for engine_id in engines:
            result[engine_id] = {
                "wired_bytes": 0,
                "controllable": True,
                "reason": "verified tensor bytes unavailable: deliberate wiring disabled",
            }
        return result

    # Cap first, then divide. Proportional shares are computed against the capped demand, so a
    # worker whose weights are smaller than its share cannot strand the surplus: with three one-byte
    # models and a ten-byte aggregate, capping after distribution would allocate three bytes and
    # quietly waste seven that the device was willing to pin.
    demand = {
        engine_id: min(int(weight), aggregate) for engine_id, weight in weights.items()
    }
    total_demand = sum(demand.values())
    if total_demand <= 0:  # pragma: no cover - every weight was verified positive above
        aggregate = 0
        total_demand = 1
    # Scale the aggregate into the demand space with integers, accepting at most one byte per
    # worker of round-down loss, then hand that loss out in qualified-identity order so two
    # launches of the same plan always compute the same numbers.
    shares = {engine_id: (aggregate * value) // total_demand for engine_id, value in demand.items()}
    remainder = aggregate - sum(shares.values())
    for engine_id in sorted(shares):
        if remainder <= 0:
            break
        if shares[engine_id] < demand[engine_id]:
            shares[engine_id] += 1
            remainder -= 1
    # Whatever is still unassigned after every worker is at its cap stays unassigned on purpose:
    # more memory than the weights can use must not become permission to pin a growing cache.
    for engine_id, value in shares.items():
        capped = min(value, demand[engine_id])
        result[engine_id] = {
            "wired_bytes": capped,
            "controllable": True,
            "reason": (
                "capped at verified tensor bytes"
                if capped == weights[engine_id]
                else "min(available, recommended working set) shared by verified tensor bytes"
            ),
        }
    return result


def _memory_policy(engines: dict[str, Any], providers: dict[str, Any]) -> dict[str, Any]:
    """The provider's declared memory strategy, refusing a mix of two different declarations."""
    declared = {
        json.dumps(
            providers[model_provider(engines, engine_id)]["execution"].get("memory", {}),
            sort_keys=True,
        )
        for engine_id in engines
    }
    if len(declared) > 1:
        raise CapacityError(
            "selected engines declare different [execution.memory] policies; a shared device "
            "cannot be bounded by two aggregate targets at once"
        )
    return json.loads(declared.pop()) if declared else {}


def model_provider(engines: dict[str, Any], engine_id: str) -> str:
    """Which provider owns one engine id. Engine ids are qualified model ids, so this is a split."""
    return engine_id.split("/", 1)[0]


def _as_positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


#: Engines whose deliberate residency this harness can actually set, through an API it does not
#: have to patch. llama.cpp is absent because its Metal wiring is decided inside the library.
_WIRING_CONTROLLABLE = frozenset({"mlx_lm"})


def allocate(models: list[dict], providers: dict, reserved: int) -> dict[str, Any]:
    """Resolve full native windows and declared slots; memory is not an input."""
    engines = {}
    for model in models:
        provider = providers[model["provider"]]
        config = provider["capacity"]
        # The profile is read from the model, which named it. Taking the provider's default here
        # would let one model select an engine and be allocated another engine's alignment, and with
        # several profiles there is no longer a single "the" provider profile to fall back to.
        profile = model["serving_settings"]
        kind = model["serving_engine"]
        context, slots = model["native_tokens"], config["sequence_slots"]
        if config["strategy"] != "native_context":
            raise CapacityError("local capacity requires the native_context strategy")
        if kind not in engine_kinds():
            raise CapacityError(
                f"{model['id']} selects engine {kind!r}, which has no adapter registered"
            )
        if type(context) is not int or context <= reserved:
            raise CapacityError(f"{model['id']} cannot fit the configured compaction reserve")
        if context % profile["context_granularity"]:
            raise CapacityError(f"{model['id']} native context is not engine-aligned")
        if type(slots) is not int or not 0 < slots <= profile["max_sequences"]:
            raise CapacityError(f"{model['id']} sequence slots exceed engine capabilities")
        engines[model["id"]] = {
            # Identity the proxy needs before it will trust a status document, and which no longer
            # assumes every engine has a llama.cpp build commit to compare a substring against.
            "engine_kind": kind,
            "serving_profile": model["serving_profile"],
            "protocol_version": PROTOCOL_VERSION,
            "capacity_strategy": "native_context",
            "memory_management": "os_managed",
            "native_context_tokens": context,
            "context_tokens": context,
            "sequences": slots,
            "total_context_tokens": context * slots,
            "allocation": profile,
            "validation_status": config.get("validation_status"),
            "validation_evidence": config.get("validation_evidence", []),
        }
    return {"engines": engines}


def projection_engine(*, install: bool = True) -> tuple[Path, dict[str, str]]:
    """Identify the pinned estimator for an explicitly requested diagnostic."""
    binary = local_llm.engine_binary(install=install)
    estimator = binary.with_name("llama-fit-params")
    if not estimator.is_file():
        raise CapacityError("the selected engine has no supported memory projection command")
    identity = engine_identity(binary)
    if engine_identity(estimator)["build_commit"] != identity["build_commit"]:
        raise CapacityError("server and memory estimator builds differ")
    return estimator, identity


def prepare(
    root: Path,
    runtime: Path,
    providers: dict[str, Any],
    models: dict[str, Any],
    selection: dict[str, str],
    reserved: int,
) -> dict[str, Any]:
    """Prepare installed, verified engines before final policy resolution and Kimi staging."""
    local = selected(providers, models, selection)
    if not local:
        return {"engines": {}}
    for model in local:
        if providers[model["provider"]].get("requires") != ["metal"]:
            raise CapacityError("every managed local engine in this provider requires Metal")
        if not local_llm.is_ready(model):
            raise CapacityError(f"{model['id']}: install and verify the selected weights first")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise CapacityError("the managed local engines require Apple silicon Metal")
    # Observations are retained for diagnostics, never as an admission requirement.
    try:
        measured = host_caps.probe(root)
    except (OSError, ValueError):
        measured = {"memory_bytes": 0, "available_memory_bytes": 0}
    snapshot_id = uuid.uuid4().hex
    measured["snapshot_id"] = snapshot_id
    write_private_json(runtime / host_caps.CAPS_FILE, measured)
    result = allocate(local, providers, reserved)
    for model in local:
        entry = result["engines"][model["id"]]
        adapter = adapter_for(entry["engine_kind"])
        # Identity, artifact description and runtime come from the adapter, which is the only thing
        # that knows how this particular engine reports them. The resolver passes the plan's own
        # numbers in and reads nothing back but facts.
        runtime_ = adapter.provision(install=True)
        identity = adapter.identity(runtime_).as_record()
        # The lock is read inside the adapter, which is the only party that knows which of its
        # fields describe a loadable artifact for this engine.
        artifact = adapter.validate_artifact(model, local_llm.read_lock(Path(model["lock_path"])))
        entry.update(identity)
        entry.update(
            {
                "engine_kind": entry["engine_kind"],
                "runtime_identity": runtime_.identity,
                "runtime_details": dict(runtime_.details),
                "snapshot_id": snapshot_id,
                "model_alias": model["model"],
                "model_path": str(artifact.primary.resolve()),
                "artifact_format": artifact.format,
                "artifact_variant": artifact.variant,
                "tensor_bytes": artifact.tensor_bytes,
                "artifact_bytes": artifact.total_bytes,
                # Cache allocation is declared by the profile and recorded in the engine so the
                # proxy can tell a growing-cache engine from a preallocated one without re-reading
                # the provider tree. It is a description of behaviour, never a capacity input.
                "cache_allocation": entry["allocation"].get("kv_allocation", "preallocated"),
                "tokenizer_identity": None,
                "template_identity": None,
                "model_lock_digest": hashlib.sha256(
                    Path(model["lock_path"]).read_bytes()
                ).hexdigest(),
                "model_manifest_digest": hashlib.sha256(
                    (Path(model["path"]) / "model.toml").read_bytes()
                ).hexdigest(),
                "provider_manifest_digest": hashlib.sha256(
                    (Path(providers[model["provider"]]["path"]) / "provider.toml").read_bytes()
                ).hexdigest(),
                "placement": {"backend": "metal"},
                "memory_policy": providers[model["provider"]]["execution"].get("memory", {}),
                "residency_group": None,
                "serving_profile_id": hashlib.sha256(
                    json.dumps(
                        {"engine": identity, "allocation": entry["allocation"]}, sort_keys=True
                    ).encode()
                ).hexdigest(),
            }
        )
        entry["fingerprint"] = hashlib.sha256(
            json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    # Residency is resolved *after* every engine record is final, so the same measurements that a
    # diagnostic will show are the ones the numbers came from, and no engine's wiring can depend on
    # a peer that had not been priced yet.
    wiring = wiring_targets(result["engines"], providers, measured)
    for engine_id, decision in wiring.items():
        # Recorded beside the plan, not inside the fingerprint: a different available-memory reading
        # must not look like a different engine, or every launch would invalidate its own receipt.
        result["engines"][engine_id]["wiring"] = decision
        if decision["controllable"] and decision["wired_bytes"]:
            result["engines"][engine_id]["residency_group"] = "metal-default"
    return result
