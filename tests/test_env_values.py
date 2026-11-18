"""The ``KEY=VALUE`` reader every launcher step depends on.

Several steps need the same three lines of parsing, so there is one implementation and it is worth
pinning exactly: the values it returns decide which model is selected and which image is evicted,
and the docstring's own yardstick is what a shell and Docker Compose would have bound. The reader is
deliberately NOT a shell evaluator -- a value that could execute is the failure this parser exists
to avoid, which is why unmatched quotes are content rather than a syntax error.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tests", ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from env_values import read_env_values  # noqa: E402


class ReadEnvValuesTests(unittest.TestCase):
    def read(self, text: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.env"
            path.write_text(text, encoding="utf-8")
            return read_env_values(path)

    def test_one_assignment_per_line(self) -> None:
        self.assertEqual(self.read("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_blank_lines_and_leading_comments_are_skipped(self) -> None:
        self.assertEqual(self.read("# generated\n\n  # indented comment\nA=1\n"), {"A": "1"})

    def test_a_later_line_wins_because_shell_and_compose_both_say_so(self) -> None:
        self.assertEqual(self.read("A=first\nA=second\n"), {"A": "second"})

    def test_one_layer_of_matching_quotes_is_removed(self) -> None:
        self.assertEqual(self.read('A="x"\nB=\'y\'\n'), {"A": "x", "B": "y"})

    def test_unmatched_quotes_are_content_and_never_a_syntax_error(self) -> None:
        # Evaluating would be the bug: an unterminated quote here is a value, not a parse failure.
        self.assertEqual(self.read('A="x\nB=y"\nC="\n'), {"A": '"x', "B": 'y"', "C": '"'})

    def test_only_the_first_equals_sign_splits_the_line(self) -> None:
        self.assertEqual(self.read("URL=https://x/y?a=b\n"), {"URL": "https://x/y?a=b"})

    def test_whitespace_belongs_to_neither_half(self) -> None:
        self.assertEqual(self.read("  A  =  1  \n"), {"A": "1"})

    def test_an_empty_value_is_a_value_and_not_an_absence(self) -> None:
        self.assertEqual(self.read("A=\n"), {"A": ""})

    def test_a_line_naming_nothing_is_not_an_assignment(self) -> None:
        # Neither sh nor Compose binds `=1`. Returning {"": "1"} would hand the launcher a set of
        # variable names containing the empty string, which no later lookup can ask for by name.
        self.assertEqual(self.read("=1\nA=2\n"), {"A": "2"})

    def test_a_hash_after_the_equals_is_part_of_the_value(self) -> None:
        # Only whole-line comments are claimed. Stripping here would corrupt the values that
        # legitimately contain the sequence -- a URL fragment, or a generated token -- and those are
        # exactly what this file is written with.
        self.assertEqual(self.read("A=1 # two\n"), {"A": "1 # two"})


if __name__ == "__main__":
    unittest.main()
