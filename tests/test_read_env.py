"""The one-key CLI the launcher calls from inside ``$( )``.

start.sh, runtime.sh and modules/comfyui/module.sh all read operator or Compose-resolved values
through this script, and runtime.sh pipes it ``/dev/stdin`` rather than a file, so both the answer
and the refusal are launcher-facing: a value that comes back quoted, or a missing key that exits
zero, changes what gets launched.

The parsing itself belongs to :func:`tools.env_values.read_env_values`; what these cases guard is
that this entry point still DELEGATES to it. The empty-key case is the proof -- a hand-rolled copy
of the parser used to read `=1` as a binding for the empty name, and only the shared parser's skip
makes this script refuse it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "read_env.py"


def run(text: str, key: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/stdin", key],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


class ReadEnvCliTests(unittest.TestCase):
    def test_a_value_is_printed_bare_and_exits_zero(self) -> None:
        result = run("HARNESS_KEEPWAKE=yes\n", "HARNESS_KEEPWAKE")
        self.assertEqual((result.returncode, result.stdout), (0, "yes\n"), result.stderr)

    def test_quotes_are_removed_and_only_the_first_equals_splits(self) -> None:
        self.assertEqual(run('URL="https://x/y?a=b"\n', "URL").stdout, "https://x/y?a=b\n")

    def test_the_last_line_wins_because_shell_and_compose_both_say_so(self) -> None:
        self.assertEqual(run("A=first\n# A=commented\n\nA=second\n", "A").stdout, "second\n")

    def test_a_key_the_file_never_names_exits_nonzero_with_the_named_file(self) -> None:
        result = run("A=1\n", "MISSING")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MISSING is not set in /dev/stdin", result.stderr)
        self.assertEqual(result.stdout, "", "a refusal must not print a value either")

    def test_an_empty_key_is_not_a_binding_here_either(self) -> None:
        # The dedupe pin: this is where the shared parser's rule has to reach the launcher too.
        result = run("=1\nA=2\n", "")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(run("=1\nA=2\n", "A").stdout, "2\n")

    def test_a_file_path_works_as_well_as_the_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolved.env"
            path.write_text("TOKEN='abc'\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(path), "TOKEN"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual((result.returncode, result.stdout), (0, "abc\n"), result.stderr)


if __name__ == "__main__":
    unittest.main()
