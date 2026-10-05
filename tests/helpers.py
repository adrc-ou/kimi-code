#!/usr/bin/env python3
"""Fixtures shared by the harness suite.

Kept deliberately small: only things that were genuinely duplicated, and only in the form the
production code expects. A helper here must never invent a value the shipped definitions already
supply, because a fixture that diverges from ``./models`` and ``./providers`` tests the fixture.
"""

from __future__ import annotations

import atexit
import fcntl
import importlib.util
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

#: Audiences as :mod:`prompt_measure` names them, mapped to the lane each one normally runs on.
#: A measured row has to be priced against the cap of the lane that produced it, so a test that
#: names an audience should not also have to know that lane's alias.
AUDIENCE_LANE = {"main": "primary", "subagent": "subagent"}


def load_script(name: str, relative: Path) -> ModuleType:
    """Import a file the way the launcher runs it: by path, as a standalone script.

    ``tools/`` and ``container/`` hold scripts with dashes and with no package, so
    ``import`` cannot reach them and every suite was re-implementing the loader.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the module has to be
    # registered before it is executed.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def add_tools_to_path() -> None:
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


#: What a fallback scan treats as not-source: version control, the launcher's generated state,
#: byte-compiled output, and installed dependencies. Only used when ``git`` cannot list the index.
UNSCANNED_DIRECTORY_NAMES = frozenset(
    {".git", ".local", ".ruff_cache", ".serena", "__pycache__", "node_modules"}
)


def tracked_files(*suffixes: str) -> list[Path]:
    """The repository's own files, optionally filtered by suffix such as ``".json"``.

    Several tests assert that no source file says something. Written as a walk of the checkout,
    that becomes a scan of everything the checkout happens to hold — the dependency tree the agent
    image installs, staged launcher state, an earlier run's scratch — which is both slower than the
    repository and a different question than the one being asked. The index is what "tracked" means.

    Falls back to a walk that prunes :data:`UNSCANNED_DIRECTORY_NAMES` when ``git`` declines to
    answer, since a checkout owned by a different user than the one running the suite is refused by
    default. The fallback is a superset, so it can only ever be stricter, never laxer.
    """
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if listed.returncode == 0:
        names = [name for name in map(os.fsdecode, listed.stdout.split(b"\0")) if name]
        paths = [ROOT / name for name in names]
    else:
        paths = []
        for directory, subdirectories, files in os.walk(ROOT):
            subdirectories[:] = [
                name for name in subdirectories if name not in UNSCANNED_DIRECTORY_NAMES
            ]
            paths.extend(Path(directory) / name for name in files)
    if suffixes:
        paths = [path for path in paths if path.suffix in suffixes]
    return sorted(path for path in paths if path.is_file())


def reserved_context_size() -> int:
    """The live reservation from the rendered Kimi configuration, never a copy of it."""
    with (ROOT / "runtime" / "config.toml").open("rb") as source:
        return tomllib.load(source)["loop_control"]["reserved_context_size"]


def shipped_plan() -> dict:
    """Resolve the checked-in definitions exactly as ``./start.sh`` does.

    Seven suites needed this and each had its own copy, which meant a change to the resolution
    contract had seven places to be wrong in. Providers are shallow-copied because the launcher
    layers ``.env`` overrides onto them and a test must not read operator state - nor write into
    a dictionary :func:`definitions.load_definitions` may hand out again.

    The selection is the first defined model for both lanes, which is the one configuration this
    repository ships with full definitions for and the case every policy number is stated against.
    """
    add_tools_to_path()
    import definitions
    import policy

    providers, models = definitions.load_definitions(ROOT)
    return policy.resolve(
        {pid: dict(provider) for pid, provider in providers.items()},
        {model["id"]: model for model in models},
        {"primary": models[0]["id"], "subagent": models[0]["id"]},
        reserved_context_size=reserved_context_size(),
    )


def lane_alias(plan: dict, lane: str) -> str:
    """A lane's alias, or the lane a normally-run audience sits on.

    Derived rather than typed so that renaming a model moves every expectation with it. A test
    that hard-codes ``"qwen3-primary"`` keeps passing after a rename by silently falling back to
    the lane default, which is exactly the assertion it was meant to be testing against.
    """
    return str(plan["lanes"][AUDIENCE_LANE.get(lane, lane)]["alias"])


def measured_record(
    plan: dict,
    audience: str,
    tokens: int,
    framing: int,
    harness: int,
    project: int,
    *,
    lane: str | None = None,
) -> dict:
    """A row shaped like :func:`prompt_measure.size_of` output.

    ``lane`` overrides the audience's usual lane; pass it to price a main-audience row against
    the long lane, which is the one case where the two legitimately differ. The timestamp is
    fixed, which keeps the *input* stable; an age is still measured against a moving clock, so a
    test that asserts age text has to pass its own ``now`` down.
    """
    return {
        "audience": audience,
        "tokens": tokens,
        "kimiFraming": framing,
        "harnessContract": harness,
        "projectInstructions": project,
        "modelAlias": lane_alias(plan, lane or AUDIENCE_LANE[audience]),
        "profileName": "agent" if audience == "main" else "explore",
        "time": 1_700_000_000_000,
    }


# --------------------------------------------------------------------------------------------
# terminal harness
# --------------------------------------------------------------------------------------------

#: Every shape of escape sequence a terminal program may emit: OSC (window title), DCS (the DECRQSS
#: capability query), CSI (colour, cursor addressing, the alternate screen) and the two-byte forms.
_ESCAPE = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[PX^_][^\x1b]*\x1b\\"
    rb"|\x1b[\[\]()#][0-?]*[ -/]*[@-~]"
    rb"|\x1b[@-Z\\-_]"
)


def plain(data: bytes) -> str:
    """What a user would have *seen*, with the control sequences taken back out.

    A fullscreen interface writes colour into the middle of a label — the focus wash, a dimmed
    segment — so matching raw bytes against ``[ ] Beta`` is a test that happens to pass only while
    the colour roles keep that row one run of one attribute. Assertions belong on the text.
    """
    return re.sub(_ESCAPE, b"", data).decode("utf-8", "replace")


@dataclass(frozen=True)
class PtyRun:
    """One completed pty interaction.

    ``output`` is the raw byte stream, kept because the *bytes* are the contract for the
    sequences we promised to send: entering and leaving the alternate screen, hiding and showing
    the cursor. ``screen`` is the same traffic stripped to text, which is what an assertion about
    what the user saw should compare against.
    """

    output: bytes
    screen: str
    status: int | None
    #: Whether the terminal driver came back the way it was found. A program that leaves the tty
    #: in cbreak mode breaks the user's shell, and it is the one side effect a test can only
    #: observe from outside the process.
    restored: bool


#: Process groups this module has handed a terminal to and not yet finished cleaning up.
_SPAWNED_GROUPS: set[int] = set()

#: Groups still alive a grace period after :func:`run_in_pty` killed them. Only a process that
#: refuses to die puts an entry here, which is why a test reads this instead of a raise: the
#: ``finally`` doing the recording may be unwinding somebody else's failure, and a leak reported by
#: masking the assertion that was already in flight is a leak nobody sees.
LEAKED_GROUPS: set[int] = set()


def _kill_group(pgid: int) -> None:
    """``SIGKILL`` every process in ``pgid``, tolerating a group that has already emptied."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    """Whether any process still belongs to ``pgid``.

    Signal ``0`` asks the question without answering it. A zombie counts as alive, so a caller that
    spawned the process itself has to reap it before this can report an empty group.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_gone(pgid: int, *, grace: float = 2.0) -> bool:
    """Wait out ``grace`` for ``pgid`` to empty, and report whether it did."""
    deadline = time.monotonic() + grace
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def reap_spawned_groups() -> None:
    """Kill any group a test did not finish with, as the suite leaves.

    :func:`run_in_pty` cleans up in a ``finally``, which buys nothing when the suite dies of a
    signal that cannot be caught — the one way a pty child used to escape holding a terminal whose
    master was already gone. This covers ordinary exits and unhandled exceptions; it cannot cover
    ``SIGKILL``, which is why the terminal itself has to notice, in :mod:`tui.term`.
    """
    for pgid in sorted(_SPAWNED_GROUPS):
        _kill_group(pgid)
    _SPAWNED_GROUPS.clear()


