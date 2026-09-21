#!/usr/bin/env python3
"""Discover trusted harness modules and assemble session-only runtime assets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .definitions import ENV_VAR, ID
    from .private_file import write_private_json
    from .safe_workspace_init import UnsafeWorkspace, components, initialize
    from .tui import flow
    from .tui.app import View, run
    from .tui.input import FieldStep
    from .tui.menu import Choice, ListStep
else:
    from definitions import ENV_VAR, ID
    from private_file import write_private_json
    from safe_workspace_init import UnsafeWorkspace, components, initialize
    from tui import flow
    from tui.app import View, run
    from tui.input import FieldStep
    from tui.menu import Choice, ListStep

#: A module directory name is an ``ID``, and an environment key an ``ENV_VAR``; both patterns are
#: defined once in ``definitions.py`` because a name accepted there and rejected here would make
#: a valid module impossible to select.
#:
#: This step's own answers, kept by name so that a flow can replay the step without re-asking.
#: ``module.env`` holds the same values shell-quoted for the launcher, and a value is not the same
#: bytes on both sides of a quote.
VALUES_FILE = "module-values.json"


def discover(root):
    result = []
    directory = root / "modules"
    if not directory.exists():
        return result
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or not (path / "module.json").exists():
            continue
        if path.is_symlink() or not ID.fullmatch(path.name):
            raise ValueError(
                "Module directories must be real directories with lowercase identifiers"
            )
        # No links or devices in executable module content, including runtime contributions.
        for child in path.rglob("*"):
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise ValueError(f"Unsafe module entry: {child}")
        doc = json.loads((path / "module.json").read_text())
        if doc.get("schema_version") != 1 or not isinstance(doc.get("label"), str):
            raise ValueError(f"Invalid module manifest: {path.name}")
        if not doc["label"].strip() or any(ord(c) < 32 for c in doc["label"]):
            raise ValueError("Invalid module label")
        for relative in doc.get("workspace_directories", []):
            components(relative)
        if not (path / "module.sh").is_file():
            raise ValueError("Module requires module.sh")
        for image in doc.get("images", []):
            if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", image):
                raise ValueError("Invalid image name")
        for item in doc.get("environment", []):
            if not ENV_VAR.fullmatch(item["name"]) or not isinstance(item.get("prompt"), str):
                raise ValueError("Invalid module environment declaration")
        result.append({**doc, "id": path.name, "path": path})
    return result


def compatible(module):
    result = subprocess.run(
        ["bash", str(module["path"] / "module.sh"), "compatible"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f"Compatibility probe failed: {module['id']}")
    return result.returncode == 0


def ordered(modules, previous):
    return sorted(modules, key=lambda m: (m["id"] not in previous, m["label"].casefold(), m["id"]))


def choose(modules, previous, non_interactive, state=None):
    """Which modules to load, one fullscreen step.

    The short-circuits are the interface's, not an implementation detail: an explicit
    ``HARNESS_MODULES`` is an operator's answer and wins without asking, a non-interactive launch
    falls back to what was chosen last time, and neither may take a terminal over. Both are also
    the flow's business: a step that answers itself has no screen in it, and says so.
    """
    modules = ordered(modules, previous)
    checked = {m["id"] for m in modules if m["id"] in previous}
    override = os.environ.get("HARNESS_MODULES")
    if override is not None:
        checked = set(filter(None, override.split(",")))
        if checked - {m["id"] for m in modules}:
            raise ValueError("HARNESS_MODULES includes missing or incompatible modules")
        if state:
            state.skip(flow.MODULES)
        return [m["id"] for m in modules if m["id"] in checked]
    if not modules or non_interactive:
        if state:
            state.skip(flow.MODULES)
        return [m["id"] for m in modules if m["id"] in checked]
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "Module selection requires a terminal; use --non-interactive or HARNESS_MODULES"
        )
    # The flow numbers this step when one is running, because only it knows how many screens came
    # before. Alone, this is a lone step with no progress to report and the rail says nothing.
    if state:
        state.declare(flow.MODULES, 1)
    position, total = state.rail(flow.MODULES) if state else (1, 1)
    step = ListStep(
        title="Modules",
        prompt="Choose modules",
        choices=[Choice(m["id"], m["label"]) for m in modules],
        previous=[m["id"] for m in modules if m["id"] in checked],
    )
    result = run(
        step,
        View(
            position=position,
            total=total,
            can_go_back=bool(state and state.previous(flow.MODULES)),
        ),
    )
    if result.status == flow.GO_BACK:
        # This step is one screen and the previous one is another process's, so the request leaves
        # with the status the launcher's loop reads, and the flow is told where to land first.
        flow.back_from(state, flow.MODULES)
    if not result.accepted:
        raise SystemExit(result.status)
    return list(result.value)


def module_field_title(module, item):
    """The frame's label for one environment question.

    A declared value that is not a secret gets the same screen with an honest name, because
    calling a repository URL a secret would teach the user to distrust the word.
    """
    return f"{module['label']} {'secret' if item.get('secret', True) else 'value'}"


def ask_module_value(module, item, view):
    """Ask for one module environment value on the shared modal engine.

    The answer is kept exactly as typed. The line this replaces never stripped it, so a value of
    one space is a real value here and the field must not call it an empty answer - which is what
    ``whitespace_is_value`` is for.
    """
    name = item["name"]
    print(
        f"{module['label']}: add {name} to .env to persist it; this value is for this session only."
    )
    step = FieldStep(
        title=module_field_title(module, item),
        prompt=f"{item['prompt']} ({name})",
        head=(f"Add {name} to .env to persist it; this value is for this session only.",),
        masked=item.get("secret", True),
    )
    result = run(step, view)
    if result.status == flow.GO_BACK:
        raise flow.BackRequested(name)
    if not result.accepted:
        raise SystemExit(result.status)
    return str(result.value)


def write_json(path, value):
    """Stage JSON at mode 0600, atomically, through the launcher's only such writer."""
    write_private_json(path, value)


