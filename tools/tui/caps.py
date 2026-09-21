"""Terminal capabilities, resolved once, before anything is drawn.

Two independent questions are answered here and kept apart deliberately. *Colour* asks how many
colour slots may be addressed, and *glyphs* asks whether a non-ASCII character can be both sent
and counted correctly. A terminal can answer either one either way, so a caller must never infer
one from the other.

Colour tiers address the sixteen ANSI slots by default rather than emitting RGB. A terminal's
palette belongs to the person using it: a program that only picks slots inherits whatever
contrast they configured, instead of authoring contrast it has no way to measure. The truecolor
tier exists so an explicit theme may use it, not so that one becomes the default.

The contracts followed are ``NO_COLOR`` (present and non-empty suppresses colour, but leaves
bold, underline and dim alone, and per-instance configuration outranks it) and the ``COLORTERM``
convention, which errs toward silence: an absent value means unsupported, not unknown.
"""

from __future__ import annotations

import locale
import os
import re
import unicodedata
from dataclasses import dataclass, field

#: Colour tiers, in increasing capability. ``NONE`` still permits weight and underline.
NONE, ANSI16, ANSI256, TRUECOLOR = "none", "16", "256", "truecolor"

_UTF8 = re.compile(r"utf[-_]?8", re.IGNORECASE)
#: Locales whose terminals conventionally render East Asian Ambiguous characters two cells wide.
_AMBIGUOUS_WIDE = re.compile(r"^(zh|ja|ko)", re.IGNORECASE)

#: Names that mean "this terminal cannot be trusted with escape sequences". An *unset* ``TERM`` is
#: not one of them: containers and test harnesses routinely have no ``TERM`` and still sit in front
#: of a real terminal, and git treats that as colour-capable for the same reason.
_DUMB_TERMS = ("dumb", "unknown", "vt52")


def _present(name: str) -> str:
    """A variable's value when it is set and not an empty string, else ``""``.

    ``NO_COLOR`` is defined by presence rather than truth, so ``NO_COLOR=0`` still means no
    colour; ``CLICOLOR`` is defined the other way round by its own convention, so it is tested
    for a non-zero value separately.
    """
    value = os.environ.get(name, "")
    return "" if value == "" else value


@dataclass(frozen=True)
class Caps:
    """Everything the renderer is allowed to assume about the terminal."""

    color: str = ANSI16
    unicode: bool = True
    ambiguous_wide: bool = False
    theme: str = "dark"
    #: Whether the window title may be rewritten through ``OSC 2``.
    titles: bool = True
    #: Whether mouse reports were asked for; enabling them is always opt-in.
    mouse: bool = False
    columns: int = 80
    rows: int = 24
    probe: bool = field(default=True, compare=False)

    @property
    def styled(self) -> bool:
        return self.color != NONE

    def width(self, text: str) -> int:
        """Columns ``text`` occupies on *this* terminal.

        Every measurement in the renderer goes through here, because the same string is a
        different number of columns in a CJK locale and a layout that guessed would drift.
        """
        return width(text, self.ambiguous_wide)

    def sgr(self, *codes: str) -> str:
        """Wrap codes for this terminal, or return ``""`` when colour is off.

        Weight and underline survive ``NO_COLOR`` on purpose; they are the load-bearing part of
        the interface, because the interface never lets hue carry meaning alone.
        """
        return "" if not codes else "\033[" + ";".join(codes) + "m"

    def color_pair(self, name: str) -> str:
        """The SGR for one semantic colour role, or ``""`` when colour is suppressed.

        Roles are resolved here and nowhere else, so a theme change is one table and a
        no-colour terminal gets the same layout rather than a degraded one.
        """
        if not self.styled:
            return ""
        return _ROLES.get(name, "")


#: Semantic roles over the sixteen ANSI slots. Magenta carries the second kind of link so that
#: separating them never depends on telling red from green, and warnings are yellow for the same
#: reason. Superseded text is dim *and* hollow *and* labelled; see ``layout``.
_ROLES = {
    "focus": "\033[1;36m",  # bold cyan
    "title": "\033[1;97m",  # bold bright white
    "active": "\033[32m",  # green: a block that is in use
    "link": "\033[35m",  # magenta: template substitution, composed from several sources
    "warn": "\033[33m",  # yellow: needs attention, but is not an error
    "info": "\033[34m",  # blue: incidental information
    "dim": "\033[2m",  # dim: superseded, and always paired with a word
    "rule": "\033[90m",  # bright black: chrome that should recede
    "over": "\033[1;35m",  # bold magenta: a session override, which is unusual by design
    "error": "\033[1;31m",
}


