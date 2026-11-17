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


def test_diag_flags_a_refused_phone_but_not_the_tx_power_noise(monkeypatch):
    def fake_run(args, timeout=0, check=True):
        if args[0] == "journalctl":
            return ("Nov 17 11:44:30 pi kernel: ieee80211 phy0: brcmf_cfg80211_get_tx_power: error (-512)\n"
                    "Nov 17 11:44:31 pi usb-router-hotspot[9]: wlan0: STA 66:55:44:33:22:11 IEEE 802.11: authenticated\n")
        return ""
    monkeypatch.setattr(netman, "_run", fake_run)
    d = netman.hotspot_diag("wlan0", None)
    assert d["refused"] is False and "authenticated" in d["last_event"]

    def fake_run2(args, timeout=0, check=True):
        if args[0] == "journalctl":
            return "Nov 17 11:44:32 pi usb-router-hotspot[9]: wlan0: AP-STA-POSSIBLE-PSK-MISMATCH 66:55:44:33:22:11\n"
        return ""
    monkeypatch.setattr(netman, "_run", fake_run2)
    assert netman.hotspot_diag("wlan0", None)["refused"] is True


# ---- v8.1 (C&G #209): scan while hostapd holds the radio; hand it back to join the venue ----
IW_SCAN = """BSS aa:bb:cc:dd:ee:01(on wlan0)
\tfreq: 2412
\tsignal: -48.00 dBm
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tSSID: Cafe Sol
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
BSS aa:bb:cc:dd:ee:02(on wlan0)
\tfreq: 2437
\tsignal: -80.00 dBm
\tcapability: ESS Privacy (0x0411)
\tSSID: Cafe Sol
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
BSS aa:bb:cc:dd:ee:03(on wlan0)
\tfreq: 5180
\tsignal: -60.00 dBm
\tcapability: ESS (0x0401)
\tSSID: Free\\x20Wifi\\xc3\\xa9
BSS aa:bb:cc:dd:ee:04(on wlan0)
\tfreq: 2462
\tsignal: -70.00 dBm
\tcapability: ESS Privacy (0x0411)
\tSSID: 
\tRSN:\t * Version: 1
BSS aa:bb:cc:dd:ee:05(on wlan0)
\tfreq: 2462
\tsignal: -66.00 dBm
\tcapability: ESS Privacy (0x0411)
\tSSID: Hotel
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK SAE
"""


def test_iw_scan_parse_strongest_per_ssid_hidden_dropped():
    nets = hotspot_ap.parse_iw_scan(IW_SCAN)
    by = {n["ssid"]: n for n in nets}
    assert set(by) == {"Cafe Sol", "Free Wifié", "Hotel"}        # hidden SSID dropped, escapes undone
    assert by["Cafe Sol"]["bssid"] == "aa:bb:cc:dd:ee:01" and by["Cafe Sol"]["signal"] == 100
    assert by["Cafe Sol"]["signal"] > by["Hotel"]["signal"] > 0
    assert by["Cafe Sol"]["security"] == "WPA2" and by["Cafe Sol"]["secured"]
    assert by["Free Wifié"]["security"] == "open" and not by["Free Wifié"]["secured"]
    assert by["Free Wifié"]["band"] == "5"
    assert by["Hotel"]["security"] == "WPA2 WPA3"
    assert [n["ssid"] for n in nets][0] == "Cafe Sol"


def test_helper_scan_retries_then_reports_the_error(monkeypatch):
    calls = []
    monkeypatch.setattr(hotspot_ap, "_sh", lambda *a, **k: calls.append(a) or
                        __import__("subprocess").CompletedProcess(a, 240, "", "command failed: Device or resource busy (-16)"))
    monkeypatch.setattr(hotspot_ap.time, "sleep", lambda s: None)
    nets, err = hotspot_ap.scan("wlan0", ap=True, tries=2)
    assert nets == [] and "busy" in err and len(calls) == 2 and calls[0][-1] == "ap-force"


