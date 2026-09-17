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

from typing import Any

if __package__:
    from .definitions import (
        CONTEXT_RULE_KINDS,
        COUNT_RULE_KINDS,
        PER_REQUEST_RULE_KINDS,
        RATE_RULE_KINDS,
        RATE_RULE_UNITS,
        DefinitionError,
    )
else:
    from definitions import (
        CONTEXT_RULE_KINDS,
        COUNT_RULE_KINDS,
        PER_REQUEST_RULE_KINDS,
        RATE_RULE_KINDS,
        RATE_RULE_UNITS,
        DefinitionError,
    )

SCHEMA_VERSION = 1
LANE_ORDER = ("primary", "long", "subagent")
LANE_DISPLAY = {"primary": "Primary", "long": "Long Context", "subagent": "Subagent"}

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
) -> dict[str, Any]:
    """Build the enforcement plan for one primary model and one subagent model."""
    if reserved_context_size <= 0:
        raise ResolutionError("reserved_context_size must be positive")
    for role in ("primary", "subagent"):
        if selection.get(role) not in models:
            available = ", ".join(sorted(models)) or "none"
            raise ResolutionError(
                f"selected {role} model {selection.get(role)!r} is not available; "
                f"defined models: {available}"
            )

    # Lane membership follows the selection: the long lane exists only when the primary
    # model declares it, so an operator who drops the lane drops the route and the alias.
    members: dict[str, list[tuple[str, str]]] = {}
    for role in ("primary", "subagent"):
        model = models[selection[role]]
        lane = role if role == "subagent" else "primary"
        members.setdefault(lane, []).append((role, model["id"]))
        if role == "primary" and "long" in model["lanes"]:
            members.setdefault("long", []).append(("primary-long", model["id"]))

    selected: dict[str, dict[str, Any]] = {}
    for lane in LANE_ORDER:
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
            for counter in _shared_counters(provider, model, scope, lane_entries):
                existing = counters.get(counter["id"])
                if existing is None:
                    counters[counter["id"]] = counter
                else:
                    existing["lanes"] = sorted(set(existing["lanes"]) | set(counter["lanes"]))
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


def render_guidance(plan: dict[str, Any]) -> str:
    """Human- and model-readable statement of the resolved envelope.

    This text is what lands in the workspace ``AGENTS.md`` managed section. It states the
    numbers and then instructs the agent to use all of them: a limit that is respected but
    not reached is a slower workspace, not a safer one.
    """
    lines: list[str] = [
        "## Model runtime envelope (generated at launch - do not hand-edit)",
        "",
        "The harness derived these numbers from the selected model definitions and their",
        "providers' policy rules. They are recomputed on every start, so an edit here is",
        "discarded, and the model proxy enforces them independently of anything written below.",
        "",
    ]
    for provider_id, provider in sorted(plan["providers"].items()):
        source = provider["policy_url"] or "no published policy URL"
        lines.append(f"- Provider **{provider['label']}** (`providers/{provider_id}`): {source}")
    lines.append("")
    lines.append(
        "| Lane | Model | Alias | Context | Input cap | Output clamp | "
        "In-flight cost | Runs alone |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for lane_name in LANE_ORDER:
        lane = plan["lanes"].get(lane_name)
        if lane is None:
            continue
        lines.append(
            f"| `{lane_name}` | {lane['label']} | `{lane['alias']}` | "
            f"{lane['context_tokens']:,} | {lane['input_tokens']:,} | "
            f"{lane['output_clamp_tokens']:,} | {lane['reservation']:,} | "
            f"{'yes' if lane['exclusive'] else 'no'} |"
        )
    lines.append("")
    for counter in plan["counters"].values():
        if counter["family"] == "context":
            budget = counter["budget"]
            ceiling = counter["ceiling"]
            lines.append(
                f"- Aggregate in-flight context for `{counter['subject']}`: **{budget:,} tokens** "
                f"(provider threshold {ceiling:,}, margin "
                f"{plan['providers'][counter['provider']]['context_margin_percent']}%), "
                f"shared by lanes {', '.join(f'`{lane}`' for lane in counter['lanes'])}"
            )
            if counter.get("exclusive_at") is not None:
                lines.append(
                    f"- Any single request at or above {counter['exclusive_at']:,} tokens "
                    "runs alone."
                )
            if counter.get("max") is not None:
                lines.append(f"- At most {counter['max']} concurrent requests for that model.")
        elif counter["family"] == "rate":
            unit = counter.get("unit", "output_tokens")
            lines.append(
                f"- Rate for `{counter['subject']}`: **{counter['capacity']:,} "
                f"{RATE_UNIT_LABELS.get(unit, unit)}** booked, measured in the unit "
                "the provider meters."
            )
        elif counter["family"] == "count":
            lines.append(
                f"- At most {counter['max']} concurrent requests across `{counter['subject']}`."
            )
    lines.append("")
    subagent_limit = plan["limits"].get("subagent_concurrency")
    if subagent_limit:
        basis = plan["limits"].get("subagent_concurrency_basis") or "the tightest provider rule"
        lines += [
            f"- Kimi may run **up to {subagent_limit} subagents concurrently** ({basis}), and the "
            "proxy holds the same ceiling; one more than that simply queues.",
            "",
            "### Use the whole envelope",
            "",
            f"- Delegate to subagents whenever the work splits, and run all {subagent_limit} "
            "at once when it is independent. Do not self-limit below the published concurrency: "
            "under-using the allowance buys nothing and costs wall-clock time.",
            "- Do not open more than that number, and do not retry around a queue. Waiting for "
            "a permit inside the proxy is normal behaviour, not a failure, and the request is "
            "not lost.",
            "- Keep primary traffic in the primary lane. The subagent model is bound by the "
            "harness and is not a way to obtain extra concurrency.",
            "- A long-context request runs alone by policy. Let it finish instead of trimming the "
            "other lanes to make room for it.",
            "- Context is the scarce resource: prefer concise evidence in a subagent hand-back, "
            "because every token in flight is charged against the shared budget above.",
        ]
    else:
        lines += [
            "- This selection publishes no subagent concurrency ceiling; the proxy still enforces "
            "whatever provider rules do apply."
        ]
    lines += [
        "",
        "Provider terms may change and model windows differ. To alter any number here, edit the",
        "definitions in `./models` and `./providers`, then restart the stack - never `.env`,",
        "never this section.",
        "",
    ]
    return "\n".join(lines)
