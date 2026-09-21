#!/usr/bin/env python3
"""Draw the context this session will carry, and ask which of it the operator wants.

This is the trust mechanism, not a wizard. It runs on every launch, it is the only place the
resolved graph is ever visible at once, and it is the only way to choose which blocks the harness
puts in front of the model.

Two renderers, one set of figures. In a terminal that can take a screen, a fullscreen modal
**tree**: the documents, the sources each one is read from, the add-ons, and a switch on every one
of them. Everywhere else - a log, a pipe, ``--plain`` - the same content as a flat **screen** of
rows, because an unattended launch still has to print what it is acting on. Neither renderer owns
a fact: both ask :func:`pieces` and :mod:`prompt_context` for the same bytes and the same prices, so
the two cannot disagree about a prompt neither of them wrote.

The tree holds the whole answer in one place, and it holds the *static* documents in it too, which
is the part a flat list could not do honestly. A block switched off there is a blank override for
this session only: no file is written, and no file is edited. Conversely, an empty ``SYSTEM.md`` or
``CONTEXT.md`` on disk already means "say nothing here", so the tree opens that block switched off,
and switching it on is how you ask to be ignored as if the file were absent.

Figures are token counts, right-aligned on one shared axis, because the point of showing them is
to let the operator see how much of the window is already spoken for before they type anything.
The boxes in each row always sum to that row's total, which is what makes the residual - Kimi's own
framing - visible as a number instead of an excuse. A ``?`` means this harness could not price the
box honestly rather than that it guessed; see :func:`static_rows`.

Nothing in here is a measurement of a provider's tokenizer. The exact figures come from a real
``profile.bind`` record, the rest from :func:`prompt_measure.estimate_tokens`, which is Kimi's own
character heuristic. :func:`draw` is what a log gets, and it prints precisely the content the modal
showed the last time someone was watching.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

if __package__:
    from . import kimi_prompts, policy
    from . import prompt_context as pc
    from . import prompt_measure as pm
    from .tui import flow
    from .tui.app import View, run
    from .tui.forest import CHECK, WORD, ForestState, ForestStep, Node
    from .tui.layout import Line, Row, Segment
else:
    import kimi_prompts
    import policy
    import prompt_context as pc
    import prompt_measure as pm
    from tui import flow
    from tui.app import View, run
    from tui.forest import CHECK, WORD, ForestState, ForestStep, Node
    from tui.layout import Line, Row, Segment

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

#: The two states a checkbox answers with, spelled as :mod:`~.tui.forest` spells a marked row.
ON_OFF = ("on", "off")
#: The word on a source that is on disk and not being read. One word, always the same, because a
#: superseded block has to be recognisable without reading a sentence about it.
UNUSED = "unused"
#: Why a switched-off document is not simply *nothing*: Kimi throws away a prompt that trims to
#: blank, so the harness sends the one token that survives. This sentence used to live in the old
#: help screen, which the modal has no reason to hide.
BLANK_NOTE = (
    f"a switched-off document is staged as {pc.EMPTY_PROMPT_SENTINEL!r}, because Kimi discards a "
    "prompt that trims to nothing and would otherwise send its own"
)
#: The rule the whole static half of the tree is built on, and the fact a reader of a dimmed row
#: needs before they trust what the dimming says.
STATIC_NOTE = (
    "An empty file is a decision and is honoured as one; an absent file is not, and a file "
    "holding only whitespace counts as empty. Switching a block here changes this session's "
    "staged document and writes nothing to your files."
)

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


class Context(NamedTuple):
    """One read of the instance directory: what the panel is willing to show, and how it knows.

    ``remembered`` is a separate fact from ``enabled`` because the screen has to say which of the
    two it is drawing - the operator's own last choices, or this build's defaults that nobody has
    agreed to yet.
    """

    plan: dict[str, Any]
    enabled: dict[str, bool]
    static: dict[str, str]
    remembered: bool



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
    static: dict[str, str] | None = None,
) -> Pieces:
    """Cut one audience's prompt into the documents that make it, priced as honestly as it can.

    ``values`` is the mapping ``render_runtime`` will substitute, because pricing a different
    composition from the one that ships would make the panel's numbers fiction. Omit it where
    there is no cache to read, which leaves the harness's own values and nothing else. The same
    applies to ``static``, the tri-state of the two file halves: it decides which file the
    composition reads at all, so leaving it out of a price would quote a document nobody gets.

    A measured row is exact and closes: Kimi's framing, the harness document, and the workspace
    documents are the whole prompt with nothing left over, because :func:`prompt_measure` derives
    the framing by subtraction. An unmeasured row prices what this harness generated and shows ``?``
    for what it did not, rather than inventing a framing figure and passing the sum off as real.

    The staged system prompt is not a fourth box. Kimi renders it ahead of the AGENTS.md documents,
    so it lives *inside* the framing region and the measured ``kimiFraming`` figure already
    contains it. The unmeasured row therefore shows the system prompt as its own estimate plus an
    unknown remainder, rather than double-counting the same text.
    """
    agents = pc.compose_agents_document(root, plan, module_guidance, enabled, values, static)
    system = pc.compose_system_document(root, plan, enabled, values, static)
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


def _source(root: Path, staged: str, static: dict[str, str] | None = None) -> str:
    """Name the file a document actually came from, including when it came from nowhere.

    A forced state is spelled out here rather than left to the filename, because "the operator's
    file is on disk and is not being read" is precisely the case a diagram must not paper over - it
    is the one place where the picture and the directory would otherwise disagree.
    """
    block = pc.STATIC_SYSTEM if staged == "system" else pc.STATIC_CONTEXT
    mode = pc.resolve_static(static)[block]
    source = pc.static_source(root, block, mode)
    if source is None:
        if mode == pc.OFF:
            return "switched off for this session"
        if mode == pc.ON:
            return "switched on, and every file for it is empty"
        return "absent, so the built-in prompt applies" if block == pc.STATIC_SYSTEM else "absent"
    return str(source.relative_to(root)) if source.is_relative_to(root) else str(source)


def _where(note: str | None) -> str:
    """Label a path as a source, and leave a prose note as prose."""
    if not note:
        return ""
    return f"from {note}" if ("/" in note or note.endswith(".md")) else note


def static_rows(
    main: Pieces,
    sub: Pieces,
    plan: dict[str, Any],
    root: Path,
    static: dict[str, str] | None = None,
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
    rows.extend(
        _audience(
            "MAIN AGENT",
            main,
            _source(root, "system", static),
            _source(root, "context", static),
        )
    )
    rows.append(("", "", ""))
    rows.extend(_audience("SUBAGENT", sub, None, _source(root, "context", static)))
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


def _regions(
    used: Pieces, system_from: str | None, context_from: str
) -> list[tuple[str, Cost, str]]:
    """The boxes for one audience: the prompt it receives, cut into who wrote it.

    These are the three regions :func:`prompt_measure.split_regions` cuts a real prompt into, so a
    measured row's boxes sum to its total. There are never four: Kimi renders the staged system
    prompt ahead of the AGENTS.md documents, so it is inside the first region and naming it twice
    would inflate the row past its own total.

    A subagent gets no system prompt - Kimi's own design, not a switch this harness offers - so the
    box is absent rather than zero, which is why ``system_from`` is ``None`` and not ``""``. Both
    renderers call this one function, so a diagram and a tree cannot disagree about a prompt.
    """
    shared = [
        ("harness context, staged as AGENTS.md", used.agents_cost, _where(context_from)),
        ("the workspace's own AGENTS.md files", used.project_cost, "yours, which Kimi reads alone"),
    ]
    if system_from is None:
        return list(shared)
    if used.measured:
        return [
            ("the system prompt, plus Kimi's framing", used.system_cost, _where(system_from)),
            *shared,
        ]
    return [
        ("the staged system prompt", used.system_cost, _where(system_from)),
        *shared,
        ("Kimi's framing around it all", used.framing, "not estimable; see the key"),
    ]


def _audience(
    title: str, used: Pieces, system_from: str | None, context_from: str
) -> list[tuple[str, str, str]]:
    """One row of the diagram: the boxes that add up to it, then the whole figure."""
    rows: list[tuple[str, str, str]] = [(title + " -", "", "bold")]
    for name, cost, note in _regions(used, system_from, context_from):
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
    """One sentence when a pair is half-selected. A suggestion, never a refusal or a block.

    The lonely options are named rather than numbered, because the tree has no numbers to type and
    a warning that pointed at nothing on screen would be the worst kind of help.
    """
    lonely = lonely_pairs(enabled)
    if not lonely:
        return ""
    names = ", ".join(option.label for option in lonely)
    plural = "s" if len(lonely) > 1 else ""
    return (
        f"{len(lonely)} option{plural} may not work as expected, because "
        f"{'they are' if len(lonely) > 1 else 'it is'} paired with a sibling you have switched off "
        f"({names}) - each one still applies, it will just read as though part of it is missing"
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
    static: dict[str, str] | None = None,
) -> str:
    """One screen, both halves, one alignment pass. Every path through the panel prints this."""
    main = pieces(plan, root, module_guidance, enabled,
                  latest.get(pm.AUDIENCE_MAIN), "primary", values, static=static)
    sub = pieces(plan, root, module_guidance, enabled,
                 latest.get(pm.AUDIENCE_SUBAGENT), "subagent", values, static=static)
    screen = Screen(colour_on)
    screen.rows(static_rows(main, sub, plan, root, static))
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


class Switch(NamedTuple):
    """One static block's switch: the states worth offering, and what each one really means.

    ``states`` holds only spellings that produce *different* documents, and ``members`` maps each
    spelling back to the stored modes that produce it. Both halves exist because a tri-state whose
    third answer is indistinguishable from one of the others is a keypress that changes nothing,
    while the file still has to remember the word the operator chose rather than the word the
    screen happened to display.
    """

    states: tuple[str, ...]
    members: Mapping[str, tuple[str, ...]]
    opening: str

    def mode(self, spelling: str, saved: str) -> str:
        """The word to store for this answer, keeping the saved one where it says the same thing."""
        members = self.members.get(spelling, ())
        if saved in members:
            return saved
        return members[0] if members else pc.AUTO


def _tier_text(path: Path) -> str:
    """What this block is made of, or ``""`` for a file that cannot be read.

    Readable-but-unreadable counts as empty rather than absent: the same rule
    :func:`prompt_context._read_tier` applies, and a tree that disagreed with the composer about
    which of the two it was looking at would be showing an override nobody asked for.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _effect(root: Path, block: str, mode: str) -> str:
    """What one state of one block puts in front of the model, reduced to a comparable name.

    This is the whole basis for collapsing three states into two: ``auto`` over an empty
    ``SYSTEM.md`` and ``off`` are the same document, so offering both would let the operator cycle
    a switch and see nothing move.
    """
    if mode == pc.OFF:
        return "blank"
    source = pc.static_source(root, block, mode)
    if source is None:
        return "built-in" if block == pc.STATIC_SYSTEM else "blank"
    return str(source) if _tier_text(source).strip() else "blank"


