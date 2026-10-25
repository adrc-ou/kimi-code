#!/usr/bin/env python3
"""Discover and validate the model and provider definition trees.

Two directories describe everything the harness needs to know before it can serve traffic:

* ``providers/<id>/provider.toml``         - endpoint, named credentials, host requirement, rules.
* ``models/<provider>/<id>/model.toml``  - one model, as that provider serves it.

Neither may appear in ``.env``: model facts change when the served model changes, and policy
numbers change when the provider republishes its terms. Keeping each in one file means one
edit tracks one truth. There is no central registry, exactly as with ``modules/``: a directory
without its manifest is ignored, and an operator adds or removes support by adding or removing
a directory.

Model definitions sit under the provider that serves them because that pair, not the model
alone, is what the rest of the harness already treats as one thing: policy keys every counter
``<provider>/<wire model>``, every secret is ``<provider>__<credential>``, and one model name can
legitimately be served two ways - a 1M-token window from a cloud gateway and a 262K-token window
from a server on the operator's own machine are two definitions, not two values in one file. The
directory name is also what makes removal atomic: deleting ``models/<provider>/`` retires every
model that provider served, which is the same rule the picker's eligibility filter relies on.

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
MANIFEST_SCHEMA_VERSIONS = frozenset({1, 2})
LANES = ("primary", "long", "subagent")
CREDENTIAL_LANES = ("primary", "subagent")

#: What a provider may ask of the machine the harness is launching on. A provider names none of
#: these and is then served from anywhere; ``local_mps`` names ``metal`` and appears only on the
#: hosts that have it. Unknown names are refused at load rather than read as "never", because a
#: typo in a capability list silences a whole provider and nothing else would say so.
HOST_CAPABILITIES = frozenset({"metal", "cuda"})

#: Subdirectory of a model definition that holds downloaded weights, and the tracked manifest
#: that says what those weights must be. The subtree is data rather than definition: it is
#: skipped by the safety walk (a downloaded tree can hold anything its publisher chose to put in
#: it, and that must not be able to fail a launch), ignored by git, and written only by
#: ``tools/local_llm.py``.
WEIGHTS_DIR = "weights"
WEIGHTS_LOCK = "weights.lock.json"

#: Host floors a model may declare for itself. Both are GiB measured against the *host*, since a
#: local server runs there and inside no container limit. Zero means no floor.
MODEL_REQUIRE_KEYS = frozenset({"memory_gib", "disk_gib"})
INSTALL_KEYS = frozenset({"variant", "lock", "sampling", "port"})
#: Sampling defaults the harness hands to a local engine. They are model facts - the vendor
#: publishes different ones for thinking and non-thinking modes - and never provider policy.
SAMPLING_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
    }
)

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

#: Rule kinds that may be declared with ``default = true``, and the scopes that may carry one.
#: A default is a fallback ceiling for models the provider has not listed individually, so it
#: belongs at a scope looser than ``model``: a model-scoped rule is already the most specific
#: statement its kind can make, and marking it default would only obscure that.
DEFAULTABLE_RULE_KINDS = COUNT_RULE_KINDS
DEFAULTABLE_RULE_SCOPES = SCOPES - {"model"}

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
    version = document.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in MANIFEST_SCHEMA_VERSIONS
    ):
        raise DefinitionError(f"{kind} manifest {path} must set schema_version = 1 or 2")
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


def _flag(table: dict[str, Any], key: str, where: str, default: bool = False) -> bool:
    """Read an optional boolean, absent meaning ``default``. Only a real boolean will do: a rule
    table that accepted ``"true"`` would make a quoting mistake read as a deliberate choice."""
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise DefinitionError(f"{where}.{key} must be a boolean")
    return value


def _optional_int(table: dict[str, Any], key: str, where: str) -> int:
    """Read an optional non-negative integer, absent meaning zero."""
    value = table.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DefinitionError(f"{where}.{key} must be a non-negative integer")
    return int(value)


def _capability_list(document: dict[str, Any], key: str, where: str) -> list[str]:
    """Read ``requires = [...]`` on a provider: what the host must offer for it to appear."""
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DefinitionError(f"{where}.{key} must be a list of host capability names")
    unknown = sorted(set(value) - HOST_CAPABILITIES)
    if unknown:
        raise DefinitionError(
            f"{where}.{key} names {', '.join(unknown)}; this harness can test a host for "
            f"{', '.join(sorted(HOST_CAPABILITIES))}"
        )
    return sorted(set(value))


def _text(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise DefinitionError(f"{where}.{key} must be a single-line non-empty string")
    return value


def _walk_safe(directory: Path, kind: str, skip: frozenset[str] = frozenset()) -> None:
    """Refuse anything in a definition tree that is not a plain file or directory.

    ``skip`` names top-level subdirectories exempt from the walk. Downloaded model weights are
    the only such subtree: their contents belong to whoever published the checkpoint, and a
    symlink inside a 100 GB weight set must not be able to fail a launch. It is not a licence to
    *read* anything from there as configuration - only :mod:`local_llm` opens that tree, and it
    digests every file it finds there.
    """
    for child in directory.rglob("*"):
        if skip and child.relative_to(directory).parts[0] in skip:
            continue
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
    version = document["schema_version"]
    known = {
        "schema_version",
        "label",
        "policy_url",
        "requires",
        "endpoint",
        "credential",
        "rule",
        "safety",
    }
    if version == 2:
        known |= {"execution", "capacity"}
    for key in document:
        if key not in known:
            raise DefinitionError(f"{where} has unknown key {key!r}")

    requires = _capability_list(document, "requires", where)

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
    # Whether this upstream tolerates an unknown field in the request body. The harness adds a
    # private cache salt to every request so that a shared gateway cannot key its prompt cache on
    # one operator's content; an engine written by nobody but the operator has no such cache to
    # protect and no obligation to ignore a key it does not model.
    sends_cache_salt = _flag(endpoint, "sends_cache_salt", f"{where}.endpoint", default=True)
    for key in endpoint:
        if key not in {"base_url", "base_url_env", "protocol", "sends_cache_salt"}:
            raise DefinitionError(f"{where}.endpoint has unknown key {key!r}")

    credentials = _parse_credentials(document.get("credential"), where, provider_id)
    execution = _parse_execution(document.get("execution"), where) if version == 2 else None
    capacity = _parse_capacity(document.get("capacity"), where) if version == 2 else None
    if execution is not None and not requires:
        raise DefinitionError(f"{where}.execution needs a measured host capability in requires")
    if execution is not None and protocol != "openai":
        raise DefinitionError(f"{where}.execution requires the engine's openai protocol")
    rules = _parse_rules(document.get("rule"), where, allow_empty=version == 2)
    if execution is not None:
        unsupported = sorted({rule["kind"] for rule in rules} & PER_REQUEST_RULE_KINDS)
        if unsupported:
            raise DefinitionError(
                f"{where}: engine-derived local envelopes do not support per-request rules "
                f"{', '.join(unsupported)}; context and generation capacity come from the engine"
            )

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
        "schema_version": version,
        "id": provider_id,
        "path": path,
        "label": label,
        "policy_url": str(policy_url or ""),
        "requires": requires,
        "base_url": base_url,
        "base_url_env": str(base_url_env or ""),
        "protocol": protocol,
        "sends_cache_salt": sends_cache_salt,
        "credentials": credentials,
        "rules": rules,
        "execution": execution,
        "capacity": capacity,
        "context_margin_percent": context_margin,
        "output_rate_margin_percent": rate_margin,
    }


def _parse_execution(raw: Any, where: str) -> dict[str, Any]:
    here = f"{where}.execution"
    if not isinstance(raw, dict):
        raise DefinitionError(f"{here} must declare local execution and its engine profile")
    if set(raw) - {"kind", "engine", "profile"}:
        raise DefinitionError(f"{here} has unknown key(s)")
    if raw.get("kind") != "local" or raw.get("engine") != "llama_cpp":
        raise DefinitionError(f"{here} must select kind = local and engine = llama_cpp")
    profile = raw.get("profile")
    if not isinstance(profile, dict):
        raise DefinitionError(f"{here}.profile must declare the engine allocation settings")
    integer_keys = {"context_granularity", "max_sequences", "batch_tokens", "microbatch_tokens"}
    choices = {
        "cache_type_k": {"f16"},
        "cache_type_v": {"f16"},
        "flash_attention": {"auto"},
        "load_mode": {"auto"},
        "lazy_mode": {"auto"},
        "gpu_layers": {"all"},
    }
    keys = integer_keys | set(choices) | {"kv_unified", "cache_ram_mib", "context_checkpoints"}
    if set(profile) != keys:
        raise DefinitionError(f"{here}.profile must declare exactly {', '.join(sorted(keys))}")
    result = {key: _positive_int(profile, key, f"{here}.profile") for key in integer_keys}
    if result["microbatch_tokens"] > result["batch_tokens"]:
        raise DefinitionError(f"{here}.profile microbatch_tokens exceeds batch_tokens")
    if result["max_sequences"] > result["microbatch_tokens"]:
        raise DefinitionError(f"{here}.profile max_sequences exceeds microbatch_tokens")
    for key, allowed in choices.items():
        if not isinstance(profile[key], str) or profile[key] not in allowed:
            raise DefinitionError(
                f"{here}.profile.{key} must be one of {', '.join(sorted(allowed))}"
            )
        result[key] = profile[key]
    if profile["kv_unified"] is not False:
        raise DefinitionError(f"{here}.profile requires independent full sequence slots")
    for key in ("cache_ram_mib", "context_checkpoints"):
        if isinstance(profile[key], bool) or not isinstance(profile[key], int) or profile[key] != 0:
            raise DefinitionError(f"{here}.profile.{key} must be zero for accounted full slots")
        result[key] = 0
    result["kv_unified"] = False
    return {"kind": "local", "engine": "llama_cpp", "profile": result}


def _parse_capacity(raw: Any, where: str) -> dict[str, Any]:
    here = f"{where}.capacity"
    if not isinstance(raw, dict):
        raise DefinitionError(f"{here} must declare the host allocation policy")
    known = {
        "strategy",
        "sequence_slots",
        "validation_status",
        "validation_evidence",
    }
    if set(raw) - known:
        raise DefinitionError(f"{here} has unknown key(s)")
    if raw.get("strategy") != "native_context":
        raise DefinitionError(f"{here}.strategy must be native_context")
    result = {
        "strategy": "native_context",
        "sequence_slots": _positive_int(raw, "sequence_slots", here),
    }
    status = raw.get("validation_status", "pending_native_validation")
    evidence = _string_list(raw, "validation_evidence", here)
    if not isinstance(status, str) or status not in {"pending_native_validation", "validated"}:
        raise DefinitionError(f"{here}.validation_status is unknown")
    if status == "validated" and not evidence:
        raise DefinitionError(f"{here} cannot claim validated without validation_evidence")
    return {**result, "validation_status": status, "validation_evidence": evidence}


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
            if key not in {"id", "label", "prompt", "env", "key_url", "generate"}:
                raise DefinitionError(f"{here} has unknown key {key!r}")
        env = item.get("env", "")
        if env and not ENV_VAR.fullmatch(str(env)):
            raise DefinitionError(f"{here}.env must be an UPPER_SNAKE variable name")
        key_url = item.get("key_url", "")
        if key_url and not re.fullmatch(r"https://[^\s]+", str(key_url)):
            raise DefinitionError(f"{here}.key_url must be an https URL")
        # A credential nobody issues. A server on the operator's own machine has no dashboard to
        # copy a key from, so the launcher mints one per session and hands the same value to the
        # engine it starts and to the proxy that calls it. `env` stays meaningful beside it as the
        # operator's own override.
        generate = _flag(item, "generate", here)
        if generate and not env:
            raise DefinitionError(
                f"{here} generates its own value but names no env variable: the operator needs one "
                "name to pin a value in .env, or nothing could override the generated token"
            )
        if generate and key_url:
            raise DefinitionError(
                f"{here} both generates its own value and names a key_url; one of those is a lie"
            )
        prompt = item.get("prompt")
        if prompt is None:
            prompt = item["label"]
        result[credential_id] = {
            "id": credential_id,
            "label": _text(item, "label", here),
            "prompt": _text({"prompt": prompt}, "prompt", here),
            "env": str(env or ""),
            "generate": generate,
            # Where an operator obtains this credential. Provider-specific facts such as a
            # key-issuing dashboard belong in the definition, not in the README.
            "key_url": str(key_url or ""),
            # A credential is addressed inside the container by a name that cannot collide
            # across providers, because both halves exclude the double underscore.
            "secret_name": f"{provider_id}__{credential_id}",
        }
    return result


def _parse_rules(raw: Any, where: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    """Read one provider's ``[[rule]]`` tables into enforceable rules.

    ``default = true`` marks a count rule as a fallback ceiling for models the provider has not
    listed by name. Such a rule is resolved by *displacing*: it applies only where no
    model-scoped rule of the same kind does, and it never adds its own counter beside one. That
    is what lets a provider carry its tightest published row as a floor for unknown models
    without capping the models it explicitly allowed more of.
    """
    if not isinstance(raw, list) or (not raw and not allow_empty):
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
            (permitted for family, permitted in ALLOWED_SCOPES.items() if kind in family),
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
        unexpected = set(item) - {"kind", "scope", "models", "default", *fields}
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
        default = _flag(item, "default", here)
        if default and kind not in DEFAULTABLE_RULE_KINDS:
            raise DefinitionError(
                f"{here}.default is meaningful only for {', '.join(sorted(DEFAULTABLE_RULE_KINDS))}"
            )
        if default and scope not in DEFAULTABLE_RULE_SCOPES:
            raise DefinitionError(
                f"{here}.default needs one of {', '.join(sorted(DEFAULTABLE_RULE_SCOPES))}; "
                "a model-scoped rule is already specific to its model and cannot be a fallback"
            )
        result.append(
            {
                "kind": kind,
                "scope": scope,
                "value": int(value),
                "models": list(applies_to) if applies_to else None,
                "default": default,
            }
        )
    return result


def discover_models(root: Path, providers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every valid model definition, dropping models whose provider is unavailable.

    Dropping (rather than failing) is deliberate: an operator may keep a model directory in
    place while its provider definition is absent, and ./start.sh must simply not offer it. The
    same rule is what makes provider-level gating free - a provider the host cannot serve is
    removed from ``providers`` by the caller, and every model under it goes with it.
    """
    directory = root / "models"
    result: list[dict[str, Any]] = []
    if not directory.is_dir():
        return result
    if directory.is_symlink():
        raise DefinitionError("models/ must not be a symlink")
    for child in sorted(directory.iterdir()):
        if (child / "model.toml").is_file():
            raise DefinitionError(
                f"{child / 'model.toml'} sits one level too shallow. Model definitions live at "
                "models/<provider>/<model>/model.toml, because one model name can be served by "
                f"more than one provider; move {child.name} under a directory named for its "
                "provider id."
            )
    for provider_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        if provider_dir.is_symlink() or not ID.fullmatch(provider_dir.name):
            raise DefinitionError(
                "models/ holds one directory per provider, each a real directory with a "
                f"lowercase identifier: {provider_dir}"
            )
        for path in sorted(p for p in provider_dir.iterdir() if p.is_dir()):
            if not (path / "model.toml").is_file():
                continue
            model_id = path.name
            if path.is_symlink() or not ID.fullmatch(model_id):
                raise DefinitionError(
                    f"Model directories must be real directories with lowercase identifiers: {path}"
                )
            _walk_safe(path, "model", frozenset({WEIGHTS_DIR}))
            parsed = _parse_model(provider_dir.name, model_id, path)
            provider = providers.get(parsed["provider"])
            if provider is None:
                continue
            # The label travels with the model because every human-facing surface that names a
            # model has to be able to name *which serving of it* - and the picker's Provider
            # column is that surface. Readers that build model tables by hand may omit it, and
            # fall back to the id.
            parsed["provider_label"] = provider["label"]
            parsed["provider_requires"] = list(provider["requires"])
            _require_local_serving(parsed, provider)
            if parsed["key_env"] and parsed["key_env"] in {
                credential["env"] for credential in provider["credentials"].values()
            }:
                raise DefinitionError(
                    f"{parsed['where']}.key_env names a variable that a [[credential]] of "
                    f"providers/{parsed['provider']} already names; a model-scoped key must be its "
                    "own variable, or the model should simply name that credential"
                )
            result.append(parsed)

    # One engine process per model, so one port per model: two definitions sharing a listener
    # produce an engine that fails to bind and a lane that answers nothing, which is a definition
    # error rather than a runtime one and belongs here.
    seen_ports: dict[tuple[str, int], str] = {}
    for model in result:
        install = model.get("install") or {}
        port = int(install.get("port") or 0)
        if not port:
            continue
        key = (model["provider"], port)
        owner = seen_ports.setdefault(key, model["where"])
        if owner != model["where"]:
            raise DefinitionError(
                f"{owner} and {model['where']} both serve on port {port}; one engine per port "
                "means one model per port"
            )

    seen_slugs: dict[str, str] = {}
    for model in result:
        owner = seen_slugs.setdefault(model["slug"], model["id"])
        if owner != model["id"]:
            raise DefinitionError(
                f"{owner} and {model['where']} share slug {model['slug']!r}, "
                "which would generate identical Kimi aliases"
            )
    return result


