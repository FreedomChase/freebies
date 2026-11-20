#!/usr/bin/env python3
"""
Lean kiosk for the Raspberry Pi Zero W build (ARMv6, 512 MB).

The PC stick runs a PyQt6 / Qt WebEngine browser (browser/browser.py). Neither
Qt WebEngine nor a usable Chromium exists for ARMv6 in 512 MB, so the Pi build
uses cog, the WPE WebKit kiosk launcher, straight on the screen (DRM, no X, no
window manager), and this small controller drives it with cogctl over D-Bus.

It speaks the same API as browser/browser.py, so the setup page, settings and
the fleet dashboard work unchanged:

  GET  /api/browser/poll     navigate once, action (reload/restart), homepage,
                             kiosk, reload_every, online, device_id
  POST /api/browser/report   {url} what the screen is showing

What it does:
  * opens the setup page at boot and stays there until the screen is online
    (or the setup page asks for a URL: "Start browsing", the sign-in page);
  * once online, opens the content URL; a changed homepage (settings page or
    fleet dashboard) is opened as soon as it is seen;
  * watchdog: the content URL is probed before it is opened and every
    PROBE_EVERY seconds after; when it does not answer, the screen shows
    /static/unavailable.html with the URL and the screen ID, and retries with
    backoff (5, 10, 20, 40, 60 s); when the network comes back it reloads;
  * reload_every minutes: scheduled reload; action "restart": cog is restarted;
  * cog crashing or exiting is restarted.

Standard library only. Run as the kiosk user from usb-router-kiosk.service.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("usb-router.kiosk")

DEFAULT_CONNECT_URL = os.environ.get("USB_ROUTER_CONNECT_URL", "http://127.0.0.1:8080")
PROBE_EVERY = 30          # seconds between content checks while it is showing
POLL_SETUP = 2            # seconds between polls on the setup page
POLL_CONTENT = 4          # seconds between polls once content shows
CLOCK_WAIT = 90           # C&G #270: seconds to wait for the network clock before the first content check
TIMESYNC_FLAG = "/run/systemd/timesync/synchronized"
LAST_CHECK = {"ok": None, "why": ""}   # the last content check, in plain words, for the phone page


def clock_synced(flag=TIMESYNC_FLAG):
    """True once systemd-timesyncd has set the clock (or when the image has no timesyncd at all).
    The Pi has no clock battery: until NTP answers it runs on the time saved at the last shutdown,
    and a certificate issued after that time reads as 'not yet valid'."""
    return os.path.exists(flag) or not os.path.isdir(os.path.dirname(flag))


def _why(e):
    """an exception from the content check as a short plain reason (never a body or a header)"""
    s = str(getattr(e, "reason", "") or e).lower()
    if "not yet valid" in s:
        return "the screen's clock is behind (certificate not valid yet)"
    if "certificate" in s or "ssl" in s:
        return "secure connection failed (certificate)"
    if "timed out" in s or isinstance(e, TimeoutError):
        return "no answer in time"
    if "name or service not known" in s or "temporary failure in name resolution" in s or "nodename" in s:
        return "the address could not be found (DNS)"
    if "refused" in s:
        return "connection refused"
    if "unreachable" in s:
        return "network unreachable"
    return type(e).__name__


def fetch_json(url, timeout=3, data=None):
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def probe(url, timeout=8):
    """True when the content server answers below 500. TLS errors count as down (cog refuses them too).
    C&G #270: the reason for a failure is kept in LAST_CHECK for the phone page."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "usb-router-kiosk"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok, why = resp.status < 500, f"the server answered {resp.status}"
    except urllib.error.HTTPError as e:
        ok, why = e.code < 500, f"the server answered {e.code}"
    except Exception as e:
        ok, why = False, _why(e)
    LAST_CHECK.update(ok=ok, why="" if ok else why)
    return ok


