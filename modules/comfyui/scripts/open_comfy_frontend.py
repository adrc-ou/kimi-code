#!/usr/bin/env python3

"""Open the ComfyUI frontend in the host's default browser once it is actually serving.

The launcher already knows what it just started, so this takes one URL rather than mining logs for
it. The URL is the host's own loopback listener, which is where ComfyUI's frontend is reachable
without a credential; nothing here ever sees the bridge's bearer token, and nothing here opens a
URL that is not loopback, so a mistyped argument cannot turn the launcher into a way of pointing a
browser at somebody else's machine.

This is a second desktop opener beside the harness's own `tools/open_kimi_browser.py`, and it stays
inside the module rather than importing that one: a module's host scripts are launched from the
module directory, and the two differ in what they refuse to open.
"""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK_HOSTS = {"localhost", "ip6-localhost", "ip6-loopback"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener that follows a redirect stops testing the origin it was given."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_loopback(url: str) -> bool:
    """Whether this URL names an address on the machine running the launcher."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.hostname.lower() in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def is_serving(url: str, timeout: float) -> bool:
    """Whether ComfyUI answers on this origin yet.

    Only the origin is probed, with no proxy and no redirects followed: an opener that opened a
    tab at a URL it had not confirmed was listening would leave the browser on an error page for
    the rest of the session.
    """
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(origin.rstrip("/") + "/system_stats", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def open_browser(url: str) -> bool:
    """Hand one URL to the desktop's default handler, if this host has one."""
    commands = ["open"] if sys.platform == "darwin" else ["wslview", "xdg-open"]
    for command in commands:
        executable = shutil.which(command)
        if not executable:
            continue
        try:
            return (
                subprocess.run(
                    [executable, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    options = parser.parse_args(argv)

    if not is_loopback(options.url):
        print(
            f"Refusing to open {options.url}: the ComfyUI frontend is only opened on loopback.",
            file=sys.stderr,
        )
        return 1
    if not is_serving(options.url, options.timeout):
        print(
            f"Nothing is serving {options.url}; open the ComfyUI frontend manually once it is.",
            file=sys.stderr,
        )
        return 1
    if not open_browser(options.url):
        print(
            "Could not open the default browser; the ComfyUI frontend is at "
            f"{options.url} on this machine.",
            file=sys.stderr,
        )
        return 1
    print("Opened the ComfyUI frontend in the default browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
