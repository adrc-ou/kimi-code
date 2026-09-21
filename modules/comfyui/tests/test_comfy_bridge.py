import argparse
import asyncio
import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("comfy_bridge", ROOT / "scripts" / "comfy_bridge.py")
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
try:
    SPEC.loader.exec_module(BRIDGE)
except ImportError as exc:
    # Reported as a skip naming the prerequisite, not as one loader error that silently
    # stands in for this module's whole suite.
    raise unittest.SkipTest(
        f"the bridge cannot be imported without its dependencies ({exc}); install what "
        "proxy/requirements.in pins to run these tests"
    ) from None

TOKEN = "a" * 64


class Clock:
    """A monotonic clock the test advances, so a lifetime is checked without sleeping."""

    def __init__(self):
        self.now = 1_000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_passes(**overrides):
    options = {"grant_ttl": 60, "session_ttl": 3600, "max_grants": 4, "max_sessions": 4}
    options.update(overrides)
    return BRIDGE.FrontendPasses(**options)


def make_app(passes=None):
    return {"token": TOKEN, "passes": passes or make_passes()}


class Request:
    """The slice of ``aiohttp.web.Request`` that ``authorized`` and ``proxy`` actually read."""

    def __init__(
        self,
        authorization="",
        *,
        app=None,
        host="untrusted.example",
        cookie=None,
        path="/queue",
        content_length=None,
    ):
        self.app = make_app() if app is None else app
        self.headers = {
            "Authorization": authorization,
            "Host": host,
            "Content-Type": "application/json",
        }
        self.cookies = {} if cookie is None else {BRIDGE.SESSION_COOKIE: cookie}
        self.rel_url = types.SimpleNamespace(path=path, query_string="")
        self.content_length = content_length


class BridgeTests(unittest.TestCase):
    def test_requires_exact_bearer_token(self):
        self.assertFalse(BRIDGE.authorized(Request()))
        self.assertFalse(BRIDGE.authorized(Request("Bearer wrong")))
        self.assertTrue(BRIDGE.authorized(Request(f"Bearer {TOKEN}")))

    def test_strips_authorization_and_host_upstream(self):
        headers = BRIDGE.request_headers(Request(f"Bearer {TOKEN}"))
        self.assertEqual(headers, {"Content-Type": "application/json"})

    def test_strips_connection_nominated_headers(self):
        request = Request(f"Bearer {TOKEN}")
        request.headers["Connection"] = "X-Private"
        request.headers["X-Private"] = "remove"
        self.assertNotIn("X-Private", BRIDGE.request_headers(request))

    def test_anonymous_request_holds_no_credential_of_either_shape(self):
        self.assertFalse(BRIDGE.authorized(Request()))

    def test_frontend_paths_are_never_proxied_to_comfyui(self):
        # An unregistered name under the reserved prefix is a mistake, not a route: it must not
        # reach the upstream, where a same-named ComfyUI path would answer it with real data.
        for path in (BRIDGE.BRIDGE_PREFIX, f"{BRIDGE.BRIDGE_PREFIX}/session/../../prompt"):
            with self.subTest(path=path), self.assertRaises(web.HTTPNotFound):
                self._run(BRIDGE.proxy(Request(path=path)))

    def test_proxy_still_demands_a_credential_after_the_guard(self):
        with self.assertRaises(web.HTTPUnauthorized):
            self._run(BRIDGE.proxy(Request(path="/history")))

    def test_session_cookie_authorizes_a_proxied_path(self):
        passes = make_passes()
        session = passes.mint_session("comfyui-ui:8188")
        request = Request(app=make_app(passes), host="comfyui-ui:8188", cookie=session)
        self.assertTrue(BRIDGE.authorized(request))

    def test_session_cookie_is_bound_to_the_host_that_minted_it(self):
        # The API plane and the frontend plane are different origins; a cookie taken from one
        # must not authenticate against the other.
        passes = make_passes()
        session = passes.mint_session("comfyui-ui:8188")
        stolen = Request(app=make_app(passes), host="host.docker.internal:8190", cookie=session)
        self.assertFalse(BRIDGE.authorized(stolen))

    def test_unknown_session_cookie_is_refused(self):
        self.assertFalse(BRIDGE.authorized(Request(cookie="not-a-session")))

    @staticmethod
    def _run(coroutine):
        return asyncio.run(coroutine)