class Cog:
    """Starts cog on the screen and controls it with cogctl."""

    def __init__(self, cog="cog", cogctl="cogctl", platform="drm", mem_limit=200, extra=None):
        self.cog = shutil.which(cog) or cog
        self.cogctl = shutil.which(cogctl) or cogctl
        self.platform = platform
        self.mem_limit = mem_limit
        self.extra = list(extra or [])
        self.proc = None
        self.starts = 0               # C&G #270: cog starts this boot, shown on :8080 as "browser restarted Nx"

    def command(self, url):
        cmd = [self.cog, f"--platform={self.platform}", "--bg-color=#052444",
               "--webprocess-failure=restart"]
        if self.mem_limit:
            cmd.append(f"--web-mem-limit={int(self.mem_limit)}")
        return cmd + self.extra + [url]

    def start(self, url):
        self.stop()
        log.info("starting cog: %s", " ".join(self.command(url)))
        self.proc = subprocess.Popen(self.command(url))
        self.starts += 1

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _ctl(self, *args):
        try:
            return subprocess.run([self.cogctl, *args], timeout=10, capture_output=True).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def open(self, url):
        return self._ctl("open", url)

    def reload(self):
        return self._ctl("reload")

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class Kiosk:
    """The decision logic, kept apart from cog and the clock so it can be tested."""

    def __init__(self, browser, connect_url=DEFAULT_CONNECT_URL, fetch=fetch_json, check=probe,
                 clock=time.monotonic, home=None, synced=clock_synced):
        self.b = browser
        self.connect = connect_url.rstrip("/")
        self.fetch = fetch
        self.check = check
        self.clock = clock
        self.home_override = home
        self.mode = "setup"           # setup | content | unavailable
        self.showing = None           # the URL cog was last told to open
        self.homepage = None
        self.device_id = ""
        self.was_online = None
        self.fails = 0
        self.retry_at = None
        self.next_probe = None
        self.reload_every = 0
        self.next_reload = None
        self.control_failures = 0     # cogctl calls that failed and forced a cog restart
        self.synced = synced
        self.online_since = None      # C&G #270: when the setup page first saw the network up

    # -- helpers -----------------------------------------------------------
    def _starts(self, data):
        """C&G #270: add the cog start count to a report (a fake browser in tests may not count)."""
        n = getattr(self.b, "starts", None)
        if isinstance(n, int) and n > 0:
            data["starts"] = n
        return data

    def _report_starts(self):
        """After cog died and was restarted: tell :8080, without changing what it says is showing."""
        data = self._starts({"url": self.showing, "check": "", "restart": True})
        if "starts" in data:
            self.fetch(self.connect + "/api/browser/report", data=data)

    def setup_url(self):
        return self.connect + "/"

    def unavailable_url(self, url, delay):
        q = urllib.parse.urlencode({"u": url, "s": self.device_id or "", "r": delay})
        return f"{self.connect}/static/unavailable.html?{q}"

    def _open(self, url):
        self.showing = url
        if not self.b.alive():
            self.b.start(url)
        elif not self.b.open(url):
            self._control_failed("open", url)

    def _reload(self):
        if not self.b.reload():
            self._control_failed("reload", self.showing or self.setup_url())

    def _control_failed(self, what, url):
        # cogctl could not reach cog (bus gone, cog hung): restart cog on the page it should show
        log.warning("cogctl %s failed; restarting cog on %s", what, url)
        self.control_failures += 1
        self.b.stop()
        self.b.start(url)

    def start(self):
        self.mode = "setup"
        self._open(self.setup_url())

    def show_content(self, url, ok=None):
        """Open url if it answers; otherwise the 'content unavailable' screen and a retry."""
        now = self.clock()
        if ok is None:
            ok = self.check(url)
        if ok:
            self.mode = "content"
            self.fails = 0
            self.retry_at = None
            self.next_probe = now + PROBE_EVERY
            self._open(url)
            self.fetch(self.connect + "/api/browser/report", data=self._starts({"url": url}))
            if self.reload_every:
                self.next_reload = now + self.reload_every * 60
            return True
        self.fails += 1
        delay = min(60, 5 * 2 ** min(self.fails - 1, 4))
        self.fetch(self.connect + "/api/browser/report",
                   data=self._starts({"url": url, "check": LAST_CHECK.get("why") or "no answer", "fails": self.fails}))
        self.mode = "unavailable"
        self.retry_at = now + delay
        self._open(self.unavailable_url(url, delay))
        return False

    # -- one step ----------------------------------------------------------
    def step(self):
        """Poll the connection manager once and act. Returns a short label (for tests/logging)."""
        if not self.b.alive():
            # cog died or exited: bring back whatever should be showing
            url = self.showing or self.setup_url()
            self.b.start(url)
            self._report_starts()
            return "restarted-browser"

        data = self.fetch(self.connect + "/api/browser/poll")
        if not data:
            return "no-manager"
        now = self.clock()
        online = bool(data.get("online"))
        home = self.home_override or data.get("homepage") or self.homepage
        self.device_id = data.get("device_id") or ""
        every = int(data.get("reload_every") or 0)
        if every != self.reload_every:
            self.reload_every = every
            self.next_reload = now + every * 60 if every else None
        was_online, self.was_online = self.was_online, online

        if data.get("action") == "restart":
            self.b.stop()
            self.b.start(self.showing or self.setup_url())
            return "restarted-browser"

        target = data.get("navigate")
        if target:
            # an explicit request from the setup page: open it as asked, no probe
            self.mode = "content"
            self.homepage = home
            self._open(target)
            return "navigated"

        if self.mode == "setup":
            if online and home and int(data.get("hold") or 0) > 0:
                # C&G #254 v8.7: a join just worked; the setup page shows the phone address for a moment
                return "holding"
            if online and home:
                # C&G #270: give the network clock up to CLOCK_WAIT s first, so a fresh flash does not
                # start on "Content unavailable" just because its clock is behind
                self.online_since = self.online_since if self.online_since is not None else now
                if not self.synced() and now - self.online_since < CLOCK_WAIT:
                    return "waiting-clock"
                self.homepage = home
                self.show_content(home)
                return "left-setup"
            return "setup"

        if home and home != self.homepage:
            # changed from the settings page or the fleet dashboard
            self.homepage = home
            self.fails = 0
            self.show_content(home)
            return "homepage-changed"

        if self.mode == "unavailable":
            if (online and was_online is False) or (self.retry_at is not None and now >= self.retry_at):
                return "recovered" if self.show_content(self.homepage) else "retrying"
            return "waiting"

        # content showing
        if online and was_online is False:
            self._reload()
            return "reloaded-network-back"
        if data.get("action") == "reload":
            self._reload()
            return "reloaded"
        if self.next_probe is not None and now >= self.next_probe:
            self.next_probe = now + PROBE_EVERY
            if not self.check(self.homepage):
                self.show_content(self.homepage, ok=False)
                return "content-down"
        if self.next_reload is not None and now >= self.next_reload:
            self.next_reload = now + self.reload_every * 60
            self._reload()
            return "scheduled-reload"
        return "ok"

    def interval(self):
        return POLL_SETUP if self.mode == "setup" else POLL_CONTENT


