"""The lock assertions the CUDA image makes about itself, run the way the build runs them.

`Dockerfile.cuda` re-checks the lock inside the image after `resolution.py` refused an unfit one,
because which CUDA flavour a torch wheel carries is this image's own claim and nothing upstream can
vouch for it. Those checks are shell inside a Dockerfile, which no other test executes, so this one
lifts the assertion block out of the file and runs it against locks: the release the harness ships,
a release that no longer depends on every reviewed torch package, and locks tampered with in each
of the ways the block exists to catch.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import resolution  # noqa: E402

DOCKERFILE = ROOT / "backend" / "Dockerfile.cuda"
# The image installs into this fixed path, and the assertions name it literally, so the test has to
# use the same name rather than a temporary one: a check aimed at a different file proves nothing.
LOCK_PATH = Path("/tmp/requirements-linux.lock")
C38_COMMIT = "6b747c0428c343e1417219641db93a4fb7cb69ae"
C38_SHA = "c8325341fcc03b480f7ddb902ce6049e418ecf718232efeaacbfe51aaa79b3fe"
# One reviewed CUDA wheel as a resolved lock writes it: the direct URL pin, then its hash.
TORCH_WHEEL = (
    "torch @ https://d/torch-2.11.0%2Bcu130-cp312.whl#sha256=ab \\\n"
    "    --hash=sha256:ab\n"
)


def assertion_block() -> str:
    """The RUN block that checks the lock, with the installs cut off.

    Continuations are joined the way Docker joins them — the backslash-newline disappears and the
    next line's indentation separates the tokens — because that is the only spelling the build ever
    hands to the shell, and a block that needs real newlines to parse is a block this test would be
    reading differently from docker.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    marker = 'RUN test -n "${COMFYUI_REQUIREMENTS_SHA256}"'
    block = text[text.index(marker) :].removeprefix("RUN ")
    block = block.replace("\\\n", " ")
    return block[: block.index("python -m pip install")].rstrip().rstrip("&").rstrip()


class CudaLockAssertionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = resolution.profile("wsl2-x86_64")
        cls.block = assertion_block()

    def setUp(self):
        self.previous = LOCK_PATH.read_bytes() if LOCK_PATH.is_file() else None
        self.addCleanup(self.restore)

    def restore(self):
        if self.previous is None:
            LOCK_PATH.unlink(missing_ok=True)
            return
        LOCK_PATH.write_bytes(self.previous)

    def run_block(
        self, text: str, commit: str, requirements_sha256: str
    ) -> subprocess.CompletedProcess:
        LOCK_PATH.write_text(text, encoding="utf-8")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TORCH_VERSION": self.profile["torch"],
            "TORCHVISION_VERSION": self.profile["torchvision"],
            "TORCHAUDIO_VERSION": self.profile["torchaudio"],
            "PYTORCH_CUDA_INDEX_URL": "https://download.pytorch.org/whl/cu130",
            "COMFYUI_COMMIT": commit,
            "COMFYUI_REQUIREMENTS_SHA256": requirements_sha256,
        }
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                ["sh", "-c", self.block],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

    def header(self, commit: str, requirements_sha256: str) -> str:
        # The block reads two lines out of the header, the commit and the requirements digest; the
        # rest of it is here because the resolver writes the whole block around those two.
        return resolution.provenance_header(
            platform="wsl2-x86_64",
            version="v0.38.0",
            commit=commit,
            requirements_sha256=requirements_sha256,
            key="0" * 64,
        )

    def check(
        self, body: str, commit: str = C38_COMMIT, requirements_sha256: str = C38_SHA
    ) -> subprocess.CompletedProcess:
        """Run the block over a lock whose header makes that commit and requirements claim."""
        return self.run_block(
            self.header(commit, requirements_sha256) + body, commit, requirements_sha256
        )

    def test_a_release_that_dropped_a_reviewed_torch_package_still_builds(self):
        # ComfyUI removed torchaudio from its requirements after v0.35.0, so a real lock for a later
        # release carries torch and torchvision only. The reviewed pins govern the version of what a
        # release asks for, not which packages it must ask for, and this is where that is enforced a
        # second time — a grep that demanded torchaudio would fail the build minutes into it.
        body = (
            TORCH_WHEEL
            + "torchvision @ https://d/torchvision-0.26.0%2Bcu130-cp312.whl#sha256=cd \\\n"
            "    --hash=sha256:cd\n"
        )
        result = self.check(body)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_shipped_baseline_lock_builds(self):
        # The lock the module ships does carry all three, so the conditional must not have stopped
        # any of them from being checked.
        baseline = self.profile["baseline"]
        body = (ROOT / self.profile["baseline_lock"]).read_text(encoding="utf-8")
        result = self.check(body, baseline["comfyui_commit"], baseline["requirements_sha256"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_package_the_lock_carries_at_another_flavour_is_refused(self):
        for wheel, replacement, named in (
            ("torch-2.11.0%2Bcu130", "torch-2.11.0%2Bcu128", "torch"),
            ("torchvision-0.26.0%2Bcu130", "torchvision-0.25.0%2Bcu130", "torchvision"),
        ):
            with self.subTest(wheel=wheel):
                body = (
                    f"{named} @ https://d/{wheel}-cp312.whl#sha256=ab \\\n"
                    "    --hash=sha256:ab\n"
                )
                clean = self.check(body)
                self.assertEqual(clean.returncode, 0, clean.stderr)
                result = self.check(body.replace(wheel, replacement))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"the reviewed {named}-", result.stderr)

    def test_a_lock_named_for_another_commit_is_refused(self):
        # The provenance header is the only thing tying these hashes to a release, so the commit it
        # claims is checked before a single wheel is installed. `check` would keep the two agreeing,
        # which is the point of every other test here, so this one writes the block by hand.
        text = self.header(C38_COMMIT, C38_SHA) + TORCH_WHEEL
        result = self.run_block(text, "4" * 40, C38_SHA)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
