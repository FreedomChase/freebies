"""
Tests for the connection manager, run against the mock NetworkManager.

    USB_ROUTER_MOCK=1 python3 -m pytest tests/
"""
import time
import importlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    server._pin_failures.clear()
    with server.app.test_client() as c:
        c.server = server
        # every admin call in these tests carries the PIN, as the screen's page does
        original_open = c.open

        def open_with_pin(*a, **kw):
            kw.setdefault("headers", {})
            if isinstance(kw["headers"], dict) and "X-Admin-Pin" not in kw["headers"] and not kw.pop("no_pin", False):
                kw["headers"] = {**kw["headers"], "X-Admin-Pin": "246810"}
            else:
                kw.pop("no_pin", None)
            return original_open(*a, **kw)
        c.open = open_with_pin
        yield c


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"USB Router" in r.data


def test_status_offline_initially(client):
    r = client.get("/api/status").get_json()
    assert r["ok"] and r["online"] is False
    assert r["uplink"] is None
    assert any(d["type"] == "wifi" for d in r["devices"])
    assert r["hotspot"]["active"] is False


def test_scan_lists_networks_with_saved_flag(client):
    nets = client.get("/api/wifi/scan").get_json()["networks"]
    names = {n["ssid"] for n in nets}
    assert {"Home WiFi", "Cafe Guest"} <= names
    home = next(n for n in nets if n["ssid"] == "Home WiFi")
    assert home["saved"] is True and home["secured"] is True


def test_connect_wrong_password_is_reported(client):
    r = client.post("/api/wifi/connect", json={"ssid": "Neighbour", "password": "incorrect1"})
    assert r.status_code == 502
    assert "password" in r.get_json()["error"].lower()


def test_short_password_rejected(client):
    r = client.post("/api/wifi/connect", json={"ssid": "Neighbour", "password": "abc"})
    assert r.status_code == 400


def test_connect_then_online_and_browser_handoff(client):
    r = client.post("/api/wifi/connect", json={"ssid": "Home WiFi"}).get_json()
    assert r["ok"] and r["online"] is True
    status = client.get("/api/status?force=1").get_json()
    assert status["online"] and status["uplink"]["iface"] == "wlan0"

    # the browser polls; nothing to navigate to yet but we are online
    poll = client.get("/api/browser/poll").get_json()
    assert poll["online"] is True and poll["navigate"] is None

    # user clicks "Start browsing" with a custom URL (scheme gets added)
    client.post("/api/browser/go", json={"url": "example.org"})
    poll = client.get("/api/browser/poll").get_json()
    assert poll["navigate"] == "https://example.org"
    # consumed once
    assert client.get("/api/browser/poll").get_json()["navigate"] is None


def test_captive_portal_detected(client):
    r = client.post("/api/wifi/connect", json={"ssid": "Hotel-Lobby"}).get_json()
    assert r["online"] is False and r["captive_portal"] is True
    status = client.get("/api/status?force=1").get_json()
    assert status["captive_portal"] and status["portal_url"]


def test_hotspot_requires_uplink_then_starts(client):
    r = client.post("/api/hotspot", json={"action": "start"})
    # wlan1 is AP-capable and free even when offline, so the hotspot can start;
    # the keeper only auto-starts when online.
    assert r.status_code == 200
    h = r.get_json()["hotspot"]
    assert h["active"] and h["iface"] in ("wlan0", "wlan1") and h["mode"] == "wifi"
    assert h["ssid"] == "USB-Router"

    r = client.post("/api/hotspot", json={"action": "stop"}).get_json()
    assert r["hotspot"]["active"] is False


def test_hotspot_does_not_steal_uplink(client):
    client.post("/api/wifi/connect", json={"ssid": "Home WiFi"})
    r = client.post("/api/hotspot", json={"action": "start", "iface": "wlan0"})
    assert r.status_code == 502
    cands = client.get("/api/hotspot").get_json()["candidates"]
    assert all(c["iface"] != "wlan0" for c in cands)


def test_settings_validation_and_persistence(client, tmp_path):
    r = client.post("/api/settings", json={"hotspot_password": "short"})
    assert r.status_code == 400
    r = client.post("/api/settings", json={"homepage": "example.com", "kiosk": "yes", "hotspot_ssid": "MyStick"}).get_json()
    assert r["settings"]["homepage"] == "https://example.com"
    assert r["settings"]["kiosk"] is True
    assert (tmp_path / "settings.json").exists()
    # the PIN is returned to the screen itself only; the fleet token never
    assert r["settings"]["admin_pin"] == "246810" and "fleet_token" not in r["settings"]
    remote = client.get("/api/settings", environ_base={"REMOTE_ADDR": "10.42.0.3"}).get_json()["settings"]
    assert remote["admin_pin"] is None