def _block_switch(root: Path, block: str, saved: str) -> Switch:
    """The switch this block deserves right now, given what is on disk and what was remembered.

    Each group of equivalent states is *named* by the word the panel already uses for it - the one
    :func:`prompt_context.static_state` would report - so a row that opens on ``off`` over an empty
    file is inviting the operator to switch that file off, which is what it means, and not offering
    a fourth state nobody defined.
    """
    groups: dict[str, list[str]] = {}
    for mode in pc.STATIC_STATES:
        groups.setdefault(_effect(root, block, mode), []).append(mode)
    members: dict[str, tuple[str, ...]] = {}
    for group in groups.values():
        spelling = pc.static_state(root, block, {block: group[0]})
        members[spelling if spelling in group else group[0]] = tuple(group)
    states = tuple(state for state in pc.STATIC_STATES if state in members)
    shown = pc.static_state(root, block, {block: saved})
    opening = next((word for word, group in members.items() if saved in group), shown)
    return Switch(states=states, members=members, opening=opening)


class ContextGraph:
    """The diagram, the prices, and the answers, kept in one object that can be asked twice.

    Every figure comes from :func:`pieces` and every state from :mod:`prompt_context`, which is to
    say from the same two places the composed documents come from. The tree is rebuilt from the
    live answer map on every paint because switching a block off moves which file the next row is
    read from - a diagram that could not say that after a keypress would be a picture of some
    other session.
    """

    def __init__(
        self,
        plan: dict[str, Any],
        root: Path,
        module_guidance: str,
        latest: dict[str, dict[str, Any]],
        enabled: dict[str, bool],
        static: dict[str, str],
        *,
        values: dict[str, str] | None = None,
        stale: Sequence[str] = (),
    ) -> None:
        self.plan = plan
        self.root = root
        self.module_guidance = module_guidance
        self.latest = latest
        self.enabled = dict(pc.resolve_enabled(enabled))
        self.static = dict(pc.resolve_static(static))
        self.values = values
        self.stale = tuple(stale)
        self.switches = {
            block: _block_switch(root, block, self.static[block]) for block in pc.STATIC_IDS
        }
        self._priced: dict[frozenset[tuple[str, str]], tuple[Pieces, Pieces]] = {}

    # -- answers ----------------------------------------------------------------------------

    def opening(self) -> dict[str, str]:
        """The answer map the tree opens with: what was remembered, in the screen's own words."""
        answer = {block: self.switches[block].opening for block in pc.STATIC_IDS}
        for option in pc.OPTIONS:
            answer[option.id] = ON_OFF[0] if self.enabled[option.id] else ON_OFF[1]
        return answer

    def answer(self, live: Mapping[str, str]) -> tuple[dict[str, bool], dict[str, str]]:
        """The two maps everything else is composed from, out of one screen state."""
        enabled = {option.id: live.get(option.id) == ON_OFF[0] for option in pc.OPTIONS}
        modes = {}
        for block, switch in self.switches.items():
            spelling = live.get(block, switch.opening)
            modes[block] = switch.mode(spelling, self.static[block])
        return enabled, modes

    def modes(self, live: Mapping[str, str]) -> dict[str, str]:
        """The modes the diagram is drawn from: each row's own spelling, not the saved member.

        Two states are collapsed into one word only when they compose the same document, so either
        member is honest about the bytes. They are not honest about *provenance*, and a diagram is a
        statement about provenance: an empty ``CONTEXT.md`` drawn from the saved ``auto`` would name
        that file as the source of a block the switch beside it has just called ``off``.
        """
        return {block: self._drawn(block, live) for block in pc.STATIC_IDS}

    def _drawn(self, block: str, live: Mapping[str, str]) -> str:
        spelling = live.get(block, self.switches[block].opening)
        return spelling if spelling in pc.STATIC_STATES else self.switches[block].opening

    def deviated(self, node: Node, value: str) -> str:
        """Colour for a row the operator has moved, which is the one thing the tree must not hide.

        Only a static block earns it: a checkbox off is a normal choice, while a switched document
        is a session override that will not be there next launch unless it is remembered too.
        """
        if node.kind == WORD and value and value != self.switches.get(node.id, self._any()).opening:
            return "over"
        return ""

    @staticmethod
    def _any() -> Switch:
        return Switch(states=(), members={}, opening="")

    # -- figures ---------------------------------------------------------------------------

    def priced(self, live: Mapping[str, str]) -> tuple[Pieces, Pieces]:
        """Both audiences' prompts for one answer map, composed once however often it is drawn.

        Memoised on the map rather than on the rows, because the rows are derived from the map: the
        cache cannot go stale in the one direction that matters, and a whole recomposition of two
        prompts costs milliseconds.
        """
        key = frozenset(live.items())
        cached = self._priced.get(key)
        if cached is not None:
            return cached
        enabled, _ = self.answer(live)
        static = self.modes(live)
        pair = (
            pieces(
                self.plan, self.root, self.module_guidance, enabled,
                self.latest.get(pm.AUDIENCE_MAIN), "primary", self.values, static=static,
            ),
            pieces(
                self.plan, self.root, self.module_guidance, enabled,
                self.latest.get(pm.AUDIENCE_SUBAGENT), "subagent", self.values, static=static,
            ),
        )
        self._priced[key] = pair
        return pair

    def notices(self, live: Mapping[str, str]) -> tuple[str, ...]:
        """The sentences that are true of this answer and of no other: warnings, bills, staleness.

        These belong to the tree rather than to a banner because they are about the selection:
        a half-chosen pair and an oversized instruction both appear and disappear as the operator
        toggles, and one that stayed on screen after the thing it warned about was fixed would
        teach people to ignore the yellow.
        """
        enabled, _ = self.answer(live)
        main, _ = self.priced(live)
        notes = [note for note in (warning(enabled), *over_limit(main), *self.stale) if note]
        return tuple(notes)

    # -- the tree --------------------------------------------------------------------------

    def nodes(self, live: Mapping[str, str]) -> tuple[Node, ...]:
        """The whole diagram: the files, the add-ons, the two prompts, and the denominator."""
        enabled, _ = self.answer(live)
        static = self.modes(live)
        main, sub = self.priced(live)
        return (
            self._static_root(static),
            self._dynamic_root(enabled),
            self._prompt_root(main, sub, static),
            self._caps_root(),
        )

    def _static_root(self, static: Mapping[str, str]) -> Node:
        return Node(
            id="static",
            label="static context - the files these prompts are read from",
            note=STATIC_NOTE,
            children=tuple(self._block_row(block, static) for block in pc.STATIC_IDS[::-1])
            + (self._addons_row(),),
        )

    def _name(self, path: Path) -> str:
        """How a file is referred to in the tree: relative when it is ours, absolute when not.

        The same spelling :func:`_source` uses, so a row and the sentence under it never name one
        file two different ways — ``runtime/AGENTS.md`` and ``AGENTS.md`` are different files here.
        """
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def _block_row(self, block: str, static: Mapping[str, str]) -> Node:
        """One block: its switch, then every file that could supply it, in authority order."""
        switch = self.switches[block]
        mode = static[block]
        source = pc.static_source(self.root, block, mode)
        children = [
            self._tier_row(block, path, source)
            for path in pc.static_tiers(self.root, block)
            if path.is_file()
        ]
        if block == pc.STATIC_SYSTEM:
            children.append(self._built_in_row(mode, source))
        label = pc.TARGET_SYSTEM if block == pc.STATIC_SYSTEM else pc.TARGET_AGENTS
        return Node(
            id=block,
            label=label,
            kind=WORD,
            states=switch.states,
            default=switch.opening,
            enabled=len(switch.states) > 1,
            note=self._block_note(block, mode, source, switch),
            children=tuple(children),
        )

    def _block_note(self, block: str, mode: str, source: Path | None, switch: Switch) -> str:
        """Why this row says what it says, including when there is nothing to switch."""
        if len(switch.states) > 1:
            return _where(_source(self.root, block, {block: mode}))
        if source is None:
            return (
                "nothing to switch: no file supplies this block, and every state would leave it "
                "exactly as empty as it is"
            )
        return _where(_source(self.root, block, {block: mode}))

    def _tier_row(self, block: str, path: Path, source: Path | None) -> Node:
        """A file that could supply this block, marked used or superseded.

        A superseded file stays in the tree: the operator put it there, and a diagram that deleted
        it would make the directory and the picture disagree.
        """
        used = path == source
        name = self._name(path)
        if used:
            note = ""
        elif source is None:
            note = "not read: this block is switched off, so no file of yours supplies it"
        else:
            note = f"not read: {self._name(source)} is the more authoritative tier of the two"
        return Node(
            id=f"{block}:{name}",
            label=name,
            enabled=used,
            branch=used and block == pc.STATIC_CONTEXT,
            badge="" if used else UNUSED,
            note=note,
        )

    def _built_in_row(self, mode: str, source: Path | None) -> Node:
        """Kimi's own prompt, which is in use until one of yours replaces it or wraps it.

        The ``${base_prompt}`` case is drawn as a link rather than as an absence, because the
        built-in text is reaching the model *through* the operator's file: that is composition,
        and the tree says composition with a coloured edge.
        """
        if mode == pc.OFF:
            return Node(id="builtin", label="Kimi's own built-in prompt", badge=UNUSED,
                        enabled=False, note=BLANK_NOTE)
        if source is None:
            return Node(id="builtin", label="Kimi's own built-in prompt",
                        note="no file of yours, so Kimi's own words open the prompt")
        if pc.BASE_PROMPT_WRAPPER in pc.strip_html_comments(_tier_text(source)):
            return Node(id="builtin", label="Kimi's own built-in prompt", link=True,
                        note=f"substituted into {self._name(source)} by {pc.BASE_PROMPT_WRAPPER}")
        return Node(id="builtin", label="Kimi's own built-in prompt", badge=UNUSED, enabled=False,
                    note=f"replaced: {self._name(source)} is the whole prompt")

    def _addons_row(self) -> Node:
        """The pointer that makes the two halves one diagram: what follows the files."""
        return Node(
            id="addons",
            label="the dynamic add-ons",
            note="appended after whichever file supplies each block above, and priced one by "
            "one in the section below",
        )

    def _dynamic_root(self, enabled: Mapping[str, bool]) -> Node:
        children = []
        for option in pc.OPTIONS:
            cost = option_cost(option, self.plan, self.module_guidance)
            children.append(
                Node(
                    id=option.id,
                    label=option.label,
                    kind=CHECK,
                    states=ON_OFF,
                    default=ON_OFF[0] if enabled[option.id] else ON_OFF[1],
                    value="no prompt cost" if cost.measured and not cost.tokens else cost.text(),
                    note=f"into {option.target}: {option.summary}",
                )
            )
        return Node(
            id="dynamic",
            label="dynamic context this harness adds",
            note="Each price is that block alone, not a measurement of a rendered prompt.",
            children=tuple(children),
        )

    def _prompt_root(self, main: Pieces, sub: Pieces, static: Mapping[str, str]) -> Node:
        """The two totals, priced from the answer on screen and labelled from the modes it draws."""
        context_from = _source(self.root, "context", static)
        return Node(
            id="figures",
            label="what the model receives",
            note=KEY,
            children=(
                # A subagent is handed no system prompt at all, so `None` below is a fact about the
                # audience rather than about the operator's files: see `_regions`.
                self._audience_row("main", "MAIN AGENT", main,
                                   _source(self.root, "system", static), context_from),
                self._audience_row("sub", "SUBAGENT", sub, None, context_from),
            ),
        )

    def _audience_row(
        self, key: str, title: str, used: Pieces, system_from: str | None, context_from: str
    ) -> Node:
        """One audience's prompt as a row, with the boxes that add up to it underneath.

        ``title`` comes from the caller rather than from ``key``, because the key has to stay the
        short id the rows are addressed by and ``"sub".upper()`` is not the name of anything.
        """
        children = []
        for name, cost, note in _regions(used, system_from, context_from):
            total, pct = figure(cost, used.cap).split("|")
            children.append(
                Node(id=f"{key}:{name}", label=name, value=total, pct=pct, note=note)
            )
        if used.measured:
            total, pct = figure(used.total, used.cap).split("|")
            return Node(
                id=key,
                label=f"{title} - the whole prompt, on every request",
                value=total,
                pct=pct,
                note=f"measured from your last real request, {used.age} ago",
                children=tuple(children),
            )
        return Node(
            id=key,
            label=f"{title} - the whole prompt cannot be totalled",
            note="the box priced ? has no honest figure; launch once and this row becomes exact",
            children=tuple(children),
        )

    def _caps_root(self) -> Node:
        children = []
        for name, cap in sorted(pm.caps(self.plan).items()):
            audience = "subagents" if name == "subagent" else "the main agent"
            window = self.plan["lanes"][name]["context_tokens"]
            children.append(
                Node(
                    id=f"cap:{name}",
                    label=f"{name} lane ({audience})",
                    value=f"cap {cap:,}",
                    note=f"window {window:,}",
                )
            )
        return Node(
            id="caps",
            label="lane caps - the denominator for every percentage above",
            children=tuple(children),
        )