def _fake_nm(tmp_path, monkeypatch, unit_active, nm_nets, connect_ok=True):
    """HostapdNetworkManager against fake systemctl/nmcli. Returns (nm, calls, state)."""
    state = {"active": unit_active, "managed": not unit_active, "saved": [], "connected": None}
    calls = []

    def fake_run(args, timeout=0, check=True):
        calls.append(list(args))
        if args[:2] == ["systemctl", "is-active"]:
            return "active" if state["active"] else "inactive"
        if args[:2] == ["systemctl", "stop"]:
            state["active"] = False
        if args[:2] == ["systemctl", "start"]:
            state["active"] = True
        return ""

    def fake_nmcli(*a, **k):
        calls.append(["nmcli", *a])
        if a[:2] == ("device", "set"):
            state["managed"] = a[-1] == "yes"
            return ""
        if a[-2:] == ("device", "status"):
            st = "disconnected" if state["managed"] else "unmanaged"
            if state["connected"]:
                st = "connected"
            return f"wlan0:wifi:{st}:{state['connected'] or ''}\n"
        if a[-3:] == ("device", "wifi", "list"):
            if not state["managed"]:
                return ""
            return "".join(f" :{n}:70:WPA2:2437 MHz:aa\\:bb\\:cc\\:dd\\:ee\\:0{i}\n" for i, n in enumerate(nm_nets))
        if a[-2:] == ("connection", "show"):
            return "".join(f"{n}:802-11-wireless:\n" for n in state["saved"])
        if a[:3] == ("device", "wifi", "connect"):
            state["saved"].append(a[3])
            if not connect_ok:
                raise netman.NetError("Error: Connection activation failed: (7) Secrets were required")
            state["connected"] = a[3]
            return ""
        if a[:2] == ("connection", "delete"):
            state["saved"] = [n for n in state["saved"] if n != a[2]]
        return ""

    monkeypatch.setattr(netman, "_run", fake_run)
    monkeypatch.setattr(netman, "nmcli", fake_nmcli)
    monkeypatch.setattr(netman.time, "sleep", lambda s: None)
    nm = netman.HostapdNetworkManager(state_dir=str(tmp_path), scan_file=str(tmp_path / "scan.json"))
    nm.sleep = lambda s: None
    monkeypatch.setattr(nm, "_ip_of", lambda iface: "10.42.0.1" if state["active"] else None)
    (tmp_path / "hotspot-ap.json").write_text(json.dumps({**GOOD, "country": "ES", "share": False}))
    return nm, calls, state


def test_scan_while_the_hotspot_runs_uses_the_helpers_list(tmp_path, monkeypatch):
    nm, calls, state = _fake_nm(tmp_path, monkeypatch, True, ["Cafe Sol"])
    hotspot_ap.write_scan(hotspot_ap.parse_iw_scan(IW_SCAN), "station", path=str(tmp_path / "scan.json"))
    nets = nm.wifi_scan(rescan=False)
    assert [n["ssid"] for n in nets] == ["Cafe Sol", "Free Wifié", "Hotel"]
    assert not any(c[:3] == ["nmcli", "device", "wifi"] for c in calls)     # NM is not asked
    nets = nm.wifi_scan(rescan=True)                                       # asks the helper, keeps the list
    assert (tmp_path / "scan-request").exists() and len(nets) == 3
    devs = nm.devices()
    assert devs[0]["state"] == "hotspot"                                   # not "unmanaged" on the screen


def test_join_from_the_hotspot_hands_the_radio_back_first(tmp_path, monkeypatch):
    nm, calls, state = _fake_nm(tmp_path, monkeypatch, True, ["Cafe Sol"])
    nm.wifi_connect("Cafe Sol", "venuepass1")
    i_stop = calls.index(["systemctl", "stop", "usb-router-hotspot.service"])
    i_managed = calls.index(["nmcli", "device", "set", "wlan0", "managed", "yes"])
    i_join = next(i for i, c in enumerate(calls) if c[:4] == ["nmcli", "device", "wifi", "connect"])
    assert i_stop < i_managed < i_join
    assert state["connected"] == "Cafe Sol" and not state["active"] and not nm.busy()


