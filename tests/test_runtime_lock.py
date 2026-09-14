import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RuntimeLockTests(unittest.TestCase):
    def run_lock(self, runtime: Path) -> subprocess.CompletedProcess[str]:
        script = f"""
set -euo pipefail
source {ROOT / "tools" / "runtime.sh"}
HARNESS_RUNTIME_DIR={runtime}
mkdir -p "$HARNESS_RUNTIME_DIR"
harness_lock
harness_unlock
"""
        return subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_stale_unlocked_file_does_not_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "launcher.lock").write_text("pid=999999\n")
            self.assertEqual(self.run_lock(runtime).returncode, 0)

    def test_live_owner_blocks_second_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            owner_script = f"""
set -euo pipefail
source {ROOT / "tools" / "runtime.sh"}
HARNESS_RUNTIME_DIR={runtime}
mkdir -p "$HARNESS_RUNTIME_DIR"
harness_lock
printf ready
sleep 30
"""
            owner = subprocess.Popen(
                ["bash", "-c", owner_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert owner.stdout is not None
                self.assertEqual(owner.stdout.read(5), "ready")
                contender = self.run_lock(runtime)
                self.assertNotEqual(contender.returncode, 0)
                self.assertIn("Another launcher owns", contender.stderr)
                independent = self.run_lock(runtime / "independent")
                self.assertEqual(independent.returncode, 0)
            finally:
                owner.terminate()
                owner.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
