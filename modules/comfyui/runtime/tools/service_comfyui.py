def frontend_probe(comfyctl):
    """Walk the browser's own path to ComfyUI, and report what it had to do to get there.

    The API request in `probe` below proves the bridge answers an authenticated client, which a
    browser is not. This asks for a frontend grant, redeems it for the cookie a page would receive,
    and then reads one API path holding nothing but that cookie - the only route by which the
    agent's Chromium can reach this ComfyUI on an MPS host. A refusal is reported as a status code:
    a grant is a credential, and it travels in a request body that no message here repeats.
    """
    import http.cookiejar
    import json
    import os
    import urllib.error
    import urllib.request

    origin = os.environ.get("COMFYUI_UI_URL", "").rstrip("/")
    if not origin:
        return "no frontend origin is configured"
    timeout = float(os.environ.get("COMFYUI_CONNECT_TIMEOUT", "10"))
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
    )

    def exchange(request):
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"the ComfyUI frontend refused a request ({exc.code})") from None
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(
                f"the ComfyUI frontend is unreachable ({type(exc).__name__})"
            ) from None
        try:
            return json.loads(body)
        except ValueError:
            raise ValueError("the ComfyUI frontend returned something that is not JSON") from None

    def get(path):
        return exchange(urllib.request.Request(f"{origin}{path}"))

    def post(path, payload):
        return exchange(
            urllib.request.Request(
                f"{origin}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    if not os.environ.get("COMFYUI_TOKEN", ""):
        # A backend whose frontend asks for no credential, so the origin is the whole plane.
        if "system" not in get("/system_stats"):
            raise ValueError("the ComfyUI frontend did not answer")
        return "frontend answers without a credential"

    # The grant is minted on the API plane, which is the only place a header credential can travel.
    link = comfyctl.request_json("POST", comfyctl.GRANT_PATH)
    path = link.get("path") if isinstance(link, dict) else None
    if not isinstance(path, str) or "#" not in path:
        raise ValueError("the bridge offered no frontend grant")
    session = post(comfyctl.SESSION_PATH, {"grant": path.split("#", 1)[1]})
    if session.get("status") != "session":
        raise ValueError("the bridge refused its own frontend grant")
    if not list(jar):
        raise ValueError("a redeemed grant produced no session cookie")
    if "system" not in get("/system_stats"):
        raise ValueError("the frontend session does not reach ComfyUI")
    return "frontend grant redeemed into a session that reaches ComfyUI"


def probe(full):
    # Exercise the same TLS, bearer auth and API helper used by the agent.
    import comfyctl

    stats = comfyctl.request_json("GET", "/system_stats")
    if not isinstance(stats, dict) or "system" not in stats:
        raise ValueError("invalid ComfyUI stats")
    detail = "authenticated stats"
    if full:
        schema = comfyctl.request_json("GET", "/object_info/EmptyImage")
        if "EmptyImage" not in schema:
            raise ValueError("missing EmptyImage node")
        detail += " and node schema"
    return detail + "; " + frontend_probe(comfyctl)
