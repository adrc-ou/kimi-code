#!/usr/bin/env python3
"""Compose the two documents the harness puts in front of Kimi Code, and remember what the
operator chose to put there.

Everything the harness adds to a session - as opposed to what Kimi adds itself and what the
user's project adds - is assembled here, in one place, from one dict of booleans. Three callers
read that dict and nothing else may: the renderer, which stages the bytes; the startup panel,
which draws the graph; and ``prompts.sh``, which prints the same graph without a prompt. That
restriction is what keeps the picture the operator approves and the files the container mounts
from drifting apart.

Two documents, three audiences:

``CONTEXT.md`` -> ``~/.kimi-code/AGENTS.md``, read through ``${agents_md}``, reaches the main
agent **and every subagent**. It holds the operating contract plus every add-on that is true of
every lane.

``SYSTEM.md`` -> ``~/.kimi-code/SYSTEM.md``, the ``agent`` profile, reaches **the main agent
only**. It holds the operator's own voice plus the add-ons that are only actionable there,
because only the main agent can choose a lane or start a child.

Each document has two tiers and no more: the operator's file, then a built-in default. The
``.example`` files beside them document the convention and are never loaded - a filename that
advertises itself as an example must not be load-bearing, and nobody looks in one for behaviour.

This module is pure string assembly: no subprocess, no Docker, no filesystem writes. What it
returns is written by ``tools/render_runtime.py``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

if __package__:
    from . import policy
else:
    import policy

#: Where each document is written by the operator, and what the harness stages it into. The
#: staged names are Kimi's own conventions and are not negotiable.
CONTEXT_FILE = "CONTEXT.md"
SYSTEM_FILE = "SYSTEM.md"
CONTEXT_EXAMPLE = "CONTEXT.md.example"
SYSTEM_EXAMPLE = "SYSTEM.md.example"
STAGED_AGENTS = "AGENTS.md"
STAGED_SYSTEM = "SYSTEM.md"
#: Tier two for the all-lane document: the contract this harness ships in its own tree. It is
#: the last word on how an agent behaves here, and it is not a `.example`.
CONTRACT_SOURCE = ("runtime", "AGENTS.md")
#: The 15 keys Kimi substitutes into a prompt template, read off the pinned bundle at
#: ``systemPromptVars`` (127774149). ``base_prompt`` is not among them: it is bound separately,
#: and only for a template that mentions it.
KIMI_PLACEHOLDERS = (
    "role_additional",
    "product_name",
    "reply_style_guide",
    "notify_user_guidance",
    "os",
    "windows_notes",
    "shell",
    "cwd",
    "cwd_listing",
    "agents_md",
    "additional_dirs_info",
    "additional_dirs_section",
    "skills",
    "skills_section",
    "plugin_sections",
)
#: Placeholders this harness resolves at staging, so a typo in one is fatal rather than literal.
HARNESS_PLACEHOLDERS = ("harness.date",)
#: The staged all-lane document is never absent, so it must never be unwritable either.
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*[:?]?[^}]*)\}")
#: An unrecognised name is only an error when it nearly matches one we do know. Anything else is
#: legitimate prose - a shell example like ``${HOME}`` belongs in an operator's file, and Kimi
#: would pass it through untouched.
PLACEHOLDER_CLOSENESS = 2

#: The wrapper that stands in for a main-only add-on when the operator has no ``SYSTEM.md``.
#: Appending to a staged file that is empty would make Kimi treat what remains as the entire
#: prompt, so the wrapper keeps its own prompt alive at a cost of exactly 14 characters, which
#: expand into the built-in text and therefore have no size of their own.
BASE_PROMPT_WRAPPER = "${base_prompt}"
#: Kimi Code discards any system prompt that is blank once trimmed and silently substitutes its
#: own, so an operator who asks for no prompt at all gets the shortest string that survives the
#: trim and carries no instruction: one token, and it is used only when nothing else is on.
EMPTY_PROMPT_SENTINEL = "."
#: Persistent per-instance state. Both survive a launch, which is the whole point: the panel's
#: choices are memory and the measurements are history. Neither is on ``start.sh``'s cleanup list.
PREFS_FILE = "prompt-context.json"
MEASUREMENTS_FILE = "prompt-measurements.jsonl"

#: Which document or setting an option governs.
TARGET_AGENTS = "harness context"
TARGET_SYSTEM = "main system prompt"
TARGET_CONFIG = "Kimi configuration"
TARGET_ENV = "agent environment"


#: The ids of the blocks whose text policy.py generates; those three constants live there, beside
#: the generators they name. These are the ids of everything else the panel governs, and they are
#: constants rather than inline strings because the panel, the toggle tables below, and the tests
#: all have to agree on them without any of them being able to import the others' literals.
OPTION_MODULE_GUIDANCE = "module_guidance"
OPTION_HARNESS_SKILLS = "harness_skills"
OPTION_FULL_SKILL_LISTING = "full_skill_listing"
OPTION_PRODUCT_SKILLS = "product_skills"
OPTION_HARNESS_AGENTS = "harness_agents"
OPTION_PERMISSION_BANNER = "permission_banner"


@dataclass(frozen=True)
class Option:
    """One checkbox on the startup panel, and the mechanism that makes it real."""

    id: str
    label: str
    target: str
    summary: str
    #: Options whose text reads better beside this one's. A suggestion, never a constraint: the
    #: panel marks these in the gutter and says so once, and the user's choice always stands.
    companions: tuple[str, ...] = ()


#: Every dynamic add-on the harness manages, in panel order. Siblings sit adjacent so a
#: half-selected pair is visible as a shape; this is topic adjacency, not grouping by provenance.
#: Nothing is outside this list - there is no umbrella flag and no exempt category.
OPTIONS: tuple[Option, ...] = (
    Option(
        id=policy.OPTION_LANE_LIMITS,
        label="Usage limits: context budget, request rate, queueing",
        target=TARGET_AGENTS,
        summary=(
            "The generated numbers every request spends regardless of who sends it, for this "
            "agent and every subagent."
        ),
        companions=(policy.OPTION_LANE_TABLE,),
    ),
    Option(
        id=policy.OPTION_LANE_TABLE,
        label="Per-model lane table: windows, aliases, ceilings",
        target=TARGET_SYSTEM,
        summary=(
            "The lane-by-lane shape of the session, which only the main agent can act on: a "
            "subagent's lane is fixed by the harness."
        ),
        companions=(policy.OPTION_LANE_LIMITS, policy.OPTION_PARALLELISM),
    ),
    Option(
        id=policy.OPTION_PARALLELISM,
        label="Encourage using every allowed parallel subagent",
        target=TARGET_SYSTEM,
        summary=(
            "Tells the main agent to fan out to the published ceiling instead of working "
            "serially, and how to choose the shape of the work."
        ),
        companions=(policy.OPTION_LANE_TABLE,),
    ),
    Option(
        id=OPTION_MODULE_GUIDANCE,
        label="Guidance from the selected modules",
        target=TARGET_AGENTS,
        summary=(
            "Each selected module's own AGENTS.md. It is the only route that text has to the "
            "agent, so a module that needs a convention cannot work without it."
        ),
    ),
    Option(
        id=OPTION_HARNESS_SKILLS,
        label="Make this harness's Skills available",
        target=TARGET_CONFIG,
        summary="Sets extra_skill_dirs, which is how the Skills shipped in this repository load.",
        companions=(OPTION_FULL_SKILL_LISTING, OPTION_PRODUCT_SKILLS),
    ),
    Option(
        id=OPTION_FULL_SKILL_LISTING,
        label="List every available Skill, not the default set",
        target=TARGET_CONFIG,
        summary="Sets merge_all_available_skills.",
        companions=(OPTION_HARNESS_SKILLS, OPTION_PRODUCT_SKILLS),
    ),
    Option(
        id=OPTION_PRODUCT_SKILLS,
        label="Make Kimi's built-in product Skills available",
        target=TARGET_CONFIG,
        summary="Sets builtin_product_skills.",
        companions=(OPTION_HARNESS_SKILLS, OPTION_FULL_SKILL_LISTING),
    ),
    Option(
        id=OPTION_HARNESS_AGENTS,
        label="Make this harness's subagent roles available",
        target=TARGET_CONFIG,
        summary="Sets extra_agent_dirs, which is how the role files in runtime/agents load.",
    ),
    Option(
        id=OPTION_PERMISSION_BANNER,
        label="Show the permission-mode banner on every turn",
        target=TARGET_ENV,
        summary="Sets KIMI_CODE_PERMISSION_MODE_REMINDER in the agent container.",
    ),
)

OPTION_IDS = tuple(option.id for option in OPTIONS)
OPTION_BY_ID = {option.id: option for option in OPTIONS}
#: Every option ships on, so a launch with no saved choices and no terminal behaves as though the
#: operator had accepted the panel.
DEFAULT_ENABLED: dict[str, bool] = dict.fromkeys(OPTION_IDS, True)


def resolve_enabled(enabled: Mapping[str, bool] | None) -> dict[str, bool]:
    """Normalise a caller's choices against the shipped defaults.

    Unknown names are dropped rather than trusted, so a stale prefs file written by an older or
    newer panel cannot silence a mechanism by accident.
    """
    if enabled is None:
        return dict(DEFAULT_ENABLED)
    return {option: bool(enabled.get(option, DEFAULT_ENABLED[option])) for option in OPTION_IDS}


def load_prefs(path: Path) -> dict[str, bool]:
    """Read remembered choices, tolerating a file that has never been written."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_ENABLED)
    if not isinstance(stored, dict):
        return dict(DEFAULT_ENABLED)
    prefs = dict(DEFAULT_ENABLED)
    for option in OPTION_IDS:
        if option in stored:
            prefs[option] = bool(stored[option])
    return prefs


