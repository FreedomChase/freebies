"""
Tests for the connection manager, run against the mock NetworkManager.

    USB_ROUTER_MOCK=1 python3 -m pytest tests/
"""
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
    for mod in ("server", "config", "netman", "router", "fleet_client"):
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
    for mod in ("server", "config", "netman", "router", "fleet_client"):
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
