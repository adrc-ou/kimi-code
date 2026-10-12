#!/usr/bin/env python3
"""Ask whether this launch should shrink multimodal attachments out of request context.

The question is one boolean, and it is asked here rather than assumed because the answer trades
two things the operator cares about in opposite directions. Inline images are re-sent on every
later step of the session that produced them — measured on one real archived request, 92.4% of
the body was base64 — so evicting them once answered keeps long image sessions inside whatever
the gateway will accept. It also means the model sees a handle where it once saw pixels, and has
to ask for them back through ``analyze_image`` to look again. An operator working on one dense
scan may prefer the bytes stay put.

Three sources decide what a launch opens on, in the order an operator's intent hardens: the answer
given at this screen last launch, then a ``MODEL_PROXY_IMAGE_EVICTION`` declared in :file:`.env`,
then **No**. The screen names which of the three it used. The declaration is the control for the
launches nobody is asked about -- ``--non-interactive``, a read-only entry point, a machine
answering its first question -- while the click is the later and more specific act, so it outranks
the line that every copied :file:`.env.example` carries. That ranking has to be said out loud: an
operator who set the value and watched the proxy report it off had no way to tell whether the file
was being ignored or their own earlier click was winning.

The answer is written to :file:`image-eviction.env`, which the launcher exports and sources and
:file:`compose.yaml` interpolates into the proxy container -- the same variable by name at every
hop, so no mapping table stands between what was answered and what is enforced. With no remembered
answer and no declaration, it is **No**: the feature changes what the model sees, and an
unlooked-at default that alters model input is the kind of thing an operator should opt into once
rather than inherit silently.

:file:`last-image-eviction.json` is copied into the instance directory at the launcher's commit
point beside the other remembered answers, so an abandoned launch cannot rewrite a preference
nobody confirmed. The answer is committed to the flow state as well, which is what lets a launch
walking backwards stop here instead of stepping over it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .env_values import read_env_values
    from .tui import flow, screen
    from .tui.app import View, run
    from .tui.menu import SINGLE, Choice, ListStep
else:  # run as a script by the launcher, which puts ``tools/`` alone on the path
    from env_values import read_env_values
    from tui import flow, screen
    from tui.app import View, run
    from tui.menu import SINGLE, Choice, ListStep

TITLE = "Multimodal attachments"
PROMPT = "Use smart context reduction strategy for multimodal attachments?"

#: What the launcher and :file:`compose.yaml` agree on. Named here so the screen and the
#: enforced variable cannot drift apart.
ENV_VARIABLE = "MODEL_PROXY_IMAGE_EVICTION"
#: The `mcpServers` key registered in ``runtime/mcp.json``. Named here so the launcher and that
#: file cannot drift into gating a server that no longer exists.
SERVER_KEY = "images"
#: The operator's own configuration file, read for one key and never printed. Same reader
#: :mod:`tools.modules` uses to see what was declared before the launcher overrode it.
OPERATOR_ENV = ".env"
CHOSEN_FILE = "image-eviction.json"
PREVIOUS_FILE = "last-image-eviction.json"
OUTPUT_ENV = "image-eviction.env"

ON = "on"
OFF = "off"

#: The first answer, for an operator who has never been asked. See the module docstring.
FIRST_DEFAULT = OFF

#: Which decision the opening default came from, said out loud on the screen and in the replay
#: line. Without it a declared ``.env`` value that a remembered answer outranks is simply missing,
#: and the only way to notice is to debug the proxy.
SOURCE_REMEMBERED = "your last launch's answer"
SOURCE_DECLARED = "your .env declaration"
SOURCE_BUILTIN = "this build's default"

CHOICES = (
    Choice(
        id=ON,
        label="Yes",
        hint="evict answered images; re-read them by handle",
    ),
    Choice(
        id=OFF,
        label="No",
        hint="send every attachment in full on every step",
    ),
)

EXPLAINED = {
    ON: (
        "Once the model has answered about an image, its base64 is replaced in the "
        "outbound request by a handle, and the `analyze_image` tool can return the "
        "picture again later. Each image is rewritten at most once, so the prompt "
        "prefix stays stable and the gateway's cache keeps working."
    ),
    OFF: (
        "Requests carry every attachment exactly as Kimi assembled them, on every step. "
        "Nothing is hidden from the model, and a long image-heavy session grows its "
        "request body until the endpoint refuses it."
    ),
}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def answer_of(value: object) -> str:
    """Normalise a stored or supplied answer to one of the two ids."""
    if isinstance(value, bool):
        return ON if value else OFF
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {ON, OFF}:
            return text
        if text in {"1", "true", "yes", "on"}:
            return ON
        if text in {"0", "false", "no", "off"}:
            return OFF
    return ""


def remembered(runtime: Path) -> str:
    """The answer from the last completed launch, or an empty string if there was none."""
    stored = read_json(runtime / PREVIOUS_FILE, {})
    if isinstance(stored, dict):
        return answer_of(stored.get("eviction"))
    # A bare "1"/"0" written by hand is honoured too, because this file is small enough that
    # an operator will sometimes just set it.
    return answer_of(stored if isinstance(stored, (bool, str)) else "")


def declared(root: Path) -> str:
    """What :file:`.env` says about this one variable, or an empty string when it says nothing.

    Read for this key alone and never printed: the same file holds the provider credentials, which
    is why every launcher step goes through :func:`tools.env_values.read_env_values` instead of
    sourcing it.
    """
    try:
        values = read_env_values(root / OPERATOR_ENV)
    except OSError:
        return ""
    return answer_of(values.get(ENV_VARIABLE, ""))


def opening(runtime: Path, root: Path) -> tuple[str, str]:
    """The answer a launch starts on, and the name of whatever chose it.

    Three sources, ordered by how recently the operator committed to them. An answer they gave the
    screen beats a value written in :file:`.env`, because clicking is the later and more specific
    act and :file:`.env.example` ships this key declared — an example file must not be able to
    overrule a decision somebody actually made. A declaration still beats nothing, which is what
    makes :file:`.env` a working control for the launches nobody asks about: ``--non-interactive``,
    a read-only entry point, a machine answering its first question.
    """
    answer = remembered(runtime)
    if answer:
        return answer, SOURCE_REMEMBERED
    answer = declared(root)
    if answer:
        return answer, SOURCE_DECLARED
    return FIRST_DEFAULT, SOURCE_BUILTIN


def choose(
    previous: str, non_interactive: bool, view: View | None = None, source: str = ""
) -> str:
    default = previous or FIRST_DEFAULT
    if non_interactive or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return default
    head = [
        "Large attachments dominate a long session's request body. Once the model has",
        "answered about an image, this option replaces its bytes with a re-readable handle.",
    ]
    # Said on the screen rather than left to inference: a declaration quietly outranked by a
    # remembered answer is indistinguishable from a declaration that does nothing.
    if source:
        head.append(f"Opening on {source}.")
    step = ListStep(
        title=TITLE,
        prompt=PROMPT,
        mode=SINGLE,
        choices=[
            Choice(
                id=choice.id,
                label=choice.label,
                hint=choice.hint,
                checked=choice.id == default,
            )
            for choice in CHOICES
        ],
        previous=[default],
        head=tuple(head),
    )
    result = run(step, view if view is not None else View())
    if result.status == flow.GO_BACK:
        raise flow.BackRequested()
    if not result.accepted:
        raise SystemExit(result.status)
    return answer_of(result.value) or default


#: Appended to the all-lane contract when the operator opted in, so every agent meets this
#: before its first attachment rather than discovering it from a note in mid-conversation.
#: Deliberately states the failure mode to avoid, not just the mechanism: an agent that knows
#: pixels may vanish but not that its own prose is what preserves them works differently.
GUIDANCE = """\
## Multimodal attachments are context-trimmed in this session

