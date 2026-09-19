#!/usr/bin/env python3
"""Discover and validate the model and provider definition trees.

Two directories describe everything the harness needs to know before it can serve traffic:

* ``providers/<id>/provider.toml``  - endpoint, named credentials, and policy rules.
* ``models/<id>/model.toml``        - immutable facts about one model, naming a provider.

Neither may appear in ``.env``: model facts change when the served model changes, and policy
numbers change when the provider republishes its terms. Keeping each in one file means one
edit tracks one truth. There is no central registry, exactly as with ``modules/``: a directory
without its manifest is ignored, and an operator adds or removes support by adding or removing
a directory.

Definitions are host-trusted operator configuration, not a sandbox. Symlinks and non-regular
files inside a definition tree are still refused, because these files decide which endpoint
receives an API key.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ID = re.compile(r"[a-z][a-z0-9_]*\Z")
SLUG = re.compile(r"[a-z][a-z0-9]*\Z")
ENV_VAR = re.compile(r"[A-Z][A-Z0-9_]*\Z")
BARE = re.compile(r"[A-Za-z0-9_-]+\Z")

#: Secrets are addressed as ``<provider>__<credential>``, and a model that authenticates with
#: its own key gets ``<provider>__<credential>__<slug>``. Those names are only injective if no
#: component contains the separator, which ``ID`` alone would permit.
NAME_SEPARATOR = "__"


def _no_secret_separator(value: str, where: str) -> str:
    if NAME_SEPARATOR in value:
        raise DefinitionError(
            f"{where} may not contain {NAME_SEPARATOR!r}: credential names are built by "
            "joining these identifiers with it, and such a name could be spelled two ways"
        )
    return value

SCHEMA_VERSION = 1
LANES = ("primary", "long", "subagent")
CREDENTIAL_LANES = ("primary", "subagent")

#: Rule kinds this harness knows how to enforce, with the fields each one requires.
#: Adding a kind here without adding its enforcer in the proxy is a bug: unknown kinds are
#: refused at startup rather than silently not enforced.
RULE_KINDS: dict[str, frozenset[str]] = {
    "output_tokens_per_minute": frozenset({"limit"}),
    "input_tokens_per_minute": frozenset({"limit"}),
    "tokens_per_minute": frozenset({"limit"}),
    "requests_per_minute": frozenset({"limit"}),
    "max_concurrent_requests": frozenset({"limit"}),
    "max_output_tokens_per_request": frozenset({"limit"}),
    "max_context_tokens": frozenset({"limit"}),
    "aggregate_context_fraction": frozenset({"percent"}),
    "exclusive_above_context_fraction": frozenset({"percent"}),
}

#: Which counter family a rule belongs to; the resolver builds one enforcer per family per key.
CONTEXT_RULE_KINDS = frozenset({"aggregate_context_fraction", "exclusive_above_context_fraction"})
RATE_RULE_KINDS = frozenset(
    {
        "output_tokens_per_minute",
        "input_tokens_per_minute",
        "tokens_per_minute",
        "requests_per_minute",
    }
)
COUNT_RULE_KINDS = frozenset({"max_concurrent_requests"})
PER_REQUEST_RULE_KINDS = frozenset({"max_output_tokens_per_request", "max_context_tokens"})

#: Scopes decide which traffic shares a counter. ``model`` keys are the wire model
#: identifier, so two lanes bound to the same model contend for the same permits.
SCOPES = frozenset({"model", "provider", "credential", "credential_model"})

#: Scopes a rule of each family may meaningfully use. A provider-scoped context fraction has
#: no single model context to measure, so it is refused instead of guessed at.
ALLOWED_SCOPES: dict[frozenset[str], frozenset[str]] = {
    CONTEXT_RULE_KINDS: frozenset({"model"}),
    RATE_RULE_KINDS: frozenset({"model", "credential", "credential_model", "provider"}),
    COUNT_RULE_KINDS: frozenset({"model", "credential", "credential_model", "provider"}),
    PER_REQUEST_RULE_KINDS: frozenset({"model", "provider"}),
}

#: What one request costs against each rate kind. The proxy books these units, so a rule
#: about requests cannot be metered in tokens; a kind missing here would be enforced in the
#: wrong unit, which is why the mapping is total over ``RATE_RULE_KINDS``.
RATE_RULE_UNITS: dict[str, str] = {
    "output_tokens_per_minute": "output_tokens",
    "input_tokens_per_minute": "input_tokens",
    "tokens_per_minute": "total_tokens",
    "requests_per_minute": "requests",
}


class DefinitionError(ValueError):
    """A definition is malformed, references a missing provider, or is unsafe to read."""


def _load_toml(path: Path, kind: str) -> dict[str, Any]:
    if path.is_symlink():
        raise DefinitionError(f"{kind} manifest must not be a symlink: {path}")
    try:
        with path.open("rb") as source:
            document = tomllib.load(source)
    except OSError as exc:
        raise DefinitionError(f"cannot read {kind} manifest {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"{kind} manifest {path} is not valid TOML: {exc}") from exc
    if not isinstance(document, dict):
        raise DefinitionError(f"{kind} manifest {path} must be a TOML table")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise DefinitionError(f"{kind} manifest {path} must set schema_version = {SCHEMA_VERSION}")
    return document


def _positive_int(table: dict[str, Any], key: str, where: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DefinitionError(f"{where}.{key} must be a positive integer")
    return int(value)


def _percent(table: dict[str, Any], key: str, where: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise DefinitionError(f"{where}.{key} must be an integer percentage between 1 and 100")
    return int(value)


def _text(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise DefinitionError(f"{where}.{key} must be a single-line non-empty string")
    return value


def _walk_safe(directory: Path, kind: str) -> None:
    for child in directory.rglob("*"):
        if child.is_symlink() or not (child.is_file() or child.is_dir()):
            raise DefinitionError(f"Unsafe {kind} definition entry: {child}")


def discover_providers(root: Path) -> dict[str, dict[str, Any]]:
    """Return every valid provider definition keyed by identifier."""
    directory = root / "providers"
    result: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return result
    if directory.is_symlink():
        raise DefinitionError("providers/ must not be a symlink")
    for path in sorted(p for p in directory.iterdir() if p.is_dir()):
        if not (path / "provider.toml").is_file():
            continue
        provider_id = path.name
        if path.is_symlink() or not ID.fullmatch(provider_id):
            raise DefinitionError(
                f"Provider directories must be real directories with lowercase identifiers: {path}"
            )
        _no_secret_separator(provider_id, f"providers/{provider_id}")
        _walk_safe(path, "provider")
        result[provider_id] = _parse_provider(provider_id, path)
    return result


def _parse_provider(provider_id: str, path: Path) -> dict[str, Any]:
    where = f"providers/{provider_id}"
    document = _load_toml(path / "provider.toml", "provider")
    label = _text(document, "label", where)
    for key in document:
        if key not in {
            "schema_version",
            "label",
            "policy_url",
            "endpoint",
            "credential",
            "rule",
            "safety",
        }:
            raise DefinitionError(f"{where} has unknown key {key!r}")

    policy_url = document.get("policy_url", "")
    if policy_url and not re.fullmatch(r"https://[^\s]+", str(policy_url)):
        raise DefinitionError(f"{where}.policy_url must be an https URL")

    endpoint = document.get("endpoint")
    if not isinstance(endpoint, dict):
        raise DefinitionError(f"{where} must declare an [endpoint] table")
    base_url = _text(endpoint, "base_url", f"{where}.endpoint").rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+", base_url):
        raise DefinitionError(f"{where}.endpoint.base_url must be an http(s) origin without a path")
    base_url_env = endpoint.get("base_url_env", "")
    if base_url_env and not ENV_VAR.fullmatch(str(base_url_env)):
        raise DefinitionError(f"{where}.endpoint.base_url_env must be an UPPER_SNAKE variable name")
    protocol = _text(endpoint, "protocol", f"{where}.endpoint")
    if protocol not in {"openai", "anthropic"}:
        raise DefinitionError(f"{where}.endpoint.protocol must be openai or anthropic")
    for key in endpoint:
        if key not in {"base_url", "base_url_env", "protocol"}:
            raise DefinitionError(f"{where}.endpoint has unknown key {key!r}")

    credentials = _parse_credentials(document.get("credential"), where, provider_id)
    rules = _parse_rules(document.get("rule"), where)

    safety = document.get("safety", {})
    if not isinstance(safety, dict):
        raise DefinitionError(f"{where}.safety must be a table")
    for key in safety:
        if key not in {"context_margin_percent", "output_rate_margin_percent"}:
            raise DefinitionError(f"{where}.safety has unknown key {key!r}")
    context_margin = (
        _percent(safety, "context_margin_percent", f"{where}.safety")
        if "context_margin_percent" in safety
        else 100
    )
    rate_margin = (
        _percent(safety, "output_rate_margin_percent", f"{where}.safety")
        if "output_rate_margin_percent" in safety
        else 100
    )

    return {
        "id": provider_id,
        "path": path,
        "label": label,
        "policy_url": str(policy_url or ""),
        "base_url": base_url,
        "base_url_env": str(base_url_env or ""),
        "protocol": protocol,
        "credentials": credentials,
        "rules": rules,
        "context_margin_percent": context_margin,
        "output_rate_margin_percent": rate_margin,
    }


def _parse_credentials(raw: Any, where: str, provider_id: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise DefinitionError(f"{where} must declare at least one [[credential]] table")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        here = f"{where}.credential[{index}]"
        if not isinstance(item, dict):
            raise DefinitionError(f"{here} must be a table")
        credential_id = _text(item, "id", here)
        if not ID.fullmatch(credential_id):
            raise DefinitionError(f"{here}.id must be a lowercase identifier")
        _no_secret_separator(credential_id, f"{here}.id")
        if credential_id in result:
            raise DefinitionError(f"{where} declares credential {credential_id!r} twice")
        for key in item:
            if key not in {"id", "label", "prompt", "env", "key_url"}:
                raise DefinitionError(f"{here} has unknown key {key!r}")
        env = item.get("env", "")
        if env and not ENV_VAR.fullmatch(str(env)):
            raise DefinitionError(f"{here}.env must be an UPPER_SNAKE variable name")
        key_url = item.get("key_url", "")
        if key_url and not re.fullmatch(r"https://[^\s]+", str(key_url)):
            raise DefinitionError(f"{here}.key_url must be an https URL")
        prompt = item.get("prompt")
        if prompt is None:
            prompt = item["label"]
        result[credential_id] = {
            "id": credential_id,
            "label": _text(item, "label", here),
            "prompt": _text({"prompt": prompt}, "prompt", here),
            "env": str(env or ""),
            # Where an operator obtains this credential. Provider-specific facts such as a
            # key-issuing dashboard belong in the definition, not in the README.
            "key_url": str(key_url or ""),
            # A credential is addressed inside the container by a name that cannot collide
            # across providers, because both halves exclude the double underscore.
            "secret_name": f"{provider_id}__{credential_id}",
        }
    return result


def _parse_rules(raw: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise DefinitionError(f"{where} must declare at least one [[rule]] table")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        here = f"{where}.rule[{index}]"
        if not isinstance(item, dict):
            raise DefinitionError(f"{here} must be a table")
        kind = _text(item, "kind", here)
        if kind not in RULE_KINDS:
            raise DefinitionError(
                f"{here}.kind {kind!r} is not a rule this harness can enforce; "
                f"known kinds: {', '.join(sorted(RULE_KINDS))}"
            )
        scope = _text(item, "scope", here)
        if scope not in SCOPES:
            raise DefinitionError(
                f"{here}.scope {scope!r} is unknown; known scopes: {', '.join(sorted(SCOPES))}"
            )
        allowed = next(
            (
                permitted
                for family, permitted in ALLOWED_SCOPES.items()
                if kind in family
            ),
            frozenset(),
        )
        if scope not in allowed:
            raise DefinitionError(
                f"{here}.kind {kind!r} cannot use scope {scope!r}; "
                f"use one of {', '.join(sorted(allowed))}"
            )
        fields = RULE_KINDS[kind]
        if not fields & set(item):
            raise DefinitionError(f"{here} must set {' or '.join(sorted(fields))}")
        unexpected = set(item) - {"kind", "scope", "models", *fields}
        if unexpected:
            raise DefinitionError(f"{here} has unknown key(s): {', '.join(sorted(unexpected))}")
        value = (
            _percent(item, next(iter(fields)), here)
            if "percent" in fields
            else _positive_int(item, "limit", here)
        )
        applies_to = item.get("models")
        if applies_to is not None and (
            not isinstance(applies_to, list)
            or not applies_to
            or not all(isinstance(name, str) and name for name in applies_to)
        ):
            raise DefinitionError(f"{here}.models must be a non-empty list of wire model names")
        result.append(
            {
                "kind": kind,
                "scope": scope,
                "value": int(value),
                "models": list(applies_to) if applies_to else None,
            }
        )
    return result


def discover_models(root: Path, providers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every valid model definition, dropping models whose provider is unavailable.

    Dropping (rather than failing) is deliberate: an operator may keep a model directory in
    place while its provider definition is absent, and ./start.sh must simply not offer it.
    """
    directory = root / "models"
    result: list[dict[str, Any]] = []
    if not directory.is_dir():
        return result
    if directory.is_symlink():
        raise DefinitionError("models/ must not be a symlink")
    for path in sorted(p for p in directory.iterdir() if p.is_dir()):
        if not (path / "model.toml").is_file():
            continue
        model_id = path.name
        if path.is_symlink() or not ID.fullmatch(model_id):
            raise DefinitionError(
                f"Model directories must be real directories with lowercase identifiers: {path}"
            )
        _walk_safe(path, "model")
        parsed = _parse_model(model_id, path)
        provider = providers.get(parsed["provider"])
        if provider is None:
            continue
        if parsed["key_env"] and parsed["key_env"] in {
            credential["env"] for credential in provider["credentials"].values()
        }:
            raise DefinitionError(
                f"models/{model_id}.key_env names a variable that a [[credential]] of "
                f"providers/{parsed['provider']} already names; a model-scoped key must be its "
                "own variable, or the model should simply name that credential"
            )
        result.append(parsed)

    seen_slugs: dict[str, str] = {}
    for model in result:
        owner = seen_slugs.setdefault(model["slug"], model["id"])
        if owner != model["id"]:
            raise DefinitionError(
                f"Models {owner} and {model['id']} share slug {model['slug']!r}, "
                "which would generate identical Kimi aliases"
            )
    return result


