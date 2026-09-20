#!/usr/bin/env python3
"""Host shell runs on macOS as well as Linux, so it may not assume GNU userland.

Two halves. The static half reads every launcher and module script the host executes and refuses
the one operand order BSD ``getopt`` gets wrong; CI has only ever run these files against
coreutils, where the same line is harmless, so nothing else would notice. The behavioural half
runs the prompt-literal extraction against a ``chmod`` stub built the way Apple's is, because the
static half can flag that call site but never exercise it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The repository root as well as tools/, so that `tests.test_launchers` resolves under every way of
# running the suite, and so that the prompt cache is read back through the module that consumes it.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import kimi_prompts as kp  # noqa: E402

# The launcher suite, held as a module rather than as its classes: unittest collects every
# TestCase reachable through a module's namespace, so importing the class here would run the
# whole prompts.sh suite a second time under this file's name.
from tests import test_launchers  # noqa: E402

#: Commands whose first operand is a mode or an owner rather than a file. GNU ``getopt`` permutes a
#: ``--`` out of the argument list wherever it appears, so ``chmod 700 -- dir`` works on Linux;
#: BSD ``getopt`` stops option parsing at the first non-option, never reaches the separator, and
#: leaves it in the file list, where it becomes a filename and a "No such file or directory".
MODE_FIRST_COMMANDS = frozenset({"chmod", "chown"})

#: Directories whose shell runs inside the Debian image, where the userland is GNU whatever the
#: operator's laptop is. Everything else in the tree that ends in ``.sh`` is host shell.
EXCLUDED_PARTS = frozenset({"container", "node_modules"})

_COMMAND_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")


def host_scripts() -> list[Path]:
    """The launcher, tool and module scripts this repository expects bash on the host to run.

    Generated trees are skipped by their leading dot: a staged copy under ``.local`` is a second
    reading of a file already scanned, and one the operator never edits.
    """
    found = []
    for path in sorted(ROOT.rglob("*.sh")):
        relative = path.relative_to(ROOT)
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        found.append(path)
    return found


def _commands(line: str) -> list[list[str]]:
    """The commands one source line runs, each as its token list.

    Comments are dropped whole, which is what keeps a note *about* this rule from tripping it.
    """
    commands = []
    for chunk in _COMMAND_SEPARATORS.split(line.split("#", 1)[0]):
        tokens = chunk.split()
        if tokens:
            commands.append(tokens)
    return commands


def separator_after_the_mode(tokens: list[str]) -> bool:
    """Whether a command's operands put a ``--`` where BSD reads a filename.

    Option parsing is walked the way BSD ``getopt`` walks it: a leading ``--`` is consumed and
    ends the scan, any other leading dash is an option, and the first plain word is the mode. From
    there every remaining word is a file operand, separators included.
    """
    operands = tokens[1:]
    while operands and operands[0].startswith("-") and operands[0] != "-":
        if operands.pop(0) == "--":
            break
    if not operands:
        return False
    return "--" in operands[1:]


def misplaced_separators(text: str) -> list[tuple[int, str]]:
    """Every ``(line number, line)`` in which a mode-first command takes ``--`` as an operand."""
    offenders = []
    for number, line in enumerate(text.splitlines(), start=1):
        for tokens in _commands(line):
            command = tokens[0].rsplit("/", 1)[-1]
            if command in MODE_FIRST_COMMANDS and separator_after_the_mode(tokens):
                offenders.append((number, line.strip()))
                break
    return offenders


class HostShellPortabilityTests(unittest.TestCase):
    """The operand order the prompt-cache call sites in both launchers once broke."""

    def test_the_suite_sees_the_host_scripts_and_not_the_image_ones(self):
        names = {path.relative_to(ROOT).as_posix() for path in host_scripts()}
        for entrypoint in (
            "start.sh",
            "prompts.sh",
            "shell.sh",
            "extensions.sh",
            "tools/runtime.sh",
        ):
            self.assertIn(entrypoint, names)
        self.assertFalse({name for name in names if name.startswith("container/")})

    def test_no_host_command_puts_a_separator_after_its_mode(self):
        offenders = [
            f"{path.relative_to(ROOT)}:{number}: {line}"
            for path in host_scripts()
            for number, line in misplaced_separators(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_a_separator_in_option_position_still_passes(self):
        # The guard has to keep spelling out the safe forms, or the next reader removes every "--"
        # and loses the protection they were added for.
        for line in (
            'chmod -- 700 "${cache}"',
            'mkdir -p -- "${cache}" && chmod 700 "${cache}"',
            'cp -- "${tmp}" "${cache}/literals.json" && chmod 600 "${cache}/literals.json"',
        ):
            self.assertEqual(misplaced_separators(line), [], line)

    def test_the_guard_catches_the_form_it_exists_to_refuse(self):
        for line in (
            'chmod 700 -- "${cache}"',
            'mkdir -p -- "${cache}" && chmod 700 -- "${cache}"',
            'chown -R 1000:1000 -- "${tree}"',
        ):
            self.assertEqual([number for number, _ in misplaced_separators(line)], [1], line)


BSD_CHMOD_STUB = """
exec @python@ - "$@" <<'PY'
import os
import sys

