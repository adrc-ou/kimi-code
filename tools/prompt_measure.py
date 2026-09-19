#!/usr/bin/env python3
"""Price the context this harness puts in front of a model, in tokens.

Two sources of numbers, deliberately kept apart:

An **estimate** is arithmetic the harness does itself. :func:`estimate_tokens` reimplements Kimi's
own ``estimateTokens`` (``packages/agent-core-v2/src/llm-adapter/contract/tokens.ts``) exactly, so
the estimated half of Kimi's accounting and these figures agree by construction. It is a heuristic
and not the provider's tokenizer, and it is presented as ``~`` for that reason.

A **measurement** is Kimi's finished article. Every session writes a ``profile.bind`` record
carrying the fully-rendered system prompt, so the whole prompt can be read rather than
re-derived. Re-deriving it would mean reimplementing Kimi's ``${...}`` renderer a second time, and
a second implementation diverges silently after an upgrade.

The two are not interchangeable, which is why the history file records which one a number is.
Neither is a cache: a rendered prompt embeds ``${cwd_listing}``, so the same profile measures
differently inside one session as the workspace changes. Every launch re-measures and appends, and
whatever the panel shows is the newest observation plus its age.

Nothing here talks to Docker. The launcher copies the agent home out of the container and hands
this module a plain directory tree, which is what keeps the whole module testable without a stack
and why the compose invocation - and everything it knows about instances and overlays - stays in
one place.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

if __package__:
    from . import prompt_context as pc
else:
    import prompt_context as pc

#: Kimi's estimate is ``ceil(ascii / 4) + non-ascii`` over code points, where "ascii" means a code
#: point at or below 127. Anything above it counts as one token, which is why a page of CJK reads
#: as its character count here and as roughly a third of that to a real tokenizer.
ASCII_CEILING = 127
ASCII_DIVISOR = 4

#: The agent's home inside the container, which is where the initializer stages the composed
#: documents. A ``profile.bind`` prompt quotes this path in the marker that opens each
#: ``AGENTS.md`` it loaded, and that marker is how the harness's own text is told apart from the
#: user's project files.
AGENT_HOME = "/home/agent/.kimi-code"
HARNESS_AGENTS_PATH = f"{AGENT_HOME}/AGENTS.md"
PART_MARKER = "<!-- From: "
PART_MARKER_END = " -->"

#: The two audiences the panel draws. ``main`` is the ``agent`` profile; ``subagent`` is any child
#: role. The long-context lane is *not* a third audience - it is the main agent on another
#: denominator, so it reuses the ``main`` rows with a different cap.
AUDIENCE_MAIN = "main"
AUDIENCE_SUBAGENT = "subagent"

#: Where session logs live relative to the directory that was copied out of the container:
#: ``sessions/<workspace>/<session>/agents/<agentId>/wire.jsonl``. The recursive fallback exists
#: because the depth is Kimi's own layout, not this harness's, and an upgrade that re-nests it must
#: cost a measurement, never a launch.
WIRE_GLOB = "sessions/*/*/agents/*/wire.jsonl"
WIRE_NAME = "wire.jsonl"
BIND_TYPE = "profile.bind"
#: The record that carries the full tool schemas sent with every request. Kimi writes one per
#: distinct schema set, keyed by agent, and it is the only place the largest region of a real
#: context window is visible: the schemas are not part of ``systemPrompt``, so no prompt figure
#: includes them.
TOOLS_TYPE = "llm.tools_snapshot"


def estimate_tokens(text: str) -> int:
    """Kimi's own arithmetic, so a block measured here matches a block Kimi would price here."""
    ascii_count = sum(1 for char in text if ord(char) <= ASCII_CEILING)
    return math.ceil(ascii_count / ASCII_DIVISOR) + (len(text) - ascii_count)


