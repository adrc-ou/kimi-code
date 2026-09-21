"""The text-shaped surface: one value typed at the screen, hidden or shown as typed.

Two steps in the launch sequence ask for a value rather than a choice — a provider's API key, and a
module's declared environment variable — and neither is a list, so neither is a
:class:`~.menu.ListStep`. They subclass :class:`~.app.Step` here instead and inherit the same frame,
the same generated footer and the same movement keys, which is what makes them read as one program
with the launcher's menus rather than as two extra questions bolted onto the screen.

A field does give one thing up, and it is the thing the panel being superseded got backwards. A
single printable character cannot be both a command and a letter of the answer: ``k`` and ``j`` are
aliases for the arrow keys on every menu, and ``?`` opens the key reference, so a user typing a
secret that contains them would lose those characters silently. :meth:`FieldStep.keys` therefore
keeps each binding that can be struck without typing a letter and drops the letter spellings,
removing a binding altogether when a letter was all it had. The footer and the key reference are
generated from that same table, so the step stops advertising what it has given up at the same
moment it gives it up — an advertised key that does nothing is the same defect as a working key
nobody is told about.

What a masked field is *for* decides what it may show, and the two hard requirements both come from
the interface it replaces. Nothing about the value is ever echoed when ``masked`` is set: the glyph
run is a texture, and the character count in the status row is the fact, which is how a user finds
out a paste arrived without the secret arriving on screen. And an answer that is not yet an answer
keeps the screen open with the reason in the note row, because the prompt loop this supersedes
simply printed the question again rather than accepting a blank.

``Backspace`` is the one key that means two things here, and it never means both at once. While
there is something to delete it deletes; on an empty field it asks for the previous step, and only
when the flow has one. The footer is regenerated per keypress from the live table, so whichever
label it is showing is what the key will actually do — the alternative, a key whose meaning depends
on a state the screen does not name, is the magic keystroke this redesign exists to remove.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, replace

from . import layout as L
from .app import ACCEPT, BACK, BACK_BINDING, Result, Step, View
from .keys import Binding, bind
from .layout import Line, Row, Segment

#: The target on the field row, so a step with three rows in it still knows which one the cursor is
#: on without counting.
FIELD = "field"

#: Deletes the character before the caret. A step action rather than a loop action: scrolling,
#: toggling and accepting are the same on every surface, but only a field has characters.
DELETE = "delete"

#: Takes ``Backspace`` off the loop's :data:`~.app.BACK_BINDING` for as long as the field has a
#: character in it.
DELETE_BINDING = bind(DELETE, "delete", "Backspace")

#: Where the next character will go. Drawn rather than delegated to the terminal's own cursor
#: because the diff painter owns cursor movement, and because a marker the front buffer contains is
#: a marker a test can assert on. ``▏`` is a one-eighth block, so it occupies one column in every
#: width table this engine trusts, and it reads as an edge rather than as a letter.
CARET = "▏"
#: :data:`CARET` for a terminal that may not have the block-drawing glyph, as ``layout`` pairs its
#: own box glyphs.
CARET_ASCII = "|"

#: :attr:`FieldState.at` for the field row itself, which is where a field opens and where a reset
#: puts the cursor back. It is a direction rather than an index because the rows above it are
#: re-wrapped whenever the window changes width, and the field is the last one either way.
AT_FIELD = -1


def _strike(item: Binding) -> tuple[str, ...]:
    """The spellings of ``item`` that a field could not also be typing.

    One printable character is a letter, a digit or a punctuation mark — which is exactly what a
    value is made of. Named keys arrive as ``Up``, modifiers as ``Ctrl-R``, and the space bar as
    ``Space``, so nothing a field needs to keep is lost, and neither is ``Backspace``, which is the
    whole reason this is not simply a ban on short strings.
    """
    return tuple(name for name in item.keys if not (len(name) == 1 and name.isprintable()))


@dataclass(frozen=True)
class FieldState:
    """What has been typed so far, and which row the cursor is resting on.

    ``text`` is deliberately one string: the caret stays at the end, because a secret is typed once
    or pasted in one go and then corrected backwards, which is what ``Backspace`` is for. Left and
    right movement inside the value would cost two rows of legend on every step for an edit almost
    nobody makes, and a mistake that far into a key means retyping it.

    ``at`` is an index into the focusable rows, :data:`AT_FIELD` unless the user has moved the
    cursor up into the explanation. Reading the sentence about where a key is issued is what that
    row is there for, so the cursor goes there like anywhere else.
    """

    text: str = ""
    at: int = AT_FIELD


class FieldStep(Step):
    """One line of text the user types, and ``Enter`` accepts."""

    #: ``Space`` is a character here, not a command. The loop drops its toggle binding on this flag,
    #: which is also what lets an unhandled space fall through to :meth:`typed`.
    toggleable = False
    accepts_text = True
    reset_note = "value cleared"

    def __init__(
        self,
        *,
        title: str,
        prompt: str,
        head: Sequence[str] = (),
        rail: str = "",
        masked: bool = True,
        whitespace_is_value: bool = True,
    ) -> None:
        """
        ``prompt`` is the question above the field and ``title`` the frame's top-left label, as in
        :class:`~.menu.ListStep`: they are separate because "Provider key" and "NRP API key
        (NRP_API_KEY)" are both useful and neither repeats the other.

        ``masked`` is the difference between a token and a path. It changes only what is drawn — the
        value the caller receives is what was typed either way, which is the property that makes it
        safe to hide.

        ``whitespace_is_value`` is left to the caller because the two callers disagree on purpose: a
        module stores what was typed verbatim, so a lone space *is* its value, while a credential is
        stripped before it is stored, so spaces would arrive as nothing at all.
        """
        self.title = title
        self.rail = rail or title
        self.prompt = prompt
        self.head = tuple(head)
        self.masked = masked
        self.whitespace_is_value = whitespace_is_value

    # -- answering ---------------------------------------------------------------------------

    def initial(self) -> FieldState:
        """Every field opens empty, with the cursor on the field.

        There is no remembered value to restore, and that is the point: a previous answer in a
        constructor would be a secret held in memory by a process that has no need of it.
        """
        return FieldState()

    def typed(self, state: object, char: str) -> tuple[FieldState, Result | None]:
        assert isinstance(state, FieldState)
        if not char.isprintable():
            return state, None
        return replace(state, text=state.text + char), None

    def answer(self, state: object, action: str, target: object) -> tuple[object, Result | None]:
        assert isinstance(state, FieldState)
        if action == DELETE and state.text:
            return replace(state, text=state.text[:-1]), None
        return state, None

    def complete(self, text: str) -> bool:
        """Whether ``text`` is an answer this step will accept."""
        if self.whitespace_is_value:
            return bool(text)
        return bool(text.strip())

    def incomplete(self, state: object) -> str:
        assert isinstance(state, FieldState)
        if not state.text:
            return "nothing entered — type or paste the value"
        if not self.complete(state.text):
            return "a value of only spaces is not an answer"
        return ""

    def commit(self, state: object) -> Result:
        assert isinstance(state, FieldState)
        # The summary is empty on purpose. This is the one step whose answer must never reach the
        # scrollback that the launcher's non-interactive printout resumes into, and a step that had
        # something safe to say about its value would fill it in.
        return Result(value=state.text)

    # -- keys --------------------------------------------------------------------------------

    def keys(self, state: object, view: View) -> tuple[Binding, ...]:
        assert isinstance(state, FieldState)
        table = [item for item in super().keys(state, view) if item.action != BACK]
        # Spliced into the slot ``Backspace`` would have had, right after ``ACCEPT``, so the three
        # keys that end a step stay together and neither spelling can be the first thing a
        # truncating footer drops.
        at = next((index for index, item in enumerate(table) if item.action == ACCEPT), 0)
        table.insert(at + 1, DELETE_BINDING if state.text else BACK_BINDING)
        out: list[Binding] = []
        for item in table:
            struck = _strike(item)
            if struck:
                out.append(item if struck == item.keys else replace(item, keys=struck))
        return tuple(out)

    # -- the cursor ----------------------------------------------------------------------------

    def focus(self, state: object) -> int:
        assert isinstance(state, FieldState)
        last = self._last_focus()
        return last if state.at < 0 else min(state.at, last)

    def with_focus(self, state: object, index: int) -> object:
        assert isinstance(state, FieldState)
        return replace(state, at=max(0, min(index, self._last_focus())))

    def _last_focus(self) -> int:
        """The field's index among focusable rows, the head's rows being all that precede it."""
        return len(self._head())

    # -- rendering ---------------------------------------------------------------------------

    def rows(self, state: object) -> list[Row]:
        assert isinstance(state, FieldState)
        out = self._head()
        if out:
            out.append(Row.gap())
        out.append(Row.heading(Line(Segment(self.prompt, self.tone("title")))))
        out.append(Row.gap())
        out.append(self._field(state))
        return out

    def _head(self) -> list[Row]:
        """The explanation above the field, one row per wrapped line.

        These rows carry no target but they do take the cursor. A field is the one surface whose
        prose can outgrow the window, and with a single focusable row in it every scrolling key is
        inert by construction — the frame would report content above and offer no way to read it.
        Focus and rows are both derived from this method so the two cannot disagree about how many
        rows there are after a resize.
        """
        return [
            Row(line=Line(Segment(line, self.tone("dim"))), focusable=True)
            for text in self.head
            for line in self.lines(text)
        ]

    def lines(self, text: str) -> list[str]:
        """``text`` broken into rows this body can hold.

        The line a masked field replaces is one sentence about where the key comes from and how
        long the value lives, and at a normal width it is longer than the window. Wrapping it here
        is what keeps that sentence whole on screen instead of clipped at the edge, which is the
        difference between telling the user where to persist the key and appearing not to.
        """
        if self.caps is None:
            return textwrap.wrap(text, self.room) or [""]
        return L.wrap(text, self.room, self.caps)

    def _field(self, state: FieldState) -> Row:
        """The field's own row: what has been typed, then where the next character goes.

        The focused row is already the one the gutter marks and the theme draws in bold cyan, but
        every one of those cues is a *colour* cue except the marker, and with colour off an empty
        field is otherwise an indistinguishable blank. The caret is the glyph that says text goes
        here, in a terminal that has no cursor to say it with.
        """
        if self.masked:
            bullet = "•" if self.caps is None or self.caps.unicode else "*"
            shown = self.clip(bullet * len(state.text))
        else:
            shown = self.clip(state.text)
        caret = CARET if self.caps is None or self.caps.unicode else CARET_ASCII
        return Row.item(
            Line(Segment(shown, self.tone("active")), Segment(caret, self.tone("focus"))), FIELD
        )

    def clip(self, text: str) -> str:
        """The leading columns of ``text`` that fit the body, leaving a column for the caret.

        Only the drawing is clipped: the value the caller gets back is the whole string, and it is
        the character count in the status row that says how much of it is off screen.
        """
        if self.caps is None:
            return text[: self.room]
        return L.cut(text, self.room - 1, self.caps)

    def status(self, state: object) -> Line:
        assert isinstance(state, FieldState)
        if not state.text:
            return Line(Segment("nothing entered yet", self.tone("dim")))
        count = len(state.text)
        parts = [Segment(f"{count} character{'s' if count != 1 else ''}", self.tone("active"))]
        if self.masked:
            parts.append(Segment(" · hidden", self.tone("dim")))
        return Line(*parts)
