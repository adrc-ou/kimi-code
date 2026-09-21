"""The per-release dependency lock: what it is keyed by, and what makes one fit to install.

``test_module.py`` holds the shipped document to its own contract. What belongs here is the
machinery that turns a chosen release into something installable, because its whole job is to
refuse — a lock that is accepted for the wrong release installs a dependency set nobody reviewed
for it, and every hash inside it would still be correct.

Nothing here reaches the network or runs a resolver: the baseline release is the one the shipped
lock was built from, so asking for it exercises the real document, the real digests and the real
CLI without needing uv.
"""

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "modules" / "comfyui"))
import resolution  # noqa: E402
import resolve_locks  # noqa: E402

CUDA = "wsl2-x86_64"
MAC = "darwin-arm64"
# The reviewed baseline: v0.35.0, whose requirements digest the shipped locks were compiled from.
BASELINE_COMMIT = "40c4fcdf513a4523e39d54a9d391908af8df8171"
OTHER_COMMIT = "1111111111111111111111111111111111111111"
RESOLVER = "uv@0.12.17"
DIGEST = resolution.digest_file(ROOT / "modules/comfyui/backend/requirements-linux.lock")
GOOD = resolution.digest_of("torch==2.11.0\n")
OTHER_GOOD = "b" * 64

def pin(specification: str, digest: str = DIGEST) -> str:
    """One requirement as a hash-pinned lock writes it: the pin, then its hashes."""
    return f"{specification} \\\n    --hash=sha256:{digest}\n"


LOCK = (
    f"# comfyui-requirements-sha256={GOOD}\n"
    + pin("aiohttp==3.14.3")
    + pin("torch==2.11.0")
    + pin("torchvision==0.26.0")
    + pin("torchaudio==2.11.0")
)
URL_PIN = (
    "torch @ https://download-r2.pytorch.org/whl/cu130/torch-2.11.0%2Bcu130-cp312-cp312-"
    f"manylinux_2_28_x86_64.whl#sha256={DIGEST}\n"
)


class LockKeyTests(unittest.TestCase):
    def setUp(self):
        self.profile = resolution.profile(CUDA)
        self.key = resolution.lock_key(self.profile, GOOD, RESOLVER)

    def test_the_key_is_stable(self):
        self.assertEqual(self.key, resolution.lock_key(resolution.profile(CUDA), GOOD, RESOLVER))
        self.assertRegex(self.key, r"^[0-9a-f]{64}$")

    def test_a_different_release_is_a_different_key(self):
        self.assertNotEqual(self.key, resolution.lock_key(self.profile, OTHER_GOOD, RESOLVER))

    def test_a_different_platform_is_a_different_key(self):
        # Both profiles hold the same reviewed torch pins and the same custom requirements, so the
        # only thing that can tell a macOS lock from a CUDA one is the profile in the key. Without
        # it, one release's lock would answer for the other's and install the wrong wheels.
        self.assertNotEqual(self.key, resolution.lock_key(resolution.profile(MAC), GOOD, RESOLVER))

    def test_a_different_resolver_is_a_different_key(self):
        # Two resolvers can pick two different sets from one input, so a lock built by one must not
        # be handed over as the other's without being rebuilt.
        self.assertNotEqual(self.key, resolution.lock_key(self.profile, GOOD, "uv@9.9.9"))

    def test_a_field_that_cannot_change_a_lock_is_not_in_the_key(self):
        # The version and repository a profile advertises decide which release is offered, not what
        # it installs with. Keying on them would re-resolve every release whenever upstream cut a
        # new tag, which is the cost the cache exists to avoid.
        moved = copy.deepcopy(self.profile)
        moved["repository"] = "somebody-else/ComfyUI"
        moved["baseline"] = {"comfyui_version": "v9.9.9"}
        self.assertEqual(self.key, resolution.lock_key(moved, GOOD, RESOLVER))