def _parse_model(model_id: str, path: Path) -> dict[str, Any]:
    where = f"models/{model_id}"
    document = _load_toml(path / "model.toml", "model")
    known = {
        "schema_version",
        "label",
        "provider",
        "model",
        "slug",
        "credential",
        "key_env",
        "context",
        "lane",
        "capabilities",
        "support_efforts",
        "default_effort",
    }
    for key in document:
        if key not in known:
            raise DefinitionError(f"{where} has unknown key {key!r}")

    label = _text(document, "label", where)
    provider = _text(document, "provider", where)
    if not ID.fullmatch(provider):
        raise DefinitionError(f"{where}.provider must be a lowercase provider identifier")
    model_name = _text(document, "model", where)
    slug = _text(document, "slug", where)
    if not SLUG.fullmatch(slug):
        raise DefinitionError(f"{where}.slug must match [a-z][a-z0-9]* (it forms Kimi aliases)")
    credential = _text(document, "credential", where)
    if not ID.fullmatch(credential):
        raise DefinitionError(f"{where}.credential must be a lowercase credential identifier")
    # Optional: the .env variable holding a key for this model alone. Absent, or present but
    # blank in .env, the model authenticates with the provider-scoped key its `credential`
    # names. Naming a key this way gives the model its own upstream identity, which is what
    # credential-scoped provider rules are then counted against.
    key_env = document.get("key_env", "")
    if key_env and not ENV_VAR.fullmatch(str(key_env)):
        raise DefinitionError(f"{where}.key_env must be an UPPER_SNAKE variable name")

    context = document.get("context", {})
    if not isinstance(context, dict):
        raise DefinitionError(f"{where}.context must be a table")
    for key in context:
        if key != "advertised_tokens":
            raise DefinitionError(f"{where}.context has unknown key {key!r}")
    advertised = (
        _positive_int(context, "advertised_tokens", f"{where}.context")
        if "advertised_tokens" in context
        else None
    )

    lanes = _parse_lanes(document.get("lane"), where)
    largest_lane = max(lane["context_tokens"] for lane in lanes.values())
    if advertised is not None and advertised < largest_lane:
        raise DefinitionError(
            f"{where}.context.advertised_tokens is smaller than a declared lane window"
        )
    if advertised is None:
        advertised = max(lane["context_tokens"] for lane in lanes.values())

    return {
        "id": model_id,
        "path": path,
        "label": label,
        "provider": provider,
        "model": model_name,
        "slug": slug,
        "credential": credential,
        "key_env": str(key_env or ""),
        "advertised_tokens": advertised,
        "lanes": lanes,
        "capabilities": _string_list(document, "capabilities", where),
        "support_efforts": _string_list(document, "support_efforts", where),
        "default_effort": document.get("default_effort", ""),
    }