def stringify_json(value: Any) -> str:
    """``JSON.stringify`` equivalent: compact separators, insertion order, no unicode escaping."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def estimate_tools_tokens(tools: Iterable[Mapping[str, Any]]) -> int:
    """Price tool schemas the way ``estimateTokensForTools`` does.

    Tool definitions ride with every request and no checkbox removes them, so this is the number
    that turns "there is context you cannot clear" from an apology into a figure.
    """
    total = 0
    for tool in tools:
        total += estimate_tokens(str(tool.get("name", "")))
        total += estimate_tokens(str(tool.get("description", "")))
        total += estimate_tokens(stringify_json(tool.get("parameters", {})))
    return total


class PromptRegions(NamedTuple):
    """A rendered prompt cut into who-wrote-what, sized so the parts add up to the whole.

    The panel needs the row to reconcile: its own contribution, the user's project files, and
    "Kimi's own framing" must sum to the measured total. The framing figure is therefore derived by
    subtraction rather than by pricing the prefix text on its own, because each part rounds up
    independently and three rounded-up parts overshoot a rounded-up whole by a token or two.
    :attr:`framing_estimate` is the independent figure, kept so the two can be seen to agree to
    within that rounding.
    """

    prefix: str
    harness: str
    project: str

    @property
    def tokens(self) -> dict[str, int]:
        harness = estimate_tokens(self.harness)
        project = estimate_tokens(self.project)
        total = estimate_tokens(self.prefix + self.harness + self.project)
        return {
            "total": total,
            "harness": harness,
            "project": project,
            "framing": total - harness - project,
        }

    @property
    def framing_estimate(self) -> int:
        """The prefix priced on its own, for cross-checking :attr:`tokens`."""
        return estimate_tokens(self.prefix)


def split_regions(prompt: str, harness_path: str = HARNESS_AGENTS_PATH) -> PromptRegions:
    """Cut a rendered prompt at the ``<!-- From: path -->`` markers Kimi writes around AGENTS.md.

    Everything before the first marker is Kimi's own framing - the built-in prompt, the tool
    rules, the environment block, and the workspace listing. Each marker opens one project or
    harness document, and the harness's is the one it staged into the agent home. A region keeps
    its own marker line: Kimi emits it, but the model is charged for it, and attributing it to
    nothing would make the boxes understate what they cost.
    """
    starts = [m.start() for m in re.finditer(re.escape(PART_MARKER), prompt)]
    if not starts:
        return PromptRegions(prompt, "", "")
    head = prompt[: starts[0]]
    harness: list[str] = []
    project: list[str] = []
    edges = starts + [len(prompt)]
    for index in range(len(starts)):
        body = prompt[edges[index] : edges[index + 1]]
        end = body.find(PART_MARKER_END, len(PART_MARKER))
        path = body[len(PART_MARKER) : end] if end != -1 else ""
        (harness if path.rstrip("/") == harness_path else project).append(body)
    return PromptRegions(head, "".join(harness), "".join(project))


def lane_audiences(plan: dict[str, Any]) -> dict[str, str]:
    """Map a model alias to the audience that sees it, from the resolved plan.

    Read off the plan rather than hard-coded, because the operator chooses which models fill which
    lanes and two lanes can legitimately share a model.
    """
    aliases: dict[str, str] = {}
    for name, lane in plan.get("lanes", {}).items():
        audience = AUDIENCE_SUBAGENT if name == "subagent" else AUDIENCE_MAIN
        aliases[str(lane.get("alias", ""))] = audience
    return aliases


def bind_records(path: Path) -> list[dict[str, Any]]:
    """Every ``profile.bind`` record in one agent's wire log, oldest first.

    Malformed lines are skipped, not fatal: this reads a live log that the running UI is appending
    to, so a torn final line is a normal thing to find.
    """
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in text.splitlines():
        if f'"{BIND_TYPE}"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == BIND_TYPE and isinstance(record.get("systemPrompt"), str):
            records.append(record)
    return records


def newest_binds(root: Path, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The latest prompt each audience rendered anywhere under ``root``, keyed by audience."""
    audiences = lane_audiences(plan)
    found: dict[str, dict[str, Any]] = {}
    for wire in sorted(root.glob(WIRE_GLOB)) or sorted(root.rglob(WIRE_NAME)):
        for record in bind_records(wire):
            audience = audiences.get(str(record.get("modelAlias", "")))
            if audience is None:
                continue
            current = found.get(audience)
            if current is None or int(record.get("time", 0)) >= int(current.get("time", 0)):
                found[audience] = record
    return found


def newest_tools(root: Path) -> dict[str, dict[str, int]]:
    """The latest tool-schema set each agent sent, priced, keyed by agent id.

    Priced with :func:`estimate_tools_tokens`, which is Kimi's own per-field arithmetic, so the
    figure is the same kind of estimate as every other unmeasured number here rather than a
    different one. ``count`` is how many tools the schema set holds, which is what makes the
    figure legible: a large number with nothing to attach it to reads like a bug.
    """
    found: dict[str, dict[str, int]] = {}
    for wire in sorted(root.glob(WIRE_GLOB)) or sorted(root.rglob(WIRE_NAME)):
        agent = wire.parent.name
        for line in wire.read_text(errors="replace").splitlines():
            if f'"{TOOLS_TYPE}"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            tools = record.get("tools")
            # Absent, malformed, or genuinely empty all mean the same thing to a reader: there is
            # no schema set here to price. Sanitising happens here rather than inside
            # estimate_tools_tokens, which stays an exact mirror of Kimi's arithmetic.
            if not isinstance(tools, list):
                continue
            priced = [tool for tool in tools if isinstance(tool, Mapping)]
            if not priced:
                continue
            stamp = int(record.get("time", 0))
            current = found.get(agent)
            if current is not None and stamp < current["time"]:
                continue
            found[agent] = {
                "time": stamp,
                "count": len(priced),
                "tokens": estimate_tools_tokens(priced),
            }
    return found


