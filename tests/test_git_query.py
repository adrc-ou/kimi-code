"""Reads run against an agent-authored .git/config, so the isolation flags and the
distinction between an empty answer and a failed one are the whole contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import git_query


class GitQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        # The harness exports GIT_CONFIG_GLOBAL at a path this test cannot see, so every
        # git invocation has to be re-pointed at an empty config of our own.
        tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "gitconfig").write_text("", encoding="utf-8")
        # Deliberately outside the repo: a config inside it would show up as untracked noise.
        self.repo = tmp / "repo"
        self.repo.mkdir()
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(tmp),
                "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_COUNT": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _git(self, *arguments: str) -> None:
        # Fixture setup uses plain git, never git_text(), so a bug cannot hide in its own setup.
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.returncode, 0)

    def _repo_with_commit(self) -> Path:
        (self.repo / "a.txt").write_text("content\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("add", "a.txt")
        self._git(
            "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "t"
        )
        return self.repo

    def test_commit_hash_reads_back_as_one_stripped_line(self) -> None:
        # Raw git emits a trailing newline; the caller compares the value to a manifest field.
        self._repo_with_commit()
        revision = git_query.git_text(self.repo, "rev-parse", "HEAD")
        self.assertRegex(revision or "", r"^[0-9a-f]{40}$")
        self.assertFalse((revision or "").endswith("\n"))

    def test_clean_repository_reports_empty_status_rather_than_none(self) -> None:
        # Nothing to report is a success; collapsing it to None would hide a healthy workspace.
        self._repo_with_commit()
        self.assertEqual(git_query.git_text(self.repo, "status", "--porcelain"), "")

    def test_directory_that_is_not_a_repository_reads_as_none(self) -> None:
        self.assertIsNone(git_query.git_text(self.repo.parent, "status", "--porcelain"))

    def test_repository_without_commits_reads_as_none(self) -> None:
        # git prints "HEAD" on stdout while failing, so stdout content cannot decide the result.
        self._git("init", "-q", "-b", "main")
        self.assertIsNone(git_query.git_text(self.repo, "rev-parse", "HEAD"))

    def test_isolation_flags_precede_the_callers_arguments(self) -> None:
        # Global options after the subcommand are parsed as subcommand arguments and ignored.
        completed = SimpleNamespace(returncode=0, stdout="ok\n\n")
        with patch.object(git_query.subprocess, "run", return_value=completed) as run:
            self.assertEqual(git_query.git_text(Path("/repo"), "log", "-1"), "ok")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "git")
        self.assertEqual(argv[1:3], ["-C", "/repo"])
        self.assertEqual(argv[3:10], list(git_query.ISOLATION))
        self.assertEqual(argv[10:], ["log", "-1"])
        self.assertIs(run.call_args.kwargs["capture_output"], True)
        self.assertIs(run.call_args.kwargs["text"], True)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_failing_command_returns_none_even_with_output(self) -> None:
        broken = SimpleNamespace(returncode=1, stdout="junk\n")
        with patch.object(git_query.subprocess, "run", return_value=broken):
            self.assertIsNone(git_query.git_text(Path("/repo"), "rev-parse", "HEAD"))

    def test_isolation_constant_names_the_external_transport_guard(self) -> None:
        # An untrusted config can name an ext:: transport; git's own block needs network to
        # exercise end to end, so the flag's presence and position above is what is pinned.
        self.assertIn("protocol.ext.allow=never", git_query.ISOLATION)
        self.assertIn("core.hooksPath=/dev/null", git_query.ISOLATION)


if __name__ == "__main__":
    unittest.main()
