"""The CI workflow is a shipped artifact, so it gets the same static checks as code.

Two failures this exists for, both from the commit that added the ``local-mac`` job: a step used
``when: always()`` where GitHub Actions requires ``if:``, and that job pinned ``actions/checkout``
to a SHA that does not exist upstream. Neither is reachable from any Python test, and the invalid
key is worse than it looks — GitHub validates the whole file, so one bad step key can take every
job down rather than just the new one.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))

#: Keys GitHub Actions accepts on a step. Anything else is silently ignored by some readers and a
#: hard file-level validation error in the workflow parser.
STEP_KEYS = frozenset(
    {
        "id",
        "if",
        "uses",
        "name",
        "run",
        "env",
        "defaults",
        "continue-on-error",
        "timeout-minutes",
        "working-directory",
        "shell",
        "with",
        "container",
    }
)

JOB_KEYS = frozenset(
    {
        "needs",
        "runs-on",
        "environment",
        "defaults",
        "if",
        "env",
        "concurrency",
        "outputs",
        "timeout-minutes",
        "steps",
        "services",
        "container",
        "strategy",
        "uses",
        "with",
        "permissions",
        "secrets",
        "job_container",
    }
)

WORKFLOW_KEYS = frozenset(
    {"name", "on", "permissions", "env", "defaults", "concurrency", "jobs", "run-name", "types"}
)

PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})")


def jobs(document: dict) -> dict:
    return document.get("jobs") or {}


def workflow_keys(document: dict) -> set:
    """Top-level keys, with YAML's truthiness bug undone.

    YAML 1.1 resolves a bare ``on:`` to the boolean ``True``, so the trigger block arrives as a key
    that no schema names. Normalising it here keeps the check honest without teaching it to ignore
    genuinely unknown keys.
    """
    return {"on" if key is True else key for key in document}


class WorkflowSchemaTests(unittest.TestCase):
    def test_workflows_are_present_and_parseable(self):
        self.assertTrue(WORKFLOWS, "no workflow files found to validate")
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(document, dict)

    def test_no_unsupported_keys_at_any_level(self):
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            with self.subTest(workflow=path.name):
                self.assertFalse(
                    workflow_keys(document) - WORKFLOW_KEYS,
                    f"{path.name}: unsupported workflow keys "
                    f"{sorted(workflow_keys(document) - WORKFLOW_KEYS)}",
                )
                for job_id, job in jobs(document).items():
                    unknown = set(job or {}) - JOB_KEYS
                    self.assertFalse(
                        unknown, f"{path.name}:{job_id}: unsupported job keys {sorted(unknown)}"
                    )
                    for index, step in enumerate((job or {}).get("steps") or []):
                        stray = set(step or {}) - STEP_KEYS
                        self.assertFalse(
                            stray,
                            f"{path.name}:{job_id} step {index}: unsupported step keys "
                            f"{sorted(stray)} — a step condition is 'if', not 'when'",
                        )

    def test_step_conditions_use_the_supported_key(self):
        # Named separately because 'when' is the specific mistake: it is a valid key in GitLab CI
        # and in compose files, so it reads as plausible to anyone who has worked in either.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith("when:"),
                    f"{path.name}:{line_number}: 'when:' is not a GitHub Actions key; use 'if:'",
                )

    def test_action_pins_are_full_commit_shas(self):
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in jobs(document).items():
                for index, step in enumerate((job or {}).get("steps") or []):
                    uses = (step or {}).get("uses")
                    if not uses or uses.startswith("./"):
                        continue
                    if "@" not in uses:
                        # An unpinned action floats with the branch, which no other job here does.
                        self.fail(f"{path.name}:{job_id} step {index}: unpinned action {uses}")
                    match = PIN_RE.match(uses)
                    self.assertIsNotNone(
                        match, f"{path.name}:{job_id} step {index}: {uses} is not pinned to a SHA"
                    )


class SharedActionPinTests(unittest.TestCase):
    """One action, one pin, repository-wide.

    A SHA is not a version label, so this cannot prove a pin resolves — that needs the network. What
    it does prove hermetically is that no job quietly points somewhere the others do not, which is
    exactly how a fabricated ``actions/checkout`` pin survived review while six other jobs in the
    same file carried the real one.
    """

    def test_each_action_uses_one_pin_everywhere(self):
        wanted: dict[str, set[str]] = {}
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in jobs(document).values():
                for step in (job or {}).get("steps") or []:
                    uses = (step or {}).get("uses")
                    if not uses or uses.startswith("./") or "@" not in uses:
                        continue
                    action, _, pin = uses.partition("@")
                    wanted.setdefault(action, set()).add(pin)
        shared = {
            action: sorted(pins) for action, pins in wanted.items() if len(pins) > 1
        }
        self.assertFalse(
            shared,
            "an action is pinned to more than one SHA across the workflows: "
            + "; ".join(f"{a} -> {p}" for a, p in sorted(shared.items())),
        )

    def test_the_local_mac_job_uses_the_same_checkout_pin_as_everything_else(self):
        pins: dict[str, set[str]] = {}
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in jobs(document).items():
                for step in (job or {}).get("steps") or []:
                    uses = (step or {}).get("uses") or ""
                    if uses.startswith("actions/checkout@"):
                        pins.setdefault(job_id, set()).add(uses.split("@", 1)[1])
        self.assertIn("local-mac", pins, "the native job disappeared from CI")
        everywhere = {pin for group in pins.values() for pin in group}
        self.assertEqual(
            len(everywhere), 1, f"jobs disagree on the checkout pin: {sorted(pins.items())}"
        )


if __name__ == "__main__":
    unittest.main()