def size_of(prompt: str, audience: str) -> dict[str, Any]:
    """The measurement the panel displays for one audience: bytes, tokens, and who owns what."""
    regions = split_regions(prompt).tokens
    return {
        "audience": audience,
        "bytes": len(prompt.encode("utf-8")),
        "characters": len(prompt),
        "tokens": regions["total"],
        "kimiFraming": regions["framing"],
        "harnessContract": regions["harness"],
        "projectInstructions": regions["project"],
    }


def measure(
    root: Path, plan: dict[str, Any], *, image: str = "", prefs: str = ""
) -> list[dict[str, Any]]:
    """One row per audience for the prompts under ``root``, oldest first.

    ``prefs`` is the serialised option set, recorded so a later reader can tell whether a smaller
    figure is a different harness or a different workspace. It is a label, not a cache key - this
    function never looks at what was recorded before.
    """
    rows = []
    binds = newest_binds(root, plan)
    schemas = newest_tools(root)
    for audience, record in sorted(binds.items()):
        alias = str(record.get("modelAlias", ""))
        row = size_of(record["systemPrompt"], audience)
        schemas_for = schemas.get(str(record.get("agentId", ""))) or {}
        row.update(
            {
                "toolSchemaTokens": schemas_for.get("tokens", 0),
                "toolSchemaCount": schemas_for.get("count", 0),
            }
        )
        row.update(
            {
                "time": int(record.get("time", 0)),
                "when": datetime.fromtimestamp(
                    int(record.get("time", 0)) / 1000, UTC
                ).isoformat(timespec="seconds"),
                "modelAlias": alias,
                "inputCap": cap_for_alias(plan, alias),
                "profileName": record.get("profileName"),
                "image": image,
                "options": prefs,
            }
        )
        rows.append(row)
    return rows


