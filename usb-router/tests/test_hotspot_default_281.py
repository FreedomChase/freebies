"""C&G #281 (Shaun, voice 21 Nov 13:22 CET): sharing hotspot ON by default, at most 10 phones.

The cap lives in one place (hotspot_max_clients in usb-router.conf, 1..10, never above 10) and is
enforced twice on the stick: hostapd max_num_sta (the 11th phone is refused at association) and
dnsmasq dhcp-lease-max (no address for an 11th, on the hostapd path and on NetworkManager's).
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import captive  # noqa: E402
import hotspot_ap  # noqa: E402
import netman  # noqa: E402
from router import Router  # noqa: E402

GOOD = {"iface": "wlan1", "ssid": "Display Player", "password": "Str0ng-pass-281", "band": "bg", "channel": 6}


def _config(tmp_path, monkeypatch, conf_text=None):
    if conf_text is not None:
        conf = tmp_path / "usb-router.conf"
        conf.write_text(conf_text, encoding="utf-8")
        monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    else:
        monkeypatch.delenv("USB_ROUTER_CONF", raising=False)
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "Str0ng-pass-281")
    sys.modules.pop("config", None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    return config.Config()


# -- the default and the one place the cap is set ------------------------------------------------

def test_sharing_is_on_by_default_with_a_cap_of_10(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    assert cfg.flag("hotspot_enabled") is True
    assert cfg.max_clients() == 10
    pub = cfg.public()
    assert pub["hotspot_enabled"] is True and pub["hotspot_max_clients"] == 10


@pytest.mark.parametrize("value, cap", [("5", 5), ("1", 1), ("10", 10), ("11", 10), ("50", 10),
                                        ("0", 1), ("abc", 10), ("", 10), ("-3", 10)])
def test_the_conf_can_lower_the_cap_but_never_raise_it(tmp_path, monkeypatch, value, cap):
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nhotspot_max_clients = {value}\n")
    assert cfg.max_clients() == cap


def test_the_conf_and_the_admin_can_still_switch_sharing_off(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "[usb-router]\nhotspot_enabled = false\n")
    assert cfg.flag("hotspot_enabled") is False
    cfg = _config(tmp_path, monkeypatch)
    cfg.save({"hotspot_enabled": "false"})
    assert cfg.flag("hotspot_enabled") is False


def test_the_cap_is_not_a_setting_a_phone_can_change(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.save({"hotspot_max_clients": "200"})          # not in EDITABLE: ignored
    assert cfg.max_clients() == 10


# -- enforced in the root helper's hostapd and dnsmasq --------------------------------------------

def test_hostapd_refuses_the_11th_phone_and_dnsmasq_has_10_addresses():
    v = hotspot_ap.validate({**GOOD, "share": True, "max_clients": 10})
    assert "max_num_sta=10" in hotspot_ap.hostapd_conf(v).splitlines()
    assert "dhcp-lease-max=10" in hotspot_ap.dnsmasq_conf(v).splitlines()


@pytest.mark.parametrize("asked, got", [(None, 10), ("x", 10), (3, 3), ("4", 4), (11, 10), (999, 10), (0, 1)])
def test_the_root_helper_clamps_whatever_the_app_wrote(asked, got):
    req = dict(GOOD) if asked is None else {**GOOD, "max_clients": asked}
    v = hotspot_ap.validate(req)
    assert v["max_clients"] == got
    assert f"max_num_sta={got}" in hotspot_ap.hostapd_conf(v).splitlines()
    assert f"dhcp-lease-max={got}" in hotspot_ap.dnsmasq_conf(v).splitlines()


def test_a_lease_line_in_the_app_written_file_never_reaches_root_dnsmasq():
    # the app-writable captive file may only add interface-name= lines (S&D #193); a cap there is ignored
    v = hotspot_ap.validate({**GOOD, "max_clients": 3})
    text = captive.dns_conf_text("wlan1", 10)
    lines = hotspot_ap.dnsmasq_conf(v, text).splitlines()
    assert "dhcp-lease-max=3" in lines and "dhcp-lease-max=10" not in lines


def test_the_app_backend_sends_the_cap_to_the_root_helper(tmp_path, monkeypatch):
    nm = netman.HostapdNetworkManager.__new__(netman.HostapdNetworkManager)
    nm.request_path = str(tmp_path / "hotspot-ap.json")
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: "")
    monkeypatch.setattr(netman, "_run", lambda *a, **k: "active")
    monkeypatch.setattr(nm, "_ip_of", lambda iface: "10.42.0.1", raising=False)
    nm.hotspot_start("wlan1", "Display Player", "Str0ng-pass-281", "bg", share=True, max_clients=10)
    import json
    req = json.loads((tmp_path / "hotspot-ap.json").read_text())
    assert req["max_clients"] == 10 and req["share"] is True
    assert hotspot_ap.validate(req)["max_clients"] == 10


# -- enforced on NetworkManager's shared dnsmasq (the non-hostapd sticks) ----------------------

def test_networkmanager_shared_dnsmasq_gets_the_lease_cap_captive_on_or_off():
    assert "dhcp-lease-max=10" in captive.dns_conf_text(None, 10).splitlines()
    on = captive.dns_conf_text("wlan1", 7).splitlines()
    assert "dhcp-lease-max=7" in on and any(x.startswith("interface-name=") for x in on)
    assert not any(x.startswith("dhcp-lease-max") for x in captive.dns_conf_text(None).splitlines())
    for bad in (0, 11, -1):
        with pytest.raises(ValueError):
            captive.dns_conf_text("wlan1", bad)


# -- the router: online + default config -> share with the cap ---------------------------------

class Spy:
    def __init__(self):
        self.calls, self.active = [], False

    def prepare(self, iface, max_clients=None):
        self.calls.append((iface, max_clients))
        self.active = iface is not None


def test_online_with_default_config_starts_sharing_with_the_cap(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    nm = netman.MockNetworkManager()
    nm.connected_ssid = "Venue Mango"                 # online over wlan0; wlan1 is the spare card
    given = []
    real = nm.hotspot_start
    nm.hotspot_start = lambda iface, ssid, pw, band="bg", **kw: (given.append((iface, kw)), real(iface, ssid, pw, band, **kw))
    spy = Spy()
    r = Router(nm, cfg, captive=spy)
    assert r.desired(online=True) == "share"
    assert r.tick(online=True) == "started-share"
    assert given == [("wlan1", {"max_clients": 10, "share": True})]
    assert spy.calls == [(None, 10)]                  # sharing: no captive DNS, but the lease cap
    s = r.status(online=True)
    assert s["active"] and s["role"] == "share" and s["enabled"] is True and s["max_clients"] == 10


def test_one_radio_and_online_by_wifi_says_why_it_cannot_share(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)

    class OneRadio(netman.MockNetworkManager):
        def devices(self):
            return [d for d in super().devices() if d["iface"] == "wlan0"]

    nm = OneRadio()
    nm.connected_ssid = "Venue Mango"
    r = Router(nm, cfg, captive=Spy())
    assert r.tick(online=True) == "failed"
    s = r.status(online=True)
    assert s["active"] is False and s["enabled"] is True
    assert "No spare adapter" in s["last_error"] and "second Wi-Fi adapter" in s["last_error"]


def test_the_setup_page_shows_sharing_on_and_n_of_10():
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js"), encoding="utf-8").read()
    assert "Sharing on as Wi-Fi network" in js
    assert "${(h.clients || []).length}/${h.max_clients}" in js
