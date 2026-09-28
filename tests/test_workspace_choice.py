#!/usr/bin/env python3
"""Choosing the workspace: what is remembered, what counts as an answer, and how the screen
behaves.

Four groups, in the order the code is layered.

``RegistryTests`` reads and writes the durable list in ``.local/workspaces.json`` — the file that
has to outlive the instance it selects, and whose ordering is load-bearing twice over: it is the
list on the screen, and it is the answer an unattended launch takes.

``PathPolicyTests`` judges directories on a real temporary tree rather than a mocked filesystem,
because every rule it enforces is a rule about what the operating system will let the launch do
next, and a fake that agrees with the code instead of with the disk proves nothing. Where a rule
depends on permissions this host will not honour, the case says so and steps aside.

``WorkspaceScreenTests`` drives the step through the same scripted terminal the engine's own suite
uses, and asserts on what the frame *shows* — the mark, the caret, the panel, the question — since
that is the contract the operator reads.

``ChoiceTests`` runs the tool the launcher calls, which is where the one-line promise on standard
output is either kept or broken.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import workspace_choice  # noqa: E402
import workspace_registry as registry  # noqa: E402

from tests.helpers import run_in_pty  # noqa: E402
from tests.test_tui import QUIET, _Script  # noqa: E402
from tools.tui import paths  # noqa: E402
from tools.tui.app import Modal, View  # noqa: E402
from tools.tui.caps import Caps  # noqa: E402
from tools.tui.keys import Key  # noqa: E402
from tools.tui.workspaces import (  # noqa: E402
    CREATE_ASK,
    NEW_LABEL,
    SEEDED,
    PathState,
    WorkspaceStep,
)


#: Some hosts cannot be told that a directory is unwritable. Root ignores discretionary permissions
#: outright, and a virtiofs share answers ``W_OK`` whatever the mode bits say — verified here, not
#: theorised, because the suite runs on such a host. Where the refusal cannot be made real the
#: verdict under test cannot be reached at all, so those cases say so and stand aside rather than
#: passing on a filesystem that agreed to nothing.
def write_refused(path: Path) -> bool:
    """Whether this host really refuses a write to ``path``, which the caller chmod'ed to 0500."""
    return not os.access(path, os.W_OK)


