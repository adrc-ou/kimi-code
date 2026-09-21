#!/usr/bin/env python3
"""The modal engine that drives ``./start.sh``'s interactive section.

Groups, ordered the way the engine is layered: the package's own surface (what a surface may reach
for), capabilities (what the terminal may be given), measurement (how wide a string really is),
decoding (what the terminal sent), geometry (what fits where), the generated footer (what the user
is told), flow state (what has been answered), then the three step shapes run against a scripted
terminal, and finally one run through a real pty. Everything before the pty group is pure and fast;
the pty group is slow, and is the only one that proves the sequences actually reach a terminal and
that the terminal comes back undamaged.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from dataclasses import replace as _replaced
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, so
# both `tools.tui` and `tests.helpers` need the root added explicitly to resolve under every way of
# running the suite.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import tools.tui as tui  # noqa: E402
from tests.helpers import run_in_pty  # noqa: E402
from tools.tui import flow  # noqa: E402
from tools.tui import screen as launch_screen  # noqa: E402
from tools.tui.app import (  # noqa: E402
    ACCEPT,
    FOCUS_UP,
    HELP,
    MIN_ROOM,
    TOGGLE,
    UNTETHERED_ROOM,
    Modal,
    Result,
    Step,
    View,
    navigation,
    rail_text,
)
from tools.tui.caps import ANSI256, NONE, TRUECOLOR, Caps, char_width, detect, width  # noqa: E402
from tools.tui.cells import ALT_OFF, ALT_ON  # noqa: E402
from tools.tui.forest import CHECK, FIXED, PLAIN, ForestStep, Node  # noqa: E402
from tools.tui.input import CARET, DELETE, FIELD, FieldState, FieldStep  # noqa: E402
from tools.tui.keys import Decoder, Key, bind, display, legend_lines, lookup  # noqa: E402
from tools.tui.layout import (  # noqa: E402
    COMFORT_BODY,
    GUTTER_WIDTH,
    MIN_BODY,
    SCROLLBAR_WIDTH,
    Line,
    Row,
    Segment,
    edge,
    focusable_rows,
    layout,
    page,
    scroll_for,
    thumb,
    visible_window,
    wrap,
)
from tools.tui.menu import SINGLE, Choice, ListStep  # noqa: E402

_ENV = ("TERM", "COLORTERM", "NO_COLOR", "TERM_PROGRAM", "LANG", "LC_ALL", "LC_CTYPE", "COLUMNS")


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, which is all :func:`detect` asks of it."""

    def isatty(self) -> bool:
        return True


#: A capability record for a painter that must not ask the terminal anything.
QUIET = Caps(color=NONE, columns=80, rows=24, probe=False)


class _Idle:
    """A terminal stand-in for a :class:`Modal` that is only ever asked to paint.

    The alternative — constructing the real :class:`~tui.term.Terminal` — would try to take over
    the suite's own standard output, which is a pipe, and the modal loop would then block forever
    on input that cannot arrive.
    """

    def __init__(self, caps=QUIET):
        self.caps = caps
        self.written: list[str] = []
        self.entered = 0
        self.left = 0
        self.closed = False

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.leave()
        return False

    def leave(self):
        self.left += 1

    @property
    def interactive(self):
        return True

    def probe(self):
        return None

    def resized(self):
        return False

    def write(self, payload):
        self.written.append(payload)

    def keys(self, timeout=None):
        raise AssertionError("this terminal answers no keys")


@dataclass(frozen=True)
class Picked:
    """A checkbox list's whole state: where the cursor is, and what is ticked.

    Frozen and replaced rather than mutated, because the loop hands a step's state to the painter
    after every key and a state that changes underneath a comparison is how a reset key ends up
    resetting nothing.
    """

    focus: int = 0
    chosen: tuple[str, ...] = ()


class Picker(Step):
    """The smallest step that reaches every path in the loop.

    Deliberately not one of the launcher's nine surfaces: a test that depends on what the model
    list happens to contain today fails for reasons that have nothing to do with the interface.
    """

    title = "Pick"
    rail = "Pick"

    def __init__(self, *, items=("alpha", "beta", "gamma"), toggleable=True, head=()):
        self.items = tuple(items)
        self.toggleable = toggleable
        self.head = tuple(head)

    def initial(self):
        return Picked()

    def rows(self, state):
        out = [Row.heading(Line(Segment(text))) for text in self.head]
        for name in self.items:
            box = "x" if name in state.chosen else " "
            out.append(Row.item(Line(Segment(f"[{box}] {name}")), name))
        return out

    def focus(self, state):
        return state.focus

    def with_focus(self, state, index):
        return _replaced(state, focus=index)

    def toggled(self, state, target):
        chosen = set(state.chosen)
        if target in chosen:
            chosen.discard(target)
        else:
            chosen.add(target)
        return _replaced(state, chosen=tuple(sorted(chosen)))

    def status(self, state):
        count = len(state.chosen)
        return Line(Segment(f"{count} of {len(self.items)} selected"))

    def commit(self, state):
        return Result(value=list(state.chosen), summary=", ".join(state.chosen))


class Package(unittest.TestCase):
    """What ``import tui`` promises its surfaces.

    A launcher step reaches for names through the package rather than through submodules, so the
    re-export list is an interface: a name that is advertised but never bound breaks the one
    statement every surface starts from, and it breaks it silently, because merely importing the
    package never reads ``__all__``.
    """

    def test_every_advertised_name_is_bound(self):
        missing = [name for name in tui.__all__ if not hasattr(tui, name)]
        self.assertEqual([], missing)


class Environment(unittest.TestCase):
    """``detect`` against a controlled environment.

    Each case names the variables it cares about and clears the rest, because a suite that inherits
    the developer's ``COLORTERM`` proves nothing about a machine without one.
    """

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in _ENV}
        self.addCleanup(self._restore)
        for name in _ENV:
            os.environ.pop(name, None)

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def detect(self, **kwargs):
        return detect(_Tty(), **kwargs)

    def test_a_terminal_asks_for_the_widest_tier_it_advertises(self):
        os.environ["TERM"] = "xterm-256color"
        self.assertEqual(self.detect().color, ANSI256)
        os.environ["COLORTERM"] = "truecolor"
        self.assertEqual(self.detect().color, TRUECOLOR)

    def test_a_pipe_gets_no_colour_at_all(self):
        os.environ["TERM"] = "xterm-256color"
        self.assertEqual(detect(io.StringIO()).color, NONE)

    def test_no_color_is_respected_but_not_over_an_explicit_request(self):
        # The convention's own FAQ: per-instance configuration outranks the ambient variable, so
        # ``--color=always`` is an answer about this run and NO_COLOR is a standing preference.
        os.environ["TERM"] = "xterm-256color"
        os.environ["NO_COLOR"] = "1"
        self.assertEqual(self.detect().color, NONE)
        self.assertEqual(self.detect(color="always").color, ANSI256)

    def test_a_flag_cannot_make_a_dumb_terminal_render_escape_sequences(self):
        os.environ["TERM"] = "dumb"
        self.assertEqual(self.detect(color="always").color, NONE)

    def test_a_never_beats_an_advertised_truecolor(self):
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        self.assertEqual(self.detect(color="never").color, NONE)

    def test_an_unknown_colour_mode_is_rejected_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            self.detect(color="rainbow")

    def test_dumb_and_screen_neither_nor_title_queries_are_offered_to_them(self):
        # A terminal that cannot be trusted with an OSC is one that will print it.
        os.environ["TERM"] = "dumb"
        self.assertFalse(self.detect().titles)
        self.assertFalse(self.detect().probe)
        os.environ["TERM"] = "screen.xterm-256color"
        self.assertFalse(self.detect().titles)
        os.environ["TERM"] = "xterm-256color"
        self.assertTrue(self.detect().titles)

    def test_the_locale_decides_the_glyph_tier(self):
        os.environ["LC_CTYPE"] = "C"
        self.assertFalse(self.detect().unicode)
        os.environ["LC_CTYPE"] = "en_US.UTF-8"
        self.assertTrue(self.detect().unicode)

    def test_an_ambiguous_glyph_is_wide_only_where_it_is_read_that_way(self):
        # The box and pointer characters this interface is built from are East Asian Ambiguous:
        # one cell in a Latin locale, two in a Korean, Japanese or Chinese one. Measuring them as
        # one where the terminal draws two is how a layout ends up overdrawn.
        os.environ["LC_CTYPE"] = "en_US.UTF-8"
        self.assertFalse(self.detect().ambiguous_wide)
        for locale in ("ko_KR.UTF-8", "ja_JP.UTF-8", "zh_CN.UTF-8"):
            os.environ["LC_CTYPE"] = locale
            self.assertTrue(self.detect().ambiguous_wide, locale)

    def test_an_explicit_tier_beats_the_ambient_locale(self):
        os.environ["LC_CTYPE"] = "ko_KR.UTF-8"
        self.assertFalse(self.detect(unicode_override=False).unicode)


class Measurement(unittest.TestCase):
    """Widths, because every box in the frame is placed with them."""

    def test_a_combining_mark_costs_nothing(self):
        # Composing a character must not shift a column: the cell is the one it sits on.
        self.assertEqual(width("e\u0301"), width("e"))
        self.assertEqual(char_width("\u0301", False), 0)

    def test_an_east_asian_character_costs_two_cells(self):
        self.assertEqual(width("\u4e00\u4e00"), 4)

    def test_an_ambiguous_character_follows_the_locale(self):
        self.assertEqual(width("\u2500", ambiguous_wide=False), 1)
        self.assertEqual(width("\u2500", ambiguous_wide=True), 2)

    def test_a_control_character_is_not_measured_as_a_cell(self):
        # ``Cell.text`` refuses to write one, so counting it would move the cursor on the grid and
        # not on screen.
        self.assertLessEqual(width("\x1b"), 1)


class Decoding(unittest.TestCase):
    """What a terminal sends, turned into what a step answers."""

    def decode(self, *chunks):
        decoder = Decoder()
        keys = []
        for chunk in chunks:
            keys.extend(decoder.feed(chunk))
        return keys

    def names(self, *chunks):
        return [key.name for key in self.decode(*chunks)]

    def test_the_named_keys_arrive_by_both_spellings(self):
        self.assertEqual(self.names(b"\x1b[A", b"\x1bOH"), ["Up", "Home"])
        self.assertEqual(self.names(b"\x1b[5~", b"\x1b[6~"), ["PgUp", "PgDn"])

    def test_a_two_byte_escape_and_a_lone_escape_are_different_keys(self):
        self.assertEqual(self.names(b"\x1b[Z"), ["BackTab"])
        self.assertEqual(self.names(b"\x1b"), [])

    def test_a_lone_escape_is_still_escape_when_nothing_follows_it(self):
        # Held back rather than guessed at, then flushed when the wait expires: dropping it would
        # lose a keypress, and reporting it immediately would turn a split arrow key into Escape.
        decoder = Decoder()
        self.assertEqual(decoder.feed(b"\x1b"), [])
        self.assertTrue(decoder.waiting)
        self.assertEqual([key.name for key in decoder.flush()], ["Escape"])
        self.assertFalse(decoder.waiting)

    def test_windows_openssh_double_enter_is_one_enter(self):
        self.assertEqual(self.names(b"\r\r"), ["Enter"])
        # ... and two enters that arrive separately are still two.
        self.assertEqual(self.names(b"\r", b"\r"), ["Enter", "Enter"])

    def test_a_control_byte_is_named_for_the_key_that_sent_it(self):
        self.assertEqual(self.names(b"\x04", b"\x12"), ["Ctrl-D", "Ctrl-R"])

    def test_a_query_reply_is_consumed_rather_than_typed(self):
        # DECRQSS answers arrive on the same descriptor as keystrokes. Read as input they would be
        # a burst of unbound keys, which after this suite's change means a status line full of
        # "does nothing here".
        self.assertEqual(self.names(b"\x1b[?64;1p", b"\x1b[B"), ["Down"])
        self.assertEqual(self.names(b"\x1b]0;title\x07"), [])

    def test_mouse_is_decoded_even_though_the_engine_does_not_enable_it(self):
        self.assertEqual(self.names(b"\x1b[<65;10;4M"), ["WheelDown"])
        keys = self.decode(b"\x1b[<0;12;5M")
        self.assertEqual(keys[0].column, 12)
        self.assertEqual(keys[0].row, 5)
        self.assertFalse(keys[0].right)

    def test_a_bound_key_resolves_and_an_unbound_one_does_not(self):
        table = navigation()
        self.assertEqual(lookup(table, Key("Enter"), View()), ACCEPT)
        self.assertEqual(lookup(table, Key("Space"), View()), TOGGLE)
        self.assertEqual(lookup(table, Key("x"), View()), "")

    def test_a_disabled_binding_answers_nothing_and_leaves_the_legend(self):
        table = (bind("go", "elsewhere", "g", when=lambda state: state),)
        self.assertEqual(lookup(table, Key("g"), False), "")
        self.assertEqual(lookup(table, Key("g"), True), "go")
        quiet = Caps(color=NONE, columns=80)
        self.assertEqual(legend_lines(table, False, quiet), [])
        self.assertEqual(legend_lines(table, True, quiet), ["g elsewhere"])


