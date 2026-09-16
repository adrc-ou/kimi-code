#!/usr/bin/env python3
"""Open the first reachable URL from Kimi's ready banner on the host.

Capture Compose logs in memory: authenticated URLs must not be copied into
new log files or diagnostics. Probe without the fragment or proxy settings.
"""

import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def advertised_urls(logs):
    logs = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", logs)
    local, network = [], []
    for kind, url in re.findall(r"\b(Local|Network):\s+(http://[^\s]+)", logs):
        parsed = urllib.parse.urlsplit(url)
        # Accept only Kimi's expected local service URLs, not arbitrary log links.
        try:
            if parsed.port != 5494 or parsed.username or parsed.password:
                continue
        except ValueError:
            continue
        if parsed.path != "/" or parsed.query or not parsed.fragment.startswith("token="):
            continue
        if kind == "Local":
            if parsed.hostname != "localhost":
                continue
            local.append(url)
        else:
            import ipaddress
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                continue
            network.append(url)
    return list(dict.fromkeys(local + network))


def first_reachable(urls):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for url in urls:
        origin = urllib.parse.urldefrag(url)[0]
        try:
            # Test both the API readiness and the web page before opening a tab.
            for path in ("api/v1/healthz", ""):
                with opener.open(origin + path, timeout=3) as response:
                    if response.status != 200:
                        raise ValueError("Service not ready")
            return url
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def open_browser(url):
    commands = ["open"] if sys.platform == "darwin" else ["wslview", "xdg-open"]
    for command in commands:
        executable = shutil.which(command)
        if executable:
            try:
                return subprocess.run(
                    [executable, url], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                    check=False,
                ).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False
    return False


def main():
    try:
        logs = subprocess.run(
            sys.argv[1:] + ["logs", "--no-color", "--tail", "200", "kimi-agent"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        url = first_reachable(advertised_urls(logs))
        if url is None:
            print(
                "No advertised Kimi URL is reachable; open the UI manually when ready.",
                file=sys.stderr,
            )
            return 1
        if not open_browser(url):
            print(
                "Could not open the default browser; use the Kimi URL in the ready banner.",
                file=sys.stderr,
            )
            return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Could not read Kimi's ready URLs; use the ready banner to open the UI manually.",
            file=sys.stderr,
        )
        return 1
    print("Opened Kimi Code in the default browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
