#!/usr/bin/env python3
"""Draw the context this session will carry, and ask which of it the operator wants.

This is the trust mechanism, not a wizard. It runs on every launch, it is the only place the
resolved graph is ever visible at once, and it is the only way to choose which dynamic blocks the
harness adds.

Two halves, deliberately different in kind. The **diagram** is static - the ``.md`` documents that
exist and how they nest. Nothing there is interactive, because the operator cannot edit a file from
this screen and pretending otherwise would invite them to try. The **checklist** below it is
dynamic: every add-on this harness manages, each priced, each togglable, all of them on by
default, and the previous launch's choices remembered.

Figures are token counts, right-aligned on one shared axis, because the point of showing them is
to let the operator see how much of the window is already spoken for before they type anything.
The boxes in each row always sum to that row's total, which is what makes the residual - Kimi's own
framing - visible as a number instead of an excuse. A ``?`` means this harness could not price the
box honestly rather than that it guessed; see :func:`static_rows`.

Nothing in here is a measurement of a provider's tokenizer. The exact figures come from a real
``profile.bind`` record, the rest from :func:`prompt_measure.estimate_tokens`, which is Kimi's own
character heuristic. ``--plain`` and the no-terminal path render through the same function as the
interactive screen, so an unattended launch prints precisely what the operator would have seen.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

if __package__:
    from . import kimi_prompts, policy
    from . import prompt_context as pc
    from . import prompt_measure as pm
else:
    import kimi_prompts
    import policy
    import prompt_context as pc
    import prompt_measure as pm

WIDTH = 78
#: The reference table's name column, its gutter, and therefore where its notes have to start.
NAME_FIELD = 28
NOTE_FIELD = 2 + NAME_FIELD + 1
THIN = "-" * WIDTH
RULE = "-" * (WIDTH - 4)
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"

#: Two columns at the right edge. The number holds a measured seven-figure figure with its tilde,
#: the percentage holds the widest true share a lane can produce, and neither ever moves.
NUMBER_FIELD = 9
PCT_FIELD = 7
#: A number jammed against a label reads as part of its name, so the gap has a floor.
MIN_GAP = 3
#: A companion pair left half-selected is not wrong, just probably unintended, so it earns one
#: quiet sentence and no refusal: the operator's choice always stands.
WARN = YELLOW

STYLE_CODES = {"dim": DIM, "bold": BOLD, "warn": WARN, "on": GREEN}

HELP = """\
Keys - type one and press Enter, the terminal is line-buffered
  <n>          toggle option <n>            a  everything on
  1..9         toggle that option           n  everything off: tabula rasa
  i <n>        where does that text come from
  r            back to this build's defaults
  Enter        accept, remember, and continue
  Ctrl-C       abort this launch (nothing is started, nothing is written)

To speak to the model with nothing hoisted in front of you: press n here, and make both of these
files exist and be empty --
  SYSTEM.md    CONTEXT.md