class Geometry(unittest.TestCase):
    """How a window is divided, and how a list is fitted into the part left over.

    The frame is the promise this interface makes to a small window — that it will stay legible
    rather than scroll off the end — so it is tested at the sizes where the promise is hardest to
    keep, not only at a comfortable 80x24.
    """

    def frame(self, columns=80, rows=24, footer_rows=1, rail=True):
        return layout(columns, rows, footer_rows=footer_rows, rail=rail)

    def test_the_regions_stack_top_to_bottom_at_a_comfortable_size(self):
        frame = self.frame()
        self.assertEqual((frame.title.top, frame.rail.top, frame.rule_top.top), (0, 1, 2))
        self.assertEqual(frame.body.top, 3)
        self.assertEqual(frame.rule_bottom.bottom, frame.status.top)
        self.assertEqual(frame.status.bottom, frame.footer.top)
        self.assertEqual(frame.footer.bottom, 24)

    def test_a_rail_with_nothing_to_say_leaves_its_row_to_the_body(self):
        # Every surface names itself in its title row, so an unnamed rail is not chrome — it is a
        # blank line above the list. The rest of the frame keeps its order all the same.
        silent = self.frame(rail=False)
        self.assertNotIn("rail", silent.shown)
        self.assertEqual(silent.body.top, 2)
        self.assertEqual(silent.body.height, self.frame().body.height + 1)
        self.assertEqual(silent.footer.top, self.frame().footer.top)

    def test_the_body_loses_the_gutter_and_the_scrollbar_to_nothing_else(self):
        frame = self.frame()
        self.assertEqual(frame.body.left, GUTTER_WIDTH)
        self.assertEqual(frame.body.right, frame.columns - 1)

    def test_chrome_is_given_up_from_the_bottom_up_as_the_window_shortens(self):
        # The status row and the rail are the two lines that repeat what the body already says, so
        # they go first; the title is what tells the user which step they are in and is the last
        # thing standing.
        self.assertNotIn("status", self.frame(rows=6).shown)
        self.assertNotIn("rail", self.frame(rows=5).shown)
        self.assertNotIn("title", self.frame(rows=4).shown)
        self.assertIn("title", self.frame(rows=5).shown)

    def test_the_body_keeps_at_least_its_minimum_at_every_size(self):
        # The single invariant the painter relies on: content never overlaps chrome, and never
        # vanishes. Swept rather than spot-checked, because the give-up loop has four regions and
        # three rules that can each win at a different size.
        for rows in range(4, 25):
            for columns in range(20, 101, 4):
                for wanted in (1, 2):
                    frame = self.frame(columns, rows, wanted)
                    with self.subTest(rows=rows, columns=columns, wanted=wanted):
                        self.assertGreaterEqual(frame.body.height, MIN_BODY)
                        self.assertLessEqual(frame.body.bottom, frame.footer.top)
                        self.assertLess(frame.footer.bottom, frame.screen.bottom + 1)
                        self.assertLessEqual(frame.body.width, columns)
                        self.assertGreaterEqual(frame.body.width, 1)

    def test_the_second_footer_row_is_surrendered_before_the_body_gets_thin(self):
        # An extra legend row is worth a row of list only while the list still shows a screenful.
        # The boundary is the exact height at which another footer row would take the body below
        # COMFORT_BODY, and it moves with the constant rather than with the wish.
        self.assertEqual(self.frame(rows=15, footer_rows=2).footer.height, 2)
        self.assertEqual(self.frame(rows=14, footer_rows=2).footer.height, 1)
        self.assertEqual(self.frame(rows=14, footer_rows=2).body.height, COMFORT_BODY)
        self.assertEqual(self.frame(rows=24, footer_rows=2).footer.height, 2)
        # ... and a window that only ever asked for one still gets one, however short it is.
        self.assertEqual(self.frame(rows=6, footer_rows=1).footer.height, 1)

    def test_a_footer_never_eats_the_window(self):
        self.assertEqual(self.frame(rows=4, footer_rows=9).footer.height, 1)

    def test_scrolling_moves_one_row_per_arrow_and_stops_at_the_last_page(self):
        # Bottom-alignment on a short final page is the difference between a list that ends and a
        # list that appears to have lost its last screenful.
        self.assertEqual(scroll_for(30, 8, 7, previous=0), 1)
        self.assertEqual(scroll_for(30, 8, 29, previous=20), 22)
        self.assertEqual(scroll_for(10, 8, 9, previous=0), 2)
        self.assertEqual(scroll_for(10, 8, 0, previous=5), 0)

    def test_a_two_row_viewport_cannot_also_keep_two_rows_of_context(self):
        # The slack is capped by the viewport rather than trusted: demanding two lines of context in
        # a three-row window would leave one row for the cursor and scroll on every keypress.
        self.assertEqual(scroll_for(30, 3, 20, previous=0, slack=2), 19)
        self.assertEqual(scroll_for(30, 2, 20, previous=0), 19)
        # One row of viewport buys no context at all, and must not scroll past the cursor either.
        self.assertEqual(scroll_for(30, 1, 20, previous=0, slack=2), 20)

    def test_the_thumb_always_shows_something_and_never_leaves_the_track(self):
        for scroll in range(0, 40, 3):
            window = visible_window(40, 8, scroll)
            start, end = thumb(window, 40, 8)
            with self.subTest(scroll=scroll):
                self.assertGreaterEqual(start, 0)
                self.assertLessEqual(end, 8)
                self.assertGreater(end, start)
        self.assertEqual(thumb(visible_window(40, 8, 0), 40, 8)[0], 0)
        self.assertEqual(thumb(visible_window(40, 8, 32), 40, 8)[1], 8)
        # A list that fits is not scrolled, so the thumb is the whole track rather than a hint.
        self.assertEqual(thumb(visible_window(6, 8, 0), 6, 8), (0, 8))
        self.assertEqual(thumb(visible_window(0, 8, 0), 0, 8), (0, 0))

    def test_a_page_moves_the_window_and_leaves_a_row_of_context_behind(self):
        self.assertEqual(page(30, 8, 0, 1), 7)
        self.assertEqual(page(30, 8, 7, 1), 14)
        self.assertEqual(page(30, 8, 3, -1), 0)
        # Paging stops at the last whole page rather than bottom-aligning a short tail.
        self.assertEqual(page(30, 8, 20, 1), 22)
        self.assertEqual(page(0, 8, 0, 1), 0)
        self.assertEqual(page(6, 8, 0, 1), 0)

    def test_the_cursor_lands_on_the_nearest_selectable_row_of_a_new_page(self):
        # Rows 4 and 5 are prose; the window slid to 8..16, so the cursor goes to the first thing in
        # it it may rest on, and not to 4, which is off the top.
        order = [0, 1, 2, 3, 6, 9]
        self.assertEqual(edge(order, 8, 16, 1), 5)
        self.assertEqual(edge(order, 8, 16, -1), 5)
        self.assertEqual(edge(order, 0, 4, 1), 0)
        # A window of pure prose takes the cursor nowhere, and says so.
        self.assertIsNone(edge(order, 4, 6, 1))
        self.assertIsNone(edge([], 0, 8, 1))

    def test_the_window_counts_what_it_hides_on_each_side(self):
        window = visible_window(30, 8, 10)
        self.assertEqual((window.first, window.last, window.above, window.below), (10, 18, 10, 12))
        self.assertFalse(visible_window(5, 8, 0).scrolling)
        self.assertTrue(window.scrolling)
        self.assertEqual(window.row_of(9), -1)
        self.assertEqual(window.row_of(10), 0)


class Footer(unittest.TestCase):
    """The generated legend — the affordance that replaces ``a``/``n``/``r``.

    These cases are the contract with the brief: everything decisive survives a narrow window,
    nothing printed is a key the step does not answer, and no printed label is a bare letter.
    """

    def setUp(self):
        self.caps = Caps(color=NONE, columns=80, rows=24, probe=False)
        self.table = Step().keys(None, View(can_go_back=True))

    def legend(self, table=None, view=None, width=80, rows=1):
        return legend_lines(
            table or self.table,
            view if view is not None else View(can_go_back=True),
            self.caps,
            width=width,
            rows=rows,
        )

    def test_the_decisive_keys_come_first_and_the_scrolling_hints_last(self):
        line = self.legend(rows=2)[0]
        self.assertLess(line.index("<Enter>"), line.index("<Backspace>"))
        self.assertLess(line.index("<Backspace>"), line.index("Space"))
        self.assertNotIn("move up", line)

    def test_a_long_legend_wraps_instead_of_truncating(self):
        lines = self.legend(width=80, rows=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("…"))
        for line in lines:
            self.assertLessEqual(width(line), 80)
        # Wrapping is by item: every entry printed is one the wide legend also printed, whole, so a
        # break can never land inside "move down" and leave a key on one row and its words on the
        # next. The ellipsis is the only thing that is ours and not an item.
        whole = self.legend(width=400, rows=1)[0].split(" · ")
        for line in lines:
            for piece in line.rstrip("· ").split(" · "):
                self.assertIn(piece.removesuffix("…"), whole, line)

    def test_a_window_too_narrow_for_two_rows_drops_items_and_says_so(self):
        lines = self.legend(width=40, rows=1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("…"))
        self.assertTrue(lines[0].startswith("<Enter> continue"))
        self.assertLessEqual(width(lines[0]), 40)

    def test_the_ellipsis_does_not_cost_a_hint_it_could_have_shown(self):
        # The mark used to be joined with a separator, which at some widths meant the row showed one
        # fewer item than it had room for in order to say "there is more".
        table = tuple(bind(f"a{i}", "word" * (i + 1), f"Key{i}") for i in range(6))
        lines = self.legend(table, width=30, rows=1)
        self.assertIn("word", lines[0])
        self.assertTrue(lines[0].endswith("…"))

    def test_a_key_that_is_not_offered_is_not_advertised(self):
        # ``Backspace`` carries its own condition, which is the whole reason the legend is
        # generated from the table rather than written by hand: a hand-written hint bar keeps
        # promising yesterday's keys, and a first step has nowhere to go back to.
        for back in (False, True):
            view = View(can_go_back=back)
            table = Picker().keys(None, view)
            shown = "\n".join(self.legend(table, view, rows=2))
            with self.subTest(can_go_back=back):
                self.assertEqual("previous step" in shown, back)

    def test_a_step_that_cannot_toggle_says_nothing_about_toggling(self):
        table = Picker(toggleable=False).keys(None, View())
        self.assertNotIn("toggle", "\n".join(self.legend(table, View(), rows=2)))
        self.assertEqual(lookup(table, Key("Space"), View()), "")

    def test_nothing_the_footer_prints_is_a_key_you_have_to_knowledge_about(self):
        # The defect named in the brief is "the opaque a/n/r expectation", and its shape is a label
        # that is one bare letter. Every spelling this interface prints is bracketed, an arrow, or
        # the word Space — and the one exception is ``?``, which is the convention for help rather
        # than an initialism for an action.
        allow = {"?", "Space", "↑", "↓", "←", "→"}
        for view in (View(), View(can_go_back=True)):
            for item in Step().keys(None, view):
                for label in item.label.split("/"):
                    with self.subTest(label=label):
                        self.assertTrue(
                            label in allow or (label.startswith("<") and label.endswith(">")),
                            label,
                        )

    def test_the_help_overlay_lists_every_spelling_of_every_key_it_answers(self):
        # ``? all keys`` is a promise. The footer prints one spelling per action to stay short, so
        # the overlay is where an alias like ``k`` can be found at all — an alias nobody can
        # discover is the same magic keystroke the brief forbids, only quieter.
        modal = Modal(Picker(), View(), terminal=_Idle(), caps=self.caps)
        text = "\n".join(line.text() for line in modal._help_lines())
        for item in modal._table():
            for name in item.keys:
                self.assertIn(display(name), text, name)

    def test_the_overlay_explains_a_key_it_is_not_currently_answering(self):
        modal = Modal(Picker(), View(can_go_back=False), terminal=_Idle(), caps=self.caps)
        text = "\n".join(line.text() for line in modal._help_lines())
        self.assertIn("not on this step", text)
        self.assertIn("<Backspace>", text)


