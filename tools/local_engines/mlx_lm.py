#!/usr/bin/env python3
"""The MLX-LM adapter: Apple's own array framework, served by a harness-written worker.

llama.cpp is a foreign binary the harness downloads and supervises. MLX is a Python library that
runs in-process, so this adapter's job differs in kind and not just in detail: it points at an
interpreter inside a provisioned native runtime rather than a released executable, and the HTTP
surface it verifies against belongs to a worker the harness itself wrote rather than to an upstream
server whose flags and defaults change between releases.

The adapter never imports MLX. Nothing in ``tools/`` may: the launcher runs on the operator's own
``python3``, which has no MLX and must not acquire one, and CI runs on Linux where the wheels do not
exist. MLX is loaded by the worker process, inside the provisioned runtime, on Apple silicon.

Nothing here decides a context window, a slot count, or a wiring target. Those arrive in the
resolved plan and this module reports whether the engine agrees with them.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from local_engines.base import (
    Artifact,
    Computed,
    EngineAdapterError,
    EngineIdentity,
    EngineStatus,
    Launch,
    Registry,
    Runtime,
    normalize_engine_document,
    positive_int,
)


class MlxLmAdapter:
    """MLX-LM served by ``tools/local_model_server.py`` in a provisioned native runtime."""

    kind = "mlx_lm"

    # ---------------------------------------------------------------- provision

    def provision(self, *, install: bool = True) -> Runtime:
        import local_mlx_runtime

        try:
            staged = local_mlx_runtime.provision(install=install)
        except local_mlx_runtime.ProvisionError as exc:
            raise EngineAdapterError(str(exc)) from exc
        return Runtime(
            kind=self.kind,
            identity=staged["identity"],
            directory=Path(staged["directory"]),
            executable=Path(staged["executable"]),
            details={
                "python_version": staged["python_version"],
                "lock_digest": staged["lock_digest"],
                "platform": staged["platform"],
            },
        )

    def identity(self, runtime: Runtime) -> EngineIdentity:
        """Ask the provisioned interpreter what MLX it actually resolved.

        The versions come from the runtime rather than from the lock file, because the lock records
        what was requested and an ABI-validating import records what loaded. Those differ when a
        wheel was replaced under the same name, which is exactly what a digest cannot see.
        """
        probe = json.loads(
            _run(
                [
                    str(runtime.executable),
                    "-c",
                    "import json,mlx,mlx_lm;"
                    "print(json.dumps({'mlx': mlx.__version__,"
                    " 'mlx_lm': mlx_lm.__version__}))",
                ],
                cwd=str(runtime.directory),
            )
        )
        return EngineIdentity(
            kind=self.kind,
            build_info=f"mlx {probe['mlx']} mlx-lm {probe['mlx_lm']}",
            source_revision=None,
            version=str(probe["mlx_lm"]),
        )

    # ---------------------------------------------------------------- artifact

    def validate_artifact(self, model: Mapping[str, Any], lock: Mapping[str, Any]) -> Artifact:
        """Require every file an MLX load will consume to be pinned, not just the shards.

        An MLX model is a directory: configuration, tokenizer sources, a chat template, a weight
        index, and the shards the index names. A valid shard means nothing if a file the loader
        reads has drifted beside it, so "all consumed files digested" is the contract here and the
        single GGUF file simply does not have the problem.
        """
        artifact = lock.get("artifact") or {}
        if artifact.get("format") != "mlx":
            raise EngineAdapterError(
                "mlx_lm serving needs a weights lock whose artifact.format is mlx"
            )
        directory = Path(model["weights_dir"])
        name = str(artifact.get("directory") or "")
        root = (directory / name) if name else directory
        if not root.resolve().is_relative_to(directory.resolve()):
            raise EngineAdapterError("artifact.directory escapes weights/")
        paths = tuple(root / entry["path"] for entry in lock["files"])
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise EngineAdapterError(
                "verified MLX files are missing: " + ", ".join(sorted(missing)[:4])
            )
        # The index is the manifest the loader follows, so its references have to land on files the
        # lock actually pinned. A shard present-but-unlisted is refused here rather than discovered
        # by a loader that would happily read it.
        tensor_bytes = _index_tensor_bytes(root, paths)
        return Artifact(
            format="mlx",
            variant=str(artifact.get("variant", "")),
            primary=root,
            paths=paths,
            tensor_bytes=tensor_bytes,
            total_bytes=sum(int(entry["size"]) for entry in lock["files"]),
        )

    # ---------------------------------------------------------------- launch

    def launch(
        self,
        model: Mapping[str, Any],
        artifact: Artifact,
        runtime: Runtime,
        engine: Mapping[str, Any],
        token: str,
    ) -> Launch:
        """Build the *frontend's* command line.

        The supervisor starts one process - the authenticated listener - and that listener owns
        the engine worker as its own child. Handing the supervisor the worker's arguments directly
        would skip the frontend entirely and leave the device with no listener in front of it.

        Neither the credential nor the resolved plan travels in argv: both are unreadable-by-others
        by construction, one on a descriptor and one in the instance directory that only the
        launcher and the worker can read.
        """
        import local_llm

        plan_file = _write_plan_document(engine, artifact, model)
        argv = (
            str(runtime.executable),
            str(_server_path()),
            "--host",
            local_llm.DEFAULT_BIND,
            "--port",
            str(local_llm.engine_port(model)),
            "--worker",
            str(runtime.executable),
            "--worker-arg",
            str(_worker_path()),
            # Everything the worker needs to identify itself is in the plan document, which the
            # frontend writes to it on a descriptor; only the artifact path is a worker argument.
            "--worker-arg",
            "--model-dir",
            "--worker-arg",
            str(artifact.primary),
            "--plan-file",
            str(plan_file),
            # The session credential reaches the frontend on fd 3, never in argv: a token on the
            # command line is visible to every process the operator's machine is running.
            "--token-fd",
            "3",
        )
        return Launch(
            argv=argv,
            # Ambient engine overrides are stripped, not merged: an exported MLX_ or GGML_METAL_
            # variable would otherwise change the residency or cache behaviour the plan just hashed
            # into the engine fingerprint, silently and only on one operator's shell.
            environment=_sanitized_environment(),
            host=local_llm.engine_host(),
            port=local_llm.engine_port(model),
            secret_argument_indices=(),
        )

    # ---------------------------------------------------------------- status

    def status_path(self) -> str:
        return "/v1/harness/status"

    def normalize_status(self, payload: Mapping[str, Any]) -> EngineStatus:
        document = normalize_engine_document(payload)
        settings = document.get("settings")
        if not isinstance(settings, Mapping):
            raise EngineAdapterError("engine status omits its effective settings")
        identity = document.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        return EngineStatus(
            model_alias=str(document.get("model", "")),
            model_path=str(document.get("model_path", "")),
            total_slots=positive_int(settings.get("sequences"), "sequence slots"),
            context_tokens=positive_int(settings.get("context_tokens"), "per-slot context"),
            # A worker that reports no fatal state is awake. ``is_sleeping`` is a llama.cpp notion;
            # this engine has no sleep mode, so absence means awake rather than unknown.
            awake=document.get("fatal") is None,
            identity_fragment=str(identity.get("build_info", "")),
            observations={
                key: document.get(key)
                for key in (
                    "launch_identity",
                    "protocol_version",
                    "tokenizer_identity",
                    "template_identity",
                    "cache_allocation",
                    "wired_limit_bytes",
                    "active_memory_bytes",
                    "peak_memory_bytes",
                    "cache_memory_bytes",
                    "fatal",
                )
                if key in document
            },
        )

    def status_request(self, base: str, token: str) -> Any:
        return urllib.request.Request(
            f"{base}{self.status_path()}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def verify_status(self, status: EngineStatus, engine: Mapping[str, Any]) -> None:
        expected = {
            "model_alias": engine["model_alias"],
            "model_path": engine["model_path"],
            "total_slots": engine["sequences"],
            "context_tokens": engine["context_tokens"],
        }
        actual = {
            "model_alias": status.model_alias,
            "model_path": status.model_path,
            "total_slots": status.total_slots,
            "context_tokens": status.context_tokens,
        }
        for key, value in expected.items():
            if type(actual[key]) is not type(value) or actual[key] != value:
                raise EngineAdapterError(f"engine {key} differs from the resolved plan")
        if not status.awake:
            raise EngineAdapterError("engine has latched a fatal failure")
        # The plan's tokenizer and template identities are compared, not just its numbers: this
        # engine counts tokens with the same preparation path it generates with, so a template that
        # changed under the plan would make every count a count of a different prompt.
        for key in ("tokenizer_identity", "template_identity", "protocol_version"):
            want = engine.get(key)
            got = status.observations.get(key)
            if want is not None and got is not None and got != want:
                raise EngineAdapterError(f"engine {key} differs from the resolved plan")

    def canary(
        self, origin: str, token: str, model: Mapping[str, Any], engine: Mapping[str, Any]
    ) -> Computed:
        """One authenticated streamed generation. Same proof, different transport.

        Shared with llama.cpp deliberately: what is being verified is that *this process on this
        device* can produce a token, and an engine-specific idea of that would let one backend's
        readiness imply the other's.
        """
        import local_llm

        host, _, port = origin.rpartition(":")
        record = local_llm.LocalModelCanary(
            host or "127.0.0.1", int(port), token, model
        ).run()
        return Computed(
            outcome=str(record["outcome"]),
            content_chars=int(record.get("content_chars", 0)),
            tool_call=record.get("tool_canary") == "tool_only",
            detail={
                key: value
                for key, value in record.items()
                if key not in {"outcome", "content_chars"}
            },
        )

    def diagnostics(self, model: Mapping[str, Any], engine: Mapping[str, Any]) -> Mapping[str, Any]:
        """No offline estimator exists for this engine.

        The adapter says so rather than inventing one. ``llama-fit-params`` knows GGML's allocator
        and nothing else; a projected number from it would be a llama.cpp estimate about an MLX
        process, which is the kind of figure that turns into an admission gate by accident.
        """
        return {"projection_engine": None}


def _worker_path() -> Path:
    return Path(__file__).resolve().parent / "mlx_worker.py"


def _write_plan_document(
    engine: Mapping[str, Any], artifact: Artifact, model: Mapping[str, Any]
) -> Path:
    """Materialise the resolved engine record where only the worker can read it.

    The plan document carries the fingerprint, the profile's numeric knobs and the sampling
    defaults. It must reach the worker verbatim - the worker compares its own effective settings
    against it - and it must not sit in argv, where the process table would show it to every other
    user on the machine. The instance runtime directory is the sanctioned home for exactly this
    kind of file, and 0600 is what keeps it there.
    """
    import os

    from private_file import write_private_json

    runtime = os.environ.get("HARNESS_RUNTIME_DIR", "")
    if not runtime:
        raise EngineAdapterError(
            "the MLX frontend needs HARNESS_RUNTIME_DIR to hold the resolved engine document"
        )
    destination = Path(runtime) / "local-engines"
    destination.mkdir(parents=True, exist_ok=True)
    document = {
        key: value
        for key, value in engine.items()
        if key
        in {
            "model_alias",
            "context_tokens",
            "sequences",
            "total_context_tokens",
            "fingerprint",
            "snapshot_id",
            "serving_profile_id",
            "runtime_identity",
            "protocol_version",
            "artifact_format",
            "cache_allocation",
            "tokenizer_identity",
            "template_identity",
            "wiring",
        }
    }
    document["allocation"] = dict(engine.get("allocation") or {})
    document["sampling"] = dict((model.get("install") or {}).get("sampling") or {})
    document["model_dir"] = str(artifact.primary)
    path = destination / f"{str(engine.get('snapshot_id') or 'current')}.engine.json"
    write_private_json(path, document)
    return path


def _server_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tools" / "local_model_server.py"


def _sanitized_environment() -> Mapping[str, str]:
    """Keep operational environment, drop anything an engine would read as policy.

    An inherited ``MLX_MAX_RECOMMENDED_WORKING_SET_SIZE`` or ``GGML_METAL_*`` would silently change
    residency on one operator's machine while the plan still claimed the profile's numbers, and the
    fingerprint would match either way because the override never passed through it.
    """
    import os

    blocked = ("MLX_", "GGML_", "LLAMA_", "PYTORCH_", "CUDA", "Metal")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(key.upper().startswith(prefix) for prefix in blocked)
    }


def _run(argv: list[str], *, cwd: str) -> str:
    import subprocess

    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=True, timeout=120, cwd=cwd
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineAdapterError("cannot interrogate the MLX runtime") from exc
    return result.stdout


def _index_tensor_bytes(root: Path, paths: tuple[Path, ...]) -> int | None:
    """Tensor storage only, read from the safetensors headers of the pinned shards.

    None means "not determinable from what the lock pins", which the caller treats as an unknown
    measurement rather than as zero bytes. Guessing a weight size is how a residency budget ends up
    justified by an invented number.
    """
    index = next((path for path in paths if path.name.endswith(".index.json")), None)
    if index is None:
        return None
    try:
        document = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    references = document.get("weight_map")
    if not isinstance(references, dict):
        return None
    named = {path.name: path for path in paths}
    total = 0
    for shard in set(references.values()):
        path = named.get(str(shard))
        if path is None:
            # The index names a file the lock does not pin. Refuse rather than load a partial set:
            # a missing shard still produces numbers, and those numbers are wrong.
            raise EngineAdapterError(f"MLX index references unpinned shard {shard!r}")
        total += _safetensors_data_bytes(path)
    return total


def _safetensors_data_bytes(path: Path) -> int:
    """Bytes of tensor storage in one safetensors file, from its own header.

    Read from the file rather than taken from the lock's size, because a shard's size also carries
    the header and any non-tensor storage, and the wiring split is proportional to *tensors*.
    """
    try:
        with path.open("rb") as handle:
            length = int.from_bytes(handle.read(8), "little")
            if length <= 0 or length > 64 * 1024 * 1024:
                raise ValueError("implausible safetensors header")
            header = json.loads(handle.read(length))
    except (OSError, ValueError) as exc:
        raise EngineAdapterError(f"cannot read tensor metadata from {path.name}") from exc
    spans = [
        (value.get("data_offsets") or [0, 0])
        for key, value in header.items()
        if key != "__metadata__" and isinstance(value, dict)
    ]
    if not spans:
        raise EngineAdapterError(f"{path.name} declares no tensors")
    return max(int(span[1]) for span in spans) - min(int(span[0]) for span in spans)


def register(target: Registry) -> MlxLmAdapter:
    adapter = MlxLmAdapter()
    target.register(adapter)
    return adapter