class ContextStep(ForestStep):
    """The graph, on screen, redrawn from the operator's own answer on every keystroke.

    A plain :class:`~.tui.forest.ForestStep` is built once and edited in place, which is right for
    a list of things that exist regardless of the answer. This one is not: switching a file off
    moves which file the prices came from, so the rows are derived from the state instead of
    being stored beside it. Node ids are stable across those rebuilds, which is what lets the
    cursor, the toggle and the scroll all survive a repaint they caused.
    """

    def __init__(self, graph: ContextGraph, head: Sequence[str] = ()) -> None:
        self.graph = graph
        opening = graph.opening()
        super().__init__(
            title="Session context",
            nodes=graph.nodes(opening),
            head=head,
            opening=opening,
            tone_of=graph.deviated,
        )
        self._live: Mapping[str, str] | None = None

    def initial(self) -> ForestState:
        self._bind(self._opening)
        return super().initial()

    def rows(self, state: object) -> list[Row]:
        assert isinstance(state, ForestState)
        self._bind(state.values)
        out = super().rows(state)
        for notice in self.graph.notices(state.values):
            for piece in self._wrapped(notice, self.room):
                out.append(Row.heading(Line(Segment(piece, self.tone("warn")))))
        return out

    def _bind(self, values: Mapping[str, str]) -> None:
        """Re-derive the tree unless the answer is the one it was built from."""
        if self._live is not None and dict(values) == self._live:
            return
        self._live = dict(values)
        self.nodes = tuple(self.graph.nodes(self._live))
        self._roots_are_checkable = any(node.kind == CHECK for node in self.nodes)