class Tree(unittest.TestCase):
    """A temporary directory tree with the shapes the policy has to tell apart."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.live = self.mkdir("live")
        self.empty = self.mkdir("empty")
        self.nested = self.mkdir("live/deep")
        (self.base / "afile").write_text("not a directory\n", encoding="utf-8")
        #: Used by the cases that ask what happens when a directory cannot be written. It is left
        #: readable so the walk into it still works, and restored in cleanup so the tree can be
        #: removed: a temporary directory that cannot be written cannot be deleted by its owner.
        self.ro = self.mkdir("readonly")
        self.addCleanup(self.ro.chmod, 0o700)
        self.ro.chmod(0o500)
        self.link = self.base / "link"
        self.link.symlink_to(self.live, target_is_directory=True)
        self.dangling = self.base / "dangling"
        self.dangling.symlink_to(self.base / "nowhere", target_is_directory=True)
        (self.base / "spaced dir").mkdir()
        (self.base / "hidden").mkdir()

    def mkdir(self, relative: str) -> Path:
        path = self.base / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, relative: str = "") -> str:
        return str(self.base / relative) if relative else str(self.base)

    def to_root(self) -> str:
        """A path that walks all the way back up to ``/``, whatever depth this tree sits at.

        Counted out rather than guessed at, because ``../..`` means the root only for a temporary
        directory of one particular depth, and the suite's own TMPDIR is not fixed.
        """
        return str(self.base) + "/.." * (len(self.base.parts) - 1)


class RegistryTests(Tree):
    """``.local/workspaces.json``: newest first, canonical, and small enough to read."""

    def test_the_list_is_empty_before_anything_has_been_chosen(self):
        self.assertEqual(registry.load(self.base), ())
        self.assertEqual(registry.newest(self.base), "")
        self.assertEqual(
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/workspace_registry.py"),
                    "--root",
                    str(self.base),
                    "newest",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).returncode,
            1,
        )

    def test_a_chosen_directory_is_canonical_and_moves_to_the_head(self):
        first = registry.touch(self.base, self.path("live"))
        registry.touch(self.base, self.path("empty"))
        again = registry.touch(self.base, first.path)
        # Re-choosing a directory moves it rather than duplicating it: the list is a history of
        # use, and two rows naming one directory is a list the operator cannot read.
        self.assertEqual([entry.path for entry in registry.load(self.base)],
                         [first.path, self.path("empty")])
        self.assertEqual(registry.newest(self.base), first.path)
        self.assertEqual(again.path, first.path)

    def test_a_symlink_and_a_trailing_slash_are_the_same_workspace(self):
        registry.touch(self.base, self.path("link"))
        registry.touch(self.base, self.path("live") + "/")
        self.assertEqual([entry.path for entry in registry.load(self.base)], [self.path("live")])

    def test_the_list_holds_ten_and_drops_the_oldest(self):
        for index in range(registry.MAX_RECENT + 5):
            directory = self.mkdir(f"batch{index}")
            registry.touch(
                self.base, directory, when=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
            )
        entries = registry.load(self.base)
        self.assertEqual(len(entries), registry.MAX_RECENT)
        self.assertEqual(entries[0].path, self.path("batch14"))
        self.assertNotIn(self.path("batch0"), [entry.path for entry in entries])

    def test_choosing_inside_the_same_second_still_orders_by_choice(self):
        # The head of this list is what an unattended launch mounts, so two choices made in one
        # second may not be settled alphabetically.
        registry.touch(self.base, self.path("empty"))
        registry.touch(self.base, self.path("live"))
        self.assertEqual(registry.newest(self.base), self.path("live"))

    def test_removal_takes_one_row_and_leaves_the_rest(self):
        registry.touch(self.base, self.path("live"))
        registry.touch(self.base, self.path("empty"))
        left = registry.remove(self.base, self.path("live"))
        self.assertEqual([entry.path for entry in left], [self.path("empty")])
        # Removing something the list never held is the state the caller asked for, not an error.
        self.assertEqual([entry.path for entry in registry.remove(self.base, self.path("gone"))],
                         [self.path("empty")])

    def test_a_file_this_tool_cannot_explain_is_an_empty_history(self):
        registry.registry_path(self.base).parent.mkdir(parents=True, exist_ok=True)
        for broken in ("", "{not json", '{"schema": 99, "workspaces": []}', '{"workspaces": 4}'):
            registry.registry_path(self.base).write_text(broken, encoding="utf-8")
            self.assertEqual(registry.load(self.base), (), broken)
        registry.registry_path(self.base).write_text(
            json.dumps({"schema": 1, "workspaces": ["/absolute", {"path": "/x"},
                                                     {"path": "/", "last_used": "now"}]}),
            encoding="utf-8",
        )
        # A row that cannot name a directory below the root is dropped rather than trusted: the
        # file is operator-editable, and a reader that guessed would mount what a typo wrote.
        self.assertEqual(registry.load(self.base), ())

    def test_the_document_is_private_and_holds_no_secrets_but_paths(self):
        registry.touch(self.base, self.path("live"))
        mode = registry.registry_path(self.base).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class PathPolicyTests(Tree):
    """What may become a workspace, judged against the disk rather than against a mock."""

    def test_the_answers_that_are_never_a_workspace(self):
        for value, expect in {
            "": "enter a directory path",
            "   ": "enter a directory path",
            "/": "filesystem root",
            "//": "filesystem root",
            self.to_root(): "filesystem root",
        }.items():
            with self.subTest(value=value):
                verdict = paths.usable(value)
                self.assertFalse(verdict.usable, verdict.path)
                self.assertIn(expect, verdict.error)
        # And a directory reached by walking up out of one is judged on where it lands, not on the
        # spelling that got there: one level up from a child of the temporary root is that root,
        # which is a perfectly usable workspace.
        self.assertTrue(paths.usable(self.path("live/..")).usable)

    def test_a_relative_path_and_a_drive_root_are_refused_for_their_own_reasons(self):
        self.assertIn("absolute", paths.usable("live").error)
        self.assertIn("absolute", paths.usable("~/live").error)
        self.assertIn("root", paths.usable("C:\\").error)
        self.assertIn("root", paths.usable("D:").error)

    def test_a_file_is_not_a_directory(self):
        verdict = paths.usable(self.path("afile"))
        self.assertFalse(verdict.usable)
        self.assertTrue(verdict.exists)
        self.assertIn("file, not a directory", verdict.error)

    def test_only_the_last_name_may_be_missing(self):
        fresh = paths.usable(self.path("live/new-project"))
        self.assertTrue(fresh.usable)
        self.assertTrue(fresh.creatable)
        self.assertFalse(fresh.exists)
        self.assertEqual(fresh.name, "new-project")
        # Two names deep is a typo, not an intention, and the missing parent is what says so.
        deeper = paths.usable(self.path("nowhere/deep"))
        self.assertFalse(deeper.usable)
        self.assertIn("does not exist", deeper.error)

    def test_a_link_is_answered_with_what_it_points_at(self):
        verdict = paths.usable(self.path("link"))
        self.assertTrue(verdict.usable)
        self.assertEqual(verdict.path, self.path("live"))
        self.assertTrue(verdict.resolved)

    def test_a_link_that_goes_nowhere_is_refused(self):
        verdict = paths.usable(self.path("dangling"))
        self.assertFalse(verdict.usable)

    def test_a_directory_is_measured_by_what_it_holds_not_by_its_name(self):
        # ``//`` and ``..`` are the same directory as the plain spelling, and reporting that as a
        # resolution would teach the operator to ignore the word when it matters.
        folded = paths.usable(self.path("live") + "//deep/")
        self.assertTrue(folded.usable)
        self.assertEqual(folded.path, self.path("live/deep"))
        self.assertFalse(folded.resolved)

    def test_a_directory_you_cannot_write_is_not_a_workspace(self):
        if not write_refused(self.ro):
            self.skipTest("this host grants a write it was told to refuse")
        verdict = paths.usable(self.path("readonly"))
        self.assertFalse(verdict.usable)
        self.assertTrue(verdict.exists)
        self.assertIn("read and write", verdict.error)
        # And the same verdict for a new name inside it: the parent is what has to be writable.
        created = paths.usable(self.path("readonly/new"))
        self.assertFalse(created.usable)
        self.assertIn("you cannot create", created.error)

    def test_a_space_is_belonged_to_the_filesystem_before_the_shell(self):
        plain = paths.usable(self.path("spaced dir"))
        self.assertTrue(plain.usable)
        self.assertEqual(plain.path, self.path("spaced dir"))
        # The shell-escaped spelling of a directory that exists is understood, silently.
        escaped = paths.usable(self.path("spaced\\ dir"))
        self.assertTrue(escaped.usable, escaped.error)
        self.assertEqual(escaped.path, self.path("spaced dir"))
        # And it never invents one: with no directory on either side of the argument, the value as
        # typed stands, so the launcher creates the name the operator actually meant.
        self.assertEqual(paths.usable(self.path("spaced\\ dir"), allow_create=False).usable, False)

    def test_a_workspace_may_not_contain_what_it_must_not_see(self):
        # The launcher refuses these on its own account; the screen has to say so while the
        # operator can still change the answer, and it has to mean containment rather than equality
        # — the parent of the checkout is the dangerous case, and a good deal easier to type.
        checkout = self.mkdir("live/checkout")
        reserved = (str(checkout),)
        for value in (self.path("live"), str(self.base)):
            with self.subTest(value=value):
                refused = paths.usable(value, reserved=reserved)
                self.assertFalse(refused.usable)
                self.assertIn("contains", refused.error)
        # The checkout itself is refused too, which is the rule the launcher has always had. What
        # the rule is *not* about is looking similar: a directory that merely shares the checkout's
        # first characters is an ordinary workspace.
        self.assertFalse(paths.usable(str(checkout), reserved=reserved).usable)
        self.assertTrue(paths.usable(self.path("live/other"), reserved=reserved).usable)
        (self.live / "checkout-annex").mkdir()
        self.assertTrue(paths.usable(self.path("live/checkout-annex"), reserved=reserved).usable)
        # A path already refused for a plainer reason keeps that reason, which is the one the
        # operator can act on.
        self.assertIn("root", paths.usable("/", reserved=reserved).error)

    def test_containment_is_about_paths_and_not_about_string_prefixes(self):
        self.assertTrue(paths.contains("/work/proj", "/work"))
        self.assertTrue(paths.contains("/work/proj/", "/work/proj"))
        self.assertFalse(paths.contains("/work/proj", "/work/pro"))
        self.assertFalse(paths.contains("/work/proj", "/work/proj/inner"))

    def test_a_value_is_judged_but_never_created_by_the_judgement(self):
        verdict = paths.usable(self.path("live/from-here"))
        self.assertTrue(verdict.usable)
        self.assertFalse((self.live / "from-here").exists())


class CompletionTests(Tree):
    """``Tab``, to the extent bash does: directories, one level, and the word under the caret."""

    def setUp(self):
        super().setUp()
        self.mkdir("live/alpha")
        self.mkdir("live/alder")
        self.mkdir("live/beta")
        (self.live / "afile.txt").write_text("x\n", encoding="utf-8")

    def names(self, text, caret=None):
        return paths.completions(text, caret).candidates

    def test_only_directories_are_offered(self):
        offered = self.names(self.path("live") + "/")
        self.assertIn("alpha/", offered)
        self.assertNotIn("afile.txt", offered)

    def test_one_match_is_inserted_and_never_listed(self):
        found = paths.completions(self.path("live") + "/bet")
        self.assertEqual(found.candidates, ())
        self.assertEqual(found.completed, "beta/")
        self.assertEqual(found.text, self.path("live") + "/beta/")
        self.assertEqual(found.caret, len(found.text))

    def test_several_matches_insert_what_they_agree_on_and_are_then_listed(self):
        found = paths.completions(self.path("live") + "/al")
        self.assertEqual(found.candidates, ("alder/", "alpha/"))
        self.assertEqual(found.text, self.path("live") + "/al")
        self.assertEqual(found.completed, "")
        narrowed = paths.completions(self.path("live") + "/ald")
        self.assertEqual(narrowed.completed, "alder/")

    def test_the_word_is_the_whole_name_and_not_only_what_precedes_the_caret(self):
        # readline completes the word the cursor is in. With the caret inside ``alpha`` the tail is
        # part of what was typed, so completing replaces all of it rather than appending to it.
        text = self.path("live") + "/alpha"
        caret = len(self.path("live")) + 3
        found = paths.completions(text, caret)
        # One directory matches the whole word, so it is inserted and nothing is listed.
        self.assertEqual(found.completed, "alpha/")
        self.assertEqual(found.candidates, ())
        self.assertEqual(found.text, self.path("live") + "/alpha/")

    def test_completing_in_the_middle_does_not_double_the_separator(self):
        text = self.path("live") + "/al/beta"
        found = paths.completions(text, len(self.path("live")) + 3)
        self.assertNotIn("//", found.text)

    def test_a_hidden_name_is_offered_once_the_dot_is_typed(self):
        self.mkdir("live/.cache")
        self.mkdir("live/.config")
        self.assertNotIn(".cache/", self.names(self.path("live") + "/"))
        self.assertIn(".cache/", self.names(self.path("live") + "/."))
        # ``.`` and ``..`` are never on the list: the current directory and the one above it are
        # not answers to "which workspace?", however bash would have treated them.
        self.assertNotIn("../", self.names(self.path("live") + "/."))
        self.assertNotIn("./", self.names(self.path("live") + "/."))
        # A lone hidden match is inserted rather than listed, exactly like any other lone match.
        self.assertEqual(paths.completions(self.path("live") + "/.cach").completed, ".cache/")

    def test_an_unreadable_parent_is_said_loudly_and_changes_nothing(self):
        found = paths.completions(self.path("nope") + "/")
        self.assertTrue(found.unreadable)
        self.assertEqual(found.candidates, ())
        self.assertEqual(found.text, self.path("nope") + "/")

    def test_matching_is_case_sensitive_as_it_is_on_a_filesystem(self):
        self.assertEqual(self.names(self.path("live") + "/ALPHA"), ())


class Screen(unittest.TestCase):
    """A workspace screen driven by scripted keystrokes, painted into a buffer a test can read."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def screen(self, step, keys=(), *, rows=30, columns=80):
        caps = Caps(color=QUIET.color, unicode=True, columns=columns, rows=rows, probe=False)
        terminal = _Script(list(keys), caps)
        modal = Modal(step, View(), terminal=terminal, caps=caps)
        modal.paint()
        return modal, "\n".join(modal.screen.snapshot())

    def drive(self, step, keys):
        """Apply keystrokes one at a time, returning the frame after each and any answer."""
        modal, first = self.screen(step)
        frames, outcome = [first], None
        for key in keys:
            result = modal.handle(key)
            if result is not None:
                outcome = result
                break
            modal.paint()
            frames.append("\n".join(modal.screen.snapshot()))
        return modal, frames, outcome