def _require_local_serving(model: dict[str, Any], provider: dict[str, Any]) -> None:
    """Keep "served on this host" a fact with exactly one spelling.

    A provider that asks something of the host runs its models there, and a model can only be run
    there if the harness can install its weights: so ``[install]`` is required of it, and it is
    the one thing every later reader tests - the picker's ``Ready?`` column, the resolver's
    per-model port, :mod:`local_llm`. A provider that asks nothing of the host serves remotely,
    where a host floor or a download would be a gate no code evaluates and a step nobody performs;
    refusing them is what makes "Ready" a fact for a cloud model rather than merely a claim nobody
    wrote down.
    """
    derived = model.get("schema_version") == 2
    if derived != bool(provider.get("execution")):
        raise DefinitionError(
            f"{model['where']} and providers/{provider['id']} must agree on engine-derived "
            "local capacity (schema version 2)"
        )
    if derived:
        if (
            provider["capacity"]["sequence_slots"]
            > provider["execution"]["profile"]["max_sequences"]
        ):
            raise DefinitionError(
                f"providers/{provider['id']}: sequence_slots exceeds max_sequences"
            )
    declared = (
        ("[requires]", any(model["requires"].values())),
        ("[install]", model["install"] is not None),
    )
    named = " and ".join(name for name, present in declared if present)
    if not provider["requires"]:
        if named:
            raise DefinitionError(
                f"{model['where']} declares {named}, but providers/{provider['id']} asks nothing "
                "of the host and so serves this model remotely. A host floor and a weight "
                "download belong on a model whose provider names one in requires."
            )
        return
    if model["install"] is None:
        raise DefinitionError(
            f"{model['where']} is served by providers/{provider['id']}, which runs on this "
            "machine, but declares no [install] block: nothing pins the weights to download, the "
            "port to serve them on, or what Ready? may claim"
        )