def module_agents_text(modules):
    """The module text that has to reach the agent, staged for the system prompt.

    Each selected module's own ``AGENTS.md`` is the guidance, verbatim under a heading naming the
    module. It is staged into the instance runtime directory rather than written into the
    workspace, because ``<workspace>/AGENTS.md`` belongs to the project being worked on.
    """
    return "\n".join(
        f"## Module: {module['label']}\n\n" + (module["path"] / "AGENTS.md").read_text()
        for module in modules
        if (module["path"] / "AGENTS.md").exists()
    )


def assemble(root, runtime, modules, workspace):
    target = runtime / "assets"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    mcp = {"mcpServers": {}}
    for category in ("skills", "agents", "tools"):
        (target / category).mkdir()
    for source in [root / "runtime", *[m["path"] / "runtime" for m in modules]]:
        for category in ("skills", "agents", "tools"):
            directory = (
                root / "tools"
                if source == root / "runtime" and category == "tools"
                else source / category
            )
            if directory.exists():
                for item in directory.iterdir():
                    if item.name == "__pycache__":
                        continue
                    dest = target / category / item.name
                    if dest.exists():
                        raise ValueError(f"Duplicate runtime asset: {category}/{item.name}")
                    if item.is_dir():
                        shutil.copytree(
                            item, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                        )
                    else:
                        shutil.copy2(item, dest)
        if (source / "mcp.json").exists():
            entries = json.loads((source / "mcp.json").read_text())["mcpServers"]
            if entries.keys() & mcp["mcpServers"].keys():
                raise ValueError("Duplicate MCP server names")
            mcp["mcpServers"].update(entries)
    write_json(target / "mcp.json", mcp)
    for module in modules:
        initialize(workspace, module.get("workspace_directories", []))
    if __package__:
        from .render_runtime import MODULE_GUIDANCE_FILE, write_secret
    else:
        from render_runtime import MODULE_GUIDANCE_FILE, write_secret

    write_secret(runtime / MODULE_GUIDANCE_FILE, module_agents_text(modules))