def test_every_setting_change_requires_pin(client):
    remote = {"REMOTE_ADDR": "10.42.0.17"}
    for env in ({}, remote):
        for path, payload in (("/api/settings", {"homepage": "https://evil.example"}),
                              ("/api/settings", {"fleet_url": "https://evil.example"}),
                              ("/api/settings", {"hotspot_password": "attacker-pass"}),
                              ("/api/wifi/connect", {"ssid": "Home WiFi"}),
                              ("/api/hotspot", {"action": "stop"}),
                              ("/api/system", {"action": "reload"}),
                              ("/api/browser/go", {"url": "https://evil.example"})):
            r = client.post(path, json=payload, environ_base=env, no_pin=True)
            assert r.status_code == 401, (path, env)
            assert r.get_json()["pin_required"] is True
    # nothing changed
    s = client.get("/api/settings").get_json()["settings"]
    assert s["homepage"] != "https://evil.example" and s["fleet_url"] == ""
    # wrong PIN is also 401; right PIN works from both sides
    assert client.post("/api/settings", json={"kiosk": True}, headers={"X-Admin-Pin": "000000"}).status_code == 401
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote).status_code == 200
    assert client.post("/api/settings", json={"kiosk": True}).status_code == 200
    # LAN access off -> remote refused outright even with the PIN
    client.server.cfg.values["lan_access"] = "false"
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote).status_code == 403
    assert client.post("/api/browser/report", json={"url": "x"}, environ_base=remote).status_code == 403


def test_pin_brute_force_lockout(client):
    remote = {"REMOTE_ADDR": "10.42.0.99"}
    for _ in range(5):
        assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote, headers={"X-Admin-Pin": "111111"}).status_code == 401
    r = client.post("/api/settings", json={"kiosk": True}, environ_base=remote, headers={"X-Admin-Pin": "111111"})
    assert r.status_code == 401 and "locked" in r.get_json()["error"]
    # now even the right PIN waits
    r = client.post("/api/settings", json={"kiosk": True}, environ_base=remote)
    assert r.status_code == 429
    # another address is unaffected
    assert client.post("/api/settings", json={"kiosk": True}, environ_base={"REMOTE_ADDR": "10.42.0.98"}).status_code == 200


def test_pin_shown_to_screen_only(client):
    local = client.get("/api/status").get_json()
    assert local["local"] is True and local["settings"]["admin_pin"] == "246810"
    remote = client.get("/api/status", environ_base={"REMOTE_ADDR": "10.42.0.5"}).get_json()
    assert remote["local"] is False and remote["settings"]["admin_pin"] is None
    assert remote["settings"]["hotspot_password"] is None
    page = client.get("/", environ_base={"REMOTE_ADDR": "10.42.0.5"})
    assert b"246810" not in page.data


def test_terse_parser_handles_escaped_colons():
    from netman import _split_terse
    assert _split_terse(r"AA\:BB\:CC:wifi:connected") == ["AA:BB:CC", "wifi", "connected"]
    assert _split_terse("a::c") == ["a", "", "c"]


def test_internet_check_mock_verdicts():
    from netman import MockNetworkManager, internet_check
    nm = MockNetworkManager()
    assert internet_check("http://x", mock_backend=nm)["online"] is False
    nm.connected_ssid = "Home WiFi"
    assert internet_check("http://x", mock_backend=nm)["online"] is True