class GrantTests(unittest.TestCase):
    def test_grant_is_single_use(self):
        passes = make_passes()
        grant = passes.mint_grant()
        self.assertTrue(passes.redeem_grant(grant))
        self.assertFalse(passes.redeem_grant(grant), "a redeemed grant must not mint twice")

    def test_grant_refuses_nothing_and_anything_never_issued(self):
        passes = make_passes()
        self.assertFalse(passes.redeem_grant(""))
        self.assertFalse(passes.redeem_grant("b" * 64))

    def test_grant_expires(self):
        clock = Clock()
        with mock.patch.object(BRIDGE, "time", clock):
            passes = make_passes(grant_ttl=30)
            grant = passes.mint_grant()
            clock.advance(29)
            self.assertTrue(passes.redeem_grant(grant))
            grant = passes.mint_grant()
            clock.advance(31)
            self.assertFalse(passes.redeem_grant(grant))

    def test_grant_slots_are_bounded(self):
        passes = make_passes(max_grants=2)
        self.assertIsNotNone(passes.mint_grant())
        self.assertIsNotNone(passes.mint_grant())
        self.assertIsNone(passes.mint_grant())

    def test_expired_grants_free_their_slots(self):
        clock = Clock()
        with mock.patch.object(BRIDGE, "time", clock):
            passes = make_passes(grant_ttl=10, max_grants=1)
            passes.mint_grant()
            self.assertIsNone(passes.mint_grant())
            clock.advance(11)
            self.assertIsNotNone(passes.mint_grant())


class SessionTests(unittest.TestCase):
    def test_session_expires(self):
        clock = Clock()
        with mock.patch.object(BRIDGE, "time", clock):
            passes = make_passes(session_ttl=120)
            session = passes.mint_session("comfyui-ui:8188")
            self.assertTrue(passes.session_active(session, "comfyui-ui:8188"))
            clock.advance(121)
            self.assertFalse(passes.session_active(session, "comfyui-ui:8188"))

    def test_session_without_a_host_is_never_active(self):
        passes = make_passes()
        session = passes.mint_session("comfyui-ui:8188")
        self.assertFalse(passes.session_active(session, ""))

    def test_session_slots_are_bounded(self):
        passes = make_passes(max_sessions=1)
        self.assertIsNotNone(passes.mint_session("one.example"))
        self.assertIsNone(passes.mint_session("two.example"))

    def test_cookie_carries_the_frontend_attributes_and_no_claim_it_cannot_keep(self):
        passes = make_passes(session_ttl=900)
        value = passes.session_cookie("s" * 20)
        self.assertIn(f"{BRIDGE.SESSION_COOKIE}={'s' * 20}", value)
        self.assertIn("HttpOnly", value)
        self.assertIn("SameSite=Strict", value)
        self.assertIn("Max-Age=900", value)
        # Both frontend planes are plaintext by necessity; a Secure flag would only make the
        # cookie unserviceable, so its absence is a decision this test pins down.
        self.assertNotIn("Secure", value)


class BootstrapTests(unittest.TestCase):
    def test_page_names_the_session_route_and_holds_no_credential(self):
        page = BRIDGE.bootstrap_page()
        self.assertIn(BRIDGE.SESSION_PATH, page)
        self.assertNotIn("@SESSION_PATH@", page, "the placeholder must not survive rendering")
        self.assertNotIn("Bearer", page)
        self.assertNotIn(TOKEN, page)

    def test_open_path_is_the_one_the_grant_advertises(self):
        passes = make_passes()
        grant = passes.mint_grant()
        self.assertTrue(BRIDGE.OPEN_PATH.startswith(BRIDGE.BRIDGE_PREFIX))
        self.assertEqual(BRIDGE.SESSION_PATH, f"{BRIDGE.BRIDGE_PREFIX}/session")
        self.assertTrue(grant)