def save_prefs(path: Path, enabled: Mapping[str, bool]) -> None:
    """Write choices keyed by option id, never by label, so renaming a label resets nobody."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {option: bool(enabled[option]) for option in OPTION_IDS}
    text = json.dumps(ordered, indent=2, sort_keys=False) + "\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` help so the file an operator edits can explain itself for free.

    Scoped by callers to the two harness-owned documents. It is a real semantic change, made on
    purpose: the bytes an operator edits are no longer byte-identical to the bytes measured.
    """
    return re.sub(r"<!--.*?-->\n?", "", text, flags=re.DOTALL)


def harness_values(when: date | None = None) -> dict[str, str]:
    """The ``${harness.*}`` names this harness resolves before staging."""
    return {"harness.date": (when or date.today()).isoformat()}


def known_placeholders() -> tuple[str, ...]:
    """Every name that will be substituted by Kimi or by us, so every name worth spelling right."""
    return (*KIMI_PLACEHOLDERS, "base_prompt", *HARNESS_PLACEHOLDERS, *kimi_literal_names())


def kimi_literal_names() -> tuple[str, ...]:
    """The ``${kimi.*}`` names an operator may write.

    This is the authoritative list of the *names*; ``tools/kimi_prompts.py`` owns the bundle
    identifiers each one is read from, and refuses to import if the two sets diverge. Adding a
    literal to one file only would otherwise let it escape both the near-miss gate and the
    unresolved-literal check without anything failing.
    """
    return (
        "kimi.system_default",
        "kimi.coder_role",
        "kimi.explore_overlay",
        "kimi.task_agent_prefix",
    )