# ---------------------------------------------------------------- signage --
def test_provisioned_wifi_profiles_created(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USB_ROUTER_WIFI_SSID", "Venue-Staff")
    monkeypatch.setenv("USB_ROUTER_WIFI_PASSWORD", "venuepass1")
    monkeypatch.setenv("USB_ROUTER_WIFI_SSID_2", "Home WiFi")  # already saved -> not re-created
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("server")
    server.provision_wifi()
    assert server._state["provisioned"] == ["Venue-Staff"]
    assert "Venue-Staff" in server.nm.saved
    with server.app.test_client() as c:
        s = c.get("/api/status").get_json()
        assert s["device"]["provisioned"] == ["Venue-Staff"]
        assert s["settings"]["provisioned_wifi"] == ["Venue-Staff", "Home WiFi"]


def test_device_id_and_url_templating(client):
    srv = client.server
    srv.cfg.values["device_id"] = "scr-lobby-1"
    srv.cfg.values["homepage"] = "https://signage.example/play?screen={device_id}&n={device_name}"
    srv.cfg.values["device_name"] = "Lobby left"
    poll = client.get("/api/browser/poll").get_json()
    assert poll["homepage"] == "https://signage.example/play?screen=scr-lobby-1&n=Lobby left"
    assert poll["device_id"] == "scr-lobby-1"
    # default id is derived, never empty
    srv.cfg.values["device_id"] = ""
    assert srv.cfg.device_id().startswith("scr-")


def test_setup_hotspot_status_and_qr(client):
    # offline + hotspot active = "setup" mode with a UI URL for phones
    client.post("/api/hotspot", json={"action": "start"})
    s = client.get("/api/status?force=1").get_json()
    assert s["online"] is False
    assert s["hotspot"]["setup"] is True
    assert s["hotspot"]["ui_url"].startswith("http://10.42.0.1:")
    assert s["hotspot"]["password"] == "testpass123"  # loopback sees it
    remote = client.get("/api/status?force=1", environ_base={"REMOTE_ADDR": "10.42.0.17"}).get_json()
    assert remote["hotspot"]["password"] is None and remote["settings"]["admin_pin"] is None
    assert client.get("/api/qr?kind=wifi", environ_base={"REMOTE_ADDR": "10.42.0.17"}).status_code == 403
    for kind in ("wifi", "ui"):
        r = client.get(f"/api/qr?kind={kind}")
        if r.status_code == 501:
            pytest.skip("qrcode not installed")
        assert r.status_code == 200 and r.mimetype == "image/png"
    assert client.get("/api/qr?kind=nope").status_code == 400


def test_single_card_hands_hotspot_over_to_wifi(client):
    # hotspot running on wlan0, user asks to join a network on wlan0 -> hotspot drops, connect succeeds
    client.post("/api/hotspot", json={"action": "start", "iface": "wlan0", "mode": "wifi"})
    assert client.get("/api/status").get_json()["hotspot"]["iface"] == "wlan0"
    r = client.post("/api/wifi/connect", json={"ssid": "Home WiFi", "iface": "wlan0"}).get_json()
    assert r["ok"] and r["online"]
    assert client.get("/api/status?force=1").get_json()["hotspot"]["active"] is False


def test_keeper_logic_offline_setup_vs_online_share(client):
    srv = client.server
    rt = srv.router
    assert rt.wants_setup_hotspot() is True
    srv.cfg.save({"setup_hotspot": False})
    assert rt.wants_setup_hotspot() is False
    srv.cfg.save({"setup_hotspot": True})
    rt.stop()  # manual stop disables auto mode
    assert rt.wants_setup_hotspot() is False
    srv.cfg.save({"setup_hotspot": True})  # re-enabling via settings endpoint clears it
    client.post("/api/settings", json={"setup_hotspot": True})
    assert rt.wants_setup_hotspot() is True


def test_system_actions_reach_browser_poll(client):
    r = client.post("/api/system", json={"action": "reload"})
    assert r.status_code == 200
    assert client.get("/api/browser/poll").get_json()["action"] == "reload"
    assert client.get("/api/browser/poll").get_json()["action"] is None
    client.post("/api/system", json={"action": "restart-browser"})
    assert client.get("/api/browser/poll").get_json()["action"] == "restart"
    assert client.post("/api/system", json={"action": "format-disk"}).status_code == 400
    assert client.post("/api/system", json={"action": "reboot"}).status_code == 200  # mock: logged only


def test_browser_report_and_reload_settings(client):
    client.post("/api/browser/report", json={"url": "https://signage.example/play"})
    assert client.get("/api/status").get_json()["current_url"] == "https://signage.example/play"
    r = client.post("/api/settings", json={"reload_every": "30", "rotation": "left", "device_name": "Lobby"}).get_json()
    assert r["settings"]["reload_every"] == 30 and r["settings"]["rotation"] == "left"
    assert client.get("/api/browser/poll").get_json()["reload_every"] == 30
    assert client.post("/api/settings", json={"rotation": "sideways"}).status_code == 400
    assert client.post("/api/settings", json={"reload_every": "soon"}).status_code == 400
    assert client.get("/api/display").get_json()["rotation"] == "left"


def test_fleet_client_applies_reply_and_hides_token(client):
    srv = client.server
    srv.cfg.save({"fleet_url": "https://fleet.example", "fleet_token": "s3cret"})
    applied = []
    srv.fleet.on_action = applied.append
    result = srv.fleet.apply({"homepage": "https://new.example/{device_id}", "rotation": "right",
                              "reload_every": 15, "name": "Lobby right", "action": "reload", "kiosk": False})
    assert result["changed"]["homepage"] == "https://new.example/{device_id}"
    assert srv.cfg.get("rotation") == "right" and srv.cfg.get("device_name") == "Lobby right"
    assert srv.cfg.flag("kiosk") is False
    assert applied == ["reload"]
    public = client.get("/api/settings").get_json()["settings"]
    assert "fleet_token" not in public and public["fleet_configured"] is True
    # ping with an unreachable dashboard reports a clean error
    r = client.post("/api/fleet/ping")
    assert r.status_code == 502 and "unreachable" in r.get_json()["error"].lower()


def test_fleet_dashboard_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_TOKEN", "tok")
    monkeypatch.setenv("FLEET_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("FLEET_DB_PATH", str(tmp_path / "fleet.db"))
    sys.path.insert(0, os.path.join(HERE, "..", "fleet"))
    sys.modules.pop("server", None)
    fleet = importlib.import_module("server")
    sys.modules.pop("server", None)
    sys.path.pop(0)
    import base64
    admin = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
    with fleet.app.test_client() as c:
        assert c.post("/api/heartbeat", json={"device_id": "scr-1"}).status_code == 401
        r = c.post("/api/heartbeat", json={"device_id": "scr-1", "name": "", "online": True},
                   headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200 and r.get_json() == {}
        assert c.get("/api/screens").status_code == 401
        rows = c.get("/api/screens", headers=admin).get_json()["screens"]
        assert rows[0]["device_id"] == "scr-1" and rows[0]["online"] is True
        c.post("/api/screens/scr-1", json={"homepage": "https://x.example", "rotation": "left", "action": "reboot",
                                           "group_name": "lobby"}, headers=admin)
        reply = c.post("/api/heartbeat", json={"device_id": "scr-1"}, headers={"Authorization": "Bearer tok"}).get_json()
        assert reply == {"homepage": "https://x.example", "rotation": "left", "action": "reboot"}
        # action is one-shot
        reply = c.post("/api/heartbeat", json={"device_id": "scr-1"}, headers={"Authorization": "Bearer tok"}).get_json()
        assert "action" not in reply
        r = c.post("/api/groups/lobby", json={"homepage": "https://group.example"}, headers=admin).get_json()
        assert r["updated"] == ["scr-1"]
        assert c.get("/", headers=admin).status_code == 200


# ---------------------------------------------------------- secrets / hotspot --
def test_generated_secrets_per_stick(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("USB_ROUTER_HOTSPOT_PASSWORD", raising=False)
    monkeypatch.delenv("USB_ROUTER_ADMIN_PIN", raising=False)
    sys.modules.pop("config", None)
    config = importlib.import_module("config")
    cfg = config.Config()
    pin, pw = cfg.admin_pin(), cfg.hotspot_password()
    assert len(pin) == 6 and pin.isdigit()
    assert len(pw) >= 12 and not config.is_weak_password(pw)
    assert (tmp_path / "secrets.json").exists()
    assert oct(os.stat(tmp_path / "secrets.json").st_mode & 0o777) == "0o600"
    # stable across restarts, different per stick
    assert config.Config().admin_pin() == pin and config.Config().hotspot_password() == pw
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "other"))
    other = config.Config()
    assert other.hotspot_password() != pw
    # weak configured values are replaced by the generated one; good ones are used
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "changeme123")
    assert config.Config().hotspot_password() != "changeme123"
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "a-proper-passphrase")
    assert config.Config().hotspot_password() == "a-proper-passphrase"


