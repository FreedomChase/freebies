"""C&G #270 (Shaun, 20 Nov 00:45 CET: 'remove duck duck go and add correct link for /tv'; 00:47: 'The flash
always has duck duck go even if config changed'). v8.7 shipped a conf whose line 13 already set a homepage, so a
second homepage line made the strict parser drop the whole file. #1527 fixed the parser after v8.7 was built;
v8.8 carries it, makes /tv the default, strips quotes, and adds a reset file, a clock wait and the check's reason
on the phone page."""
import importlib
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "pizero"))
EXAMPLE = os.path.join(ROOT, "config", "usb-router.conf.example")
TV = "https://www.vvcapital.se/tv"


def _config(tmp_path, monkeypatch, data: bytes, saved=None):
    conf = tmp_path / "usb-router.conf"
    conf.write_bytes(data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if saved is not None:
        (state / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(state))
    sys.modules.pop("config", None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    return config.Config()


def _notepad(text: str) -> bytes:
    """what Windows Notepad writes: a BOM and CRLF line ends"""
    return b"\xef\xbb\xbf" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def _example():
    return open(EXAMPLE, encoding="utf-8").read()


# -- DuckDuckGo is gone, /tv is the default ---------------------------------------------------------
def test_default_homepage_is_the_vvc_tv_page(tmp_path, monkeypatch):
    assert _config(tmp_path, monkeypatch, b"").get("homepage") == TV
    assert _config(tmp_path, monkeypatch, _example().encode()).get("homepage") == TV


def test_no_duckduckgo_left_in_anything_that_ships():
    hits = []
    for top in ("app", "pizero", "config", "system", "browser", "README.md"):
        path = os.path.join(ROOT, top)
        files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
        for f in files:
            if f.endswith((".py", ".js", ".html", ".css", ".conf", ".example", ".md", ".sh", ".service", ".txt")):
                if "duckduckgo" in open(f, encoding="utf-8", errors="replace").read().lower():
                    hits.append(os.path.relpath(f, ROOT))
    assert hits == []


def test_the_shipped_file_says_change_this_line():
    lines = _example().splitlines()
    n = next(i for i, l in enumerate(lines) if l.startswith("homepage ="))
    assert lines[n] == f"homepage = {TV}" and "do not add a second one" in lines[n - 1]


# -- Shaun's file: the shipped conf + one appended line, saved by Notepad ------------------------------
def test_notepad_file_with_an_appended_homepage_line_is_used(tmp_path, monkeypatch):
    text = _example().rstrip("\n") + "\nhomepage = https://www.vvcapital.se/tv?screen=office\nwifi_ssid = VVAdmin\n"
    cfg = _config(tmp_path, monkeypatch, _notepad(text))
    assert cfg.get("homepage") == "https://www.vvcapital.se/tv?screen=office"
    assert cfg.provisioned_wifi() == [("VVAdmin", None)]          # the rest of the file counts too
    assert "homepage is set 2 times" in cfg.conf_problems()[0]


@pytest.mark.parametrize("written, used", [
    ('"https://www.vvcapital.se/tv"', TV),
    ("'https://www.vvcapital.se/tv'", TV),
    ("www.vvcapital.se/tv", TV),
    ('"www.vvcapital.se/tv"  ', TV),
    ("http://example.org/x", "http://example.org/x"),
])
def test_homepage_quotes_and_missing_https_are_forgiven(tmp_path, monkeypatch, written, used):
    cfg = _config(tmp_path, monkeypatch, _notepad(f"[usb-router]\nhomepage = {written}\n"))
    assert cfg.get("homepage") == used
    assert cfg.resolve_url() == used


def test_quotes_are_stripped_on_every_setting(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, b'[usb-router]\ndevice_name = "Lobby left"\nkiosk = "false"\n')
    assert cfg.get("device_name") == "Lobby left" and cfg.flag("kiosk") is False


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    conf = tmp_path / "usb-router.conf"
    conf.write_bytes(_notepad(_example().rstrip("\n") + '\nhomepage = "https://www.vvcapital.se/tv?screen=2"\n'))
    monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return server


def test_the_8080_page_reads_back_the_file_homepage(srv):
    c = srv.app.test_client()
    want = "https://www.vvcapital.se/tv?screen=2"
    assert c.get("/api/status").get_json()["settings"]["resolved_homepage"] == want
    assert c.get("/api/browser/poll").get_json()["homepage"] == want


# -- the kiosk's check, visible on the phone page ---------------------------------------------------
def test_a_failed_check_is_reported_and_a_pass_clears_it(srv):
    c = srv.app.test_client()
    c.post("/api/browser/report", json={"url": TV})
    c.post("/api/browser/report", json={"url": TV, "check": "the address could not be found (DNS)", "fails": 3})
    s = c.get("/api/status").get_json()
    assert s["content_check"]["failed"] == "the address could not be found (DNS)" and s["content_check"]["fails"] == 3
    assert s["current_url"] == TV                                  # a failed try does not claim to be showing
    c.post("/api/browser/report", json={"url": TV})
    assert c.get("/api/status").get_json()["content_check"] is None


def test_the_report_is_refused_from_off_the_box(srv):
    r = srv.app.test_client().post("/api/browser/report", json={"check": "x"}, environ_base={"REMOTE_ADDR": "10.42.0.7"})
    assert r.status_code == 403


def test_page_has_the_check_line_and_writes_it_as_text():
    html = open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
    assert 'id="content-check"' in html
    assert '$("content-check").textContent = cc ?' in js and "innerHTML" not in js.split('$("content-check")')[1][:400]


def _kiosk_mod():
    sys.modules.pop("kiosk", None)
    return importlib.import_module("kiosk")


@pytest.mark.parametrize("exc, why", [
    ("ssl.SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid')",
     "the screen's clock is behind (certificate not valid yet)"),
    ("urllib.error.URLError(socket.gaierror(-2, 'Name or service not known'))", "the address could not be found (DNS)"),
    ("urllib.error.URLError(socket.gaierror(-3, 'Temporary failure in name resolution'))", "the address could not be found (DNS)"),
    ("TimeoutError('timed out')", "no answer in time"),
    ("urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))", "connection refused"),
])
def test_probe_keeps_a_plain_reason(monkeypatch, exc, why):
    import socket, ssl, urllib.error  # noqa: F401  (used by eval)
    K = _kiosk_mod()
    err = eval(exc)

    def boom(*a, **k):
        raise err
    monkeypatch.setattr(K.urllib.request, "urlopen", boom)
    assert K.probe(TV) is False and K.LAST_CHECK == {"ok": False, "why": why}


def test_probe_pass_clears_the_reason(monkeypatch):
    K = _kiosk_mod()

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    K.LAST_CHECK.update(ok=False, why="old")
    monkeypatch.setattr(K.urllib.request, "urlopen", lambda *a, **k: R())
    assert K.probe(TV) is True and K.LAST_CHECK == {"ok": True, "why": ""}


class _World:
    def __init__(self):
        self.t, self.reports = 0.0, []
        self.poll = {"online": True, "homepage": TV}
        self.checks = 0

    def fetch(self, url, timeout=3, data=None):
        if url.endswith("/api/browser/poll"):
            return dict(self.poll)
        if url.endswith("/api/browser/report"):
            self.reports.append(data)
            return {"ok": True}

    def check(self, url):
        self.checks += 1
        return False


class _Cog:
    def __init__(self):
        self.opened = []
    def alive(self): return True
    def start(self, url): self.opened.append(url)
    def open(self, url): self.opened.append(url); return True
    def reload(self): return True
    def stop(self): pass


def test_first_check_waits_for_the_network_clock_then_goes_ahead():
    K = _kiosk_mod()
    w, cog, synced = _World(), _Cog(), {"v": False}
    k = K.Kiosk(cog, "http://127.0.0.1:8080", fetch=w.fetch, check=w.check, clock=lambda: w.t,
                synced=lambda: synced["v"])
    k.start()
    assert k.step() == "waiting-clock" and w.checks == 0
    w.t = K.CLOCK_WAIT - 1
    assert k.step() == "waiting-clock" and w.checks == 0
    w.t = K.CLOCK_WAIT                                   # C&G #273: no cutoff, the screen says it waits
    assert k.step() == "showing-clock-wait" and w.checks == 0
    synced["v"] = True
    assert k.step() == "left-clock-wait" and w.checks == 1


def test_a_synced_clock_does_not_wait_and_a_failure_is_reported_with_its_reason():
    K = _kiosk_mod()
    K.LAST_CHECK.update(ok=False, why="the address could not be found (DNS)")
    w, cog = _World(), _Cog()
    k = K.Kiosk(cog, "http://127.0.0.1:8080", fetch=w.fetch, check=w.check, clock=lambda: w.t, synced=lambda: True)
    k.start()
    assert k.step() == "left-setup" and k.mode == "unavailable"
    assert w.reports[-1] == {"url": TV, "check": "the address could not be found (DNS)", "fails": 1}


def test_clock_synced_reads_the_timesyncd_flag(tmp_path):
    K = _kiosk_mod()
    flag = tmp_path / "timesync" / "synchronized"
    assert K.clock_synced(str(flag)) is True             # no timesyncd on this box: nothing to wait for
    flag.parent.mkdir()
    assert K.clock_synced(str(flag)) is False
    flag.write_text("")
    assert K.clock_synced(str(flag)) is True


# -- reset-settings.txt on the boot drive ------------------------------------------------------------
RESET = os.path.join(ROOT, "system", "usb-router-reset-settings.sh")


@pytest.mark.parametrize("name", ["reset-settings.txt", "RESET-SETTINGS.TXT", "reset-settings.txt.txt"])
def test_reset_file_clears_saved_settings_once(tmp_path, name):
    state, boot = tmp_path / "state", tmp_path / "boot"
    state.mkdir(); boot.mkdir()
    (state / "settings.json").write_text('{"homepage": "https://saved.example"}')
    (state / "secrets.json").write_text("{}")
    (boot / name).write_text("")
    out = subprocess.run(["sh", RESET, str(state), str(boot)], capture_output=True, text=True, check=True).stdout
    assert not (state / "settings.json").exists() and (state / "secrets.json").exists()
    assert not (boot / name).exists() and (boot / "reset-settings.done").exists() and "cleared" in out
    (state / "settings.json").write_text("{}")             # a save after the reset survives the next start
    subprocess.run(["sh", RESET, str(state), str(boot)], check=True)
    assert (state / "settings.json").exists()


def test_no_reset_file_touches_nothing(tmp_path):
    state, boot = tmp_path / "state", tmp_path / "boot"
    state.mkdir(); boot.mkdir()
    (state / "settings.json").write_text("{}")
    subprocess.run(["sh", RESET, str(state), str(boot), str(tmp_path / "missing")], check=True)
    assert (state / "settings.json").exists()


def test_the_connect_service_runs_the_reset_before_the_server():
    unit = open(os.path.join(ROOT, "system", "usb-router-connect.service"), encoding="utf-8").read()
    pre = unit.index("usb-router-reset-settings.sh")
    assert "ExecStartPre=+/bin/sh /opt/usb-router/system/usb-router-reset-settings.sh /var/lib/usb-router" in unit
    assert pre < unit.index("ExecStart=/usr/bin/python3")


# -- dev4's raises carried from #1528 ----------------------------------------------------------------
def test_a_repeat_whose_used_line_is_broken_by_a_join_is_one_sentence(tmp_path, monkeypatch):
    text = "[usb-router]\nhomepage = https://a.example\nkiosk = true\nhomepage = https://b.example\n  /tv\n"
    problems = _config(tmp_path, monkeypatch, text.encode()).conf_problems()
    assert problems[0] == ("usb-router.conf: homepage is set 2 times (lines 2, 4); the last one, line 4, is used, "
                           "but line 5 starts with a space, so it is added to it and homepage is wrong. "
                           "Remove the space at the start.")
    assert not any("added to homepage above it" in p for p in problems)   # not said twice


def test_all_kinds_in_one_file_come_in_order(tmp_path, monkeypatch):
    text = ("[usb-router]\nhome_page = x\n"           # 2 unknown setting       (rank 3)
            "https://a.example\n"                     # 3 a bare web address    (rank 2)
            "junk\n"                                  # 4 not a setting         (rank 2)
            "device_name = Lobby\n  left\n"           # 6 a join, no repeat     (rank 1)
            "homepage = https://a.example\nhomepage = https://b.example\n")   # 7, 8 repeat (rank 0)
    problems = _config(tmp_path, monkeypatch, text.encode(), saved={"homepage": "https://saved.example"}).conf_problems()
    starts = [
        "usb-router.conf: homepage is set 2 times (lines 7, 8); the last one, line 8, is used.",
        "Content URL was saved on this screen",                                  # rank 0.5
        "usb-router.conf, line 6: starts with a space, so it is added to device_name",
        "usb-router.conf, line 3: a web address on its own",
        "usb-router.conf, line 4: not a setting (no = sign)",
        "usb-router.conf, line 2: not a setting the player knows",
    ]
    assert len(problems) == len(starts), problems
    for got, want in zip(problems, starts):
        assert got.startswith(want), (got, want)
