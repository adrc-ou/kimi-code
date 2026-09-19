import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.comfyui.versions import comfy_catalog

ROOT = Path(__file__).resolve().parents[1]


class ComfyModuleTests(unittest.TestCase):
    def catalog_path(self, entries: list[dict]) -> Path:
        """A compatibility document holding exactly these entries."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "compatibility.json"
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        return path

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

    def test_catalog_preserves_locked_immutable_backends(self):
        for platform in ("darwin-arm64", "wsl2-x86_64"):
            with self.subTest(platform=platform):
                catalog, latest = comfy_catalog(platform)
                self.assertTrue(catalog)
                self.assertEqual(latest, "v0.35.0")
                self.assertTrue(
                    all(item["status"] == "locked" for item in catalog),
                    "only a backend this module has certified may be selectable",
                )

    def test_catalog_orders_by_semver_not_by_string(self):
        # A lexicographic sort puts v0.9.0 above v0.10.0, and the version menu would then offer
        # the wrong release as "latest" with nothing else in the launcher noticing. The shipped
        # document has one entry per platform, so this needs a fixture to be checkable at all.
        path = self.catalog_path(
            [
                {"platform": "linux-x86_64", "status": "locked",
                 "comfyui_version": "v0.9.0", "comfyui_commit": "a" * 40},
                {"platform": "linux-x86_64", "status": "locked",
                 "comfyui_version": "v0.10.0", "comfyui_commit": "b" * 40},
                {"platform": "linux-x86_64", "status": "pending",
                 "comfyui_version": "v0.11.0", "comfyui_commit": "c" * 40},
                {"platform": "windows-x86_64", "status": "locked",
                 "comfyui_version": "v0.12.0", "comfyui_commit": "d" * 40},
            ]
        )
        catalog, latest = comfy_catalog("linux-x86_64", path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0", "v0.9.0"])
        self.assertEqual(latest, "v0.10.0")

    def test_catalog_refuses_an_entry_without_an_immutable_commit(self):
        # A floating ref is not a reproducible backend, and the release policy this repository
        # states is that a selectable release resolves to a commit or a published digest.
        with self.assertRaises(SystemExit):
            comfy_catalog(
                "linux-x86_64",
                self.catalog_path(
                    [{"platform": "linux-x86_64", "status": "locked",
                      "comfyui_version": "v0.3.0", "comfyui_commit": "main"}]
                ),
            )

    def test_an_excluded_platform_has_no_catalog_at_all(self):
        self.assertEqual(comfy_catalog("darwin-x86_64"), ([], ""))

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
