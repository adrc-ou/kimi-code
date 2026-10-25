"""Host capability gating: which models a given machine is allowed to be shown.

The rule these tests exist to hold is the one the operator sees: a model this machine cannot run
is never mentioned, not offered and refused. Everything else in the file is the arithmetic behind
that - the platform test, the two floors, and the filter that removes a provider and takes its
models with it.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import definitions  # noqa: E402
import host_caps  # noqa: E402

import models as model_step  # noqa: E402

M3_ULTRA = {
    "schema_version": 1,
    "system": "Darwin",
    "arch": "arm64",
    "memory_gib": 251,
    "free_disk_gib": 900,
    "capabilities": ["metal"],
}
M1_MAX = dict(M3_ULTRA, memory_gib=32, free_disk_gib=200)
THIS_CONTAINER = dict(M3_ULTRA, system="Linux", arch="aarch64", memory_gib=7, capabilities=[])
# A Mac mini at the vendor's stated floor for a 4-bit 27B, which this harness still refuses:
# the co-resident Docker VM is paid for out of the same unified memory.
MAC_24 = dict(M3_ULTRA, memory_gib=24)


class CapabilityTests(unittest.TestCase):
    def test_macos_available_memory_does_not_double_count_purgeable_pages(self):
        output = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free: 10.\nPages inactive: 20.\nPages purgeable: 100.\n"
        )
        result = mock.Mock(stdout=output)
        with mock.patch.object(host_caps.subprocess, "run", return_value=result):
            self.assertEqual(host_caps.available_memory_bytes("Darwin"), 30 * 16384)

    def test_unavailable_memory_probe_is_unknown_not_total_ram(self):
        with mock.patch.object(host_caps.subprocess, "run", side_effect=OSError):
            self.assertEqual(host_caps.available_memory_bytes("Darwin"), 0)

    def test_metal_is_apple_silicon_and_nothing_else(self):
        self.assertEqual(host_caps.host_capabilities("Darwin", "arm64"), ["metal"])
        for system, machine in (
            ("Darwin", "x86_64"),
            ("Linux", "aarch64"),
            ("Linux", "x86_64"),
            ("Windows", "AMD64"),
        ):
            with self.subTest(system=system, machine=machine):
                self.assertEqual(host_caps.host_capabilities(system, machine), [])

    def test_cuda_is_a_capability_the_schema_accepts_and_no_host_reports(self):
        # Named in the schema so a provider can require it, but nothing measures it until
        # local_cuda is built - and an unmeasured capability staying absent is what makes that
        # safe rather than optimistic.
        self.assertIn("cuda", definitions.HOST_CAPABILITIES)
        self.assertNotIn("cuda", THIS_CONTAINER["capabilities"])

    def test_a_provider_that_asks_for_nothing_is_supported_everywhere(self):
        self.assertTrue(host_caps.provider_supported({"requires": []}, THIS_CONTAINER))
        self.assertTrue(host_caps.provider_supported({}, THIS_CONTAINER))

    def test_a_provider_is_offered_only_where_its_capability_exists(self):
        local = {"requires": ["metal"]}
        self.assertTrue(host_caps.provider_supported(local, M3_ULTRA))
        self.assertFalse(host_caps.provider_supported(local, THIS_CONTAINER))

    def test_a_model_needs_both_its_floors_met(self):
        big = {"requires": {"memory_gib": 160, "disk_gib": 130}}
        small = {"requires": {"memory_gib": 32, "disk_gib": 24}}
        self.assertTrue(host_caps.model_supported(big, M3_ULTRA))
        self.assertFalse(host_caps.model_supported(big, M1_MAX))
        self.assertTrue(host_caps.model_supported(small, M1_MAX))
        self.assertFalse(host_caps.model_supported(small, THIS_CONTAINER))

    def test_a_measurement_of_zero_refuses_anything_asking(self):
        unknown = dict(M3_ULTRA, memory_gib=0, free_disk_gib=0)
        self.assertFalse(host_caps.model_supported({"requires": {"memory_gib": 1}}, unknown))
        self.assertTrue(host_caps.model_supported({"requires": {"memory_gib": 0}}, unknown))


class CatalogFilterTests(unittest.TestCase):
    """The filter the picker runs, against the definitions this repository ships."""

    @classmethod
    def setUpClass(cls):
        cls.providers, cls.defined = definitions.load_definitions(ROOT)

    def rows_for(self, measured):
        providers, usable = host_caps.filter_definitions(self.providers, self.defined, measured)
        return providers, [
            model_step.row_label(row) for row in model_step.lane_rows(usable, "primary")
        ]

    def test_a_cloud_only_host_never_hears_about_local_models(self):
        providers, rows = self.rows_for(THIS_CONTAINER)
        self.assertEqual(list(providers), ["nrp"])
        self.assertNotIn("local_mps", providers)
        self.assertNotIn("Local (MPS)", " ".join(rows))
        self.assertTrue(rows, "the picker would be empty, which is not the same as cloud-only")

    def test_the_m3_ultra_is_offered_everything(self):
        _, rows = self.rows_for(M3_ULTRA)
        # Native local windows do not depend on RAM. NRP keeps its policy labels.
        self.assertEqual(
            rows,
            [
                "Qwen3.8-27B - Local (native 256k)",
                "Qwen3.8-Flash-Next - Local (native 256k)",
                "Qwen3.8-Flash-Next - Long (queued, 1M window)",
                "Qwen3.8-Flash-Next - Medium (concurrent)",
            ],
        )

    def test_the_m1_max_gets_the_27b_and_not_the_local_moe(self):
        _, rows = self.rows_for(M1_MAX)
        # Installed-memory eligibility is distinct from available-memory fitting.
        self.assertEqual(
            rows,
            [
                "Qwen3.8-27B - Local (native 256k)",
                "Qwen3.8-Flash-Next - Long (queued, 1M window)",
                "Qwen3.8-Flash-Next - Medium (concurrent)",
            ],
        )

    def test_the_two_servings_of_one_model_are_told_apart_by_their_provider(self):
        # The Model column alone cannot answer which one is being launched, and the Provider
        # column is what answers it: two rows share the label and differ only in who serves them.
        _, usable = host_caps.filter_definitions(self.providers, self.defined, M3_ULTRA)
        rows = model_step.lane_rows(usable, "primary")
        cells = [model_step._option(row, "", "").cells[:3] for row in rows]
        self.assertEqual(len(set(cells)), len(cells), "two rows spell the identical row")
        flash = [cell for cell in cells if cell[0] == "Qwen3.8-Flash-Next"]
        self.assertEqual({cell[1] for cell in flash}, {"NRP", "Local (MPS)"})
        # Sorted by label then qualified id, so one model's servings sit together and a
        # provider's rows are stable next to each other rather than interleaved by directory.
        # Two of these rows share both their model and their lane, so the Provider column is the
        # Local native capacity is provisional; the remote lanes keep their fixed envelopes.
        self.assertEqual(
            flash,
            [
                ("Qwen3.8-Flash-Next", "Local (MPS)", "Local (native 256k)"),
                ("Qwen3.8-Flash-Next", "NRP", "Long (queued, 1M window)"),
                ("Qwen3.8-Flash-Next", "NRP", "Medium (concurrent)"),
            ],
        )

    def test_a_smaller_mac_is_below_both_local_hardware_floors(self):
        _, rows = self.rows_for(MAC_24)
        self.assertNotIn("Qwen3.8-27B - Local (native 256k)", rows)
        self.assertNotIn("Qwen3.8-Flash-Next - Local (native 256k)", rows)

    def test_the_cache_salt_and_port_survive_the_filter(self):
        providers, _ = host_caps.filter_definitions(self.providers, self.defined, M3_ULTRA)
        self.assertFalse(providers["local_mps"]["sends_cache_salt"])
        self.assertTrue(providers["nrp"]["sends_cache_salt"])


class LauncherCapsFileTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.runtime = base / "runtime"
        self.runtime.mkdir()

    def test_the_cached_probe_is_what_the_picker_reads(self):
        (self.runtime / host_caps.CAPS_FILE).write_text(json.dumps(M1_MAX))
        self.assertEqual(host_caps.caps(self.runtime)["memory_gib"], 32)

    def test_a_missing_or_unusable_cache_probes_rather_than_guessing(self):
        for payload in ("", "not json", "{}", '{"schema_version": 9}', '{"capabilities": "metal"}'):
            path = self.runtime / host_caps.CAPS_FILE
            path.write_text(payload)
            with self.subTest(payload=payload):
                measured = host_caps.caps(self.runtime)
                self.assertIn("capabilities", measured)
                self.assertIsInstance(measured["capabilities"], list)
            path.unlink()
        self.assertIsInstance(host_caps.caps(self.runtime)["capabilities"], list)

    def test_a_probe_of_this_machine_reports_no_accelerator_it_cannot_use(self):
        measured = host_caps.probe()
        if measured["system"] != "Darwin" or measured["arch"] != "arm64":
            self.assertNotIn("metal", measured["capabilities"])
        self.assertGreaterEqual(measured["memory_gib"], 0)
        # The summary is the line the launch prints; it must not crash on a zero measurement.
        self.assertIn("Host:", host_caps.summary(dict(measured, memory_gib=0)))