An empty file is a decision and is honoured as one; an absent file is not. A file holding only
whitespace counts as empty. Kimi falls back to its own built-in prompt when it is handed a prompt
that trims to nothing, so the harness sends a single period instead, which is the closest reachable
thing to no prompt at all.
"""

#: What the last line of a row means, and why the boxes above it add up to it.
KEY = """\
The boxes in each row are the whole prompt that row names, cut into who wrote it: this harness,
your workspace, and Kimi's own framing around both - which includes the system prompt this harness
stages, since Kimi puts it ahead of the AGENTS.md documents. Measured figures come from one real
request that this workspace actually made. An estimate is marked ~ and is the harness's own text
priced before Kimi has rendered anything; Kimi's framing cannot be estimated without having been
seen, so a first launch shows ? and the whole figure with it.
"""


class Cost(NamedTuple):
    """One priced thing, and how honest that price is allowed to claim to be.

    ``estimated`` rather than ``not measured`` because the third state - unpriceable - is real and
    renders as ``?``, and a boolean would have to lie about it.
    """

    tokens: int
    measured: bool = False
    priceable: bool = True

    def text(self) -> str:
        if not self.priceable:
            return "?"
        return f"{self.tokens:,}" if self.measured else f"~{self.tokens:,}"


#: Something that could not be priced at all, spelled once. Not ``Cost(0)``: zero is a figure the
#: operator could act on, and this is one they cannot.
UNKNOWN = Cost(0, priceable=False)


class Pieces(NamedTuple):
    """Everything the diagram needs, computed once per render from one set of choices."""

    system: str
    agents: str
    project: str
    framing: Cost
    system_cost: Cost
    agents_cost: Cost
    project_cost: Cost
    total: Cost
    age: str
    cap: int | None
    measured: bool = False



def colour(text: str, code: str, on: bool) -> str:
    return f"{code}{text}{RESET}" if on and code else text


def pad(label: str, value: str, width: int) -> str:
    """Right-align `value` inside `width` columns, with a guaranteed gap after the label."""
    if not value:
        return label
    room = max(1, width - len(value))
    gap = " " * max(MIN_GAP, room - len(label))
    return f"{label}{gap}{value}".rstrip()


def tail(value: str, pct: str) -> str:
    """The two numeric columns, joined: number, gutter, percentage.

    Built as one string only at the end, so `pad` can keep its single-axis contract and the test for
    the alignment of the axis is a test of one function rather than of the whole screen.
    """
    if not value and not pct:
        return ""
    number = pad("", value, NUMBER_FIELD) if value else ""
    if not number:
        return "" if not pct else pad("", pct, PCT_FIELD)
    return number + pad("", pct, PCT_FIELD)


def percent(tokens: int, cap: int | None) -> str:
    """The share of the lane's input cap this figure would spend, as its own aligned column.

    A parenthetical glued to the number would make the numbers ragged, and reading them down the
    column is the only thing they are on the screen for.
    """
    if not cap:
        return ""
    return f"{tokens / cap * 100:.1f}%"


class Screen:
    """Lines whose two numeric fields are right-aligned after the fact, across the whole screen.

    Alignment has to belong to the finished screen rather than to each row, or the diagram and the
    checklist would each carry their own column and the eye could not compare a cost in one half of
    the panel with a cost in the other. It also has to be two columns and not one string, because a
    percentage hung off the end of a number makes the numbers themselves ragged - and comparing
    numbers is the entire reason they are on the screen.
    """

    def __init__(self, colour_on: bool) -> None:
        self.lines: list[tuple[str, str, str, str]] = []
        self.colour_on = colour_on

    def row(self, label: str, value: str = "", pct: str = "", style: str = "") -> None:
        """Queue one line. A label carrying its own paragraph becomes that many lines.

        Each physical line is styled separately, because an escape opened on the first line of a
        wrapped note would otherwise leak its attributes into every line after it and leave the
        terminal dimmed for the rest of the session.
        """
        if "\n" in label:
            # A paragraph note: reflow it into one logical line and let render() re-break it to the
            # panel width, rather than shipping the author's breaks through as a column of
            # fragments. The collapse is unconditional here, so a leading indent is expected to go.
            label = " ".join(label.split())
        self.lines.append((label, value, pct, style))

    def rows(self, rows: Iterable[tuple[str, str, str]]) -> None:
        for label, value, style in rows:
            text, _, pct = value.partition("|")
            self.row(label, text, pct, style)

    def render(self) -> list[str]:
        """One shared axis. A label too long for it wraps instead of pushing the axis over.

        The percentage column is reserved for every numeric row or for none of them, decided from
        the whole screen. Reserving it per row would leave the diagram's numbers seven columns to
        the right of the checklist's, which is the drift the single Screen exists to prevent.
        """
        reserve = PCT_FIELD if any(pct for _, _, pct, _ in self.lines) else 0
        out = []
        for label, value, pct, style in self.lines:
            code = STYLE_CODES.get(style, "")
            numbers = tail(value, pct if reserve else "")
            room = WIDTH - (len(numbers) + MIN_GAP if numbers else 0)
            head = label
            if len(label) > room:
                # textwrap() strips leading whitespace, which would pull an indented option back to
                # column zero and destroy the only structure the checklist has.
                indent = label[: len(label) - len(label.lstrip())]
                wrapped = textwrap.wrap(
                    label.strip(),
                    width=max(8, room),
                    initial_indent=indent,
                    subsequent_indent=indent + "    ",
                ) or [indent]
                head = wrapped[-1]
                for part in wrapped[:-1]:
                    out.append(colour(part, code, self.colour_on))
            line = pad(head, numbers, WIDTH) if numbers else head
            out.append(colour(line, code, self.colour_on))
        return out


def figure(cost: Cost, cap: int | None) -> str:
    """One row's numeric tail: its own text, then its share of the cap, in the two columns."""
    if not cost.priceable:
        return "?|"
    return f"{cost.text()}|{percent(cost.tokens, cap)}"


