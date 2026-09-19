#!/usr/bin/env python3
"""Fixtures shared by the harness suite.

Kept deliberately small: only things that were genuinely duplicated, and only in the form the
production code expects. A helper here must never invent a value the shipped definitions already
supply, because a fixture that diverges from ``./models`` and ``./providers`` tests the fixture.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

#: Audiences as :mod:`prompt_measure` names them, mapped to the lane each one normally runs on.
#: A measured row has to be priced against the cap of the lane that produced it, so a test that
#: names an audience should not also have to know that lane's alias.
AUDIENCE_LANE = {"main": "primary", "subagent": "subagent"}


def load_script(name: str, relative: Path) -> ModuleType:
    """Import a file the way the launcher runs it: by path, as a standalone script.

    ``tools/`` and ``container/`` hold scripts with dashes and with no package, so
    ``import`` cannot reach them and every suite was re-implementing the loader.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the module has to be
    # registered before it is executed.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def add_tools_to_path() -> None:
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def reserved_context_size() -> int:
    """The live reservation from the rendered Kimi configuration, never a copy of it."""
    with (ROOT / "runtime" / "config.toml").open("rb") as source:
        return tomllib.load(source)["loop_control"]["reserved_context_size"]


def shipped_plan() -> dict:
    """Resolve the checked-in definitions exactly as ``./start.sh`` does.

    Seven suites needed this and each had its own copy, which meant a change to the resolution
    contract had seven places to be wrong in. Providers are shallow-copied because the launcher
    layers ``.env`` overrides onto them and a test must not read operator state - nor write into
    a dictionary :func:`definitions.load_definitions` may hand out again.

    The selection is the first defined model for both lanes, which is the one configuration this
    repository ships with full definitions for and the case every policy number is stated against.
    """
    add_tools_to_path()
    import definitions
    import policy

    providers, models = definitions.load_definitions(ROOT)
    return policy.resolve(
        {pid: dict(provider) for pid, provider in providers.items()},
        {model["id"]: model for model in models},
        {"primary": models[0]["id"], "subagent": models[0]["id"]},
        reserved_context_size=reserved_context_size(),
    )


def lane_alias(plan: dict, lane: str) -> str:
    """A lane's alias, or the lane a normally-run audience sits on.

    Derived rather than typed so that renaming a model moves every expectation with it. A test
    that hard-codes ``"qwen3-primary"`` keeps passing after a rename by silently falling back to
    the lane default, which is exactly the assertion it was meant to be testing against.
    """
    return str(plan["lanes"][AUDIENCE_LANE.get(lane, lane)]["alias"])


def measured_record(
    plan: dict,
    audience: str,
    tokens: int,
    framing: int,
    harness: int,
    project: int,
    *,
    lane: str | None = None,
) -> dict:
    """A row shaped like :func:`prompt_measure.size_of` output.

    ``lane`` overrides the audience's usual lane; pass it to price a main-audience row against
    the long lane, which is the one case where the two legitimately differ. The timestamp is
    fixed, which keeps the *input* stable; an age is still measured against a moving clock, so a
    test that asserts age text has to pass its own ``now`` down.
    """
    return {
        "audience": audience,
        "tokens": tokens,
        "kimiFraming": framing,
        "harnessContract": harness,
        "projectInstructions": project,
        "modelAlias": lane_alias(plan, lane or AUDIENCE_LANE[audience]),
        "profileName": "agent" if audience == "main" else "explore",
        "time": 1_700_000_000_000,
    }