class KeyedInputTests(unittest.TestCase):
    """Whether the three reviewed files really feed the key, against a copy of the module."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.module = self.base / "modules/comfyui"
        shutil.copytree(
            ROOT / "modules/comfyui",
            self.module,
            ignore=shutil.ignore_patterns("__pycache__", "tests"),
        )
        self.root = self.base / ".local/runtime/test"
        self.root.mkdir(parents=True)

    def key_for(self, field: str) -> str:
        prof = resolution.profile(CUDA, self.module / "backend/compatibility.json")
        return resolution.lock_key(prof, GOOD, RESOLVER, root=self.module)

    def test_each_keyed_file_moves_the_key(self):
        baseline = self.key_for("backend_lock")
        for field in resolution.KEYED_INPUTS:
            with self.subTest(field=field):
                path = self.module / resolution.profile(
                    CUDA, self.module / "backend/compatibility.json"
                )[field]
                path.write_text(path.read_text() + "\n# reviewed differently\n")
                self.assertNotEqual(baseline, self.key_for(field))
                shutil.copyfile(ROOT / "modules/comfyui" / path.relative_to(self.module), path)
                self.assertEqual(baseline, self.key_for(field))

    def test_a_file_nothing_reviewed_is_refused_rather_than_defaulted(self):
        prof = resolution.profile(CUDA, self.module / "backend/compatibility.json")
        prof["torch_constraints"] = "backend/torch-constraints-typo.txt"
        with self.assertRaises(SystemExit):
            resolution.lock_key(prof, GOOD, RESOLVER, root=self.module)


class KeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.profile = resolution.profile(CUDA)

    def test_a_malformed_requirements_digest_is_refused(self):
        for bad in ("", "not-a-digest", GOOD.upper(), GOOD[:-1]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                resolution.lock_key(self.profile, bad, RESOLVER)

    def test_a_lock_built_by_an_unknown_resolver_is_refused(self):
        # Naming the resolver is what makes a uv upgrade a cache miss; an empty name would quietly
        # reintroduce the aliasing the key exists to prevent.
        for bad in ("", "   "):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                resolution.lock_key(self.profile, GOOD, bad)

    def test_an_undeclared_platform_has_no_profile(self):
        with self.assertRaises(SystemExit):
            resolution.profile("linux-aarch64")


class HeaderTests(unittest.TestCase):
    def test_the_header_states_what_the_lock_was_built_from(self):
        header = resolution.provenance_header(
            platform=CUDA, version="v0.9.9", commit=BASELINE_COMMIT, requirements_sha256=GOOD,
            key="c" * 64,
        )
        self.assertEqual(resolution.PROVENANCE_RE.search(header).group(1), GOOD)
        for line in header.splitlines():
            self.assertTrue(line.startswith("# "), line)

    def test_the_header_survives_uv_appending_its_own(self):
        # uv writes its command line above the output, and the resolver prepends, so both have to be
        # readable in one file without either hiding the other.
        text = (
            resolution.provenance_header(
                platform=CUDA, version="v0.9.9", commit=BASELINE_COMMIT,
                requirements_sha256=GOOD, key="c" * 64,
            )
            + "# This file was autogenerated by uv via the following command:\n"
            + "#    uv pip compile x\n"
            + LOCK
        )
        self.assertEqual(resolution.check_text(text, GOOD, "x.lock"), [])


class CheckTests(unittest.TestCase):
    def test_a_lock_that_claims_the_right_requirements_passes(self):
        self.assertEqual(resolution.check_text(LOCK, GOOD, "x.lock"), [])

    def test_a_lock_that_says_nothing_is_reported(self):
        # Hash-pinned so the missing provenance is the only finding: check_text deliberately reports
        # every problem at once, so an unhashed pin here would add a second, correct complaint.
        problems = resolution.check_text(pin("torch==2.11.0"), GOOD, "x.lock")
        self.assertEqual(len(problems), 1)
        self.assertIn("does not say which requirements", problems[0])

    def test_a_lock_built_from_other_requirements_is_named_by_both_digests(self):
        problems = resolution.check_text(LOCK, OTHER_GOOD, "x.lock")
        self.assertEqual(len(problems), 1)
        self.assertIn(GOOD[:12], problems[0])
        self.assertIn(OTHER_GOOD[:12], problems[0])

    def test_a_pin_without_a_hash_is_found_before_the_install(self):
        # pip fails on the first hashless requirement, after downloading the wheels.
        text = LOCK.replace(pin("torch==2.11.0"), "somepack==1.2.3\n")
        problems = resolution.check_text(text, GOOD, "x.lock")
        self.assertEqual(len(problems), 1)
        self.assertIn("somepack has no sha256 hash", problems[0])

    def test_a_direct_url_pin_carries_its_digest_in_the_fragment(self):
        # Every CUDA torch entry is this form; treating it as a missing hash would reject the only
        # lock the reviewed backend can build on that platform.
        self.assertEqual(
            resolution.check_text(LOCK.replace(pin("torch==2.11.0"), URL_PIN), GOOD, "x.lock"), []
        )

    def test_an_unparsable_line_is_reported_with_its_number(self):
        problems = resolution.check_text(LOCK + "totally not a requirement\n", GOOD, "x.lock")
        self.assertEqual(len(problems), 1)
        self.assertIn("x.lock:10:", problems[0])

    def test_an_empty_lock_is_one_problem_not_two(self):
        # The missing hashes of nothing are not a separate finding, and reporting them would bury
        # the one thing the operator needs to read.
        text = f"# comfyui-requirements-sha256={GOOD}\n"
        problems = resolution.check_text(text, GOOD, "x.lock")
        self.assertEqual(problems, ["x.lock is empty"])

    def test_check_reads_a_file_and_reports_its_absence(self):
        self.assertEqual(resolution.check(Path("/nonexistent/x.lock"), GOOD), [
            "/nonexistent/x.lock does not exist"
        ])


class TorchCheckTests(unittest.TestCase):
    def setUp(self):
        self.backend_env = resolution.PROFILE_ROOT / resolution.profile(CUDA)["backend_lock"]

    def test_the_reviewed_torch_set_passes_in_either_spelling(self):
        self.assertEqual(resolution.check_torch_text(LOCK, self.backend_env, "x.lock"), [])
        cuda = f"# comfyui-requirements-sha256={GOOD}\n" + "".join(
            f"{name} @ https://x/{name}-{version}%2Bcu130-cp312.whl#sha256={DIGEST}\n"
            for name, version in (
                ("torch", "2.11.0"),
                ("torchvision", "0.26.0"),
                ("torchaudio", "2.11.0"),
            )
        )
        self.assertEqual(resolution.check_torch_text(cuda, self.backend_env, "x.lock"), [])

    def test_a_resolver_that_picked_another_torch_is_refused(self):
        # A constraint layer is only a request. Accepting a different torch would change which
        # hardware the image can use, which is not recoverable after the build.
        problems = resolution.check_torch_text(
            LOCK.replace(pin("torch==2.11.0"), pin("torch==2.9.0")), self.backend_env, "x.lock"
        )
        self.assertEqual(problems, ["x.lock did not resolve torch to the reviewed 2.11.0"])

    def test_a_lock_missing_a_torch_entry_is_refused(self):
        stripped = "\n".join(
            line for line in LOCK.splitlines() if not line.startswith("torchaudio")
        )
        problems = resolution.check_torch_text(stripped, self.backend_env, "x.lock")
        self.assertEqual(len(problems), 1)
        self.assertIn("torchaudio", problems[0])

    def test_a_backend_that_stopped_naming_its_torch_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            bare = Path(directory) / "backend.env"
            bare.write_text("UNRELATED=1\n")
            problems = resolution.check_torch_text(LOCK, bare, "x.lock")
        self.assertEqual(len(problems), 3)
        self.assertIn("does not pin TORCH_VERSION", problems[0])


class BaselineLockTests(unittest.TestCase):
    def setUp(self):
        self.profile = resolution.profile(CUDA)
        with (ROOT / "modules/comfyui/backend/compatibility.json").open() as handle:
            document = json.load(handle)
        self.digest = next(
            p["baseline"]["requirements_sha256"] for p in document["platforms"]
            if p["platform"] == CUDA
        )

    def test_the_shipped_lock_answers_for_the_release_it_was_compiled_from(self):
        shipped = resolution.shipped_lock(self.profile, self.digest)
        self.assertIsNotNone(shipped)
        self.assertTrue(shipped.is_file())
        self.assertEqual(shipped.name, "requirements-linux.lock")

    def test_the_shipped_lock_answers_for_nothing_else(self):
        # This is the whole reason a second release needs resolving: the digest that does not match
        # has to yield nothing rather than the nearest available lock.
        self.assertIsNone(resolution.shipped_lock(self.profile, OTHER_GOOD))

    def test_a_baseline_that_stopped_being_a_baseline_yields_no_recorded_release(self):
        profile = copy.deepcopy(self.profile)
        del profile["baseline"]
        self.assertIsNone(resolution.shipped_lock(profile, self.digest))

    def test_a_profile_without_a_shipped_baseline_lock_is_an_error(self):
        profile = copy.deepcopy(self.profile)
        profile["baseline_lock"] = "backend/requirements-nowhere.lock"
        with self.assertRaises(SystemExit):
            resolution.shipped_lock(profile, self.digest)


class ResolverPinTests(unittest.TestCase):
    def test_the_shipped_resolver_is_pinned_to_the_official_image(self):
        pin = resolve_locks.resolver_pin()
        lock = json.loads((ROOT / "modules/comfyui/dependencies.lock.json").read_text())
        self.assertEqual(pin["version"], lock["downloads"]["uv-resolver"]["version"])
        self.assertEqual(pin["image"], f"ghcr.io/astral-sh/uv:{pin['version']}")
        self.assertRegex(pin["digest"], r"^sha256:[0-9a-f]{64}$")

    def run_pin(self, entry):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory)
            (module / "dependencies.lock.json").write_text(
                json.dumps({"downloads": {"uv-resolver": entry}})
            )
            original = resolve_locks.MODULE
            resolve_locks.MODULE = module
            self.addCleanup(setattr, resolve_locks, "MODULE", original)
            return resolve_locks.resolver_pin()

    def test_a_resolver_with_no_image_digest_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_pin({"version": "0.12.17", "image": "ghcr.io/astral-sh/uv:0.12.17"})

    def test_an_unofficial_resolver_image_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_pin(
                {
                    "version": "0.12.17",
                    "image": "docker.io/library/uv:0.12.17",
                    "digest": "sha256:" + "a" * 64,
                }
            )

    def test_a_tag_and_a_digest_that_disagree_are_refused(self):
        # The version is what goes into the lock key and what the operator is told; the digest is
        # what is actually pulled. One naming the other keeps them from drifting apart.
        with self.assertRaises(SystemExit):
            self.run_pin(
                {
                    "version": "0.12.17",
                    "image": "ghcr.io/astral-sh/uv:0.11.0",
                    "digest": "sha256:" + "a" * 64,
                }
            )


class ContainerMountTests(unittest.TestCase):
    """What the resolver container is allowed to see."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name).resolve() / "instance"
        self.plan = resolution.plan(
            CUDA, "v0.35.0", BASELINE_COMMIT, GOOD, self.runtime, RESOLVER
        )

    def test_the_instance_root_is_never_mounted(self):
        # The instance directory holds the provider key and the proxy token. A build tool resolving
        # a requirements file has no use for either, and mounting the root to reach one
        # subdirectory would hand both to a container that pulls wheels from the internet.
        argv = resolve_locks.container_argv(self.plan, {"digest": "sha256:" + "a" * 64})
        mounts = [argv[index + 1] for index, word in enumerate(argv) if word == "-v"]
        self.assertTrue(mounts, "the resolver container is given no bind at all")
        for binding in mounts:
            source = binding.split(":")[0]
            self.assertNotEqual(Path(source), self.runtime)
            self.assertNotEqual(Path(source), self.runtime.parent)
            self.assertTrue(
                Path(source).is_relative_to(self.runtime / "comfyui")
                or Path(source).is_relative_to(resolution.PROFILE_ROOT / "backend"),
                f"{source} is outside what resolution needs",
            )

    def test_the_lock_and_the_requirements_are_reachable_from_the_mounts(self):
        argv = resolve_locks.container_argv(self.plan, {"digest": "sha256:" + "a" * 64})
        sources = [argv[index + 1].split(":")[0] for index, w in enumerate(argv) if w == "-v"]
        for path in (self.plan["source"], self.plan["lock"], self.plan["cache"]):
            with self.subTest(path=path):
                self.assertTrue(
                    any(Path(str(path)).is_relative_to(Path(source)) for source in sources)
                )

    def test_the_plan_paths_are_absolute(self):
        # A bind source has to name the same file inside and outside the container, and a relative
        # path means the working directory decides which file the resolver reads.
        for key in ("source", "lock", "stage", "cache", "mount"):
            with self.subTest(key=key):
                self.assertTrue(Path(self.plan[key]).is_absolute())

    def test_the_resolver_image_is_reached_by_digest(self):
        argv = resolve_locks.container_argv(self.plan, {"digest": "sha256:" + "a" * 64})
        self.assertIn("ghcr.io/astral-sh/uv@" + "a" * 64, argv)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.plan = resolution.plan(
            MAC, "v0.35.0", BASELINE_COMMIT, GOOD, Path(self.temporary.name), RESOLVER
        )

    def test_the_plan_asks_for_the_profile_s_resolver_target(self):
        argv = [str(part) for part in self.plan["argv"]]
        self.assertIn("aarch64-apple-darwin", argv)
        self.assertIn("--generate-hashes", argv)
        self.assertIn("--constraint", argv)
        # The command line has to name the target explicitly: a resolver that quietly used the
        # host's own platform would produce a lock that installs on neither path.
        self.assertIn("--python-platform", argv)
        self.assertIn("--no-annotate", argv)

    def test_the_constraint_layer_is_read_from_the_repository_not_a_copy(self):
        # The bytes a lock was resolved against have to be the reviewed bytes; staging a copy would
        # let a stale one produce a lock that looks current.
        argv = " ".join(str(part) for part in self.plan["argv"])
        constraints = resolution.profile_file(
            resolution.profile(MAC), "torch_constraints"
        )
        self.assertIn(str(constraints), argv)
        self.assertTrue(str(constraints).startswith(str(resolution.PROFILE_ROOT)))

    def test_the_lock_is_named_by_the_key_it_was_built_under(self):
        self.assertEqual(self.plan["lock"].name, f"{self.plan['key']}.lock")