Inline images are large enough to dominate a request — nine tenths of a real archived
session's body was base64 picture — so once an assistant turn has produced prose after an
image, the proxy replaces that image's bytes in the outbound request with a short notice
carrying a handle. This is invisible to you as a user action; it changes only what later
requests carry.

What that means in practice:

- **Your own text is the durable record.** Anything you write out from an image survives;
  anything you only looked at and did not describe is no longer in front of you. When you read
  an image, write down what you will need later, in as much detail as you will need it.
- **The picture is not gone, only unfolded.** The notice names the tool that folds it back
  out: call `analyze_image` with the handle from the notice and a specific question, and you
  receive the image again with your question attached. Its `list_images` sibling shows every
  image this project can still return.
- **Ask once, precisely.** A re-read costs a round trip and the image will be trimmed again
  afterwards, so ask the question you actually need answered rather than browsing.
- **An image nobody has written about yet is still before you.** Trimming looks backwards from
  prose: an image is folded away only once some assistant turn has said something in text after
  it. A reply that is only a tool call does not count, so the attachment you were just given is
  genuinely still there while you are working on it.
- **But nothing checks *which* image a later sentence was about.** Any prose after a picture is
  enough to fold that picture away, even prose answering a different question entirely. So the
  habit that keeps you informed is: read the image, and write down what you took from it,
  before you move on to something else.
