"""Browser selection probes addresses before opening authenticated URLs."""

import importlib.util
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("browser", ROOT / "tools/open_kimi_browser.py")
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)
LOCAL = "http://localhost:5494/#token=fixture"
NETWORK = "http://172.22.0.2:5494/#token=fixture"


class BrowserTests(unittest.TestCase):
    def test_banner_order_local_first_and_ignores_unrelated_links(self):
        logs = (f"kimi-agent | Network: {NETWORK}\n"
                f"kimi-agent | Local: \x1b[32m{LOCAL}\x1b[0m\n"
                "kimi-agent | Network: http://172.24.0.3:5494/#token=fixture\n"
                "kimi-agent | Local: http://example.com:5494/#token=fixture\n"
                f"kimi-agent | Local: {LOCAL}\n")
        self.assertEqual(
            browser.advertised_urls(logs),
            [LOCAL, NETWORK, "http://172.24.0.3:5494/#token=fixture"],
        )

    def test_local_unreachable_tries_network_without_sending_token(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(browser.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = [
                urllib.error.URLError("unreachable"),
                response,
                response,
            ]
            self.assertEqual(browser.first_reachable([LOCAL, NETWORK]), NETWORK)
            self.assertEqual([c.args[0] for c in build.return_value.open.call_args_list], [
                "http://localhost:5494/api/v1/healthz",
                "http://172.22.0.2:5494/api/v1/healthz", "http://172.22.0.2:5494/",
            ])
            self.assertEqual(build.call_args.args[0].proxies, {})

    def test_local_success_does_not_probe_network(self):
        with patch.object(browser.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.status = 200
            self.assertEqual(browser.first_reachable([LOCAL, NETWORK]), LOCAL)
            self.assertEqual(build.return_value.open.call_count, 2)

    def test_all_unreachable_never_opens_browser(self):
        with (
            patch.object(browser.subprocess, "run") as run,
            patch.object(browser, "first_reachable", return_value=None),
            patch.object(browser, "open_browser") as open_tab,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            run.return_value.stdout = f"Local: {LOCAL}"
            self.assertEqual(browser.main(), 1)
            open_tab.assert_not_called()

    def test_mac_opener_receives_fragment_without_shell(self):
        with (
            patch.object(browser.sys, "platform", "darwin"),
            patch.object(browser.shutil, "which", return_value="/usr/bin/open"),
            patch.object(browser.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertTrue(browser.open_browser(LOCAL))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", LOCAL])
            self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