def glyph(caps: Caps, unicode_value: str, ascii_value: str) -> str:
    """Pick the glyph register once, so no caller has to remember the locale exists."""
    return unicode_value if caps.unicode else ascii_value


def char_width(character: str, ambiguous_wide: bool) -> int:
    """Columns one character occupies; zero-width marks occupy none.

    East Asian Ambiguous is the reason this function exists: U+25CF ``●``, U+258C ``▌`` and the
    box-drawing set are all Ambiguous, so they are one cell in a Western terminal and two in a
    CJK one. Nothing may be assumed to be one cell wide, which is why the checkbox in use is
    ASCII ``[x]`` and not a circle.

    The return value is never negative. A combining mark still costs a cell to a layout that
    counts positions, so it contributes zero here rather than subtracting one from its neighbour
    and making ``"a" + U+0301`` measure narrower than ``"a"``.
    """
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn"):
        return 0
    if unicodedata.east_asian_width(character) in ("W", "F"):
        return 2
    if unicodedata.east_asian_width(character) == "A":
        return 2 if ambiguous_wide else 1
    return 1


def width(text: str, ambiguous_wide: bool = False) -> int:
    """Display columns of a string, ignoring combining marks and control characters."""
    return sum(char_width(character, ambiguous_wide) for character in text)


def _locale_text() -> str:
    """The locale that decides encoding and width, found by the usual variable precedence.

    ``LC_ALL`` outranks ``LC_CTYPE``, which outranks ``LANG``, and the first of them that is set
    settles the question on its own. That is what the C library does when it picks a locale, and
    the reason to copy it rather than collect all three: an ``LC_ALL=C`` sitting on top of an
    ambient ``LANG=en_US.UTF-8`` is somebody deliberately asking for byte-oriented output, and a
    search that joined the two would answer "UTF-8" and hand that terminal glyphs it just
    overrode.

    Only when none of the three is set does the process-level answer get consulted, through
    ``locale.getpreferredencoding`` with ``do_setlocale=False`` — asking it to set the locale
    would make a capability probe change the program's behaviour. That fallback matters because
    the variables are frequently all absent in a container while the C library is still
    initialised to something usable.
    """
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(name, "")
        if value:
            return value
    try:
        return locale.getpreferredencoding(False) or ""
    except locale.Error:
        # A locale the C library cannot name is not a reason to refuse to draw; the ASCII tier is
        # a safe floor and is what an empty return value selects below.
        return ""


#: Explicit answers a caller may give for the colour question, beyond ``auto``/``always``.
_PINNED_TIER = {"16": ANSI16, "256": ANSI256, "truecolor": TRUECOLOR}
#: Spellings of "do not use colour" that mean the same as ``--color=never``.
_OFF = ("never", "none", "no", "off", "0")


def _detected_tier() -> str:
    """The best tier the environment claims, ignoring whether colour is wanted at all.

    ``COLORTERM`` is asked first because it is the only variable that speaks for the terminal's
    actual capability; ``TERM`` names a family, and a ``TERM``-string check can only ever guess
    from it. Apple's Terminal is capped here rather than at sixteen because it renders 256-colour
    SGR faithfully and truecolor not at all — the one case where a terminal's identity, rather
    than its advertisement, is the better evidence.
    """
    colorterm = _present("COLORTERM").lower()
    # An absent COLORTERM is a negative answer, not a missing one; it is only set when support is
    # real. Case-sensitive matching is what the S-Lang check does, but terminals have drifted, so
    # the comparison is relaxed here rather than losing colour to a typo in an environment file.
    if colorterm in ("truecolor", "24bit"):
        return TRUECOLOR
    term = os.environ.get("TERM", "")
    if "256color" in term or "256 colours" in term:
        return ANSI256
    if "truecolor" in term:
        return TRUECOLOR
    if _present("TERM_PROGRAM") == "Apple_Terminal":
        return ANSI256
    return ANSI16


def _color_tier(isatty: bool, forced: str | None) -> str:
    """Resolve the colour tier, with an explicit caller choice outranking the environment.

    ``forced`` is a per-instance decision — a flag, or whatever the launcher settled on — and the
    ``NO_COLOR`` convention says so in its own FAQ: per-instance configuration wins over the
    ambient variable. That is why ``always`` is not merely "auto, more strongly". It skips the
    suppression rules that exist to honour a *user's* standing preference and a *pipe's* lack of
    a terminal, while still skipping nothing that describes the terminal itself: ``TERM=dumb``
    cannot render an escape sequence however loudly the caller asks, so that one stays.
    """
    mode = "auto" if forced in (None, "") else str(forced).lower()
    if mode in _PINNED_TIER:
        return _PINNED_TIER[mode]
    if mode in _OFF:
        return NONE
    if mode not in ("auto", "always"):
        raise ValueError(
            f"unknown colour mode {mode!r}; expected auto, always, never, 16, 256 or truecolor"
        )
    if os.environ.get("TERM", "") in _DUMB_TERMS:
        return NONE
    if _present("CLICOLOR_FORCE") not in ("", "0"):
        return _detected_tier()
    if mode == "always":
        return _detected_tier()
    if _present("NO_COLOR"):
        return NONE
    if os.environ.get("CLICOLOR", "") == "0":
        return NONE
    if not isatty:
        return NONE
    return _detected_tier()