def read_history(path: Path) -> list[dict[str, Any]]:
    """Every measurement this machine has ever taken, oldest first. Absent history is normal."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("audience") in (AUDIENCE_MAIN, AUDIENCE_SUBAGENT):
            rows.append(row)
    return rows


def latest_by_audience(history: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    newest: dict[str, dict[str, Any]] = {}
    for row in history:
        audience = str(row.get("audience", ""))
        current = newest.get(audience)
        if current is None or int(row.get("time", 0)) >= int(current.get("time", 0)):
            newest[audience] = row
    return newest


def age_minutes(row: Mapping[str, Any], now: datetime | None = None) -> float:
    """How stale a measurement is, because "measured" has to mean "measured recently"."""
    moment = datetime.fromtimestamp(int(row.get("time", 0)) / 1000, UTC)
    return ((now or datetime.now(UTC)) - moment).total_seconds() / 60.0


def append_history(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append, never rewrite. The file is history the panel reads ages from, not state it derives.

    Mode ``0600`` because the rows sit beside the panel's prefs in a directory that also held
    rendered provider configuration during the same launch.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, sort_keys=False) + "\n")
    if not existed:
        path.chmod(0o600)


def input_cap(plan: dict[str, Any], lane: str) -> int | None:
    """The denominator for a row: the lane's *input cap*, never its window.

    The cap is what actually bounds a request, because the proxy clamps the output budget out of
    the same reservation and each lane's cap therefore sits below its own window. Quoting windows
    would understate pressure on the busiest lane by a quarter.
    """
    entry = plan.get("lanes", {}).get(lane)
    if entry is None:
        return None
    return int(entry["input_tokens"])


def cap_for_alias(plan: dict[str, Any], alias: str) -> int | None:
    """Resolve a measured prompt's own lane, so the percentage is against the cap it used.

    This is why the long lane needs no third row in the diagram. It is the *main* audience on a
    different denominator, and the ``profile.bind`` record says which alias was actually bound, so
    picking the denominator from the record is both exact and free of new branching.
    """
    for lane in plan.get("lanes", {}).values():
        if lane.get("alias") == alias:
            return int(lane["input_tokens"])
    return None


def caps(plan: dict[str, Any]) -> dict[str, int]:
    """Every lane's cap, for the legend: the operator needs to see the long lane even when today's
    session never used it."""
    return {name: int(lane["input_tokens"]) for name, lane in plan.get("lanes", {}).items()}


def residual_placeholders(prompt: str) -> list[str]:
    """Names Kimi was never given a value for, which it ships to the model as literal text.

    Code spans are excluded, by the same rule the placeholder gate uses: documentation about a
    name is not a use of it. Without that, this harness's own contract text - which explains
    ``${base_prompt}`` to the operator - would make every live report claim a leak.
    """
    return sorted(set(re.findall(r"\$\{([A-Za-z0-9_.]+)", pc.outside_code(prompt))))


def report(
    root: Path,
    plan: dict[str, Any],
    history: Path | None = None,
    out=sys.stdout,
    now: datetime | None = None,
) -> int:
    """Print the prompt each audience actually received, beside the last number we predicted.

    This is the read-only half of the module and the reason ``./prompts.sh --live`` can answer
    "what is in my context window right now" without a history file: the history only supplies the
    delta, and its absence is reported rather than guessed at.
    """
    binds = newest_binds(root, plan)
    if not binds:
        print(f"no {BIND_TYPE} records under {root}", file=sys.stderr)
        return 1
    previous = latest_by_audience(read_history(history)) if history else {}
    schemas = newest_tools(root)
    for audience, record in sorted(binds.items()):
        row = size_of(record["systemPrompt"], audience)
        alias = str(record.get("modelAlias", ""))
        cap = cap_for_alias(plan, alias)
        print(
            f"{audience}  profile={record.get('profileName') or '?'}  model={alias or '?'}",
            file=out,
        )
        print(
            f"  {row['tokens']:,} tokens in {row['bytes']:,} bytes:"
            f" framing {row['kimiFraming']:,},"
            f" harness {row['harnessContract']:,},"
            f" project {row['projectInstructions']:,}",
            file=out,
        )
        if cap:
            print(
                f"  {100.0 * row['tokens'] / cap:.1f}% of the {cap:,}-token input cap",
                file=out,
            )
        # Reported outside the sum on purpose: the schemas ride beside the prompt, so adding them
        # to the figure above would break the one property that makes the split trustworthy.
        schemas_for = schemas.get(str(record.get("agentId", "")))
        if schemas_for:
            print(
                f"  plus {schemas_for['tokens']:,} estimated tokens of tool schemas"
                f" ({schemas_for['count']} tools), beside the prompt and not clearable",
                file=out,
            )
        residue = residual_placeholders(record["systemPrompt"])
        print("  unsubstituted: " + (", ".join(residue) if residue else "none"), file=out)
        earlier = previous.get(audience)
        if earlier is None:
            print("  previous measurement: none recorded on this machine", file=out)
        else:
            before = int(earlier.get("tokens", 0))
            print(
                f"  previous measurement: {before:,} tokens"
                f" {age_minutes(earlier, now):.0f} min ago,"
                f" delta {row['tokens'] - before:+,}",
                file=out,
            )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    summary = (__doc__ or "").splitlines()[0]
    parser = argparse.ArgumentParser(prog="tools/prompt_measure.py", description=summary)
    parser.add_argument("--sessions-dir", type=Path, required=True,
                        help="A directory containing the agent home copied out of the container.")
    parser.add_argument("--plan", type=Path, required=True, help="The resolved policy plan JSON.")
    parser.add_argument("--out", type=Path,
                        help="JSONL history to append one row to; required unless --report.")
    parser.add_argument("--history", type=Path,
                        help="Recorded measurements to compare against, for --report.")
    parser.add_argument("--image", default="", help="Image digest the prompts came from.")
    parser.add_argument("--prefs", default="", help="Serialised option set, recorded as a label.")
    parser.add_argument("--report", action="store_true",
                        help="Print what each audience received and write nothing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Record what this session actually measured. Always exits 0 when it can open the file.

    A measurement that never lands must never fail a launch, so every error is reported on the
    log the caller redirected and swallowed. The exit code stays zero even on failure; only a bad
    command line is a real error. ``--report`` is the exception: it is a question an operator asked
    on purpose, so a missing record is a real answer with a real exit code.
    """
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.report:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        return report(args.sessions_dir, plan, args.history)
    if args.out is None:
        print("write mode needs --out (or ask for --report instead)", file=sys.stderr)
        return 2
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        rows = measure(args.sessions_dir, plan, image=args.image, prefs=args.prefs)
        if not rows:
            print(f"no {BIND_TYPE} records yet under {args.sessions_dir}", file=sys.stderr)
            return 0
        append_history(args.out, rows)
        for row in rows:
            print(f"measured {row['audience']} {row['tokens']} tokens ({row['bytes']} bytes)",
                  file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - a missing measurement is never a failed launch
        print(f"prompt measurement skipped: {type(error).__name__}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