#: A name the published documentation offers and this build does not define, so it survives to the
#: model as literal text. ``now`` is within two edits of ``os``, which means the near-miss gate in
#: :func:`check_placeholders` would otherwise abort on the very behaviour the panel advertises.
DOCUMENTED_BUT_UNDEFINED = ("now",)

#: What an operator is told when their file quotes one of those names and this image's cache has
#: not been warmed. A quoted literal is a promise about upstream text, so shipping the name as
#: literal characters instead of failing would put ``${kimi.system_default}`` in front of the model.
COLD_CACHE = (
    "no cached Kimi prompt literals for this image yet; ./start.sh reads them from the bundle "
    "once the stack is up, so this resolves on the next launch, or now via ./prompts.sh --extract"
)


#: Fenced blocks first, then the two inline idioms, longest delimiter first: Markdown lets a span
#: containing a backtick be written with two of them, and a reader who writes ``${cwd}`` that way
#: means it just as much as the single-backtick author does.
_FENCED = re.compile(r"(```.*?```|~~~.*?~~~|``[^`\n]*``|`[^`\n]*`)", re.DOTALL)


def outside_code(text: str) -> str:
    """Blank out fenced and inline code spans, which hold examples rather than intentions.

    Shared by the two places that have to read an operator's file the way the operator wrote it:
    the placeholder gate, and the report of what a live prompt still carries literally.
    """
    return _FENCED.sub(lambda span: " " * len(span.group(0)), text)