class ResolveLocksCliTests(unittest.TestCase):
    """The launcher's actual invocation, offline, against the shipped baseline."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "instance"
        with (ROOT / "modules/comfyui/backend/compatibility.json").open() as handle:
            self.baseline = next(
                p["baseline"]
                for p in json.load(handle)["platforms"]
                if p["platform"] == CUDA
            )

    def run_resolver(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "modules/comfyui/resolve_locks.py"),
                "--platform",
                CUDA,
                "--version",
                self.baseline["comfyui_version"],
                "--commit",
                self.baseline["comfyui_commit"],
                "--requirements-sha256",
                self.baseline["requirements_sha256"],
                "--runtime-dir",
                str(self.runtime),
                *extra,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(self.temporary.name)},
            timeout=120,
        )

    def test_the_baseline_needs_no_resolver_and_still_produces_a_keyed_lock(self):
        # This is the default launch: nothing is fetched, nothing is resolved, and the installer
        # still gets a lock that states which release it belongs to.
        announced = str(self.runtime / "lock-path")
        result = self.run_resolver("--output", announced)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("baseline lock", result.stdout)
        lock = Path(announced).read_text().strip()
        self.assertTrue(Path(lock).is_absolute())
        text = Path(lock).read_text()
        self.assertEqual(
            resolution.check_text(text, self.baseline["requirements_sha256"], "lock"), []
        )

    def test_a_second_launch_reuses_the_lock_it_already_wrote(self):
        first = self.run_resolver()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_resolver()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already resolved", second.stdout)
        self.assertEqual(len(list((self.runtime / "comfyui/locks").glob("*.lock"))), 1)

    def test_a_lock_that_stopped_fitting_is_discarded_rather_than_reused(self):
        self.assertEqual(self.run_resolver().returncode, 0)
        locks = list((self.runtime / "comfyui/locks").glob("*.lock"))
        self.assertEqual(len(locks), 1)
        locks[0].write_text("# comfyui-requirements-sha256=" + OTHER_GOOD + "\n")
        result = self.run_resolver()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Discarding unfit lock", result.stderr)
        # Discarded, not repaired in place: the run that follows has to rebuild it from the inputs
        # rather than trust the file it just rejected.
        self.assertNotIn("already resolved", result.stdout)
        rebuilt = list((self.runtime / "comfyui/locks").glob("*.lock"))
        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(
            resolution.check_text(
                rebuilt[0].read_text(), self.baseline["requirements_sha256"], "lock"
            ),
            [],
        )

    def test_a_commit_that_is_not_a_commit_is_refused_before_anything_runs(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "modules/comfyui/resolve_locks.py"),
                "--platform",
                CUDA,
                "--version",
                "v0.35.0",
                "--commit",
                "not-a-commit",
                "--requirements-sha256",
                GOOD,
                "--runtime-dir",
                str(self.runtime),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hex digest", result.stderr)


class FetchDigestTests(unittest.TestCase):
    def test_requirements_are_digest_as_one_read(self):
        # The picker digests the bytes it fetched and the resolver digests them again before
        # handing them to uv. Both have to mean the same thing or the binding between a lock and a
        # release is a coincidence.
        body = "torch==2.11.0\nnumpy==2.0.0\n"
        self.assertEqual(resolution.digest_of(body), hashlib.sha256(body.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text(body, encoding="utf-8")
            self.assertEqual(resolution.digest_of(body), resolution.digest_file(path))


class EnsureRefusesStaleRequirementsTests(unittest.TestCase):
    def test_requirements_that_no_longer_match_the_verified_digest_stop_the_run(self):
        # The picker verified one file; if the resolver is about to read another, every hash in the
        # output describes some other release's dependency set.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "requirements.txt").write_text("torch==1.0.0\n")
            args = argparse.Namespace(
                platform=CUDA,
                version="v0.99.0",
                commit=OTHER_COMMIT,
                requirements_sha256=GOOD,
                runtime_dir=base / "runtime",
                requirements_file=base / "requirements.txt",
                native_uv="",
            )
            with self.assertRaises(SystemExit) as caught:
                resolve_locks.ensure(args)
        self.assertIn("changed since the picker verified them", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