def option_text(option: pc.Option, plan: dict[str, Any], module_guidance: str) -> str:
    """The exact bytes this option is responsible for, or ``""`` if it adds no prompt text.

    Dispatch is by option id and not by target, because siblings that share a document are priced
    by different means: the usage-limits block is generated text, module guidance is copied text,
    and a config or environment switch writes nothing into the prompt at all.
    """
    if option.id == policy.OPTION_LANE_LIMITS:
        return _generated(policy.OPTION_LANE_LIMITS, plan)
    if option.id in (policy.OPTION_LANE_TABLE, policy.OPTION_PARALLELISM):
        return _generated(option.id, plan)
    if option.id == pc.OPTION_MODULE_GUIDANCE:
        return _module(module_guidance)
    return ""


def _generated(option: str, plan: dict[str, Any]) -> str:
    for audience in policy.GUIDANCE_AUDIENCES:
        for section in policy.guidance_sections(plan, audience):
            if section.option == option:
                return policy.guidance_block(section)
    return ""


def _module(module_guidance: str) -> str:
    return module_guidance.strip("\n")


def option_cost(option: pc.Option, plan: dict[str, Any], module_guidance: str) -> Cost:
    """Price one option on its own bytes. A switch that adds no text costs no tokens."""
    text = option_text(option, plan, module_guidance)
    if not text:
        return Cost(0, measured=True)
    return Cost(pm.estimate_tokens(text))


def _numbered(root: Path) -> str:
    """The workspace documents Kimi hoists itself, which this harness neither writes nor gates."""
    found = [root / "AGENTS.md", *sorted(root.glob("*/AGENTS.md"))]
    return "".join(
        f"{path.read_text(encoding='utf-8', errors='replace')}\n"
        for path in found
        if path.is_file()
    )


def _age(row: dict[str, Any], now: datetime | None = None) -> str:
    """How stale a measurement is, in the unit the operator would say it in.

    A measurement is only "exact" for the context it was taken in, so staleness is part of the
    figure rather than a footnote: a row from last week describes a workspace that may no longer
    exist.
    """
    minutes = pm.age_minutes(row, now)
    if minutes < 90:
        return f"{max(1, round(minutes))}m"
    return f"{round(minutes / 60)}h"


def pieces(
    plan: dict[str, Any],
    root: Path,
    module_guidance: str,
    enabled: dict[str, bool],
    measured: dict[str, Any] | None,
    lane: str,
    values: dict[str, str] | None = None,
    now: datetime | None = None,
) -> Pieces:
    """Cut one audience's prompt into the documents that make it, priced as honestly as it can.

    ``values`` is the mapping ``render_runtime`` will substitute, because pricing a different
    composition from the one that ships would make the panel's numbers fiction. Omit it where
    there is no cache to read, which leaves the harness's own values and nothing else.

    A measured row is exact and closes: Kimi's framing, the harness document, and the workspace
    documents are the whole prompt with nothing left over, because :func:`prompt_measure` derives
    the framing by subtraction. An unmeasured row prices what this harness generated and shows ``?``
    for what it did not, rather than inventing a framing figure and passing the sum off as real.

    The staged system prompt is not a fourth box. Kimi renders it ahead of the AGENTS.md documents,
    so it lives *inside* the framing region and the measured ``kimiFraming`` figure already
    contains it. The unmeasured row therefore shows the system prompt as its own estimate plus an
    unknown remainder, rather than double-counting the same text.
    """
    agents = pc.compose_agents_document(root, plan, module_guidance, enabled, values)
    system = pc.compose_system_document(root, plan, enabled, values)
    # Kept whatever the measurement history says: the token cost is measured, but Kimi's instruction
    # warning counts *bytes*, and no token figure can be converted back into the text it came from.
    # Dropping it here would silently remove the workspace's own documents from that bill.
    project = _numbered(root)
    # Priced from the bytes that will actually be staged, whatever the measurement history says, so
    # a module or option that costs nothing here genuinely contributes nothing to the prompt.
    if measured is None:
        return Pieces(
            system=system,
            agents=agents,
            project=project,
            framing=UNKNOWN,
            system_cost=Cost(pm.estimate_tokens(system)),
            agents_cost=Cost(pm.estimate_tokens(agents)),
            project_cost=Cost(pm.estimate_tokens(project)),
            total=UNKNOWN,
            age="",
            cap=pm.input_cap(plan, lane),
        )
    return Pieces(
        system=system,
        agents=agents,
        project=project,
        # No framing box on a measured row: the measured figure already contains it, so drawing a
        # second one would double-count. Subtracting our own estimate from a measured total to
        # produce a number labelled "measured" would be the worse lie.
        framing=UNKNOWN,
        system_cost=Cost(int(measured["kimiFraming"]), measured=True),
        agents_cost=Cost(int(measured["harnessContract"]), measured=True),
        project_cost=Cost(int(measured["projectInstructions"]), measured=True),
        total=Cost(int(measured["tokens"]), measured=True),
        age=_age(measured),
        cap=pm.cap_for_alias(plan, str(measured.get("modelAlias", ""))) or pm.input_cap(plan, lane),
        measured=True,
    )


