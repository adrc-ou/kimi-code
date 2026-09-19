#!/usr/bin/env python3
"""Resolve selected models against their providers' policy rules into one enforcement plan.

The plan is the single answer to "what may this workspace actually do right now". It is derived
at every launch from two inputs the operator owns - which model serves the primary lane, which
serves subagents, and the rules each one's provider publishes - so changing a model changes the
limits instead of requiring a second edit somewhere else.

Counters are keyed by *scope*, which is what makes the shape of the selection matter:

* both lanes on one model  -> one ``model``-scoped key, so the lanes contend for the same
  permits and the aggregate context budget spans both of them;
* two models, one provider -> two ``model``-scoped keys, with only credential- and
  provider-scoped rules shared;
* two models, two providers -> fully disjoint key sets.

Resolution is deliberately greedy. Every derived limit is the largest value the provider's
own rules allow after the declared safety margin, because under-using a paid allowance buys
nothing: it only makes the operator's work slower. The margin is where accidental overage is
prevented, and it is applied to the provider's threshold rather than being subtracted by hand
from a number in ``.env``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

if __package__:
    from .definitions import (
        CONTEXT_RULE_KINDS,
        COUNT_RULE_KINDS,
        LANES,
        NAME_SEPARATOR,
        PER_REQUEST_RULE_KINDS,
        RATE_RULE_KINDS,
        RATE_RULE_UNITS,
        SCHEMA_VERSION,
        DefinitionError,
    )
else:
    from definitions import (
        CONTEXT_RULE_KINDS,
        COUNT_RULE_KINDS,
        LANES,
        NAME_SEPARATOR,
        PER_REQUEST_RULE_KINDS,
        RATE_RULE_KINDS,
        RATE_RULE_UNITS,
        SCHEMA_VERSION,
        DefinitionError,
    )

LANE_DISPLAY = {"primary": "Primary", "long": "Long Context", "subagent": "Subagent"}

#: Which generated guidance a caller may ask for. ``lane`` is what every audience gets - the
#: numbers that bound it - and ``main`` adds the blocks only the primary agent can act on.
#: Neither is a lane name, and the two vocabularies stay deliberately separate.
AUDIENCE_LANE = "lane"
AUDIENCE_MAIN = "main"


#: Every keying scope a counter may be built for, narrowest first. ``model`` is included
#: because a rate rule about one model still needs its own ledger at that scope.
COUNTER_SCOPES = ("model", "credential", "credential_model", "provider")

#: What to give Kimi's subagent fan-out when the selected provider states no ceiling at all.
#: A provider with no concurrency rule permits anything, and "unbounded" is not a number the
#: launcher can publish; this is the harness's own default, reported as such in the guidance.
DEFAULT_SUBAGENT_FAN_OUT = 8
#: How to say a rate counter's capacity to a human, in the unit the provider meters it in.
RATE_UNIT_LABELS = {
    "output_tokens": "output tokens/minute",
    "input_tokens": "input tokens/minute",
    "total_tokens": "tokens/minute",
    "requests": "requests/minute",
}


class ResolutionError(DefinitionError):
    """The selected models cannot be served together inside their providers' rules."""


#: The proxy reads a credential from one file named by the plan, and constrains that name to
#: ``[a-z0-9_]{1,64}``. Model-scoped names are synthesised here, so the same bound is checked
#: at resolution: a name the proxy would refuse must stop the launch, not the first request.
SECRET_NAME_MAX = 64


def _has_value(values: dict[str, str], name: str) -> bool:
    """Whether the resolved environment actually carries a value for one variable name.

    Blank is absent. An operator who leaves a declared name empty in ``.env`` means "not set
    here", which is the same state as omitting the line, and Compose hands over the empty
    string for both.
    """
    return bool(name) and bool((values.get(name) or "").strip())