def recap(enabled: Mapping[str, bool], static: Mapping[str, str]) -> str:
    """One line for the scrollback, naming both halves of what the screen just accepted."""
    on = sum(1 for option in pc.OPTIONS if enabled[option.id])
    blocks = ", ".join(f"{block} {static[block]}" for block in pc.STATIC_IDS)
    return f"{on} of {len(pc.OPTIONS)} add-ons on; {blocks}"


def choose_context(
    runtime: Path,
    root: Path,
    context: Context,
    module_guidance: str,
    latest: dict[str, dict[str, Any]],
    values: dict[str, str] | None,
    stale: Sequence[str],
    *,
    asking: bool,
) -> tuple[dict[str, bool], dict[str, str]]:
    """The context step: one screen when there is a terminal, one printout when there is not.

    This is also the one place the flow learns that the step happened. The launcher has always
    listed ``context`` in its sequence, and until now nothing ever committed or skipped it, so a
    Back from the credentials step walked past it into the module values - a hole in the middle of
    the only part of a launch you are supposed to be able to return through.
    """
    state = flow.running(runtime)
    rendering = state.plan(flow.CONTEXT, 1 if asking else 0) if state else asking
    back_available = bool(state and state.previous(flow.CONTEXT))
    if not rendering:
        # An unattended launch, or a replay of an answer this pass already has: either way the
        # printout is the record, and nothing here may write a preference nobody was asked for.
        print(
            draw(
                context.plan, root, module_guidance, context.enabled, latest,
                values=values, colour_on=False, remembered=context.remembered, stale=stale,
                static=context.static,
            )
        )
        return context.enabled, context.static
    state_name = (
        "remembered from your last launch" if context.remembered else "this build's defaults"
    )
    graph = ContextGraph(
        context.plan, root, module_guidance, latest, context.enabled, context.static,
        values=values, stale=stale,
    )
    position, total = state.rail(flow.CONTEXT) if state else (1, 1)
    result = run(
        ContextStep(graph, (f"Showing {state_name}. Enter accepts; nothing is written or "
                            "started until you do.",)),
        View(
            position=position,
            total=total,
            can_go_back=back_available,
        ),
    )
    if result.status == flow.GO_BACK:
        flow.back_from(state, flow.CONTEXT)
    if not result.accepted:
        raise SystemExit(result.status)
    enabled, static = graph.answer(result.value)
    if state:
        state.commit(flow.CONTEXT, recap(enabled, static))
    pc.save_prefs(runtime / pc.PREFS_FILE, enabled, static)
    return enabled, static