class WorkspaceScreenTests(Screen):
    """The screen an operator answers, asserted on what it shows."""

    def step(self, recent=(), **kwargs):
        return WorkspaceStep(recent=recent, **kwargs)

    def test_the_newest_remembered_directory_opens_marked_below_the_field(self):
        first, second = "/data/one", "/data/two"
        _, screen = self.screen(self.step([first, second]))
        self.assertIn("(x) /data/one", screen)
        self.assertIn("( ) /data/two", screen)
        self.assertIn(f"( ) {NEW_LABEL}", screen)
        self.assertIn("Choose a workspace directory", screen)
        # "New Workspace…" is the row *above* the list, which is where a reader who does not see
        # their directory on it looks first.
        lines = [line for line in screen.splitlines() if line.strip()]
        order = [n for n, line in enumerate(lines) if NEW_LABEL in line or "/data/" in line]
        self.assertLess(
            [n for n, line in enumerate(lines) if NEW_LABEL in line][0],
            [n for n, line in enumerate(lines) if "/data/one" in line][0],
        )
        self.assertTrue(order)
        # The last name is the part worth bolding, and only it. ``bold`` is a capability answer, so
        # the rows are read back from the step the modal fitted — a detached step has no cell table
        # to weigh the emphasis against.
        modal, _ = self.screen(self.step([first]))
        rows = modal.step.rows(modal.session.state)
        remembered = [row for row in rows if row.target == first]
        self.assertEqual(len(remembered), 1)
        segments = remembered[0].line.segments
        self.assertEqual([s.text for s in segments if s.attr.endswith("1m")], ["one"])
        self.assertEqual(segments[0].text, "(x) ")

    def test_enter_takes_the_marked_row_without_touching_anything_else(self):
        lived = self.root / "lived"
        lived.mkdir()
        modal, _, outcome = self.drive(self.step([str(lived)]), [Key("Enter")])
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.value, str(lived))
        self.assertFalse((self.root / "lived").is_symlink())

    def test_the_arrow_keys_move_the_selection_and_the_field_opens_under_the_cursor(self):
        # Up from the newest remembered row reaches the field above it. The label is what that row
        # looks like until the cursor gets there, and the value is what it looks like after.
        _, frames, _ = self.drive(self.step(["/data/one", "/data/two"]), [])
        self.assertIn(f"( ) {NEW_LABEL}", frames[-1])
        _, frames, _ = self.drive(self.step(["/data/one", "/data/two"]), [Key("Up")])
        self.assertNotIn(NEW_LABEL, frames[-1])
        self.assertIn(f"(x) {SEEDED}", frames[-1])
        self.assertIn("( ) /data/one", frames[-1])

    def test_the_field_opens_at_the_root_with_the_insertion_point_after_it(self):
        _, frames, _ = self.drive(self.step([]), [])
        self.assertIn(f"{SEEDED}▏", frames[-1])
        # And the seed is not treated as an answer the operator has given: the row says what to do
        # next rather than refusing a root nobody chose.
        self.assertIn("type a path", frames[-1])
        self.assertNotIn("filesystem root", frames[-1])

    def test_typing_lands_at_the_caret_and_the_arrows_move_it_one_character(self):
        _, frames, _ = self.drive(self.step([]), [Key("t", "t"), Key("m", "m"), Key("Left")])
        self.assertIn("/t▏m", frames[-1])
        # ``Up`` and ``Down`` go to either end of the value, which is what frees ``Home`` and
        # ``End`` to stay what they are on every other screen: first and last row of the list.
        _, frames, _ = self.drive(self.step([]), [Key("t", "t"), Key("m", "m"), Key("Up")])
        self.assertIn("▏/tm", frames[-1])
        _, frames, _ = self.drive(self.step([]), [Key("Up"), Key("x", "x")])
        self.assertIn("x▏/", frames[-1])
        _, frames, _ = self.drive(self.step([]), [Key("Up"), Key("Down"), Key("y", "y")])
        self.assertIn("/y▏", frames[-1])

    def test_a_value_too_long_for_one_row_is_shown_across_several_and_whole(self):
        long = "/" + "/".join(f"directory-{index}" for index in range(9))
        modal, frames, _ = self.drive(self.step([]), [Key(c, c) for c in long[1:]])
        body = modal.frame.body
        painted = frames[-1].splitlines()
        rows = [line[body.left : body.right] for line in painted[body.top : body.bottom]]
        rows = [line for line in rows if "directory-" in line]
        self.assertGreater(len(rows), 1)
        # Every character of the value is on the screen: nothing clipped, nothing scrolled away,
        # and no ellipsis standing in for the tail the operator still has to be able to read.
        self.assertEqual("".join(line.replace("▏", "").strip() for line in rows), long)
        self.assertNotIn("…", "".join(rows))

    def test_the_field_takes_the_arrow_keys_until_escape_gives_them_back(self):
        _, frames, _ = self.drive(self.step(["/data/one"]), [Key("Up"), Key("Escape")])
        # Blurred: the value is still there, the caret is not, and the list answers the arrows.
        self.assertIn("/data/one", frames[-1])
        self.assertNotIn("▏", frames[-1])
        self.assertIn("leave the field", " ".join(frames[-2:]))
        # And back down onto the remembered row, which is the only thing the arrows can mean now.
        _, frames, _ = self.drive(self.step(["/data/one"]), [Key("Up"), Key("Escape"), Key("Down")])
        self.assertIn("(x) /data/one", frames[-1])

    def test_a_tab_with_several_matches_leaves_a_list_at_the_bottom_of_the_panel(self):
        (self.root / "alpha").mkdir()
        (self.root / "alps").mkdir()
        prefix = self.root.as_posix() + "/al"
        modal, frames, _ = self.drive(self.step([]), [Key(c, c) for c in prefix[1:]] + [Key("Tab")])
        painted = frames[-1].splitlines()
        body = modal.frame.body
        shown = [line for line in painted[body.top : body.bottom] if line.strip()]
        self.assertTrue(any("alpha/" in line for line in shown), shown)
        self.assertTrue(any("alps/" in line for line in shown), shown)
        # Bottom-anchored, and clear of the legend: the last painted body row holds a candidate.
        self.assertIn("alps/", painted[body.bottom - 1] + painted[body.bottom - 2])
        self.assertNotIn("alpha/", "\n".join(painted[modal.frame.footer.top :]))

    def test_a_lone_completion_leaves_no_panel_behind(self):
        # Completing is the one Tab that has already said everything: the field shows the name, so a
        # panel under it could only repeat the row or contradict it with a "no match" line.
        (self.root / "gamma").mkdir()
        prefix = (self.root / "gam").as_posix()[1:]
        _, frames, _ = self.drive(
            self.step([]), [Key(c, c) for c in prefix] + [Key("Tab")]
        )
        self.assertIn(f"{self.root}/gamma/", frames[-1])
        self.assertNotIn("no directory matches", frames[-1])
        self.assertNotIn("nothing to list", frames[-1])

    def test_the_completion_list_stays_until_the_next_one_replaces_it(self):
        (self.root / "alpha").mkdir()
        (self.root / "alps").mkdir()
        prefix = (self.root.as_posix() + "/al")[1:]
        _, frames, _ = self.drive(self.step([]), [Key(c, c) for c in prefix] + [Key("Tab")])
        self.assertIn("alpha/", frames[-1])
        # Typing narrows the *answer*, not the panel: the list is what the operator is reading
        # from, and it disappears when the next Tab has something else to show, not before.
        _, frames, _ = self.drive(self.step([]), [Key(c, c) for c in prefix] +
                                  [Key("Tab"), Key("x", "x")])
        self.assertIn("alpha/", frames[-1])
        _, frames, _ = self.drive(self.step([]), [Key(c, c) for c in prefix] +
                                  [Key("Tab"), Key("x", "x"), Key("Tab")])
        self.assertNotIn("alpha/", frames[-1])
        self.assertIn("no directory matches", frames[-1])

    def test_a_bad_answer_keeps_the_screen_and_says_why(self):
        modal, frames, outcome = self.drive(self.step([]), [Key("Enter")])
        self.assertIsNone(outcome)
        self.assertIn("filesystem root", frames[-1])
        _, frames, outcome = self.drive(
            self.step([]), [Key("Backspace")] + [Key(c, c) for c in "relative"] + [Key("Enter")]
        )
        self.assertIsNone(outcome)
        self.assertIn("absolute", frames[-1])

    def test_a_remembered_directory_that_is_gone_but_could_be_made_asks_first(self):
        gone = str(self.root / "gone")
        asked = []
        modal, frames, outcome = self.drive(
            self.step([gone], on_remove=asked.append), [Key("Enter")]
        )
        self.assertIsNone(outcome)
        self.assertIn(CREATE_ASK, frames[-1])
        # OK is the default, and the button that would create the directory.
        self.assertIn("< OK >", frames[-1])
        self.assertIn("  Cancel  ", frames[-1])
        _, frames, outcome = self.drive(self.step([gone]), [Key("Enter"), Key("Enter")])
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.value, gone)
        self.assertFalse(Path(gone).exists(), "answering the screen must not create anything")

    def test_the_create_question_can_be_cancelled_with_the_keyboard(self):
        gone = str(self.root / "gone")
        for keys in ([Key("Enter"), Key("Escape")], [Key("Enter"), Key("Left"), Key("Enter")]):
            with self.subTest(keys=len(keys)):
                _, frames, outcome = self.drive(self.step([gone]), keys)
                self.assertIsNone(outcome)
                self.assertIn("(x)", frames[-1])

    def test_a_remembered_directory_that_cannot_be_made_offers_to_be_forgotten(self):
        # Two names deep with neither present. The leaf could be made if its parent existed, and
        # the parent does not, so this row is not recoverable and only the list can change. It
        # never has to be remembered first: the screen answers what the disk says now, not what was
        # recorded.
        missing = str(self.root / "nope" / "gone")
        forgotten = []
        step = self.step([missing], on_remove=forgotten.append)
        _, frames, outcome = self.drive(step, [Key("Enter")])
        self.assertIsNone(outcome)
        self.assertIn("Remove it from the Recent Workspaces list?", frames[-1].replace("\n", " "))
        self.assertIn("< Remove From List >", frames[-1])
        self.assertNotIn("< Cancel >", frames[-1])
        _, frames, outcome = self.drive(self.step([missing], on_remove=forgotten.append),
                                        [Key("Enter"), Key("Left"), Key("Enter")])
        self.assertIsNone(outcome, "cancelling the question must not remove the row either")
        self.assertEqual(forgotten, [])
        self.assertFalse((self.root / "nope").exists())
        _, frames, outcome = self.drive(step, [Key("Enter"), Key("Enter")])
        self.assertIsNone(outcome, "neither button continues the launch")
        self.assertEqual(forgotten, [missing])
        # The row is gone from the screen as well as from the file.
        self.assertNotIn("gone", frames[-1])

    def test_with_nothing_remembered_only_the_field_is_shown(self):
        modal, frames, _ = self.drive(self.step([]), [])
        self.assertNotIn("( )", frames[-1])
        self.assertNotIn("(x)", frames[-1])
        self.assertIn("Choose a workspace directory", frames[-1])
        self.assertIn("▏", frames[-1])
        # And there is nothing to blur to, so the key is not offered.
        table = self.step([]).keys(PathState(), View())
        self.assertNotIn("Escape", [name for binding in table for name in binding.keys])

    def test_the_screen_refuses_a_parent_of_the_checkout_before_the_launcher_has_to(self):
        # Type the directory the checkout lives in, which exists and is perfectly writable: without
        # the reserved rule the screen would answer with it and the launch would die one step
        # later, after the operator had already committed.
        parent = self.root / "harness"
        parent.mkdir()
        typed = [Key(character, character) for character in str(parent)[1:]]

        def attempt(*reserved):
            step = WorkspaceStep(recent=[], reserved=reserved)
            _, frames, outcome = self.drive(step, [*typed, Key("Enter")])
            return frames[-1], outcome

        frames, outcome = attempt(str(parent))
        self.assertIsNone(outcome)
        self.assertIn("contains", frames)
        # The same path with nothing reserved is an ordinary answer, which is what makes the
        # refusal above the rule rather than an accident of the fixture.
        frames, outcome = attempt()
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.value, str(parent))

    def test_a_directory_that_exists_but_cannot_be_used_is_said_rather_than_asked_about(self):
        readonly = self.root / "locked"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o755)
        if not write_refused(readonly):
            self.skipTest("this host grants a write it was told to refuse")
        _, frames, outcome = self.drive(self.step([str(readonly)]), [Key("Enter")])
        self.assertIsNone(outcome)
        self.assertIn("read and write", frames[-1])
        self.assertNotIn("Remove it from the Recent", frames[-1])



