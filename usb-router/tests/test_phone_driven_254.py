"""C&G #254 v8.7: Shaun's Pi has no working keyboard or mouse, so every screen action must be
reachable from a phone with the admin PIN, and the screen must say where the phone finds it."""
import importlib, os, sys, time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "pizero"))

PHONE_HOTSPOT = {"REMOTE_ADDR": "10.42.0.7"}
PHONE_LAN = {"REMOTE_ADDR": "192.168.1.77"}
PIN = {"X-Admin-Pin": "246810"}


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    server._pin_failures.clear()
    return server


def test_phone_opens_and_closes_the_setup_window_with_the_pin(srv):
    c = srv.app.test_client()
    assert c.post("/api/setup-mode", json={"action": "open"}, environ_base=PHONE_LAN).status_code == 401
    assert srv.router.setup_window_open() is False
    r = c.post("/api/setup-mode", json={"action": "open"}, environ_base=PHONE_LAN, headers=PIN)
    assert r.status_code == 200 and srv.router.setup_window_open() is True
    assert "testpass123" not in r.get_data(as_text=True)      # the phone never gets the hotspot password
    r = c.post("/api/setup-mode", json={"action": "close"}, environ_base=PHONE_HOTSPOT, headers=PIN)
    assert r.status_code == 200 and srv.router.setup_window_open() is False


def test_setup_window_from_a_phone_is_refused_when_lan_access_is_off(srv):
    srv.cfg.values["lan_access"] = "false"
    c = srv.app.test_client()
    assert c.post("/api/setup-mode", json={"action": "open"}, environ_base=PHONE_LAN, headers=PIN).status_code == 403


def test_phone_with_pin_reaches_every_screen_action(srv):
    """Wi-Fi join, content URL, kiosk, sharing, reload, reboot, shut down: all 200 from a phone."""
    c = srv.app.test_client()
    calls = [
        ("/api/wifi/connect", {"ssid": "Home WiFi", "password": "correct-horse"}),
        ("/api/settings", {"homepage": "https://scoreboard.example/tv", "kiosk": "true", "reload_every": "5"}),
        ("/api/settings", {"hotspot_enabled": "false", "setup_hotspot": "true"}),
        ("/api/browser/go", {"url": "https://scoreboard.example/tv"}),
        ("/api/system", {"action": "reload"}),
        ("/api/system", {"action": "restart-browser"}),
        ("/api/system", {"action": "reboot"}),
        ("/api/system", {"action": "shutdown"}),
    ]
    for path, payload in calls:
        assert c.post(path, json=payload, environ_base=PHONE_HOTSPOT).status_code == 401, path
        r = c.post(path, json=payload, environ_base=PHONE_HOTSPOT, headers=PIN)
        assert r.status_code == 200, (path, r.get_json())
    assert srv.cfg.get("homepage") == "https://scoreboard.example/tv"


def test_status_gives_the_lan_address_once_on_a_network(srv):
    c = srv.app.test_client()
    assert c.get("/api/status").get_json()["lan_url"] is None          # mock starts offline
    srv.nm.connected_ssid = "Home WiFi"
    url = c.get("/api/status").get_json()["lan_url"]
    assert url == f"http://192.168.1.42:{srv.cfg.get('listen_port')}"
    srv.cfg.values["lan_access"] = "false"
    assert c.get("/api/status").get_json()["lan_url"] is None          # not reachable from a phone then


def test_lan_qr_is_for_the_screen_only(srv):
    c = srv.app.test_client()
    srv.nm.connected_ssid = "Home WiFi"
    assert c.get("/api/qr?kind=lan", environ_base=PHONE_LAN).status_code == 403
    r = c.get("/api/qr?kind=lan")
    assert r.status_code in (200, 501)   # 501 only where the qrcode library is not installed
    srv.nm.connected_ssid = None
    assert c.get("/api/qr?kind=lan").status_code in (404, 501)


def test_poll_holds_after_a_join_that_worked(srv):
    c = srv.app.test_client()
    srv.nm.connected_ssid = "Home WiFi"
    srv.nm.last_join = {"ssid": "Home WiFi", "step": "done", "ok": True, "at": time.time()}
    hold = c.get("/api/browser/poll").get_json()["hold"]
    assert 15 <= hold <= srv.JOIN_ADDRESS_HOLD
    srv.nm.last_join["at"] = time.time() - srv.JOIN_ADDRESS_HOLD - 1
    assert c.get("/api/browser/poll").get_json()["hold"] == 0
    srv.nm.last_join = {"ssid": "Home WiFi", "step": "failed", "ok": False, "at": time.time()}
    assert c.get("/api/browser/poll").get_json()["hold"] == 0


def test_page_has_the_phone_controls(srv):
    html = srv.app.test_client().get("/", environ_base=PHONE_HOTSPOT).get_data(as_text=True)
    for needle in ('id="phone-more"', 'id="set-homepage"', 'data-action="reboot"', 'data-action="shutdown"',
                   'id="hotspot-enabled"', 'id="setup-open"', 'id="set-kiosk"', 'id="wifi-list"'):
        assert needle in html, needle


def test_kiosk_waits_on_the_setup_page_while_the_server_holds():
    K = importlib.import_module("kiosk")
    from test_pizero_kiosk import World, FakeCog, CONNECT, HOME
    w, cog = World(), FakeCog()
    k = K.Kiosk(cog, CONNECT, fetch=w.fetch, check=w.check, clock=w.clock)
    k.start()
    w.poll.update(online=True, hold=12)
    assert k.step() == "holding" and k.mode == "setup"
    w.poll["hold"] = 0
    assert k.step() == "left-setup" and k.mode == "content"
    assert ("open", HOME) in cog.log or ("start", HOME) in cog.log