def load_context(runtime_dir: Path) -> Context:
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
    path = runtime_dir / pc.PREFS_FILE
    return Context(
        plan=plan,
        enabled=pc.load_prefs(path),
        static=pc.load_static(path),
        remembered=path.is_file(),
    )


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



#: The static blocks in words, for the two places that have to name one without drawing the
#: diagram: the scripted report below, and the panel's own row for it.
STATIC_NOTES = {
    pc.STATIC_CONTEXT: "the file half of the all-lane contract, staged as AGENTS.md",
    pc.STATIC_SYSTEM: "the file half of the main agent's prompt, staged as SYSTEM.md",
}


def parse_static(values: Sequence[str]) -> dict[str, str] | None:
    """``--static ID=STATE`` over and over, or ``None`` if the caller named nothing.

    ``None`` is a different answer from an empty map and from ``{id: "auto"}``: it is "this command
    has no opinion, leave whatever is remembered". A caller that meant to reset would say so, and
    the difference is one an operator can only get right if the two are never conflated.
    """
    states = {}
    for value in values:
        name, sep, state = value.partition("=")
        if not sep or name not in pc.STATIC_IDS or state not in pc.STATIC_STATES:
            raise ValueError(value)
        states[name] = state
    return states or None


def report_choices(enabled: dict[str, bool], static: dict[str, str]) -> None:
    """Print both halves of the selection, one line each, in the order the screen shows them."""
    for option in pc.OPTIONS:
        state = "on " if enabled[option.id] else "off"
        print(f"{state} {option.id:20} {option.label}")
    for block in pc.STATIC_IDS:
        print(f"{static[block]:4} {block:20} {STATIC_NOTES[block]}")


