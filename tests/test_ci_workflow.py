"""The CI workflow is a shipped artifact, so it gets the same static checks as code.

Two failures this exists for, both from the commit that added the ``local-mac`` job: a step used
``when: always()`` where GitHub Actions requires ``if:``, and that job pinned ``actions/checkout``
to a SHA that does not exist upstream. Neither is reachable from any Python test, and the invalid
key is worse than it looks — GitHub validates the whole file, so one bad step key can take every
job down rather than just the new one.
"""

import ast
import json
import os
import re
import sys
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

    def test_ubuntu_runners_name_a_version(self):
        # A floating label moves on a schedule GitHub owns, which would make a green run stop
        # meaning "the image under test passed". Lists are allowed for matrix or self-hosted label
        # sets; only a value that claims to be Ubuntu has to say which Ubuntu it is.
        pinned = re.compile(r"^ubuntu-\d{2}\.\d{2}$")
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in jobs(document).items():
                runs_on = (job or {}).get("runs-on")
                for image in runs_on if isinstance(runs_on, list) else [runs_on]:
                    if not isinstance(image, str) or not image.startswith("ubuntu"):
                        continue
                    with self.subTest(workflow=path.name, job=job_id, image=image):
                        self.assertRegex(
                            image, pinned, f"{job_id} runs on the floating label {image}"
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


#: Directories whose contents are never import candidates: build output, dependencies, and the
#: scratch a running session leaves beside the checkout.
SKIP_DIRS = frozenset({".git", ".local", ".venv", "node_modules", "__pycache__", ".ruff_cache"})

#: Distribution names that differ from the module they install. Everything else is covered by PEP
#: 503 normalisation (`aiohttp` installs `aiohttp`), and an import that matches neither the table
#: nor the job's install step is exactly what this reports.
DISTRIBUTION_ALIASES = {"yaml": "pyyaml"}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def repo_module_names() -> set:
    """Every module and package name this checkout itself provides.

    The suites reach these through ``sys.path`` entries rather than imports of the repo, so
    ``tools/local_engines/mlx_worker.py`` arrives as a bare ``import mlx_worker`` and is no more
    third-party than ``import yaml`` is not.
    """
    names = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            if name != "__init__.py":
                names.add(name[:-3])
            # Every directory above a module is itself importable: `tests/helpers.py` is reached as
            # both `import helpers` and `from tests.helpers import ...`, and the suites use each.
            names.update(
                part for part in Path(base).relative_to(ROOT).parts if not part.startswith(".")
            )
    return names


def hard_imports(path: Path) -> set:
    """Module-scope imports that run before any skip can take effect.

    Only a bare ``import`` statement at the top level qualifies. An import inside a ``try`` is a
    suite saying "this is optional here" - which is how ``test_mcp_probe`` and the MLX tests survive
    a runner without the SDK or the wheels - and one inside a module-level ``if`` is gated on
    something the file already tests, the same way an import in a function body waits for a test
    that may never run. Missing a gated import costs nothing here; demanding an install for one
    would be a false failure, which is the only way this check can make CI worse.
    """
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            continue
        for alias in node.names:
            dotted = node.module if isinstance(node, ast.ImportFrom) else alias.name
            found.add(dotted.split(".")[0])
    return found


def suite_sources() -> list:
    roots = [ROOT / "tests", *sorted((ROOT / "modules").glob("*/tests"))]
    return [path for root in roots if root.is_dir() for path in sorted(root.glob("*.py"))]


class TestDependencyCoverage(unittest.TestCase):
    """A test file cannot import what its job never installed.

    ``unittest discover`` turns that into an import error for the file, which is an error for the
    step, which is a red job whose message names neither the missing package nor the workflow line
    that should install it: ``tests/test_ci_workflow.py``'s own ``import yaml`` kept ``static``
    failing for four runs, and the file that exists to catch a broken workflow was the thing broken.
    """

    def installed(self, job: dict) -> set:
        """Requirements the job's ``pip install`` steps make available, `-r` files resolved."""
        names: set[str] = set()
        for step in job.get("steps") or []:
            script = step.get("run")
            if not script or "pip install" not in script:
                continue
            for line in script.splitlines():
                line = line.strip()
                if not line.startswith("pip install"):
                    continue
                for token in line.split()[2:]:
                    if token.startswith("-r"):
                        continue
                    if token.endswith(".in") or token.endswith(".txt"):
                        requirement = ROOT / token
                        text = (
                            requirement.read_text(encoding="utf-8")
                            if requirement.is_file()
                            else ""
                        )
                        for pinned in text.splitlines():
                            head = re.match(r"([A-Za-z0-9._-]+)\s*==", pinned.strip())
                            if head:
                                names.add(canonical(head.group(1)))
                        continue
                    head = re.match(r"([A-Za-z0-9._-]+)", token)
                    if head:
                        names.add(canonical(head.group(1)))
        return names

    def test_every_hard_test_import_is_installed_by_the_job_that_runs_it(self):
        local = repo_module_names()
        wanted: dict[str, set] = {}
        for path in suite_sources():
            for name in hard_imports(path):
                if name in sys.stdlib_module_names or name in local:
                    continue
                wanted.setdefault(canonical(DISTRIBUTION_ALIASES.get(name, name)), set()).add(
                    path.relative_to(ROOT).as_posix()
                )
        self.assertTrue(wanted, "no third-party import found to check; the suites changed shape")
        for path in WORKFLOWS:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in jobs(document).items():
                script = json.dumps(job or {})
                if "unittest discover" not in script:
                    continue
                available = self.installed(job or {})
                for distribution, sources in sorted(wanted.items()):
                    with self.subTest(workflow=path.name, job=job_id, distribution=distribution):
                        self.assertIn(
                            distribution,
                            available,
                            f"{job_id} runs {sorted(sources)} which imports {distribution!r}, but "
                            f"its pip steps install only {sorted(available)}",
                        )


if __name__ == "__main__":
    unittest.main()
