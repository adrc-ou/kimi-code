#!/usr/bin/env python3
"""Pre-flight the `security` job's vulnerability gate without Docker and without a build.

Two reviews of that gate reasoned about the wrong artifact. One scanned the floating
``python:3.14-alpine`` tag while the Dockerfile pinned a two-months-older digest, which is how
CVE-2026-115557 (Critical) and CVE-2026-107891 (High) reached CI after a supposedly verified fix.
The other scanned the base image while the finding lived in the ``pip install`` layer above it.
Both produced a confident wrong answer, so this script refuses to take an image name from a human:

* which images are gated is read out of the ``docker build -t <image> <context>`` lines of the
  ``security`` job itself, so adding a service to the gate adds it here on the next commit;
* the base scanned is the **final stage** of that context's Dockerfile, by digest, because a
  build-stage base never ships and a tag is not the thing being built;
* ``apk add --upgrade pkg=ver`` lines are read and credited only when they name the exact
  version the advisory says fixes the finding;
* each lock the gated image installs is checked against OSV, the source the gate's own
  data derives from;

Stdlib only. With grype on PATH it uses the same scanner, flags and database the job does; without
it, it says so and fails rather than reporting a clean run it did not perform.

    python3 scripts/check_vulnerabilities.py           # what the security job will decide
    python3 scripts/check_vulnerabilities.py --drift-only   # pins vs their tags, no scanner needed
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SKIP_DIRS = {".git", ".local", "node_modules", "__pycache__", ".venv", "dist", "build"}
# Repeated from the gate on purpose: a pre-flight that judged a different threshold would agree
# with nothing the pipeline does. scripts/generate_sbom.sh holds the authoritative invocation.
GATE_FLAGS = ["--fail-on", "high", "--only-fixed"]
HIGH_OR_BETTER = {"HIGH", "CRITICAL"}
BUILD_RE = re.compile(r"docker build [^\n]*?-t\s+(\S+)\s+(\S+)")
FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)
APK_UPGRADE_RE = re.compile(r"apk add[^\n]*?--upgrade[^\n]*?\b([a-z0-9][a-z0-9._-]*)=([^\s\\]+)")
COPY_RE = re.compile(r"^\s*COPY\s+(?:--from=\S+\s+)?(\S+)\s+\S+", re.IGNORECASE | re.MULTILINE)
LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^ \\\\]+)")
DIGEST_RE = re.compile(r"^(?P<ref>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})$")

#: One scan of a pinned base reference, returning the scanner's raw finding objects: grype itself in
#: a real run, a stand-in in a test. ``check_bases`` judges the findings; this says how they arrive.
Scanner = Callable[[str, Path], list[dict]]


def gated_contexts() -> list[tuple[str, str]]:
    """(image, build context) pairs the security job builds and therefore scans."""
    if not WORKFLOW.is_file():
        raise SystemExit(f"cannot read the workflow that defines the gate: {WORKFLOW}")
    seen = []
    for image, context in BUILD_RE.findall(WORKFLOW.read_text(encoding="utf-8")):
        dockerfile = ROOT / context / "Dockerfile"
        pair = (image, str(dockerfile))
        if dockerfile.is_file() and pair not in seen:
            seen.append(pair)
    return seen


def final_stage_base(dockerfile: Path) -> str:
    """The base of the stage that ships, which in a single-stage file is simply its only FROM."""
    matches = FROM_RE.findall(dockerfile.read_text(encoding="utf-8"))
    if not matches:
        raise SystemExit(f"{dockerfile.relative_to(ROOT)} has no FROM line")
    return matches[-1][0]


def final_stage_text(dockerfile: Path) -> str:
    lines = dockerfile.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if FROM_RE.match(line)]
    return "".join(lines[starts[-1]:]) if starts else "".join(lines)


def osv_severity(vuln: dict) -> str:
    severity = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if severity:
        return severity
    for entry in vuln.get("severity") or []:
        parts = str(entry.get("score") or "").split("/")
        if len(parts) > 3:
            try:
                score = float(parts[3])
            except ValueError:
                continue
            if score >= 9:
                return "CRITICAL"
            if score >= 7:
                return "HIGH"
            return "MEDIUM" if score >= 4 else "LOW"
    return "UNKNOWN"


def osv_query(name: str, version: str, ecosystem: str) -> list[dict]:
    body = json.dumps({"package": {"name": name, "ecosystem": ecosystem}, "version": version})
    request = urllib.request.Request(
        "https://api.osv.dev/v1/query",
        data=body.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "adrc-kimi-harness-preflight"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response).get("vulns", []) or []


def load_exceptions() -> dict[tuple[str, str], str]:
    path = ROOT / "vulnerability-exceptions.json"
    if not path.is_file():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8")).get("exceptions", [])
    return {(e["vulnerability"].lower(), e["package"].lower()): e["expires"] for e in entries}


def write_ignore(accepted: dict) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    path = Path(handle.name)
    if not accepted:
        handle.write("ignore: []\n")
    else:
        handle.write("ignore:\n")
        for (vulnerability, package), _expiry in sorted(accepted.items()):
            handle.write(f"  - vulnerability: {vulnerability}\n")
            handle.write(f"    package:\n      name: {package}\n")
    handle.close()
    # Left at tempfile's 0600. grype is spawned by this same user with no `user=`, so a wider
    # mode bought nothing and put the accepted-vulnerability list — and each entry's expiry — in
    # world-readable shared memory for the length of the scan. If a future invocation ever runs
    # the scanner as somebody else, grype exits non-zero and grype_json names it.
    return path


def grype_json(reference: str, ignore_file: Path) -> list[dict]:
    command = ["grype", f"registry:{reference}", *GATE_FLAGS,
               "--config", str(ignore_file), "-o", "json"]
    result = subprocess.run(command, capture_output=True, text=True)
    # 2 is grype's verdict that --fail-on was met and its report is still valid output; anything
    # else non-zero means it did not scan, which must never be read as a pass.
    if result.returncode not in (0, 2):
        raise SystemExit(
            f"grype failed to scan {reference} (exit {result.returncode}): {result.stderr[-300:]}"
        )
    return json.loads(result.stdout).get("matches", []) if result.stdout.strip() else []


def check_bases(gate: list, accepted: dict, scanner: Scanner) -> list[str]:
    problems = []
    ignore_file = write_ignore(accepted)
    try:
        for image, dockerfile_name in gate:
            dockerfile = Path(dockerfile_name)
            reference = final_stage_base(dockerfile)
            pinned = DIGEST_RE.match(reference)
            if pinned is None:
                problems.append(
                    f"{dockerfile.relative_to(ROOT)}: final-stage base {reference} is not pinned "
                    "by digest, so nothing here can vouch for what it contains"
                )
                continue
            upgrades = dict(APK_UPGRADE_RE.findall(final_stage_text(dockerfile)))
            print(f"  {image} <- {reference[:56]}…")
            for finding in scanner(reference, ignore_file):
                vulnerability = finding.get("vulnerability") or {}
                artifact = finding.get("artifact") or {}
                severity = str(vulnerability.get("severity") or "").upper()
                package = str(artifact.get("name") or "").lower()
                identifier = str(vulnerability.get("id") or "").lower()
                if severity not in HIGH_OR_BETTER or (identifier, package) in accepted:
                    continue
                fixed_in = {str(v) for v in (vulnerability.get("fix") or {}).get("versions") or []}
                if upgrades.get(package) in fixed_in:
                    print(f"      {vulnerability.get('id')} {package}: removed by the Dockerfile's"
                          f" `apk add --upgrade {package}={upgrades[package]}`")
                    continue
                advice = f"upgrade {package} to {'/'.join(sorted(fixed_in)[:2])}" if fixed_in \
                    else "no upstream fix yet"
                problems.append(
                    f"{dockerfile.relative_to(ROOT)}: {vulnerability.get('id')} {severity} in "
                    f"{package}=={artifact.get('version')} - {advice}"
                )
    finally:
        ignore_file.unlink(missing_ok=True)
    return problems


def locks_in_gate(gate: list) -> list[Path]:
    """Locks the gated images copy in - the only app layer the gate can be blamed for."""
    found: list[Path] = []
    for _image, dockerfile_name in gate:
        dockerfile = Path(dockerfile_name)
        for source in COPY_RE.findall(dockerfile.read_text(encoding="utf-8")):
            if not source.endswith(".lock") and not source.endswith(".json"):
                continue
            candidate = (dockerfile.parent / Path(source).name).resolve()
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def check_locks(paths: list[Path], accepted: dict) -> list[str]:
    problems = []
    for path in paths:
        pins = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = LOCK_PIN_RE.match(line)
            if match:
                pins.append((match.group(1), match.group(2)))
        for name, version in pins:
            try:
                vulns = osv_query(name, version, "PyPI")
            except Exception as error:  # noqa: BLE001 - one unreachable package is not a clean sheet
                problems.append(f"{path.relative_to(ROOT)}: {name} could not be queried: {error}")
                continue
            for vuln in vulns:
                severity = osv_severity(vuln)
                if severity not in HIGH_OR_BETTER:
                    continue
                ids = {vuln["id"].lower(), *(a.lower() for a in vuln.get("aliases") or [])}
                if any((i, name.lower()) in accepted for i in ids):
                    continue
                fixes = sorted(
                    {
                        event["fixed"]
                        for affected in vuln.get("affected", [])
                        for group in affected.get("ranges", [])
                        for event in group.get("events", [])
                        if "fixed" in event
                    }
                )
                problems.append(
                    f"{path.relative_to(ROOT)}: {name}=={version} {vuln['id']} {severity} "
                    f"fix={', '.join(fixes[:3]) or 'none'}"
                )
        print(f"  {path.relative_to(ROOT)}: {len(pins)} pins checked against OSV")
    return problems


def check_expires(accepted: dict) -> list[str]:
    today = datetime.date.today().isoformat()
    return [
        f"vulnerability-exceptions.json: {vulnerability}/{package} expired on {expiry}"
        for (vulnerability, package), expiry in sorted(accepted.items())
        if expiry < today
    ]


def registry_index_digest(reference: str) -> str:
    name_and_tag = reference.split("@", 1)[0]
    name, _, tag = name_and_tag.rpartition(":")
    repository, _, _ = name.partition("docker.io/")
    repository = repository if "/" in repository else f"library/{repository}"
    scope = f"repository:{repository}:pull"
    scope_url = f"https://auth.docker.io/token?service=registry.docker.io&scope={scope}"
    token = json.load(urllib.request.urlopen(scope_url, timeout=60))["access_token"]
    accept = ("application/vnd.oci.image.index.v1+json,"
              "application/vnd.docker.distribution.manifest.list.v2+json")
    request = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{repository}/manifests/{tag or 'latest'}",
        headers={"Authorization": f"Bearer {token}", "Accept": accept, "User-Agent": "preflight"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.headers.get("Docker-Content-Digest", "")


def report_drift() -> None:
    """Every digest pin in the repo, against what its tag resolves to today. Informational."""
    dockerfiles = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        dockerfiles += [Path(base) / n for n in sorted(files) if n == "Dockerfile"]
    for dockerfile in dockerfiles:
        for reference in FROM_RE.findall(dockerfile.read_text(encoding="utf-8")):
            raw = reference[0]
            pinned = DIGEST_RE.match(raw)
            if pinned is None:
                continue
            try:
                now = registry_index_digest(raw)
            except Exception as error:  # noqa: BLE001 - drift never fails the run
                print(f"  {dockerfile.relative_to(ROOT)}: could not resolve {raw}: {error}")
                continue
            state = "current" if now == pinned.group("digest") else f"BEHIND the tag (now {now})"
            print(f"  {dockerfile.relative_to(ROOT)}: {pinned.group('ref')} → {state}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drift-only", action="store_true", help="report pin drift and exit")
    args = parser.parse_args()

    gate = gated_contexts()
    accepted = load_exceptions()
    print(f"gate covers {len(gate)} image(s); {len(accepted)} accepted exception(s)")
    report_drift()
    if args.drift_only:
        return 0

    problems = check_expires(accepted)
    if shutil.which("grype") is None:
        problems.append(
            "grype is not on PATH, so the image layer was never checked. Install the version the "
            "security job pins, or trust the pipeline instead of this script - but do not read a "
            "run that scanned nothing as a pass."
        )
    else:
        # Which grype runs is decided the same way here and inside grype_json, so the two cannot
        # disagree about what "the scanner is present" meant.
        problems += check_bases(gate, accepted, grype_json)
    problems += check_locks(locks_in_gate(gate), accepted)

    if problems:
        print(f"\n{len(problems)} unaccepted finding(s):")
        for problem in problems:
            print(f"  {problem}")
        return 2
    print("\npreflight clean: nothing High or Critical beyond the accepted exceptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
