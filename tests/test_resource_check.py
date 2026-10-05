"""This check never fails a launch, so everything it promises is a line it prints."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import resource_check

GIB = 1024**3


class ResourceCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        # A real directory keeps argparse's type=Path conversion on the tested path.
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)

    def run_main(
        self,
        *,
        disk_env: str,
        memory_env: str,
        free: int = 10 * GIB,
        memory_words: tuple[int, int] = (4096, 2097152),
        workspace: Path | None = None,
        disk_error: OSError | None = None,
    ) -> str:
        # Both thresholds are set explicitly so the operator's ambient env cannot move the answer.
        target = self.workspace if workspace is None else workspace
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["resource_check", str(target)]),
            patch.dict(
                os.environ,
                {
                    "KIMI_WARN_FREE_DISK_GIB": disk_env,
                    "KIMI_WARN_TOTAL_MEMORY_GIB": memory_env,
                },
                clear=True,
            ),
            patch.object(
                resource_check.shutil,
                "disk_usage",
                **({"side_effect": disk_error} if disk_error else {"return_value": (
                    SimpleNamespace(total=100 * GIB, used=90 * GIB, free=free)
                )}),
            ),
            patch.object(resource_check.os, "sysconf", side_effect=list(memory_words)),
            contextlib.redirect_stdout(stdout),
        ):
            result = resource_check.main()
        self.assertIsNone(result)
        return stdout.getvalue()

    def capture_threshold(self, name: str, value: str | None, default: int) -> tuple[int, str]:
        stdout = io.StringIO()
        environment = {} if value is None else {name: value}
        with (
            patch.dict(os.environ, environment, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            parsed = resource_check._threshold(name, default)
        return parsed, stdout.getvalue()

    def test_gib_is_binary_gibibytes(self) -> None:
        # The thresholds are documented in GiB, not decimal GB.
        self.assertEqual(resource_check.gib(0), 0.0)
        self.assertEqual(resource_check.gib(512 * 1024**2), 0.5)
        self.assertEqual(resource_check.gib(1024**3), 1.0)

    def test_threshold_uses_the_default_when_unset_or_blank(self) -> None:
        # An exported-but-empty variable is the same as not setting one, and must not warn.
        for value in ("", "   ", None):
            with self.subTest(value=value):
                parsed, text = self.capture_threshold("KIMI_WARN_FREE_DISK_GIB", value, 20)
                self.assertEqual(parsed, 20)
                self.assertEqual(text, "")

    def test_threshold_accepts_a_plain_integer(self) -> None:
        parsed, text = self.capture_threshold("KIMI_WARN_FREE_DISK_GIB", "5", 20)
        self.assertEqual(parsed, 5)
        self.assertEqual(text, "")

    def test_malformed_threshold_warns_with_the_raw_value(self) -> None:
        # The value is repr-quoted so an operator can see a stray space or shell quote.
        parsed, text = self.capture_threshold("KIMI_WARN_FREE_DISK_GIB", "1.5", 20)
        self.assertEqual(parsed, 20)
        self.assertEqual(
            text, "warning: KIMI_WARN_FREE_DISK_GIB='1.5' is not a number, using 20\n"
        )
        parsed, text = self.capture_threshold("KIMI_WARN_TOTAL_MEMORY_GIB", "abc", 8)
        self.assertEqual(parsed, 8)
        self.assertEqual(
            text, "warning: KIMI_WARN_TOTAL_MEMORY_GIB='abc' is not a number, using 8\n"
        )

    def test_capacity_line_and_disk_warning(self) -> None:
        # Exact whole-output pin: a low-disk host warns, and 8 GiB against an 8 GiB
        # threshold does not, which is what the strict comparison promises.
        self.assertEqual(
            self.run_main(disk_env="20", memory_env="8"),
            "Host capacity: free_disk=10.0GiB total_memory=8.0GiB\n"
            "warning: workspace filesystem has less than 20 GiB free\n",
        )

    def test_generous_thresholds_print_only_capacity(self) -> None:
        self.assertNotIn("less than", self.run_main(disk_env="1", memory_env="1"))

    def test_low_memory_warns(self) -> None:
        text = self.run_main(disk_env="1", memory_env="16")
        self.assertIn("warning: host has less than 16 GiB total memory", text)
        self.assertNotIn("workspace filesystem", text)

    def test_zero_measured_memory_suppresses_the_memory_warning(self) -> None:
        # Where page size is unknown, a huge default threshold must not alarm every launch.
        text = self.run_main(disk_env="1", memory_env="1024", memory_words=(0, 0))
        self.assertIn("total_memory=0.0GiB", text)
        self.assertNotIn("less than 1024", text)

    def test_zero_disk_threshold_disables_the_disk_warning(self) -> None:
        text = self.run_main(disk_env="0", memory_env="1024", free=0)
        self.assertNotIn("workspace filesystem", text)

    def test_malformed_threshold_warning_precedes_the_capacity_verdicts(self) -> None:
        # The warning is printed while resolving thresholds, before either verdict uses them.
        text = self.run_main(disk_env="1.5", memory_env="8")
        self.assertTrue(text.startswith("Host capacity:"))
        self.assertLess(text.index("is not a number"), text.index("less than"))

    def test_workspace_argument_is_converted_to_a_path(self) -> None:
        # The launcher passes a path string; disk_usage must receive the parsed Path.
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["resource_check", str(self.workspace)]),
            patch.dict(
                os.environ,
                {"KIMI_WARN_FREE_DISK_GIB": "1", "KIMI_WARN_TOTAL_MEMORY_GIB": "1"},
                clear=True,
            ),
            patch.object(
                resource_check.shutil,
                "disk_usage",
                return_value=SimpleNamespace(total=100 * GIB, used=90 * GIB, free=100 * GIB),
            ) as disk_usage,
            patch.object(resource_check.os, "sysconf", side_effect=[4096, 2097152]),
            contextlib.redirect_stdout(stdout),
        ):
            resource_check.main()
        self.assertEqual(disk_usage.call_args.args[0], self.workspace)
        self.assertIsInstance(disk_usage.call_args.args[0], Path)

    def test_missing_workspace_argument_exits_two(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["resource_check"]),
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as caught:
                resource_check.main()
        self.assertEqual(caught.exception.code, 2)

    def test_extra_argument_exits_two(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["resource_check", str(self.workspace), "extra"]),
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as caught:
                resource_check.main()
        self.assertEqual(caught.exception.code, 2)

    def report_unreadable(self, *, disk_env: str, memory_env: str) -> str:
        """The real syscall on a path that cannot be statvfs'd, with no disk stub at all.

        Left unpatched on purpose: the promise under test is about an actual errno arriving from
        an actual filesystem, so stubbing the call that raises it would prove only the stub.
        """
        stdout = io.StringIO()
        with (
            patch.object(
                sys, "argv", ["resource_check", str(self.workspace / "not-created-yet")]
            ),
            patch.dict(
                os.environ,
                {"KIMI_WARN_FREE_DISK_GIB": disk_env, "KIMI_WARN_TOTAL_MEMORY_GIB": memory_env},
                clear=True,
            ),
            patch.object(resource_check.os, "sysconf", side_effect=[4096, 2097152]),
            contextlib.redirect_stdout(stdout),
        ):
            resource_check.main()
        return stdout.getvalue()

    def test_an_unstatable_workspace_reports_instead_of_raising(self) -> None:
        # `start.sh` runs this under `|| return`, so an escaping exception here has always been an
        # aborted launch, not a skipped warning. The module's own contract is warn and exit zero.
        text = self.report_unreadable(disk_env="20", memory_env="8")
        self.assertIn("free disk for", text)
        self.assertIn("not-created-yet", text)
        self.assertIn("No such file or directory", text)

    def test_the_capacity_line_keeps_its_shape_when_disk_is_unknown(self) -> None:
        # The line is one the operator and the launcher's log both read; an unknown figure is
        # named as unknown in place, rather than the line being dropped or the field omitted.
        text = self.report_unreadable(disk_env="20", memory_env="8")
        self.assertTrue(text.startswith("Host capacity: free_disk=unknown total_memory=8.0GiB"))

    def test_an_unknown_free_disk_is_never_reported_as_low_space(self) -> None:
        # A tight threshold against a measurement that does not exist must not invent a verdict:
        # "less than 20 GiB free" would be a claim about a filesystem nobody read, and the
        # plausible response to it is a disk investigation that finds nothing wrong.
        text = self.run_main(disk_env="20", memory_env="8", disk_error=FileNotFoundError())
        self.assertIn("is unknown", text)
        self.assertNotIn("workspace filesystem has less than", text)

    def test_memory_is_still_judged_when_the_workspace_cannot_be_read(self) -> None:
        # The two measurements are independent, and only one of them failed. Dropping the whole
        # report over the unreadable half would hide the one warning this host actually needs.
        text = self.run_main(disk_env="1", memory_env="16", disk_error=PermissionError())
        self.assertIn("warning: host has less than 16 GiB total memory", text)
        self.assertIn("free disk for", text)


if __name__ == "__main__":
    unittest.main()