def wait_for_manager(connect, seconds=60):
    for _ in range(seconds):
        if fetch_json(connect.rstrip("/") + "/healthz") is not None:
            return True
        time.sleep(1)
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--connect-url", default=DEFAULT_CONNECT_URL)
    p.add_argument("--home", help="override the homepage from the settings")
    p.add_argument("--platform", default=os.environ.get("USB_ROUTER_COG_PLATFORM", "drm"),
                   help="cog platform plug-in: drm (the screen), headless (tests), wl")
    p.add_argument("--mem-limit", type=int, default=int(os.environ.get("USB_ROUTER_COG_MEM", "200")),
                   help="WebKit web process memory limit in MiB (0 = none)")
    p.add_argument("--once", action="store_true", help="start, run a few steps and exit (smoke test)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if not wait_for_manager(a.connect_url):
        log.warning("connection manager not answering at %s; starting anyway", a.connect_url)
    display = fetch_json(a.connect_url.rstrip("/") + "/api/display") or {}
    if display.get("rotation", "normal") != "normal":
        log.warning("rotation=%s is not supported by the Pi Zero build (cog DRM has no rotation); "
                    "showing unrotated", display.get("rotation"))

    browser = Cog(platform=a.platform, mem_limit=a.mem_limit)
    kiosk = Kiosk(browser, a.connect_url, home=a.home)
    kiosk.start()
    try:
        steps = 0
        while True:
            time.sleep(kiosk.interval())
            what = kiosk.step()
            if what not in ("ok", "setup", "waiting"):
                log.info("%s (%s)", what, kiosk.showing)
            steps += 1
            if a.once and steps >= 3:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        browser.stop()


if __name__ == "__main__":
    sys.exit(main())
