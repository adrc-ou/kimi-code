#!/usr/bin/env python3
"""Engine adapters: one narrow interface, one implementation per inference engine.

The harness used to know one engine. ``tools/local_llm.py`` named ``llama-server`` in a constant,
built its command line from its flags, read its ``/props``, and judged readiness by whether that
document matched the plan - and every one of those was the right shape for llama.cpp and the wrong
shape for anything else. Adding a second engine by branching inside that file would have produced a
module where "is this engine ready" meant different things depending on which provider directory
the model sat in, which is the failure the readiness work just removed.

So an adapter answers seven questions and holds no policy:

``provision``        - make the engine runtime exist and return its identity.
``identity``         - what this runtime is, in canonical fields.
``validate_artifact`` - do the verified bytes match what this engine can load.
``launch``           - the command line and the listener this engine needs.
``normalize_status`` - this engine's status document, in canonical fields.
``verify_status``    - does the running engine match its resolved plan.
``canary``           - prove it can compute, not merely answer.

Policy stays in the resolver. An adapter never decides a context window, a slot count, a wiring
target or whether a lane may run: it is told those and reports whether the engine agrees. That
boundary is why ``capacity.py`` can price a request against a plan without knowing whether anything
on the host speaks GGML.

Nothing here imports an inference library. ``mlx_lm`` is loaded by the worker process, in the
provisioned native runtime, on the operator's machine - never by the launcher's interpreter, never
by the proxy image, and never on Linux, where the wheels do not exist.
"""

from __future__ import annotations

from .base import (
    Artifact,
    Computed,
    EngineAdapter,
    EngineIdentity,
    EngineStatus,
    Launch,
    Registry,
    Runtime,
    registry,
)

__all__ = [
    "Artifact",
    "Computed",
    "EngineAdapter",
    "EngineIdentity",
    "EngineStatus",
    "Launch",
    "Registry",
    "Runtime",
    "registry",
]