class FlowState(unittest.TestCase):
    """What has been answered, so a step can be replayed instead of re-asked."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self.steps = ("models", "modules", "context")

    def flow(self, steps=None):
        return flow.Flow(self.dir, self.steps if steps is None else steps)

    def test_a_flow_that_has_not_run_knows_nothing(self):
        state = self.flow()
        self.assertEqual(state.status("models"), flow.MISSING)
        self.assertTrue(state.should_render("models"))
        self.assertFalse(flow.is_live(self.dir))

    def test_a_committed_step_is_replayed_and_the_back_target_is_not(self):
        state = self.flow()
        state.commit("models", "Qwen3.8-Flash-Next")
        self.assertEqual(state.status("models"), flow.COMMITTED)
        self.assertFalse(state.should_render("models"))
        state.go_back("modules")
        self.assertEqual(state.status("models"), flow.TARGET)
        self.assertTrue(state.should_render("models"))

    def test_planning_a_step_counts_it_and_planning_none_removes_it(self):
        # A surface asks "do I render?" and answers "here is how many screens I have" in one call,
        # because the two must not disagree. Zero is a refusal: the step leaves the sequence, which
        # is what keeps the rail's total honest when a step turns out to have no question in it.
        state = self.flow()
        self.assertTrue(state.plan("models", 1))
        self.assertEqual(state.rail("context"), (3, 3))
        self.assertFalse(state.plan("modules", 0))
        self.assertEqual(state.rail("context"), (2, 2))
        self.assertEqual(state.sequence(), ("models", "context"))
        # A refusal is not an answer, so the step is still free to render on a later pass.
        self.assertTrue(self.flow().should_render("modules"))

    def test_planning_a_replaying_step_neither_counts_it_nor_forgets_it(self):
        # The back target's screens were counted on the pass that drew them, and a step answering
        # from its stored choice must not delete that answer by declaring this pass's zero screens.
        state = self.flow()
        state.commit("models", "a")
        self.assertFalse(state.plan("models", 1))
        self.assertFalse(state.plan("models", 0))
        self.assertFalse(state.should_render("models"))
        self.assertEqual(state.recap(), (("models", "a"),))

    def test_going_back_lands_on_the_previous_answered_step_not_the_previous_name(self):
        # A step that was never asked — a deselected module's version menu — has no answer to go
        # back to, and landing there would prompt for something the user is not choosing.
        state = self.flow(("one", "two", "three"))
        state.commit("one")
        self.assertEqual(state.go_back("three"), "one")
        self.assertEqual(state.previous("two"), "one")
        self.assertEqual(state.previous("one"), "")

    def test_an_unknown_step_has_nowhere_to_go_back_to(self):
        self.assertEqual(self.flow().previous("nothing"), "")

    def test_passing_through_the_target_clears_it(self):
        # Otherwise the flow would render that step on every subsequent pass, forever.
        state = self.flow()
        state.commit("models")
        state.go_back("modules")
        state.commit("models", "again")
        self.assertEqual(state.target, "")
        self.assertFalse(state.should_render("models"))

    def test_committing_is_idempotent_and_forgetting_asks_again(self):
        state = self.flow()
        state.commit("models", "first")
        state.commit("models", "second")
        self.assertEqual(state.recap(), (("models", "second"),))
        state.forget("models")
        self.assertTrue(state.should_render("models"))
        self.assertEqual(state.recap(), ())

    def test_the_recap_is_in_flow_order_not_answer_order(self):
        state = self.flow()
        state.commit("context", "c")
        state.commit("models", "m")
        self.assertEqual([name for name, _ in state.recap()], ["models", "context"])

    def test_the_state_is_written_atomically_and_privately(self):
        self.flow().commit("models")
        path = flow.path_for(self.dir)
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        # No scratch file is left beside it, whichever way the write finished.
        self.assertEqual([item.name for item in self.dir.iterdir()], [flow.FILE_NAME])

    def test_a_file_we_cannot_parse_is_a_flow_that_has_not_run(self):
        # Not a crash, and not a replay of something that was never answered.
        Path(flow.path_for(self.dir)).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.flow().status("models"), flow.MISSING)
        self.assertFalse(flow.is_live(self.dir))
        self.flow().commit("models")
        self.assertEqual(self.flow().status("models"), flow.COMMITTED)

    def test_a_step_the_launcher_stopped_declaring_keeps_its_answer(self):
        # The sequence is passed by every call; if it changed mid-launch, an old committed step must
        # not become unrecognisable and re-prompt the user for something already settled.
        state = self.flow()
        state.commit("context")
        narrower = flow.Flow(self.dir, ("models",))
        self.assertIn("context", narrower.steps)
        self.assertFalse(narrower.should_render("context"))

    def test_the_live_flag_is_what_a_destructive_step_checks(self):
        # ``modules.py`` removes docker images; while a flow is live, going back could still want
        # them, so the flag has to be readable in-process by anything that destroys.
        state = self.flow()
        self.assertFalse(flow.is_live(self.dir))
        state.begin()
        self.assertTrue(flow.is_live(self.dir))
        self.assertTrue(self.flow().live)
        state.finish()
        self.assertFalse(flow.is_live(self.dir))
        self.assertFalse(Path(flow.path_for(self.dir)).exists())

    def test_the_shell_drives_the_same_machine_the_python_does(self):
        # start.sh reads this through ``flow.py`` one word per call, so the CLI is the real
        # interface and not a convenience wrapper around it.
        argv = ["--runtime-dir", str(self.dir), "--steps", "models,modules"]

        def call(*rest):
            """The exit status and whatever was printed, since the printed part is the answer."""
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = flow.main([*argv, *rest])
            return status, buffer.getvalue()

        self.assertEqual(call("status", "models"), (flow.CONTINUE, "new\n"))
        self.assertEqual(call("commit", "models", "--summary", "Qwen3.8-Flash-Next")[0], 0)
        self.assertEqual(call("status", "models"), (0, "committed\n"))
        # ``live`` is a flag the shell tests with ``&&``, so it answers in its status and says
        # nothing — a printed word would be read as a step name by anything parsing the output.
        self.assertEqual(call("live"), (1, ""))
        call("begin")
        self.assertEqual(call("live"), (0, ""))
        self.assertEqual(call("back", "modules"), (0, "models\n"))
        self.assertEqual(call("status", "models"), (0, "target\n"))
        self.assertEqual(call("recap"), (0, "models\tQwen3.8-Flash-Next\n"))
        call("end")
        self.assertFalse(Path(flow.path_for(self.dir)).exists())

    def test_the_rail_numbers_the_sequence_rather_than_the_surface_showing_it(self):
        # A picker that counts its own screens restarts at one halfway through the launch, which
        # stops "N of M" being a map of where the user is. The flow is the only thing that knows
        # what came before.
        state = self.flow()
        self.assertEqual(state.rail("models"), (1, 3))
        self.assertEqual(state.rail("modules"), (2, 3))
        self.assertEqual(state.rail("context"), (3, 3))

    def test_a_surface_that_has_more_than_one_screen_declares_it(self):
        state = self.flow()
        state.declare("models", 2)
        self.assertEqual(state.rail("models", 0), (1, 4))
        self.assertEqual(state.rail("models", 1), (2, 4))
        self.assertEqual(state.rail("modules"), (3, 4))

    def test_the_declared_count_survives_the_process_boundary(self):
        # Every step is its own process, so a count learned in one has to be readable from the next.
        self.flow().declare("context", 3)
        self.assertEqual(self.flow().rail("context"), (3, 5))

    def test_a_step_that_renders_nothing_leaves_the_sequence_so_the_total_stays_honest(self):
        state = self.flow()
        state.skip("modules")
        self.assertEqual(state.rail("models"), (1, 2))
        self.assertEqual(state.rail("context"), (2, 2))
        self.assertIn("modules", state.steps)
        self.assertNotIn("modules", state.sequence())
        # The launcher names every step on every call, so a skip has to outrank the name list rather
        # than be undone by the next one.
        self.assertNotIn("modules", self.flow().sequence())
        self.assertEqual(self.flow().rail("context"), (2, 2))

    def test_a_skipped_step_that_gives_an_answer_or_a_screen_comes_back(self):
        # A step that rendered nothing has nothing to have answered either, and a later pass that
        # finds a question in it has to ask it rather than replay the value it dropped.
        answered = self.flow()
        answered.skip("modules")
        self.assertEqual(answered.status("modules"), flow.MISSING)
        answered.commit("modules", "comfyui")
        self.assertIn("modules", self.flow().sequence())
        self.assertFalse(self.flow().should_render("modules"))

        rendering = self.flow()
        rendering.skip("modules")
        rendering.declare("modules", 2)
        self.assertEqual(self.flow().rail("modules"), (2, 4))

    def test_a_step_more_ambiguous_than_expected_still_fits_its_own_rail(self):
        # A surface declaring one screen and finding two questions to ask is a miscount, not a
        # licence to print "9 of 8".
        state = self.flow()
        self.assertEqual(state.rail("context", 9), (3, 3))

    def test_a_step_the_flow_never_heard_of_still_gets_a_number(self):
        # --steps is an override for tests and for an older launcher, so an unlisted name must
        # degrade to the end of the rail rather than raise while a screen is being drawn.
        state = self.flow()
        self.assertEqual(state.rail("unknown"), (3, 3))

    def test_a_step_reports_what_it_has_to_draw_and_the_flow_picks_the_halves(self):
        # One call is the whole contract a surface has: the count it worked out becomes either
        # screens to number or a step that leaves the sequence.
        state = self.flow()
        state.report("modules", 2)
        self.assertEqual(state.rail("modules"), (2, 4))
        state.report("modules", 0)
        self.assertNotIn("modules", state.sequence())
        state.report("modules", 3)
        self.assertEqual(state.rail("modules"), (2, 5))

    def test_a_surveyed_step_with_no_screen_never_reaches_the_rail(self):
        # The rail's total is the one figure on the screen the user cannot recompute, so a launch
        # that turns out to have one question cannot announce eight. A forecast taken before the
        # first screen is drawn is what lets that launch say "1 of 1", and a step answered at zero
        # is not on it at all.
        state = self.flow()
        state.survey({"models": 0, "modules": 1, "context": 0})
        self.assertEqual(state.visible(), ("modules",))
        self.assertEqual(state.rail("modules"), (1, 1))

    def test_a_forecast_survives_the_process_boundary(self):
        # The survey is one process and every step is another, so the guess has to be readable
        # from the screen it is there to number.
        self.flow().survey({"models": 0})
        self.assertEqual(self.flow().rail("context"), (2, 2))

    def test_a_step_counts_itself_once_it_has_reached_the_screen(self):
        # A forecast is a guess about a step the user has not met and a fact about one they have.
        # The step's own number wins from then on, and the rest of the guesswork stays in charge of
        # every screen still to come.
        state = self.flow()
        state.survey({"models": 1, "modules": 1, "context": 1})
        state.declare("modules", 3)
        self.assertEqual(state.rail("modules", 0), (2, 5))
        self.assertEqual(state.rail("modules", 2), (4, 5))
        self.assertEqual(state.screens_for("context"), 1)

    def test_a_forecast_cannot_undo_a_count_a_step_already_gave(self):
        # The launcher takes its survey once, before the pass. Steps that rendered on an earlier
        # pass of the same launch have counted themselves already and have no reason to be second-
        # guessed by a forecast that could not see them.
        state = self.flow()
        state.declare("modules", 2)
        state.survey({"modules": 0})
        self.assertEqual(state.screens_for("modules"), 2)
        self.assertIn("modules", state.visible())

    def test_a_step_forecast_at_zero_still_gets_its_screen_if_it_finds_one(self):
        # Zero is the launcher's guess and never the step's answer. A step that reaches a screen
        # after being forecast out of one has to be able to rejoin the sequence, or a wrong guess
        # would silently swallow a prompt the user needed.
        state = self.flow()
        state.survey({"modules": 0})
        self.assertEqual(state.rail("context"), (2, 2))
        self.assertTrue(state.plan("modules", 1))
        self.assertEqual(state.rail("modules"), (2, 3))

    def test_the_shell_asks_for_the_same_numbers_the_python_does(self):
        argv = ["--runtime-dir", str(self.dir), "--steps", "models,modules"]

        def call(*rest):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = flow.main([*argv, *rest])
            return status, buffer.getvalue()

        self.assertEqual(call("rail", "modules"), (0, "2 2\n"))
        self.assertEqual(call("declare", "modules", "--screens", "3")[0], 0)
        self.assertEqual(call("rail", "modules"), (0, "2 4\n"))
        self.assertEqual(call("skip", "models")[0], 0)
        self.assertEqual(call("rail", "modules"), (0, "1 3\n"))
        self.assertEqual(call("status", "models"), (0, "new\n"))

    def test_the_launcher_hands_its_forecast_over_in_one_line(self):
        argv = ["--runtime-dir", str(self.dir), "--steps", "models,modules,context"]

        def call(*rest):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = flow.main([*argv, *rest])
            return status, buffer.getvalue()

        self.assertEqual(call("survey", "--counts", "models=0,notacount,context=2")[0], 0)
        self.assertEqual(call("rail", "modules"), (0, "1 3\n"))
        self.assertEqual(call("rail", "context"), (0, "2 3\n"))

    def test_a_count_nobody_can_read_is_dropped_rather_than_guessed(self):
        # A launch must not fail because a progress bar was guessed at, so an unparseable pair is
        # no answer rather than a zero one - while zero, which is the answer the launcher most
        # wants, stays perfectly legal to pass.
        self.assertEqual(
            flow.parse_counts("models=1,modules=,junk,context=-3"),
            {"models": 1, "context": 0},
        )
        self.assertEqual(flow.parse_counts(""), {})


class _Script(_Idle):
    """A terminal with a fixed set of keystrokes, so a run is reproducible and instant.

    Running out of keys is a failure rather than a return: a step that stays open after every key
    it was given is exactly the hung interface the launcher must not ship.
    """

    def __init__(self, keys=(), caps=QUIET):
        super().__init__(caps)
        self.pending = list(keys)

    def keys(self, timeout=None):
        if not self.pending:
            raise AssertionError("the step is still open after its keys ran out")
        return [self.pending.pop(0)]


class _Tail(Picker):
    """A short list whose prose continues below the last row the cursor can rest on.

    The step exists to pin the one bug it is easiest to ship: when the viewport is *derived* from
    the cursor each paint, content that no row can focus is content nobody can ever bring on screen,
    however loudly the overflow line says it is down there.
    """

    def rows(self, state):
        out = super().rows(state)
        out.append(Row.gap())
        out.extend(Row.heading(Line(Segment(f"note {index}"))) for index in range(20))
        return out


class ModalTests(unittest.TestCase):
    """The loop: one step, on screen, until it answers.

    Painting is asserted through :meth:`~tui.cells.Screen.snapshot` — the text a user would read
    off the screen, in screen order — rather than against the byte stream, because the byte stream
    is the diff optimiser's business and every assertion here is about content.
    """

    def modal(self, step=None, *, view=None, keys=(), caps=QUIET):
        self.script = _Script(list(keys), caps)
        return Modal(step or Picker(), view or View(), terminal=self.script, caps=caps)

    def shown(self, modal):
        modal.paint()
        text = "\n".join(modal.screen.snapshot())
        # No assertion below can pass on a painted object repr, which is the way a renderer fails
        # quietly: the text is *in* there, so an ``assertIn`` is satisfied while the user sees
        # ``Segment(text='...', attr='')`` where a status line should be.
        self.assertNotIn("Segment(", text)
        self.assertNotIn("object at 0x", text)
        return text

    def test_the_arrow_keys_move_the_cursor_and_space_toggles_the_row_under_it(self):
        modal = self.modal(keys=[Key("Down"), Key("Space"), Key("Enter")])
        result = modal.run()
        self.assertTrue(result.accepted)
        self.assertEqual(result.value, ["beta"])
        self.assertEqual(result.summary, "beta")
        self.assertEqual((self.script.entered, self.script.left), (1, 1))

    def test_the_rail_names_the_step_only_when_the_title_has_not(self):
        # The rail's job is progress; the step's name is a bonus it may not repeat. Three shapes,
        # one rule: never say from the rail what the title row already said.
        self.assertEqual(rail_text(View(position=2, total=9), Picker()), "2 of 9")
        self.assertEqual(rail_text(View(), Picker()), "")
        shorter = Picker()
        shorter.rail = "Pick one"
        self.assertEqual(rail_text(View(), shorter), "Pick one")
        self.assertEqual(rail_text(View(position=1, total=2), shorter), "1 of 2 · Pick one")
        # And the row is only there when it has something in it.
        self.assertNotIn("rail", self.modal(Picker()).frame.shown)
        self.assertIn("rail", self.modal(Picker(), view=View(position=1, total=3)).frame.shown)

    def test_the_focused_row_is_marked_in_a_gutter_that_never_moves(self):
        modal = self.modal(Picker(head=("Choices",)), keys=[Key("Down")])
        before = self.shown(modal)
        self.assertIn("▌ [ ] alpha", before)
        modal.handle(Key("Down"))
        after = self.shown(modal)
        self.assertIn("▌ [ ] beta", after)
        # The heading is a row but not a target, so the cursor steps over it — and the label column
        # does not move whether or not anything is marked, which is why an unwashed row reads as
        # "  [ ]" and not as a shift.
        self.assertEqual(before.splitlines()[3], "▌ [ ] alpha")
        self.assertEqual(after.splitlines()[3], "  [ ] alpha")
        self.assertEqual(after.splitlines()[4], "▌ [ ] beta")
        self.assertEqual(before.splitlines()[4], "  [ ] beta")

    def test_the_status_row_says_what_the_current_answer_amounts_to(self):
        modal = self.modal()
        modal.handle(Key("Space"))
        self.assertEqual(self.shown(modal).splitlines()[modal.frame.status.top], "1 of 3 selected")

    def test_a_key_that_answers_nothing_says_so_rather_than_ignoring_you(self):
        # A keypress with no visible consequence is how a user decides the program has hung.
        modal = self.modal()
        self.assertIsNone(modal.handle(Key("q")))
        shown = self.shown(modal)
        self.assertIn("does nothing here", shown)
        self.assertIn("press ? for keys", shown)
        # ... and it did not change the answer.
        self.assertIn("0 of 3 selected", shown)

    def test_a_note_shares_the_status_row_with_the_step_s_own_answer(self):
        modal = self.modal()
        modal.handle(Key("Space"))
        modal.handle(Key("Down"))
        shown = self.shown(modal)
        self.assertIn("1 of 3 selected", shown)
        self.assertNotIn("does nothing here", shown)

    def test_the_reset_key_restores_the_options_and_the_cursor_together(self):
        modal = self.modal()
        for key in (Key("Down"), Key("Space"), Key("Down"), Key("Space")):
            modal.handle(key)
        self.assertEqual(modal.session.state.chosen, ("beta", "gamma"))
        self.assertEqual(modal.session.state.focus, 2)
        modal.handle(Key("Ctrl-R"))
        self.assertEqual(modal.session.state, Picker().initial())
        self.assertIn("choices reset", self.shown(modal))

    def test_back_is_a_key_only_where_there_is_a_step_to_return_to(self):
        view = View(position=3, total=9, can_go_back=True)
        modal = self.modal(view=view, keys=[Key("Backspace")])
        result = modal.run()
        self.assertTrue(result.going_back)
        self.assertEqual(result.status, flow.GO_BACK)
        self.assertEqual(self.script.left, 1)

        other = self.modal(view=View())
        self.assertIsNone(other.handle(Key("Backspace")))
        self.assertIn("does nothing here", self.shown(other))

    def test_a_body_the_cursor_cannot_cross_still_scrolls_to_its_last_row(self):
        # The overflow line promises the tail exists; the viewport owes the user a way to reach it.
        modal = self.modal(_Tail(items=("alpha", "beta")))
        opening = self.shown(modal)
        self.assertIn("more below", opening)
        self.assertNotIn("note 19", opening)
        modal.handle(Key("End"))
        bottom = self.shown(modal)
        self.assertIn("note 19", bottom)
        # ...and the count goes away once there is nothing left to count.
        self.assertNotIn("more below", bottom)
        # The cursor came with the window rather than being stranded off the top of it.
        self.assertIn("▌", bottom)
        modal.handle(Key("Home"))
        self.assertIn("▌ [ ] alpha", self.shown(modal))

    def test_page_down_walks_the_whole_body_row_by_visible_row(self):
        modal = self.modal(_Tail(items=("alpha", "beta")))
        seen: set[int] = set()
        for _ in range(12):
            for line in self.shown(modal).splitlines():
                words = line.split()
                if len(words) >= 2 and words[0] == "note" and words[1].isdigit():
                    seen.add(int(words[1]))
            modal.handle(Key("PgDn"))
        self.assertIn(19, seen, "the last row never came on screen")
        self.assertEqual(seen, set(range(20)), "a row was skipped over by the page")

    def test_down_at_the_last_row_moves_the_content_instead_of_going_inert(self):
        modal = self.modal(_Tail(items=("alpha", "beta")))
        self.shown(modal)
        modal.handle(Key("Down"))
        modal.handle(Key("Down"))
        # The cursor is now on the last selectable row and cannot go further, but the key still has
        # an answer: the content slides one row, and the cursor stays on screen while it does.
        first = modal.session.scroll
        modal.handle(Key("Down"))
        self.assertGreater(modal.session.scroll, first)
        self.assertIn("▌ [ ] beta", self.shown(modal))
        # Up does the same thing from the other end.
        modal.handle(Key("Up"))
        modal.handle(Key("Up"))
        modal.handle(Key("Up"))
        self.assertIn("▌ [ ] alpha", self.shown(modal))

    def test_the_help_overlay_answers_only_itself(self):
        modal = self.modal(Picker(items=tuple(f"item{i}" for i in range(40))))
        modal.handle(Key("Down"))
        modal.handle(Key("Space"))
        state = modal.session.state
        modal.handle(Key("?"))
        self.assertTrue(modal.session.help_open)
        opened = self.shown(modal)
        self.assertIn("Keys", opened)
        self.assertIn("PgDn", opened)
        # The list is not on screen while the reference is, and nothing behind it changed.
        self.assertNotIn("[ ] item0", opened)
        modal.handle(Key("Down"))
        modal.handle(Key("PgDn"))
        self.assertGreater(modal.session.help_scroll, 0)
        self.assertEqual(modal.session.state, state)
        modal.handle(Key("Escape"))
        self.assertFalse(modal.session.help_open)
        self.assertEqual(modal.session.state, state)
        self.assertIn("[ ] item1", self.shown(modal))

    def test_a_line_built_by_the_other_import_name_of_this_engine_still_paints_as_text(self):
        # A tool under ``tools/`` imports this package as ``tui``; the suite imports it as
        # ``tools.tui``. Both are live in one interpreter and they are *different classes*, so a
        # line built by one and painted by the other used to satisfy no isinstance() check, fall
        # through to the scalar case, and paint its repr on the status row.
        from tui.layout import Line as flat_line
        from tui.layout import Segment as flat_segment

        class Foreign(Picker):
            def status(self, state):
                return flat_line(flat_segment(f"{len(state.chosen)} chosen"))

        modal = self.modal(Foreign())
        modal.handle(Key("Space"))
        self.assertEqual(self.shown(modal).splitlines()[modal.frame.status.top], "1 chosen")

    def test_the_overlay_and_the_footer_list_the_step_s_own_keys_too(self):
        class WithExtra(Picker):
            def custom(self, state):
                return (bind("pick-all", "every row", "Ctrl-A"),)

            def answer(self, state, action, target):
                del target
                if action == "pick-all":
                    return _replaced(state, chosen=self.items), None
                return state, None

        modal = self.modal(WithExtra())
        self.assertIn("every row", self.shown(modal))
        modal.handle(Key("Ctrl-A"))
        self.assertEqual(len(modal.session.state.chosen), 3)

    def test_an_overflowing_list_says_how_much_is_out_of_view(self):
        modal = self.modal(Picker(items=tuple(f"item{i}" for i in range(40))))
        shown = self.shown(modal)
        self.assertIn("more below", shown)
        self.assertNotIn("[ ] item39", shown)
        modal.handle(Key("End"))
        shown = self.shown(modal)
        self.assertIn("[ ] item39", shown)
        self.assertIn("more above", shown)
        self.assertNotIn("mores", shown)

    def test_a_short_window_asks_the_footer_for_one_row_and_draws_one(self):
        caps = Caps(color=NONE, columns=40, rows=10, probe=False)
        modal = self.modal(Picker(items=tuple(f"item{i}" for i in range(12))), caps=caps)
        shown = self.shown(modal)
        self.assertEqual(modal.frame.footer.height, 1)
        self.assertEqual(modal.frame.body.height, 5)
        self.assertIn("<Enter> continue", shown)
        self.assertNotIn("<Home>", shown)
        self.assertIn("↓ 8 more below", shown)

    def test_the_screen_comes_back_when_the_step_is_abandoned(self):
        # Ctrl-C normally arrives as a signal, which is why the terminal is in cbreak and not raw.
        # If a terminal does hand over the byte, the alternate screen still has to be left behind.
        modal = self.modal(keys=[Key("Ctrl-C")])
        with self.assertRaises(KeyboardInterrupt):
            modal.run()
        self.assertEqual(self.script.left, 1)

    def test_a_step_that_cannot_toggle_drops_the_key_from_the_footer(self):
        modal = self.modal(Picker(toggleable=False))
        self.assertNotIn("toggle", self.shown(modal))
        self.assertIsNone(modal.handle(Key("Space")))
        self.assertEqual(modal.session.state, Picker().initial())


class MenuTests(unittest.TestCase):
    """:class:`~tui.menu.ListStep` — the step class six of the nine surfaces are built from.

    Asserted through what the launcher receives and what the screen shows, never through the
    step's private state, because the point of the class is that a caller can build a menu without
    knowing how it is drawn.
    """

    def step(self, **changes):
        options = changes.pop("options", None)
        defaults = {
            "title": "Modules",
            "prompt": "Choose which modules to load",
            "choices": [
                Choice("alpha", "Alpha", "adds a GPU service"),
                Choice("beta", "Beta", "adds an editor"),
                Choice("gamma", "Gamma"),
            ],
        }
        defaults.update(changes)
        if options is not None:
            defaults["choices"] = options
        return ListStep(**defaults)

    def modal(self, step, *, view=None, keys=(), caps=QUIET):
        self.script = _Script(list(keys), caps)
        return Modal(step, view or View(), terminal=self.script, caps=caps)

    def shown(self, modal):
        modal.paint()
        text = "\n".join(modal.screen.snapshot())
        self.assertNotIn("Segment(", text)
        self.assertNotIn("object at 0x", text)
        return text

    def answer(self, step, keys, **changes):
        modal = self.modal(step, keys=[Key(name) for name in keys] + [Key("Enter")], **changes)
        return modal.run()

    # -- what opens ------------------------------------------------------------------------

    def test_the_remembered_answer_opens_with_the_cursor_on_it(self):
        step = self.step(previous=["beta"])
        result = self.answer(step, [])
        # Landing on the remembered row is what makes Enter correct without reading the list,
        # which is the entire value of remembering anything.
        self.assertEqual(result.value, ["beta"])
        self.assertEqual(step.initial().focus, 1)

    def test_an_explicit_answer_beats_last_launch_which_beats_the_declared_default(self):
        options = [
            Choice("alpha", "Alpha", checked=True),
            Choice("beta", "Beta"),
            Choice("gamma", "Gamma"),
        ]
        self.assertEqual(self.step(options=options).initial().chosen, ("alpha",))
        self.assertEqual(
            self.step(options=options, previous=["gamma"]).initial().chosen, ("gamma",)
        )
        self.assertEqual(
            self.step(options=options, previous=["gamma"], initial=["beta"]).initial().chosen,
            ("beta",),
        )

    def test_an_answer_to_a_step_that_has_nothing_to_remember_starts_empty(self):
        # ``previous`` naming nothing selectable must not silently select the default: a user who
        # deselected every module last time has to see an empty list, not a re-ticked one.
        step = self.step(previous=["nothing"], initial=())
        self.assertEqual(step.initial().chosen, ())
        self.assertEqual(self.answer(step, []).value, [])

    def test_a_row_no_key_can_reach_is_never_part_of_the_opening_answer(self):
        options = [Choice("alpha", "Alpha", enabled=False, checked=True), Choice("beta", "Beta")]
        step = self.step(options=options)
        self.assertEqual(step.initial().chosen, ())

    # -- what the keys do ------------------------------------------------------------------

    def test_space_toggles_and_enter_returns_ids_in_list_order_not_click_order(self):
        result = self.answer(self.step(), ["Space", "Down", "Down", "Space", "Up", "Space"])
        self.assertEqual(result.value, ["alpha", "beta", "gamma"])
        self.assertEqual(result.summary, "Alpha, Beta, Gamma")

    def test_the_cursor_steps_over_the_heading_and_the_disabled_row(self):
        options = [
            Choice("alpha", "Alpha"),
            Choice("secret", "Restricted", "needs a GPU", enabled=False),
            Choice("beta", "Beta"),
        ]
        modal = self.modal(self.step(options=options))
        for _ in range(4):
            self.assertIsNone(modal.handle(Key("Down")))
        # Two selectable rows, so the fourth Down must still be sitting on the second one.
        self.assertEqual(modal.session.state.focus, 1)
        self.assertEqual(modal.step.option(modal.session.state).id, "beta")

    def test_the_disabled_row_is_shown_but_the_cursor_never_lands_on_it(self):
        options = [
            Choice("alpha", "Alpha"),
            Choice("secret", "Restricted", "needs a GPU", enabled=False),
        ]
        shown = self.shown(self.modal(self.step(options=options)))
        self.assertIn("Restricted", shown)
        self.assertIn("needs a GPU", shown)
        self.assertIn("[-]", shown)

    def test_enter_on_a_radio_is_the_choice_and_space_is_not_even_offered(self):
        step = self.step(mode=SINGLE, previous=["beta"])
        modal = self.modal(step, keys=[Key("Space"), Key("Down"), Key("Enter")])
        shown = self.shown(modal)
        self.assertNotIn("Space toggle", shown)
        result = modal.run()
        self.assertEqual(result.value, "gamma")
        self.assertEqual(result.summary, "Gamma")

    def test_a_radio_marks_its_row_with_parentheses_a_checkbox_with_brackets(self):
        # Two states that look alike are two ways to misread one screen.
        self.assertEqual(self.step().mark(self.step().choices[0], True), "[x]")
        radio = self.step(mode=SINGLE)
        self.assertEqual(radio.mark(radio.choices[0], True), "(x)")
        self.assertEqual(radio.mark(radio.choices[0], False), "( )")

    def test_reset_restores_the_opening_answer_and_the_opening_cursor_together(self):
        step = self.step(previous=["alpha"])
        modal = self.modal(step)
        modal.handle(Key("Down"))
        modal.handle(Key("Space"))
        self.assertEqual(modal.session.state.chosen, ("alpha", "beta"))
        self.assertEqual(modal.session.state.focus, 1)
        modal.handle(Key("Ctrl-R"))
        self.assertEqual(modal.session.state.chosen, ("alpha",))
        self.assertEqual(modal.session.state.focus, step.initial().focus)
        # The opening cursor, not merely row zero: reset has to undo the walk as well as the ticks.
        self.assertEqual(modal.session.state.focus, 0)

    def test_an_unknown_mode_is_refused_rather_than_rendered_as_multi(self):
        with self.assertRaises(ValueError):
            self.step(mode="either")

    # -- what the screen says --------------------------------------------------------------

    def test_the_heading_and_the_prompt_are_rows_the_cursor_skips(self):
        modal = self.modal(self.step())
        shown = self.shown(modal).splitlines()
        self.assertIn("Modules", shown[0])
        self.assertIn("Choose which modules to load", shown[2])
        self.assertIn("▌ [ ] Alpha", shown[4])
        # Two Downs from the first row land on the third, not on the blank or the heading above it.
        for _ in range(2):
            self.assertIsNone(modal.handle(Key("Down")))
        self.assertEqual(modal.step.option(modal.session.state).id, "gamma")

    def test_the_hint_column_lines_up_and_the_status_counts_only_reachable_rows(self):
        modal = self.modal(self.step())
        shown = self.shown(modal).splitlines()
        hints = [line for line in shown if "adds " in line]
        self.assertEqual(len(hints), 2)
        # One column, whatever it happens to be: the label field is the widest label in the list, so
        # a hint never starts beside one label and one cell further along on the next row down.
        starts = set()
        for line in hints:
            for word in ("adds a GPU", "adds an"):
                if word in line:
                    starts.add(line.find(word))
        self.assertEqual(len(starts), 1)
        self.assertEqual(
            {line.find("[") for line in shown if "[ ]" in line or "[-]" in line},
            {GUTTER_WIDTH},
        )
        self.assertIn("0 of 3 selected", shown[modal.frame.status.top])
        modal.handle(Key("Space"))
        self.assertIn("1 of 3 selected", self.shown(modal).splitlines()[modal.frame.status.top])

    def test_a_hint_clipped_from_the_list_is_still_readable_whole_in_the_status_row(self):
        options = [
            Choice("alpha", "Alpha", "a hint far too long to sit beside a label in forty columns"),
            Choice("beta", "Beta", "short"),
        ]
        modal = self.modal(self.step(mode=SINGLE, options=options), caps=QUIET)
        shown = self.shown(modal)
        self.assertIn(
            "a hint far too long to sit beside a label in forty columns",
            shown.splitlines()[modal.frame.status.top],
        )

    def test_the_labels_are_what_the_summary_says_and_the_ids_are_what_the_launcher_gets(self):
        # A manifest may rename an option without stranding the identifier recorded last launch;
        # that is why these two are separate columns of the same row.
        result = self.answer(self.step(previous=["alpha"]), [])
        self.assertEqual(result.value, ["alpha"])
        self.assertEqual(result.summary, "Alpha")

    def test_a_terminal_with_no_colour_gets_the_same_layout_as_one_with_it(self):
        options = [Choice("alpha", "Alpha", "a hint"), Choice("beta", "Beta")]
        plain = self.shown(self.modal(self.step(options=options), caps=QUIET))
        coloured = Caps(columns=80, rows=24, probe=False)
        painted = self.shown(self.modal(self.step(options=options), caps=coloured))
        self.assertEqual(plain, painted)
        step = self.step(options=options)
        step.caps = coloured
        rows = step.rows(step.initial())
        self.assertTrue(any(seg.attr for row in rows for seg in row.line.segments))
        step.caps = QUIET
        rows = step.rows(step.initial())
        self.assertFalse(any(seg.attr for row in rows for seg in row.line.segments))

    def test_an_empty_list_answers_nothing_rather_than_hanging(self):
        step = self.step(choices=[])
        self.assertFalse(step.selectable())
        modal = self.modal(step, keys=[Key("Enter")])
        self.assertIn("0 of 0 selected", self.shown(modal))
        result = modal.run()
        self.assertTrue(result.accepted)
        self.assertEqual(result.value, [])


class FieldTests(unittest.TestCase):
    """The one-field step: what it echoes, what it refuses, and what it promises in the footer.

    A field is the only surface in the launcher where the user's keystrokes become content rather
    than commands, so most of what is pinned here is a *negative*: the secret does not reach the
    screen, does not reach the scrollback, and the one key that means two different things says
    which one it currently means.
    """

    def step(self, **kwargs):
        kwargs.setdefault("title", "Provider key")
        kwargs.setdefault("prompt", "NRP API key (NRP_API_KEY)")
        step = FieldStep(**kwargs)
        step.caps = QUIET
        return step

    def modal(self, step=None, *, view=None, keys=(), caps=QUIET):
        self.script = _Script(list(keys), caps)
        return Modal(step or self.step(), view or View(), terminal=self.script, caps=caps)

    def shown(self, modal):
        modal.paint()
        return "\n".join(modal.screen.snapshot())

    def typed(self, modal, value):
        """Send ``value`` as the keystrokes that would have produced it."""
        for character in value:
            modal.handle(Key(character, character) if character != " " else Key("Space"))

    def flowed(self, modal):
        """The screen as one string, with the wrap and the scrollbar column undone.

        Wrapped rows are only meaningful joined, and the scrollbar occupies a column of its own on
        every row, so both have to go before a sentence can be looked for whole.
        """
        return " ".join(
            " ".join(line.rstrip(" │█") for line in self.shown(modal).splitlines()).split()
        )

    def test_typing_fills_the_field_and_space_is_a_character_not_a_toggle(self):
        modal = self.modal()
        self.typed(modal, "ab c")
        self.assertEqual(modal.session.state.text, "ab c")
        result = modal.handle(Key("Enter"))
        self.assertTrue(result.accepted)
        self.assertEqual(result.value, "ab c")

    def test_a_masked_field_shows_glyphs_and_never_the_value(self):
        step = self.step()
        modal = self.modal(step)
        self.typed(modal, "hunter2secret")
        screen = self.shown(modal)
        self.assertNotIn("hunter2", screen)
        self.assertNotIn("secret", screen)
        self.assertIn("•" * 11, screen)

    def test_an_unmasked_field_shows_what_was_typed(self):
        modal = self.modal(self.step(masked=False))
        self.typed(modal, "/var/lib/data")
        self.assertIn("/var/lib/data", self.shown(modal))

    def test_the_caret_marks_the_field_even_with_no_colour_and_nothing_in_it(self):
        # An empty field is the one state a user must not have to guess at, and colour is off in a
        # plain terminal, so the cursor has to be a glyph.
        self.assertIn(CARET, self.shown(self.modal()))

    def test_backspace_edits_until_the_field_is_empty_then_asks_for_the_previous_step(self):
        modal = self.modal(view=View(position=2, total=2, can_go_back=True))
        self.typed(modal, "ab")
        self.assertIsNone(modal.handle(Key("Backspace")))
        self.assertEqual(modal.session.state.text, "a")
        self.assertIsNone(modal.handle(Key("Backspace")))
        self.assertEqual(modal.session.state.text, "")
        # Empty, and the same key changes meaning — which is why the footer names it per keypress.
        self.assertEqual(modal.handle(Key("Backspace")).status, flow.GO_BACK)

    def test_backspace_cannot_leave_the_first_step(self):
        modal = self.modal(view=View(position=1, total=2))
        self.assertIsNone(modal.handle(Key("Backspace")))
        # The key says so rather than doing something else, and the legend never promised it.
        self.assertIn("does nothing here", modal.session.note)
        footer = self.shown(modal).splitlines()[-modal.frame.footer.height :]
        self.assertFalse(any("Backspace" in line for line in footer))

    def test_the_field_row_is_where_the_cursor_opens(self):
        step = self.step(head=("Where the key comes from",))
        rows = step.rows(FieldState())
        self.assertEqual([row.target for row in rows], [None, None, None, None, FIELD])
        # The head's row and the field take the cursor; the gap, the question and the gap under it
        # are drawn but not walked. Only the last row carries a target, so the prose can be read
        # but never answered, and a stray ``Enter`` cannot choose something the user did not pick.
        self.assertEqual([row.focusable for row in rows], [True, False, False, False, True])
        self.assertEqual(step.focus(step.initial()), 1)
        self.assertEqual(step.focus(FieldState(at=0)), 0)
        # Deleting a character is not an answer, and never closes the step.
        self.assertIsNone(step.answer(FieldState(text="a"), DELETE, FIELD)[1])

    def test_a_letter_that_is_a_menu_alias_elsewhere_still_reaches_the_field(self):
        # ``k`` and ``j`` are the arrow aliases on every menu of the launcher and ``?`` opens the
        # key reference, so all three are keystrokes the engine answers. Here they are characters
        # of a value: an API key is a word like any other, and one that quietly arrives three
        # letters short is a worse betrayal than a legend with one row less in it.
        modal = self.modal()
        self.typed(modal, "sk-jk?q")
        self.assertEqual(modal.session.state.text, "sk-jk?q")

    def test_a_field_step_advertises_no_key_it_has_to_spend_on_typing(self):
        # The other half of the same trade. The footer and the reference are generated from this
        # table, so pruning the spellings is also what stops the frame promising them.
        step = self.step()
        view = View(can_go_back=True)
        table = step.keys(step.initial(), view)
        actions = {item.action for item in table}
        self.assertNotIn(HELP, actions)
        self.assertIn(FOCUS_UP, actions)
        spelled = {name for item in table for name in item.keys}
        self.assertTrue({"?", "k", "j"}.isdisjoint(spelled))
        self.assertTrue({"Up", "Down", "Enter", "Backspace", "Ctrl-R"}.issubset(spelled))
        modal = self.modal(step)
        footer = self.shown(modal).splitlines()[-modal.frame.footer.height :]
        self.assertFalse(any("?" in line for line in footer))

    def test_an_empty_field_refuses_to_close_and_says_why(self):
        modal = self.modal()
        result = modal.handle(Key("Enter"))
        self.assertIsNone(result)
        self.assertIn("nothing entered", modal.session.note)

    def test_spaces_are_nothing_only_where_the_caller_strips_them(self):
        # A credential is stripped before it is stored, so spaces would arrive as nothing; a module
        # stores what was typed, so a space is the value. The step cannot know this, so the caller
        # says so and the field stops arguing.
        stripped = self.modal(self.step(whitespace_is_value=False))
        self.typed(stripped, "   ")
        self.assertIsNone(stripped.handle(Key("Enter")))
        self.assertIn("only spaces", stripped.session.note)
        self.assertTrue(self.step().complete("   "))

    def test_typing_after_a_refusal_clears_the_complaint(self):
        modal = self.modal()
        modal.handle(Key("Enter"))
        self.typed(modal, "x")
        self.assertEqual(modal.session.note, "")

    def test_reset_empties_the_field_and_says_that_is_what_it_did(self):
        modal = self.modal()
        self.typed(modal, "hunter2")
        modal.handle(Key("Ctrl-R"))
        self.assertEqual(modal.session.state.text, "")
        self.assertEqual(modal.session.note, "value cleared")

    def test_a_secret_never_becomes_a_scrollback_line(self):
        # The launcher prints each answer after the screen closes, which is right for a chosen
        # module name and a leak for a key.
        modal = self.modal()
        self.typed(modal, "hunter2secret")
        self.assertEqual(modal.handle(Key("Enter")).summary, "")

    def test_the_status_counts_characters_without_revealing_them(self):
        modal = self.modal()
        self.assertIn("nothing entered", self.shown(modal))
        self.typed(modal, "abcde")
        screen = self.shown(modal)
        self.assertIn("5 characters · hidden", screen)
        self.assertNotIn("abcde", screen)
        unmasked = self.modal(self.step(masked=False))
        self.typed(unmasked, "abcde")
        self.assertIn("5 characters", self.shown(unmasked))
        self.assertNotIn("hidden", self.shown(unmasked))

    def test_a_long_explanation_wraps_and_the_body_scrolls_instead_of_clipping(self):
        # The sentence naming where to persist a key is the one thing on this screen that must
        # survive a narrow window whole.
        step = self.step(
            head=(
                "NRP: Qwen3.8-Flash-Next (issued at https://nrp.ai/documentation/billing)",
                "Set NRP_API_KEY in .env to persist it; this value is for this session only.",
            )
        )
        modal = self.modal(step, caps=Caps(color=NONE, columns=40, rows=14, probe=False))
        # Flatten the wrap: what must survive a narrow window is the whole sentence telling the
        # user where to persist the key, not the prefix of it that fit on the first row.
        self.assertIn(
            "Set NRP_API_KEY in .env to persist it; this value is for this session only.",
            self.flowed(modal),
        )
        # And the sentence that did not fit is announced as hidden rather than silently dropped.
        self.assertIn("more above", self.shown(modal))

    def test_the_arrows_move_the_view_when_the_explanation_overflows(self):
        # Saying "2 more above" is only an affordance if a key reaches it. The cursor can leave the
        # field row and walk up into the prose, and the viewport follows it, which is also how the
        # scroll returns when the user comes back down to type.
        step = self.step(
            head=(
                "NRP: Qwen3.8-Flash-Next (issued at https://nrp.ai/documentation/billing)",
                "Set NRP_API_KEY in .env to persist it; this value is for this session only.",
            )
        )
        modal = self.modal(step, caps=Caps(color=NONE, columns=40, rows=14, probe=False))
        opened = step.focus(modal.session.state)
        self.assertNotIn("Qwen3.8-Flash-Next", self.shown(modal))
        for _ in range(4):
            modal.handle(Key("Up"))
        self.assertLess(step.focus(modal.session.state), opened)
        self.assertIn("Qwen3.8-Flash-Next", self.shown(modal))
        modal.handle(Key("End"))
        self.assertEqual(step.focus(modal.session.state), opened)
        self.assertNotIn("Qwen3.8-Flash-Next", self.shown(modal))
        # Reading the head is not answering the step: Enter still commits the value from whichever
        # row the cursor happens to be resting on.
        self.assertIsNone(modal.handle(Key("Enter")))
        self.typed(modal, "abc")
        self.assertEqual(modal.handle(Key("Enter")).value, "abc")

    def test_a_value_longer_than_the_window_is_clipped_at_the_edge_not_wrapped(self):
        # Bullets are a texture, so the row keeps its single height and the count carries the truth.
        step = self.step()
        modal = self.modal(step)
        self.typed(modal, "x" * 200)
        self.assertIn("200 characters · hidden", self.shown(modal))


class WrapTests(unittest.TestCase):
    """``layout.wrap`` — the row-breaker a heading is cut with before it becomes rows."""

    def test_words_are_broken_at_spaces_and_the_lines_fit(self):
        lines = wrap("the quick brown fox jumps", 12, QUIET)
        self.assertTrue(all(len(line) <= 12 for line in lines))
        self.assertEqual(" ".join(lines), "the quick brown fox jumps")

    def test_a_word_wider_than_the_room_keeps_its_own_line(self):
        # Half a URL is useless; a clipped one is at least whole in the buffer for the next scroll.
        long_word = "supercalifragilistic"
        self.assertEqual(wrap(f"{long_word} expi", 10, QUIET), [long_word, "expi"])

    def test_blank_text_is_one_blank_line_not_no_lines(self):
        # A caller drawing rows has nothing to do with an empty list.
        self.assertEqual(wrap("", 20, QUIET), [""])
        self.assertEqual(wrap("   ", 20, QUIET), [""])


class RoomTests(unittest.TestCase):
    """The width a step may actually draw in."""

    def test_a_step_with_no_caps_says_so_rather_than_claiming_the_terminal(self):
        self.assertEqual(FieldStep(title="t", prompt="p").room, UNTETHERED_ROOM)

    def test_the_gutter_and_the_scrollbar_are_paid_for_out_of_the_window(self):
        step = FieldStep(title="t", prompt="p")
        step.caps = Caps(color=NONE, columns=80, rows=24, probe=False)
        self.assertEqual(step.room, 80 - GUTTER_WIDTH - SCROLLBAR_WIDTH)

    def test_an_absurdly_narrow_window_keeps_a_field_usable(self):
        step = FieldStep(title="t", prompt="p")
        step.caps = Caps(color=NONE, columns=2, rows=24, probe=False)
        self.assertEqual(step.room, MIN_ROOM)


class PtyMenu(unittest.TestCase):
    """The shared list step, on a real terminal driver.

    Kept separate from :class:`Pty`, whose hand-rolled step proves the bare ``Step`` contract that
    the forest and the secret prompts are built on. This proves the convenience every menu actually
    uses, in the flat import identity a real surface gets.
    """

    SCRIPT = """
