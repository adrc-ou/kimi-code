#!/usr/bin/env python3
"""The llama.cpp adapter.

This module *delegates* to the engine-specific code that already lives in ``tools/local_llm.py``
rather than relocating it. That is a deliberate choice and not an unfinished refactor: that code is
under test, its failure semantics were just rewritten, and moving several hundred lines to buy no
change in behaviour would trade a green suite for a diff nobody can review. What matters is the
boundary - callers reach llama.cpp through the adapter and not by naming ``llama-server`` - and the
boundary is established here.

``tools/local_llm.py`` keeps the installer, the CLI and the supervisor, which are engine-neutral,
and stops being the place where a flag spelling is decided.
"""

from __future__ import annotations

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


class LlamaCppAdapter:
    """llama.cpp over Metal, served by the pinned ``llama-server`` release."""

    kind = "llama_cpp"

    # ---------------------------------------------------------------- provision

    def provision(self, *, install: bool = True) -> Runtime:
        import local_llm

        binary = local_llm.engine_binary(install=install)
        entry = local_llm.engine_plan()
        return Runtime(
            kind=self.kind,
            identity=str(entry.get("version", "")),
            directory=binary.parent,
            executable=binary,
            details={
                "source_commit": str(entry.get("commit", "")),
                "archive_digest": str(entry.get("digest", "")),
            },
        )

    def identity(self, runtime: Runtime) -> EngineIdentity:
        import local_llm

        record = local_llm.capacity_identity(Path(runtime.executable))
        return EngineIdentity(
            kind=self.kind,
            build_info=str(record["build_info"]),
            source_revision=str(record["source_commit"]),
            version=runtime.identity or None,
            build_commit=str(record["build_commit"]),
        )

    # ---------------------------------------------------------------- artifact

    def validate_artifact(self, model: Mapping[str, Any], lock: Mapping[str, Any]) -> Artifact:
        import local_llm

        directory = local_llm.weights_dir(model)
        paths = tuple(directory / entry["path"] for entry in lock["files"])
        for path in paths:
            if not path.is_file():
                raise EngineAdapterError(f"verified weight file is missing: {path}")
        primary = local_llm.primary_weights_path(model)
        return Artifact(
            format="gguf",
            variant=str((model.get("install") or {}).get("variant", "")),
            primary=primary,
            paths=paths,
            # A GGUF is one tensor blob per file, so the lock's sizes are the artifact's bytes
            # exactly. MLX cannot say this, which is why the field is optional.
            tensor_bytes=sum(int(entry["size"]) for entry in lock["files"]),
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
        import local_llm

        sequences = positive_int(engine.get("sequences"), "sequence slots")
        argv = tuple(
            local_llm.engine_args(
                model,
                int(engine["context_tokens"]),
                sequences,
                token,
                dict(engine["allocation"]),
            )
        )
        # The API key is a positional value here, and a supervisor that logs argv or a host that
        # shows process arguments would leak the session credential. The launcher does not log argv,
        # and the index is published so anything that starts reporting it can redact correctly.
        secret_indices = (
            (argv.index("--api-key") + 1,) if "--api-key" in argv else ()
        )
        return Launch(
            argv=argv,
            environment=local_llm.engine_environment(),
            host=local_llm.engine_host(),
            port=int((model.get("install") or {}).get("port") or local_llm.DEFAULT_PORT),
            secret_argument_indices=secret_indices,
        )

    # ---------------------------------------------------------------- status

    def status_path(self) -> str:
        return "/props"

    def normalize_status(self, payload: Mapping[str, Any]) -> EngineStatus:
        document = normalize_engine_document(payload)
        settings = document.get("default_generation_settings")
        if not isinstance(settings, Mapping):
            raise EngineAdapterError("engine properties omit generation settings")
        n_ctx = settings.get("n_ctx")
        return EngineStatus(
            model_alias=str(document.get("model_alias", "")),
            model_path=str(document.get("model_path", "")),
            total_slots=positive_int(document.get("total_slots"), "total_slots"),
            context_tokens=positive_int(n_ctx, "per-slot context"),
            awake=document.get("is_sleeping") is False,
            identity_fragment=str(document.get("build_info", "")),
            observations={
                key: value
                for key, value in document.items()
                if key
                in {
                    "slot_n_ctx",
                    "watchers",
                    "busy_slots",
                    "id_slot",
                    "velocities",
                    "n_ctx",
                }
            },
        )

    def status_request(self, base: str, token: str) -> Any:
        import urllib.request

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
            raise EngineAdapterError("engine is not awake")
        identity = engine.get("identity") or {}
        commit = str(engine.get("build_commit") or identity.get("source_revision") or "")
        if commit and commit not in status.identity_fragment:
            raise EngineAdapterError("engine build differs from the resolved plan")

    def canary(
        self, origin: str, token: str, model: Mapping[str, Any], engine: Mapping[str, Any]
    ) -> Computed:
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

    # ---------------------------------------------------------------- diagnostics

    def diagnostics(self, model: Mapping[str, Any], engine: Mapping[str, Any]) -> Mapping[str, Any]:
        """Offline memory projection. Explicitly requested, never part of a launch.

        llama.cpp is the only engine with an estimator shipped beside the server, and the change
        that removed harness RAM admission kept it as a diagnostic. It stays here rather than in the
        resolver so nothing can mistake a projection for an approval.
        """
        import capacity

        return {"projection_engine": capacity.projection_engine(install=False)[0].name}


def register(target: Registry) -> LlamaCppAdapter:
    adapter = LlamaCppAdapter()
    target.register(adapter)
    return adapter