argv = sys.argv[1:]
while argv and argv[0].startswith("-") and argv[0] != "-":
    if argv.pop(0) == "--":
        break
if not argv:
    sys.exit("usage: chmod [-fhv] [-R [-H | -L | -P]] mode file ...")
mode = argv.pop(0)
try:
    bits = int(mode, 8)
except ValueError:
    sys.exit(f"chmod: Invalid file mode: {mode}")
status = 0
for path in argv:
    try:
        os.chmod(path, bits)
    except OSError as error:
        print(f"chmod: {path}: {error.strerror}", file=sys.stderr)
        status = 1
sys.exit(status)
PY
"""


class BsdChmodPromptCacheTests(unittest.TestCase):
    """``./prompts.sh --extract`` against the chmod an Apple host actually has on its PATH."""

    setUp = test_launchers.PromptsScriptTests.setUp
    command = test_launchers.PromptsScriptTests.command
    run_script = test_launchers.PromptsScriptTests.run_script
    require_executable_fixtures = test_launchers.PromptsScriptTests.require_executable_fixtures
    instance_runtime_dir = test_launchers.PromptsScriptTests.instance_runtime_dir
    with_plan = test_launchers.PromptsScriptTests.with_plan

    def setUp_bsd_chmod(self) -> None:
        self.command(
            "chmod",
            BSD_CHMOD_STUB.replace("@python@", shlex.quote(sys.executable)),
        )

    def run_bsd_chmod(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", "-c", "chmod " + " ".join(shlex.quote(a) for a in arguments)],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_stub_fails_exactly_where_macos_chmod_fails(self):
        # The extraction test below is only evidence if this stub really refuses the way Apple's
        # /bin/chmod does; a stub that tolerated anything would let the whole class pass on air.
        self.setUp_bsd_chmod()
        (self.base / "operand.txt").write_text("x", encoding="utf-8")
        broken = self.run_bsd_chmod("600", "--", "operand.txt")
        self.assertEqual(broken.returncode, 1, broken.stderr)
        self.assertEqual(broken.stderr.strip(), "chmod: --: No such file or directory")
        portable = self.run_bsd_chmod("600", "operand.txt")
        self.assertEqual(portable.returncode, 0, portable.stderr)
        self.assertEqual(os.stat(self.base / "operand.txt").st_mode & 0o777, 0o600)

    def test_extract_caches_literals_the_renderer_can_read(self):
        self.setUp_bsd_chmod()
        self.with_plan({})
        literals = {
            "base_prompt": "You are Kimi Code, working in the user's workspace.",
            "coder_role": "Ship the change, with the checks that prove it.",
        }
        document = kp.document(literals, "sha256:" + "a" * 64)
        payload = self.base / "bundle-literals.json"
        payload.write_text(json.dumps(document), encoding="utf-8")
        self.env["TEST_LITERALS"] = str(payload)
        self.command(
            "docker",
            """
if [[ "$*" == "compose version" || "$*" == "info" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
if [[ "$1" == ps ]]; then echo fixture-container; exit 0; fi
if [[ "$*" == *"kimi_prompts.py"* ]]; then cat "$TEST_LITERALS"; exit 0; fi
exit 0
""",
        )
        result = self.run_script("prompts.sh", "--extract")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Harness failed", result.stderr)
        runtime = self.instance_runtime_dir()
        self.assertEqual(kp.read_document(runtime), document)
        self.assertEqual(kp.load(runtime), literals)


if __name__ == "__main__":
    unittest.main()