atexit.register(reap_spawned_groups)


def run_in_pty(
    argv: list[str],
    *,
    keys: bytes | list[bytes] = b"",
    expect: bytes = b"",
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    rows: int = 24,
    columns: int = 80,
    timeout: float = 12.0,
    settle: float = 0.05,
) -> PtyRun:
    """Run ``argv`` attached to a real terminal, feed it ``keys``, and report what happened.

    This is the only way to test the modal engine: ``isatty``, ``tcgetattr``, the alternate
    screen and a delivered Ctrl-C all behave differently against a pipe, and a pipe is what every
    other suite in this repository uses.

    Waits for ``expect`` to appear in the stripped output before writing ``keys``, so a test drives
    the interface rather than racing it. A needle that never appears is an error rather than a head
    start: a test that types into a screen that was never drawn proves only that the bytes were
    accepted, which is how a launcher step that had silently stopped asking could still satisfy the
    test written to notice.

    ``keys`` is either one burst or a list of chunks. A fullscreen loop paints once per batch of
    keystrokes it reads, so a whole script written at once is applied between two frames and every
    frame the user would have seen in between is lost to the test — which is precisely where a
    masked field, a legend that changes meaning, or a scrolled viewport lives. Chunked keys are
    handed over with the stream quiet in between, so each chunk's repaint is observable.
    """
    master, slave = pty.openpty()
    process: subprocess.Popen | None = None
    pgid: int | None = None
    try:
        _set_winsize(slave, rows, columns)
        before = termios.tcgetattr(slave)
        spawn = dict(env or os.environ)
        spawn.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=spawn,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            # Its own process group, so teardown has something to kill that covers the grandchildren
            # too. A launcher step driven through ``bash -c`` is that shape: killing the shell alone
            # leaves the screen it spawned running, on a terminal the shell had open.
            start_new_session=True,
        )
        # The slave stays open in this process for the whole run. Closing it would let the master
        # report ``EIO`` the moment the child finished, which is a tidy way to learn the child is
        # gone — and also the only descriptor the *line discipline settings* can still be read from
        # afterwards, which is half of what this helper is for. The exit status is polled instead.
        pgid = os.getpgid(process.pid)
        _SPAWNED_GROUPS.add(pgid)
        output = bytearray()
        deadline = time.monotonic() + timeout
        if expect:
            needle = expect if isinstance(expect, bytes) else str(expect).encode()
            # The needle is re-checked after every read rather than before the next one, because the
            # read that delivers it is also often the read that learns the writer is gone. Ordering
            # those two facts the other way round fails a run that said exactly what was asked.
            while needle not in plain(bytes(output)).encode():
                if not _await(master, output, deadline):
                    if needle in plain(bytes(output)).encode():
                        break
                    raise AssertionError(
                        f"{needle.decode(errors='replace')!r} never appeared on the terminal; "
                        f"the run said: {plain(bytes(output))[-400:]!r}"
                    )
        chunks = [keys] if isinstance(keys, (bytes, bytearray)) else list(keys)
        for chunk in chunks:
            if not chunk:
                continue
            os.write(master, chunk)
            if len(chunks) > 1:
                _settle(master, output, process, deadline, settle)
        while time.monotonic() < deadline and process.poll() is None:
            _pump(master, output, process, deadline)
        after = termios.tcgetattr(slave)
        # macOS sets PENDIN while applying attributes; it is transient driver state, not a setting
        # the program chose to leave behind.
        transient = getattr(termios, "PENDIN", 0)
        after[3] &= ~transient
        before[3] &= ~transient
        # A last line written microseconds before exiting sits in the driver, not in the process,
        # so keep reading until the stream is quiet rather than stopping at the exit status.
        quiet = time.monotonic() + 0.3
        while time.monotonic() < quiet:
            _pump(master, output, process, quiet)
        return PtyRun(
            bytes(output), plain(bytes(output)), process.poll(), list(after) == list(before)
        )
    finally:
        # Teardown order is the substance of this block. Kill the group while its leader is still
        # ours to reap, so the id cannot have been recycled underneath the signal; then close the
        # terminal; then check the group emptied. The group rather than the child, because the child
        # is often a shell and the screen is its grandchild — killing only the direct child is how a
        # launcher step used to be left running on a pty with no master.
        if process is not None:
            if pgid is not None:
                _kill_group(pgid)
            elif process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - nothing survives the group kill
                if pgid is not None:
                    LEAKED_GROUPS.add(pgid)
        for fd in (master, slave):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if pgid is not None:
            _SPAWNED_GROUPS.discard(pgid)
            if not _group_gone(pgid):
                LEAKED_GROUPS.add(pgid)


