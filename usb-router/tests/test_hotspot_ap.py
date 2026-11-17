"""S&D #193 v7: the Pi's setup hotspot with hostapd + dnsmasq (root helper and app backend)."""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import captive  # noqa: E402
import hotspot_ap  # noqa: E402
import netman  # noqa: E402

GOOD = {"iface": "wlan0", "ssid": "USB-Router", "password": "abc;d:e,f\\g\"h", "band": "bg", "channel": 6}


def test_valid_request_gives_plain_wpa2_without_pmf():
    v = hotspot_ap.validate(GOOD)
    conf = hotspot_ap.hostapd_conf(v)
    lines = conf.splitlines()
    assert "wpa=2" in lines and "wpa_key_mgmt=WPA-PSK" in lines and "rsn_pairwise=CCMP" in lines
    assert "ieee80211w=0" in lines and "channel=6" in lines and "hw_mode=g" in lines
    assert f"ssid2={'USB-Router'.encode().hex()}" in lines
    assert f"wpa_passphrase={GOOD['password']}" in lines          # the same password the QR carries
    assert "country_code=ES" in lines


@pytest.mark.parametrize("bad", [
    {"iface": "wlan0; rm -rf /"}, {"iface": ""}, {"ssid": ""}, {"ssid": "x" * 33},
    {"password": "short"}, {"password": "a" * 64}, {"password": "line\nctrl_interface=/tmp"},
    {"password": "nonasciié123"}, {"channel": 14}, {"band": "a", "channel": 6}, {"channel": "x"},
])
def test_bad_requests_are_refused(bad):
    with pytest.raises(hotspot_ap.Invalid):
        hotspot_ap.validate({**GOOD, **bad})


def test_only_captive_lines_for_the_same_interface_reach_root_dnsmasq(tmp_path):
    path = captive.write_dns_conf("wlan0", str(tmp_path / "c.conf"))
    text = open(path).read() + "dhcp-script=/tmp/evil\nconf-file=/tmp/x\ninterface-name=a.b,eth0/4\naddress=/#/1.2.3.4\n"
    conf = hotspot_ap.dnsmasq_conf(hotspot_ap.validate(GOOD), text)
    assert "dhcp-script" not in conf and "/tmp/" not in conf and "address=/#/" not in conf
    assert "eth0/4" not in conf
    assert "interface-name=captive.apple.com,wlan0/4" in conf
    assert "dhcp-option=option:router,10.42.0.1" in conf and "no-resolv" in conf


def test_share_mode_forwards_dns():
    conf = hotspot_ap.dnsmasq_conf(hotspot_ap.validate({**GOOD, "share": True}), "")
    assert "server=1.1.1.1" in conf


def test_backend_writes_a_private_request_and_starts_the_unit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(netman, "_run", lambda args, timeout=0, check=True: calls.append(args) or (
        "active" if args[:2] == ["systemctl", "is-active"] else ""))
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: calls.append(["nmcli", *a]) or "")
    nm = netman.HostapdNetworkManager(state_dir=str(tmp_path))
    monkeypatch.setattr(nm, "_ip_of", lambda iface: "10.42.0.1")
    nm.hotspot_start("wlan0", "USB-Router", GOOD["password"], "bg")
    req = tmp_path / "hotspot-ap.json"
    assert stat.S_IMODE(req.stat().st_mode) == 0o600
    data = json.loads(req.read_text())
    assert hotspot_ap.validate(data)["password"] == GOOD["password"]
    assert ["nmcli", "device", "set", "wlan0", "managed", "no"] in calls
    assert ["systemctl", "start", "usb-router-hotspot.service"] in calls
    st = nm.hotspot_status()
    assert st["active"] and st["iface"] == "wlan0" and st["mode"] == "wifi"


def test_auto_backend_is_networkmanager_without_hostapd(monkeypatch):
    monkeypatch.setattr(netman, "hostapd_ready", lambda: False)
    assert type(netman.get_backend(False, "auto")) is netman.NetworkManager
    monkeypatch.setattr(netman, "hostapd_ready", lambda: True)
    assert type(netman.get_backend(False, "auto")) is netman.HostapdNetworkManager
    assert type(netman.get_backend(False, "networkmanager")) is netman.NetworkManager
