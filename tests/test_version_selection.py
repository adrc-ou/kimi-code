import importlib.util
import os
import ssl
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_versions", ROOT / "scripts" / "select_versions.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VersionSelectionTests(unittest.TestCase):
    def test_macos_empty_trust_store_loads_system_bundle(self):
        context = mock.Mock()
        context.cert_store_stats.return_value = {"x509_ca": 0}
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MODULE.sys, "platform", "darwin"),
            mock.patch.object(MODULE.ssl, "create_default_context", return_value=context),
            mock.patch.object(Path, "is_file", return_value=True),
        ):
            self.assertIs(MODULE.release_ssl_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_system_fallback_preserves_existing_or_explicit_trust(self):
        for platform, ca_count, environment in (
            ("linux", 0, {}),
            ("darwin", 1, {}),
            ("darwin", 0, {"SSL_CERT_FILE": "/custom/ca.pem"}),
            ("darwin", 0, {"SSL_CERT_DIR": "/custom/certs"}),
        ):
            with self.subTest(platform=platform, ca_count=ca_count, environment=environment):
                context = mock.Mock()
                context.cert_store_stats.return_value = {"x509_ca": ca_count}
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.object(MODULE.sys, "platform", platform),
                    mock.patch.object(MODULE.ssl, "create_default_context", return_value=context),
                    mock.patch.object(Path, "is_file", return_value=True),
                ):
                    self.assertIs(MODULE.release_ssl_context(), context)
                context.load_verify_locations.assert_not_called()

    def test_missing_system_bundle_does_not_disable_verification(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MODULE.sys, "platform", "darwin"),
            mock.patch.object(MODULE.ssl, "create_default_context", return_value=context),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            actual = MODULE.release_ssl_context()
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual.check_hostname)
        self.assertEqual(actual.cert_store_stats()["x509_ca"], 0)

    def test_both_downloads_use_verified_context_and_reject_bad_certificates(self):
        for download, target in (
            (MODULE.github_json, "/repos/MoonshotAI/kimi-code/releases/latest"),
            (MODULE.read_text_url, "https://example.invalid/checksum.sha256"),
        ):
            with self.subTest(download=download.__name__):
                context = ssl.create_default_context()
                failure = urllib.error.URLError(
                    ssl.SSLCertVerificationError(1, "certificate verify failed")
                )
                with (
                    mock.patch.object(MODULE, "release_ssl_context", return_value=context),
                    mock.patch.object(
                        MODULE.urllib.request, "urlopen", side_effect=failure
                    ) as fetch,
                    self.assertRaisesRegex(SystemExit, "certificate verify failed"),
                ):
                    download(target)
                fetch.assert_called_once()
                self.assertIs(fetch.call_args.kwargs["context"], context)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)

    def test_catalog_contains_only_locked_platform_entries(self):
        catalog, latest = MODULE.comfy_catalog("wsl2-x86_64")
        self.assertTrue(catalog)
        self.assertEqual(catalog[0]["version"], latest)
        self.assertRegex(catalog[0]["commit"], r"^[0-9a-f]{40}$")

    def test_installed_version_replaces_tenth_choice(self):
        catalog = [{"version": f"1.{minor}.0"} for minor in range(20, 0, -1)]
        choices = MODULE.visible_choices(catalog, "1.1.0")
        self.assertEqual(len(choices), 10)
        self.assertIn("1.1.0", {item["version"] for item in choices})
        self.assertNotIn("1.11.0", {item["version"] for item in choices})
        self.assertEqual(
            [item["version"] for item in choices],
            [f"1.{minor}.0" for minor in range(20, 11, -1)] + ["1.1.0"],
        )

    def test_existing_recent_install_does_not_expand_list(self):
        catalog = [{"version": f"2.{minor}.0"} for minor in range(12, 0, -1)]
        choices = MODULE.visible_choices(catalog, "2.11.0")
        self.assertEqual(len(choices), 10)
        self.assertEqual(choices[0]["version"], "2.12.0")
        self.assertEqual(choices[1]["version"], "2.11.0")

    def test_semver_sort_is_numeric(self):
        catalog = [
            {"version": "1.9.0"},
            {"version": "1.11.0"},
            {"version": "1.10.0"},
        ]
        choices = MODULE.visible_choices(catalog, "")
        self.assertEqual(
            [item["version"] for item in choices],
            ["1.11.0", "1.10.0", "1.9.0"],
        )


if __name__ == "__main__":
    unittest.main()