def _parse_requires(raw: Any, where: str) -> dict[str, int]:
    """``[requires]``: the host floors under which this model stops being offered.

    Absent means no floor, which is the honest answer for anything served by somebody else's
    datacenter. Values are GiB against the whole machine, measured by ``tools/host_caps.py``.
    """
    if raw is None:
        return {"memory_gib": 0, "disk_gib": 0}
    if not isinstance(raw, dict):
        raise DefinitionError(f"{where}.requires must be a table")
    for key in raw:
        if key not in MODEL_REQUIRE_KEYS:
            raise DefinitionError(f"{where}.requires has unknown key {key!r}")
    return {
        "memory_gib": _optional_int(raw, "memory_gib", f"{where}.requires"),
        "disk_gib": _optional_int(raw, "disk_gib", f"{where}.requires"),
    }


def _parse_install(raw: Any, where: str) -> dict[str, Any] | None:
    """``[install]``: which build the harness downloads for this model, and how it samples.

    ``None`` means there is nothing to install - the answer for every remotely served model, and
    the reason ``Ready?`` can state a fact instead of a guess. The bytes themselves are described
    by the lock file this block names; this one says which of a model's builds to take and what
    defaults to start the engine with.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DefinitionError(f"{where}.install must be a table")
    for key in raw:
        if key not in INSTALL_KEYS:
            raise DefinitionError(f"{where}.install has unknown key {key!r}")
    variant = _text(raw, "variant", f"{where}.install")
    if not BARE.fullmatch(variant):
        raise DefinitionError(
            f"{where}.install.variant must be a bare word naming one build of this model"
        )
    lock = raw.get("lock", WEIGHTS_LOCK)
    if not isinstance(lock, str) or "/" in lock or ".." in lock or not lock.endswith(".json"):
        raise DefinitionError(
            f"{where}.install.lock must name a .json file inside the model directory, not a path"
        )
    sampling = raw.get("sampling", {})
    if not isinstance(sampling, dict):
        raise DefinitionError(f"{where}.install.sampling must be a table")
    for key in sampling:
        if key not in SAMPLING_KEYS:
            raise DefinitionError(f"{where}.install.sampling has unknown key {key!r}")
    numbers: dict[str, float] = {}
    for key, value in sampling.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise DefinitionError(f"{where}.install.sampling.{key} must be a non-negative number")
        numbers[key] = float(value)
    # An engine serves one model per process, so two local models on one host need two
    # listeners. The port replaces only the port of the provider's origin, which leaves
    # ``base_url_env`` free to move the host for every model at once.
    port = _optional_int(raw, "port", f"{where}.install")
    if port and not 1 <= port <= 65535:
        raise DefinitionError(f"{where}.install.port must be a usable TCP port")
    return {"variant": variant, "lock": str(lock), "sampling": numbers, "port": port}


def _parse_model(provider_id: str, model_id: str, path: Path) -> dict[str, Any]:
    where = f"models/{provider_id}/{model_id}"
    document = _load_toml(path / "model.toml", "model")
    version = document["schema_version"]
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
        "requires",
        "install",
    }
    for key in document:
        if key not in known:
            raise DefinitionError(f"{where} has unknown key {key!r}")

    label = _text(document, "label", where)
    provider = _text(document, "provider", where)
    if not ID.fullmatch(provider):
        raise DefinitionError(f"{where}.provider must be a lowercase provider identifier")
    if provider != provider_id:
        raise DefinitionError(
            f"{where}.provider is {provider!r} but its directory says {provider_id!r}; the "
            "directory is what retires a model together with its provider, so the two must agree"
        )
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
    context_key = "native_tokens" if version == 2 else "advertised_tokens"
    for key in context:
        if key != context_key:
            raise DefinitionError(f"{where}.context has unknown key {key!r}")
    advertised = (
        _positive_int(context, context_key, f"{where}.context") if context_key in context else None
    )

    lanes = _parse_lanes(document.get("lane"), where, version=version)
    largest_lane = max(lane.get("context_tokens", 0) for lane in lanes.values())
    if version == 2 and advertised is None:
        raise DefinitionError(f"{where}.context.native_tokens must be declared")
    if version == 1 and advertised is not None and advertised < largest_lane:
        raise DefinitionError(
            f"{where}.context.advertised_tokens is smaller than a declared lane window"
        )
    if advertised is None:
        advertised = max(lane["context_tokens"] for lane in lanes.values())

    requires = _parse_requires(document.get("requires"), where)
    install = _parse_install(document.get("install"), where)
    lock_path = path / (install["lock"] if install else WEIGHTS_LOCK)
    if install is not None and not lock_path.is_file():
        raise DefinitionError(
            f"{where}.install names {lock_path.name}, which the model directory does not contain; "
            "a model with weights to install must also ship the lock that pins them, or ./start.sh "
            "would offer a row whose download cannot be verified"
        )
    return {
        "schema_version": version,
        # The qualified pair is the identifier, because it is the identity: two providers can
        # serve one model name, and a selection document that stored only the model half would
        # not say which serving was chosen. The directory keeps its short name (`name`), and every
        # path in an error message is the `where` below.
        "id": f"{provider_id}/{model_id}",
        "name": model_id,
        "where": where,
        "path": path,
        "label": label,
        "provider": provider,
        "model": model_name,
        "slug": slug,
        "credential": credential,
        "key_env": str(key_env or ""),
        "advertised_tokens": advertised,
        "native_tokens": advertised if version == 2 else None,
        "lanes": lanes,
        "capabilities": _string_list(document, "capabilities", where),
        "support_efforts": _string_list(document, "support_efforts", where),
        "default_effort": document.get("default_effort", ""),
        "requires": requires,
        "install": install,
        "weights_dir": path / WEIGHTS_DIR,
        "lock_path": lock_path,
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


def _parse_lanes(raw: Any, where: str, *, version: int = 1) -> dict[str, dict[str, Any]]:
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
        keys = (
            {
                "context_tokens",
                "input_tokens",
                "output_clamp_tokens",
                "default_effort",
            }
            if version == 1
            else {"allocation", "output_mode", "default_effort"}
        )
        for key in table:
            if key not in keys:
                raise DefinitionError(f"{here} has unknown key {key!r}")
        effort = table.get("default_effort", "")
        if effort and (not isinstance(effort, str) or not BARE.fullmatch(effort)):
            raise DefinitionError(f"{here}.default_effort must be a bare lowercase word")
        if version == 2:
            if (
                table.get("allocation") != "engine"
                or table.get("output_mode") != "remaining_context"
            ):
                raise DefinitionError(
                    f"{here} must use allocation = engine and output_mode = remaining_context"
                )
            lanes[name] = {
                "lane": name,
                "allocation": "engine",
                "output_mode": "remaining_context",
                "default_effort": str(effort or ""),
            }
            continue
        lanes[name] = {
            "lane": name,
            "context_tokens": _positive_int(table, "context_tokens", here),
            "input_tokens": _positive_int(table, "input_tokens", here),
            "output_clamp_tokens": _positive_int(table, "output_clamp_tokens", here),
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
