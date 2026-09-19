#!/usr/bin/env python3
"""Discover trusted harness modules and assemble session-only runtime assets."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import termios
import tty
from pathlib import Path

if __package__:
    from .definitions import ENV_VAR, ID
    from .private_file import write_private_json
    from .safe_workspace_init import UnsafeWorkspace, components, initialize
else:
    from definitions import ENV_VAR, ID
    from private_file import write_private_json
    from safe_workspace_init import UnsafeWorkspace, components, initialize

#: A module directory name is an ``ID``, and an environment key an ``ENV_VAR``; both patterns are
#: defined once in ``definitions.py`` because a name accepted there and rejected here would make
#: a valid module impossible to select.


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


def choose(modules, previous, non_interactive):
    modules = ordered(modules, previous)
    checked = {m["id"] for m in modules if m["id"] in previous}
    override = os.environ.get("HARNESS_MODULES")
    if override is not None:
        checked = set(filter(None, override.split(",")))
        if checked - {m["id"] for m in modules}:
            raise ValueError("HARNESS_MODULES includes missing or incompatible modules")
        return [m["id"] for m in modules if m["id"] in checked]
    if not modules or non_interactive:
        return [m["id"] for m in modules if m["id"] in checked]
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "Module selection requires a terminal; use --non-interactive or HARNESS_MODULES"
        )
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    focus = 0
    try:
        tty.setcbreak(fd)
        print("Choose modules: ↑/↓ move, Space toggle, Enter continue", flush=True)
        while True:
            for i, module in enumerate(modules):
                print(
                    f"\033[2K{'>' if focus == i else ' '} "
                    f"[{'X' if module['id'] in checked else ' '}] {module['label']}",
                    flush=True,
                )
            key = os.read(fd, 1).decode()
            if key in ("\r", "\n"):
                break
            if key in ("\x03", "\x04", ""):
                raise KeyboardInterrupt
            if key == "\x1b":
                import select

                if select.select([fd], [], [], 0.1)[0]:
                    sequence = os.read(fd, 2)
                    if sequence == b"[A":
                        focus = (focus - 1) % len(modules)
                    if sequence == b"[B":
                        focus = (focus + 1) % len(modules)
            elif key == " ":
                name = modules[focus]["id"]
                checked.symmetric_difference_update({name})
            print(f"\033[{len(modules)}A", end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return [m["id"] for m in modules if m["id"] in checked]


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
    """Forget removed modules and remove only their private disposable installations."""
    registry = runtime / "installed-modules.json"
    previous = json.loads(registry.read_text()) if registry.exists() else {}
    installed = {m["id"]: m.get("images", []) for m in modules}
    for name, images in previous.items():
        if name in installed:
            continue
        if not ID.fullmatch(name):
            raise ValueError("Invalid stored module identifier")
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
    write_json(registry, installed)


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
        selected = choose([m for m in modules if compatible(m)], previous, args.non_interactive)
        write_json(selection, selected)
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
        key.removeprefix("export "): value
        for key, value in read_env_values(root / ".env").items()
    }
    session = {}
    agent = {}
    for module in modules:
        for item in module.get("environment", []):
            name = item["name"]
            value = session.get(name, values.get(name, "") if name in declared else "")
            if not value:
                if args.non_interactive or not sys.stdin.isatty():
                    raise ValueError(
                        f"{module['label']} requires {name} in .env for non-interactive startup"
                    )
                print(
                    f"{module['label']}: add {name} to .env to persist it; "
                    "this value is for this session only."
                )
                while not value:
                    prompt = f"{item['prompt']} ({name}): "
                    value = getpass.getpass(prompt) if item.get("secret", True) else input(prompt)
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