def run_configure(runtime_dir: Path, argv: list[str]) -> int:
    """``--configure`` is the scripted answer to the panel, so headless is not second-class.

    Deleting the opt-out environment variables left no non-interactive way to express a selection
    at all. This is that way, and it is also the only supported scriptable contract over the
    preference file, so it ships in the same change as the panel rather than after it.

    ``--all-on`` and ``--all-off`` reset the option list and leave the static blocks alone: they are
    a second axis, and blanking both documents because someone typed two characters they already
    knew meant "the boxes" would be the sort of surprise no flag documentation survives.
    """
    parser = argparse.ArgumentParser(prog="./prompts.sh --configure")
    parser.add_argument("--enable", action="append", default=[], metavar="ID")
    parser.add_argument("--disable", action="append", default=[], metavar="ID")
    parser.add_argument(
        "--static",
        action="append",
        default=[],
        metavar="ID=STATE",
        help=f"force a document: {', '.join(pc.STATIC_IDS)} = {'|'.join(pc.STATIC_STATES)}",
    )
    parser.add_argument("--all-on", action="store_true")
    parser.add_argument("--all-off", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    for name in (*args.enable, *args.disable):
        if name not in pc.OPTION_IDS:
            print(f"unknown option: {name}", file=sys.stderr)
            print(f"valid ids: {', '.join(pc.OPTION_IDS)}", file=sys.stderr)
            return 2
    try:
        wanted = parse_static(args.static)
    except ValueError as error:
        forms = ", ".join(f"{b}={s}" for b in pc.STATIC_IDS for s in pc.STATIC_STATES)
        print(f"unknown static state: {error}", file=sys.stderr)
        print(f"valid forms: {forms}", file=sys.stderr)
        return 2
    path = runtime_dir / pc.PREFS_FILE
    enabled = pc.load_prefs(path)
    static = pc.load_static(path) if wanted is None else {**pc.load_static(path), **wanted}
    if args.all_on:
        enabled = dict(pc.DEFAULT_ENABLED)
    if args.all_off:
        enabled = dict.fromkeys(pc.OPTION_IDS, False)
    for name in args.enable:
        enabled[name] = True
    for name in args.disable:
        enabled[name] = False
    changed = args.enable or args.disable or args.all_on or args.all_off or wanted
    if args.show or not changed:
        report_choices(enabled, static)
        return 0
    pc.save_prefs(path, enabled, static)
    print(f"wrote {path}")
    report_choices(enabled, static)
    return 0


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

    context = load_context(args.runtime_dir)
    module_guidance = read_module_guidance(args.runtime_dir)
    latest = read_latest(args.runtime_dir)
    values = kimi_prompts.substitutions(args.runtime_dir)
    # Staleness is a property of a live stack: the sidecar describes bytes that are mounted right
    # now. At startup this panel runs before render_runtime.py stages anything, so every file would
    # read as stale and the advice would be false - the very next step applies the edit. --show is
    # the inspect-a-running-session path, and that is where the comparison means something.
    stale = pc.stale_sources(args.root, args.runtime_dir) if args.show else []
    # Both halves of the answer travel back together now that a static block is a choice like any
    # other, but this entry point's contract is a status code: the files it decides are staged by
    # render_runtime.py from the preference file choose_context just wrote.
    choose_context(
        args.runtime_dir, args.root, context, module_guidance, latest, values, stale,
        asking=sys.stdin.isatty() and sys.stdout.isatty() and not (args.plain or args.show),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
