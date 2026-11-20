"""C&G #273 v9.2: a page that keeps crashing the browser is not reopened in a loop, and the first content
check waits for the network clock with no cutoff, saying so on the screen after CLOCK_WAIT s."""
import importlib
import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pizero"))
TV = "https://www.vvcapital.se/tv"
CONNECT = "http://127.0.0.1:8080"


def _k():
    sys.modules.pop("kiosk", None)
    return importlib.import_module("kiosk")


class World:
    def __init__(self):
        self.t, self.reports, self.checks = 0.0, [], 0
        self.poll = {"online": True, "homepage": TV}
        self.up = True

    def fetch(self, url, timeout=3, data=None):
        if url.endswith("/api/browser/poll"):
            return dict(self.poll)
        if url.endswith("/api/browser/report"):
            self.reports.append(data)
            return {"ok": True}

    def check(self, url):
        self.checks += 1
        return self.up


class Cog:
    """A cog that can be made to die, as --webprocess-failure=exit does on a crashed page."""
    def __init__(self):
        self.started, self.opened, self.live = [], [], True
    def alive(self): return self.live
    def start(self, url): self.started.append(url); self.live = True
    def open(self, url): self.opened.append(url); return True
    def reload(self): return True
    def stop(self): self.live = False


def _q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _showing(synced=True):
    K, w, cog = _k(), World(), Cog()
    flag = {"v": synced}
    k = K.Kiosk(cog, CONNECT, fetch=w.fetch, check=w.check, clock=lambda: w.t, synced=lambda: flag["v"])
    k.start()
    return K, w, cog, k, flag


def test_one_crash_is_restarted_on_the_page():
    K, w, cog, k, _ = _showing()
    assert k.step() == "left-setup" and k.showing == TV
    cog.live = False
    assert k.step() == "restarted-browser" and cog.started[-1] == TV


def test_a_page_that_keeps_crashing_stops_the_loop_and_says_why():
    K, w, cog, k, _ = _showing()
    k.step()
    for i in range(K.CRASH_LIMIT - 1):
        w.t += 10
        cog.live = False
        assert k.step() == "restarted-browser"
    w.t += 10
    cog.live = False
    assert k.step() == "page-crashing"
    q = _q(cog.started[-1])
    assert cog.started[-1].startswith(CONNECT + "/static/unavailable.html?")
    assert q["u"] == TV and q["r"] == str(K.CRASH_BACKOFF)
    assert q["t"] == "This page keeps stopping the browser"
    assert f"{K.CRASH_LIMIT} times" in q["w"] and "WebGL" in q["w"] and "Show content" in q["w"]
    rep = w.reports[-1]
    assert rep["url"] == TV and rep["fails"] == K.CRASH_LIMIT and "WebGL" in rep["check"]
    # it does not reopen the page before the backoff
    checks = w.checks
    w.t += K.CRASH_BACKOFF - 1
    assert k.step() == "waiting" and w.checks == checks
    # after the backoff it checks and opens the page again
    w.t += 1
    assert k.step() == "recovered" and k.showing == TV


def test_crashes_spread_out_wider_than_the_window_never_stop_the_page():
    K, w, cog, k, _ = _showing()
    k.step()
    for i in range(K.CRASH_LIMIT * 3):
        w.t += K.CRASH_WINDOW / (K.CRASH_LIMIT - 1) + 1
        cog.live = False
        assert k.step() == "restarted-browser" and cog.started[-1] == TV


def test_a_crash_on_the_setup_or_unavailable_screen_is_always_restarted():
    K, w, cog, k, _ = _showing()
    w.poll = {"online": False, "homepage": TV}
    for i in range(K.CRASH_LIMIT + 2):
        cog.live = False
        assert k.step() == "restarted-browser" and cog.started[-1] == CONNECT + "/"


def test_a_page_error_that_does_not_crash_is_left_alone():
    """The WebGL notice on the TV: the page answers, cog keeps running, the kiosk opens nothing new."""
    K, w, cog, k, _ = _showing()
    k.step()
    n_open, n_start = len(cog.opened), len(cog.started)
    for i in range(20):
        w.t += K.POLL_CONTENT
        assert k.step() in ("ok",)
    assert len(cog.opened) == n_open and len(cog.started) == n_start


def test_clock_wait_has_no_cutoff_and_the_screen_says_so():
    K, w, cog, k, flag = _showing(synced=False)
    assert k.step() == "waiting-clock" and w.checks == 0
    w.t = K.CLOCK_WAIT
    assert k.step() == "showing-clock-wait"
    q = _q(cog.opened[-1])
    assert q["t"] == "Waiting for the network clock" and q["u"] == TV and "r" not in q
    assert "NTP" in q["w"] and "UDP port 123" in q["w"]
    for hours in (1, 6, 24):                             # never gives up and opens the page on a wrong clock
        w.t = hours * 3600
        assert k.step() == "waiting-clock" and w.checks == 0
    flag["v"] = True
    assert k.step() == "left-clock-wait" and w.checks == 1 and k.showing == TV


def test_clock_wait_goes_back_to_setup_when_the_network_drops():
    K, w, cog, k, flag = _showing(synced=False)
    k.step(); w.t = K.CLOCK_WAIT; k.step()
    w.poll = {"online": False, "homepage": TV}
    assert k.step() == "setup" and k.showing == CONNECT + "/" and k.mode == "setup"


def test_show_content_from_the_phone_skips_both_waits():
    K, w, cog, k, flag = _showing(synced=False)
    k.step(); w.t = K.CLOCK_WAIT; k.step()
    w.poll = {"online": True, "homepage": TV, "navigate": TV}
    assert k.step() == "navigated" and k.showing == TV


def test_unavailable_page_reads_heading_reason_and_no_countdown():
    js = open(os.path.join(ROOT, "app", "static", "unavailable.js")).read()
    html = open(os.path.join(ROOT, "app", "static", "unavailable.html")).read()
    assert 'id="t"' in html and 'id="w"' in html and 'class="unavail-why hidden"' in html
    assert 'q.get("t")' in js and 'q.get("w")' in js and '"Waiting"' in js
    assert "innerHTML" not in js                         # the reason is text, never markup
    assert "style=" not in html                          # one stylesheet, no inline CSS