def test_hotspot_refuses_weak_password(client):
    srv = client.server
    srv.cfg.values["hotspot_password"] = ""
    srv.cfg.secrets["hotspot_password"] = "changeme123"  # simulate a tampered secrets file
    r = client.post("/api/hotspot", json={"action": "start"})
    assert r.status_code == 502 and "default password" in r.get_json()["error"]
    assert client.get("/api/status").get_json()["hotspot"]["active"] is False
    # the settings endpoint will not accept a weak one either
    assert client.post("/api/settings", json={"hotspot_password": "password1"}).status_code == 400
    assert client.post("/api/settings", json={"hotspot_password": "changeme123"}).status_code == 400
    # rotation produces a usable one and the hotspot can start
    r = client.post("/api/secrets/rotate", json={"name": "hotspot_password"}).get_json()
    assert r["settings"]["hotspot_password"] and r["settings"]["hotspot_password"] != "changeme123"
    assert client.post("/api/hotspot", json={"action": "start"}).status_code == 200


def test_setup_hotspot_off_once_online(client):
    srv, rt = client.server, client.server.router
    rt.booted -= 60                      # past the boot grace period
    srv.nm.saved.clear()                 # an unconfigured screen
    assert rt.is_configured() is False
    assert rt.desired(online=False) == "setup"
    assert rt.tick(online=False) == "started-setup"
    assert srv.nm.hotspot_status()["active"] and rt.role == "setup"
    # the screen comes online -> the setup hotspot goes away and stays away
    assert rt.desired(online=True) == "off"
    assert rt.tick(online=True) == "stopped-setup"
    assert srv.nm.hotspot_status()["active"] is False
    assert rt.tick(online=True) == "noop"
    # end to end: connect via the API, then run the keeper step
    srv.nm.saved.clear()
    rt.tick(online=False)
    client.post("/api/wifi/connect", json={"ssid": "Home WiFi", "password": "venue-pass-1"})
    online = client.get("/api/status?force=1").get_json()["online"]
    assert online is True
    rt.tick(online=True)
    assert client.get("/api/status").get_json()["hotspot"]["active"] is False
    # sharing is opt-in and separate
    assert srv.cfg.flag("hotspot_enabled") is False
    client.post("/api/settings", json={"hotspot_enabled": True})
    assert rt.desired(online=True) == "share"


def test_hotspot_left_up_across_app_restart_is_switched_off_once_online(client):
    """dev3's gap on #180: the app restarts while NetworkManager keeps the setup hotspot up."""
    srv, rt = client.server, client.server.router
    rt.booted -= 60
    srv.nm.saved.clear()
    srv.nm.hotspot_start("wlan0", "USB-Router", "testpass123", "bg")   # up, but not started by this process
    assert rt.role is None and srv.nm.hotspot_status()["active"] is True
    # still offline and unconfigured: the adopted hotspot stays up for phone setup
    assert rt.tick(online=False) == "noop"
    assert rt.role == "setup" and srv.nm.hotspot_status()["active"] is True
    # online: it goes away, as a hotspot this process started would
    assert rt.tick(online=True) == "stopped-setup"
    assert srv.nm.hotspot_status()["active"] is False
    assert rt.tick(online=True) == "noop"


