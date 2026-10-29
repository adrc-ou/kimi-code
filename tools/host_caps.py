#!/usr/bin/env python3
"""Measure what this machine can serve, once per launch.

A provider that runs on the operator's hardware has to know something the cloud ones never ask
about: whether the machine underneath the launcher has that hardware at all. This module answers
that with three facts - the platform, the memory, and the free disk - and everything downstream
either trusts the answer or shows nothing.

The rule at every layer is the same: **a capability that cannot be measured is treated as
absent**. A failed ``sysconf``, an unstatable directory, or a platform this file has never heard
of all end the same way, because the alternative is offering a 111 GB download to a machine that
cannot run it. The failure the harness must never produce is false hope.

Nothing here is cached across launches, and nothing needs to be: the whole probe is three system
calls and a string comparison, and a Mac that changes its memory or architecture has bigger
problems than a stale ``host-caps.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

#: Where the launcher keeps the probe, so the log and the picker read one answer rather than
#: measuring twice and hoping. Optional: :func:`caps` re-probes when it is missing.
CAPS_FILE = "host-caps.json"

GIB = 1024**3


def host_capabilities(system: str, machine: str) -> list[str]:
    """Which accelerator frameworks this host can offer its own processes.

    ``metal`` is Apple's GPU API, and on Apple silicon the GPU addresses the same memory the CPU
    does - which is the whole reason a 27 GB weight set can live on a laptop. The test is
    deliberately the same one :mod:`modules.comfyui` uses (``Darwin`` + ``arm64``) rather than a
    probe of its own: every Mac that runs this harness is arm64 since 2020, and an Intel Mac
    cannot serve these models in any case, so a check for a Metal device would only add a way to
    be wrong.

    ``cuda`` is named here so a provider can require it before the probe exists. Unknown
    capabilities are never returned, which is what keeps an unmeasured one absent.
    """
    capabilities = set()
    if system == "Darwin" and machine == "arm64":
        capabilities.add("metal")
    return sorted(capabilities)


def total_memory_bytes() -> int:
    """Installed physical bytes. Zero when the system will not say."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0
    if not isinstance(pages, int) or not isinstance(size, int) or pages <= 0 or size <= 0:
        return 0
    return int(pages) * int(size)


def total_memory_gib() -> int:
    """Installed memory, rounded down to whole GiB. Zero when the system will not say."""
    return total_memory_bytes() // GIB


def available_memory_bytes(system: str | None = None) -> int:
    """Reclaimable physical memory now, never swap or a promise about future availability.

    Linux publishes MemAvailable. On macOS free and inactive pages are a deliberately
    conservative estimate: compressed and purgeable pages are not added a second time.
    Unknown is zero and makes local capacity planning refuse rather than guess.
    """
    system = system or platform.system()
    if system == "Linux":
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return 0
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", text, re.MULTILINE)
        return int(match[1]) * 1024 if match else 0
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True, check=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        page = re.search(r"page size of (\d+) bytes", result.stdout)
        free = re.search(r"^Pages free:\s+(\d+)\.", result.stdout, re.MULTILINE)
        inactive = re.search(r"^Pages inactive:\s+(\d+)\.", result.stdout, re.MULTILINE)
        if not all((page, free, inactive)):
            return 0
        assert page is not None and free is not None and inactive is not None
        return int(page[1]) * (int(free[1]) + int(inactive[1]))
    return 0


