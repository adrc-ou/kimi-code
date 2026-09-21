"""The shared run loop every step is rendered by, and the protocol a step implements.

The division of labour is the point of the redesign. The loop owns *navigation* — focus, paging,
scrolling, the help overlay, reset, the footer legend, resize — because that is the same on all nine
surfaces, and a step that re-implements it is a step that will do it slightly differently. A step
owns *meaning*: which rows exist, which one is focused, what an action does to it, and what
committing produces.

That split is also why the surfaces read as one application instead of nine programs that happen to
share a colour scheme. Nothing in this module knows what a module, a model, or a context block is.

Mouse reporting is deliberately **not** enabled. The engine decodes wheel and click events and
``Terminal`` will switch them on if ``Caps.mouse`` is set, but every list in this launcher is
between two and nine rows, so the gain is negligible — while enabling reporting suppresses the
terminal's own click-and-drag text selection, and a configuration screen is exactly where someone
copies a model identifier out of it. A surface with genuinely long content can turn it on without
the engine changing.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import layout as L
from .caps import Caps
from .cells import Screen
from .flow import CONTINUE, GO_BACK
from .keys import Binding, Key, bind, display, legend_lines, lookup
from .layout import BLANK, Line, Row, Segment, Window
from .term import Terminal

#: Actions the loop answers itself, so no step can disagree about them by accident.
FOCUS_UP = "focus-up"
FOCUS_DOWN = "focus-down"
PAGE_UP = "page-up"
PAGE_DOWN = "page-down"
FIRST = "first"
LAST = "last"
HELP = "help"
CLOSE = "close"
RESET = "reset"

#: Actions a step may answer.
TOGGLE = "toggle"
ACCEPT = "accept"
BACK = "back"
ABORT = "abort"

#: Exit statuses come from ``flow`` rather than being restated here: the shell, the state file and
#: this loop have to agree on the three numbers, and a second copy is a second thing to get wrong.
#: Back is its own code rather than a field on the result because it has to survive a process
#: boundary and stay legible in ``start.sh`` without parsing stdout.

#: How long the loop blocks before looking at the window size again. Blocking forever would be
#: cheaper, but a resize would then sit unnoticed until the next keypress, and a stale frame in a
#: resized window is precisely the "feels sluggish" failure this redesign exists to remove. Four
#: wakeups a second, each one an ``ioctl``, is not something a user can measure.
INPUT_POLL = 0.25

#: How many rows the legend may ask for before it starts dropping keys. Three, because the four
#: affordances a step promises — confirm, go back, reset, scroll — are the whole argument for a
#: permanent hint bar, and two rows at eighty columns end it mid-list. The frame gives the third row
#: back on any window too short to spare it, so the cost falls where it can be paid.
MAX_FOOTER_ROWS = 3

#: Columns a wrapped legend row is painted in from the left, which the wrap has to pay for in its
#: measurement or the frame clips the row it was measured to fit.
FOOTER_INDENT = 2

#: What :attr:`Step.room` answers while no terminal is attached — eighty columns with the body's
#: furniture paid for, which is what :class:`~.caps.Caps` defaults to. A step built in a test and a
#: step drawn on a real window then disagree only about windows nobody types into.
UNTETHERED_ROOM = 77

#: The narrowest body a step is allowed to lay out. Below a handful of columns there is nothing left
#: to fit, and a step that divided by the difference would be measuring its own failure.
MIN_ROOM = 8


def navigation() -> tuple[Binding, ...]:
    """The keys every step answers, in legend order.

    Order is a budget decision as much as a discoverability one: the footer fills left to right and
    truncates on the right, so what leads is what survives a narrow window. The keys that end a
    step come first — ``Enter``, ``Space``, and ``Backspace`` where there is a step to go back to —
    then reset and the key reference, and only then the scrolling group, which the least experienced
    user needs least and the arrow keys already cover.

    Aliases follow the canonical spelling and are never printed: ``k``/``j`` work because people
    reach for them, and the legend says ``↑``/``↓`` because that is what everyone else reads.
    ``Backspace`` is spelled out because no surveyed tool has a back affordance, so there is no
    existing muscle memory to lean on — it has to be read before it can be used.
    """
    return (
        bind(ACCEPT, "continue", "Enter"),
        bind(TOGGLE, "toggle", "Space"),
        bind(RESET, "reset choices", "Ctrl-R"),
        bind(HELP, "all keys", "?"),
        bind(FOCUS_UP, "move up", "Up", "k"),
        bind(FOCUS_DOWN, "move down", "Down", "j"),
        bind(PAGE_UP, "page up", "PgUp", "Ctrl-U"),
        bind(PAGE_DOWN, "page down", "PgDn", "Ctrl-D"),
        bind(FIRST, "first", "Home"),
        bind(LAST, "last", "End"),
        bind(ABORT, "abort", "Ctrl-C"),
    )


#: Spliced in after ``continue`` wherever the flow has an earlier answer to return to.
#:
#: It carries its own condition rather than being added or omitted by the caller, so a step that
#: cannot go back still *lists* the key and the overlay can say why it is inert. Leaving it out
#: entirely would make the footer honest and the reference wrong, and the reference is the place
#: a user goes to find out what a key does.
BACK_BINDING = bind(BACK, "previous step", "Backspace", when=lambda view: view.can_go_back)

#: The keys that move a cursor rather than change an answer, and so are last in the legend.
#:
#: Everyone already knows the arrow keys and ``PgDn``; a step's own key is the one nobody can
#: guess. When the footer runs out of room, that is what the budget should have bought.
_MOVEMENT = frozenset({FOCUS_UP, FOCUS_DOWN, PAGE_UP, PAGE_DOWN, FIRST, LAST, ABORT})

#: The overlay's own keys. ``Esc`` closes the overlay; it does not quit the step. A bare ``Esc`` as
#: an abort would also collide with escape sequences a terminal may not have finished sending, which
#: is why no surveyed tool risks it and neither does this one.
HELP_KEYS = (
    bind(FOCUS_UP, "scroll up", "Up", "k"),
    bind(FOCUS_DOWN, "scroll down", "Down", "j"),
    bind(PAGE_UP, "page up", "PgUp", "Ctrl-U"),
    bind(PAGE_DOWN, "page down", "PgDn", "Ctrl-D"),
    bind(FIRST, "top", "Home"),
    bind(LAST, "bottom", "End"),
    bind(CLOSE, "close", "Escape", "?", "Enter", "q"),
)


@dataclass(frozen=True)
class View:
    """Where a step sits in the flow — chrome a step cannot know on its own.

    Carried apart from the step's state because each surface is a separate process and only the
    launcher knows how many there are. Without it the step rail would have to guess.
    """

    position: int = 1
    total: int = 1
    label: str = ""
    can_go_back: bool = False


def rail_text(view: View, step: Step) -> str:
    """What the step rail says: how far along the user is, plus the step's name if that is news.

    Both halves of the rule come from one question — does this row add anything the title row does
    not already say? A rail that repeats the title spends a row saying it twice, and a rail with
    nothing left to say spends a row saying nothing, so the body gets it instead. With one step
    there is no progress to report either, which is why a lone module picker shows no rail at all.
    """
    name = view.label or step.rail
    if name == step.title:
        name = ""
    if view.total <= 1:
        return name
    progress = f"{view.position} of {view.total}"
    return f"{progress} · {name}" if name else progress


@dataclass
class Session:
    """Loop-owned view state, kept out of the step so navigation cannot be forked per surface."""

    state: object = None
    scroll: int = 0
    help_open: bool = False
    help_scroll: int = 0
    note: str = ""


@dataclass(frozen=True)
class Result:
    """What one surface hands back: an exit status, a value, and a line for the scrollback."""

    status: int = CONTINUE
    value: object = None
    #: Printed by the launcher once the screen is gone, so the answer survives in the
    #: scrollback that the non-interactive printout still uses.
    summary: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == CONTINUE

    @property
    def going_back(self) -> bool:
        return self.status == GO_BACK


class Step:
    """What a surface provides. Everything else, the loop already does.

    A subclass answers four questions — which rows, which one is focused, what an action does, what
    committing produces — and inherits the frame, the scrolling, the legend, the help overlay, the
    reset key and the resize behaviour.
    """

    #: Human-friendly name for this step, drawn top-left in the frame.
    title: str = ""
    #: Shorter name for this step, for the step rail. Where it equals :attr:`title` the rail says
    #: only the position, since the title row has already named the step.
    rail: str = ""
    #: Whether ``Space`` means anything here. A radio step says no and the legend drops the entry,
    #: rather than advertising a key that would be ignored.
    toggleable: bool = True
    #: Whether a character that answers no binding should reach :meth:`typed`. Only a step
    #: that takes text says yes. Everywhere else a stray letter is noise, and the note row is
    #: what tells the user that the keypress was seen rather than dropped.
    accepts_text: bool = False
    #: What the note row says after ``Ctrl-R``, because what a reset puts back is the step's
    #: business and the loop only knows that it happened.
    reset_note: str = "choices reset"
    #: What the terminal can do, set by the :class:`Modal` that renders this step and refreshed on
    #: resize. A step is never supposed to probe for itself — a second detection would disagree with
    #: the first whenever the launcher pipes progress to a log — so it is ``None`` until then, and
    #: :meth:`tone` and :meth:`cells` answer for a step used outside a loop.
    caps: Caps | None = None

    def tone(self, role: str) -> str:
        """The SGR for one semantic colour role, or ``""`` where colour is off.

        Colour is how this interface says *superseded*, *in use*, and *this is the cursor* without
        adding words, so a step has to be able to ask for it — but only by role, so that one table
        still decides what every step means by it.
        """
        return self.caps.color_pair(role) if self.caps is not None else ""

    def cells(self, text: str) -> int:
        """Columns ``text`` occupies on this terminal, or its length before one is attached."""
        return self.caps.width(text) if self.caps is not None else len(text)

    @property
    def room(self) -> int:
        """Columns a body row actually has, which is not the width of the window.

        The gutter that holds the focus marker and the column that may hold a scrollbar are carved
        out of the body by :func:`~.layout.layout`, so a row measured against ``columns`` is a row
        that gets clipped at the frame edge and looks truncated for no reason the user can see.
        """
        if self.caps is None:
            return UNTETHERED_ROOM
        return max(MIN_ROOM, self.caps.columns - L.GUTTER_WIDTH - L.SCROLLBAR_WIDTH)

    def initial(self) -> object:
        raise NotImplementedError

    def rows(self, state: object) -> list[Row]:
        """The whole body — headings, gaps and selectable items alike, focusable or not."""
        raise NotImplementedError

    def focus(self, state: object) -> int:
        """Index into the *focusable* rows, not into ``rows()``."""
        return 0

    def with_focus(self, state: object, index: int) -> object:
        return state

    def status(self, state: object) -> Line:
        """The status row: what the current answer amounts to, in the user's terms."""
        return BLANK

    def toggled(self, state: object, target: object) -> object:
        return state

    def chosen(self, state: object, target: object) -> object:
        """``Enter`` landed on this row, which is not the same as committing.

        A radio list records the choice here; a multi-select usually ignores it and commits whatever
        is checked.
        """
        del target
        return state

    def commit(self, state: object) -> Result:
        """Accept the step. A non-``CONTINUE`` status sends the user elsewhere."""
        return Result()

    def incomplete(self, state: object) -> str:
        """Why ``Enter`` may not close this step yet, or ``""`` when it may.

        Checked by the loop before :meth:`commit`, so a step whose answer is not usable *yet*
        keeps the screen rather than closing and reopening it. This is what replaces the re-ask
        loop of the interface being superseded, where a blank reply simply printed the prompt
        again.
        """
        del state
        return ""

    def reset(self, state: object) -> object:
        """The state as it was when the step opened, focus included.

        The brief asks for resetting the *visible options*, but users read a reset that leaves the
        cursor wherever they dragged it as half-done, so this restores the whole initial state.
        """
        del state
        return self.initial()

    def answer(self, state: object, action: str, target: object) -> tuple[object, Result | None]:
        """The single extension point for step-specific keys.

        One method rather than a dispatcher of named callbacks, because a step that does not handle
        an action returns ``(state, None)`` and the key is ignored — the alternative is an
        ``AttributeError`` in the middle of a modal.
        """
        del action, target
        return state, None

    def typed(self, state: object, char: str) -> tuple[object, Result | None]:
        """One printable character the user typed at the screen, for a step that takes text.

        Separate from :meth:`answer` because an action name does not carry the character, and a
        field that cannot see what was pressed cannot fill itself in. Only called when
        :attr:`accepts_text` is set, so a list never has to explain why it ignored a letter.

        The character has already survived :class:`~.keys.Decoder`, so it is never a control
        byte and never part of a sequence still arriving. It is not filtered any further than
        that here: what an interface accepts is the step's decision, not the loop's.
        """
        del char
        return state, None

    def custom(self, state: object) -> tuple[Binding, ...]:
        """Step-specific bindings, which the shared navigation block does not know about.

        A secret step adds paste, the context step adds its tri-state key. These land ahead of the
        scrolling group rather than behind it: on a narrow window the footer is a budget, and a
        key that only exists on this one screen is the key nobody can guess, whereas everyone
        already knows ``PgDn``.
        """
        del state
        return ()

    def keys(self, state: object, view: View) -> tuple[Binding, ...]:
        """The complete table for this step, generated rather than declared.

        Generation is why the legend can be trusted: a surface cannot render a hint for a key it
        stopped answering, which is how the current panel ended up documenting ``a``/``n``/``r``.

        The order is the legend's order, and the legend truncates on the right, so it is also the
        priority: what ends a step, what changes the answer, what this step uniquely offers, and
        only then how to move around inside it.
        """
        table = list(navigation())
        index = next(
            (position for position, item in enumerate(table) if item.action == ACCEPT),
            len(table) - 1,
        )
        # Right after ``continue``, so the three keys that move between steps read as one group and
        # none of them can be the first thing a truncating footer drops. Its ``when`` decides
        # whether it is offered, listed, or merely explained.
        table.insert(index + 1, BACK_BINDING)
        if not self.toggleable:
            table = [item for item in table if item.action != TOGGLE]
        extra = self.custom(state)
        if extra:
            at = next(
                (position for position, item in enumerate(table) if item.action in _MOVEMENT),
                len(table),
            )
            table[at:at] = list(extra)
        return tuple(table)


