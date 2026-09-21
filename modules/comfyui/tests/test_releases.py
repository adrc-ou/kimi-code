"""The fetched ComfyUI release catalog: ordering, provenance, and offline refusal.

The catalog is no longer a list this repository transcribes, so these tests supply the two inputs
the layer actually reads — a backend profile and a release cache — and assert what the launcher
would offer. Nothing here reaches the network: the cache answers in place of the source, and where
the fetch itself is the thing under test it is patched at the one function that talks to GitHub.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modules.comfyui import versions

RELEASES = versions.releases
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
DIGEST = "c" * 64
OTHER_DIGEST = "d" * 64
REPOSITORY = "Comfy-Org/ComfyUI"
NOW = 1_800_000_000.0
# The one seam that touches the network, patched whenever a test wants the source unreachable
# without also wanting to prove that the code above it never asked.
UNREACHABLE = mock.patch.object(RELEASES, "_fetch_releases", side_effect=SystemExit("unreachable"))


class ReleaseCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.cache = self.directory / "releases.json"

    def profile_path(self, platforms: list[dict], records: list[dict] | None = None) -> Path:
        path = self.directory / "compatibility.json"
        path.write_text(
            json.dumps({"schema_version": 2, "platforms": platforms, "records": records or []}),
            encoding="utf-8",
        )
        return path

    def linux(self, baseline: dict | None = ..., records: list[dict] | None = None) -> Path:
        """A one-platform profile, with the default baseline unless one is given."""
        entry = {"platform": "linux-x86_64", "repository": REPOSITORY}
        if baseline is ...:
            entry["baseline"] = self.record("v0.9.0", COMMIT, DIGEST)
        elif baseline is not None:
            entry["baseline"] = baseline
        return self.profile_path([entry], records)

    @staticmethod
    def record(version: str, commit: str, digest: str, status: str = "locked") -> dict:
        return {
            "comfyui_version": version,
            "comfyui_commit": commit,
            "requirements_sha256": digest,
            "status": status,
        }

    def cached(self, listed: list[str], *, age: float = 0.0, repository=REPOSITORY) -> None:
        self.cache.write_text(
            json.dumps(
                {
                    "repository": repository,
                    "fetched_at": NOW - age,
                    "releases": listed,
                    "requirements": {},
                }
            ),
            encoding="utf-8",
        )

    def catalog(self, path: Path, *, now: float = NOW):
        return RELEASES.catalog_for("linux-x86_64", profile_path=path, cache=self.cache, now=now)

    # --- the listing is the catalog ---

    def test_catalog_offers_what_the_source_lists(self):
        path = self.linux()
        self.cached(["v0.10.0", "v0.9.0"])
        catalog, latest = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0", "v0.9.0"])
        self.assertEqual(latest, "v0.10.0")
        # A release this harness has no record of is still a row: it is what the menu is for. The
        # commit is looked up only once the row is chosen, so offering it costs nothing.
        self.assertNotIn("commit", catalog[0])
        self.assertEqual([item["provenance"] for item in catalog], ["listed", "listed"])

    def test_catalog_orders_by_semver_not_by_string(self):
        # A lexicographic sort puts v0.9.0 above v0.10.0, and the menu would then name the wrong
        # release "latest" with nothing else in the launcher noticing. The cache is fed in the wrong
        # order on purpose: the listing is not trusted to arrive sorted.
        path = self.linux(baseline=None)
        self.cached(["v0.9.0", "v0.10.0"])
        catalog, latest = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0", "v0.9.0"])
        self.assertEqual(latest, "v0.10.0")

    def test_a_fresh_cache_answers_without_reaching_the_source(self):
        # This is the whole reason for caching: a launch that keeps its installed version should not
        # spend a request on GitHub, and it must not matter whether GitHub is up.
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"])
        with UNREACHABLE as fetch:
            catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0"])
        fetch.assert_not_called()

    def test_a_recorded_release_outside_the_listing_keeps_its_identity(self):
        # The baseline is the release the shipped lock was built from, and a listing that has
        # moved on cannot make that release uninstallable — it is the one row whose commit is
        # already known.
        path = self.linux()
        self.cached(["v0.10.0"])
        catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0", "v0.9.0"])
        self.assertEqual(catalog[1]["commit"], COMMIT)
        self.assertEqual(catalog[1]["requirements_sha256"], DIGEST)

    # --- provenance: how far a row can be trusted ---

    def test_an_unreachable_source_falls_back_to_what_was_cached_earlier(self):
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"], age=RELEASES.LISTING_TTL_SECONDS * 3)
        with UNREACHABLE:
            catalog, _ = self.catalog(path)
        self.assertEqual([item["provenance"] for item in catalog], ["stale"])

    def test_no_source_and_no_cache_offers_only_recorded_releases(self):
        path = self.linux()
        with UNREACHABLE:
            catalog, latest = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.9.0"])
        self.assertEqual(latest, "v0.9.0")
        self.assertEqual([item["provenance"] for item in catalog], ["local"])

    def test_nothing_installable_refuses_rather_than_offering_an_empty_menu(self):
        path = self.linux(baseline=None)
        with UNREACHABLE, self.assertRaisesRegex(SystemExit, "No compatible ComfyUI releases"):
            self.catalog(path)

    def test_a_record_without_an_immutable_commit_is_never_offered(self):
        # A floating ref is not a reproducible backend. It is dropped rather than fatal, because one
        # malformed record must not take the whole offline fallback with it — but that also means it
        # cannot come back as a row, so with nothing else left the menu refuses.
        path = self.linux(self.record("v0.3.0", "main", DIGEST))
        with UNREACHABLE, self.assertRaises(SystemExit):
            self.catalog(path)

    def test_a_pending_certification_is_not_offline_installable(self):
        # status is the document's own claim about whether this release was actually run; a release
        # merely recorded as pending hardware cannot be the fallback the launcher trusts.
        path = self.linux(self.record("v0.9.0", COMMIT, DIGEST, status="pending"))
        with UNREACHABLE, self.assertRaises(SystemExit):
            self.catalog(path)

    def test_a_record_without_a_requirements_digest_is_not_offered(self):
        # The digest is what the dependency lock is keyed by. Without it the row would resolve to a
        # commit this harness has never matched to a dependency set.
        path = self.linux(
            {
                "comfyui_version": "v0.9.0",
                "comfyui_commit": COMMIT,
                "requirements_sha256": "not-a-digest",
                "status": "locked",
            }
        )
        with UNREACHABLE, self.assertRaises(SystemExit):
            self.catalog(path)

    def test_a_shared_record_appears_under_the_platform_that_offers_it(self):
        # A certification run that did not care about the host records one release for every
        # platform; it has to show up here even though the upstream listing, not the document, is
        # what put it on the menu.
        record = self.record("v0.8.0", OTHER_COMMIT, OTHER_DIGEST)
        path = self.linux(baseline=None, records=[record])
        self.cached(["v0.8.0"])
        catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.8.0"])
        self.assertEqual(catalog[0]["commit"], OTHER_COMMIT)
        # Listed now, so it is not the fallback list — even though its identity came from a record.
        self.assertEqual(catalog[0]["provenance"], "listed")

    # --- the platform, not the caller, decides what is installable ---

    def test_an_excluded_platform_refuses_instead_of_offering_nothing(self):
        # Returning an empty catalog would let the launcher reach a menu with no rows in it and
        # report that as "no releases found" rather than as the misconfiguration it is.
        path = self.linux()
        with self.assertRaisesRegex(SystemExit, "No ComfyUI backend profile is declared"):
            RELEASES.catalog_for("darwin-x86_64", profile_path=path, cache=self.cache)

    def test_a_profile_that_names_no_repository_is_refused(self):
        # Silently reaching some default repository would be worse than stopping: the releases
        # offered would be someone else's.
        path = self.profile_path([{"platform": "linux-x86_64"}])
        with self.assertRaisesRegex(SystemExit, "names no repository"):
            RELEASES.catalog_for("linux-x86_64", profile_path=path, cache=self.cache)

    def test_an_empty_profile_document_is_refused(self):
        path = self.directory / "compatibility.json"
        path.write_text(json.dumps({"schema_version": 2, "platforms": []}), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "declares no platforms"):
            RELEASES.catalog_for("linux-x86_64", profile_path=path, cache=self.cache)

    # --- resolution: deferred to the row that was actually chosen ---

    def test_resolve_names_the_commit_and_digest_of_a_listed_release(self):
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"])
        catalog, _ = self.catalog(path)
        with (
            mock.patch.object(RELEASES, "_tag_commit", return_value=OTHER_COMMIT) as tag,
            mock.patch.object(
                RELEASES, "_requirements_digest", return_value=OTHER_DIGEST
            ) as digest,
        ):
            chosen = RELEASES.resolve(catalog[0], "linux-x86_64", profile_path=path)
        self.assertEqual(chosen["commit"], OTHER_COMMIT)
        self.assertEqual(chosen["requirements_sha256"], OTHER_DIGEST)
        # Scrolling past a release must not cost a lookup for it, so one row is resolved per launch.
        tag.assert_called_once_with(REPOSITORY, "v0.10.0")
        digest.assert_called_once()

    def test_resolve_does_not_re_look_up_what_the_profile_already_knows(self):
        path = self.linux()
        self.cached(["v0.10.0"])
        catalog, _ = self.catalog(path)
        with mock.patch.object(RELEASES, "_tag_commit", side_effect=AssertionError("network")):
            chosen = RELEASES.resolve(catalog[-1], "linux-x86_64", profile_path=path)
        self.assertEqual(chosen["commit"], COMMIT)

    def test_the_digest_of_a_commit_is_remembered_across_launches(self):
        # Requirements at a given commit do not change, so learning the digest once has to be
        # enough forever — and it must not do it by making the release listing look fresh again.
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"])
        body = "torch==2.11.0\n"
        with (
            mock.patch.object(RELEASES, "_tag_commit", return_value=OTHER_COMMIT),
            mock.patch.object(RELEASES, "read_text_url", return_value=body) as read,
        ):
            first = RELEASES.resolve(
                {"version": "v0.10.0"}, "linux-x86_64", profile_path=path, cache=self.cache, now=NOW
            )
            second = RELEASES.resolve(
                {"version": "v0.10.0"}, "linux-x86_64", profile_path=path, cache=self.cache, now=NOW
            )
        self.assertEqual(first["requirements_sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(second, first)
        read.assert_called_once()
        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(stored["requirements"][OTHER_COMMIT], first["requirements_sha256"])
        self.assertEqual(stored["fetched_at"], NOW)

    def test_a_release_with_no_lock_and_no_way_to_read_its_requirements_is_refused(self):
        # The honest answer: this harness can install a release whose dependency set it can name.
        # With neither a cache nor a reachable source, offering the row would fail later, less
        # legibly, inside the installer.
        path = self.linux(baseline=None)
        with (
            mock.patch.object(RELEASES, "_tag_commit", return_value=OTHER_COMMIT),
            mock.patch.object(RELEASES, "read_text_url", side_effect=SystemExit("unreachable")),
            self.assertRaisesRegex(SystemExit, "cannot be installed without"),
        ):
            RELEASES.resolve(
                {"version": "v0.10.0"}, "linux-x86_64", profile_path=path, cache=self.cache
            )

    def test_the_baseline_digest_still_answers_when_the_source_is_down(self):
        # The shipped lock was built against one specific tree, so that tree's digest is known
        # without fetching it — which is what lets a launch keep its installed release offline.
        path = self.linux()
        with mock.patch.object(RELEASES, "read_text_url", side_effect=SystemExit("unreachable")):
            chosen = RELEASES.resolve(
                {"version": "v0.9.0", "commit": COMMIT},
                "linux-x86_64",
                profile_path=path,
                cache=self.cache,
            )
        self.assertEqual(chosen["requirements_sha256"], DIGEST)

    def test_a_digest_is_not_borrowed_from_the_baseline_for_another_commit(self):
        # The offline relief above is scoped to the one tree the shipped lock was built from. If it
        # applied to any commit, every unreachable launch would install v0.9.0's dependencies under
        # some other release's name.
        path = self.linux()
        with (
            mock.patch.object(RELEASES, "_tag_commit", return_value=OTHER_COMMIT),
            mock.patch.object(RELEASES, "read_text_url", side_effect=SystemExit("unreachable")),
            self.assertRaisesRegex(SystemExit, "cannot be installed without"),
        ):
            RELEASES.resolve(
                {"version": "v0.10.0"}, "linux-x86_64", profile_path=path, cache=self.cache
            )

    # --- the listing itself ---

    def test_drafts_prereleases_and_odd_tags_are_not_releases_to_install(self):
        releases = [
            {"tag_name": "v1.2.3"},
            {"tag_name": "v1.2.4", "draft": True},
            {"tag_name": "v1.2.5", "prerelease": True},
            {"tag_name": "nightly-2026-09-01"},
            {"tag_name": "v1.3"},
        ]
        with mock.patch.object(RELEASES, "github_json", return_value=releases):
            self.assertEqual(RELEASES._fetch_releases(REPOSITORY), ["v1.2.3"])

    def test_the_listing_is_bounded_however_many_releases_are_returned(self):
        # per_page=100 is the request, not the answer: an unbounded catalog would turn the menu into
        # a pager and the cache into a copy of the project's whole release history.
        releases = [{"tag_name": f"v0.{n}.0"} for n in range(1, 140)]
        with mock.patch.object(RELEASES, "github_json", return_value=releases):
            listed = RELEASES._fetch_releases(REPOSITORY)
        self.assertEqual(len(listed), RELEASES.LISTING_DEPTH)
        self.assertEqual(listed[0], "v0.139.0")

    def test_a_cache_for_another_repository_does_not_answer(self):
        # A profile that changed repository must not keep offering the old one's releases just
        # because a file with the right shape is lying around.
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"], repository="someone-else/ComfyUI")
        with mock.patch.object(RELEASES, "github_json", return_value=[{"tag_name": "v7.0.0"}]):
            catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v7.0.0"])

    def test_a_cache_that_cannot_be_read_is_no_answer_at_all(self):
        # A truncated or hand-edited file must not become a menu; it is treated as absent, which
        # sends the launch to the source, and to the recorded releases if that too fails.
        path = self.linux()
        self.cache.write_text("{not json", encoding="utf-8")
        with UNREACHABLE:
            catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.9.0"])

    def test_a_cache_written_in_the_future_is_not_believed(self):
        # A clock that moved backwards would otherwise keep answering as fresh forever, because the
        # age test is a range and not a magnitude.
        path = self.linux(baseline=None)
        self.cached(["v0.10.0"], age=-RELEASES.LISTING_TTL_SECONDS * 3)
        with UNREACHABLE:
            catalog, _ = self.catalog(path)
        self.assertEqual([item["version"] for item in catalog], ["v0.10.0"])
        self.assertEqual([item["provenance"] for item in catalog], ["stale"])


if __name__ == "__main__":
    unittest.main()
