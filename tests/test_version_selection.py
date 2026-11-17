import importlib.util
import os
import re
import ssl
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_versions", ROOT / "scripts" / "select_versions.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ASSET = "kimi-code-linux-x64.tar.gz"
DOWNLOAD_PREFIX = "https://github.com/MoonshotAI/kimi-code/releases/download/"
DIGEST = "sha256:" + "ab" * 32


def release(version: str, *, prerelease: bool = False, asset: str | None = ASSET) -> dict:
    """One release document, shaped like the paginated listing returns it."""
    return {
        "tag_name": f"@moonshot-ai/kimi-code@{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": []
        if asset is None
        else [
            {
                "name": asset,
                "browser_download_url": f"{DOWNLOAD_PREFIX}{version}/{asset}",
                "digest": DIGEST,
            }
        ],
    }


def releases(versions, **kwargs: Any) -> list[dict]:
    return [release(version, **kwargs) for version in versions]


def stable_page(start: int, count: int = 30, **kwargs: Any) -> list[dict]:
    """A page of ``3.x.0`` releases descending from ``3.{start}.0``, newest first.

    Successive pages take a descending ``start`` so the second page is older than the first in
    both publish date and version, which is the ordering the real listing returns and the one the
    walk's stop condition reasons about.
    """
    return releases([f"3.{start - n}.0" for n in range(count)], **kwargs)


