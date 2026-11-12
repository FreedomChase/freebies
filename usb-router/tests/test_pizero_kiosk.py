"""
Tests for the Pi Zero kiosk controller (pizero/kiosk.py) with a fake cog, a fake clock
and a fake connection manager.

    python3 -m pytest tests/test_pizero_kiosk.py
"""
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pizero"))

import kiosk as K  # noqa: E402

CONNECT = "http://127.0.0.1:8080"
HOME = "https://signage.example.com/play?screen=pz-1"


class FakeCog:
    def __init__(self):
        self.running = False
        self.log = []
        self.ctl_ok = True      # False = cogctl cannot reach cog

    def start(self, url):
        self.running = True
        self.log.append(("start", url))

    def alive(self):
        return self.running

    def open(self, url):
        self.log.append(("open", url))
        return self.ctl_ok

    def reload(self):
        self.log.append(("reload",))
        return self.ctl_ok

    def stop(self):
        self.running = False
        self.log.append(("stop",))


class World:
    """The manager's poll answer, the content server and the clock."""

    def __init__(self):
        self.t = 1000.0
        self.poll = {"ok": True, "online": False, "homepage": HOME, "kiosk": True,
                     "reload_every": 0, "device_id": "pz-1", "navigate": None, "action": None}
        self.content_up = True
        self.reports = []

    def fetch(self, url, timeout=3, data=None):
        if url.endswith("/api/browser/poll"):
            answer = dict(self.poll)
            self.poll["navigate"] = None     # one-shot, as on the server
            self.poll["action"] = None
            return answer
        if url.endswith("/api/browser/report"):
            self.reports.append(data["url"])
            return {"ok": True}
        return None

    def check(self, url):
        return self.content_up

    def clock(self):
        return self.t


def make():
    w, cog = World(), FakeCog()
    k = K.Kiosk(cog, CONNECT, fetch=w.fetch, check=w.check, clock=w.clock)
    k.start()
    return w, cog, k


def last_url(cog):
    return next(e[1] for e in reversed(cog.log) if e[0] in ("start", "open"))


def test_boots_on_the_setup_page_and_stays_there_offline():
    w, cog, k = make()
    assert cog.log == [("start", CONNECT + "/")]
    assert k.step() == "setup"
    assert k.step() == "setup"
    assert k.interval() == K.POLL_SETUP


def test_goes_to_content_once_online_and_reports_it():
    w, cog, k = make()
    w.poll["online"] = True
    assert k.step() == "left-setup"
    assert last_url(cog) == HOME and k.mode == "content"
    assert w.reports == [HOME]
    assert k.step() == "ok"
    assert k.interval() == K.POLL_CONTENT


def test_content_down_shows_unavailable_then_retries_with_backoff_and_recovers():
    w, cog, k = make()
    w.poll["online"] = True
    w.content_up = False
    assert k.step() == "left-setup"
    assert k.mode == "unavailable"
    page = urllib.parse.urlparse(last_url(cog))
    assert page.path == "/static/unavailable.html"
    q = urllib.parse.parse_qs(page.query)
    assert q["u"] == [HOME] and q["s"] == ["pz-1"] and q["r"] == ["5"]
    assert k.step() == "waiting"            # before the retry time
    w.t += 5
    assert k.step() == "retrying"           # still down: next delay doubles
    assert urllib.parse.parse_qs(urllib.parse.urlparse(last_url(cog)).query)["r"] == ["10"]
    w.t += 10
    w.content_up = True
    assert k.step() == "recovered"
    assert last_url(cog) == HOME and k.mode == "content"


def test_backoff_caps_at_60_seconds():
    w, cog, k = make()
    w.poll["online"] = True
    w.content_up = False
    k.step()
    delays = []
    for _ in range(7):
        w.t += 60
        k.step()
        delays.append(urllib.parse.parse_qs(urllib.parse.urlparse(last_url(cog)).query)["r"][0])
    assert delays[-3:] == ["60", "60", "60"]


def test_watchdog_notices_content_going_down_while_showing():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    w.content_up = False
    assert k.step() == "ok"                 # not yet time to probe
    w.t += K.PROBE_EVERY
    assert k.step() == "content-down"
    assert k.mode == "unavailable"


def test_network_back_reloads_content():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    w.poll["online"] = False
    k.step()
    w.poll["online"] = True
    assert k.step() == "reloaded-network-back"
    assert cog.log[-1] == ("reload",)


def test_homepage_changed_from_the_dashboard_is_opened():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    w.poll["homepage"] = "https://signage.example.com/play?screen=pz-1&v=2"
    assert k.step() == "homepage-changed"
    assert last_url(cog) == "https://signage.example.com/play?screen=pz-1&v=2"


def test_navigate_request_from_the_setup_page_is_opened_without_a_probe():
    w, cog, k = make()
    w.content_up = False                    # e.g. a captive portal page: no probe in the way
    w.poll["navigate"] = "http://portal.example.net/login"
    assert k.step() == "navigated"
    assert last_url(cog) == "http://portal.example.net/login"


def test_scheduled_reload_and_remote_reload_and_restart():
    w, cog, k = make()
    w.poll["online"] = True
    w.poll["reload_every"] = 10
    k.step()
    w.t += 10 * 60 - 1
    assert k.step() == "ok"
    w.t += 1
    assert k.step() == "scheduled-reload"
    assert cog.log[-1] == ("reload",)
    w.poll["action"] = "reload"
    assert k.step() == "reloaded"
    w.poll["action"] = "restart"
    assert k.step() == "restarted-browser"
    assert cog.log[-2:] == [("stop",), ("start", HOME)]


def test_cog_crash_is_restarted_on_what_was_showing():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    cog.running = False
    assert k.step() == "restarted-browser"
    assert cog.log[-1] == ("start", HOME)


def test_manager_down_does_nothing():
    w, cog, k = make()
    k.fetch = lambda *a, **kw: None
    assert k.step() == "no-manager"
    assert cog.log == [("start", CONNECT + "/")]


def test_cog_command_line():
    c = K.Cog(cog="cog", cogctl="cogctl", platform="drm", mem_limit=200)
    cmd = c.command(HOME)
    assert cmd[0].endswith("cog") and cmd[-1] == HOME
    assert "--platform=drm" in cmd and "--web-mem-limit=200" in cmd
    assert "--webprocess-failure=restart" in cmd
    assert not any("ignore-tls" in a for a in cmd)   # never bypass certificate errors


def test_failed_cogctl_open_restarts_cog_on_the_wanted_page():
    w, cog, k = make()
    cog.ctl_ok = False
    w.poll["online"] = True
    assert k.step() == "left-setup"
    assert cog.log[-3:] == [("open", HOME), ("stop",), ("start", HOME)]
    assert k.control_failures == 1 and k.showing == HOME


def test_failed_cogctl_reload_restarts_cog_on_what_was_showing():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    cog.ctl_ok = False
    w.poll["action"] = "reload"
    assert k.step() == "reloaded"
    assert cog.log[-3:] == [("reload",), ("stop",), ("start", HOME)]
    assert k.control_failures == 1


def test_working_cogctl_never_restarts_cog():
    w, cog, k = make()
    w.poll["online"] = True
    k.step()
    w.poll["action"] = "reload"
    k.step()
    assert ("stop",) not in cog.log and k.control_failures == 0