class Modal:
    """One step, on screen, until it produces a result."""

    def __init__(
        self,
        step: Step,
        view: View | None = None,
        *,
        terminal: Terminal | None = None,
        caps: Caps | None = None,
    ) -> None:
        self.step = step
        self.view = view if view is not None else View()
        # ``caps`` is passed through rather than defaulted here: Terminal detects against its own
        # output stream, and a bare detect() in this process would see a non-tty stdout whenever
        # the launcher pipes progress and colour the screen off for no reason.
        self.terminal = terminal if terminal is not None else Terminal(caps, title=step.title)
        self.caps: Caps = self.terminal.caps
        self.screen = Screen(self.caps, self.caps.rows, self.caps.columns)
        self._rail = rail_text(self.view, step)
        self.frame = L.layout(self.caps.columns, self.caps.rows, rail=bool(self._rail))
        self._legend: list[str] = []
        # Ahead of ``initial()``, so a step may measure text while working out where to open.
        step.caps = self.caps
        self.session = Session(state=step.initial())

    # -- the loop ---------------------------------------------------------------------------

    def run(self) -> Result:
        """Take over the screen until the step commits, goes back, or aborts."""
        with self.terminal:
            self.adopt()
            while True:
                self.paint()
                for key in self.terminal.keys(timeout=INPUT_POLL):
                    outcome = self.handle(key)
                    if outcome is not None:
                        self.screen.clear()
                        return outcome
                self.adopt()

    def adopt(self) -> None:
        """Pull the terminal's current size into the renderer, after a resize or on entry."""
        if self.terminal.resized() or self.caps is not self.terminal.caps:
            self.caps = self.terminal.caps
            self.screen.resize(self.caps.rows, self.caps.columns)
            # The step measures against the same object the frame does, or a clip computed from a
            # stale width leaves a row that no longer fits the window it is drawn in.
            self.step.caps = self.caps

    def handle(self, key: Key) -> Result | None:
        """Dispatch one keypress, returning a result once the step is finished."""
        if self.session.help_open:
            return self._help_key(key)
        action = lookup(self._table(), key, self.view)
        if not action:
            # Space arrives as a named key with no character attached, and it is a character
            # wherever a list has already given up its binding for it.
            char = key.char or (" " if key.name == "Space" else "")
            if self.step.accepts_text and char:
                return self._type(char)
            self.session.note = _unbound(key)
            return None
        return self._act(action)

    def _type(self, char: str) -> Result | None:
        """Hand one typed character to the step that is taking text."""
        moved, outcome = self.step.typed(self.session.state, char)
        self.session.state = moved
        # Whatever the note row was complaining about is stale the moment the user starts answering.
        self.session.note = ""
        return outcome

    def _act(self, action: str) -> Result | None:
        session = self.session
        state = session.state
        rows, order = self._body()
        focused = self.step.focus(state)
        target = _target(rows, order, focused)
        if action in (FOCUS_UP, FOCUS_DOWN):
            delta = -1 if action == FOCUS_UP else 1
            session.state = self.step.with_focus(state, _advance(focused, delta, len(order)))
        elif action in (PAGE_UP, PAGE_DOWN):
            direction = -1 if action == PAGE_UP else 1
            index = L.page(len(order), self._viewport(rows), direction, focused)
            session.state = self.step.with_focus(state, index)
        elif action in (FIRST, LAST):
            index = 0 if action == FIRST else max(0, len(order) - 1)
            session.state = self.step.with_focus(state, index)
        elif action == TOGGLE:
            session.state = self.step.toggled(state, target)
        elif action == ACCEPT:
            session.state = self.step.chosen(state, target)
            reason = self.step.incomplete(session.state)
            if reason:
                # The screen stays up and says why, which is what the old prompt loop did by
                # printing the question again — minus the flicker of closing and reopening a
                # modal.
                session.note = reason
                return None
            return self.step.commit(session.state)
        elif action == BACK:
            return Result(status=GO_BACK)
        elif action == RESET:
            session.state = self.step.reset(state)
            session.scroll = 0
            session.note = self.step.reset_note
            return None
        elif action == HELP:
            session.help_open = True
            session.help_scroll = 0
        elif action == ABORT:
            # Only reachable when the terminal delivered 0x03 as a byte instead of as SIGINT, which
            # cbreak should prevent. Honouring it anyway costs nothing and means the key the legend
            # advertises always does what it says.
            raise KeyboardInterrupt
        else:
            moved, outcome = self.step.answer(state, action, target)
            session.state = moved
            if outcome is not None:
                return outcome
            if moved is state:
                session.note = f"{display(action)} does nothing here"
                return None
        session.note = ""
        return None

    def _help_key(self, key: Key) -> Result | None:
        """Keys inside the overlay: it scrolls and closes, and nothing else.

        The overlay is inert by design. A key that both dismisses the reference and changes an
        answer would be the worst possible magic keystroke — a ``?`` pressed to read the help
        should never be able to alter the configuration.
        """
        action = lookup(HELP_KEYS, key, self.view)
        if not action or action == CLOSE:
            self.session.help_open = False
            return None
        lines = self._help_lines()
        last = max(0, len(lines) - 1)
        current = self.session.help_scroll
        if action == FOCUS_UP:
            self.session.help_scroll = max(0, current - 1)
        elif action == FOCUS_DOWN:
            self.session.help_scroll = min(last, current + 1)
        elif action in (PAGE_UP, PAGE_DOWN):
            step = max(1, self.frame.body.height - 1)
            delta = -step if action == PAGE_UP else step
            self.session.help_scroll = max(0, min(last, current + delta))
        elif action == FIRST:
            self.session.help_scroll = 0
        elif action == LAST:
            self.session.help_scroll = last
        return None

    # -- painting ---------------------------------------------------------------------------

    def paint(self) -> None:
        """Compose the frame into the back buffer, then write only what changed."""
        # Two passes, because the footer's height is a negotiation: the legend says how many rows it
        # would like, the frame says how many a window this short can spare, and only then does the
        # legend get wrapped to what it was actually given. Wrapping once and clipping the result
        # would drop the ellipsis and leave a truncated bar that looks complete.
        table = self._table()
        wanted = legend_lines(
            table, self.view, self.caps, width=self.caps.columns, rows=MAX_FOOTER_ROWS
        )
        self._rail = rail_text(self.view, self.step)
        self.frame = L.layout(
            self.caps.columns,
            self.caps.rows,
            footer_rows=max(1, len(wanted)),
            rail=bool(self._rail),
        )
        # A continuation row is painted indented, so the wrap has to happen in the narrower box it
        # will actually get: measured at the full width, the last row comes out two columns too wide
        # and the frame clips it mid-word instead of the wrapper dropping a whole hint.
        indent = FOOTER_INDENT if self.frame.footer.height > 1 else 0
        self._legend = legend_lines(
            table,
            self.view,
            self.caps,
            width=self.frame.footer.width - indent,
            rows=self.frame.footer.height,
        )
        rows, order = self._body()
        if self.session.help_open:
            self._paint_help()
        else:
            self._paint_body(rows, order)
        self._paint_chrome()
        self.terminal.write(self.screen.paint())

    def _paint_chrome(self) -> None:
        frame = self.frame
        L.paint_rule(self.screen, frame.rule_top, self.caps)
        if frame.has("status"):
            L.paint_rule(self.screen, frame.rule_bottom, self.caps)
        if frame.has("title"):
            L.paint_line(
                self.screen,
                frame.title.top,
                self._span(frame.title.top),
                Line(Segment(self.step.title or "Configure", self.caps.color_pair("title"))),
                self.caps,
            )
        if frame.has("rail"):
            self._paint_rail()
        if frame.has("status"):
            self._paint_status()
        self._paint_footer()

    def _paint_rail(self) -> None:
        """Position and label, plus a fill bar.

        The count answers "where am I"; the bar answers "how much is left" without being read. It is
        blocks rather than a colour ramp so that it still means something with colour off.
        """
        view = self.view
        fill = ""
        if view.total > 1:
            width = max(4, min(24, self.caps.columns // 4))
            done = max(1, min(width, round(width * view.position / view.total)))
            filled, empty = ("▰", "▱") if self.caps.unicode else ("#", ".")
            fill = "  " + filled * done + empty * (width - done)
        L.paint_line(
            self.screen,
            self.frame.rail.top,
            self._span(self.frame.rail.top),
            Line(
                Segment(self._rail, self.caps.color_pair("info")),
                Segment(fill, self.caps.color_pair("rule")),
            ),
            self.caps,
        )

    def _paint_status(self) -> None:
        parts = [self.step.status(self.session.state)]
        if self.session.note:
            if parts[0].segments:
                parts.append(Line(Segment("   ")))
            parts.append(Line(Segment(self.session.note, self.caps.color_pair("warn"))))
        L.paint_line(
            self.screen,
            self.frame.status.top,
            self._span(self.frame.status.top),
            Line(*(part.segments for part in parts)),
            self.caps,
        )

    def _paint_footer(self) -> None:
        """The legend, generated from the same table the loop dispatched through.

        This is the affordance the brief wants in place of magic keystrokes: every key the step
        answers is on screen permanently, and nothing on screen is a key the step does not answer.

        ``paint()`` already worked out how many rows it needs, so the footer is as tall as its own
        text and no taller — the body never loses a row to a legend that fit on one line. A
        continuation row is indented, which is how a wrapped hint bar stays readable as one
        sentence instead of looking like a second status line.
        """
        attr = self.caps.color_pair("focus")
        footer = self.frame.footer
        for offset, text in enumerate(self._legend[: footer.height]):
            row = footer.top + offset
            L.paint_line(
                self.screen,
                row,
                self._span(row),
                Line(Segment((FOOTER_INDENT * " " if offset else "") + text, attr)),
                self.caps,
            )

    def _paint_body(self, rows: list[Row], order: list[int]) -> None:
        body = self.frame.body
        height = self._viewport(rows)
        at = _target_row(order, self.step.focus(self.session.state))
        self.session.scroll = L.scroll_for(len(rows), height, at, self.session.scroll)
        window = L.visible_window(len(rows), height, self.session.scroll)
        L.paint_rows(
            self.screen,
            body,
            rows,
            window,
            self.caps,
            focused=at,
            focus_attr=self.caps.color_pair("focus"),
        )
        L.scrollbar(self.screen, body, window, len(rows), self.caps)
        self._paint_overflow(window, height)

    def _paint_overflow(self, window: Window, height: int) -> None:
        """One reserved row under the viewport, naming what is hidden on each side.

        A scrollbar shows *that* content continues; a count shows *how much*, which is what makes a
        user decide to press the key again instead of assuming they had reached the end. Both
        directions share a row so a long list costs one row of content, not two.
        """
        if not window.scrolling:
            return
        note = Line()
        if window.above:
            note += L.overflow_note(window.above, "above", self.caps)
        if window.above and window.below:
            note += Line(Segment("   ", self.caps.color_pair("rule")))
        if window.below:
            note += L.overflow_note(window.below, "below", self.caps)
        row = self.frame.body.top + height
        L.paint_line(self.screen, row, self._span(row), note, self.caps)

    def _paint_help(self) -> None:
        """The overlay of every binding, generated from the same table as the footer.

        Overlaid rather than a tenth step, because a key reference you must navigate *to* is one
        fewer than anyone reads. It is also the escape hatch for a legend that had to truncate on a
        narrow window, so truncation and overlay are one mechanism rather than two half-answers.
        """
        lines = self._help_lines()
        body = self.frame.body
        height = max(1, body.height)
        self.session.help_scroll = L.scroll_for(
            len(lines), height, -1, max(0, self.session.help_scroll)
        )
        window = L.visible_window(len(lines), height, self.session.help_scroll)
        for offset in range(window.rows):
            row = body.top + offset
            if row >= body.bottom:
                break
            index = window.first + offset
            value = lines[index] if index < len(lines) else BLANK
            L.paint_line(self.screen, row, self._span(row), value, self.caps)
        L.scrollbar(self.screen, body, window, len(lines), self.caps)

    def _help_lines(self) -> list[Line]:
        """Every binding this step answers, and every spelling of it, including unavailable ones.

        Two things this fixes that a list of canonical keys would not. Listing a dimmed
        ``Backspace`` that is not offered on the first step lets the overlay explain *why* a key
        does nothing, where a footer that quietly omits it leaves the user guessing whether they
        misread it. And printing whatever aliases the step has — ``k`` beside ``↑``, ``Ctrl-U``
        beside ``PgUp``, and none of them on a step where a letter has to stay typeable — is what
        makes the word ``all`` in ``? all keys`` true: the footer keeps one spelling per action so
        it stays short, so the overlay is the only place an alias could be found, and a key nobody
        can discover is the magic keystroke this interface is not allowed to have.

        A step's own bindings appear here too, which is why the overlay is generated from the
        table rather than written by the step.
        """
        dim = self.caps.color_pair("dim")
        table = self._table()
        labels = [L.styled("/".join(display(name) for name in item.keys)) for item in table]
        width = max((L.fit(label, 16, self.caps).width(self.caps) for label in labels), default=4)
        out = [
            Line(Segment("Keys", self.caps.color_pair("title"))),
            Line(Segment(f"  {self.step.title}", dim)),
            BLANK,
        ]
        for item, label in zip(table, labels, strict=True):
            available = item.available(self.view)
            tail = item.description if available else f"{item.description} — not on this step"
            out.append(
                Line(
                    Segment("  "),
                    *L.padded(L.fit(label, width, self.caps), width, self.caps).segments,
                    Segment("  "),
                    Segment(tail, self.caps.color_pair("focus") if available else dim),
                )
            )
        out.append(BLANK)
        out.append(Line(Segment("  ↑ ↓ PgUp PgDn scroll this list · any other key closes it", dim)))
        return out

    # -- geometry ---------------------------------------------------------------------------

    def _table(self) -> tuple[Binding, ...]:
        return self.step.keys(self.session.state, self.view)

    def _body(self) -> tuple[list[Row], list[int]]:
        rows = self.step.rows(self.session.state)
        return rows, L.focusable_rows(rows)

    def _span(self, row: int) -> L.Rect:
        return L.Rect(row, 0, 1, self.caps.columns)

    def _viewport(self, rows: list[Row]) -> int:
        """Rows available for content, given that overflow reserves one row for its note.

        Reserving only when the list is *already* too long for the full body is the non-circular way
        to do it: the comparison is made once against a height that cannot change as a result of it.
        """
        height = self.frame.body.height
        if len(rows) > height:
            return max(1, height - 1)
        return max(1, height)


def run(
    step: Step,
    view: View | None = None,
    *,
    terminal: Terminal | None = None,
    caps: Caps | None = None,
) -> Result:
    """Render one step modally and return its result.

    This is the only entry point a surface needs. A CLI wrapper passes ``result.status`` straight
    to ``sys.exit``: it is already one of ``flow.CONTINUE``, ``flow.GO_BACK`` or ``flow.ABORTED``,
    which is the whole contract between a step and the launcher's loop.
    """
    return Modal(step, view, terminal=terminal, caps=caps).run()


def _advance(current: int, delta: int, total: int) -> int:
    """Move focus within the selectable rows, without wrapping.

    Not wrapping is a decision, not an omission: when the last row is ``Other`` or a trailing
    note, wrapping from it to the first row reads as a missed keypress, because the user has no
    way to tell that the cursor teleported.
    """
    if total <= 0:
        return 0
    return max(0, min(current + delta, total - 1))


def _target_row(order: list[int], focus: int) -> int:
    return order[focus] if order and 0 <= focus < len(order) else -1


def _target(rows: list[Row], order: list[int], focus: int) -> object:
    at = _target_row(order, focus)
    return rows[at].target if 0 <= at < len(rows) else None


def _unbound(key: Key) -> str:
    """Say so, rather than doing nothing silently.

    The alternative is a keypress with no visible consequence, which is how a user concludes the
    interface has hung. One line in the status row turns that into an answer.
    """
    return f"{display(key.name)} does nothing here — press ? for keys"