def test_hotspot_left_up_while_sharing_is_kept_after_app_restart(client):
    srv, rt = client.server, client.server.router
    rt.booted -= 60
    client.post("/api/settings", json={"hotspot_enabled": True})
    srv.nm.hotspot_start("wlan1", "USB-Router", "testpass123", "bg")
    assert rt.role is None
    assert rt.tick(online=True) == "noop"
    assert rt.role == "share" and srv.nm.hotspot_status()["active"] is True
    # sharing switched off -> the adopted hotspot stops (the settings route or the next tick)
    client.post("/api/settings", json={"hotspot_enabled": False})
    rt.tick(online=True)
    assert srv.nm.hotspot_status()["active"] is False


def test_setup_hotspot_only_when_unconfigured_or_requested(client):
    srv, rt = client.server, client.server.router
    rt.booted -= 60
    # configured (a saved Wi-Fi exists) and offline -> no broadcast
    assert rt.is_configured() is True
    assert rt.desired(online=False) == "off"
    assert rt.tick(online=False) == "noop"
    # Ctrl+N from the screen re-opens it (loopback only)
    assert client.post("/api/setup-mode", json={"action": "open"}, environ_base={"REMOTE_ADDR": "10.42.0.2"}).status_code == 403
    r = client.post("/api/setup-mode", json={"action": "open"}).get_json()
    assert r["hotspot"]["setup_window_until"]
    assert rt.desired(online=False) == "setup"
    assert rt.tick(online=False) == "started-setup"
    # but never while online
    assert rt.desired(online=True) == "off"
    assert rt.tick(online=True) == "stopped-setup"
    client.post("/api/setup-mode", json={"action": "close"})
    assert rt.setup_window_open() is False
    # provisioned Wi-Fi in the config also counts as configured
    srv.nm.saved.clear()
    srv.cfg.values["wifi_ssid"] = "Venue"
    assert rt.is_configured() is True and rt.desired(online=False) == "off"
    # boot grace: an unconfigured screen waits before broadcasting
    srv.cfg.values["wifi_ssid"] = ""
    rt.booted = __import__("time").time()
    assert rt.desired(online=False) == "off"


def test_fleet_client_requires_token(client):
    srv = client.server
    client.post("/api/settings", json={"fleet_url": "https://fleet.example"})
    assert srv.fleet.enabled() is False
    assert srv.fleet.heartbeat() is None
    info = client.get("/api/status").get_json()["fleet"]
    assert info["enabled"] is False and "token" in info["last_error"]
    r = client.post("/api/fleet/ping")
    assert r.status_code == 400 and "token" in r.get_json()["error"]
    client.post("/api/settings", json={"fleet_token": "0123456789abcdef0123456789abcdef"})
    assert srv.fleet.enabled() is True


def test_fleet_dashboard_refuses_without_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_DB_PATH", str(tmp_path / "f.db"))
    for tok, pw in (("", ""), ("", "longenoughpw"), ("0123456789abcdef", ""), ("short", "longenoughpw"), ("0123456789abcdef", "short")):
        monkeypatch.setenv("FLEET_TOKEN", tok)
        monkeypatch.setenv("FLEET_ADMIN_PASSWORD", pw)
        sys.path.insert(0, os.path.join(HERE, "..", "fleet"))
        sys.modules.pop("server", None)
        fleet = importlib.import_module("server")
        sys.modules.pop("server", None)
        sys.path.pop(0)
        with pytest.raises(SystemExit):
            fleet.require_config()
        with fleet.app.test_client() as c:  # and it never serves the dashboard open
            assert c.get("/api/screens").status_code in (401, 503)
            assert c.post("/api/heartbeat", json={"device_id": "x"}).status_code == 401
    monkeypatch.setenv("FLEET_TOKEN", "0123456789abcdef")
    monkeypatch.setenv("FLEET_ADMIN_PASSWORD", "longenoughpw")
    sys.path.insert(0, os.path.join(HERE, "..", "fleet"))
    sys.modules.pop("server", None)
    fleet = importlib.import_module("server")
    sys.modules.pop("server", None)
    sys.path.pop(0)
    fleet.require_config()  # ok


def test_no_default_secrets_in_code_or_docs():
    """Secrets that once appeared in the README must not exist anywhere in the shipped tree."""
    forbidden = ("changeme123", "letmein", "supersecret", "admin123", "hunter2")
    root = os.path.abspath(os.path.join(HERE, ".."))
    offenders = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".pytest_cache", "work", "out", "tests")]
        for name in filenames:
            if name.endswith((".png", ".db", ".pyc", ".iso")):
                continue
            path = os.path.join(dirpath, name)
            try:
                text = open(path, encoding="utf-8", errors="ignore").read().lower()
            except OSError:
                continue
            for word in forbidden:
                if word in text:
                    # the blocklist in config.py and the "refused" note in docs are the only allowed mentions
                    allowed = (name == "config.py" and "WEAK_PASSWORDS" in open(path, encoding="utf-8").read())
                    if not allowed:
                        offenders.append((os.path.relpath(path, root), word))
    assert offenders == [], offenders
    # and the config example ships blank secrets
    example = open(os.path.join(root, "config", "usb-router.conf.example"), encoding="utf-8").read()
    for key in ("hotspot_password", "admin_pin", "fleet_token", "wifi_password"):
        assert any(line.strip() == f"{key} =" for line in example.splitlines()), key


