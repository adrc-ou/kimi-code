"""The ComfyUI module's own contract: what it ships, and which hosts it will run on.

Which releases the launcher offers is tested in ``test_releases.py`` against a fixture profile,
because it is now fetched rather than transcribed. What belongs here is the shipped document: the
platforms it declares, the immutability of what it records, and that the files it points at exist.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.comfyui import versions

ROOT = Path(__file__).resolve().parents[1]
RELEASES = versions.releases
COMPATIBILITY = ROOT / "backend" / "compatibility.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
# The keys every installer step downstream reads. A profile missing one is a profile that will fail
# partway through a build rather than at the menu, which is the worst place to find out.
REQUIRED = (
    "platform",
    "installer",
    "repository",
    "python",
    "backend",
    "resolver_platform",
    "torch",
    "torchvision",
    "torchaudio",
    "backend_lock",
    "torch_constraints",
    "baseline_lock",
    "custom_requirements",
    "custom_lock",
)


class ComfyModuleTests(unittest.TestCase):
    def document(self) -> dict:
        return json.loads(COMPATIBILITY.read_text(encoding="utf-8"))

    def compatible(self, system: str, machine: str, gpu: bool) -> int:
        """Run ``module.sh compatible`` on a host with this shape, and report its exit code.

        The stubs are rebuilt per call: a shared temp directory keeps the executable bits, while
        the two scripts are what the module actually probes.
        """
        with tempfile.TemporaryDirectory() as directory:
            bin_path = Path(directory)
            uname = bin_path / "uname"
            nvidia = bin_path / "nvidia-smi"
            uname.write_text(
                f'#!/bin/sh\ncase "$1" in -s) echo {system};; -m) echo {machine};; esac\n'
            )
            nvidia.write_text('#!/bin/sh\n' + ('echo "GPU 0: NVIDIA"\n' if gpu else "exit 1\n"))
            uname.chmod(0o700)
            nvidia.chmod(0o700)
            env = {**os.environ, "PATH": f"{bin_path}:/usr/bin:/bin"}
            return subprocess.run(
                ["bash", str(ROOT / "module.sh"), "compatible"], env=env, capture_output=True
            ).returncode

    def test_every_declared_platform_is_fully_described(self):
        platforms = self.document()["platforms"]
        self.assertTrue(platforms)
        for entry in platforms:
            with self.subTest(platform=entry.get("platform")):
                missing = [key for key in REQUIRED if key not in entry]
                self.assertEqual(missing, [], "a profile the installer cannot follow")
                self.assertIn(entry["installer"], {"cuda", "mps"})

    def test_every_lock_a_profile_names_is_shipped(self):
        # A profile that points at a file this repository does not contain would pass the menu and
        # fail the build, so the paths are checked rather than trusted.
        for entry in self.document()["platforms"]:
            for key in (
                "backend_lock",
                "torch_constraints",
                "baseline_lock",
                "custom_requirements",
                "custom_lock",
            ):
                with self.subTest(platform=entry["platform"], key=key):
                    self.assertTrue(
                        (ROOT / entry[key]).is_file(),
                        f"{entry['platform']} declares {key}={entry[key]}, which is not shipped",
                    )

    def test_the_shipped_profiles_are_the_platforms_the_module_configures(self):
        # module_configure() derives COMFYUI_PLATFORM from the launcher's HARNESS_PLATFORM, and the
        # picker refuses any platform without a profile. If the two lists drift, a host that passes
        # the compatibility gate still cannot start — and the installer it picks has to be the one
        # that path actually runs.
        self.assertEqual(
            {e["platform"]: e["installer"] for e in self.document()["platforms"]},
            {"darwin-arm64": "mps", "wsl2-x86_64": "cuda"},
        )

    def test_each_platform_records_an_immutable_installable_baseline(self):
        for entry in self.document()["platforms"]:
            platform = entry["platform"]
            with self.subTest(platform=platform):
                recorded = RELEASES.recorded_releases(platform)
                self.assertTrue(recorded, f"{platform} has no release installable without network")
                for item in recorded:
                    self.assertTrue(COMMIT_RE.fullmatch(item["commit"]))
                    self.assertTrue(DIGEST_RE.fullmatch(item["requirements_sha256"]))

    def test_the_baseline_lock_digests_agree_with_the_shipped_files(self):
        # The four digests are what the offline fallback and check_locks both rely on. If one stops
        # matching the file beside it, the lock is stale and an install would be silently reproving
        # a dependency set nobody reviewed.
        for entry in self.document()["platforms"]:
            baseline = entry["baseline"]
            for key, relative in (
                ("backend_lock_sha256", entry["backend_lock"]),
                ("custom_requirements_sha256", entry["custom_requirements"]),
                ("requirements_lock_sha256", entry["baseline_lock"]),
                ("custom_lock_sha256", entry["custom_lock"]),
            ):
                with self.subTest(platform=entry["platform"], key=key):
                    actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                    self.assertEqual(baseline[key], actual)

    def test_an_undeclared_platform_is_refused_rather_than_defaulted(self):
        # Defaulting to the nearest profile would install linux wheels on a mac because both are
        # "not the other one"; the picker has to stop instead.
        with self.assertRaises(SystemExit):
            RELEASES.profile("darwin-x86_64", COMPATIBILITY)

    def test_intel_mac_is_incompatible_and_arm_mac_is_not(self):
        self.assertEqual(self.compatible("Darwin", "arm64", gpu=False), 0)
        self.assertEqual(self.compatible("Darwin", "x86_64", gpu=False), 1)

    def test_a_cpu_only_linux_is_incompatible_on_either_architecture(self):
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine):
                self.assertEqual(self.compatible("Linux", machine, gpu=False), 1)

    def test_linux_with_a_gpu_present_is_compatible(self):
        # The pair this belongs with: without it, "CPU-only Linux is excluded" is indistinguishable
        # from "all Linux is excluded", and the test above would pass either way.
        self.assertEqual(self.compatible("Linux", "x86_64", gpu=True), 0)


if __name__ == "__main__":
    unittest.main()
