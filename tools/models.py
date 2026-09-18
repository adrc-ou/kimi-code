#!/usr/bin/env python3
"""Choose the primary and subagent models, resolve provider policy, and publish the plan.

Run from ``start.sh`` before the Kimi Code version selection:

    tools/models.py select     # two interactive prompts -> model-selection.json
    tools/models.py resolve    # definitions + selection -> model-policy.json and friends

Everything the rest of the launcher needs about models then comes from ``model-policy.json``,
which is derived rather than configured: the proxy mounts it as its enforcement input, Kimi's
model tables are rendered from it, and the runtime envelope appended to the system prompt is
generated from it. No number in ``.env`` describes a model, and nothing here writes into the
workspace: that file belongs to the project Kimi is working on.

Model and provider directories are operator-supplied configuration that this launcher reads in
order to send a credential to an endpoint, so they are treated as trusted input: symlinks and
non-regular files inside a definition tree are refused rather than sanitised.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
import termios
import tomllib
import tty
from pathlib import Path
from typing import Any

if __package__:
    from . import model_config, policy
    from .definitions import DefinitionError, load_definitions
    from .env_values import read_env_values
else:
    import model_config
    import policy
    from definitions import DefinitionError, load_definitions
    from env_values import read_env_values

LANES = ("primary", "long", "subagent")
SELECTABLE = ("primary", "subagent")
SELECTION = "model-selection.json"
PREVIOUS_SELECTION = "last-model-selection.json"
POLICY_FILE = "model-policy.json"
MODEL_ENV = "model.env"
COMPOSE_FRAGMENT = "compose/models.json"
CREDENTIALS_DIR = "credentials"
PROMPTS = {"primary": "Primary agent model", "subagent": "Subagent model"}
#: Both failure families mean "this selection cannot be served", and both must print one
#: clean line rather than a traceback, because start.sh surfaces the launcher's stderr.
Refusal = (DefinitionError, policy.ResolutionError, OSError, ValueError)


def write_text(path: Path, text: str) -> None:
    """Publish a session artifact atomically, readable only by its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    temporary.replace(path)
    path.chmod(0o600)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is None:
            raise DefinitionError(f"{path.name} is missing; run tools/models.py select first")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DefinitionError(f"cannot read {path}: {exc}") from exc


def load_plan(runtime: Path) -> dict[str, Any]:
    """Read the resolved plan. ``render_runtime.py`` and the proxy consume the same document."""
    plan = read_json(runtime / POLICY_FILE)
    if not isinstance(plan, dict) or plan.get("schema_version") != policy.SCHEMA_VERSION:
        raise DefinitionError(f"{POLICY_FILE} is not a schema {policy.SCHEMA_VERSION} plan")
    return plan


def reserved_context_size(root: Path) -> int:
    """Read Kimi's reserved output budget, which every lane must leave room for.

    This lives in the runtime template rather than in a model or provider definition because it
    describes how Kimi paces its own compaction, not what the model is or what the provider
    permits.
    """
    config = root / "runtime" / "config.toml"
    try:
        with config.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DefinitionError(f"cannot read {config}: {exc}") from exc
    value = document.get("loop_control", {}).get("reserved_context_size")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DefinitionError(
            "runtime/config.toml needs a positive loop_control.reserved_context_size"
        )
    return value


def selectable(models: list[dict[str, Any]], lane: str) -> list[dict[str, Any]]:
    """Definitions usable for one role, alphabetised by the name the operator reads.

    A model whose provider definition is absent never appears here, because ``load_definitions``
    only returns models naming a provider that exists: deleting a provider directory removes
    its models from both pickers in one step.
    """
    return sorted(
        (model for model in models if lane in model["lanes"]),
        key=lambda model: (model["label"].casefold(), model["id"]),
    )