def _source(root: Path, staged: str) -> str:
    """Name the file a document actually came from, including when it came from nowhere."""
    if staged == "context":
        source = pc.context_source(root)
    else:
        source = pc.system_source(root)
    if source is None:
        return "none: absent, so the built-in default applies"
    return str(source.relative_to(root)) if source.is_relative_to(root) else str(source)


def _where(note: str | None) -> str:
    """Label a path as a source, and leave a prose note as prose."""
    if not note:
        return ""
    return f"from {note}" if ("/" in note or note.endswith(".md")) else note


def static_rows(
    main: Pieces, sub: Pieces, plan: dict[str, Any], root: Path
) -> list[tuple[str, str, str]]:
    """The diagram: who sees what, what it costs, and which document it came from.

    Absence is never drawn as a box. A file that does not exist is described by the box that
    replaced it, and the synthesised ``${base_prompt}`` wrapper - a mechanism with no bytes of its
    own, whose rendered length is exactly Kimi's built-in prompt - gets no row at all.
    """
    rows: list[tuple[str, str, str]] = [
        (
            "STATIC CONTEXT - what these files put in front of the model",
            "tokens %cap",
            "bold",
        ),
        (THIN, "", "dim"),
    ]
    rows.extend(_audience("MAIN AGENT", main, _source(root, "system"), _source(root, "context")))
    rows.append(("", "", ""))
    rows.extend(_audience("SUBAGENT", sub, None, _source(root, "context")))
    # `None` above is the whole statement that a subagent sees no system prompt; see _audience.
    rows.append(("", "", ""))
    rows.append(("Lane caps, the denominator for every percentage above", "", "bold"))
    for name, cap in sorted(pm.caps(plan).items()):
        lane = plan["lanes"][name]
        audience = "subagents" if name == "subagent" else "the main agent"
        rows.append((f"  {name} lane ({audience})", "", "bold"))
        rows.append((f"      window {lane['context_tokens']:,}", f"cap {cap:,}", "dim"))
    rows.append((KEY, "", "dim"))
    return rows


def _audience(
    title: str, used: Pieces, system_from: str | None, context_from: str
) -> list[tuple[str, str, str]]:
    """One row of the diagram: the boxes that add up to it, then the whole figure.

    The boxes are the three regions :func:`prompt_measure.split_regions` cuts a real prompt into,
    so a measured row sums exactly. There are never four: Kimi renders the staged system prompt
    ahead of the AGENTS.md documents, so it is inside the first region and naming it twice would
    inflate the row past its own total.
    """
    shared = [
        ("harness context, staged as AGENTS.md", used.agents_cost, _where(context_from)),
        ("the workspace's own AGENTS.md files", used.project_cost, "yours, which Kimi reads alone"),
    ]
    # A subagent gets no system prompt - Kimi's own design, not a switch this harness offers - so
    # the box is absent rather than zero. Drawing it at 0 would imply a mechanism to turn off.
    if system_from is None:
        boxes = list(shared)
    elif used.measured:
        boxes = [("the system prompt, plus Kimi's framing", used.system_cost, _where(system_from))]
        boxes += shared
    else:
        boxes = [("the staged system prompt", used.system_cost, _where(system_from))]
        boxes += shared
        boxes.append(("Kimi's framing around it all", used.framing, "not estimable; see the key"))
    rows: list[tuple[str, str, str]] = [(title + " -", "", "bold")]
    for name, cost, note in boxes:
        rows.append((f"  {name}", figure(cost, used.cap), ""))
        if note:
            rows.append((f"      {note}", "", "dim"))
    rows.append(("  " + RULE, "", "dim"))
    if used.measured:
        rows.append(("  the whole prompt, on every request", figure(used.total, used.cap), "bold"))
        rows.append((f"  measured from your last real request, {used.age} ago", "", "dim"))
    else:
        rows.append(("  the whole prompt cannot be totalled: see the ? above", "", "bold"))
        rows.append(("  launch once and this whole row becomes exact", "", "dim"))
    return rows