def test_real_hotspot_profile_is_plain_wpa2_without_pmf(monkeypatch):
    """S&D #191: the profile the real backend creates is WPA2-PSK/CCMP with PMF off (iPhones on brcmfmac)."""
    for mod in ("netman",):
        sys.modules.pop(mod, None)
    netman = importlib.import_module("netman")
    calls = []
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: calls.append(a) or "")
    netman.NetworkManager().hotspot_start("wlan0", "USB-Router", "testpass123", "bg")
    add = next(c for c in calls if c[:2] == ("connection", "add"))
    opts = dict(zip(add[2::2], add[3::2]))
    assert opts["802-11-wireless.mode"] == "ap"
    assert opts["wifi-sec.key-mgmt"] == "wpa-psk"
    assert opts["wifi-sec.proto"] == "rsn"
    assert opts["wifi-sec.pairwise"] == "ccmp" and opts["wifi-sec.group"] == "ccmp"
    assert opts["wifi-sec.pmf"] == "disable"
    assert ("connection", "up", "usb-router-hotspot") in calls


def test_password_on_screen_is_the_one_the_hotspot_gets(client, tmp_path):
    """S&D #191: Shaun's sequence (New hotspot password > Save > Start sharing): the password
    the screen shows is the one handed to NetworkManager, and it survives an app restart."""
    srv = client.server
    given = []
    real = srv.nm.hotspot_start
    srv.nm.hotspot_start = lambda iface, ssid, pw, band="bg": (given.append(pw), real(iface, ssid, pw, band))
    before = client.get("/api/settings").get_json()["settings"]["hotspot_password"]
    shown = client.post("/api/secrets/rotate", json={"name": "hotspot_password"}).get_json()["settings"]["hotspot_password"]
    # a configured password (here the env, on a stick usb-router.conf or an earlier Save) used to
    # win over the generated one, so the button changed nothing (C&G #208): now it is replaced
    assert before == "testpass123" and shown != before
    assert client.get("/api/settings").get_json()["settings"]["hotspot_password_source"] == "generated"
    # Save posts the field as it stands (the rotated password)
    assert client.post("/api/settings", json={"hotspot_ssid": "USB-Router", "hotspot_password": shown,
                                              "hotspot_band": "bg"}).status_code == 200
    assert client.post("/api/hotspot", json={"action": "start"}).status_code == 200
    assert given[-1] == shown
    assert client.get("/api/status").get_json()["hotspot"]["password"] == shown
    # a typed password: saved, then used
    assert client.post("/api/settings", json={"hotspot_password": "ourown-pass-42"}).status_code == 200
    assert given[-1] == "ourown-pass-42"          # the running hotspot was restarted with it
    # both live in the state dir, so a restarted app (or reboot) hands NM the same one
    import json as _json
    saved = _json.loads((tmp_path / "settings.json").read_text())
    assert saved["hotspot_password"] == "ourown-pass-42"


def test_rotated_password_reaches_the_hotspot_when_none_is_configured(client):
    srv = client.server
    srv.cfg.values["hotspot_password"] = ""       # nothing in usb-router.conf, no earlier Save
    given = []
    real = srv.nm.hotspot_start
    srv.nm.hotspot_start = lambda iface, ssid, pw, band="bg": (given.append(pw), real(iface, ssid, pw, band))
    assert client.post("/api/hotspot", json={"action": "start"}).status_code == 200
    first = given[-1]
    shown = client.post("/api/secrets/rotate", json={"name": "hotspot_password"}).get_json()["settings"]["hotspot_password"]
    assert len(shown) == 12 and shown != first
    assert given[-1] == shown                      # the running hotspot was re-created with it
    assert client.get("/api/status").get_json()["hotspot"]["password"] == shown


def test_every_element_the_page_script_asks_for_exists(client):
    """S&D #193: the QR-first setup card renamed elements; a missing id breaks the whole page script."""
    import re
    html = client.get("/").get_data(as_text=True)
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js")).read()
    wanted = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
    have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    assert {"setup-card", "setup-phone", "qr-wifi", "qr-ui", "setup-pin", "pin-gate"} <= have
    assert wanted - have == set()


def test_pin_check_is_the_phone_gate(client):
    """S&D #193: a phone on the hotspot types the PIN first; /api/pin/check says yes or no, with the lockout."""
    remote = {"REMOTE_ADDR": "10.42.0.50"}
    assert client.post("/api/pin/check", environ_base=remote, no_pin=True).status_code == 401
    assert client.post("/api/pin/check", environ_base=remote, headers={"X-Admin-Pin": "000000"}).status_code == 401
    assert client.post("/api/pin/check", environ_base=remote).get_json()["ok"] is True
    html = client.get("/", environ_base=remote).get_data(as_text=True)
    assert 'id="pin-gate"' in html and 'id="pin-gate-input"' in html