def _set_winsize(fd: int, rows: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", rows, columns, 0, 0))


def _await(master: int, output: bytearray, deadline: float) -> bool:
    """Read one exchange from the terminal, reporting whether anything may still follow.

    A pty has writers rather than one writer: the process that was spawned may be a shell that
    hands the screen to its own child and leaves, and the master keeps delivering that child's
    output until the last descriptor on the slave side closes. Watching the direct child's liveness
    here is what made the fixture stop reading microseconds before the line it was waiting for, so
    the only two endings this recognises are the end of the stream — ``EIO``, which is permanent —
    and the deadline.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        ready = select.select([master], [], [], min(0.1, remaining))[0]
        if ready:
            output += os.read(master, 65536)
    except OSError:
        return False
    return time.monotonic() < deadline


def _pump(master: int, output: bytearray, process: subprocess.Popen, deadline: float) -> bool:
    """Read whatever the child has written, returning whether the exchange may continue.

    ``EIO`` is normal here rather than a failure: a pty master reports it the moment the last
    descriptor on the slave side closes, which is how a finished program tells us it is done.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        ready = select.select([master], [], [], min(0.1, remaining))[0]
        if ready:
            output += os.read(master, 65536)
    except OSError:
        return process.poll() is None
    return process.poll() is None


def _settle(
    master: int, output: bytearray, process: subprocess.Popen, deadline: float, quiet: float
) -> None:
    """Wait until the child has stopped writing, so the next keystroke meets a settled screen.

    Quiet rather than a sleep: a modal loop also repaints on a timer, so a fixed pause either races
    the painter or burns a second per key. Nothing new for ``quiet`` seconds means the frame that
    this keystroke caused is already in the buffer.
    """
    since = time.monotonic()
    size = len(output)
    while time.monotonic() < deadline:
        if len(output) != size:
            size = len(output)
            since = time.monotonic()
        elif time.monotonic() - since >= quiet:
            return
        if not _pump(master, output, process, deadline):
            return