import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui.menu import SINGLE, Choice, ListStep
from tui.app import View, run

mode = sys.argv[1] if len(sys.argv) > 1 else "multi"
step = ListStep(
    title="Modules",
    prompt="Choose which modules to load",
    mode=mode,
    previous=["beta"],
    choices=[
        Choice("alpha", "Alpha", "adds a GPU service"),
        Choice("beta", "Beta", "adds an editor"),
        Choice("gamma", "Gamma"),
        Choice("off", "Restricted", "needs a machine this one lacks", enabled=False),
    ],
)
result = run(step, View(position=3, total=9, label="Modules", can_go_back=True))
print("RESULT=" + json.dumps(result.value))
print("SUMMARY=" + result.summary)
sys.exit(result.status)
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        script = Path(holder.name) / "menu.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.path = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)

    def run_menu(self, keys, *, mode="multi", **kwargs):
        return run_in_pty(
            [sys.executable, str(self.path), mode], keys=keys, env=self.env, cwd=ROOT, **kwargs
        )

    def test_the_menu_paints_marks_and_answers_on_a_real_terminal(self):
        session = self.run_menu(b"\r", expect=b"Choose which modules to load")
        self.assertEqual(session.status, flow.CONTINUE)
        self.assertIn('RESULT=["beta"]', session.screen)
        self.assertIn("SUMMARY=Beta", session.screen)
        self.assertIn("1 of 3 selected", session.screen)
        self.assertIn("[-] Restricted", session.screen)
        self.assertIn("<Enter> continue", session.screen)
        self.assertIn("Space toggle", session.screen)
        self.assertIn("<Backspace> previous step", session.screen)
        self.assertNotIn("Segment(", session.screen)
        self.assertTrue(session.restored)

    def test_space_on_a_toggled_row_and_the_cursor_cannot_reach_the_disabled_one(self):
        session = self.run_menu(b" \x1b[B\x1b[B\x1b[B\x1b[B \r", expect=b"Restricted")
        # Down four times from Beta: Gamma, then Restricted is skipped, then it wraps nowhere.
        self.assertIn('RESULT=["gamma"]', session.screen)
        self.assertTrue(session.restored)

    def test_a_radio_menu_offers_no_toggle_key_at_all(self):
        session = self.run_menu(b"\x1b[B\r", mode="single", expect=b"( )")
        self.assertNotIn("Space toggle", session.screen)
        self.assertIn('RESULT="gamma"', session.screen)
        self.assertTrue(session.restored)


