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

if __package__:
    from . import policy
else:  # run as a loose script, with tools/ on sys.path
    import policy  # noqa: E402

from tests.helpers import shipped_plan  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "local-inference"

#: The host a captured plan's endpoint URLs are rewritten to. ``.invalid`` is reserved by RFC 2606
#: and can never resolve, so a fixture cannot accidentally become a working endpoint.
REDACTED_HOST = "gateway.invalid"


def redact_host(value, host: str | None = None):
    """Replace the *gateway* host in any URL with a reserved one, recursing through the document.

    Which product sits behind the endpoint is an operator's choice and the harness must not assume
    it — ``tests/test_configuration.py`` enforces that the real host appears in exactly one tracked
    file, the provider manifest. A captured plan is a *second* copy of that host and would make the
    fixture a place a gateway name can silently accumulate.

    ``host`` names the only host that is replaced, so a URL pointing somewhere else — the provider's
    policy terms live on a different one — keeps its real value. Scheme and path are preserved even
    on the redacted URL, because they are part of the resolved contract and a change to either must
    still move the fixture.
    """
    if isinstance(value, str):
        if "://" not in value:
            return value
        scheme, _, rest = value.partition("://")
        found, slash, tail = rest.partition("/")
        found = found.split(":", 1)[0]
        if host is None or found != host:
            return value
        port = rest.partition("/")[0].partition(":")[2]
        return f"{scheme}://{REDACTED_HOST}{':' + port if port else ''}{slash + tail}"
    if isinstance(value, dict):
        return {key: redact_host(item, host) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_host(item, host) for item in value]
    return value


def gateway_host(plan: dict) -> str:
    """The host the resolved plan points its endpoint at, without scheme or path."""
    return plan["providers"]["nrp"]["base_url"].split("://", 1)[-1].split("/")[0]


def redact_plan(plan: dict) -> dict:
    """Redact the plan with its own gateway host, so writer and reader agree by construction."""
    return redact_host(plan, gateway_host(plan))


def capture(lane: str) -> dict:
    """One complete NRP-only plan plus every generated guidance block for it."""
    plan = json.loads(json.dumps(shipped_plan(agent_lane=lane)))
    if plan.get("engines"):
        raise SystemExit(
            f"the {lane} reference selection resolved engines; it must be NRP-only or the "
            "reference stops guarding NRP behaviour"
        )
    guidance = {name: policy.render_guidance(plan, name) for name in policy.GUIDANCE_AUDIENCES}
    host = gateway_host(plan)
    for name, text in guidance.items():
        if host in text:
            raise SystemExit(
                f"the {name} guidance renders the provider host {host!r}, so a captured "
                "fixture could not redact it without also breaking the guidance comparison"
            )
    return {"plan": redact_plan(plan), "guidance": guidance}


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
        path.write_text(rendered, encoding="utf-8")
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