def checklist(
    enabled: dict[str, bool], plan: dict[str, Any], module_guidance: str
) -> list[tuple[str, str, str]]:
    """Every dynamic add-on, one line each, flat, in panel order, siblings adjacent."""
    rows: list[tuple[str, str, str]] = [
        ("", "", ""),
        ("DYNAMIC CONTEXT THIS HARNESS ADDS", "tokens", "bold"),
        ("Each price is that block alone, not a measurement of a rendered prompt.", "", "dim"),
    ]
    note = warning(enabled)
    if note:
        rows.append(("  " + note, "", "warn"))
    rows.append(("", "", ""))
    for index, option in enumerate(pc.OPTIONS, start=1):
        on = enabled[option.id]
        cost = option_cost(option, plan, module_guidance)
        value = "no prompt cost" if cost.measured and not cost.tokens else cost.text()
        mark = "x" if on else " "
        style = "on" if on else "dim"
        rows.append((f" [{mark}] {index}. {option.label}", value, style))
        rows.append((f"       into {option.target}: {option.summary}", "", "dim"))
    rows.append(("", "", ""))
    return rows


def lonely_pairs(enabled: dict[str, bool]) -> list[pc.Option]:
    """Options that are on while a companion of theirs is off.

    Off-on is lonely but on-off is not warned about twice: the pair gets one sentence, naming the
    side the operator left switched on, because that is the one whose text will look incomplete.
    """
    return [
        option
        for option in pc.OPTIONS
        if enabled.get(option.id)
        and option.companions
        and not all(enabled.get(other) for other in option.companions)
    ]


def warning(enabled: dict[str, bool]) -> str:
    """One sentence when a pair is half-selected. A suggestion, never a refusal or a block."""
    lonely = lonely_pairs(enabled)
    if not lonely:
        return ""
    names = ", ".join(str(pc.OPTIONS.index(o) + 1) for o in lonely)
    plural = "s" if len(lonely) > 1 else ""
    return (
        f"{len(lonely)} option{plural} may not work as expected, because "
        f"{'they are' if len(lonely) > 1 else 'it is'} paired with a sibling you have switched off "
        f"(number{plural} {names}) - each one still applies, it will just read as though part of "
        "it is missing"
    )


def explain(choice: str, plan: dict[str, Any], module_guidance: str) -> str:
    """Where one option's bytes are written, so the screen is not the only place to learn that."""
    index = int(choice) - 1 if choice.isdigit() else -1
    if not 0 <= index < len(pc.OPTIONS):
        return "Type i followed by a number to see where that option's text comes from."
    option = pc.OPTIONS[index]
    text = option_text(option, plan, module_guidance)
    origin = {
        policy.OPTION_LANE_LIMITS: "tools/policy.py _lane_limit_lines",
        policy.OPTION_LANE_TABLE: "tools/policy.py _lane_table_lines",
        policy.OPTION_PARALLELISM: "tools/policy.py _parallelism_lines",
        pc.OPTION_MODULE_GUIDANCE: "the modules' own guidance, staged by tools/modules.py",
    }.get(option.id, f"{option.target}: a switch, not a block of text")
    return (
        f"{option.label}\n"
        f"  written by  {origin}\n"
        f"  reaches     {option.target}\n"
        f"  priced      {option_cost(option, plan, module_guidance).text()} tokens, its own text\n"
        f"  paired with {', '.join(option.companions) or 'nothing'}\n"
        f"  this launch {text and f'{len(text)} characters' or 'adds no text'}\n"
    )


def draw(
    plan: dict[str, Any],
    root: Path,
    module_guidance: str,
    enabled: dict[str, bool],
    latest: dict[str, dict[str, Any]],
    *,
    values: dict[str, str] | None = None,
    colour_on: bool,
    remembered: bool,
    stale: Sequence[str] = (),
) -> str:
    """One screen, both halves, one alignment pass. Every path through the panel prints this."""
    main = pieces(plan, root, module_guidance, enabled,
                  latest.get(pm.AUDIENCE_MAIN), "primary", values)
    sub = pieces(plan, root, module_guidance, enabled,
                 latest.get(pm.AUDIENCE_SUBAGENT), "subagent", values)
    screen = Screen(colour_on)
    screen.rows(static_rows(main, sub, plan, root))
    screen.rows(checklist(enabled, plan, module_guidance))
    state = "remembered from your last launch" if remembered else "this build's defaults"
    shown = f"Showing {state}. Enter accepts; nothing is written or started until you do."
    screen.row(shown, style="dim")
    screen.row("Press ? for the keys and how to reach a completely blank prompt.", style="dim")
    for notice in over_limit(main) + tuple(stale):
        screen.row(notice, style="warn")
    return "\n".join(screen.render())