class PtyField(unittest.TestCase):
    """The one-field step, on a real terminal driver.

    :class:`FieldTests` proves what the key table says; this proves what the bytes do. A secret is a
    burst of ordinary characters into a program that is repainting while it reads, which is the one
    arrangement in which a decoder, an echoing line discipline and a diff painter can each quietly
    lose a letter — and a key that arrives two letters short is not a cosmetic failure.
    """

    SECRET = "sk-jk?q"

    SCRIPT = """
import json
import os
import sys

# The flat identity a real surface gets, so the child and the engine agree on one Segment class.
sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui.app import View, run
from tui.input import FieldStep

masked = (sys.argv[1] if len(sys.argv) > 1 else "hidden") == "hidden"
step = FieldStep(
    title="Provider key",
    prompt="NRP API key (NRP_API_KEY)",
    head=(
        "NRP: Qwen3.8-Flash-Next (issued at https://nrp.ai/documentation/billing)",
        "Set NRP_API_KEY in .env to persist it; this value is for this session only.",
    ),
    masked=masked,
)
result = run(step, View(position=8, total=9, label="Provider key", can_go_back=True))
print("RESULT=" + json.dumps(result.value))
sys.exit(result.status)
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        script = Path(holder.name) / "field.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.path = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)

    def run_field(self, keys, *, masked=True, **kwargs):
        return run_in_pty(
            [sys.executable, str(self.path), "hidden" if masked else "shown"],
            keys=keys,
            env=self.env,
            cwd=ROOT,
            **kwargs,
        )

    @staticmethod
    def typed(value: str, *after: bytes) -> list[bytes]:
        """``value`` one keystroke at a time, then ``after``.

        Chunking is the point: the loop paints once per batch of keys it reads, so a string written
        in one go is applied between two frames and the frames a user actually saw — bullets
        appearing, a legend changing meaning — never reach the terminal at all.
        """
        return [value[index : index + 1].encode() for index in range(len(value))] + list(after)

    def test_a_key_the_menus_answer_as_a_command_arrives_as_a_character(self):
        # ``k``, ``j`` and ``?`` are all keystrokes the launcher's menus answer — two arrow aliases
        # and the key reference — and all three occur in a real API key. They reach the value here,
        # whole, through a decoder and a repainting screen.
        session = self.run_field(self.typed(self.SECRET, b"\r"), expect=b"NRP API key")
        self.assertEqual(session.status, flow.CONTINUE)
        self.assertIn(f'RESULT="{self.SECRET}"', session.screen)
        # Once — in the line the launcher's own process printed after the screen came back. The
        # modal drew no character of it: a repaint writes only the cells that changed, and the seven
        # cells this one changed were seven bullets.
        self.assertEqual(session.screen.count(self.SECRET), 1)
        self.assertEqual(session.screen.count("•"), len(self.SECRET))
        self.assertIn("characters · hidden", session.screen)
        self.assertTrue(session.restored)

    def test_a_blank_answer_is_refused_in_place_of_closing_the_step(self):
        session = self.run_field([b"\r", *self.typed("abc", b"\r")], expect=b"NRP API key")
        self.assertIn("type or paste", session.screen)
        self.assertIn('RESULT="abc"', session.screen)
        self.assertTrue(session.restored)

    def test_the_legend_names_what_backspace_means_at_that_moment(self):
        session = self.run_field([*self.typed("ab"), b"\x7f", b"\x7f", b"\x7f"], expect=b"API key")
        self.assertEqual(session.status, flow.GO_BACK)
        self.assertIn("RESULT=null", session.screen)
        # Two deletions empty the field and the third asks for the previous step. The proof is the
        # order: the legend had to say ``delete`` before it could go back to saying ``previous
        # step``, which is the same key changing meaning in front of the user, per keypress.
        self.assertGreater(session.screen.rindex("previous step"), session.screen.index("delete"))
        self.assertTrue(session.restored)

    def test_a_visible_field_shows_what_was_typed_and_omits_the_reference(self):
        session = self.run_field(self.typed("abc", b"\r"), expect=b"NRP API key", masked=False)
        self.assertIn('RESULT="abc"', session.screen)
        # A count row that does not claim to be hiding anything, which is the only difference an
        # unmasked field makes.
        self.assertIn("characters", session.screen)
        self.assertNotIn("hidden", session.screen)
        # The footer and the reference come from one table, so a step that had to give ``?`` up to
        # the value stops promising it too.
        self.assertNotIn("? all keys", session.screen)
        self.assertNotIn("Space toggle", session.screen)
        self.assertIn("<Enter> continue", session.screen)
        self.assertTrue(session.restored)

    def test_the_arrows_scroll_the_explanation_that_did_not_fit(self):
        # The two sentences are five rows and a short window shows three of them, so the cursor has
        # to be able to leave the field or the ``more above`` line names content nobody can reach.
        session = self.run_field(
            [b"\x1b[A", b"\x1b[A", b"\x1b[A", b"\x1b[A", *self.typed(self.SECRET, b"\r")],
            expect=b"more above",
            rows=13,
            columns=40,
        )
        self.assertIn(f'RESULT="{self.SECRET}"', session.screen)
        self.assertIn("NRP: Qwen3.8-Flash-Next", session.screen)
        self.assertTrue(session.restored)


class Pty(unittest.TestCase):
    """One whole run against a real terminal driver.

    Everything above this class is a simulation, and a simulation shares its assumptions with the
    code it tests. This is the group that checks the assumptions: that ``isatty`` is true where the
    engine thinks it is, that the alternate screen is actually entered and left, that the arrow keys
    arrive as the bytes this program decodes, and that the terminal settings are handed back.
    """

    SCRIPT = """