def test_failed_join_brings_the_setup_hotspot_back_and_forgets_the_try(tmp_path, monkeypatch):
    nm, calls, state = _fake_nm(tmp_path, monkeypatch, True, ["Cafe Sol"], connect_ok=False)
    with pytest.raises(netman.NetError):
        nm.wifi_connect("Cafe Sol", "wrongpass1")
    assert state["active"] and not state["managed"]                       # hostapd back on the radio
    assert "Cafe Sol" not in state["saved"]                               # not "configured" by a failed try
    assert json.loads((tmp_path / "hotspot-ap.json").read_text())["password"] == GOOD["password"]
    assert not nm.busy()


def test_radio_left_unmanaged_without_hostapd_is_handed_back(tmp_path, monkeypatch):
    nm, calls, state = _fake_nm(tmp_path, monkeypatch, False, [])
    state["managed"] = False
    devs = nm.devices()
    assert ["nmcli", "device", "set", "wlan0", "managed", "yes"] in calls and devs[0]["state"] == "disconnected"


def test_keeper_leaves_the_radio_alone_during_a_join():
    import router as router_mod

    class NM:
        def busy(self):
            return True

        def hotspot_status(self):
            raise AssertionError("keeper must not look at the hotspot while a join holds the radio")

    class Cfg:
        def flag(self, name):
            return True

    assert router_mod.Router(NM(), Cfg()).tick(False) == "busy"


def test_helper_run_scans_before_hostapd_and_rescans_on_request(tmp_path, monkeypatch):
    import subprocess as sp
    order, state = [], {"loops": 0}
    monkeypatch.setattr(hotspot_ap, "REQUEST", str(tmp_path / "hotspot-ap.json"))
    monkeypatch.setattr(hotspot_ap, "CAPTIVE", str(tmp_path / "captive.conf"))
    monkeypatch.setattr(hotspot_ap, "RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(hotspot_ap, "LEASES", str(tmp_path / "run" / "leases"))
    monkeypatch.setattr(hotspot_ap, "SCAN_FILE", str(tmp_path / "run" / "scan.json"))
    monkeypatch.setattr(hotspot_ap, "SCAN_REQUEST", str(tmp_path / "scan-request"))
    monkeypatch.setattr(hotspot_ap, "RESCAN_MIN_GAP", 0)
    (tmp_path / "hotspot-ap.json").write_text(json.dumps(GOOD))

    def fake_sh(*a, check=False):
        order.append(" ".join(str(x) for x in a))
        if "scan" in a:
            return sp.CompletedProcess(a, 0, IW_SCAN if "ap-force" not in a else IW_SCAN.split("BSS aa:bb:cc:dd:ee:03")[0], "")
        if a[:3] == ("ip", "-o", "link"):
            return sp.CompletedProcess(a, 0, "3: wlan0: <BROADCAST,UP> mtu", "")
        return sp.CompletedProcess(a, 0, "", "")

    class P:
        def __init__(self, args):
            order.append("popen " + os.path.basename(args[0]))
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, t=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_sleep(s):
        state["loops"] += 1
        if state["loops"] == 3:
            (tmp_path / "scan-request").write_text("x")
        if state["loops"] > 8:
            hotspot_ap_procs[0].returncode = 1
    hotspot_ap_procs = []
    monkeypatch.setattr(hotspot_ap, "_sh", fake_sh)
    monkeypatch.setattr(hotspot_ap.subprocess, "Popen", lambda args: hotspot_ap_procs.append(P(args)) or hotspot_ap_procs[-1])
    monkeypatch.setattr(hotspot_ap.time, "sleep", fake_sleep)
    monkeypatch.setattr(hotspot_ap.signal, "signal", lambda *a: None)
    assert hotspot_ap.run() == 1                     # hostapd "died" at the end of the fake
    first_scan = next(i for i, c in enumerate(order) if c.endswith("scan"))
    assert first_scan < order.index("popen hostapd")
    assert any(c.endswith("scan ap-force") for c in order[order.index("popen hostapd"):])
    data = json.loads((tmp_path / "run" / "scan.json").read_text())
    assert data["mode"] == "ap" and [n["ssid"] for n in data["networks"]] == ["Cafe Sol"]
    assert stat.S_IMODE((tmp_path / "run" / "scan.json").stat().st_mode) == 0o644