def over_limit(main: Pieces) -> tuple[str, ...]:
    """Kimi's own oversized-instruction warning, quoted before it fires on a live load.

    Kimi counts the ``AGENTS.md`` files it hoists, which is the staged harness contract plus the
    workspace's own - not the system prompt, and not whatever the model does with them afterwards.
    It warns past 32 KB and truncates nothing, so this is a bill, not an error: the number is here
    so the operator knows the bill exists rather than reading it in a load warning later.
    """
    total = pc.instruction_bytes(main.agents, main.project)
    notice = pc.over_instruction_limit(total)
    return (notice,) if notice else ()


def read_line(prompt: str) -> str | None:
    """One command line, or ``None`` at end-of-input.

    The terminal is line-buffered, so a key needs Enter after it; the prompt says so and the help
    screen repeats it, because pretending to be character-addressable here would mean either a
    ``termios`` dependency or a screen that lies about what it accepted.

    End-of-input is a degraded terminal rather than a decision, so the caller treats it exactly
    like the unattended path: keep what is already remembered and continue. Ctrl-C is deliberately
    left alone - it has to reach ``start.sh``'s trap, which is what releases the instance lock.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except EOFError:
        return None
    if not line:
        return None
    return line.strip()


def apply_command(command: str, enabled: dict[str, bool]) -> tuple[dict[str, bool], bool]:
    """New selection, and whether the command ends the interaction."""
    if command == "":
        return enabled, True
    lowered = command.lower()
    if lowered == "a":
        return dict(pc.DEFAULT_ENABLED), False
    if lowered == "n":
        return dict.fromkeys(pc.OPTION_IDS, False), False
    if lowered == "r":
        return dict(pc.DEFAULT_ENABLED), False
    if lowered.isdigit():
        return toggle(enabled, int(lowered))
    return enabled, False


def toggle(enabled: dict[str, bool], one_based: int) -> tuple[dict[str, bool], bool]:
    index = one_based - 1
    if not 0 <= index < len(pc.OPTIONS):
        return enabled, False
    option = pc.OPTIONS[index].id
    return {**enabled, option: not enabled[option]}, False


def load_context(runtime_dir: Path) -> tuple[dict[str, Any], dict[str, bool], bool]:
    """The resolved plan plus whatever the operator chose last time.

    The plan has to exist: the panel is drawn after the version and model selectors and before
    ``render_runtime.py``, which is what writes it, so a missing file means the launch is out of
    order rather than that the operator has no preference yet.
    """
    plan_path = runtime_dir / "model-policy.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"the panel needs the resolved policy plan: {error}") from error
    # A readable file is not a usable plan, and the difference matters here: every figure the panel
    # prints is a lane's input cap, so a plan without lanes would price nothing at all. Fail with
    # the sentence rather than the KeyError an operator would otherwise get at the bottom of a
    # startup they are watching for the first time.
    if not isinstance(plan, dict) or not isinstance(plan.get("lanes"), dict) or not plan["lanes"]:
        raise SystemExit(
            f"the panel needs the resolved policy plan: {plan_path} names no lanes,"
            " so there is nothing to price"
        )
    remembered = (runtime_dir / pc.PREFS_FILE).is_file()
    return plan, pc.load_prefs(runtime_dir / pc.PREFS_FILE), remembered


def read_module_guidance(runtime_dir: Path) -> str:
    path = runtime_dir / "module-guidance.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def read_latest(runtime_dir: Path) -> dict[str, dict[str, Any]]:
    return pm.latest_by_audience(pm.read_history(runtime_dir / pc.MEASUREMENTS_FILE))


def _yes(value: bool | None) -> str:
    """One cell of the reference table.

    Three states because the question has three honest answers: the built-in prompt has not been
    read yet, and the built-in prompt is the thing being asked about, are different answers and
    neither is "no".
    """
    if value is None:
        return "?"
    return "yes" if value else "no"


def _used(text_: str, name: str) -> bool:
    """Whether a prompt body actually asks for this variable."""
    return "${" + name + "}" in text_


def _note(label: str, body: str) -> list[str]:
    """One explanation, hanging-indented so a wrapped note still reads as belonging to its name.

    The label column *is* the first line's indent, so only the continuation lines carry one;
    indenting both puts the text at twice the column and wraps it into a ribbon. Both halves use
    NOTE_FIELD, which is the table's own name column plus its gutter, or the notes would sit two
    columns away from the names they explain.
    """
    # Both indents go to textwrap, or the first line runs to twice the width of the rest: wrap()
    # counts the indent it is given as part of the line, and the label is that indent.
    pad = " " * NOTE_FIELD
    wrapped = textwrap.wrap(
        body, width=WIDTH, initial_indent=pad, subsequent_indent=pad
    ) or [""]
    return [("  " + label).ljust(NOTE_FIELD) + wrapped[0][NOTE_FIELD:], *wrapped[1:]]


def _paragraph(body: str) -> list[str]:
    """Prose with no name in front of it, wrapped to the same right edge as everything else."""
    return textwrap.wrap(body, width=WIDTH) or [""]


def owned_tables(root: Path) -> list[str]:
    """The operator's own config tables, named from the policy file rather than from memory."""
    try:
        document = json.loads((root / "runtime" / "config-policy.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ["runtime/config-policy.json could not be read, so nothing here is known about"]
    keys = document.get("user_owned")
    names = ", ".join(f"[{key}]" for key in keys) if isinstance(keys, list) and keys else "none"
    return _paragraph(
        f"{names} are yours alone. The initializer re-pins every other table on every launch, so "
        "a panel option that seemed to change one of these would be lying about what it did."
    )


def vars_report(runtime_dir: Path, root: Path) -> str:
    """Every name a prompt file may hold, who resolves it, and when it actually carries text.

    Both "carries" columns are computed - one from the built-in prompt extracted from this image,
    one from the files the operator has - because a hand-kept table would drift from the build it
    claims to describe and still print with full confidence.
    """
    literals = kimi_prompts.load(runtime_dir)
    built_in = literals.get("kimi.system_default")
    yours = "".join(
        pc.strip_html_comments(path.read_text(encoding="utf-8"))
        for path in (pc.system_source(root), pc.context_source(root))
        if path is not None and path.is_file()
    )
    lines = [
        "Placeholders a prompt file may contain.",
        "",
        f"  {'name':<{NAME_FIELD}}{'resolved by':<12}{'in prompt':<11}in staged files",
    ]
    for name in pc.KIMI_PLACEHOLDERS:
        label = "${" + name + "}"
        lines.append(
            f"  {label:<{NAME_FIELD}}{'Kimi':<12}"
            f"{_yes(None if built_in is None else _used(built_in, name)):<11}"
            f"{_yes(_used(yours, name))}"
        )
    lines.append("")
    for name in ("base_prompt", *pc.HARNESS_PLACEHOLDERS, *pc.kimi_literal_names()):
        label = "${" + name + "}"
        who = "Kimi" if name == "base_prompt" else "harness"
        # Dash for all three groups, and not because the cache is cold: the built-in prompt *is*
        # the base, and it can never mention a name the harness invented. A "no" here would claim
        # the question was asked and answered.
        in_prompt = "-"
        lines.append(
            f"  {label:<{NAME_FIELD}}{who:<12}{in_prompt:<11}{_yes(_used(yours, name))}"
        )
    lines += ["", "What each of Kimi's own variables carries, in this build:"]
    for name, body in kimi_prompts.PLACEHOLDER_CONDITIONS.items():
        lines += _note("${" + name + "}", body)
    lines += ["", "What the harness resolves:"]
    lines += _note(
        "${base_prompt}",
        "Kimi's whole built-in prompt, and only for a template that mentions it, which is what "
        "lets a wrapper cost nothing",
    )
    for name in pc.HARNESS_PLACEHOLDERS:
        lines += _note(
            "${" + name + "}",
            "today's date, resolved at staging",
        )
    for name in pc.kimi_literal_names():
        lines += _note(
            "${" + name + "}",
            "read out of the running build"
            if name in literals
            else "pending: not cached for this image yet, so naming it stops the launch instead "
                 "of shipping the name itself",
        )
    for name in pc.DOCUMENTED_BUT_UNDEFINED:
        lines += _note(
            "${" + name + "}",
            "documented upstream and undefined here, so it reaches the model as literal text",
        )
    lines += ["", "Settings the harness reads and never rewrites:", *owned_tables(root)]
    return "\n".join(lines)



def run_configure(runtime_dir: Path, argv: list[str]) -> int:
    """``--configure`` is the scripted answer to the panel, so headless is not second-class.

    Deleting the opt-out environment variables left no non-interactive way to express a selection
    at all. This is that way, and it is also the only supported scriptable contract over the
    preference file, so it ships in the same change as the panel rather than after it.
    """
    parser = argparse.ArgumentParser(prog="./prompts.sh --configure")
    parser.add_argument("--enable", action="append", default=[], metavar="ID")
    parser.add_argument("--disable", action="append", default=[], metavar="ID")
    parser.add_argument("--all-on", action="store_true")
    parser.add_argument("--all-off", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    for name in (*args.enable, *args.disable):
        if name not in pc.OPTION_IDS:
            print(f"unknown option: {name}", file=sys.stderr)
            print(f"valid ids: {', '.join(pc.OPTION_IDS)}", file=sys.stderr)
            return 2
    path = runtime_dir / pc.PREFS_FILE
    enabled = pc.load_prefs(path)
    if args.all_on:
        enabled = dict(pc.DEFAULT_ENABLED)
    if args.all_off:
        enabled = dict.fromkeys(pc.OPTION_IDS, False)
    for name in args.enable:
        enabled[name] = True
    for name in args.disable:
        enabled[name] = False
    changed = args.enable or args.disable or args.all_on or args.all_off
    if args.show or not changed:
        for option in pc.OPTIONS:
            state = "on " if enabled[option.id] else "off"
            print(f"{state} {option.id:20} {option.label}")
        return 0
    pc.save_prefs(path, enabled)
    print(f"wrote {path}")
    for option in pc.OPTIONS:
        state = "on " if enabled[option.id] else "off"
        print(f"{state} {option.id:20} {option.label}")
    return 0


def interactive_loop(
    plan: dict[str, Any],
    root: Path,
    module_guidance: str,
    enabled: dict[str, bool],
    latest: dict[str, dict[str, Any]],
    *,
    values: dict[str, str] | None = None,
    colour_on: bool,
    remembered: bool,
    stale: Sequence[str] = (),
) -> dict[str, bool]:
    """Redraw, take one command, repeat. Enter accepts; end-of-input accepts too."""
    selection = dict(enabled)
    while True:
        print(draw(plan, root, module_guidance, selection, latest, values=values,
                   colour_on=colour_on, remembered=remembered, stale=stale))
        command = read_line("Enter accepts, 1-9 toggles, a/n/r, ? for help: ")
        if command is None:
            return selection
        lowered = command.lower()
        if lowered == "?":
            print(HELP)
            continue
        if lowered == "i" or lowered.startswith("i "):
            detail = command[1:].strip() or (read_line("which option? ") or "")
            print(explain(detail, plan, module_guidance))
            continue
        if lowered.isdigit() and len(lowered) > 1:
            print(explain(lowered, plan, module_guidance))
            continue
        selection, done = apply_command(command, selection)
        if done:
            return selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/prompt_panel.py")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plain", action="store_true", help="Never emit colour, never prompt.")
    parser.add_argument("--show", action="store_true", help="Draw once and exit.")
    parser.add_argument("--configure", action="store_true", help="Set choices without a screen.")
    parser.add_argument("--vars", action="store_true",
                        help="list every placeholder a prompt file may hold, and who resolves it")
    args, rest = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if args.configure:
        return run_configure(args.runtime_dir, rest)
    if args.vars:
        print(vars_report(args.runtime_dir, args.root))
        return 0

    plan, enabled, remembered = load_context(args.runtime_dir)
    module_guidance = read_module_guidance(args.runtime_dir)
    latest = read_latest(args.runtime_dir)
    values = kimi_prompts.substitutions(args.runtime_dir)
    # Staleness is a property of a live stack: the sidecar describes bytes that are mounted right
    # now. At startup this panel runs before render_runtime.py stages anything, so every file would
    # read as stale and the advice would be false - the very next step applies the edit. --show is
    # the inspect-a-running-session path, and that is where the comparison means something.
    stale = pc.stale_sources(args.root, args.runtime_dir) if args.show else []
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not (args.plain or args.show)
    if not interactive:
        # An unattended launch still prints the screen it is acting on, and still honours what was
        # remembered last time; a headless run is not a licence to change the operator's context.
        print(draw(plan, args.root, module_guidance, enabled, latest, values=values,
                   colour_on=False, remembered=remembered, stale=stale))
        return 0
    selection = interactive_loop(plan, args.root, module_guidance, enabled, latest,
                                 values=values, colour_on=True, remembered=remembered,
                                 stale=stale)
    pc.save_prefs(args.runtime_dir / pc.PREFS_FILE, selection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