class FakeGitHub:
    """Answers the listing endpoint from fixed pages and records every URL asked for.

    What a walk costs is how many requests it makes, so the request log is the assertion most of
    these cases really want, and a page that was never asked for is a page that cannot be examined
    any other way.
    """

    def __init__(self, pages: list[list[dict]], latest: str = "3.60.0") -> None:
        self.pages = pages
        self.latest = latest
        self.urls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.urls.append(path)
        if path.endswith("/releases/latest"):
            return {"tag_name": f"@moonshot-ai/kimi-code@{self.latest}"}
        page = int(re.search(r"[?&]page=(\d+)", path).group(1))
        return self.pages[page - 1] if page <= len(self.pages) else []

    @property
    def listing_requests(self) -> list[str]:
        return [url for url in self.urls if "/releases?" in url]

    def run(self, wanted: list[str] = (), asset: str = ASSET):
        with mock.patch.object(MODULE, "github_json", self):
            return MODULE.kimi_catalog(asset, wanted=wanted)


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

    def test_installed_version_adds_an_eleventh_choice(self):
        # The version a launch is already running is never the row that gets dropped to keep the
        # window at ten: shortening the list would hide the newest release instead of the one the
        # operator is using, so an older install is appended and the menu is one row longer.
        catalog = [{"version": f"1.{minor}.0"} for minor in range(20, 0, -1)]
        choices = MODULE.visible_choices(catalog, "1.1.0")
        self.assertEqual(
            [item["version"] for item in choices],
            [f"1.{minor}.0" for minor in range(20, 10, -1)] + ["1.1.0"],
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


class ReleaseWalkTests(unittest.TestCase):
    """Reading history in pages, so a small response limit cannot hide an old release.

    One page has to stay small: the listing carries every asset of every release, and the whole
    set no longer fits the selector's own response cap. What the walk owes in exchange is that
    depth costs requests rather than availability, and that the ordinary launch still spends one.
    """

    def test_a_page_below_the_window_floor_ends_the_walk_though_more_exist(self):
        # Thirty usable releases is three times the menu, and the oldest of them sits below the
        # eleventh, so nothing deeper could displace a row. Reading on would be a request spent to
        # learn nothing, which is the cost this walk is supposed to avoid rather than cause.
        github = FakeGitHub([stable_page(60), stable_page(30)])
        catalog, _ = github.run()
        self.assertEqual(len(github.listing_requests), 1)
        self.assertEqual(len(catalog), 30)
        self.assertEqual(len(MODULE.visible_choices(catalog, "")), MODULE.RECENT_RELEASES)

    def test_a_page_of_canaries_offers_nothing_so_the_walk_continues(self):
        # A full page of prereleases fills no menu row, and a listing busy with canaries is exactly
        # where stopping at "enough releases came back" would leave nothing to select at all.
        canaries = releases([f"3.{60 - n}.0-canary.{n}" for n in range(30)], prerelease=True)
        github = FakeGitHub([canaries, stable_page(30, 20)])
        catalog, _ = github.run()
        self.assertEqual(len(github.listing_requests), 2)
        self.assertEqual(len(catalog), 20)

    def test_a_pinned_version_below_the_window_is_walked_to(self):
        # The point of the walk. Before it, a pin to a release older than one page was a launch
        # failure with a message claiming the version did not exist, and the README's own
        # non-interactive example was a few releases from becoming that.
        github = FakeGitHub([stable_page(60), stable_page(30)])
        pinned = "3.25.0"
        catalog, _ = github.run(wanted=[pinned])
        self.assertEqual(len(github.listing_requests), 2)
        self.assertIn(pinned, [item["version"] for item in catalog])
        # And it is reachable in the menu as the appended row, not merely present in the catalogue.
        choices = MODULE.visible_choices(catalog, pinned)
        self.assertEqual(len(choices), MODULE.RECENT_RELEASES + 1)
        self.assertEqual(choices[-1]["version"], pinned)

    def test_the_walk_stops_at_the_end_of_the_listing(self):
        # A short page is the last page however hard the walk wants more, so an impossible pin costs
        # the history and then reports, rather than paginating forever.
        github = FakeGitHub([stable_page(60), stable_page(5, 5)])
        catalog, _ = github.run(wanted=["0.0.1"])
        self.assertEqual(len(github.listing_requests), 2)
        self.assertNotIn("0.0.1", [item["version"] for item in catalog])

    def test_the_walk_is_bounded_however_many_pages_remain(self):
        # Every page here is full and the pin is on none of them, so only the bound ends this. The
        # bound exists because an unbounded one is a wrong environment variable spent against the
        # API's rate limit instead of answered on the first page.
        github = FakeGitHub([stable_page(300 - n * 30) for n in range(20)])
        catalog, _ = github.run(wanted=["0.0.1"])
        self.assertEqual(len(github.listing_requests), MODULE.MAX_RELEASE_PAGES)
        self.assertTrue(catalog)

    def test_pages_are_requested_in_order(self):
        github = FakeGitHub([stable_page(300 - n * 30) for n in range(9)])
        github.run(wanted=["0.0.1"])
        asked = [int(page) for page in re.findall(r"[?&]page=(\d+)", "\n".join(github.urls))]
        self.assertEqual(asked, list(range(1, MODULE.MAX_RELEASE_PAGES + 1)))

    def test_a_release_published_between_pages_is_not_offered_twice(self):
        # Page boundaries shift when a release lands mid-walk, which repeats a row at the top of the
        # next page. Every later consumer indexes by version, so the duplicate would be silent until
        # the digest disagreed with the URL beside it.
        first = stable_page(60)
        second = [first[0], *stable_page(30)]
        github = FakeGitHub([first, second])
        catalog, _ = github.run(wanted=["3.25.0"])
        versions = [item["version"] for item in catalog]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertEqual(len(versions), 60)

    def test_a_release_without_this_platforms_asset_is_skipped(self):
        # Ordinary shapes of the upstream listing, each of which must cost no failure and no extra
        # page: an offer for another platform, and a tag served from somewhere else entirely.
        github = FakeGitHub([[*releases(["3.60.0"], asset=None), *stable_page(59)]])
        catalog, _ = github.run()
        self.assertNotIn("3.60.0", [item["version"] for item in catalog])
        self.assertEqual(len(github.listing_requests), 1)

    def test_a_release_url_outside_the_repository_refuses_the_launch(self):
        # Skipping would be the wrong reflex here: a well-formed tag served from somewhere else is
        # the shape of a hijacked release, and this selector is what stands between it and a
        # container build that trusts the digest it reports.
        misplaced = {
            "tag_name": "@moonshot-ai/kimi-code@3.60.0",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": ASSET, "browser_download_url": "https://mirror.invalid/x"}],
        }
        github = FakeGitHub([[misplaced, *stable_page(59)]])
        with self.assertRaisesRegex(SystemExit, "Unexpected Kimi release URL"):
            github.run()

    def test_page_position_ignores_foreign_tags_and_counts_canary_heads(self):
        foreign = [{"tag_name": "v1.0.0"}, {"tag_name": "tooling-9"}]
        self.assertIsNone(MODULE.oldest_tag_key(foreign))
        # A page that answers nothing must not read as "below the floor", or a selector that has not
        # yet seen one usable release would stop and report that none exist.
        canary = releases(["2.6.0-canary.20261113055733-391d9e7"], prerelease=True)
        self.assertEqual(MODULE.oldest_tag_key(canary), (2, 6, 0))


if __name__ == "__main__":
    unittest.main()