import json
import os
import sys

# One module identity for the engine, which is what a real surface gets: ``tools/`` on the path and
# ``tui`` imported flat. Importing it twice — once as ``tui`` and once as ``tools.tui`` — gives two
# distinct ``Segment`` classes, and a line built by one painted by the other renders as a repr.
sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui.app import Result, Step, View, run
from tui.layout import Line, Row, Segment


class Picker(Step):
    title = "Pick"
    rail = "Pick"

    def __init__(self, items):
        self.items = items

    def initial(self):
        return (0, ())

    def rows(self, state):
        _, chosen = state
        out = []
        for name in self.items:
            mark = "x" if name in chosen else " "
            out.append(Row.item(Line(Segment(f"[{mark}] {name}")), name))
        return out

    def focus(self, state):
        return state[0]

    def with_focus(self, state, index):
        return (index, state[1])

    def toggled(self, state, target):
        chosen = set(state[1])
        chosen ^= {target}
        return (state[0], tuple(sorted(chosen)))

    def status(self, state):
        return Line(Segment(f"{len(state[1])} of {len(self.items)} selected"))

    def commit(self, state):
        return Result(value=list(state[1]), summary=", ".join(state[1]))


count = int(sys.argv[1]) if len(sys.argv) > 1 else 3
items = ("alpha", "beta", "gamma") if count == 3 else tuple(f"item{i}" for i in range(count))
result = run(Picker(items), View(position=2, total=9, label="Pick", can_go_back=True))
print("RESULT=" + json.dumps(result.value))
print("SUMMARY=" + result.summary)
sys.exit(result.status)
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        script = Path(holder.name) / "surface.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.path = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            # The engine takes its size from the terminal driver, as it must inside a window the
            # user can resize, but two stale variables would outrank that and describe a window
            # this pty does not have. NO_COLOR and TERM are dropped so the child sees a colour
            # terminal whatever the suite's own shell happens to be.
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)

    def run_surface(self, keys, *, items=3, **kwargs):
        return run_in_pty(
            [sys.executable, str(self.path), str(items)],
            keys=keys,
            env=self.env,
            cwd=ROOT,
            **kwargs,
        )

    def test_a_key_sequence_selects_a_row_and_answers_the_step(self):
        session = self.run_surface(b"\x1b[B \r", expect=b"[ ] beta", rows=24, columns=80)
        self.assertEqual(session.status, flow.CONTINUE)
        self.assertIn('RESULT=["beta"]', session.screen)
        self.assertIn("SUMMARY=beta", session.screen)
        # The body painted, inside the frame the brief asks for: a title, a step rail, a marked
        # cursor row, and a generated legend naming the keys in words rather than letters.
        # Assertions are on fragments, not whole rows: the output is the *stream* of updates a
        # terminal was told to apply, so a row the renderer painted in three writes arrives as
        # three pieces with the cursor moves stripped out of the middle.
        self.assertIn("Pick", session.screen)
        self.assertIn("2 of 9", session.screen)
        self.assertIn("▌", session.screen)
        self.assertIn("[ ] beta", session.screen)
        self.assertIn("<Enter> continue", session.screen)
        self.assertNotIn("Segment(", session.screen)

    def test_the_alternate_screen_is_borrowed_and_handed_back(self):
        session = self.run_surface(b"\r", expect=b"[ ] beta")
        self.assertIn(ALT_ON.encode(), session.output)
        self.assertIn(ALT_OFF.encode(), session.output)
        self.assertLess(
            session.output.index(ALT_ON.encode()), session.output.index(ALT_OFF.encode())
        )
        self.assertTrue(session.restored)

    def test_a_backspace_returns_its_own_status_rather_than_an_error(self):
        session = self.run_surface(b"\x7f", expect=b"[ ] beta")
        self.assertEqual(session.status, flow.GO_BACK)
        # Going back answers nothing, and the launcher must be able to tell that apart from an
        # answer of "nothing selected" — hence a distinct exit status rather than an empty value.
        self.assertIn("RESULT=null", session.screen)
        self.assertTrue(session.restored)

    def test_the_window_size_comes_from_the_terminal_and_not_the_environment(self):
        # A 10-row window has to shed the second footer row rather than overflow, which is only
        # worth testing against a real driver because the driver is where the two size sources
        # disagree: the launcher's own shell may well have stale COLUMNS in its environment.
        session = self.run_surface(
            b"\x1b[F \r", expect=b"more below", rows=10, columns=40, items=12
        )
        self.assertEqual(session.status, flow.CONTINUE)
        # One footer row, the overflow note, and the last row reachable from the first.
        self.assertIn("more below", session.screen)
        self.assertNotIn("<Home>", session.screen)
        self.assertIn("item11", session.screen)
        self.assertIn('RESULT=["item11"]', session.screen)
        self.assertTrue(session.restored)


class SpooledSentences(unittest.TestCase):
    """``screen.note``, which is how a step says something while the launcher has the window.

    A plain ``print`` under a held screen scrolls the questions away and the next frame paints over
    only part of it, so every sentence the launcher owes the operator between two screens waits in
    ``screen.NOTES`` instead. Waiting is the whole risk: a spool that silently swallowed a warning,
    or truncated the line before it, would be a worse bug than the overlap it prevents, so each
    case here is about the sentence arriving exactly once, somewhere.
    """

    #: The two variables the surface reads, cleared so that a developer's own launch cannot leak
    #: a held screen or a runtime directory into the suite.
    _ENV = (launch_screen.HELD, "HARNESS_RUNTIME_DIR")

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.runtime = Path(holder.name)

    def say(self, *lines, held=False, runtime=None):
        """Say ``lines`` in a controlled environment and return what reached stdout."""
        saved = {name: os.environ.get(name) for name in self._ENV}
        self.addCleanup(lambda: self._restore(saved))
        for name in self._ENV:
            os.environ.pop(name, None)
        if held:
            os.environ[launch_screen.HELD] = launch_screen.HELD_VALUE
        if runtime is not None:
            os.environ["HARNESS_RUNTIME_DIR"] = str(runtime)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            launch_screen.note(*lines)
        return buffer.getvalue()

    @staticmethod
    def _restore(saved):
        for name, value in saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def spooled(self):
        path = self.runtime / launch_screen.NOTES
        return path.read_text(encoding="utf-8") if path.exists() else None

    def test_a_sentence_said_under_a_held_screen_waits_for_the_window(self):
        # The point of the spool: nothing reaches the terminal while the modal owns it.
        self.assertEqual(self.say("resolved", "plan", held=True, runtime=self.runtime), "")
        self.assertEqual(self.spooled(), "resolved\nplan\n")

    def test_a_sentence_said_with_an_ordinary_screen_prints_straight_out(self):
        # A launcher that could not borrow the screen leaves every step to print as it always
        # did, so the spool must not become a requirement for the sentence to be said at all.
        self.assertEqual(self.say("resolved", held=False, runtime=self.runtime), "resolved\n")
        self.assertIsNone(self.spooled())

    def test_a_sentence_said_with_nowhere_to_wait_prints(self):
        # Held with no runtime directory is a launcher that set one variable and not the other.
        # Falling through to stdout is the only answer that does not lose the line.
        self.assertEqual(self.say("resolved", held=True), "resolved\n")

    def test_a_spool_that_cannot_be_written_does_not_swallow_the_sentence(self):
        missing = self.runtime / "not-a-directory"
        missing.write_text("a file is not a directory", encoding="utf-8")
        self.assertEqual(
            self.say("resolved", held=True, runtime=missing / "deeper"),
            "resolved\n",
        )

    def test_each_sentence_appends_rather_than_replacing_the_last(self):
        # The pass says several things between screens, and a recap that kept only the last one
        # would be a subtler way of losing a warning than printing it over the questions.
        self.say("first", held=True, runtime=self.runtime)
        self.say("second", held=True, runtime=self.runtime)
        self.assertEqual(self.spooled(), "first\nsecond\n")

    def test_the_spool_is_the_file_the_launcher_promises_to_read(self):
        # Two languages name this path and neither can import the other, so the constant is the
        # single statement of it and the launcher's own declaration has to match, name and all --
        # including the copy in the list of session material deleted on exit.
        launcher = (ROOT / "start.sh").read_text()
        self.assertIn(
            f"flow_notes=${{HARNESS_RUNTIME_DIR}}/{launch_screen.NOTES}",
            launcher,
            "start.sh no longer spools where screen.NOTES points",
        )
        deleted = launcher[
            launcher.index("  for file in proxy-token") : launcher.index("harness_unlock")
        ]
        self.assertIn(launch_screen.NOTES, deleted, "the spool outlives the session")


