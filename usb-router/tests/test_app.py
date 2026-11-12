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
    for mod in ("server", "config", "netman", "router"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        c.server = server
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
    # admin_pin never leaks
    assert "admin_pin" not in r["settings"]


def test_lan_access_rules(client):
    srv = client.server
    remote = {"REMOTE_ADDR": "10.42.0.17"}
    # default: LAN access on (phone-based setup), no PIN -> allowed; the hotspot password is the gate
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote).status_code == 200
    # with a PIN set, remote changes need the header; the local screen never does
    srv.cfg.values["admin_pin"] = "4321"
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote).status_code == 401
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote, headers={"X-Admin-Pin": "4321"}).status_code == 200
    assert client.post("/api/settings", json={"kiosk": True}).status_code == 200
    # LAN access off -> remote is refused outright
    srv.cfg.values["lan_access"] = "false"
    assert client.post("/api/settings", json={"kiosk": True}, environ_base=remote).status_code == 403
    assert client.post("/api/browser/report", json={"url": "x"}, environ_base=remote).status_code == 403


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
    assert s["hotspot"]["password"] == "testpass123"
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