def reconcile_installed(runtime, modules):
    """Forget removed modules, and remove only their private disposable installations.

    Nothing is reclaimed while a modal flow is live without an explicit ``HARNESS_MODULES``, since
    Back may still put that module's answer in play again.
    """
    registry = runtime / "installed-modules.json"
    previous = json.loads(registry.read_text()) if registry.exists() else {}
    installed = {m["id"]: m.get("images", []) for m in modules}
    # Deferring rather than forgetting: an entry dropped from the registry is an image nobody will
    # ever remove again, and a rebuild the user can undo is the cheaper of the two mistakes.
    pending = flow.is_live(runtime) and "HARNESS_MODULES" not in os.environ
    deferred = {}
    for name, images in previous.items():
        if name in installed:
            continue
        if not ID.fullmatch(name):
            raise ValueError("Invalid stored module identifier")
        if pending:
            deferred[name] = images
            continue
        data = runtime / "module-data" / name
        if data.is_symlink():
            raise ValueError("Module data must not be a symlink")
        if data.exists():
            shutil.rmtree(data)
        for image in images:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", image):
                raise ValueError("Invalid stored image name")
            suffix = os.environ.get("HARNESS_IMAGE_SUFFIX")
            if not suffix:
                raise ValueError("HARNESS_IMAGE_SUFFIX must be set to remove a module image")
            tag = f"{image}:{suffix}"
            exists = (
                subprocess.run(
                    ["docker", "image", "inspect", tag],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                == 0
            )
            if exists:
                subprocess.run(
                    ["docker", "image", "rm", tag],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    write_json(registry, {**installed, **deferred})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["select", "environment", "assemble"])
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ["HARNESS_ROOT"])
    runtime = Path(os.environ["HARNESS_RUNTIME_DIR"])
    modules = discover(root)
    selection = runtime / "modules.json"
    if args.action == "select":
        previous = runtime / "last-modules.json"
        previous = json.loads(previous.read_text()) if previous.exists() else []
        state = flow.running(runtime)
        # The chosen list is its own record of the answer, so a replay reads it back rather than
        # asking again; re-running the command is still what re-publishes modules.list downstream.
        chosen = json.loads(selection.read_text()) if selection.exists() else None
        if state and chosen is not None and not state.should_render(flow.MODULES):
            selected = chosen
        else:
            candidates = [m for m in modules if compatible(m)]
            selected = choose(candidates, previous, args.non_interactive, state)
            write_json(selection, selected)
            if state:
                state.commit(flow.MODULES, ",".join(selected))
                # The version menu and the environment questions are asked *of* this list, so a
                # change here makes any answer they gave belong to modules the user no longer has.
                if chosen is not None and chosen != selected:
                    state.forget(flow.MODULE_VERSION)
                    state.forget(flow.MODULE_VALUES)
        if state and not selected:
            # Nothing selected, so no module that could be asked for a version. A step that renders
            # nothing has to say so, or it stays on the rail uncounted and every ``of N`` above it
            # is one too high.
            state.plan(flow.MODULE_VERSION, 0)
        reconcile_installed(runtime, modules)
        # Shell consumes identifiers only, never labels or arbitrary manifest strings.
        (runtime / "modules.list").write_text("".join(name + "\n" for name in selected))
        return
    selected = json.loads(selection.read_text())
    modules = [next(m for m in modules if m["id"] == name) for name in selected]
    if args.action == "assemble":
        assemble(root, runtime, modules, Path(os.environ["HARNESS_WORKSPACE"]))
        return
    if __package__:
        from .env_values import read_env_values
        from .render_runtime import write_secret
    else:
        from env_values import read_env_values
        from render_runtime import write_secret
    import shlex

    values = read_env_values(Path(os.environ["HARNESS_RESOLVED_BOOTSTRAP"]))
    declared = {
        key.removeprefix("export "): value for key, value in read_env_values(root / ".env").items()
    }
    # Every declaration is kept, in order, because the agent overlay is per declaration rather than
    # per name: two modules may name one variable and only the second may ask for it in the agent.
    pairs = [(module, item) for module in modules for item in module.get("environment", [])]
    state = flow.running(runtime)
    # A replayed pass reads its own answers back rather than re-asking, so that walking the flow
    # forward again costs no keystrokes and cannot lose what the last pass wrote. They come from a
    # file of this step's rather than from ``module.env``, because that one is shell-quoted for the
    # launcher and an answer is not the same bytes on both sides of a quote.
    replaying = bool(state) and not state.should_render(flow.MODULE_VALUES)
    record = runtime / VALUES_FILE
    carried = json.loads(record.read_text()) if replaying and record.exists() else {}
    # Two modules naming one unset variable are one question, not two, as before - but the
    # questions are counted up front now, because the step rail has to say which answer of how
    # many the user is giving.
    pending = []
    seen = set()
    for module, item in pairs:
        name = item["name"]
        if name in seen or name in carried:
            continue
        if name in declared and values.get(name, ""):
            continue
        seen.add(name)
        pending.append((module, item))
    if state and not replaying:
        state.report(flow.MODULE_VALUES, len(pending))
    answers = dict(carried)
    index = 0
    while index < len(pending):
        module, item = pending[index]
        name = item["name"]
        if args.non_interactive or not sys.stdin.isatty():
            raise ValueError(
                f"{module['label']} requires {name} in .env for non-interactive startup"
            )
        try:
            position, total = (
                state.rail(flow.MODULE_VALUES, index) if state else (index + 1, len(pending))
            )
            answers[name] = ask_module_value(
                module,
                item,
                View(
                    position=position,
                    total=total,
                    label=module_field_title(module, item),
                    can_go_back=index > 0 or bool(state and state.previous(flow.MODULE_VALUES)),
                ),
            )
        except flow.BackRequested:
            if index == 0:
                # The first question has nothing earlier inside this step to land on, so Back means
                # the previous step, which is another process's.
                flow.back_from(state, flow.MODULE_VALUES)
            index -= 1
            # The landing question's own answer goes with the later ones. Answers are stored per
            # variable name, so keeping it would let the sequence walk past the very step the user
            # asked to return to.
            for answered in pending[index:]:
                answers.pop(answered[1]["name"], None)
            continue
        index += 1
    session = {}
    agent = {}
    # A second pass over every declaration, so that backing up cannot leave the agent overlay
    # holding the value of a question the user has just asked to re-answer.
    for _, item in pairs:
        name = item["name"]
        value = (values.get(name, "") if name in declared else "") or answers.get(name, "")
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("Environment values must be single-line")
        session[name] = value
        if item.get("agent", False):
            agent[name] = value.replace("$", "$$")
    write_json(
        runtime / "compose/module-environment.json",
        {"services": {"kimi-agent": {"environment": agent}}},
    )
    write_secret(
        runtime / "module.env", "".join(f"{k}={shlex.quote(v)}\n" for k, v in session.items())
    )
    # The answers, kept by name for the next pass of the flow to read back. The record is only
    # written when a flow is running: outside one there is no next pass, and a file nothing reads
    # would hold values for the life of the instance directory.
    if state and answers:
        write_json(record, answers)
        if not replaying:
            # Names, never values: the recap line says what was asked, and the values are already in
            # the two files that use them.
            state.commit(flow.MODULE_VALUES, " ".join(sorted(answers)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (
        ValueError,
        OSError,
        StopIteration,
        UnsafeWorkspace,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(f"Module setup refused: {error}") from None