def check_placeholders(text: str, path: Path | str = "") -> None:
    """Refuse a near-miss placeholder, and pass every other ``${...}`` through untouched.

    Kimi's ``renderPrompt`` leaves an unrecognised name as literal text, so ``${base_prampt}``
    would reach the model as 13 characters and no error would ever exist. Only names within edit
    distance 2 of a known one are fatal, because an operator's legitimate ``${HOME}`` or
    ``${FOO:?bar}`` is prose, and code spans are exempt from *that* check.

    Names the upstream documentation advertises and this build does not define are exempt too.
    The panel promises they reach the model as literal text, and a three-letter name sits within
    distance 2 of several that do exist, so gating them would retract that promise with an abort.

    The duplicate-wrapper check is deliberately not code-span exempt, unlike the near-miss one.
    Substitution is unconditional, so a second wrapper inside backticks really would ship the
    built-in prompt twice: the span says what the author meant, not what the file will do. An
    operator who needs to say it twice says it in a comment, which is stripped before this runs.
    """
    known = known_placeholders()
    literal = DOCUMENTED_BUT_UNDEFINED
    problems: list[str] = []
    prose = outside_code(text)
    for span in PLACEHOLDER_PATTERN.finditer(prose):
        name = span.group(1).split(":", 1)[0].strip()
        if name in known or name in literal:
            continue
        near = difflib.get_close_matches(name, known, n=1, cutoff=0.0)
        if near and _edit_distance(name, near[0]) <= PLACEHOLDER_CLOSENESS:
            problems.append(f"{span.group(0)} looks like a misspelling of ${{{near[0]}}}")
    if text.count(BASE_PROMPT_WRAPPER) > 1:
        problems.append("${base_prompt} appears more than once, which would duplicate the prompt")
    if problems:
        where = f"{path}: " if path else ""
        raise SystemExit(f"{where}unresolved prompt placeholder: " + "; ".join(problems))


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein distance, two rows at a time."""
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def substitute_harness_placeholders(text: str, values: Mapping[str, str] | None = None) -> str:
    """Resolve only what the harness owns; ``${base_prompt}`` and Kimi's 15 stay for Kimi."""
    mapping = values or harness_values()
    for name, value in mapping.items():
        text = text.replace("${" + name + "}", value)
    return text


#: Sidecar the renderer leaves beside the documents it stages, recording which file each one came
#: from and the exact bytes it was read as. Everything is installed read-only and immutable, so an
#: edit mid-session cannot take effect; this is what makes "nothing changed" explainable.
SOURCES_FILE = "prompt-sources.json"

#: What Kimi itself treats as a large instruction set, read out of the pinned bundle as
#: ``AGENTS_MD_RECOMMENDED_MAX_BYTES = 32 * 1024``. Past it Kimi prints a load warning and loads
#: every byte anyway - instruction text is never truncated - so the only honest harness response
#: is to say so in the same unit Kimi uses.
KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES = 32 * 1024


def instruction_bytes(*documents: str) -> int:
    """Total bytes of instruction text Kimi will hoist, which is what its own warning counts."""
    return sum(len(document.encode("utf-8")) for document in documents)


def over_instruction_limit(total_bytes: int) -> str:
    """The one sentence worth saying when instruction text passes Kimi's recommended limit."""
    if total_bytes <= KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES:
        return ""
    limit = KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES
    return (
        f"{total_bytes / 1024:.1f} KB of instruction text, over Kimi's recommended"
        f" {limit // 1024} KB: it warns on every load and ships all of it regardless."
        " Nothing here is truncated, so trimming is yours to do or to leave."
    )


