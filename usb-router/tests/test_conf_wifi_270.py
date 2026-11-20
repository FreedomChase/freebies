"""C&G #270 (Shaun's v8.8 flash, 20 Nov ~02:58 CET): the screen came up on the setup page and did not join
the Wi-Fi written in usb-router.conf (OWNIT-FCD6), and nothing on it said why. The setup card now says, for
each Wi-Fi name in the file: joined, not seen (2.4 GHz only), WPA3 only, NetworkManager's reason, or that
the file holds no Wi-Fi name at all (empty, or not read)."""
import importlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

import netman  # noqa: E402

WRONG_PW = {"code": "no-secrets", "words": netman.NM_REASONS["no-secrets"]}


def net(ssid, security="WPA2", signal=60):
    return {"ssid": ssid, "security": security, "signal": signal, "band": "2.4"}


def report(ssids, nets, connected=(), reason=None):
    return netman.conf_wifi_report(list(ssids), nets, set(connected), reason)


# -- the pure report -----------------------------------------------------------------------------
def test_no_wifi_name_in_the_file_says_so():
    [r] = report([], [net("OWNIT-FCD6")])
    assert r["state"] == "none" and r["ssid"] is None
    assert "no Wi-Fi name" in r["words"] and "not read" in r["words"]


def test_joined_network_is_joined_even_with_an_old_failure_reason():
    [r] = report(["OWNIT-FCD6"], [net("OWNIT-FCD6")], connected=["OWNIT-FCD6"], reason=WRONG_PW)
    assert r["state"] == "joined"


def test_name_not_in_the_scan_points_at_5ghz_or_distance():
    [r] = report(["OWNIT-FCD6"], [net("Neighbour"), net("Cafe")])
    assert r["state"] == "not-seen"
    assert "2.4 GHz" in r["words"] and "5 GHz" in r["words"]


def test_a_name_that_differs_only_in_capitals_is_not_seen():
    # the profile's ssid must match exactly; NetworkManager would not join 'ownit-fcd6' either
    [r] = report(["ownit-fcd6"], [net("OWNIT-FCD6")])
    assert r["state"] == "not-seen"


@pytest.mark.parametrize("sec,state", [("WPA3", "wpa3-only"), ("WPA2 WPA3", "seen"), ("WPA2", "seen")])
def test_wpa3_only_is_named_and_mixed_is_not(sec, state):
    [r] = report(["OWNIT-FCD6"], [net("OWNIT-FCD6", security=sec)])
    assert r["state"] == state
    assert r["security"] == sec


def test_network_manager_reason_is_shown_in_plain_words():
    [r] = report(["OWNIT-FCD6"], [net("OWNIT-FCD6")], reason=WRONG_PW)
    assert r["state"] == "failed"
    assert r["words"] == "did not join: wrong password (the network refused it)"


def test_not_seen_beats_a_reason():
    # ssid-not-found and a name missing from the scan say the same; the scan is the plainer fact
    [r] = report(["OWNIT-FCD6"], [], reason=WRONG_PW)
    assert r["state"] == "not-seen"


def test_no_scan_at_all_does_not_claim_not_seen():
    [r] = report(["OWNIT-FCD6"], None)
    assert r["state"] == "unknown" and r["words"] == "not joined yet"


def test_three_names_keep_the_file_order():
    rs = report(["A", "B", "C"], [net("B")], connected=["C"])
    assert [(r["ssid"], r["state"]) for r in rs] == [("A", "not-seen"), ("B", "seen"), ("C", "joined")]


def test_the_password_never_appears_in_the_report():
    rs = report(["OWNIT-FCD6"], [net("OWNIT-FCD6")], reason=WRONG_PW)
    assert "pw-Secret-123" not in repr(rs)
    assert set(rs[0]) == {"ssid", "state", "words", "signal", "security", "band"}


# -- through the running app (mock NetworkManager) ----------------------------------------------
@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    monkeypatch.setenv("USB_ROUTER_WIFI_SSID", "OWNIT-FCD6")
    monkeypatch.setenv("USB_ROUTER_WIFI_PASSWORD", "pw-Secret-123")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    srv = importlib.import_module("server")
    srv.app.config["TESTING"] = True
    srv._conf_wifi_cache.update(at=0.0, value=None)
    return srv


def test_status_carries_the_conf_wifi_state_and_no_password(server):
    with server.app.test_client() as c:
        body = c.get("/api/status").get_json()
    assert [w["ssid"] for w in body["conf_wifi"]] == ["OWNIT-FCD6"]
    assert body["conf_wifi"][0]["state"] == "not-seen"          # the mock scan has no OWNIT-FCD6
    assert "pw-Secret-123" not in repr(body["conf_wifi"])


def test_the_report_is_cached_so_the_pi_does_not_scan_every_poll(server, monkeypatch):
    calls = []
    real = server.nm.wifi_scan
    monkeypatch.setattr(server.nm, "wifi_scan", lambda rescan=True: calls.append(rescan) or real(rescan=False))
    devices = server.nm.devices()
    server.conf_wifi(devices)
    server.conf_wifi(devices)
    assert calls == [False]                                     # once, and never a rescan
    server._conf_wifi_cache["at"] = 0.0
    server.conf_wifi(devices)
    assert calls == [False, False]


def test_a_failed_phone_join_to_another_name_is_not_blamed_on_the_file(server, monkeypatch):
    monkeypatch.setattr(server, "MOCK", False)
    monkeypatch.setattr(server.nm, "devices", lambda: [{"iface": "wlan0", "type": "wifi", "state": "disconnected",
                                                         "connection": None}])
    monkeypatch.setattr(server.nm, "wifi_scan", lambda rescan=True: [net("OWNIT-FCD6")])
    monkeypatch.setattr(server, "nm_reason", lambda iface, since: WRONG_PW)
    monkeypatch.setattr(server.nm, "join_state", lambda: {"ssid": "Cafe Guest", "ok": False})
    [r] = server.conf_wifi(server.nm.devices(), max_age=0)
    assert r["state"] == "seen"
    monkeypatch.setattr(server.nm, "join_state", lambda: None)
    [r] = server.conf_wifi(server.nm.devices(), max_age=0)
    assert r["state"] == "failed" and "wrong password" in r["words"]


# -- the setup card shows it ----------------------------------------------------------------------
def test_setup_card_has_the_line_and_escapes_the_name():
    html = open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
    assert 'id="setup-confwifi"' in html
    assert "renderConfWifi(s);" in js
    body = js[js.index("const renderConfWifi"):js.index("/* ---------------- venue join on the screen")]
    assert "esc(w.ssid)" in body and "esc(w.words)" in body
    assert "${w.ssid}" not in body and "${w.words}" not in body