def choose(
    lane: str,
    choices: list[dict[str, Any]],
    previous: str,
    *,
    non_interactive: bool,
    override: str,
) -> str:
    """Single-choice picker, matching the module picker's keys and terminal handling."""
    label = PROMPTS[lane]
    if not choices:
        raise DefinitionError(f"no available model declares a {lane} lane")
    by_id = {model["id"]: model for model in choices}
    if override:
        if override not in by_id:
            raise DefinitionError(
                f"HARNESS_{lane.upper()}_MODEL={override!r} is not an available {lane} model; "
                f"choose one of {', '.join(sorted(by_id))}"
            )
        return override
    default = previous if previous in by_id else choices[0]["id"]
    if non_interactive or len(choices) == 1:
        return default
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DefinitionError(
            f"choosing the {label.lower()} needs a terminal; set HARNESS_{lane.upper()}_MODEL "
            "or pass --non-interactive"
        )

    index = next(position for position, model in enumerate(choices) if model["id"] == default)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        print(f"Choose the {label}: up/down move, Enter continue", flush=True)
        while True:
            for position, model in enumerate(choices):
                marks = []
                if previous and model["id"] == previous:
                    marks.append("last used")
                elif model["id"] == default:
                    marks.append("default")
                suffix = f" ({'; '.join(marks)})" if marks else ""
                print(
                    f"\033[2K{'>' if position == index else ' '} {model['label']} "
                    f"[{model['provider']}]{suffix}",
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
                        index = (index - 1) % len(choices)
                    if sequence == b"[B":
                        index = (index + 1) % len(choices)
            print(f"\033[{len(choices)}A", end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return choices[index]["id"]


def bootstrap_values(root: Path) -> dict[str, str]:
    """Resolved operator configuration, read the way the rest of the launcher reads it."""
    configured = os.environ.get("HARNESS_RESOLVED_BOOTSTRAP", "")
    path = Path(configured) if configured else root / ".env"
    if not path.is_file():
        return {}
    return read_env_values(path)


def apply_endpoint_overrides(
    providers: dict[str, dict[str, Any]], values: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Let ``.env`` redirect a provider, while the definition keeps the documented default."""
    resolved = {}
    for provider_id, provider in providers.items():
        entry = dict(provider)
        name = provider["base_url_env"]
        if name and values.get(name, "").strip():
            entry["base_url"] = values[name].strip().rstrip("/")
        resolved[provider_id] = entry
    return resolved


#: Names the launcher itself needs out of ``.env``, beyond any one provider's definitions.
HARNESS_BOOTSTRAP_NAMES = ("KIMI_BACKGROUND_TASK_SLOTS", "KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE")
BOOTSTRAP_COMPOSE = "compose.bootstrap.yaml"


def bootstrap_names(plan: dict[str, Any]) -> set[str]:
    """Every ``.env`` variable name the selected definitions ask the launcher to read.

    A name left empty is not a name: that credential is prompt-only, and asking Compose to
    interpolate an empty variable would make the declaration file look like the thing refusing
    to launch.
    """
    names = {name for name in HARNESS_BOOTSTRAP_NAMES if name}
    for provider in plan["providers"].values():
        if provider.get("base_url_env"):
            names.add(str(provider["base_url_env"]))
        for credential in (provider.get("credentials") or {}).values():
            if credential.get("env"):
                names.add(str(credential["env"]))
    for lane in plan["lanes"].values():
        # The model-scoped name is required whether or not it won this session: Compose drops a
        # name it never declared, and the model would silently fall back to the provider key.
        if lane.get("key_env"):
            names.add(str(lane["key_env"]))
    return names


def require_bootstrap_declarations(root: Path, plan: dict[str, Any]) -> None:
    """Refuse a definition whose ``.env`` name the launcher never resolves.

    Compose interpolates only the variables a file declares, so a provider naming a variable
    that ``compose.bootstrap.yaml`` omits would have that variable silently dropped - the
    operator sets a key in ``.env`` and the launcher behaves as if it were empty. Checked here,
    at the one point where both the definitions and the declaration file are known, so the fix
    is one line in one file instead of a debugging session. A workspace without that file (a
    fixture tree, or a direct tool run outside the launcher) has nothing to check against.
    """
    path = root / BOOTSTRAP_COMPOSE
    if not path.is_file():
        return
    declared = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not line.startswith(" ") or stripped.startswith("#") or ":" not in line:
            continue
        declared.add(line.split(":", 1)[0].strip())
    missing = sorted(bootstrap_names(plan) - declared)
    if missing:
        raise DefinitionError(
            f"{BOOTSTRAP_COMPOSE} must declare "
            f"{', '.join(missing)} under bootstrap.environment so the launcher can read "
            "them from .env"
        )


def cmd_select(root: Path, runtime: Path, *, non_interactive: bool) -> int:
    _, models = load_definitions(root)
    for lane in SELECTABLE:
        if not selectable(models, lane):
            raise DefinitionError(
                f"no available model declares a {lane} lane; add one under models/<id>/model.toml "
                "with a provider that exists in ./providers"
            )
    previous = read_json(runtime / PREVIOUS_SELECTION, {})
    if not isinstance(previous, dict):
        previous = {}
    selection: dict[str, str] = {}
    for lane in SELECTABLE:
        choices = selectable(models, lane)
        chosen = choose(
            lane,
            choices,
            str(previous.get(lane, "")),
            non_interactive=non_interactive,
            override=os.environ.get(f"HARNESS_{lane.upper()}_MODEL", "").strip(),
        )
        selection[lane] = chosen
        model = next(item for item in choices if item["id"] == chosen)
        suffix = " (last used)" if previous.get(lane) == chosen else ""
        print(f"{PROMPTS[lane]}: {model['label']} [{model['provider']}]{suffix}")
    write_json(runtime / SELECTION, selection)
    return 0


def used_credentials(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Every credential the selected lanes actually use, de-duplicated by secret name.

    Which entries that is depends on how the operator scoped their keys. Two models naming one
    credential collapse into one entry, which is key sharing; a model that declared a
    ``key_env`` with a value in the environment got a credential of its own at resolution, so
    it appears separately and its key never reaches the other model. A lane can name only one
    credential, so one lane always has exactly one upstream identity either way.
    """
    seen: dict[str, dict[str, str]] = {}
    for lane in plan["lanes"].values():
        provider_id = lane["provider"]
        credential = lane["credential"]
        secret = f"{provider_id}__{credential}"
        if secret in seen:
            continue
        declaration = plan["providers"][provider_id]["credentials"][credential]
        seen[secret] = {
            "secret": secret,
            "env": declaration["env"],
            "prompt": declaration["prompt"],
            "label": declaration["label"],
            "key_url": declaration.get("key_url", ""),
            "provider": provider_id,
            "provider_label": plan["providers"][provider_id]["label"],
            # Set only for a model-scoped credential: the variable that key fell back from,
            # which the operator may equally well have filled in.
            "fallback_env": declaration.get("fallback_env", ""),
        }
    return [seen[key] for key in sorted(seen)]


def key_scopes(item: dict[str, str]) -> str:
    """Name the variables that would satisfy one key, saying what each one covers.

    A model-scoped key has two answers and the operator should not have to know which one the
    definition preferred, so both names are spelled out with their scope attached.
    """
    if not item["fallback_env"]:
        return item["env"]
    return (
        f"{item['env']} for this model alone, or {item['fallback_env']} for every model "
        f"of {item['provider_label']}"
    )


def materialise_credentials(plan: dict[str, Any], runtime: Path, values: dict[str, str]) -> None:
    """Write one mode-0600 file per used credential, ready to mount as a Docker secret.

    A value comes from the resolved bootstrap environment when the operator persisted it there,
    and is otherwise asked for once, for this session only - asked for *that model*, since a
    model that declared its own variable gets its own file whatever the answer is. A prompted
    value is never written back to ``.env`` or to any launcher environment file, and no
    credential value ever reaches the agent container: only the proxy mounts these paths, and
    the launcher deletes them when it exits.
    """
    directory = runtime / CREDENTIALS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    wanted = set()
    for item in used_credentials(plan):
        secret = values.get(item["env"], "").strip()
        if not secret:
            where = f" (issued at {item['key_url']})" if item["key_url"] else ""
            if not sys.stdin.isatty():
                raise DefinitionError(
                    f"{item['provider_label']} needs a key for {item['label']}{where}: set "
                    f"{key_scopes(item)} in .env for non-interactive startup"
                )
            print(
                f"{item['provider_label']}: {item['label']}{where}. Set {key_scopes(item)} in "
                ".env to persist it; this value is for this session only."
            )
            while not secret:
                secret = getpass.getpass(f"{item['prompt']} ({item['env']}): ").strip()
        if any(character in secret for character in ("\n", "\r", "\x00")):
            raise DefinitionError("credential values must be single-line")
        target = directory / item["secret"]
        target.unlink(missing_ok=True)
        write_text(target, secret)
        wanted.add(item["secret"])
    # A credential left behind by a deselected model must not stay mountable.
    for existing in directory.iterdir():
        if existing.is_file() and existing.name not in wanted:
            existing.unlink()


def compose_fragment(plan: dict[str, Any], runtime: Path) -> dict[str, Any]:
    """Attach exactly the credentials this selection uses to the proxy service.

    Generated because the set of secrets depends on the models chosen, which keeps provider and
    credential names out of ``compose.yaml`` and the mount-hygiene checks selection-independent.
    """
    used = used_credentials(plan)
    return {
        "services": {"model-proxy": {"secrets": [item["secret"] for item in used]}},
        "secrets": {
            item["secret"]: {"file": str(runtime / CREDENTIALS_DIR / item["secret"])}
            for item in used
        },
    }


def model_environment(plan: dict[str, Any], values: dict[str, str]) -> dict[str, str]:
    """Launcher-visible facts derived from the plan, so nothing has to be set twice.

    Only the two numbers compose actually interpolates belong here; the lane aliases reach
    Kimi through the rendered runtime config and the plan, not through the shell.

    ``KIMI_SUBAGENT_CONCURRENCY`` is the largest fan-out the selected models' provider rules
    allow, which is also what the proxy enforces, so Kimi fills its envelope exactly instead
    of hoping two numbers agree.
    """
    subagent_limit = plan["limits"].get("subagent_concurrency") or 1
    slots = values.get("KIMI_BACKGROUND_TASK_SLOTS", "").strip()
    if not slots:
        # Background slots sit above the subagent ceiling on purpose: a backgrounded build must
        # not cost a child its lane. Fair use is paced by the proxy, not by this number.
        slots = str(max(8, subagent_limit + 3))
    return {
        "KIMI_SUBAGENT_CONCURRENCY": str(subagent_limit),
        "KIMI_BACKGROUND_TASK_SLOTS": slots,
    }


def cmd_resolve(root: Path, runtime: Path) -> int:
    providers, model_list = load_definitions(root)
    values = bootstrap_values(root)
    providers = apply_endpoint_overrides(providers, values)
    models = {model["id"]: model for model in model_list}
    selection = read_json(runtime / SELECTION)
    if not isinstance(selection, dict):
        raise DefinitionError(f"{SELECTION} must contain an object")
    plan = policy.resolve(
        providers,
        models,
        {lane: str(selection.get(lane, "")) for lane in SELECTABLE},
        reserved_context_size=reserved_context_size(root),
        key_values=values,
    )
    require_bootstrap_declarations(root, plan)
    write_json(runtime / POLICY_FILE, plan)
    write_json(runtime / COMPOSE_FRAGMENT, compose_fragment(plan, runtime))
    environment = model_environment(plan, values)
    write_text(
        runtime / MODEL_ENV,
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in environment.items()),
    )
    # The envelope is not published from here: tools/render_runtime.py appends the same rendered
    # text to the staged system prompt, so no launcher step writes inside the workspace.

    print(
        "Model policy resolved: "
        + " ".join(
            f"{lane}={plan['lanes'][lane]['alias']}" for lane in LANES if lane in plan["lanes"]
        )
    )
    for counter in plan["counters"].values():
        if counter["family"] == "context":
            print(
                f"  {counter['subject']}: in-flight budget {counter['budget']} of "
                f"{counter['ceiling']}, up to {counter['max']} concurrent"
            )
        elif counter["family"] == "rate":
            unit = counter.get("unit", "output_tokens")
            print(
                f"  {counter['subject']}: {counter['capacity']} "
                f"{policy.RATE_UNIT_LABELS.get(unit, unit)} booked"
            )
    print(f"  subagents: up to {plan['limits'].get('subagent_concurrency')} concurrent")
    return 0


def kimi_config(plan: dict[str, Any]) -> str:
    """The generated half of Kimi's ``config.toml`` for one plan, as TOML text."""
    return model_config.render(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["select", "resolve"])
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("HARNESS_ROOT", Path(__file__).resolve().parents[1]))
    runtime = Path(os.environ["HARNESS_RUNTIME_DIR"])
    runtime.mkdir(parents=True, exist_ok=True)
    if args.action == "select":
        return cmd_select(root, runtime, non_interactive=args.non_interactive)
    return cmd_resolve(root, runtime)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Refusal as error:
        raise SystemExit(f"Model setup refused: {error}") from error