def document_sources(root: Path) -> dict[str, dict[str, str | None]]:
    """What each staged document was read from, keyed by ``agents`` and ``system``.

    The digest is of the comment-stripped text rather than the file, which is what makes the
    staleness report worth reading: editing help text genuinely does not need a restart, because
    the bytes that ship are unchanged.
    """
    recorded: dict[str, dict[str, str | None]] = {}
    for role, path in (("agents", context_source(root)), ("system", system_source(root))):
        if path is None:
            recorded[role] = {"source": None, "digest": None}
            continue
        try:
            text = strip_html_comments(path.read_text(encoding="utf-8"))
        except OSError:
            recorded[role] = {"source": str(path), "digest": None}
            continue
        recorded[role] = {
            "source": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return recorded


def stale_sources(root: Path, runtime_dir: Path) -> list[str]:
    """One sentence per document whose source moved after it was staged, and restart is required.

    Silent when there is no record: a stack staged by an older harness has nothing honest to
    compare against, and inventing a warning would teach the operator to ignore them.
    """
    try:
        recorded = json.loads((runtime_dir / SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(recorded, dict):
        return []
    now = document_sources(root)
    stale = []
    for role, before in recorded.items():
        after = now.get(role)
        if not isinstance(before, dict) or after is None:
            continue
        if before.get("digest") != after.get("digest"):
            label = after.get("source") or before.get("source") or role
            stale.append(f"{label} changed since it was staged - restart to apply it")
    return stale


def system_source(root: Path) -> Path | None:
    """The file the main agent's voice is read from, or ``None`` for "no opinion".

    Tier one is the operator's ``SYSTEM.md`` and there is no tier two: ``SYSTEM.md.example`` is
    documentation and is never loaded, and the last resort is Kimi's own built-in prompt, which
    the harness reaches by staging nothing at all.
    """
    path = root / SYSTEM_FILE
    return path if path.is_file() else None


def context_source(root: Path) -> Path | None:
    """The file the all-lane contract is read from: the operator's, else this harness's own.

    Both tiers are real files with real authority. ``CONTEXT.md.example`` is in neither.
    """
    operator = root / CONTEXT_FILE
    if operator.is_file():
        return operator
    default = root.joinpath(*CONTRACT_SOURCE)
    return default if default.is_file() else None


def _prepare(text: str, source: Path | str, values: Mapping[str, str] | None = None) -> str:
    """Comments out, harness placeholders in, edge newlines off - in that order.

    The check runs *before* substitution on purpose: a Kimi literal carries Kimi's own ``${cwd}``
    and friends, and those belong to Kimi to resolve at bind time. Anything still unresolved
    afterwards is a name the harness promised and could not deliver.
    """
    cleaned = strip_html_comments(text)
    check_placeholders(cleaned, source)
    resolved = substitute_harness_placeholders(cleaned, values)
    missing = [name for name in kimi_literal_names() if "${" + name + "}" in resolved]
    if missing:
        where = f"{source}: " if source else ""
        raise SystemExit(f"{where}${{{missing[0]}}} is unresolved - {COLD_CACHE}")
    return resolved.strip("\n")


def enabled_guidance(plan: dict[str, Any], audience: str, enabled: Mapping[str, bool]) -> list[str]:
    """The generated blocks for one audience that the operator left switched on.

    Sections are rendered one at a time rather than through ``policy.render_guidance`` so a block
    that is off costs nothing, and so the panel can measure each block alone.
    """
    return [
        policy.guidance_block(section)
        for section in policy.guidance_sections(plan, audience)
        if enabled.get(section.option, True)
    ]


def compose_agents_document(
    root: Path,
    plan: dict[str, Any],
    module_guidance: str,
    enabled: Mapping[str, bool] | None = None,
    values: Mapping[str, str] | None = None,
) -> str:
    """The all-lane contract: tier one or tier two, then every enabled all-lane add-on.

    The result is always written, even when it is empty, because Docker creates a missing bind
    source as a directory and that failure surfaces at container start rather than at render.
    """
    choices = resolve_enabled(enabled)
    parts: list[str] = []
    source = context_source(root)
    if source is not None:
        parts.append(_prepare(source.read_text(encoding="utf-8"), source, values))
    parts.extend(enabled_guidance(plan, policy.AUDIENCE_LANE, choices))
    if choices[OPTION_MODULE_GUIDANCE] and module_guidance.strip():
        parts.append(module_guidance.strip("\n"))
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def compose_system_document(
    root: Path,
    plan: dict[str, Any],
    enabled: Mapping[str, bool] | None = None,
    values: Mapping[str, str] | None = None,
) -> str:
    """The main agent's voice plus the main-only add-ons, following the four-row rule.

    +------------+-----------+--------------------------------------------------+
    | operator   | add-ons   | staged result                                      |
    +============+===========+==================================================+
    | absent     | any       | ``${base_prompt}`` wrapper plus the add-ons, or   |
    |            |           | nothing at all when there are none                |
    +------------+-----------+--------------------------------------------------+
    | non-empty  | any       | the file's own text plus the add-ons              |
    +------------+-----------+--------------------------------------------------+
    | empty      | on        | the add-ons alone, which are then the whole       |
    |            |           | prompt - a file that exists is a decision         |
    +------------+-----------+--------------------------------------------------+
    | empty      | off       | :data:`EMPTY_PROMPT_SENTINEL`                     |
    +------------+-----------+--------------------------------------------------+

    Row three is the one worth stating rather than inferring. Wrapping a deliberately emptied file
    would reintroduce the prompt the operator just declined, so its emptiness is honoured even
    though the consequence is that the add-ons become a complete prompt.
    """
    choices = resolve_enabled(enabled)
    additions = enabled_guidance(plan, policy.AUDIENCE_MAIN, choices)
    path = system_source(root)
    text = path.read_text(encoding="utf-8") if path is not None else None
    body = _prepare(text, path, values) if text is not None and text.strip() else ""
    parts = [part for part in [body, *additions] if part]
    if text is None:
        if not parts:
            return ""
        return "\n\n".join([BASE_PROMPT_WRAPPER, *parts]) + "\n"
    if not parts:
        return EMPTY_PROMPT_SENTINEL + "\n"
    return "\n\n".join(parts) + "\n"


#: The config keys the panel governs, and what each option switches them to. Only keys that are
#: *not* operator-owned in runtime/config-policy.json may be rewritten here: the initializer
#: re-pins those every launch anyway, while a rewrite of an operator-owned key would silently
#: clobber a choice the operator made, which the default-deny merge rule forbids.
CONFIG_TOGGLES = {
    OPTION_HARNESS_SKILLS: ("extra_skill_dirs", "[]"),
    OPTION_FULL_SKILL_LISTING: ("merge_all_available_skills", "false"),
    OPTION_PRODUCT_SKILLS: ("builtin_product_skills", "false"),
    OPTION_HARNESS_AGENTS: ("extra_agent_dirs", "[]"),
}
#: Env names the panel governs, and the value that switches the behaviour off. ``false`` is what
#: Kimi's own boolean-env parser reads as no, and an unset or blank value keeps the behaviour on.
ENV_TOGGLES = {OPTION_PERMISSION_BANNER: ("KIMI_CODE_PERMISSION_MODE_REMINDER", "false")}


def apply_config_toggles(config: str, enabled: Mapping[str, bool]) -> str:
    """Switch governed keys off in a rendered Kimi config, failing loudly if one has moved.

    A rewrite that silently does not happen is worse than one that raises: the option would read
    as off in the panel while the shipped behaviour stayed on.
    """
    for option, (key, off_value) in CONFIG_TOGGLES.items():
        if enabled.get(option, True):
            continue
        pattern = re.compile(rf"^{re.escape(key)} = .*$", re.MULTILINE)
        if not pattern.search(config):
            raise SystemExit(
                f"runtime/config.toml no longer declares {key}, which the "
                f"'{option}' panel option is supposed to switch off"
            )
        config = pattern.sub(f"{key} = {off_value}", config)
    return config


def apply_env_toggles(values: dict[str, str], enabled: Mapping[str, bool]) -> dict[str, str]:
    """The governed container environment, so the panel and compose agree."""
    for option, (name, off_value) in ENV_TOGGLES.items():
        values[name] = "true" if enabled.get(option, True) else off_value
    return values
