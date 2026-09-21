"""The host-side opener probes before it opens, and only ever opens a loopback origin."""

import importlib.util
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "open_comfy_frontend", ROOT / "scripts" / "open_comfy_frontend.py"
)
assert SPEC and SPEC.loader
OPENER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPENER)

LOOPBACK = "http://127.0.0.1:8188"


def ready_response():
    response = MagicMock()
    response.__enter__.return_value.status = 200
    return response


class LoopbackTests(unittest.TestCase):
    def test_local_origins_are_openable(self):
        for url in (LOOPBACK, "http://localhost:8188", "http://[::1]:8188"):
            with self.subTest(url=url):
                self.assertTrue(OPENER.is_loopback(url))

    def test_nothing_but_this_machine_is_openable(self):
        for url in (
            "http://192.168.1.7:8188",  # somebody else's LAN
            "http://comfyui-ui:8188",  # a container's private network
            "http://example.com:8188",
            "https://127.0.0.1:8188",  # not what ComfyUI serves on loopback
            "http://user:pass@127.0.0.1:8188",  # a credential hidden in a URL is not a preference
            "http://127.0.0.1.evil.example:8188",
            "file:///etc/hosts",
        ):
            with self.subTest(url=url):
                self.assertFalse(OPENER.is_loopback(url))


class ServingTests(unittest.TestCase):
    def test_the_probe_ignores_the_environment_proxy_and_follows_no_redirect(self):
        with patch.object(OPENER.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = ready_response()
            self.assertTrue(OPENER.is_serving(LOOPBACK, 5))
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], OPENER.NoRedirect)
            self.assertEqual(
                build.return_value.open.call_args.args[0], "http://127.0.0.1:8188/system_stats"
            )

    def test_an_unreachable_origin_is_reported_rather_than_raised(self):
        with patch.object(OPENER.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("refused")
            self.assertFalse(OPENER.is_serving(LOOPBACK, 5))


class OpenerTests(unittest.TestCase):
    def test_a_remote_url_is_refused_without_touching_the_browser(self):
        with (
            patch.object(OPENER, "open_browser") as open_tab,
            patch.object(OPENER, "is_serving", return_value=True),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(OPENER.main(["--url", "http://10.0.0.5:8188"]), 1)
            open_tab.assert_not_called()

    def test_nothing_is_opened_before_the_frontend_answers(self):
        with (
            patch.object(OPENER, "open_browser") as open_tab,
            patch.object(OPENER, "is_serving", return_value=False),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(OPENER.main(["--url", LOOPBACK]), 1)
            open_tab.assert_not_called()

    def test_mac_opens_the_url_without_a_shell(self):
        with (
            patch.object(OPENER.sys, "platform", "darwin"),
            patch.object(OPENER.shutil, "which", return_value="/usr/bin/open"),
            patch.object(OPENER.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertTrue(OPENER.open_browser(LOOPBACK))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", LOOPBACK])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_a_linux_host_tries_the_wsl_handler_before_its_own(self):
        with (
            patch.object(OPENER.sys, "platform", "linux"),
            patch.object(OPENER.shutil, "which", return_value=None) as which,
            patch.object(OPENER.subprocess, "run") as run,
        ):
            which.side_effect = lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None
            run.return_value.returncode = 0
            self.assertTrue(OPENER.open_browser(LOOPBACK))
            self.assertEqual(which.call_args_list[-1].args[0], "xdg-open")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/xdg-open", LOOPBACK])

    def test_a_desktop_handler_that_fails_is_reported_not_raised(self):
        with (
            patch.object(OPENER.sys, "platform", "darwin"),
            patch.object(OPENER.shutil, "which", return_value="/usr/bin/open"),
            patch.object(OPENER.subprocess, "run", side_effect=OSError("no window server")),
        ):
            self.assertFalse(OPENER.open_browser(LOOPBACK))

    def test_a_host_with_no_desktop_handler_is_simply_unable(self):
        # Windows, and any headless box: no candidate exists, so this is a clean false rather than
        # an error, which is what lets the launcher treat opening as optional.
        with (
            patch.object(OPENER.sys, "platform", "win32"),
            patch.object(OPENER.shutil, "which", return_value=None),
            patch.object(OPENER.subprocess, "run") as run,
        ):
            self.assertFalse(OPENER.open_browser(LOOPBACK))
            run.assert_not_called()

    def test_a_failing_launcher_leaves_the_url_for_the_operator(self):
        with (
            patch.object(OPENER, "is_serving", return_value=True),
            patch.object(OPENER, "open_browser", return_value=False),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(OPENER.main(["--url", LOOPBACK]), 1)
            self.assertIn(LOOPBACK, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
