#!/usr/bin/env python3
"""Regenerate the NRP-only plan and guidance references the local work is checked against.

``tests/test_local_inference_references.py`` fails when a byte of an NRP-only plan moves. That is
the point of it, and it is also why the fix must never be to delete or silently refresh a fixture:
regenerating here is a deliberate act whose diff is the review. Run it, read the diff, and commit
the two together.

Everything is resolved from the checked-in definitions with synthetic credentials, so the output is
a property of the repository and not of any operator's machine, key, or running instance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tools"), str(ROOT / "proxy")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import policy  # noqa: E402

from tests.helpers import shipped_plan  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "local-inference"


def capture(lane: str) -> dict:
    """One complete NRP-only plan plus every generated guidance block for it."""
    plan = json.loads(json.dumps(shipped_plan(agent_lane=lane)))
    if plan.get("engines"):
        raise SystemExit(
            f"the {lane} reference selection resolved engines; it must be NRP-only or the "
            "reference stops guarding NRP behaviour"
        )
    return {
        "plan": plan,
        "guidance": {
            name: policy.render_guidance(plan, name) for name in policy.GUIDANCE_AUDIENCES
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing; the mode a CI job would use",
    )
    args = parser.parse_args(argv)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    drifted: list[str] = []
    for lane in policy.AGENT_LANES:
        path = FIXTURES / f"nrp-only-{lane}.json"
        rendered = json.dumps(capture(lane), indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
                drifted.append(path.name)
            continue
        path.write_text(rendered)
        print(f"wrote {path.relative_to(ROOT)}")
    if drifted:
        print(
            "NRP references drifted from the checked-in definitions: "
            + ", ".join(sorted(drifted))
            + "\nRe-run tools/capture_local_inference_references.py and review the diff.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
