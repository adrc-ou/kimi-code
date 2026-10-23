"""Completed steps acknowledge Enter and animate across the preparation gap."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import run_in_pty
from tests.test_tui import _Idle, _Script
from tools import models
from tools.definitions import load_definitions
from tools.tui import busy
from tools.tui.app import Modal, View
from tools.tui.caps import ANSI16, NONE, Caps
from tools.tui.keys import Key
from tools.tui.menu import SINGLE, Choice, ListStep

ROOT = Path(__file__).resolve().parents[1]


class FeedbackPaintingTests(unittest.TestCase):
    def modal(self, caps=None):
        caps = caps or Caps(color=ANSI16, columns=80, rows=24, probe=False)
        step = ListStep(title="Modules", choices=[Choice("a", "Alpha", checked=True)])
        return Modal(step, View(position=2, total=4), terminal=_Idle(caps))

    def test_completed_body_and_footer_dim_while_header_and_answer_stay_visible(self):
        modal = self.modal()
        modal.paint()
        before = modal.screen.snapshot()
        header = modal.screen._front[modal.frame.title.top][:]
        with patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": ""}):
            modal.paint(waiting=True)
        after = modal.screen.snapshot()
        self.assertEqual(before[modal.frame.rule_top.top:], after[modal.frame.rule_top.top:])
        self.assertEqual(header, modal.screen._front[modal.frame.title.top])
        self.assertIn("[x] Alpha", "\n".join(after))
        self.assertIn(busy.UNICODE_FRAMES[0], after[modal.frame.rail.top])
        for row in modal.screen._front[modal.frame.rule_top.top:]:
            self.assertTrue(all(cell.attr == "\033[2;90m" for cell in row))

    def test_monochrome_ascii_feedback_uses_weight_and_an_ascii_spinner(self):
        modal = self.modal(Caps(color=NONE, unicode=False, columns=40, rows=14, probe=False))
        with patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": ""}):
            modal.paint(waiting=True)
        self.assertIn("|", modal.screen.snapshot()[modal.frame.rail.top])
        for row in modal.screen._front[modal.frame.rule_top.top:]:
            self.assertTrue(all(cell.attr == "\033[2m" for cell in row))

    def test_accept_and_back_show_waiting_but_abort_and_standalone_steps_do_not(self):
        for key, held, waiting in (("Enter", True, True), ("Backspace", True, True),
                                   ("Ctrl-C", True, False), ("Enter", False, False)):
            terminal = _Script([Key(key)], Caps(color=ANSI16, probe=False))
            terminal.held = held
            step = ListStep(title="Modules", choices=[Choice("a", "Alpha")])
            modal = Modal(step, View(position=2, total=4, can_go_back=True), terminal=terminal)
            with patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": ""}):
                if key == "Ctrl-C":
                    with self.assertRaises(KeyboardInterrupt):
                        modal.run()
                else:
                    modal.run()
            output = "".join(terminal.written)
            self.assertEqual("\033[2;90m" in output, waiting)

    def test_present_model_labels_and_hints_fit_without_truncation_at_normal_width(self):
        _, definitions = load_definitions(ROOT)
        rows = models.agent_rows(definitions)
        options = [models._option(row, rows[0]["id"], rows[0]["id"]) for row in rows]
        caps = Caps(color=NONE, columns=80, rows=24, probe=False)
        step = ListStep(title="Models", mode=SINGLE, choices=options)
        modal = Modal(step, terminal=_Idle(caps))
        modal.paint()
        body = "\n".join(modal.screen.snapshot()[modal.frame.body.top:modal.frame.body.bottom])
        for option in options:
            self.assertIn(option.label, body)
            self.assertIn(option.hint, body)

    def test_narrow_and_wide_lists_resize_without_crossing_the_body_edge(self):
        options = [Choice("a", "A long identifying label " * 3, "provider and preference"),
                   Choice("b", "Another label", "provider")]
        for columns in (30, 50, 120):
            caps = Caps(color=NONE, columns=columns, rows=24, probe=False)
            modal = Modal(ListStep(title="Choices", choices=options), terminal=_Idle(caps))
            modal.paint()
            for line in modal.screen.snapshot():
                self.assertLessEqual(caps.width(line), columns)
            if columns == 120:
                self.assertIn(options[0].label, "\n".join(modal.screen.snapshot()))

    def test_runtime_wait_state_contains_no_frame_or_answer(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": directory}):
            (Path(directory) / busy.CLAIM).write_text("test-claim")
            modal = self.modal()
            modal.paint(waiting=True)
            state = json.loads((Path(directory) / busy.STATE).read_text())
            self.assertTrue(state["active"])
            self.assertEqual(set(state), {"active", "claim", "row", "column", "unicode", "styled"})
            self.assertNotIn("Alpha", json.dumps(state))
            self.assertEqual((Path(directory) / busy.STATE).stat().st_mode & 0o777, 0o600)


class FeedbackTerminalTests(unittest.TestCase):
    def test_frame_fills_real_window_with_piped_stdout_stale_hints_and_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "geometry.py"
            report = Path(directory) / "geometry.json"
            driver.write_text('''
import contextlib, fcntl, io, json, os, struct, sys, termios
from tools.tui import screen
from tools.tui.app import Modal, View
from tools.tui.menu import Choice, ListStep
from tools.tui.term import Terminal

directory, report = sys.argv[1:]
os.environ['HARNESS_RUNTIME_DIR'] = directory
screen.main(['enter', '--runtime-dir', directory])
os.environ['HARNESS_TUI_SCREEN'] = 'held'
records = []
try:
    # Workspace selection captures stdout as the answer; drawing uses the keyboard's tty.
    with os.fdopen(os.dup(sys.stdin.fileno()), 'w') as output, \
            contextlib.redirect_stdout(io.StringIO()):
        terminal = Terminal(stream=output, probe=False)
        initial = terminal.size
        with terminal:
            modal = Modal(ListStep(title='Geometry', choices=[Choice('a', 'Alpha')]),
                          View(1, 2), terminal=terminal)
            def record(waiting=False):
                modal.adopt()
                modal.paint(waiting=waiting)
                records.append(dict(size=terminal.size,
                    top=modal.screen.snapshot()[modal.frame.rule_top.top],
                    bottom=modal.screen.snapshot()[modal.frame.rule_bottom.top],
                    footer_bottom=modal.frame.footer.bottom,
                    footer=modal.screen.snapshot()[modal.frame.footer.top:]))
            record()
            while not any(key.name == 'Enter' for key in terminal.keys(timeout=0.1)):
                pass
            record(waiting=True)
            fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSWINSZ,
                        struct.pack('HHHH', 46, 160, 0, 0))
            record()
            record(waiting=True)
    with open(report, 'w') as target:
        json.dump(dict(initial=initial, frames=records), target)
finally:
    screen.main(['leave', '--runtime-dir', directory])
''')
            env = {**os.environ, "PYTHONPATH": str(ROOT), "TERM": "xterm-256color",
                   "COLUMNS": "80", "LINES": "24"}
            session = run_in_pty([sys.executable, str(driver), directory, str(report)],
                                 keys=b"\r", expect=b"Alpha", env=env,
                                 rows=40, columns=132)
            self.assertEqual(session.status, 0, session.screen[-500:])
            result = json.loads(report.read_text())
            self.assertEqual(result["initial"], [132, 40])
            for frame, (columns, rows) in zip(
                result["frames"], [(132, 40)] * 2 + [(160, 46)] * 2, strict=True
            ):
                self.assertEqual(frame["size"], [columns, rows])
                self.assertEqual(len(frame["top"]), columns)
                self.assertEqual(len(frame["bottom"]), columns)
                self.assertEqual(frame["footer_bottom"], rows)
                self.assertIn("<Enter> continue", "\n".join(frame["footer"]))
            self.assertTrue(session.restored)

    def test_spinner_moves_during_work_and_stops_before_next_screen_and_leave(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "driver.py"
            driver.write_text('''
import os, subprocess, sys, time
from tools.tui import screen
from tools.tui.app import Modal, View, run
from tools.tui.caps import Caps
from tools.tui.menu import Choice, ListStep
from tools.tui.term import Terminal

directory = sys.argv[1]
os.environ['HARNESS_RUNTIME_DIR'] = directory
screen.main(['enter', '--runtime-dir', directory])
os.environ['HARNESS_TUI_SCREEN'] = 'held'
animator = subprocess.Popen([sys.executable, screen.__file__, 'animate', '--runtime-dir', directory,
                             '--owner-pid', str(os.getpid())], stdin=subprocess.DEVNULL)
try:
    run(ListStep(title='Modules', choices=[Choice('a', 'Alpha')]), View(1, 2),
        terminal=Terminal(Caps(probe=False, titles=False), held=True))
    time.sleep(0.5)
    with Terminal(Caps(probe=False, titles=False), held=True) as terminal:
        modal = Modal(ListStep(title='Next screen', choices=[Choice('b', 'Beta')]),
                      View(2, 2), terminal=terminal)
        modal.adopt()
        modal.paint()
        while not any(key.name == 'Enter' for key in terminal.keys(timeout=0.1)):
            pass
finally:
    screen.main(['leave', '--runtime-dir', directory])
    animator.wait(timeout=2)
''')
            env = {**os.environ, "PYTHONPATH": str(ROOT), "TERM": "xterm-256color"}
            session = run_in_pty([sys.executable, str(driver), directory], keys=[b"\r", b"\r"],
                                 expect=b"Alpha", env=env, timeout=10, settle=0.6)
            self.assertEqual(session.status, 0, session.screen[-500:])
            before, after = session.output.split(b"Next screen", 1)
            frames = re.findall(rb"\x1b\[0m\x1b\[34m([^\x1b]+)\x1b\[0m\x1b\[\?2026l", before)
            self.assertGreaterEqual(len(set(frames)), 2, "waiting was not animated")
            self.assertNotRegex(after, rb"\x1b\[0m\x1b\[34m[^\x1b]+\x1b\[0m\x1b\[\?2026l")
            self.assertIn(b"\x1b[2;90m", before)
            self.assertEqual(session.output.count(b"\x1b[?1049h"), 1)
            self.assertEqual(session.output.count(b"\x1b[?1049l"), 1)
            self.assertTrue(session.restored)

    def test_background_animation_is_silent_without_a_terminal(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/tui/screen.py"), "animate"],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_animator_never_claims_a_dead_launcher_as_its_parent(self):
        # If exec is scheduled after its parent exited, the expected parent identity still
        # comes from the launcher, rather than silently accepting init/launchd as the owner.
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("tools.tui.busy.os.getppid", return_value=999):
            (Path(directory) / busy.CLAIM).write_text("test-claim")
            busy.animate(directory, output, owner=1000)
        self.assertEqual(output.getvalue(), "")