def test_default_pin_from_the_image(tmp_path, monkeypatch):
    """S&D #193: one default PIN baked into the image; usb-router.conf beats it; New PIN beats it; never shown."""
    pin_file = tmp_path / "default-pin"
    pin_file.write_text("1234567890123456\n")
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("USB_ROUTER_DEFAULT_PIN_FILE", str(pin_file))
    monkeypatch.delenv("USB_ROUTER_ADMIN_PIN", raising=False)
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("server")
    server._pin_failures.clear()
    cfg = server.cfg
    cfg.values["admin_pin"] = ""                       # a usb-router.conf on this machine must not decide the test
    assert cfg.admin_pin_source() == "default" and cfg.admin_pin() == "1234567890123456"
    with server.app.test_client() as c:
        remote = {"REMOTE_ADDR": "10.42.0.51"}
        assert c.post("/api/pin/check", environ_base=remote, headers={"X-Admin-Pin": "1234567890123456"}).status_code == 200
        st = c.get("/api/status").get_json()
        assert st["settings"]["admin_pin_source"] == "default"
        assert c.get("/api/status", environ_base=remote).get_json()["settings"]["admin_pin"] is None
        # New PIN on this box replaces the default, and survives a restart (secrets.json)
        r = c.post("/api/secrets/rotate", json={"name": "admin_pin"}, headers={"X-Admin-Pin": "1234567890123456"})
        new = r.get_json()["settings"]["admin_pin"]
        assert len(new) == 6 and new != "1234567890123456"
        assert c.post("/api/pin/check", environ_base=remote, headers={"X-Admin-Pin": "1234567890123456"}).status_code == 401
        assert c.post("/api/pin/check", environ_base=remote, headers={"X-Admin-Pin": new}).status_code == 200
    import config as config_mod
    again = config_mod.Config()
    again.values["admin_pin"] = ""
    assert again.admin_pin_source() == "new" and again.admin_pin() == new
    # a weak or empty default file is ignored: back to the per-stick generated PIN
    pin_file.write_text("12a\n")
    fresh_state = tmp_path / "state2"
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(fresh_state))
    other = config_mod.Config()
    other.values["admin_pin"] = ""
    assert other.admin_pin_source() == "generated" and len(other.admin_pin()) == 6


def test_no_hotspot_password_text_on_the_page(client):
    """Shaun, 17 Nov: the hotspot password is in the QR only, never in text on the screen."""
    html = client.get("/").get_data(as_text=True)
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js")).read()
    assert 'id="setup-pass"' not in html and "setup-pass" not in js
    assert "h.password" not in js and "s.hotspot_password ||" not in js
    assert 'id="hs-pass" class="input" type="password"' in html


def test_screen_gets_a_wifi_check_line_phone_does_not(client):
    """S&D #193: the screen shows channel, security, chip handshake, phones joined, last join event."""
    client.post("/api/hotspot", json={"action": "start"})
    local = client.get("/api/status").get_json()["hotspot"]
    assert local["active"] and local["diag"]["channel"] == 6 and local["diag"]["fw_offload"] == "off"
    remote = client.get("/api/status", environ_base={"REMOTE_ADDR": "10.42.0.60"}).get_json()["hotspot"]
    assert remote["diag"] is None
    html = client.get("/").get_data(as_text=True)
    assert 'id="setup-diag"' in html and 'id="hs-diag"' in html


def test_hotspot_profile_fixed_channel_no_powersave(monkeypatch):
    """S&D #193: the AP profile pins the channel and turns Wi-Fi power saving off."""
    import netman
    calls = []
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: calls.append(a) or "")
    netman.NetworkManager.hotspot_start(netman.NetworkManager.__new__(netman.NetworkManager), "wlan0", "USB-Router", "abcdefgh2345", "bg")
    add = next(a for a in calls if a[:2] == ("connection", "add"))
    kv = {add[i]: add[i + 1] for i in range(len(add) - 1)}
    assert kv["802-11-wireless.channel"] == "6" and kv["802-11-wireless.powersave"] == "2"
    assert kv["wifi-sec.key-mgmt"] == "wpa-psk" and kv["wifi-sec.pmf"] == "disable"