"""


def guidance_for(answer: str) -> str:
    """The contract text for one answer — empty when the operator declined.

    The gate is here rather than at each caller so the file written for
    ``render_runtime.py`` and any future consumer are produced by the same rule.
    """
    return GUIDANCE if answer == ON else ""


#: The all-lane contract fragment render_runtime.py appends, mirroring how module guidance
#: reaches the session. Written empty when declined: an absent file and an empty one both add
#: nothing, and the empty file says which of the two happened.
GUIDANCE_FILE = "multimodal-eviction.md"


def record(runtime: Path, answer: str) -> None:
    """Write this launch's answer, and the environment line the launcher sources."""
    payload = {"eviction": answer, "explained": EXPLAINED[answer]}
    (runtime / CHOSEN_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (runtime / OUTPUT_ENV).write_text(
        f"{ENV_VARIABLE}={1 if answer == ON else 0}\n", encoding="utf-8"
    )
    # The explanation and the behaviour are written from the same call, so a session can never
    # be told about a reduction it is not getting, or left to discover one it is.
    (runtime / GUIDANCE_FILE).write_text(guidance_for(answer), encoding="utf-8")
    gate_server(runtime, answer == ON)


def gate_server(runtime: Path, present: bool) -> None:
    """Add or drop the `images` MCP server in the staged config, matching the answer.

    An agent offered a re-read tool for a reduction that is not happening would call it for
    nothing, and the tool's own description would be a false statement about the session. The
    staged file is the one the container mounts, and ``tools/modules.py assemble`` has already
    written it by the time this step runs.
    """
    path = runtime / "assets" / "mcp.json"
    if not path.is_file():
        # Nothing staged yet (a read-only entry point, or an order change): the shipped
        # registration stands, and the answer is still honoured for the proxy itself.
        return
    document = read_json(path, None)
    if not isinstance(document, dict) or SERVER_KEY not in (document.get("mcpServers") or {}):
        return
    servers = document["mcpServers"]
    if present:
        servers[SERVER_KEY]["enabled"] = True
    else:
        del servers[SERVER_KEY]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get("HARNESS_RUNTIME_DIR", ""),
        help="instance runtime directory the answer is recorded in",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("HARNESS_ROOT", ""),
        help="harness root whose .env declares the answer for a launch nobody is asked about",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="report the answer, write nothing",
    )
    args = parser.parse_args(argv)

    if not args.runtime_dir:
        parser.error("--runtime-dir or HARNESS_RUNTIME_DIR is required")
    runtime = Path(args.runtime_dir)
    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]

    standing, source = opening(runtime, root)
    if args.print_only:
        print(standing)  # noqa: S106 - an operator-facing report, not a credential
        return 0

    # A step in the launch like the eight around it, and the flow state is what makes it one. The
    # rail has to be able to say which of the nine this is, and the answer has to be committed or
    # Back from the credential step walks straight past it -- the hole `tools/prompt_panel.py`
    # documents for the context step, in the step launched beside it.
    asking = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
    state = flow.running(runtime)
    rendering = state.plan(flow.EVICTION, 1 if asking else 0) if state else asking
    if rendering:
        position, total = state.rail(flow.EVICTION) if state else (1, 1)
        try:
            answer = choose(
                standing,
                non_interactive=False,
                view=View(
                    position=position,
                    total=total,
                    can_go_back=bool(state and state.previous(flow.EVICTION)),
                ),
                source=source,
            )
        except flow.BackRequested:
            # Leaving without naming the target is how a back-request goes quiet: the next pass
            # would replay every committed step, redraw nothing, and arrive back here.
            flow.back_from(state, flow.EVICTION)
    else:
        # An unattended launch, or a pass replaying an answer this launch already has. Nothing here
        # may take over a screen that is not a terminal, and the line says which source answered.
        screen.note(f"Multimodal attachments: {standing}, from {source}.")
        answer = choose(standing, non_interactive=True)

    # Recorded on every path, including the ones that never drew a screen: this file is the only
    # thing between the operator and `${MODEL_PROXY_IMAGE_EVICTION:-0}` in `compose.yaml`, and the
    # launcher sources it as a step that must have produced its output before returning.
    runtime.mkdir(parents=True, exist_ok=True)
    record(runtime, answer)
    if state and rendering:
        # Only a screen that was actually drawn commits. An answer that skipped its own screens is
        # a step with nothing on the rail, and committing it would restore what `plan(..., 0)` just
        # took off -- the rule `tools/models.py` applies to the model lanes.
        state.commit(flow.EVICTION, answer)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except flow.BackRequested:
        # Exit 3 is the launcher's `flow_back`, which is how a step says "go back one screen"
        # across a process boundary.
        raise SystemExit(flow.GO_BACK) from None