def _string_list(document: dict[str, Any], key: str, where: str) -> list[str]:
    """Read an optional list of single-line strings; absence is an empty list, not an error.

    Both capabilities and effort levels may legitimately be undeclared, so a missing key has one
    answer here. A required list would have to be spelled by a caller that does not exist.
    """
    value = document.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and not any(ord(c) < 32 for c in item) for item in value
    ):
        raise DefinitionError(f"{where}.{key} must be a list of single-line strings")
    return list(value)


def _parse_lanes(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise DefinitionError(f"{where} must declare at least one [lane.*] table")
    lanes: dict[str, dict[str, Any]] = {}
    for name, table in raw.items():
        here = f"{where}.lane.{name}"
        if name not in LANES:
            raise DefinitionError(
                f"{where} declares lane {name!r}; the harness serves {', '.join(LANES)}"
            )
        if not isinstance(table, dict):
            raise DefinitionError(f"{here} must be a table")
        for key in table:
            if key not in {
                "context_tokens",
                "input_tokens",
                "output_clamp_tokens",
                "default_effort",
            }:
                raise DefinitionError(f"{here} has unknown key {key!r}")
        context_tokens = _positive_int(table, "context_tokens", here)
        input_tokens = _positive_int(table, "input_tokens", here)
        clamp = _positive_int(table, "output_clamp_tokens", here)
        effort = table.get("default_effort", "")
        if effort and (not isinstance(effort, str) or not BARE.fullmatch(effort)):
            raise DefinitionError(f"{here}.default_effort must be a bare lowercase word")
        lanes[name] = {
            "lane": name,
            "context_tokens": context_tokens,
            "input_tokens": input_tokens,
            "output_clamp_tokens": clamp,
            "default_effort": str(effort or ""),
        }
    for required in CREDENTIAL_LANES:
        if required not in lanes:
            raise DefinitionError(
                f"{where} must declare [lane.{required}] to be selectable for that role"
            )
    return lanes


def load_definitions(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Discover both trees, refusing a tree that is present but unusable."""
    providers = discover_providers(root)
    models = discover_models(root, providers)
    return providers, models