class PtyHeldScreen(unittest.TestCase):
    """The modal stays put across the boundary between two steps.

    Each step is its own process, which is what lets Backspace walk backwards, and it is also what
    used to break the screen: a process that borrows the alternate screen necessarily returns it on
    the way out, so the terminal dropped to the launcher's printout between every pair of questions.
    The user read that as a crash. The fix moves the borrow out to the launcher and puts the steps
    in held mode, and the only honest proof is a byte count on a real terminal — the *number* of
    times the alternate screen is released across a run of several steps is the defect.
    """

    #: Two steps back to back in one interpreter, which is the byte-level contract the launcher's
    #: loop produces: each call to ``run()`` enters and leaves a Terminal, so this is exactly the
    #: sequence that used to drop to the printout between questions. Chaining two *processes*
    #: instead would race on when the second one receives the keypress, and a test that measures
    #: keystroke timing cannot assert on escape-sequence counts.
    SCRIPT = """
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui import screen
from tui.app import run as run_step

count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
for index in range(count):
    run_step(__import__("surface_two").Picker(("alpha", "beta", "gamma")))
    if len(sys.argv) > 2:
        # The launcher says one of these between every pair of screens.
        screen.note(f"{sys.argv[2]} {index}")
"""

    #: The step the driver above runs, in its own module so the driver stays short.
    PICKER = """
from tui.app import Result, Step
from tui.layout import Line, Row, Segment


class Picker(Step):
    title = "Pick"

    def __init__(self, items):
        self.items = items

    def initial(self):
        return (0, ())

    def rows(self, state):
        _, chosen = state
        out = []
        for name in self.items:
            mark = "x" if name in chosen else " "
            out.append(Row.item(Line(Segment(f"[{mark}] {name}")), name))
        return out

    def focus(self, state):
        return state[0]

    def with_focus(self, state, index):
        return (index, state[1])

    def toggled(self, state, target):
        chosen = set(state[1])
        chosen ^= {target}
        return (state[0], tuple(sorted(chosen)))

    def commit(self, state):
        return Result(value=list(state[1]))
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.runtime = Path(holder.name)
        (self.runtime / "surface_two.py").write_text(self.PICKER, encoding="utf-8")
        script = self.runtime / "driver.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.script = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM", "HARNESS_TUI_SCREEN")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)
        # The driver imports its step from alongside itself, as any launched surface would.
        self.env["PYTHONPATH"] = str(self.runtime)

    def session(self, steps: int, *, held: bool, note: str = "", read_back: bool = False):
        """Run ``steps`` surfaces inside one launcher-shaped terminal session.

        ``note`` has every surface say a sentence between its screens, which is what the launcher's
        own printout between two steps does, and ``read_back`` then replays the spool the way
        ``start.sh``'s ``harness_flow_unhold`` does. Both together reproduce the byte order of a
        real launch, and that order is the thing under test: a sentence painted into the modal is
        defect 2 with different words.
        """
        borrow = f"{sys.executable} {ROOT / 'tools/tui/screen.py'}"
        body = f"{sys.executable} {self.script} {steps}"
        if note:
            body += f" {note}"
        if held:
            program = (
                f"{borrow} --runtime-dir {self.runtime} enter"
                f" && export HARNESS_TUI_SCREEN=held HARNESS_RUNTIME_DIR={self.runtime}"
                f" && {body};"
                f" {borrow} --runtime-dir {self.runtime} leave"
            )
            if read_back:
                program += f"; cat {self.runtime / launch_screen.NOTES}"
        else:
            program = body
        return run_in_pty(
            ["bash", "-c", program],
            # One Enter per surface, chunked so every step gets its own frame rather than having
            # the whole answer consumed by the first one to poll.
            keys=[b"\r"] * steps,
            expect=b"alpha",
            env=self.env,
            cwd=ROOT,
        )

    def test_a_held_run_borrows_the_screen_once_for_the_whole_sequence(self):
        session = self.session(3, held=True)
        self.assertEqual(session.output.count(b"\x1b[?1049l"), 1, "the modal was left mid-run")
        self.assertEqual(session.output.count(b"\x1b[?1049h"), 1, "the modal was entered twice")
        self.assertTrue(session.restored)

    def test_an_unheld_run_still_borrows_and_returns_per_step(self):
        # Held mode must not become the only mode: a launcher that could not take the screen leaves
        # every step to manage its own, exactly as it did before, and this is what that looks like.
        session = self.session(2, held=False)
        self.assertEqual(session.output.count(b"\x1b[?1049h"), 2)
        self.assertEqual(session.output.count(b"\x1b[?1049l"), 2)
        self.assertTrue(session.restored)

    def test_a_held_step_clears_before_it_paints(self):
        # The previous process left its frame on the glass and the new one diffs against a buffer
        # it believes is empty, so without a clear the two steps' content shows through each other.
        session = self.session(2, held=True)
        # One clear per step, plus the launcher's own on entry.
        self.assertGreaterEqual(session.output.count(b"\x1b[2J"), 2)

    def test_a_sentence_said_under_the_modal_arrives_after_it(self):
        # The second half of the flicker defect. Holding the screen kept the questions up, which
        # left the launcher's own printout with nowhere to go but *through* them: the operator
        # answered a screen with the previous step's summary painted across it. So the words wait
        # in the spool and are read out once the window is ordinary scrollback again, and the byte
        # order here is that promise -- the sentence must land after ALT_OFF, and once only.
        session = self.session(2, held=True, note="policy", read_back=True)
        left = session.output.index(b"\x1b[?1049l")
        self.assertNotIn(b"policy", session.output[:left], "printed into the modal")
        self.assertEqual(
            session.output.count(b"policy"), 2, "one sentence per step, said once each"
        )
        self.assertIn(b"policy 0", session.output[left:])
        self.assertIn(b"policy 1", session.output[left:])
        self.assertTrue(session.restored)

    def test_a_spooled_sentence_still_arrives_when_nobody_reads_it_back(self):
        # The spool is not a promise that the launcher shows up: a step that dies before the leave
        # must not have taken its warning with it, which is why the trap reads the file too. Here
        # the driver leaves nothing behind on the terminal, so the file is the surviving evidence.
        session = self.session(1, held=True, note="policy")
        self.assertIn(b"policy 0", (self.runtime / launch_screen.NOTES).read_bytes())
        self.assertNotIn(b"policy 0", session.output)
        self.assertTrue(session.restored)

    def test_leaving_twice_hands_the_screen_back_once(self):
        # The launcher's own leave and its EXIT trap are the same call, and a trap that re-emitted
        # the release would scroll the user's real terminal a second time after the run.
        screen = f"{sys.executable} {ROOT / 'tools/tui/screen.py'}"
        session = run_in_pty(
            [
                "bash",
                "-c",
                f"{screen} --runtime-dir {self.runtime} enter;"
                f" {screen} --runtime-dir {self.runtime} leave;"
                f" {screen} --runtime-dir {self.runtime} leave",
            ],
            env=self.env,
            cwd=ROOT,
        )
        self.assertEqual(session.output.count(b"\x1b[?1049l"), 1)

    def test_a_piped_launcher_borrows_nothing(self):
        # Redirected output is the launcher's log, and control codes written into it are damage
        # that outlives the run. This is the check behind start.sh's `|| true` on enter.
        result = subprocess.run(
            [
                sys.executable,
                f"{ROOT}/tools/tui/screen.py",
                "--runtime-dir",
                str(self.runtime),
                "enter",
            ],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=ROOT,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("\x1b", result.stdout)


#: The diagram's own column arithmetic, spelled out as numbers rather than as the surface's
#: constants: a test that imports MARK_WIDTH moves its expectation whenever the surface moves, and
#: the whole point of the label edge is that it does not move.
_MARK = 4
_RAIL = 3
_GUIDE = 2


def _tree() -> tuple:
    """A diagram that exercises every kind of row, independent of what the launcher shows today.

    Deliberately not the real context tree: a test that depends on which add-ons happen to exist
    fails for reasons that have nothing to do with the interface.
    """
    return (
        Node("head", "STATIC CONTEXT"),
        Node(
            "sys",
            "SYSTEM.md",
            states=("auto", "off"),
            kind=CHECK,
            default="auto",
            value="4,000",
            pct="2%",
            detail="what Kimi is told before the first message",
            children=(
                Node(
                    "sys.op",
                    "operator file",
                    kind=FIXED,
                    detail="staged from the repository, so the built-in prompt is not used",
                    children=(Node("sys.line", "included text", kind=FIXED),),
                ),
                Node("sys.old", "staged tier", kind=FIXED, enabled=False),
                Node(
                    "sys.base",
                    "built-in prompt",
                    kind=PLAIN,
                    branch=True,
                    link=True,
                    detail="reaches the main agent and every subagent",
                ),
            ),
        ),
        Node(
            "lim",
            "usage limits",
            states=("on", "off"),
            kind=CHECK,
            default="on",
            value="1,024",
            pct="1%",
        ),
    )


class ForestTests(unittest.TestCase):
    """The tree surface: geometry, the two kinds of switch, and what the cursor may reach.

    A diagram is the one surface here where a wrong measurement is a wrong *meaning* — a figure that
    drifts two columns is a figure attached to the wrong row — so most of this group is about
    columns rather than about colour.
    """

    def step(self, nodes=None, *, caps=QUIET, **kwargs):
        step = ForestStep(title="Context", nodes=self.nodes() if nodes is None else nodes, **kwargs)
        step.caps = caps
        return step

    def nodes(self):
        return _tree()

    def text(self, step, state=None):
        return [row.line.text() for row in step.rows(step.initial() if state is None else state)]

    def shown(self, modal):
        modal.paint()
        return "\n".join(modal.screen.snapshot())

    def modal(self, step, keys=(), caps=QUIET):
        self.script = _Script(list(keys), caps)
        return Modal(step, View(can_go_back=True), terminal=self.script, caps=caps)

    def sized(self, columns):
        return Caps(color=NONE, columns=columns, rows=24, probe=False)

    # -- what the cursor may reach ----------------------------------------------------------

    def test_only_a_row_with_a_switch_can_hold_the_cursor(self):
        step = self.step()
        self.assertEqual([node.id for node in step.options()], ["sys", "lim"])
        # The loop counts focusable *rows*; if the two disagreed, Home and End would land somewhere
        # the step's own idea of the cursor cannot describe.
        self.assertEqual(len(focusable_rows(step.rows(step.initial()))), len(step.options()))

    def test_a_row_that_cannot_answer_cannot_be_toggled_either(self):
        step = self.step()
        state = step.initial()
        self.assertIs(step.toggled(state, "sys.op"), state)
        self.assertIs(step.toggled(state, "no-such-row"), state)

    def test_a_switch_with_no_honest_mark_is_refused_while_the_tree_is_built(self):
        # A checkbox means exactly two states and a hollow delimiter means nobody's to move.
        # Inventing a third mark, or leaving a switch unmarked, is the undocumented signal this
        # redesign exists to remove — so it is a construction error rather than a screen nobody
        # can read.
        with self.assertRaises(ValueError):
            Node("bad", "a switch with no mark", states=("on", "off"), kind=PLAIN)
        with self.assertRaises(ValueError):
            Node("bad", "a box with three states", states=("a", "b", "c"), kind=CHECK)
        with self.assertRaises(ValueError):
            Node("bad", "a fact with a switch on it", states=("a", "b"), kind=FIXED)

    def test_the_mark_vocabulary_says_the_value_and_the_changeability_at_once(self):
        # One column, two axes: the character inside says the value and the characters around it
        # say whether a key can move it. Nothing else may appear here, and no row may be the only
        # place a mark is spelled — a reader learns the grammar once or not at all.
        step = self.step(
            nodes=(
                Node("a", "a switch that is on", states=("on", "off"), kind=CHECK, default="on"),
                Node("b", "a switch that is off", states=("on", "off"), kind=CHECK, default="off"),
                Node(
                    "c",
                    "a switch nothing can switch",
                    states=("on", "off"),
                    kind=CHECK,
                    enabled=False,
                ),
                Node("d", "a file that holds", kind=FIXED),
                Node("e", "a file that is superseded", kind=FIXED, enabled=False),
                Node("f", "A HEADING"),
            )
        )
        self.assertEqual(
            [row.line.text()[:3] for row in step.rows(step.initial()) if row.line.text().strip()],
            ["[x]", "[ ]", "[-]", "-x-", "- -", "   "],
        )

    # -- geometry ---------------------------------------------------------------------------

    def test_every_section_shares_one_label_edge_and_each_level_steps_right(self):
        lines = self.text(self.step())
        heading = next(line for line in lines if "STATIC CONTEXT" in line)
        box = next(line for line in lines if "SYSTEM.md" in line)
        tail = next(line for line in lines if "usage limits" in line)
        child = next(line for line in lines if "operator file" in line)
        joint = next(line for line in lines if "built-in prompt" in line)
        grandchild = next(line for line in lines if "included text" in line)
        # A heading, a switch and a fixed source in one tree all start their label here: the mark
        # column is bought by the tree, not row by row.
        self.assertEqual(heading.index("STATIC"), _MARK)
        self.assertEqual(box.index("SYSTEM.md"), _MARK)
        self.assertEqual(tail.index("usage limits"), _MARK)
        self.assertEqual(child.index("operator file"), _RAIL + _MARK)
        self.assertEqual(joint.index("built-in prompt"), _RAIL + _MARK)
        # The third level steps right again, and the rail of the level above it keeps running
        # through the blank column a branch leaves.
        self.assertEqual(grandchild.index("included text"), 2 * _RAIL + _MARK)

    def test_a_section_is_separated_from_the_one_above_it_by_a_blank_row(self):
        # Sections drawn in one grammar read as one long list, and a glyph in the mark column would
        # be a fifth spelling in a vocabulary whose whole job is saying "this is a switch". The
        # separator is therefore the row itself: nothing above the first root, and a blank between
        # every pair that follows.
        rows = self.step().rows(self.step().initial())
        edges = [
            index
            for index, row in enumerate(rows)
            if any(word in row.line.text() for word in ("STATIC CONTEXT", "SYSTEM.md", "usage"))
        ]
        self.assertEqual(len(edges), 3)
        self.assertEqual(edges[0], 0)
        for index in edges[1:]:
            self.assertEqual(rows[index - 1].line.text(), "")

    def test_a_root_is_weighted_and_a_row_inside_it_is_not(self):
        # The blank row says where a section begins; the weight says which row owns it. Both are
        # readable without colour, which is why the root also keeps its own column rather than a
        # marker that would have to compete with the four marks the switch vocabulary already has.
        colour = Caps(color=ANSI256, columns=80, rows=24, probe=False)
        rows = self.step(caps=colour).rows(self.step(caps=colour).initial())

        def tone(needle: str) -> str:
            line = next(row.line for row in rows if needle in row.line.text())
            return next(part.attr for part in line.segments if needle in part.text)

        title = colour.color_pair("title")
        self.assertEqual(tone("usage limits"), title)
        self.assertEqual(tone("STATIC CONTEXT"), title)
        self.assertEqual(tone("operator file"), "")

    def test_a_root_that_fans_out_buys_its_marker_column_for_the_whole_tree(self):
        # Two roots starting at different columns read as two unrelated lists rather than as one
        # map with a branch in it, so the marker is reserved on every row whether a row uses it or
        # not. This is the only arrangement that leaves the label edge still standing.
        step = self.step(
            nodes=(
                Node("base", "shared source", kind=FIXED, branch=True),
                Node("one", "first block", states=("on", "off"), kind=CHECK),
            )
        )
        lines = self.text(step)
        branching = next(line for line in lines if "shared source" in line)
        checkable = next(line for line in lines if "first block" in line)
        self.assertIn("▶", branching)
        self.assertEqual(branching.index("shared source"), _GUIDE + _MARK)
        self.assertEqual(checkable.index("first block"), _GUIDE + _MARK)

    def test_a_source_feeding_two_parents_opens_its_joint_in_either_glyph_register(self):
        unicode = self.step()
        ascii = self.step(caps=Caps(color=NONE, unicode=False, columns=80, rows=24, probe=False))
        heavy = next(line for line in self.text(unicode) if "built-in prompt" in line)
        plain = next(line for line in self.text(ascii) if "built-in prompt" in line)
        self.assertIn("▶", heavy)
        self.assertNotIn("▶", plain)
        self.assertIn(">", plain)
        # The fan-out lives *in* the joint, so the ASCII tier neither loses the signal nor pays a
        # column for it.
        self.assertEqual(unicode.cells(heavy), ascii.cells(plain))

    def test_no_row_costs_more_columns_than_the_window_has(self):
        # The regression this pins: a figure, a percentage and a wrapped heading are all content,
        # and each one, measured wrongly, pushed a row past the frame edge at some width.
        for columns in range(8, 200):
            step = self.step(
                caps=self.sized(columns),
                head=("Space changes a switch and Enter continues to the next step.",),
            )
            for line in self.text(step):
                self.assertLessEqual(step.cells(line), step.room, f"{columns}: {line!r}")

    def test_a_prose_row_that_overflows_wraps_rather_than_running_past_the_edge(self):
        step = self.step(
            nodes=(
                Node(
                    "a",
                    "LABEL",
                    states=("on", "off"),
                    kind=CHECK,
                    detail="supercalifragilisticexpialidocious-then-some trailing words",
                ),
            ),
            caps=self.sized(20),
        )
        lines = self.text(step)
        self.assertTrue(all(step.cells(line) <= step.room for line in lines), lines)
        # The sentence is what the pane is for; the tree's job is only never to overrun.
        self.assertNotIn("supercalifragilistic", "\n".join(lines))
        state = step.with_focus(step.initial(), 0)
        pane = [line.text() for line in step.detail(state)]
        self.assertTrue(pane, "the row's own prose went nowhere at all")
        self.assertTrue(all(step.cells(line) <= step.detail_room for line in pane), pane)
        # A word longer than the column is cut rather than given a column of its own, so the whole
        # of a sentence is what the overlay prints from ``detail_source``, not the pane.
        self.assertEqual(
            step.detail_source(state),
            ("supercalifragilisticexpialidocious-then-some trailing words",),
        )

    def test_the_pane_holds_the_sentence_the_tree_refused_to_stack(self):
        # Every row's prose is reachable from the row and nowhere else, which is what lets the map
        # stay a map. A row with nothing to add says nothing, rather than repeating its own name.
        step = self.step()
        self.assertNotIn("what Kimi is told", "\n".join(self.text(step)))
        on_block = step.with_focus(step.initial(), 0)
        self.assertEqual(
            [line.text() for line in step.detail(on_block)],
            ["what Kimi is told before the first message"],
        )
        # The unwrapped sentence travels with it, because the overlay re-breaks it across its own
        # width — a reader who went looking for the whole of a description should not get the
        # pane's line endings with it.
        self.assertEqual(
            step.detail_source(on_block),
            ("what Kimi is told before the first message",),
        )
        on_limits = step.with_focus(step.initial(), 1)
        self.assertEqual(step.detail(on_limits), [])
        self.assertEqual(step.detail_source(on_limits), ())

    def test_the_tail_gives_up_the_percentage_then_the_figure(self):
        # Two columns of figures, and the rightmost is the one that says the least per cell. The
        # breakpoints are the surface's own arithmetic: MIN_LABEL is what the label gets to keep.
        def tail(columns):
            step = self.step(caps=self.sized(columns))
            return step.fit_tail(step.tail_widths())

        self.assertEqual(tail(37), (5, 2))
        self.assertEqual(tail(36), (5, 0))
        self.assertEqual(tail(33), (5, 0))
        self.assertEqual(tail(32), (0, 0))

    def test_a_label_survives_every_tail_the_window_cannot_hold(self):
        narrow = self.step(caps=self.sized(30))
        lines = "\n".join(self.text(narrow))
        self.assertNotIn("4,000", lines)
        self.assertNotIn("2%", lines)
        # The name is the row's identity, so it is the last thing to go.
        self.assertIn("staged tier", lines)
        self.assertIn("usage limits", lines)

    def test_the_columns_hold_still_while_a_state_cycles(self):
        step = self.step()
        state = step.initial()
        places = set()
        for _ in range(3):
            state = step.toggled(state, "sys")
            line = next(text for text in self.text(step, state) if "SYSTEM.md" in text)
            places.add(line.index("4,000"))
            self.assertLessEqual(step.cells(line), step.room)
        self.assertEqual(len(places), 1)

    def test_scrolling_moves_rows_and_never_columns(self):
        # The tail is sized from every row in the tree rather than from the visible slice, and the
        # viewport belongs to the loop and not to the step, so a row painted at the top of a window
        # and the same row painted at the bottom put their figure in the same column.
        nodes = (Node("head", "SECTION"),) + tuple(
            Node(
                f"n{i}",
                f"add-on {i}",
                states=("on", "off"),
                kind=CHECK,
                default=(i % 2 == 0),
                value=f"{1000 + i:,}",
                pct=f"{i}%",
            )
            for i in range(12)
        )
        step = self.step(nodes)
        modal = self.modal(step, caps=self.sized(60))
        places: dict[str, set[int]] = {}
        for index in range(8):
            if index:
                modal.handle(Key("Down"))
            for line in self.shown(modal).splitlines():
                for node in nodes[1:]:
                    if node.label in line and node.value in line:
                        places.setdefault(node.id, set()).add(line.index(node.value))
        self.assertGreater(len(places), 4, "the window never scrolled")
        for node_id, seen in places.items():
            self.assertEqual(len(seen), 1, f"{node_id} moved: {sorted(seen)}")

    def test_a_page_past_the_last_switch_moves_the_window_and_keeps_an_answer(self):
        # The invariant behind "12 more below" being reachable. A diagram's tail is often rows that
        # describe an outcome rather than offer a switch -- the price block on the real context step
        # is all of them -- so a window that could only ever follow the cursor would strand those
        # rows out of view for good. Paging moves the window, and the cursor lands on the nearest
        # row that can still answer, so Space keeps meaning what the footer says it means.
        nodes = (
            (Node("head", "SECTION"),)
            + tuple(
                Node(f"n{i}", f"switch {i}", states=("on", "off"), kind=CHECK, default="on")
                for i in range(3)
            )
            + tuple(Node("", f"unreachable fact {i}", kind=PLAIN) for i in range(12))
        )
        step = self.step(nodes)
        modal = self.modal(step, caps=self.sized(60))
        self.assertNotIn("unreachable fact 11", self.shown(modal))
        for _ in range(8):
            modal.handle(Key("PgDn"))
        self.assertIn("unreachable fact 11", self.shown(modal), "the tail stayed out of view")
        # And the cursor is still somewhere a switch lives.
        held = step.options()[step.focus(modal.session.state)]
        self.assertTrue(held.states, "the cursor ended on a row that cannot answer")
        modal.handle(Key("Space"))
        self.assertEqual(modal.session.state.values[held.id], "off")

    # -- answering --------------------------------------------------------------------------

    def test_a_switch_marks_its_first_state_whatever_that_state_is_called(self):
        # The mark column says the value, not a vocabulary: ``auto`` is the box's checked position
        # here exactly as ``on`` is on the add-on rows, and no row spends a column spelling either
        # word. What the cursor is on and what it says are the status line's job, and the sentence
        # explaining why belongs to the pane.
        step = self.step()
        line = next(text for text in self.text(step) if "SYSTEM.md" in text)
        self.assertTrue(line.startswith("[x] SYSTEM.md"), line)
        self.assertNotIn("auto", line)
        self.assertNotIn("off", line)
        self.assertIn("auto", step.status(step.initial()).text())
        off = step.toggled(step.initial(), "sys")
        held = next(text for text in self.text(step, off) if "SYSTEM.md" in text)
        self.assertTrue(held.startswith("[ ] SYSTEM.md"), held)
        self.assertIn("off", step.status(off).text())

    def test_space_cycles_a_state_and_wraps_at_the_end(self):
        # Wrapping is the whole of a two-state row's usability: a switch that clamped at its last
        # state could be moved once and never moved back. The names here are deliberately not
        # ``on``/``off``, which is the other half of what the row has to get right.
        step = self.step()
        state = step.initial()
        seen = []
        for _ in range(4):
            state = step.toggled(state, "sys")
            seen.append(state.values["sys"])
        self.assertEqual(seen, ["off", "auto", "off", "auto"])

    def test_space_toggles_a_checkbox(self):
        step = self.step()
        state = step.initial()
        seen = []
        for _ in range(3):
            state = step.toggled(state, "lim")
            seen.append((state.values["lim"], step.rows(state)[-1]))
        self.assertEqual([value for value, _ in seen], ["off", "on", "off"])
        self.assertEqual([row.line.text()[:3] for _, row in seen], ["[ ]", "[x]", "[ ]"])

    def test_a_superseded_source_is_hollow_and_dim_rather_than_explained_in_place(self):
        # Only a row that could have carried a switch carries a hollow one. A source has no switch
        # of its own — its answer belongs to the block above it — so drawing a box there would claim
        # a control that no key can reach. ``- -`` is that statement, and unlike the grey it is
        # still there under ``NO_COLOR`` and on a monochrome terminal, so the fact a row contributes
        # nothing survives the colour going away. *Why* it contributes nothing is a sentence, and
        # sentences live in the pane.
        colour = Caps(color=ANSI256, columns=80, rows=24, probe=False)
        step = self.step(caps=colour)
        row = next(r for r in step.rows(step.initial()) if "staged tier" in r.line.text())
        self.assertIn("- - staged tier", row.line.text())
        self.assertNotIn("]", row.line.text())
        self.assertEqual(row.attr, colour.color_pair("dim"))
        self.assertFalse(row.focusable)
        mono = self.step()
        plain = next(r for r in mono.rows(mono.initial()) if "staged tier" in r.line.text())
        self.assertIn("- - staged tier", plain.line.text())
        self.assertNotIn("\x1b", plain.line.text())

    def test_a_switch_no_key_can_reach_is_hollow(self):
        step = self.step(
            nodes=(
                Node("on", "reachable", states=("on", "off"), kind=CHECK, default="on"),
                Node("off", "unreachable", states=("on", "off"), kind=CHECK, enabled=False),
            )
        )
        lines = self.text(step)
        reachable = next(line for line in lines if " reachable" in line)
        unreachable = next(line for line in lines if "unreachable" in line)
        self.assertIn("[x] reachable", reachable)
        self.assertIn("[-] unreachable", unreachable)
        self.assertNotIn("off", [node.id for node in step.options()])

    def test_reset_restores_the_opening_answer_and_the_opening_cursor(self):
        step = self.step()
        state = step.toggled(step.toggled(step.initial(), "lim"), "sys")
        state = step.with_focus(state, 1)
        self.assertNotEqual(state.values, step.initial().values)
        self.assertEqual(step.reset(state), step.initial())
        self.assertEqual(step.focus(step.reset(state)), 0)

    def test_an_opening_answer_beats_a_rows_own_default(self):
        step = self.step(opening={"lim": "off", "sys": "on"})
        state = step.initial()
        self.assertEqual(state.values["lim"], "off")
        self.assertEqual(state.values["sys"], "on")
        self.assertEqual(state.values["head"], "")

    def test_commit_answers_a_copy_of_the_map_rather_than_the_loops_own(self):
        step = self.step()
        state = step.initial()
        answer = step.commit(state)
        self.assertEqual(answer.status, flow.CONTINUE)
        self.assertEqual(answer.summary, "")
        answer.value["sys"] = "nonsense"
        self.assertEqual(state.values["sys"], "auto")

    def test_the_status_row_names_the_focused_row_and_its_state(self):
        step = self.step()
        state = step.with_focus(step.initial(), 1)
        self.assertEqual(step.status(state).text(), "usage limits  on")
        self.assertEqual(step.status(step.toggled(state, "lim")).text(), "usage limits  off")

    def test_head_prose_sits_above_the_tree_and_never_holds_the_cursor(self):
        step = self.step(head=("Space changes a switch.", "Enter continues."))
        rows = step.rows(step.initial())
        self.assertEqual(
            [rows[0].line.text(), rows[1].line.text()],
            [
                "Space changes a switch.",
                "Enter continues.",
            ],
        )
        self.assertEqual(rows[2].line.text(), "")
        self.assertFalse(any(row.focusable for row in rows[:3]))
        self.assertEqual(len(focusable_rows(rows)), len(step.options()))

    def test_an_unbound_letter_reports_itself_instead_of_acting(self):
        step = self.step()
        modal = self.modal(step)
        modal.handle(Key("a"))
        self.assertIn("does nothing here", modal.session.note)
        self.assertEqual(modal.session.state.values["lim"], "on")

    def test_the_generated_legend_carries_no_unlabelled_single_letter(self):
        step = self.step()
        modal = self.modal(step)
        modal.paint()
        spelled = " ".join(
            " ".join(line.split())
            for line in "\n".join(modal.screen.snapshot()).splitlines()
            if line.strip()
        )
        self.assertIn("Space toggle", spelled)
        self.assertIn("<Enter> continue", spelled)
        self.assertIn("<Backspace> previous step", spelled)
        self.assertIn("<Ctrl+R> reset choices", spelled)
        self.assertIn("? all keys", spelled)
        # Nothing the operator must know is hidden behind a bare letter.
        for letter in ("a", "n", "r", "t", "s", "d"):
            self.assertNotIn(f" {letter} ", spelled)


class PtyForest(unittest.TestCase):
    """The tree surface on a real terminal driver, where the byte sequences are the question.

    :class:`ForestTests` measures rows; this proves the keys reach them, that the marks and the
    words come out of a colour terminal as painted glyphs, and that a tree taller than the window
    scrolls inside the frame instead of pushing the frame open.
    """

    SCRIPT = """