class ChoiceToolTests(Tree):
    """The contract between the picker and :file:`start.sh`: one line on standard output."""

    def run_tool(self, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools/workspace_choice.py"), "--root", str(self.base),
             *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={**os.environ, "COLUMNS": "80", "LINES": "24"},
        )

    def test_an_unattended_launch_takes_the_head_of_the_list_and_prints_it_alone(self):
        registry.touch(self.base, self.path("empty"))
        registry.touch(self.base, self.path("live"))
        result = self.run_tool("--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.path("live") + "\n")

    def test_an_unattended_launch_with_nothing_remembered_stops_and_says_so(self):
        result = self.run_tool("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("./start.sh", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_piped_launch_uses_the_remembered_workspace_and_says_so_off_the_pipe(self):
        # stdout is the launcher's variable, so a notice printed there would be captured as a path.
        registry.touch(self.base, self.path("live"))
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/workspace_choice.py"), "--root", str(self.base)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.stdout, self.path("live") + "\n")
        self.assertIn("No terminal", result.stderr)

    def test_the_choice_of_a_screen_is_recorded_for_the_next_unattended_launch(self):
        # The screen and the file are one contract: a launch that chose a workspace but did not
        # record it would ask again, and an unattended launch would find nothing to take.
        registry.touch(self.base, self.path("live"))
        self.assertEqual(workspace_choice.choose(self.base, interactive=False), self.path("live"))
        entries = registry.load(self.base)
        self.assertEqual([entry.path for entry in entries], [self.path("live")])

    def test_the_caution_the_screen_shows_is_about_what_the_answer_opens(self):
        self.assertIn("reads and writes", workspace_choice.CAUTION)


class PtyWorkspaceTests(unittest.TestCase):
    """The screen driven by real bytes, which is the only way to prove the keys arrive.

    ``Tab``, ``Escape`` and the arrow keys are all escape sequences, and a scripted key skips the
    decoder that turns them into one keypress rather than several — the difference between a field
    that blurs and a field that receives ``[A`` as three characters of the answer.

    Every path used here exists on disk, because a remembered row that does not is a *question*,
    not an answer, and a test that pressed on through it would be asserting on a screen that stayed
    open for a different reason than the one under test.
    """

    SCRIPT = """
import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui.app import View, run
from tui.workspaces import WorkspaceStep

recent = json.loads(os.environ.get("TEST_RECENT", "[]"))
result = run(WorkspaceStep(recent=recent, head=()), View())
print("RESULT=" + json.dumps(result.value))
sys.exit(result.status)
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name).resolve()
        (self.base / "one").mkdir()
        (self.base / "two").mkdir()
        deep = self.base / "deep-project"
        deep.mkdir()
        self.deep = deep
        script = self.base / "screen.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.path = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)

    def run_screen(self, keys, recent=(), **kwargs):
        self.env["TEST_RECENT"] = json.dumps([str(item) for item in recent])
        return run_in_pty(
            [sys.executable, str(self.path)], keys=keys, env=self.env, cwd=ROOT, **kwargs
        )

    def test_a_remembered_directory_is_taken_with_one_return(self):
        session = self.run_screen(
            b"\r", recent=[self.base / "one", self.base / "two"], expect=b"workspace directory"
        )
        self.assertEqual(session.status, 0, session.screen[-400:])
        self.assertIn(f'RESULT="{self.base / "one"}"', session.screen)
        self.assertTrue(session.restored)

    def test_tab_completes_a_lone_directory_and_return_answers_it(self):
        prefix = (self.base / "deep").as_posix().lstrip("/")
        session = self.run_screen(
            [b"/" + prefix.encode(), b"\t", b"\r"], expect=b"workspace directory"
        )
        self.assertEqual(session.status, 0, session.screen[-400:])
        self.assertIn(f'RESULT="{self.deep}"', session.screen)
        self.assertTrue(session.restored)

    def test_escape_hands_the_arrows_back_and_an_arrow_selects_again(self):
        # Up lands on the field above the list, Escape leaves it, and Down moves the selection back
        # onto the remembered row. The sequence matters: a decoder that read the first byte of
        # ``ESC [ A`` as the Escape key would blur twice and answer the wrong row.
        session = self.run_screen(
            [b"\x1b[A", b"\x1b", b"\x1b[B", b"\r"],
            recent=[self.base / "one"],
            expect=b"New Workspace...",
        )
        self.assertEqual(session.status, 0, session.screen[-400:])
        self.assertIn(f'RESULT="{self.base / "one"}"', session.screen)
        self.assertTrue(session.restored)

    def test_a_rejected_answer_keeps_the_screen_and_the_next_attempt_is_its_own(self):
        # Enter on the seed ``/`` refuses the filesystem root; the operator then types a usable
        # path and Enter answers it, so a refusal must not have consumed the field or closed the
        # window.
        fresh = self.base / "brand-new"
        session = self.run_screen(
            [b"\r", b"/" + (self.base / "brand-new").as_posix().lstrip("/").encode(), b"\r"],
            expect=b"workspace directory",
        )
        self.assertEqual(session.status, 0, session.screen[-400:])
        self.assertIn(f'RESULT="{fresh}"', session.screen)
        self.assertTrue(session.restored)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