def scope_keys(
    providers: dict[str, dict[str, Any]],
    models: dict[str, dict[str, Any]],
    model_ids: tuple[str, ...],
    values: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Give each model that asked for its own key a credential identity of its own.

    A model authenticates with its own key when the variable its ``model.toml`` names has a
    value, and with the provider-scoped key of its ``credential`` otherwise. When neither has
    a value the model keeps its own scope, because that is what the launcher then asks for: a
    key requested for one model must not be written into the file every model of the provider
    reads.

    The rewrite happens before any counter is built, because the credential id is part of what
    a credential-scoped rule counts against. Two models on two keys share no ledger upstream,
    so they must share none here either; two models on one provider key keep sharing one.
    """
    scoped_providers = {
        provider_id: {**provider, "credentials": dict(provider["credentials"])}
        for provider_id, provider in providers.items()
    }
    scoped_models = {model_id: dict(model) for model_id, model in models.items()}
    for model_id in dict.fromkeys(model_ids):
        model = scoped_models[model_id]
        key_env = model.get("key_env") or ""
        if not key_env:
            continue
        provider = scoped_providers[model["provider"]]
        base = provider["credentials"].get(model["credential"])
        if base is None:
            raise ResolutionError(
                f"model {model_id} names credential {model['credential']!r}, which "
                f"providers/{provider['id']} does not declare"
            )
        if base.get("fallback_env"):
            # Already this model's own scope: one model selected for several lanes must be
            # scoped once, or its credential id would grow a suffix per lane.
            continue
        own_key = _has_value(values, key_env)
        shared_key = _has_value(values, base["env"])
        if not own_key and shared_key:
            # Nothing to scope: this model reads the provider-scoped key like its siblings.
            continue
        credential_id = f"{model['credential']}{NAME_SEPARATOR}{model['slug']}"
        secret_name = f"{provider['id']}{NAME_SEPARATOR}{credential_id}"
        if credential_id in provider["credentials"]:
            raise ResolutionError(
                f"{model_id} would scope its key as credential {credential_id!r}, which "
                f"providers/{provider['id']} already declares; rename that credential or the "
                "model's slug"
            )
        if len(secret_name) > SECRET_NAME_MAX:
            raise ResolutionError(
                f"{model_id} credential name {secret_name!r} exceeds {SECRET_NAME_MAX} "
                "characters, which the proxy refuses to open"
            )
        provider["credentials"][credential_id] = {
            **base,
            "id": credential_id,
            # The launcher shows these when it has to ask, and the question is about one model:
            # naming both variables is what tells the operator which scope they are choosing.
            "label": f"{model['label']} API key",
            "prompt": f"API key for {model['label']}",
            "env": key_env,
            "fallback_env": base["env"],
            "secret_name": secret_name,
        }
        model["credential"] = credential_id
    return scoped_providers, scoped_models


def _key_for(scope: str, provider: str, model: dict[str, Any]) -> tuple[str, str]:
    """Return (scope family, subject) deciding which traffic shares one rule's counter."""
    credential = model["credential"]
    name = model["model"]
    if scope == "model":
        return ("model", f"{provider}/{name}")
    if scope == "provider":
        return ("provider", provider)
    if scope == "credential":
        return ("credential", f"{provider}/{credential}")
    return ("credential_model", f"{provider}/{credential}/{name}")


def _applies(rule: dict[str, Any], model: dict[str, Any]) -> bool:
    return rule["models"] is None or model["model"] in rule["models"]


def _identity_groups(
    models: dict[str, dict[str, Any]], scope: str, lane_entries: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Split one model group by the upstream identity a scope actually counts against.

    ``model`` and ``provider`` subjects are built from things every definition in the group
    shares, so the group stays whole. ``credential`` and ``credential_model`` subjects name a
    credential id, and two definitions of one wire model authenticated with two different keys
    share no ledger upstream -- so they must share none here, and each key's counter carries
    only the lanes using it.
    """
    if scope not in {"credential", "credential_model"}:
        return [(models[lane_entries[0]["model_id"]], lane_entries)]
    groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for entry in lane_entries:
        model = models[entry["model_id"]]
        group = groups.setdefault(model["credential"], (model, []))
        group[1].append(entry)
    return list(groups.values())


def _provider_rules(provider: dict[str, Any], family: frozenset[str]) -> list[dict[str, Any]]:
    return [rule for rule in provider["rules"] if rule["kind"] in family]


def _model_context_cap(provider: dict[str, Any], model: dict[str, Any]) -> int | None:
    caps = [
        rule["value"]
        for rule in _provider_rules(provider, PER_REQUEST_RULE_KINDS)
        if rule["kind"] == "max_context_tokens" and _applies(rule, model)
    ]
    return min(caps) if caps else None


def _model_output_cap(provider: dict[str, Any], model: dict[str, Any]) -> int | None:
    caps = [
        rule["value"]
        for rule in _provider_rules(provider, PER_REQUEST_RULE_KINDS)
        if rule["kind"] == "max_output_tokens_per_request" and _applies(rule, model)
    ]
    return min(caps) if caps else None


def _context_counter(
    provider: dict[str, Any], model: dict[str, Any], lanes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Aggregate-context and exclusivity thresholds for one model-scoped counter."""
    aggregate = [
        rule
        for rule in _provider_rules(provider, CONTEXT_RULE_KINDS)
        if rule["kind"] == "aggregate_context_fraction" and _applies(rule, model)
    ]
    exclusive = [
        rule
        for rule in _provider_rules(provider, CONTEXT_RULE_KINDS)
        if rule["kind"] == "exclusive_above_context_fraction" and _applies(rule, model)
    ]
    counts = [
        rule["value"]
        for rule in _provider_rules(provider, COUNT_RULE_KINDS)
        if _applies(rule, model) and rule["scope"] == "model"
    ]
    if not aggregate and not exclusive and not counts:
        return None
    margin = provider["context_margin_percent"]
    percent = min((rule["value"] for rule in aggregate), default=None)
    ceiling = None if percent is None else model["advertised_tokens"] * percent // 100
    exclusive_percent = min((rule["value"] for rule in exclusive), default=None)
    exclusive_at = (
        None
        if exclusive_percent is None
        else model["advertised_tokens"] * exclusive_percent // 100
    )
    subject = f"{provider['id']}/{model['model']}"
    return {
        "id": f"context:{subject}",
        "family": "context",
        "provider": provider["id"],
        "subject": model["model"],
        "advertised_tokens": model["advertised_tokens"],
        # The provider's threshold is the hard rule; the budget applies the declared margin.
        # Budget is never allowed above the threshold even at a 100% margin.
        "ceiling": ceiling,
        "budget": None if ceiling is None else min(ceiling, ceiling * margin // 100),
        "exclusive_at": exclusive_at,
        "max": min(counts) if counts else None,
        "lanes": [lane["lane"] for lane in lanes],
    }


def _shared_counters(
    provider: dict[str, Any], model: dict[str, Any], scope: str, lanes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rate and request-count counters for one keying scope.

    The model scope differs in one respect: a model-scoped concurrency limit is folded into
    that model's context counter, because both describe the same set of in-flight requests and
    one admission should take one permit per rule. A rate rule gets its own ledger at every
    scope, because a rolling per-minute window is not the same object as an in-flight cap.
    """
    family, subject = _key_for(scope, provider["id"], model)
    result: list[dict[str, Any]] = []
    rate_margin = provider["output_rate_margin_percent"]
    for kind in sorted(RATE_RULE_KINDS):
        limits = [
            rule["value"]
            for rule in provider["rules"]
            if rule["kind"] == kind and rule["scope"] == scope and _applies(rule, model)
        ]
        if not limits:
            continue
        limit = min(limits)
        result.append(
            {
                "id": f"rate:{kind}:{family}:{subject}",
                "family": "rate",
                "kind": kind,
                "unit": RATE_RULE_UNITS[kind],
                "scope": scope,
                "provider": provider["id"],
                "subject": subject,
                # NRP measures only output tokens, and the proxy books output, so the
                # rate margin applies. Providers that publish input rules get their own
                # independent ledger for the same reason.
                "capacity": max(1, limit * rate_margin // 100),
                "lanes": [lane["lane"] for lane in lanes],
            }
        )
    if family == "model":
        return result
    counts = [
        rule["value"]
        for rule in provider["rules"]
        if rule["kind"] in COUNT_RULE_KINDS and rule["scope"] == scope and _applies(rule, model)
    ]
    if counts:
        result.append(
            {
                "id": f"count:{family}:{subject}",
                "family": "count",
                "scope": scope,
                "provider": provider["id"],
                "subject": subject,
                "max": min(counts),
                "lanes": [lane["lane"] for lane in lanes],
            }
        )
    return result


def _lane_entry(
    lane: str,
    model: dict[str, Any],
    provider: dict[str, Any],
    *,
    reserved_context_size: int,
    counters: list[str] | None = None,
) -> dict[str, Any]:
    context_cap = _model_context_cap(provider, model)
    output_cap = _model_output_cap(provider, model)
    table = model["lanes"][lane]
    context = table["context_tokens"]
    if context_cap is not None:
        context = min(context, context_cap)
    clamp = table["output_clamp_tokens"]
    if output_cap is not None:
        clamp = min(clamp, output_cap)
    input_tokens = min(table["input_tokens"], context - clamp)
    if input_tokens + reserved_context_size > context:
        raise ResolutionError(
            f"{model['id']} lane {lane!r}: {input_tokens} input + {reserved_context_size} "
            f"reserved does not fit {context} context"
        )
    return {
        "lane": lane,
        "model_id": model["id"],
        "label": model["label"],
        "alias": f"{model['slug']}-{lane}",
        "provider": provider["id"],
        "provider_name": f"{provider['id']}-{lane}",
        "route": f"/{lane}/v1",
        "model": model["model"],
        "credential": model["credential"],
        # The declared name, kept even when the provider-scoped key won, because the launcher
        # has to read it for the model-scoped choice to be possible at all.
        "key_env": model.get("key_env", ""),
        "context_tokens": context,
        "input_tokens": input_tokens,
        "output_clamp_tokens": clamp,
        "reservation": input_tokens + clamp,
        "capabilities": model["capabilities"],
        "support_efforts": model["support_efforts"],
        "default_effort": table["default_effort"] or model["default_effort"],
        "counters": counters,
        "display_name": f"{model['label']} - {LANE_DISPLAY.get(lane, lane)}",
    }


def resolve(
    providers: dict[str, dict[str, Any]],
    models: dict[str, dict[str, Any]],
    selection: dict[str, str],
    *,
    reserved_context_size: int,
    key_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the enforcement plan for one primary model and one subagent model.

    ``key_values`` is the resolved launcher environment, and it is an input because which key
    a model authenticates with is a fact about the upstream identity being metered. Every
    derived limit is unchanged by it: scoping a key per model splits ledgers, it does not
    enlarge any allowance.
    """
    if reserved_context_size <= 0:
        raise ResolutionError("reserved_context_size must be positive")
    for role in ("primary", "subagent"):
        if selection.get(role) not in models:
            available = ", ".join(sorted(models)) or "none"
            raise ResolutionError(
                f"selected {role} model {selection.get(role)!r} is not available; "
                f"defined models: {available}"
            )

    providers, models = scope_keys(
        providers,
        models,
        (selection["primary"], selection["subagent"]),
        key_values or {},
    )

    # Lane membership follows the selection: the long lane exists only when the primary
    # model declares it, so an operator who drops the lane drops the route and the alias.
    members: dict[str, list[tuple[str, str]]] = {}
    for role in ("primary", "subagent"):
        model = models[selection[role]]
        members.setdefault(role, []).append((role, model["id"]))
        if role == "primary" and "long" in model["lanes"]:
            members.setdefault("long", []).append(("primary-long", model["id"]))

    selected: dict[str, dict[str, Any]] = {}
    for lane in LANES:
        if lane not in members:
            continue
        _, model_id = members[lane][0]
        model = models[model_id]
        provider = providers.get(model["provider"])
        if provider is None:
            raise ResolutionError(
                f"model {model_id} references missing provider {model['provider']!r}"
            )
        selected[lane] = _lane_entry(
            lane, model, provider, reserved_context_size=reserved_context_size
        )

    # Counters are built once per provider and model, with the full list of lanes bound to
    # that pair, and only then bound back to each lane by membership. That order matters: a
    # model whose provider publishes no context rule gets no context counter at all, and a
    # lane bound to a counter nobody created would strand its traffic on a permit that never
    # exists.
    counters: dict[str, dict[str, Any]] = {}
    lanes_by_model: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for lane in selected.values():
        lanes_by_model.setdefault((lane["provider"], lane["model"]), []).append(lane)
    for (provider_id, _model_name), lane_entries in lanes_by_model.items():
        model = next(models[entry["model_id"]] for entry in lane_entries)
        provider = providers[provider_id]
        context = _context_counter(provider, model, lane_entries)
        if context is not None:
            counters[context["id"]] = context
        for scope in COUNTER_SCOPES:
            groups = _identity_groups(models, scope, lane_entries)
            for group_model, group_lanes in groups:
                for counter in _shared_counters(provider, group_model, scope, group_lanes):
                    existing = counters.get(counter["id"])
                    if existing is None:
                        counters[counter["id"]] = counter
                    else:
                        existing["lanes"] = sorted(
                            set(existing["lanes"]) | set(counter["lanes"])
                        )
                        if "capacity" in counter:
                            existing["capacity"] = min(existing["capacity"], counter["capacity"])
                        if "max" in counter:
                            existing["max"] = min(existing["max"] or counter["max"], counter["max"])

    for lane_name, lane in selected.items():
        lane["counters"] = sorted(
            key for key, counter in counters.items() if lane_name in counter["lanes"]
        )

    for lane in selected.values():
        threshold = _exclusive_threshold(lane, counters)
        lane["exclusive"] = threshold is not None and lane["reservation"] >= threshold
        lane["exclusive_at"] = threshold

    _check_budgets(selected, counters)
    limits = _derive_limits(selected, counters)

    return {
        "schema_version": SCHEMA_VERSION,
        "reserved_context_size": reserved_context_size,
        "selection": dict(selection),
        "providers": {
            provider_id: {
                "label": provider["label"],
                "protocol": provider["protocol"],
                "base_url": provider["base_url"],
                "base_url_env": provider["base_url_env"],
                "policy_url": provider["policy_url"],
                "context_margin_percent": provider["context_margin_percent"],
                "output_rate_margin_percent": provider["output_rate_margin_percent"],
                "credentials": {
                    credential_id: {
                        "label": credential["label"],
                        "prompt": credential["prompt"],
                        "env": credential["env"],
                        "key_url": credential["key_url"],
                        "secret_name": credential["secret_name"],
                        # Present only on a model-scoped credential: the provider-scoped
                        # variable this key would have fallen back to, which is the other half
                        # of what the launcher tells the operator to set.
                        "fallback_env": credential.get("fallback_env", ""),
                    }
                    for credential_id, credential in provider["credentials"].items()
                },
                "rules": provider["rules"],
            }
            for provider_id, provider in providers.items()
            if any(lane["provider"] == provider_id for lane in selected.values())
        },
        "lanes": selected,
        "counters": counters,
        "limits": limits,
    }


def _exclusive_threshold(
    lane: dict[str, Any], counters: dict[str, dict[str, Any]]
) -> int | None:
    thresholds = [
        counters[counter]["exclusive_at"]
        for counter in lane["counters"]
        if counter in counters and counters[counter].get("exclusive_at") is not None
    ]
    return min(thresholds) if thresholds else None


def _check_budgets(
    selected: dict[str, dict[str, Any]], counters: dict[str, dict[str, Any]]
) -> None:
    """Refuse any configuration where a lane could never be admitted.

    A lane whose reservation reaches its provider's exclusive threshold is allowed to exceed
    the aggregate budget, because the provider itself permits exactly that request to run
    alone; the proxy admits it only when the counter is empty.
    """
    for lane in selected.values():
        for counter_id in lane["counters"]:
            counter = counters.get(counter_id)
            if counter is None:
                continue
            budget = counter.get("budget")
            if budget is None:
                continue
            if lane["reservation"] > budget and not lane["exclusive"]:
                raise ResolutionError(
                    f"{lane['alias']} reserves {lane['reservation']} tokens but counter "
                    f"{counter_id} only budgets {budget}; no request on this lane could ever "
                    "be admitted. Shrink the lane's input cap or raise the provider margin."
                )
            ceiling = counter.get("ceiling")
            if (
                ceiling is not None
                and "advertised_tokens" in counter
                and lane["context_tokens"] > counter["advertised_tokens"]
            ):
                raise ResolutionError(
                    f"{lane['alias']} declares {lane['context_tokens']} tokens of context "
                    f"above the {counter['advertised_tokens']} the provider advertises for "
                    f"{counter['subject']}"
                )


def _derive_limits(
    selected: dict[str, dict[str, Any]], counters: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Largest subagent fan-out the provider rules allow, and the ceilings beside it.

    Every candidate limit is an upper bound the provider publishes or an arithmetic ceiling
    its rules imply; the result is the smallest of them, never a smaller hand-picked value.
    """
    limits: dict[str, Any] = {
        "subagent_concurrency": None,
        "subagent_concurrency_basis": None,
        "lane_concurrency": {},
        "shared_context_counters": False,
    }
    for lane_name, lane in selected.items():
        caps = [
            counters[counter_id]["max"]
            for counter_id in lane["counters"]
            if counter_id in counters and counters[counter_id].get("max") is not None
        ]
        limits["lane_concurrency"][lane_name] = min(caps) if caps else None

    subagent = selected.get("subagent")
    if subagent is None:
        return limits
    if subagent["exclusive"]:
        # The provider says a request this large runs alone, so a second subagent is not
        # waiting for capacity the first one released - it is waiting for the first to end.
        limits["subagent_concurrency"] = 1
        limits["subagent_concurrency_basis"] = "each subagent request runs alone"
        return limits
    candidates = [
        limits["lane_concurrency"]["subagent"]
    ] if limits["lane_concurrency"].get("subagent") else []
    for counter_id in subagent["counters"]:
        counter = counters.get(counter_id)
        if counter is None or counter.get("budget") is None:
            continue
        candidates.append(counter["budget"] // subagent["reservation"])
        if len(counter["lanes"]) > 1:
            limits["shared_context_counters"] = True
    if not candidates:
        # Nothing in the selected provider's rules bounds how many subagents run at once.
        # An unconstrained provider is a reason to use the allowance, not a reason to stall
        # the workspace on a 1, so the harness publishes its own default and says so.
        limits["subagent_concurrency"] = DEFAULT_SUBAGENT_FAN_OUT
        limits["subagent_concurrency_basis"] = "harness default; provider states no ceiling"
        return limits
    limits["subagent_concurrency"] = max(1, min(candidates))
    limits["subagent_concurrency_basis"] = "the tightest provider rule"
    return limits


#: Every generated guidance block belongs to exactly one startup-panel option and is written for
#: exactly one audience. ``lane`` text reaches every agent, because ``${agents_md}`` is substituted
#: into the main prompt and each subagent's alike; ``main`` text reaches only the main agent,
#: through the staged system prompt. Main carries no lane text, so nothing arrives twice.
GUIDANCE_AUDIENCES = ("lane", "main")
#: The option ids that switch each generated block on or off.
OPTION_LANE_LIMITS = "lane_limits"
OPTION_LANE_TABLE = "lane_table"
OPTION_PARALLELISM = "parallelism"


class GuidanceSection(NamedTuple):
    """One generated block: the option that toggles it, the audience it addresses, its text."""

    option: str
    audience: str
    lines: list[str]


def _context_counters(plan: dict[str, Any]):
    return (c for c in plan["counters"].values() if c["family"] == "context")


def _lane_order(plan: dict[str, Any]) -> list[str]:
    return [name for name in LANES if name in plan["lanes"]]


def _lane_limit_lines(plan: dict[str, Any]) -> list[str]:
    """What every agent must respect, because every request spends the same capacity.

    Free of anything only the main agent can act on. A subagent has no spawning tool, so
    concurrency advice in its prompt is several hundred tokens about a thing it cannot do.
    """
    lines = [
        "## Model usage limits (generated at launch)",
        "",
        "The harness derived these numbers from the selected model definitions and their",
        "providers' policy rules at every start, and the model proxy enforces every one of them",
        "independently of this text. Nothing in this section is maintained by hand.",
        "",
    ]
    lines += [
        f"- Provider **{provider['label']}** (`providers/{provider_id}`): "
        f"{provider['policy_url'] or 'no published policy URL'}"
        for provider_id, provider in sorted(plan["providers"].items())
    ]
    for counter in _context_counters(plan):
        margin = plan["providers"][counter["provider"]]["context_margin_percent"]
        lines.append(
            f"- Every request spends one shared in-flight context budget: "
            f"**{counter['budget']:,} tokens** for `{counter['subject']}`. The provider's own "
            f"threshold is {counter['ceiling']:,} and the declared safety margin is {margin}%. "
            "A request you delegate spends that budget exactly as your own does."
        )
        if counter.get("exclusive_at") is not None:
            lines.append(
                f"- A single request at or above {counter['exclusive_at']:,} tokens runs alone: "
                "the proxy admits it only when nothing else is in flight and holds every other "
                "request queued until it finishes."
            )
    for counter in plan["counters"].values():
        if counter["family"] != "rate":
            continue
        unit = counter.get("unit", "output_tokens")
        lines.append(
            f"- Rate: **{counter['capacity']:,} {RATE_UNIT_LABELS.get(unit, unit)}** booked per "
            f"minute against `{counter['subject']}`, measured in the unit the provider meters."
        )
    lines.append("- Per-lane ceilings, as window / input cap / output clamp:")
    for name in _lane_order(plan):
        lane = plan["lanes"][name]
        lines.append(
            f"  - `{name}` {lane['context_tokens']:,} / {lane['input_tokens']:,} / "
            f"{lane['output_clamp_tokens']:,}"
        )
    lines += [
        "  Each input cap sits below its own window on purpose: the proxy clamps the response's",
        "  output budget, so a request that filled its window would lose its tail. Compacting a",
        "  little early is the intended response, not a truncation.",
        "- Queueing is normal operation. A permit you are waiting for belongs to a request that",
        "  will still run, and the proxy holds it while it waits.",
        "- HTTP 429 and provider server errors are backpressure, not a licence to widen",
        "  concurrency. Never answer them by opening extra connections, containers, credentials",
        "  or sessions, and never call a provider endpoint around the proxy: each spends",
        "  capacity the proxy cannot see.",
        "- Only the main agent in this workspace has a subagent-spawning tool. If you are reading",
        "  this as a subagent you cannot delegate further, and the lane serving you is bound by",
        "  the harness - do not try to change it.",
        "",
    ]
    return lines


def _lane_table_lines(plan: dict[str, Any]) -> list[str]:
    """The per-lane shape of the session, which is only useful to the agent that chooses lanes.

    It restates the pool these lanes draw from rather than pointing at the all-lane block for it.
    The two are separate checkboxes, so a table whose "Runs alone" column meant nothing without a
    sibling would be a hole in the prompt the moment an operator unchecked one.
    """
    lines = [
        "## Model runtime envelope (generated at launch)",
        "",
        "The per-lane shape of this session's capacity. The proxy enforces every number below and",
        "the launcher configures Kimi's own dispatch limit to match, so the two cannot disagree.",
        "",
        "| Lane | Model | Alias | Context | Input cap | Output clamp | In-flight cost | "
        "Runs alone |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in _lane_order(plan):
        lane = plan["lanes"][name]
        lines.append(
            f"| `{name}` | {lane['label']} | `{lane['alias']}` | "
            f"{lane['context_tokens']:,} | {lane['input_tokens']:,} | "
            f"{lane['output_clamp_tokens']:,} | {lane['reservation']:,} | "
            f"{'yes' if lane['exclusive'] else 'no'} |"
        )
    lines += [
        "",
        "What that capacity is drawn from, so the table reads on its own:",
        "",
    ]
    lines += [
        f"- Provider **{plan['providers'][provider_id]['label']}** serves `{model}` under "
        f"`providers/{provider_id}`."
        for provider_id, model in sorted(
            {(lane["provider"], lane["model"]) for lane in plan["lanes"].values()}
        )
        if plan["providers"].get(provider_id, {}).get("label")
    ]
    for counter in _context_counters(plan):
        lines.append(
            f"- The `{counter['subject']}` pool that every one of those rows reserves against "
            f"holds **{counter['budget']:,} tokens** in flight in total, not per lane. Opening a "
            "lane is a reservation against it, and closing a request is what releases it."
        )
        if counter.get("exclusive_at") is not None:
            lines.append(
                f"- The **Runs alone** column is decided by one figure: at or above "
                f"{counter['exclusive_at']:,} tokens a request is admitted only when the pool is "
                "otherwise empty."
            )
    for counter in plan["counters"].values():
        if counter["family"] != "rate":
            continue
        unit = counter.get("unit", "output_tokens")
        lines.append(
            f"- The pool is also metered by time: **{counter['capacity']:,} "
            f"{RATE_UNIT_LABELS.get(unit, unit)}** against `{counter['subject']}`, "
            "booked before a request runs and settled against what it used."
        )
    for counter in _context_counters(plan):
        if counter.get("max") is not None:
            lines.append(
                f"- At most {counter['max']} concurrent requests for `{counter['subject']}`."
            )
    for counter in plan["counters"].values():
        if counter["family"] == "count":
            lines.append(
                f"- At most {counter['max']} concurrent requests across `{counter['subject']}`."
            )
    per_lane = plan["limits"].get("lane_concurrency") or {}
    if per_lane:
        ceilings = ", ".join(
            f"`{name}` {per_lane[name]}" for name in _lane_order(plan) if name in per_lane
        )
        lines.append(f"- Per-lane request ceiling: {ceilings}.")
    subagent_limit = plan["limits"].get("subagent_concurrency")
    if subagent_limit:
        basis = plan["limits"].get("subagent_concurrency_basis") or "the tightest provider rule"
        lines.append(
            f"- Kimi may run **up to {subagent_limit} subagents concurrently** ({basis}); one "
            "more than that simply queues at the proxy rather than failing."
        )
    else:
        lines.append(
            "- This selection publishes no subagent concurrency ceiling; the proxy still enforces "
            "whatever provider rules do apply."
        )
    lines += [
        "- Keep the default model as launched. A primary request must never be deliberately routed",
        "  through the subagent lane, and the subagent model is bound by the harness rather than",
        "  chosen per call.",
        "- A long-context request is a bigger primary step, not a way to obtain subagent-style",
        "  concurrency: it is served alone precisely because it is large. Let it finish instead of",
        "  trimming the other lanes to make room for it.",
        "- Backgrounded agents and background Bash share one task-slot pool, separate from these",
        "  lanes, and exceeding it fails outright rather than queueing.",
        "- Subagent and swarm wall-clock limits are unlimited, so a long run is bounded by the",
        "  proxy and by context instead. A stalled stream is a reason to resume the work, not to",
        "  redesign it.",
        "- Provider terms change and model windows differ. To alter any number here, edit the",
        "  definitions in `./models` and `./providers`, then restart the stack - never `.env`.",
        "  This section states the limits and changes none.",
        "- Queueing at the proxy is normal, so never retry around a queue you are waiting on.",
        "",
    ]
    return lines


def _parallelism_lines(plan: dict[str, Any]) -> list[str]:
    """How wide to fan out, stated with its own numbers so it reads coherently on its own."""
    subagent_limit = plan["limits"].get("subagent_concurrency")
    if subagent_limit:
        basis = plan["limits"].get("subagent_concurrency_basis") or "the tightest provider rule"
        opening = (
            f"This session may run up to {subagent_limit} subagents at once - {basis} - and the "
            "model proxy enforces that same ceiling, so it is capacity that has already been paid "
            f"for. Treat {subagent_limit} as the default fan-out for any step that splits into "
            "independent parts, and read running below it as a cost rather than as caution."
        )
    else:
        opening = (
            "The model proxy publishes no subagent ceiling for this selection, but it still "
            "enforces whatever provider rules do apply, so delegate freely and let the proxy "
            "pace you."
        )
    return [
        "## Parallel work",
        "",
        opening,
        "",
        "Choosing the shape of the work:",
        "",
        "- Three or more independent lookups, reads, searches, or investigations belong in one",
        "  `AgentSwarm` call rather than several narrower ones. Each call ramps its own launches",
        "  up, so splitting a fan-out across calls pays that ramp repeatedly.",
        "- Delegate work whose bulk output this context would otherwise absorb. Several children",
        "  that each hand back a short conclusion are cheaper than one search that returns its",
        "  raw results here.",
        "- Do not delegate a read whose path is already known, or a step that depends on reasoning",
        "  this turn is still holding. Handoff has a price; pay it when the child does the work,",
        "  not when it carries it.",
        "- Investigate in parallel, write serially. Many agents may read; only one may touch a",
        "  given set of files at a time.",
        "",
        "Respect capacity as well as count:",
        "",
        "- A lane reserves its full window the moment it is admitted, whether or not the prompt is",
        "  that large, so a wide fan-out holds the whole allowance until it finishes. Let it",
        "  finish instead of generating alongside it.",
        "- Prefer concise evidence in a hand-back: every token a child holds is charged against",
        "  the shared in-flight budget rather than against its own lane alone.",
        "- Waiting for a permit at the proxy is normal pacing, not a failure, and the request is",
        "  not lost. Never dodge a queue with extra connections, lanes, containers or credentials.",
        "",
        "Before starting one agent, ask how many things could be true at the same time. That",
        "number, not the number of questions you happen to have written down, is the fan-out to",
        "use.",
        "",
    ]


def guidance_block(section: GuidanceSection) -> str:
    """One generated section as text, edge newlines off.

    Both the composer and :func:`render_guidance` go through here, so a block measured on its own
    in the panel is byte-identical to the same block inside a composed document.
    """
    return "\n".join(section.lines).strip("\n")


def guidance_sections(plan: dict[str, Any], audience: str) -> list[GuidanceSection]:
    """The generated blocks written for one audience, in the order they are appended."""
    if audience == AUDIENCE_LANE:
        return [GuidanceSection(OPTION_LANE_LIMITS, audience, _lane_limit_lines(plan))]
    if audience == AUDIENCE_MAIN:
        return [
            GuidanceSection(OPTION_LANE_TABLE, audience, _lane_table_lines(plan)),
            GuidanceSection(OPTION_PARALLELISM, audience, _parallelism_lines(plan)),
        ]
    raise ValueError(f"unknown guidance audience: {audience}")


def render_guidance(
    plan: dict[str, Any], audience: str, enabled: dict[str, bool] | None = None
) -> str:
    """The generated envelope text for one audience, honouring the panel's choices.

    ``audience`` is what keeps harness talk aimed at the agent that can act on it: ``lane`` is the
    core every request obeys regardless of who sends it, and ``main`` is the lane table and the
    fan-out advice, which are meaningless to an agent that cannot pick a lane or start a child.
    ``enabled`` maps an option id to a boolean; ``None`` means every option is on, which is the
    shipped default and what a non-interactive launch with no saved choices gets.
    """
    blocks = [
        guidance_block(section)
        for section in guidance_sections(plan, audience)
        if enabled is None or enabled.get(section.option, True)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""