import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "tools"))

from tui.app import View, run
from tui.forest import CHECK, FIXED, PLAIN, Node, ForestStep

deep = int(sys.argv[1]) if len(sys.argv) > 1 else 0
static = Node(
    "system",
    "SYSTEM.md",
    states=("auto", "off"),
    kind=CHECK,
    default="auto",
    value="4,000",
    pct="2%",
    detail="what the main agent is told before the first message",
    children=(
        Node("system.staged", "staged tier", kind=FIXED, enabled=False),
        Node("system.base", "built-in prompt", kind=PLAIN, branch=True, link=True),
    ),
)
rows = [Node("head", "STATIC CONTEXT"), static]
for index in range(deep):
    rows.append(
        Node(
            "add-on-%d" % index,
            "generated add-on %d" % index,
            states=("on", "off"),
            kind=CHECK,
            default="on" if index % 2 == 0 else "off",
            value="%d" % (100 * index),
        )
    )
# The real context step ends with a price section whose rows describe an outcome rather than
# offer a switch, so the last thing on the screen is not the last thing the cursor can hold.
for index in range(int(sys.argv[2]) if len(sys.argv) > 2 else 0):
    rows.append(
        Node(
            "",
            "unreachable fact %d" % index,
            kind=PLAIN,
            value="%d" % (7 * index),
        )
    )
result = run(
    ForestStep(title="Context", nodes=rows),
    View(position=7, total=8, label="Context", can_go_back=True),
)
print("RESULT=" + json.dumps(result.value))
sys.exit(result.status)
"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        script = Path(holder.name) / "forest.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        self.path = script
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }
        self.env["HARNESS_ROOT"] = str(ROOT)

    def run_forest(self, keys, *, deep=3, tail=0, **kwargs):
        return run_in_pty(
            [sys.executable, str(self.path), str(deep), str(tail)],
            keys=keys,
            env=self.env,
            cwd=ROOT,
            **kwargs,
        )

    def answer(self, session):
        return json.loads(session.screen.split("RESULT=")[1].splitlines()[0])

    def test_the_tree_paints_and_a_key_sequence_answers_it(self):
        session = self.run_forest(b"\x1b[B \r", expect=b"generated add-on 0", rows=24, columns=80)
        self.assertEqual(session.status, flow.CONTINUE)
        answer = self.answer(session)
        self.assertEqual(answer["system"], "auto")
        self.assertEqual(answer["add-on-0"], "off")
        self.assertEqual(answer["add-on-1"], "off")
        self.assertIn("▌", session.screen)
        # A superseded source reads as a hollow mark, and the fan-out joint renders.
        self.assertIn("- -", session.screen)
        self.assertIn("▶", session.screen)
        self.assertIn("auto", session.screen)
        self.assertIn("<Enter> continue", session.screen)
        self.assertIn("Space toggle", session.screen)
        self.assertTrue(session.restored)

    def test_the_cursor_opens_on_the_first_switch_and_up_cannot_leave_it(self):
        session = self.run_forest(b"\x1b[A \r", expect=b"STATIC CONTEXT", rows=24, columns=80)
        answer = self.answer(session)
        # Up at the top of the tree goes nowhere, and the row above the switch is a heading while
        # the ones below it are sources no key can reach, so the only thing Space could move here
        # is the switch the cursor started on, and the only state it can move to is the other one.
        self.assertEqual(answer["system"], "off")
        self.assertTrue(session.restored)

    def test_backspace_asks_for_the_previous_step_from_inside_the_tree(self):
        session = self.run_forest(b"\x7f", expect=b"STATIC CONTEXT", rows=24, columns=80)
        self.assertEqual(session.status, flow.GO_BACK)
        # Going back answers nothing, and the launcher has to tell that apart from an answer of
        # "everything left as it was".
        self.assertIn("RESULT=null", session.screen)
        self.assertTrue(session.restored)

    def test_a_tree_taller_than_the_window_scrolls_inside_the_frame(self):
        session = self.run_forest(b"\x1b[F \r", expect=b"more below", deep=14, rows=14, columns=60)
        self.assertEqual(session.status, flow.CONTINUE)
        # The overflow count says how much is out of view rather than merely that something is, and
        # jumping to the end brings the last row on screen without the frame growing a column.
        self.assertIn("more below", session.screen)
        self.assertIn("add-on-13", session.screen)
        self.assertEqual(self.answer(session)["add-on-13"], "on")
        self.assertTrue(session.restored)

    def test_page_down_reaches_rows_the_cursor_cannot_hold(self):
        # The second half of the flicker defect's sibling complaint: on the real context step the
        # price rows at the bottom of the diagram describe an outcome and take no input, so a
        # viewport fitted around the *cursor* could never move past the last switch and the screen
        # advertised a tail nobody could bring on view. Here the bytes are the question -- PgDn is
        # CSI 6~, and only a real terminal driver says whether that reaches the window at all.
        session = self.run_forest(
            [b"\x1b[6~"] * 8 + [b"\r"],
            expect=b"more below",
            deep=2,
            tail=12,
            rows=20,
            columns=76,
        )
        self.assertIn("unreachable fact 11", session.screen, "the tail stayed out of view")
        # Paging is not answering, so the commit that follows it still reports the tree's own
        # opening state and every switch the operator never touched.
        self.assertEqual(self.answer(session)["system"], "auto")
        self.assertTrue(session.restored)

    def test_page_down_past_the_end_leaves_the_answer_untouched(self):
        # Scrolling is not selecting: paging to the bottom and back has to answer what the operator
        # actually toggled, not whatever row the window happened to stop on. Here nothing was
        # toggled at all, and the whole opening map comes back.
        session = self.run_forest(
            [b"\x1b[6~"] * 4 + [b"\x1b[5~"] * 4 + [b"\r"],
            expect=b"more below",
            deep=2,
            tail=12,
            rows=20,
            columns=76,
        )
        self.assertEqual(session.status, flow.CONTINUE)
        answer = self.answer(session)
        self.assertEqual(answer["system"], "auto")
        self.assertEqual(answer["add-on-0"], "on")
        self.assertEqual(answer["add-on-1"], "off")
        self.assertTrue(session.restored)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