class FrontendFlowTests(unittest.TestCase):
    """The flow as the bridge really serves it: its own routes, real cookies, real proxying."""

    def test_a_browser_reaches_comfyui_only_after_redeeming_a_grant(self):
        self.assertEqual(asyncio.run(self._flow()), [])

    @staticmethod
    async def _flow():
        failures = []
        seen = []

        def check(condition, message):
            if not condition:
                failures.append(message)

        async def upstream_handler(request):
            seen.append(f"{request.method} {request.path_qs}")
            return web.json_response({"system": {"name": "stub-comfyui"}})

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        options = argparse.Namespace(
            max_body=1024 * 1024,
            max_ws_message=1024 * 1024,
            max_connections=4,
            upstream=str(upstream.make_url("/")),
            grant_ttl=60,
            session_ttl=3600,
            max_grants=4,
            max_sessions=4,
        )
        bridge = TestServer(BRIDGE.build_app(options, TOKEN))
        await bridge.start_server()
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        try:
            # aiohttp's jar ignores cookies set on an IP host, which 127.0.0.1 is here; a real
            # browser, and the hostname this frontend is actually reached by, would keep it.
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as browser:
                page = await browser.get(bridge.make_url(BRIDGE.OPEN_PATH))
                document = await page.text()
                check(page.status == 200, "the bootstrap page is not served")
                check(BRIDGE.SESSION_PATH in document, "the bootstrap page cannot reach a session")
                check(TOKEN not in document, "the bootstrap page leaks the bearer token")

                refused = await browser.post(bridge.make_url(BRIDGE.GRANT_PATH))
                await refused.read()
                check(refused.status == 401, "a grant is offered without the bearer token")

                anonymous = await browser.get(bridge.make_url("/system_stats"))
                await anonymous.read()
                check(anonymous.status == 401, "an anonymous browser reaches ComfyUI")
                check(seen == [], "something was proxied before any credential existed")

                minted = await browser.post(bridge.make_url(BRIDGE.GRANT_PATH), headers=bearer)
                link = await minted.json()
                check(minted.status == 200, "the bearer token cannot mint a grant")
                check(
                    link["path"].startswith(f"{BRIDGE.OPEN_PATH}#"),
                    "the grant is not carried in the page link's fragment",
                )
                grant = link["path"].split("#", 1)[1]

                opened = await browser.post(
                    bridge.make_url(BRIDGE.SESSION_PATH), json={"grant": grant}
                )
                await opened.text()
                check(opened.status == 200, "a fresh grant does not mint a session")
                check(
                    "HttpOnly" in opened.headers.get("Set-Cookie", ""),
                    "the session cookie is readable by page script",
                )

                stats = await browser.get(bridge.make_url("/system_stats"))
                body = await stats.text()
                check(stats.status == 200, "the session cookie does not authorize a frontend path")
                check("stub-comfyui" in body, "the response is not the upstream's")
                check(
                    not any("Authorization" in entry for entry in seen),
                    "a header credential reached the upstream",
                )

                replayed = await browser.post(
                    bridge.make_url(BRIDGE.SESSION_PATH), json={"grant": grant}
                )
                await replayed.read()
                check(replayed.status == 401, "a redeemed grant still mints a session")

                reserved = await browser.get(bridge.make_url(f"{BRIDGE.BRIDGE_PREFIX}/nope"))
                await reserved.read()
                check(reserved.status == 404, "a reserved bridge path reached ComfyUI")
                check(len(seen) == 1, f"the upstream handled more than the session path: {seen}")

                api = await browser.get(
                    bridge.make_url("/system_stats"), headers={"Authorization": f"Bearer {TOKEN}"}
                )
                await api.read()
                check(api.status == 200, "the bearer token stopped working on its own plane")
        finally:
            await bridge.close()
            await upstream.close()
        return failures


if __name__ == "__main__":
    unittest.main()
