"""The launch sequence's fullscreen modal interface.

One engine, eight steps. ``caps`` decides what the terminal can do, ``keys`` turns bytes into
keypresses and owns the binding table the footer is generated from, ``cells`` holds the buffer the
diff writes from, ``layout`` divides a window into regions and scrolls what does not fit, ``term``
owns the terminal itself, ``app`` runs one step until it answers, ``menu`` is the list-of-options
step that most of the launcher's steps are, ``forest`` is the tree-of-switches step, ``input`` is
the one-field step for the two questions that take typed text instead of a choice, and ``flow``
remembers which steps have answered and where a back-navigation should land.

A surface imports ``Step``, ``View``, ``Result`` and ``run``, describes its rows, and gets the rest:
the title, the step rail, the focus ring, the scrollbar, the overflow counts, the help overlay,
reset, resize, and a footer that cannot advertise a key the step does not answer.

``menu`` rather than ``list``, then: importing a submodule binds it as an attribute of this
package, so a ``tui.list`` would quietly replace the builtin for anything here that reached for
``list()``.
"""

from __future__ import annotations

from .app import (
    ABORT,
    ACCEPT,
    BACK,
    CLOSE,
    FIRST,
    FOCUS_DOWN,
    FOCUS_UP,
    HELP,
    LAST,
    PAGE_DOWN,
    PAGE_UP,
    RESET,
    TOGGLE,
    Modal,
    Result,
    Session,
    Step,
    View,
    navigation,
    run,
)
from .caps import Caps, detect
from .cells import Screen
from .flow import ABORTED, COMMITTED, CONTINUE, GO_BACK, MISSING, TARGET, Flow, is_live
from .forest import CHECK, PLAIN, WORD, ForestState, ForestStep, Node, next_state, walk
from .input import DELETE, FIELD, FieldState, FieldStep
from .keys import Binding, Key, Wheel, bind, legend, lookup

# ``layout`` the function is deliberately not re-exported: it would shadow ``tui.layout`` the
# module for any caller that reached for a region type by attribute, and the frame splitter is the
# engine's own arithmetic rather than anything a surface calls.
from .layout import BLANK, Line, Rect, Row, Segment, Window
from .menu import MULTI, SINGLE, Choice, ListState, ListStep
from .term import Terminal

__all__ = [
    "ABORT",
    "ABORTED",
    "ACCEPT",
    "BACK",
    "BLANK",
    "Binding",
    "CHECK",
    "CLOSE",
    "COMMITTED",
    "CONTINUE",
    "Caps",
    "Choice",
    "DELETE",
    "FIELD",
    "FIRST",
    "FOCUS_DOWN",
    "FOCUS_UP",
    "FieldState",
    "FieldStep",
    "Flow",
    "ForestState",
    "ForestStep",
    "GO_BACK",
    "HELP",
    "Key",
    "LAST",
    "Line",
    "ListState",
    "ListStep",
    "MULTI",
    "MISSING",
    "Modal",
    "Node",
    "PAGE_DOWN",
    "PAGE_UP",
    "PLAIN",
    "RESET",
    "Rect",
    "Result",
    "Row",
    "SINGLE",
    "Segment",
    "Screen",
    "Session",
    "Step",
    "TARGET",
    "TOGGLE",
    "Terminal",
    "WORD",
    "View",
    "Wheel",
    "Window",
    "bind",
    "detect",
    "is_live",
    "legend",
    "lookup",
    "navigation",
    "next_state",
    "run",
    "walk",
]