def test_diag_parses_iw_and_journal(monkeypatch, tmp_path):
    import netman
    out = {
        "info": "Interface wlan0\n\ttype AP\n\tchannel 6 (2437 MHz), width: 20 MHz\n",
        "reg": "global\ncountry ES: DFS-ETSI\n",
        "station": "Station 66:55:44:33:22:11 (on wlan0)\n\tinactive time: 10 ms\n",
        "journalctl": "Nov 17 04:12:30 pi kernel: something else\nNov 17 04:12:33 pi wpa_supplicant[512]: wlan0: AP-STA-POSSIBLE-PSK-MISMATCH 66:55:44:33:22:11\n",
    }
    def fake_run(args, timeout=0, check=True):
        if args[0] == "journalctl":
            return out["journalctl"]
        return out["station" if "station" in args else ("reg" if "reg" in args else "info")]
    monkeypatch.setattr(netman, "_run", fake_run)
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: "wpa-psk\n1\n")
    d = netman.hotspot_diag("wlan0", "usb-router-hotspot")
    assert (d["channel"], d["freq"], d["country"], d["stations"]) == (6, 2437, "ES", 1)
    assert (d["security"], d["pmf"]) == ("wpa-psk", "off")
    assert d["last_event"] == "04:12:33 wlan0: AP-STA-POSSIBLE-PSK-MISMATCH 66:55:44:33:22:11"


def test_shut_down_needs_the_pin_and_tells_every_screen(client):
    """C&G #209: Shut down beside Reboot, same PIN gate; status says so, for the safe-to-unplug screen."""
    phone = {"REMOTE_ADDR": "10.42.0.29"}
    assert client.post("/api/system", json={"action": "shutdown"}, environ_base=phone, no_pin=True).status_code == 401
    assert client.get("/api/status").get_json()["power"] is None
    assert client.post("/api/system", json={"action": "shutdown"}, environ_base=phone).status_code == 200
    assert client.get("/api/status").get_json()["power"] == "shutdown"
    page = client.get("/").get_data(as_text=True)
    assert page.count('data-action="reboot"') == 1 and page.count('data-action="shutdown"') == 1
    assert "Safe to unplug when the green light stops blinking." in page


def test_rotate_replaces_a_configured_password_and_pin(client, tmp_path):
    """C&G #208: with a password set (conf/env/earlier Save), New hotspot password and New PIN
    took no effect, because the configured value outranks the generated one."""
    srv = client.server
    given = []
    real = srv.nm.hotspot_start
    srv.nm.hotspot_start = lambda iface, ssid, pw, band="bg", **kw: (given.append(pw), real(iface, ssid, pw, band, **kw))
    assert client.post("/api/settings", json={"hotspot_password": "first-pass-77"}).status_code == 200
    assert client.post("/api/hotspot", json={"action": "start"}).status_code == 200
    assert given[-1] == "first-pass-77"
    new = client.post("/api/secrets/rotate", json={"name": "hotspot_password"}).get_json()["settings"]["hotspot_password"]
    assert new != "first-pass-77"
    assert given[-1] == new                        # the running hotspot was restarted with it
    assert client.get("/api/status").get_json()["hotspot"]["password"] == new
    import json as _json
    assert _json.loads((tmp_path / "settings.json").read_text())["hotspot_password"] == ""
    # survives a restarted app: settings.json's blank outranks the env/conf value
    assert srv.Config().hotspot_password() == new
    # the PIN: configured 246810 (env), New PIN replaces it and the old one stops working
    assert srv.cfg.admin_pin_source() == "config"
    pin = srv.cfg.rotate_secret("admin_pin")
    assert srv.cfg.admin_pin() == pin != "246810"
    assert srv.cfg.admin_pin_source() == "new"


def test_restart_keeps_the_share_and_setup_role(client):
    """C&G #210: a password change while sharing restarted the hotspot as role manual,
    so it came back without share=True (no NAT) and the role was lost."""
    srv, rt = client.server, client.server.router
    calls = []
    real = srv.nm.hotspot_start
    srv.nm.hotspot_start = lambda iface, ssid, pw, band="bg", **kw: (calls.append(kw.get("share", False)), real(iface, ssid, pw, band, **kw))
    rt.start(role="share")
    assert calls[-1] is True and rt.role == "share"
    assert client.post("/api/settings", json={"hotspot_password": "share-pass-55"}).status_code == 200
    assert calls[-1] is True and rt.role == "share"
    client.post("/api/secrets/rotate", json={"name": "hotspot_password"})
    assert calls[-1] is True and rt.role == "share"
    rt.stop(manual=False)
    rt.start(role="setup")
    rt.restart()
    assert calls[-1] is False and rt.role == "setup"


def test_status_carries_the_last_venue_join_for_ten_minutes(client):
    """C&G #209 v8.3: the phone is gone during a join, so the screen shows its step and reason."""
    assert client.get("/api/status").get_json()["join"] is None
    nm = client.server.nm
    nm.last_join = {"ssid": "Livebox6s-CB36", "step": "failed", "ok": False, "reason": "wrong password",
                    "nm_reason": "no-secrets", "nm_error": "Error: x", "at": time.time()}
    j = client.get("/api/status").get_json()["join"]
    assert j["ssid"] == "Livebox6s-CB36" and j["reason"] == "wrong password" and "nm_error" not in j
    nm.last_join["at"] = time.time() - 601
    assert client.get("/api/status").get_json()["join"] is None


def test_page_has_the_join_banner_and_the_phone_drop_note(client):
    html = client.get("/").get_data(as_text=True)
    for el in ('id="join-banner"', 'id="setup-join"', 'id="wifi-drop"', 'id="wifi-drop-ssid"'):
        assert el in html
