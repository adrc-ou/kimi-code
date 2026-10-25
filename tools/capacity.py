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
from pathlib import Path
from typing import Any

if __package__:
    from . import host_caps, local_llm
    from .private_file import write_private_json
else:
    import host_caps
    import local_llm
    from private_file import write_private_json

MIB = 1024**2
GIB = 1024**3


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


def allocate(models: list[dict], providers: dict, reserved: int) -> dict[str, Any]:
    """Resolve full native windows and declared slots; memory is not an input."""
    engines = {}
    for model in models:
        provider = providers[model["provider"]]
        config, profile = provider["capacity"], provider["execution"]["profile"]
        context, slots = model["native_tokens"], config["sequence_slots"]
        if config["strategy"] != "native_context":
            raise CapacityError("local capacity requires the native_context strategy")
        if type(context) is not int or context <= reserved:
            raise CapacityError(f"{model['id']} cannot fit the configured compaction reserve")
        if context % profile["context_granularity"]:
            raise CapacityError(f"{model['id']} native context is not engine-aligned")
        if type(slots) is not int or not 0 < slots <= profile["max_sequences"]:
            raise CapacityError(f"{model['id']} sequence slots exceed engine capabilities")
        engines[model["id"]] = {
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
            raise CapacityError("the llama.cpp capacity adapter currently supports Metal only")
        if not local_llm.is_ready(model):
            raise CapacityError(f"{model['id']}: install and verify the selected weights first")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise CapacityError("the llama.cpp adapter requires Apple silicon Metal")
    identity = engine_identity(local_llm.engine_binary())
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
        entry.update(identity)
        entry.update(
            {
                "snapshot_id": snapshot_id,
                "model_alias": model["model"],
                "model_path": str(local_llm.primary_weights_path(model).resolve()),
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
    return result
