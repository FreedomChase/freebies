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


# -- the shipped conf: make-usb.sh copies the example onto every stick, so it must not undo the default --

def test_the_shipped_example_conf_keeps_sharing_on_with_a_cap_of_10(tmp_path, monkeypatch):
    example = os.path.join(os.path.dirname(__file__), "..", "config", "usb-router.conf.example")
    cfg = _config(tmp_path, monkeypatch, open(example, encoding="utf-8").read())
    assert cfg.flag("hotspot_enabled") is True
    assert cfg.max_clients() == 10


# -- r2 (security + dev4, 21 Nov): LAN guard, isolation, PIN, legal line ------------------------------------

import re  # noqa: E402
import subprocess  # noqa: E402

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def test_hostapd_isolates_phones_from_each_other():
    v = hotspot_ap.validate({**GOOD, "share": True, "max_clients": 10})
    assert "ap_isolate=1" in hotspot_ap.hostapd_conf(v).splitlines()


def test_guard_rules_drop_every_private_range_from_the_hotspot_only():
    text = hotspot_ap.guard_rules("wlan1")
    assert "hook forward" in text and 'iifname "wlan1"' in text and "drop" in text
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"):
        assert net in text


def test_the_shell_guard_and_the_python_guard_drop_the_same_ranges():
    sh = open(os.path.join(ROOT, "scripts", "usb-router-lan-guard"), encoding="utf-8").read()
    listed = re.search(r'^PRIVATE="([^"]+)"', sh, re.M).group(1)
    assert tuple(x.strip() for x in listed.split(",")) == hotspot_ap.GUARD_PRIVATE


def test_networkmanager_hotspot_turns_on_ap_isolation(monkeypatch):
    calls = []
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: calls.append(a) or "")
    netman.NetworkManager().hotspot_start("wlan1", "Display Player", "Str0ng-pass-281", share=True, max_clients=10)
    add = next(c for c in calls if c[:2] == ("connection", "add"))
    i = add.index("802-11-wireless.ap-isolation")
    assert add[i + 1] == "yes"


def _fake_bin(tmp_path, name):
    log = tmp_path / f"{name}.log"
    f = tmp_path / name
    f.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n[ -z "$FAIL" ]\n', encoding="utf-8")
    f.chmod(0o755)
    return f, log


@pytest.mark.parametrize("con,called", [("usb-router-hotspot", True), ("usb-router-share-eth1", True), ("Venue WiFi", False)])
def test_dispatcher_loads_the_guard_only_for_our_sharing_connections(tmp_path, con, called):
    guard, log = _fake_bin(tmp_path, "guard")
    env = dict(os.environ, CONNECTION_ID=con, USB_ROUTER_LAN_GUARD=str(guard))
    disp = os.path.join(ROOT, "system", "usb-router-nm-dispatcher")
    assert subprocess.run(["sh", disp, "wlan1", "up"], env=env).returncode == 0
    assert subprocess.run(["sh", disp, "wlan1", "down"], env=env).returncode == 0
    got = log.read_text().splitlines() if log.exists() else []
    assert got == (["up wlan1", "down wlan1"] if called else [])


def test_dispatcher_fails_closed_when_the_guard_cannot_load(tmp_path):
    guard, _ = _fake_bin(tmp_path, "guard")
    nm, nmlog = _fake_bin(tmp_path, "nmcli")
    env = dict(os.environ, CONNECTION_ID="usb-router-hotspot", USB_ROUTER_LAN_GUARD=str(guard), NMCLI=str(nm), FAIL="1")
    subprocess.run(["sh", os.path.join(ROOT, "system", "usb-router-nm-dispatcher"), "wlan1", "up"], env=env)
    assert nmlog.read_text().split() == ["connection", "down", "id", "usb-router-hotspot"]


def test_lan_guard_script_refuses_a_bad_interface_name(tmp_path):
    nft, log = _fake_bin(tmp_path, "nft")
    r = subprocess.run(["sh", os.path.join(ROOT, "scripts", "usb-router-lan-guard"), "up", "wlan1; rm -rf /"],
                       env=dict(os.environ, NFT=str(nft)), capture_output=True, text=True)
    assert r.returncode == 2 and not log.exists()


@pytest.mark.parametrize("sharing,source", [("true", "generated"), ("false", "default")])
def test_the_shared_default_pin_is_off_while_sharing_is_on(tmp_path, monkeypatch, sharing, source):
    pin_file = tmp_path / "default-pin"
    pin_file.write_text("246813\n", encoding="utf-8")
    monkeypatch.setenv("USB_ROUTER_DEFAULT_PIN_FILE", str(pin_file))
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nhotspot_enabled = {sharing}\n")
    assert cfg.admin_pin_source() == source
    assert (cfg.admin_pin() == "246813") is (source == "default")


def test_setup_page_says_who_is_responsible_and_what_is_logged():
    html = open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8").read()
    line = re.search(r'<p class="hint" id="hs-legal">([^<]+)</p>', html).group(1)
    assert "responsible" in line and "14 days" in line and "never the sites" in line


def test_every_installer_puts_the_guard_and_the_journal_cap_in_place():
    pc = open(os.path.join(ROOT, "scripts", "install-local.sh"), encoding="utf-8").read()
    live = open(os.path.join(ROOT, "build", "live", "config", "hooks", "live", "0500-usb-router.hook.chroot"), encoding="utf-8").read()
    pi = open(os.path.join(ROOT, "pizero", "install-pizero.sh"), encoding="utf-8").read()
    for text in (pc, live):
        assert "dispatcher.d/50-usb-router-lan-guard" in text and "usb-router-journald.conf" in text
    assert "nftables" in pc.split("PKGS=", 1)[1].splitlines()[0]
    assert "usb-router-journald.conf" in pi
