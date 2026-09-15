import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.comfyui.versions import comfy_catalog

ROOT = Path(__file__).resolve().parents[1]


class ComfyModuleTests(unittest.TestCase):
    def test_catalog_preserves_locked_immutable_backends(self):
        for platform in ("darwin-arm64", "wsl2-x86_64"):
            catalog, latest = comfy_catalog(platform)
            self.assertTrue(catalog)
            self.assertEqual(catalog[0]["version"], latest)
            self.assertRegex(catalog[0]["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(comfy_catalog("darwin-x86_64"), ([], ""))

    def test_compatibility_excludes_intel_mac_and_cpu_linux(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            uname = directory / "uname"
            gpu = directory / "nvidia-smi"
            gpu.write_text("#!/bin/sh\nexit 1\n")
            gpu.chmod(0o700)
            env = {**os.environ, "PATH": f"{directory}:/usr/bin:/bin"}
            for system, machine, expected in [
                ("Darwin", "arm64", 0),
                ("Darwin", "x86_64", 1),
                ("Linux", "x86_64", 1),
                ("Linux", "aarch64", 1),
            ]:
                uname.write_text(
                    f'#!/bin/sh\ncase "$1" in -s) echo {system};; -m) echo {machine};; esac\n'
                )
                uname.chmod(0o700)
                result = subprocess.run(["bash", str(ROOT / "module.sh"), "compatible"], env=env)
                self.assertEqual(result.returncode, expected)
            uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
            gpu.write_text('#!/bin/sh\necho "GPU 0: NVIDIA"\n')
            result = subprocess.run(
                ["bash", str(ROOT / "module.sh"), "compatible"], env=env, capture_output=True
            )
            self.assertEqual(result.returncode, 0)
