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
    assert h["active"] and h["iface"] == "wlan1" and h["mode"] == "wifi"
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


def test_lan_access_blocked_by_default(client):
    r = client.post("/api/settings", json={"kiosk": True}, environ_base={"REMOTE_ADDR": "10.42.0.17"})
    assert r.status_code == 403


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