def _columns_rows(fallback_columns: int, fallback_rows: int) -> tuple[int, int]:
    """Window size, preferring the environment hint that pty callers and ``resize(1)`` set.

    ``shutil.get_terminal_size`` already consults ``COLUMNS``/``LINES`` before the kernel, so this
    is the whole of the policy — re-reading those variables here would only create a second and
    slightly different answer to the same question.
    """
    try:
        import shutil

        columns, rows = shutil.get_terminal_size(fallback=(fallback_columns, fallback_rows))
    except OSError:  # pragma: no cover - get_terminal_size documents a fallback, not a raise
        columns, rows = fallback_columns, fallback_rows
    return max(20, columns), max(6, rows)


def detect(
    stream=None,
    *,
    color: str | None = None,
    theme: str | None = None,
    unicode_override: bool | None = None,
    mouse: bool = False,
    probe: bool = True,
) -> Caps:
    """Build the capability record for ``stream``, defaulting to standard output.

    ``color`` and ``theme`` are the caller's explicit answer, and they win: a flag is
    per-instance configuration, and per-instance configuration outranks the environment.
    """
    text = _locale_text()
    isatty = bool(stream is not None and hasattr(stream, "isatty") and stream.isatty())
    encoding_ok = bool(_UTF8.search(text)) or "utf" in text.lower()
    uses_unicode = encoding_ok if unicode_override is None else unicode_override
    columns, rows = _columns_rows(80, 24)
    resolved_theme = theme or os.environ.get("HARNESS_TUI_THEME") or "dark"
    if resolved_theme not in ("dark", "light"):
        raise ValueError(f"unknown theme {resolved_theme!r}; expected 'dark' or 'light'")
    term = os.environ.get("TERM", "")
    # ``dumb`` and an unset TERM both mean "assume nothing works", and an OSC query is exactly the
    # thing that would come back as literal text. GNU screen additionally rewrites OSC 2 into its
    # own hardstatus and can leave the title wrong after exit, so it is declined too.
    plain = term in ("", "dumb", "unknown")
    titles = isatty and not plain and "screen" not in term
    tier = _color_tier(isatty, color)
    return Caps(
        color=tier,
        unicode=uses_unicode,
        ambiguous_wide=bool(_AMBIGUOUS_WIDE.search(text)),
        theme=resolved_theme,
        titles=titles,
        mouse=mouse,
        columns=columns,
        rows=rows,
        # Probing is pointless without a tier left to upgrade into, and harmful on a terminal we
        # have just decided cannot take escape sequences: the query is the garbage it would print.
        probe=probe and isatty and tier != NONE,
    )


def truecolor_query() -> bytes:
    """The DECRQSS exchange that asks a terminal whether it accepted a truecolor set.

    Setting an improbable colour and reading it back is transparent to ``sudo`` and ``ssh``,
    which is exactly where ``COLORTERM`` is unreliable. A terminal that echoes the request back
    understood it; one that replies with a palette colour, replies ``CSI 0$r``, or stays silent did
    not.
    """
    return b"\033[48:2:1:2:3m\033P$qm\033\\"


def truecolor_supported(response: bytes) -> bool:
    """Interpret one reply to :func:`truecolor_query`."""
    if b"1$r48:2:1:2:3m" in response:
        return True
    if b"1$r48;2;1;2;3m" in response:  # terminals that answer with semicolon delimiters
        return True
    return False


def upgrade(caps: Caps, response: bytes) -> Caps:
    """Return ``caps`` widened to truecolor if ``response`` proves support, unchanged otherwise."""
    if caps.color == TRUECOLOR or not truecolor_supported(response):
        return caps
    return Caps(
        color=TRUECOLOR,
        unicode=caps.unicode,
        ambiguous_wide=caps.ambiguous_wide,
        theme=caps.theme,
        titles=caps.titles,
        mouse=caps.mouse,
        columns=caps.columns,
        rows=caps.rows,
        probe=False,
    )
