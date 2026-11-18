"""Every Python file that reaches a container alone must import nothing it cannot find there.

The launcher assembles images and bind mounts from lists of individual files, not from a tree:
``compose.yaml`` maps ``tools/kimi_config_merge.py`` to ``/stage/`` by itself, and
``proxy/Dockerfile`` copies exactly two modules into ``/app``. A first-party import added to one
of those files resolves on the host - the test suite runs with ``tools/`` and the repository root
on ``sys.path`` - and fails inside the container, where nothing else was copied. That is a startup
failure on the operator's next launch, which is the one kind of break nothing in here can catch
after the fact.

Neither the mount list nor the import list is written down twice. The boundaries are read out of
the deployment files themselves, so adding a mounted file or a ``COPY`` adds it to the check on the
next commit - the way ``scripts/check_vulnerabilities.py`` derives the scanned images from the CI
job, and ``tests.helpers.stage_cross_tree_imports`` derives fixture copies from parsed imports.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COPY_PY = re.compile(r"^COPY\s+(?:--\S+\s+)*(?P<src>[^\s]+\.py)\s+(?P<dest>/\S+)\s*$")


def tracked(*suffixes: str) -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [Path(p) for p in listing if p.endswith(suffixes)]


def repo_module_names() -> set[str]:
    """Every importable name this repository owns: a module file or a package directory."""
    return {p.stem for p in tracked(".py")} | {p.parent.name for p in tracked("__init__.py")}


def first_party_imports(path: Path, known: set[str]) -> tuple[set[str], list[str]]:
    """Names this module reaches for that resolve to a repository file, and any relative import.

    Function bodies count. A lazy ``from private_file import write_private`` never runs at import
    time, so a probe that only executes the module cannot see it - and it is no more available
    inside the container than a top-level one would be.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    wanted: set[str] = set()
    relative: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in stdlib:
                    wanted.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.module or "<anonymous>")
            elif node.module:
                top = node.module.split(".")[0]
                if top not in stdlib:
                    wanted.add(top)
    return wanted & known, relative


def compose_mounts(document: Path) -> list[tuple[str, str]]:
    """(source, target) pairs from one compose file, in both short and long volume syntax."""
    text = document.read_text(encoding="utf-8")
    # Interpolation defaults make no difference to a path's shape, and an unset required name is a
    # launcher error rather than this test's business.
    text = re.sub(r"\$\{[^}]*\}", "PLACEHOLDER", text)
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    pairs: list[tuple[str, str]] = []
    for service in (parsed.get("services") or {}).values():
        if not isinstance(service, dict):
            continue
        for entry in service.get("volumes") or []:
            if isinstance(entry, str):
                source, _, target = entry.partition(":")
                pairs.append((source, target))
            elif isinstance(entry, dict) and entry.get("source") and entry.get("target"):
                pairs.append((str(entry["source"]), str(entry["target"])))
    return pairs


def boundaries() -> dict[tuple[str, str], set[str]]:
    """(isolated module, destination directory) -> every module name that arrives alongside it."""
    result: dict[tuple[str, str], set[str]] = {}
    documents = sorted(ROOT.rglob("compose*.yaml"))
    for document in documents:
        # A module overlay is written relative to its own directory.
        base = document.parent
        pairs = compose_mounts(document)
        by_dir: dict[str, set[str]] = {}
        for source, target in pairs:
            destination = Path(target)
            container = str(destination.parent) or "/"
            names = by_dir.setdefault(container, set())
            resolved = (base / source) if source.startswith("./") else Path(source)
            if source.startswith("./") and resolved.is_file():
                names.add(resolved.stem)
            elif resolved.is_dir():
                names |= {p.stem for p in resolved.glob("*.py")}
        for source, target in pairs:
            resolved = (base / source) if source.startswith("./") else Path(source)
            if not (source.startswith("./") and resolved.is_file()
                    and resolved.suffix == ".py"):
                continue
            container = str(Path(target).parent) or "/"
            result[(str(resolved), container)] = by_dir.get(container, set())

    for dockerfile in sorted(tracked("Dockerfile")):
        matches = [
            COPY_PY.match(line) for line in dockerfile.read_text(encoding="utf-8").splitlines()
        ]
        matches = [m for m in matches if m]
        present = {Path(m.group("src")).stem for m in matches}
        for match in matches:
            source = resolve_copy_source(dockerfile, match.group("src"))
            if source is None:
                continue
            container = str(Path(match.group("dest")).parent)
            key = (str(source), container)
            result[key] = result.get(key, set()) | present
    return result


def resolve_copy_source(dockerfile: Path, relative: str) -> Path | None:
    """Locate a COPY'd file.

    A COPY path is relative to the *build context*, and the contexts here differ: the proxy and
    search-adapter images are built with their own directory as the context, while the ComfyUI
    frontend is built with ``context: .`` from the repository root and so names its script
    ``modules/comfyui/scripts/...`` inside its own Dockerfile. Resolving against the Dockerfile's
    directory alone would invent a path that exists nowhere, and a silently missing file is worse
    than an uncovered one, so both candidates are tried before giving up.
    """
    for candidate in (
        dockerfile.parent / relative,
        ROOT / relative,
        dockerfile.parent.parent / relative,
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise AssertionError(
        f"{dockerfile}: COPY {relative} matches no file in the Dockerfile's directory or the "
        "repository root - the build would fail too, so this is a real finding, not a gap here"
    )


class IsolatedPythonModuleTests(unittest.TestCase):
    def test_boundaries_were_actually_found(self):
        # Guards the guard. If the compose or Dockerfile parsing ever matches nothing, the check
        # below would iterate over an empty mapping and pass, which is the one outcome that makes
        # this file worse than not having it.
        found = boundaries()
        self.assertGreaterEqual(len(found), 4, f"only {len(found)} isolated modules discovered")
        located = {Path(source).name for source, _ in found}
        for expected in ("kimi_config_merge.py", "model_proxy.py", "app.py"):
            self.assertIn(expected, located, f"{expected} is no longer covered")

    def test_every_isolated_module_imports_only_what_arrives_with_it(self):
        known = repo_module_names()
        for (source_name, container), alongside in sorted(boundaries().items()):
            source = Path(source_name)
            with self.subTest(module=source.name, lands_in=container):
                wanted, relative = first_party_imports(source, known)
                self.assertEqual(
                    relative,
                    [],
                    f"{source.name} uses a relative import; mounted alone at {container} it has "
                    "no parent package to resolve against",
                )
                missing = sorted(wanted - alongside - {source.stem})
                self.assertFalse(
                    missing,
                    f"{source.relative_to(ROOT)} imports {missing}, which nothing mounts or copies "
                    f"into {container}. The host resolves them through sys.path; the container "
                    "will not, and the failure surfaces as agent-state staging or an image that "
                    "exits at startup.",
                )


if __name__ == "__main__":
    unittest.main()