def process_memory_bytes(pid: int) -> int:
    """Resident bytes for an owned child; zero when the OS cannot report them."""
    if pid <= 0:
        return 0
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return int(result.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def free_disk_gib(path: Path) -> int:
    """Free space under ``path``, which is where a download would actually land.

    Measured against the harness root and not the workspace: weights are written beside the
    definition that pins them, and that is a different filesystem on every machine that does not
    keep this checkout inside its own working tree.
    """
    try:
        return int(shutil.disk_usage(path).free) // GIB
    except OSError:
        return 0


def _count(value: Any, *, positive: bool = False) -> int | None:
    """A reported integer, or ``None``. Rejects the bool-that-pretends-to-be-an-int case."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return None if positive and value <= 0 else value


def metal_observations() -> dict[str, Any]:
    """Device-level Metal facts, as distinct measurements rather than one usable total.

    Read on the host by the launcher, never by the proxy image, and never as an admission test: a
    recommended working set is Apple's *performance* guidance, not a physical ceiling, and treating
    it as one would reintroduce the RAM-derived context sizing this harness removed on purpose. The
    value is that it lets a residency target be explained instead of invented.

    Every field is ``None`` when the host will not say. Unknown is not zero: zero bytes of available
    memory would be a measurement, and a wire that reports residency disabled is a different fact
    from one that reports residency of zero bytes.
    """
    unknown: dict[str, Any] = {
        "device_count": None,
        "recommended_working_set_bytes": None,
        "presentation_model": None,
        "low_power_mode": None,
        "source": None,
    }
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return {**unknown, "source": "not apple silicon"}
    # There is no stable syscall for this. ``system_profiler`` reports the SP_DisplayData card
    # fields, and ``sysctl`` knows nothing about a device working set, so the observation is taken
    # from a one-shot helper the engine runtime already has bindings for. Absent that helper - which
    # is the case on every Linux test host and on a Mac without the provisioned MLX runtime - the
    # answer is unknown, and the caller disables deliberate wiring rather than guessing.
    helper = os.environ.get("LOCAL_MPS_METAL_PROBE", "").strip()
    if not helper or not Path(helper).is_file():
        return {**unknown, "source": "no metal probe available"}
    try:
        result = subprocess.run(
            [sys.executable, helper],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        document = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**unknown, "source": "probe failed"}
    if not isinstance(document, dict):
        return {**unknown, "source": "probe malformed"}
    working_set = document.get("recommended_max_working_set_size")
    count = document.get("device_count")
    return {
        "device_count": _count(count),
        "recommended_working_set_bytes": _count(working_set, positive=True),
        "presentation_model": document.get("presentation_model"),
        "low_power_mode": document.get("low_power_mode"),
        "source": "probe",
    }


def swap_activity() -> dict[str, Any]:
    """Cumulative compressor and swap counters, as deltas waiting to be taken.

    Split out from :func:`metal_observations` because they answer a different question and change at
    a different rate: a working set is a property of the device, these are a property of the moment.
    High swap occupancy alone does not establish thrashing - only the movement between two samples
    does, which is why this returns counters and no verdict.
    """
    blank = {"swap_used_bytes": None, "swap_capacity_bytes": None, "compressed_bytes": None}
    if platform.system() != "Darwin":
        return {**blank, "source": "not darwin"}
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage", "vm.page_free_count"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {**blank, "source": "sysctl unavailable"}
    lines = result.stdout.strip().splitlines()
    values = dict(blank)
    if lines:
        # ``total = 1024.00M used = 512.00M free = 512.00M`` is the documented shape; a parser that
        # cannot read it says unknown rather than guessing a scale factor.
        used = re.search(r"used\s*=\s*([\d.]+)\s*([KMG])", lines[0])
        total = re.search(r"total\s*=\s*([\d.]+)\s*([KMG])", lines[0])
        scale = {"K": 1024, "M": 1024**2, "G": 1024**3}
        if used:
            values["swap_used_bytes"] = int(float(used[1]) * scale[used[2]])
        if total:
            values["swap_capacity_bytes"] = int(float(total[1]) * scale[total[2]])
    return {**values, "source": "sysctl"}


def probe(workspace: Path | None = None) -> dict[str, Any]:
    """One measurement of the machine running this process.

    ``workspace`` is recorded for the launch log and never consulted for eligibility: free space
    is measured against the harness root, because that is where :mod:`local_llm` writes weights,
    and a project directory can be on any volume at all.
    """
    system = platform.system()
    machine = platform.machine()
    # Distinct keys, deliberately never summed. Host memory, available memory, GPU allocations,
    # process RSS and swap are different quantities that overlap on unified memory, and adding any
    # two of them produces a number that describes nothing.
    return {
        "schema_version": 2,
        "system": system,
        "arch": machine,
        "memory_gib": total_memory_gib(),
        "memory_bytes": total_memory_bytes(),
        "available_memory_bytes": available_memory_bytes(system),
        "free_disk_gib": free_disk_gib(ROOT),
        "capabilities": host_capabilities(system, machine),
        "workspace": str(ROOT if workspace is None else Path(workspace)),
        "metal": metal_observations(),
        "swap": swap_activity(),
    }


def read(runtime_dir: Path | None) -> dict[str, Any] | None:
    """The launcher's cached probe, if it is there and shaped right."""
    if runtime_dir is None:
        return None
    path = Path(runtime_dir) / CAPS_FILE
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") not in (1, 2):
        return None
    if not isinstance(document.get("capabilities"), list):
        return None
    return document


def caps(runtime_dir: Path | None = None, workspace: Path | None = None) -> dict[str, Any]:
    """The cached probe when the launcher wrote one, otherwise a fresh measurement.

    Reading the file when it exists is what makes the picker, the resolver and the launch log
    agree. Probing when it does not is what lets ``tools/models.py`` be run on its own, which is
    how every test in this repository exercises it.
    """
    cached = read(runtime_dir)
    if cached is not None:
        return cached
    return probe(workspace)


def provider_supported(provider: dict[str, Any], measured: dict[str, Any]) -> bool:
    """Whether the host offers every capability a provider requires.

    A provider that asks for nothing is supported everywhere, which is the case for every cloud
    endpoint and the reason this returns True for a definition predating the key entirely.
    """
    required = set(provider.get("requires") or ())
    return required <= set(measured.get("capabilities") or ())


def model_supported(model: dict[str, Any], measured: dict[str, Any]) -> bool:
    """Whether the host clears the floors one model declares for itself.

    A zero floor is no floor. A measured memory or disk of zero means the system would not say,
    and anything with a requirement is then refused - the same rule that hides the whole provider
    rather than risk offering it.
    """
    required = model.get("requires") or {}
    memory = int(measured.get("memory_gib") or 0)
    disk = int(measured.get("free_disk_gib") or 0)
    if int(required.get("memory_gib") or 0) > memory:
        return False
    return int(required.get("disk_gib") or 0) <= disk


def filter_definitions(
    providers: dict[str, Any], models: list[dict[str, Any]], measured: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Drop what this machine cannot serve, taking a provider's models with it.

    This is the same mechanism discovery already uses for a provider directory that is absent -
    remove it from the table and every model naming it disappears - which is deliberate: there is
    one way for a model to stop being offered, and it is enforced by the code that already
    refuses a model whose provider is missing.
    """
    supported = {
        provider_id
        for provider_id, provider in providers.items()
        if provider_supported(provider, measured)
    }
    kept = {provider_id: providers[provider_id] for provider_id in sorted(supported)}
    usable = [
        model
        for model in models
        if model["provider"] in kept and model_supported(model, measured)
    ]
    return kept, usable


def summary(measured: dict[str, Any]) -> str:
    """One line for the launch notes."""
    capabilities = ", ".join(measured.get("capabilities") or ()) or "none"
    return (
        f"Host: {measured.get('system', '?')}/{measured.get('arch', '?')} "
        f"memory={measured.get('memory_gib', 0)}GiB "
        f"free_disk={measured.get('free_disk_gib', 0)}GiB accelerators={capabilities}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "summary"])
    parser.add_argument("--runtime-dir", default="", help="write the probe here for this launch")
    parser.add_argument("workspace", nargs="?", default="")
    args = parser.parse_args(argv)
    measured = probe(Path(args.workspace) if args.workspace else None)
    if args.runtime_dir:
        directory = Path(args.runtime_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CAPS_FILE
        path.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
        path.chmod(0o600)
    print(summary(measured) if args.action == "summary" else json.dumps(measured, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
