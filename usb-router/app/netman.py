"""
Thin wrapper around nmcli (NetworkManager) for the connection manager.

Everything that touches the network goes through this module so the Flask app
stays testable: set USB_ROUTER_MOCK=1 and a fake NetworkManager is used instead.

nmcli terse output (-t) separates fields with ':' and escapes literal colons
as '\\:' so we split on unescaped colons only.
"""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

NMCLI_TIMEOUT = 40


log = logging.getLogger("usb-router.netman")


class NetError(Exception):
    pass


def _split_terse(line: str):
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def _run(args, timeout=NMCLI_TIMEOUT, check=True):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise NetError(f"{args[0]} is not installed")
    except subprocess.TimeoutExpired:
        raise NetError(f"{' '.join(args[:3])} timed out")
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise NetError(msg)
    return proc.stdout


def nmcli(*args, timeout=NMCLI_TIMEOUT, check=True):
    return _run(["nmcli", "-t", "-e", "yes", *args], timeout=timeout, check=check)


# ---------------------------------------------------------------------------
# Real NetworkManager backend
# ---------------------------------------------------------------------------
class NetworkManager:
    name = "nmcli"

    def available(self) -> bool:
        return shutil.which("nmcli") is not None

    # -- devices -----------------------------------------------------------
    def devices(self):
        out = nmcli("-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
        result = []
        for line in out.splitlines():
            if not line.strip():
                continue
            dev, typ, state, con = (_split_terse(line) + ["", "", "", ""])[:4]
            if typ in ("loopback", "bridge", "tun", "p2p-wifi", "wifi-p2p"):
                continue
            result.append({
                "iface": dev,
                "type": typ,
                "state": state,
                "connection": con or None,
                "ip": self._ip_of(dev) if state.startswith("connected") else None,
            })
        return result

    def _ip_of(self, iface):
        try:
            out = nmcli("-f", "IP4.ADDRESS", "device", "show", iface, check=False)
        except NetError:
            return None
        for line in out.splitlines():
            if line.startswith("IP4.ADDRESS"):
                return _split_terse(line)[-1].split("/")[0] or None
        return None

    def wifi_ap_capable(self, iface) -> bool:
        """True if the card advertises AP mode (needed to run the hotspot)."""
        if not shutil.which("iw"):
            return True  # cannot tell; let nmcli try
        try:
            phy_link = os.readlink(f"/sys/class/net/{iface}/phy80211")
            phy = os.path.basename(phy_link)
        except OSError:
            return False
        try:
            out = _run(["iw", "phy", phy, "info"], timeout=10, check=False)
        except NetError:
            return True
        m = re.search(r"Supported interface modes:(.*?)\n\n", out + "\n\n", re.S)
        return bool(m and re.search(r"^\s*\*\s*AP\s*$", m.group(1), re.M))

    # -- wifi --------------------------------------------------------------
    def wifi_scan(self, rescan=True):
        if rescan:
            nmcli("device", "wifi", "rescan", check=False, timeout=15)
            time.sleep(1.5)
        out = nmcli("-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ,BSSID", "device", "wifi", "list")
        seen, nets = {}, []
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) < 6:
                continue
            in_use, ssid, signal, security, freq, bssid = parts[:6]
            if not ssid:
                continue
            try:
                signal = int(signal)
            except ValueError:
                signal = 0
            band = "5" if freq.strip().startswith("5") else "2.4"
            entry = {
                "ssid": ssid,
                "signal": signal,
                "security": security.strip() or "open",
                "secured": bool(security.strip()) and security.strip() != "--",
                "band": band,
                "in_use": in_use.strip() == "*",
                "bssid": bssid,
            }
            # keep the strongest BSSID for each SSID
            if ssid not in seen or signal > seen[ssid]["signal"] or entry["in_use"]:
                if ssid in seen:
                    nets.remove(seen[ssid])
                seen[ssid] = entry
                nets.append(entry)
        nets.sort(key=lambda n: (not n["in_use"], -n["signal"]))
        return nets

    def saved_connections(self):
        out = nmcli("-f", "NAME,TYPE,DEVICE", "connection", "show")
        cons = []
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 3:
                cons.append({"name": parts[0], "type": parts[1], "device": parts[2] or None})
        return cons

    def wifi_connect(self, ssid, password=None, iface=None):
        if not ssid:
            raise NetError("No network name given")
        # A single Wi-Fi card cannot run the setup hotspot and join a network at once:
        # drop the hotspot first (the phone doing the setup will be disconnected).
        hs = self.hotspot_status()
        if hs["active"] and hs["mode"] == "wifi":
            wifi = self.wifi_ifaces()
            if (iface and hs["iface"] == iface) or len(wifi) <= 1:
                self.hotspot_stop()
                time.sleep(2)
        saved = {c["name"] for c in self.saved_connections()}
        args = []
        if ssid in saved and not password:
            args = ["connection", "up", ssid]
        else:
            args = ["device", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            if iface:
                args += ["ifname", iface]
        try:
            nmcli(*args, timeout=60)
        except NetError as e:
            text = str(e)
            if "Secrets were required" in text or "802-11-wireless-security" in text or "No suitable" in text:
                # a stale profile with a wrong password: delete and retry fresh
                if ssid in saved and password:
                    nmcli("connection", "delete", ssid, check=False)
                    nmcli("device", "wifi", "connect", ssid, "password", password, timeout=60)
                    return
                raise NetError("Wrong password or the network rejected the connection")
            raise

    def wifi_forget(self, ssid):
        nmcli("connection", "delete", ssid)

    def join_state(self):
        return None          # only the hotspot backend joins blind, with the phone gone (C&G #209)

    def ensure_profile(self, ssid, password=None, priority=10):
        """Create a saved, auto-connecting profile (zero-touch provisioning).

        C&G #254: a profile that already exists gets the config's password and priority, so a
        password corrected in usb-router.conf takes effect at the next boot (it used to be ignored).
        Returns True when a profile was created, False when an existing one was updated.
        """
        if ssid in {c["name"] for c in self.saved_connections()}:
            args = ["connection", "modify", ssid,
                    "connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority)]
            if password:
                args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
            nmcli(*args)
            return False
        args = ["connection", "add", "type", "wifi", "con-name", ssid, "ssid", ssid,
                "connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority)]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        nmcli(*args)
        return True

    def wifi_ifaces(self):
        return [d["iface"] for d in self.devices() if d["type"] == "wifi"]

    def disconnect(self, iface):
        nmcli("device", "disconnect", iface)

    def ethernet_connect(self, iface):
        nmcli("device", "connect", iface, timeout=45)

    # -- hotspot / sharing ------------------------------------------------
    HOTSPOT_CON = "usb-router-hotspot"
    SHARE_CON = "usb-router-share-"

    def hotspot_start(self, iface, ssid, password, band="bg", share=False, max_clients=None):
        self.hotspot_stop()
        args = ["connection", "add", "type", "wifi", "ifname", iface,
                "con-name", self.HOTSPOT_CON, "autoconnect", "no", "ssid", ssid,
                "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
                # a fixed channel and no Wi-Fi power saving: brcmfmac APs (Pi Zero W) drop
                # joins on auto channel / powersave with some phones (S&D #193)
                "802-11-wireless.channel", "6" if band == "bg" else "36",
                "802-11-wireless.powersave", "2",
                # C&G #281: phones on the hotspot cannot reach each other over the air (the dispatcher's LAN guard
                # keeps them off the venue's network: system/usb-router-nm-dispatcher)
                "802-11-wireless.ap-isolation", "yes",
                "ipv4.method", "shared", "ipv6.method", "ignore"]
        if password:
            # pmf disable: left at default, NetworkManager (1.42) makes a wpa-psk AP WPA2/WPA3
            # transition mode with PMF optional, which iPhones fail to join on the Pi's
            # brcmfmac Wi-Fi ("Unable to join the network"). Disabled, it is plain WPA2-PSK (S&D #191).
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                     "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                     "wifi-sec.pmf", "disable"]
        nmcli(*args)
        nmcli("connection", "up", self.HOTSPOT_CON, timeout=45)

    def hotspot_diag(self, iface):
        return hotspot_diag(iface, self.HOTSPOT_CON)

    def hotspot_stop(self):
        nmcli("connection", "down", self.HOTSPOT_CON, check=False)
        nmcli("connection", "delete", self.HOTSPOT_CON, check=False)

    def share_ethernet_start(self, iface):
        """Share the uplink out of an Ethernet port (PC becomes a wired router)."""
        name = self.SHARE_CON + iface
        nmcli("connection", "delete", name, check=False)
        nmcli("connection", "add", "type", "ethernet", "ifname", iface, "con-name", name,
              "autoconnect", "no", "ipv4.method", "shared", "ipv6.method", "ignore")
        nmcli("connection", "up", name, timeout=45)

    def share_ethernet_stop(self, iface):
        name = self.SHARE_CON + iface
        nmcli("connection", "down", name, check=False)
        nmcli("connection", "delete", name, check=False)

    def hotspot_status(self):
        cons = self.saved_connections()
        for c in cons:
            if c["name"] == self.HOTSPOT_CON and c["device"]:
                return {"active": True, "iface": c["device"], "mode": "wifi",
                        "ip": self._ip_of(c["device"]), "clients": self.hotspot_clients(c["device"])}
        for c in cons:
            if c["name"].startswith(self.SHARE_CON) and c["device"]:
                return {"active": True, "iface": c["device"], "mode": "ethernet",
                        "ip": self._ip_of(c["device"]), "clients": self.hotspot_clients(c["device"])}
        return {"active": False, "iface": None, "mode": None, "ip": None, "clients": []}

    def hotspot_clients(self, iface):
        """Devices connected to us, from the neighbour table and dnsmasq leases."""
        clients = {}
        try:
            out = _run(["ip", "-4", "neigh", "show", "dev", iface], timeout=5, check=False)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and "lladdr" in parts and "FAILED" not in parts:
                    mac = parts[parts.index("lladdr") + 1]
                    clients[mac] = {"ip": parts[0], "mac": mac, "name": None}
        except NetError:
            pass
        for lease_file in ("/var/lib/NetworkManager/dnsmasq-%s.leases" % iface,
                           "/var/lib/misc/dnsmasq.leases"):
            try:
                with open(lease_file, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) >= 4:
                            mac = parts[1]
                            clients.setdefault(mac, {"ip": parts[2], "mac": mac, "name": None})
                            clients[mac]["name"] = parts[3] if parts[3] != "*" else None
            except OSError:
                continue
        return list(clients.values())

    # -- connectivity ------------------------------------------------------
    def connectivity(self):
        """NetworkManager's own verdict: none / portal / limited / full / unknown."""
        try:
            return nmcli("networking", "connectivity", "check", timeout=15).strip() or "unknown"
        except NetError:
            return "unknown"


# ---------------------------------------------------------------------------
# Mock backend for development without NetworkManager
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Hotspot diagnostics for the screen (S&D #193: an iPhone could not join on a real Pi)
# --------------------------------------------------------------------------
BRCMFMAC_CONF = "/etc/modprobe.d/usb-router-brcmfmac.conf"


REFUSAL = re.compile(r"PSK-MISMATCH|WRONG_KEY|pre-shared key may be incorrect|deauthenticated due to|"
                     r"disassociated due to|AP-STA-POSSIBLE-PSK-MISMATCH|EAPOL-Key timeout|"
                     r"auth.*(fail|timeout|reject)|assoc.*(fail|reject)|Failed to|hostapd exited|"
                     r"dnsmasq exited|did not start", re.I)


# NetworkManager's failure reasons (journal: "state change: config -> failed (reason 'X', ...)")
# in the words the screen shows (C&G #209 v8.3). Anything else is shown as NetworkManager's own code.
NM_REASONS = {
    "no-secrets": "wrong password (the network refused it)",
    "supplicant-disconnect": "wrong password, or the network dropped the screen while joining",
    "supplicant-timeout": "the network did not answer: too far, or it needs WPA3, which this screen cannot do",
    "supplicant-failed": "the Wi-Fi chip could not start the join",
    "ssid-not-found": "network not found: too far, or 5 GHz only",
    "ip-config-unavailable": "joined, but the network gave no address (DHCP)",
    "ip-config-expired": "joined, but the address ran out (DHCP)",
    "dhcp-failed": "joined, but the network gave no address (DHCP)",
    "dhcp-start-failed": "joined, but could not ask for an address (DHCP)",
}
_IGNORED_REASONS = {"none", "unknown", "new-activation", "user-requested", "connection-assumed",
                    "now-managed", "now-unmanaged", "removed", "managed", "unmanaged",
                    # C&G #273: Shaun's TV showed "NetworkManager said: supplicant-available". That is logged
                    # every time the chip comes back from the hotspot (unavailable -> disconnected): a normal
                    # step, never why a join failed.
                    "supplicant-available", "sleeping", "connection-removed"}


def nm_reason(iface, since):
    """The last real failure reason NetworkManager logged for iface since `since` (epoch s)."""
    out = _run(["journalctl", "-u", "NetworkManager", "--since", f"@{int(since) - 1}", "-o", "cat",
                "--no-pager", "-q"], timeout=10, check=False) or ""
    code = failed = None
    for line in out.splitlines():
        if f"device ({iface})" not in line or "state change" not in line:
            continue
        m = re.search(r"reason '([a-z0-9-]+)'", line)
        if m and m.group(1) not in _IGNORED_REASONS:
            code = m.group(1)
            if re.search(r"-> failed\b", line):   # C&G #273: a reason on the step INTO failed is the cause
                failed = code
    code = failed or code
    if not code:
        return None
    return {"code": code, "words": NM_REASONS.get(code, f"NetworkManager said: {code}")}


def conf_wifi_report(ssids, nets, connected, reason=None, why=None):
    """C&G #270: what became of each Wi-Fi name in usb-router.conf, in plain words for the setup screen.
    On Shaun's v8.8 the conf network did not join and the screen said nothing about why.
    ssids: names from the file, in order. nets: the last scan (None = no scan to look at).
    connected: names of the active connections. reason: nm_reason() for the Wi-Fi chip since boot.
    This chip (Pi Zero W) hears 2.4 GHz only, so a name it never sees is 5 GHz only or out of range."""
    if not ssids:   # C&G #273: why (Config.wifi_why): not found, a .txt name, empty, other heading, ...
        return [{"ssid": None, "state": "none",
                 "words": why or "usb-router.conf has no Wi-Fi name (wifi_ssid is empty, or the file was not read)"}]
    seen = {n.get("ssid"): n for n in (nets or [])}
    out = []
    for ssid in ssids:
        n = seen.get(ssid)
        sec = (n or {}).get("security") or ""
        if ssid in connected:
            state, words = "joined", "joined"
        elif nets is not None and n is None:
            state, words = "not-seen", "not seen: the screen hears 2.4 GHz only (5 GHz only, or too far away)"
        elif "WPA3" in sec and "WPA2" not in sec:
            state, words = "wpa3-only", "the network is WPA3 only; this screen joins WPA2 (turn on WPA2/WPA3 mixed)"
        elif reason:
            state, words = "failed", f"did not join: {reason['words']}"
        elif n is not None:
            state, words = "seen", "seen, not joined yet"
        else:
            state, words = "unknown", "not joined yet"
        out.append({"ssid": ssid, "state": state, "words": words,
                    "signal": (n or {}).get("signal"), "security": sec or None, "band": (n or {}).get("band")})
    return out


def _seen_words(ssid, net, seen):
    """C&G #273: was the network in the last scan, and on which band, for the failed-join line."""
    if not seen or not net:
        return f"{ssid} was not in the last scan (this screen hears 2.4 GHz only: 5 GHz only, or too far)"
    band = net.get("band")
    bits = [f"{band} GHz" if band else None, net.get("security") or "open",
            f"signal {net['signal']}%" if net.get("signal") is not None else None]
    return f"{ssid} was in the last scan: " + ", ".join(b for b in bits if b)


def _plain_error(text):
    if "Secrets were required" in text or "802-11-wireless-security" in text:
        return NM_REASONS["no-secrets"]
    if "No network with SSID" in text:
        return NM_REASONS["ssid-not-found"]
    if "not available" in text:
        return "the Wi-Fi chip did not come back from the setup hotspot in time"
    return text.replace("Error: ", "").strip()[:160] or "the join failed"


def clock_source():
    """'internet' when systemd says the clock is synced, else 'not set' (the Pi has no RTC)."""
    try:
        out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5, check=False).strip()
        return "internet" if out == "yes" else "not set"
    except NetError:
        return None


DIAG_EVENTS = re.compile(r"AP-STA-|AP-ENABLED|AP-DISABLED|IEEE 802\.11: |WPA: |nl80211:|"
                         r"usb-router hotspot:|could not|Could not|Failed to|"
                         r"PSK-MISMATCH|4-Way|WRONG_KEY|pre-shared key|deauth|disassoc|"
                         r"auth.*(fail|timeout|reject)|assoc.*(fail|reject)|brcmf_\w+: .*(err|fail)", re.I)


def _tool(name):
    return shutil.which(name) or next((p for p in (f"/usr/sbin/{name}", f"/sbin/{name}") if os.path.exists(p)), name)


def hotspot_diag(iface, con_name):
    """
    One look at the access point, in plain facts: channel, regulatory country, security as
    NetworkManager holds it, whether the Wi-Fi chip does the WPA handshake itself (brcmfmac
    firmware offload), how many phones are associated, and the last join event in the journal.
    Every part is best effort: a missing tool or permission leaves that part None.
    """
    d = {"iface": iface, "ap": "NetworkManager", "channel": None, "freq": None, "country": None,
         "security": None, "pmf": None, "fw_offload": None, "stations": None, "last_event": None,
         "clock": clock_source(), "refused": False}
    iw = _tool("iw")
    try:
        m = re.search(r"channel (\d+) \((\d+) MHz\)", _run([iw, "dev", iface, "info"], timeout=5, check=False))
        if m:
            d["channel"], d["freq"] = int(m.group(1)), int(m.group(2))
    except NetError:
        pass
    try:
        m = re.search(r"country (\w\w)", _run([iw, "reg", "get"], timeout=5, check=False))
        d["country"] = m.group(1) if m else None
    except NetError:
        pass
    try:
        out = _run([iw, "dev", iface, "station", "dump"], timeout=5, check=False)
        d["stations"] = len(re.findall(r"^Station ", out, re.M))
    except NetError:
        pass
    if con_name:
        try:
            out = nmcli("-g", "802-11-wireless-security.key-mgmt,802-11-wireless-security.pmf",
                        "connection", "show", con_name, check=False).splitlines()
            if len(out) >= 2:
                d["security"], d["pmf"] = out[0] or "open", {"1": "off", "2": "optional", "3": "required"}.get(out[1], out[1])
        except NetError:
            pass
    # brcmfmac's feature_disable is not readable in /sys (module_param perm 0, so v6 showed "?"):
    # read what modprobe will apply instead, the option in /etc/modprobe.d (S&D #193)
    try:
        m = re.search(r"feature_disable=(0x[0-9a-fA-F]+|\d+)", Path(BRCMFMAC_CONF).read_text())
        d["fw_offload"] = "off" if (m and int(m.group(1), 0) & 0x2000) else "on"
    except (OSError, ValueError):
        d["fw_offload"] = "on"
    try:
        # no --since: the Pi has no clock until it reaches internet time, so journal times
        # are not real and a time window can skip everything (S&D #193, v6)
        out = _run(["journalctl", "-b", "--no-pager", "-o", "short", "-n", "400",
                    "-t", "wpa_supplicant", "-t", "kernel", "-t", "NetworkManager", "-t", "hostapd",
                    "-t", "usb-router-hotspot"], timeout=5, check=False)
        # get_tx_power -512 is NetworkManager polling the chip, not a join event (v6 showed only that)
        hits = [l for l in out.splitlines() if DIAG_EVENTS.search(l) and "get_tx_power" not in l]
        if hits:
            parts = hits[-1].split(None, 5)    # Mon DD HH:MM:SS host ident: message
            d["last_event"] = (parts[2] + " " + parts[5])[:160] if len(parts) == 6 else hits[-1][:160]
        # a phone tried and was turned away, or the access point itself failed: the screen shows the line
        d["refused"] = any(REFUSAL.search(l) for l in hits)
    except NetError:
        pass
    return d


class MockNetworkManager:
    name = "mock"
    HOTSPOT_CON = NetworkManager.HOTSPOT_CON
    SHARE_CON = NetworkManager.SHARE_CON

    def __init__(self):
        self.connected_ssid = None
        self.saved = {"Home WiFi"}
        self.hotspot = None
        self.ethernet_plugged = os.environ.get("USB_ROUTER_MOCK_ETH", "0") == "1"
        self.fail_password = "wrong"

    def available(self):
        return True

    def devices(self):
        devs = [{"iface": "wlan0", "type": "wifi",
                 "state": "connected" if self.connected_ssid else "disconnected",
                 "connection": self.connected_ssid, "ip": "192.168.1.42" if self.connected_ssid else None},
                {"iface": "eth0", "type": "ethernet",
                 "state": "connected" if self.ethernet_plugged else "unavailable",
                 "connection": "Wired" if self.ethernet_plugged else None,
                 "ip": "192.168.0.23" if self.ethernet_plugged else None},
                {"iface": "wlan1", "type": "wifi", "state": "disconnected", "connection": None, "ip": None}]
        if self.hotspot:
            for d in devs:
                if d["iface"] == self.hotspot["iface"]:
                    d["state"], d["connection"], d["ip"] = "connected", "usb-router-hotspot", "10.42.0.1"
        return devs

    def wifi_ap_capable(self, iface):
        return True  # both mock cards can run a hotspot; wlan1 is preferred when wlan0 is the uplink

    def wifi_scan(self, rescan=True):
        nets = [
            {"ssid": "Home WiFi", "signal": 82, "security": "WPA2", "secured": True, "band": "5"},
            {"ssid": "Cafe Guest", "signal": 61, "security": "open", "secured": False, "band": "2.4"},
            {"ssid": "Neighbour", "signal": 34, "security": "WPA2", "secured": True, "band": "2.4"},
            {"ssid": "Hotel-Lobby", "signal": 55, "security": "open", "secured": False, "band": "2.4"},
        ]
        for n in nets:
            n["in_use"] = n["ssid"] == self.connected_ssid
            n["bssid"] = "AA:BB:CC:DD:EE:%02X" % (len(n["ssid"]) % 255)
        return nets

    def saved_connections(self):
        return [{"name": s, "type": "802-11-wireless", "device": "wlan0" if s == self.connected_ssid else None}
                for s in sorted(self.saved)]

    def wifi_connect(self, ssid, password=None, iface=None):
        time.sleep(0.3)
        # the mock's only client-capable card is wlan0, so a Wi-Fi hotspot always yields to a connection
        if self.hotspot and self.hotspot["mode"] == "wifi" and iface in (None, self.hotspot["iface"]):
            self.hotspot = None
        if ssid == "Neighbour" and password != "n3ighb0ur-pass":
            raise NetError("Wrong password or the network rejected the connection")
        if ssid in ("Home WiFi", "Neighbour") and not password and ssid not in self.saved:
            raise NetError("Wrong password or the network rejected the connection")
        self.connected_ssid = ssid
        self.saved.add(ssid)

    def wifi_forget(self, ssid):
        self.saved.discard(ssid)
        if self.connected_ssid == ssid:
            self.connected_ssid = None

    def join_state(self):
        return getattr(self, "last_join", None)

    def ensure_profile(self, ssid, password=None, priority=10):
        self.profile_psk = getattr(self, "profile_psk", {})
        if ssid in self.saved:
            if password:
                self.profile_psk[ssid] = password      # C&G #254: an existing profile is updated
            return False
        self.saved.add(ssid)
        self.profile_psk[ssid] = password
        self.provisioned = getattr(self, "provisioned", []) + [ssid]
        return True

    def wifi_ifaces(self):
        return [d["iface"] for d in self.devices() if d["type"] == "wifi"]

    def disconnect(self, iface):
        if iface == "wlan0":
            self.connected_ssid = None
        if iface == "eth0":
            self.ethernet_plugged = False

    def ethernet_connect(self, iface):
        self.ethernet_plugged = True

    def hotspot_start(self, iface, ssid, password, band="bg", share=False, max_clients=None):
        self.hotspot = {"iface": iface, "ssid": ssid, "mode": "wifi", "max_clients": max_clients}

    def hotspot_diag(self, iface):
        refused = os.environ.get("USB_ROUTER_MOCK_REFUSED", "0") == "1"
        return {"iface": iface, "ap": "hostapd", "channel": 6, "freq": 2437, "country": "ES", "security": "wpa-psk",
                "pmf": "off", "fw_offload": "off", "stations": 0, "clock": "not set", "refused": refused,
                "last_event": "04:12:33 wlan0: AP-STA-POSSIBLE-PSK-MISMATCH 66:55:44:33:22:11" if refused else None}

    def hotspot_stop(self):
        self.hotspot = None

    def share_ethernet_start(self, iface):
        self.hotspot = {"iface": iface, "ssid": None, "mode": "ethernet"}

    def share_ethernet_stop(self, iface):
        self.hotspot = None

    def hotspot_status(self):
        if not self.hotspot:
            return {"active": False, "iface": None, "mode": None, "ip": None, "clients": []}
        return {"active": True, "iface": self.hotspot["iface"], "mode": self.hotspot["mode"], "ip": "10.42.0.1",
                "clients": [{"ip": "10.42.0.17", "mac": "11:22:33:44:55:66", "name": "phone"}]}

    def connectivity(self):
        if self.connected_ssid in ("Hotel-Lobby", "Cafe Guest"):
            return "portal"
        return "full" if (self.connected_ssid or self.ethernet_plugged) else "none"


# ---------------------------------------------------------------------------
# Internet check (independent of NetworkManager)
# ---------------------------------------------------------------------------
def internet_check(url, fallback_url=None, timeout=5, mock_backend=None):
    """
    Returns {"online": bool, "captive_portal": bool, "portal_url": str|None}.
    A 204 / expected body means real internet. Any other 2xx/3xx with a body
    means something (a hotel login page) is intercepting traffic.
    """
    if mock_backend is not None:
        verdict = mock_backend.connectivity()
        return {"online": verdict == "full", "captive_portal": verdict == "portal",
                "portal_url": "http://portal.example/login" if verdict == "portal" else None,
                "verdict": verdict}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    for target in [u for u in (url, fallback_url) if u]:
        req = urllib.request.Request(target, headers={"User-Agent": "usb-router/1.0", "Cache-Control": "no-cache"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(512)
                if resp.status == 204 or (resp.status == 200 and body.strip() in (b"success", b"")):
                    return {"online": True, "captive_portal": False, "portal_url": None, "verdict": "full"}
                return {"online": False, "captive_portal": True, "portal_url": target, "verdict": "portal"}
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return {"online": False, "captive_portal": True,
                        "portal_url": e.headers.get("Location") or target, "verdict": "portal"}
            continue
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return {"online": False, "captive_portal": False, "portal_url": None, "verdict": "none"}


class HostapdNetworkManager(NetworkManager):
    """
    NetworkManager for everything, except the Wi-Fi setup hotspot: hostapd + dnsmasq run it as
    root through usb-router-hotspot.service (app/hotspot_ap.py), started over polkit (S&D #193).
    The app writes the request (interface, SSID, password, channel) to a 0600 JSON file the
    service validates. NetworkManager lets go of the interface while the hotspot is up.
    """
    name = "networkmanager+hostapd"
    UNIT = "usb-router-hotspot.service"
    LEASES = "/run/usb-router-ap/leases"

    SCAN_FILE = "/run/usb-router-ap/scan.json"
    HANDBACK_WAIT = 45                # s for NetworkManager to take wlan0 back (v8.4: was 20)
    JOIN_BUSY = 150                   # s the keeper leaves the radio alone during a venue join

    def __init__(self, state_dir=None, scan_file=None):
        self.state_dir = state_dir or os.environ.get("USB_ROUTER_STATE_DIR", "/var/lib/usb-router")
        self.request_path = os.path.join(self.state_dir, "hotspot-ap.json")
        self.scan_request = os.path.join(self.state_dir, "scan-request")
        self.scan_file = scan_file or self.SCAN_FILE
        self._busy_until = 0.0
        self._join = None
        self.sleep = time.sleep

    def busy(self):
        """True while a venue join owns the radio: the keeper must not start the hotspot (C&G #209)."""
        return time.time() < self._busy_until

    # -- devices: say "hotspot", not "unmanaged", for the radio hostapd holds ------------------
    def devices(self):
        devs = super().devices()
        active = None
        for d in devs:
            if d["type"] == "wifi" and d["state"] == "unmanaged":
                if active is None:
                    active = self._unit_active()
                req_iface = self._request().get("iface")
                if active and d["iface"] == req_iface:
                    d["state"] = "hotspot"
                elif not active and d["iface"] == req_iface and not self.busy():
                    # hostapd stopped without handing the radio back (crash, reboot mid-switch)
                    nmcli("device", "set", d["iface"], "managed", "yes", check=False)
                    d["state"] = "disconnected"
        return devs

    # -- scan: NetworkManager cannot scan a radio hostapd holds; the root helper's list instead --
    def _scan_from_helper(self):
        try:
            with open(self.scan_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None, {}
        nets = [n for n in data.get("networks") or [] if isinstance(n, dict) and n.get("ssid")]
        return nets, data

    def wifi_scan(self, rescan=True):
        if not self._unit_active():
            return super().wifi_scan(rescan)
        nets, data = self._scan_from_helper()
        if rescan:
            before = data.get("at", 0)
            try:
                with open(self.scan_request, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            for _ in range(16):           # the helper rescans at most every 30 s; wait up to 8 s
                self.sleep(0.5)
                fresh, d2 = self._scan_from_helper()
                if d2.get("at", 0) > before:
                    nets = fresh
                    break
        nets = sorted(nets or [], key=lambda n: -int(n.get("signal") or 0))
        return nets

    def _radio(self, iface):
        """NetworkManager's state of iface and the chip's own mode (iw: managed / AP), for the trail."""
        try:
            st = {d["iface"]: d["state"] for d in NetworkManager.devices(self)}.get(iface)
        except NetError:
            st = None
        try:
            out = _run(["iw", "dev", iface, "info"], timeout=10, check=False) or ""
        except NetError:
            out = ""
        m = re.search(r"^\s*type\s+(\S+)", out, re.M)
        return st, (m.group(1) if m else None)

    def _handback(self, iface, ssid):
        """
        Stop hostapd, give iface back to NetworkManager, wait until it is usable and sees ssid.
        v8.4 (C&G #209, Edwards_cowork): every step is checked, the wait is HANDBACK_WAIT (45 s)
        counted in sleeps, a stalled radio gets 'managed yes' once more, and each change of state
        lands in the join trail so the screen can show where it stuck.
        Returns (seen, ready).
        """
        trail = []
        t0 = time.time()

        def note(what):
            trail.append(f"{what}@{time.time() - t0:.0f}s")
            self._join_step((self._join or {}).get("step") or "handing the radio back", trail=list(trail))

        _run(["systemctl", "stop", self.UNIT], timeout=30, check=False)
        for _ in range(20):                       # the unit stops within TimeoutStopSec=10
            if not self._unit_active():
                break
            self.sleep(0.5)
        else:
            note("hotspot unit still running")
        try:
            nmcli("device", "set", iface, "managed", "yes")
        except NetError as e:
            note(f"managed yes refused: {str(e)[:80]}")
        last, ready, nudged = None, False, False
        steps = int(self.HANDBACK_WAIT / 0.5)
        for i in range(steps):
            st, typ = self._radio(iface)
            if (st, typ) != last:
                note(f"{st or '?'}/{typ or '?'}")
                last = (st, typ)
            if st not in (None, "unmanaged", "unavailable"):
                ready = True
                break
            if not nudged and i >= steps // 3:    # stuck for 15 s: ask once more
                nudged = True
                nmcli("device", "set", iface, "managed", "yes", check=False)
                note("managed yes again")
            self.sleep(0.5)
        if not ready:
            return False, False
        self._seen_net = None
        for _ in range(15):
            try:
                hit = [n for n in NetworkManager.wifi_scan(self, rescan=True) if n["ssid"] == ssid]
                if hit:
                    self._seen_net = max(hit, key=lambda n: n.get("signal") or 0)   # C&G #273: band + security
                    return True, True
            except NetError:
                pass
            self.sleep(1)
        return False, True

    def join_state(self):
        """The last venue join from the hotspot, for the screen (C&G #209 v8.3): which name was
        asked for, the step it reached and, when it failed, NetworkManager's reason in plain words."""
        return dict(self._join) if self._join else None

    def _join_step(self, step, **kw):
        self._join = {**(self._join or {}), "step": step, "at": time.time(), **kw}
        log.info("venue join: %s %r %s", step, self._join.get("ssid"), kw or "")

    def _join_profile(self, ssid, password, iface):
        """Join with a profile carrying this exact name (C&G #209 v8.3). `device wifi connect` looks
        the name up among the access points NetworkManager has seen; on Shaun's screen it came up
        on a neighbouring name. A profile with ssid=<name>, bound to iface, leaves nothing to look up."""
        saved = {c["name"] for c in self.saved_connections()}
        if ssid in saved and not password:
            nmcli("--wait", "45", "connection", "up", ssid, "ifname", iface, timeout=60)
            return
        if ssid in saved:
            nmcli("connection", "delete", ssid, check=False)       # new password: a fresh profile
        args = ["connection", "add", "type", "wifi", "ifname", iface, "con-name", ssid,
                "ssid", ssid, "connection.autoconnect", "yes"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        nmcli(*args)
        nmcli("--wait", "45", "connection", "up", ssid, "ifname", iface, timeout=60)

    def wifi_connect(self, ssid, password=None, iface=None):
        """
        Join the venue Wi-Fi from the setup hotspot (C&G #209): hostapd holds the only radio, so
        stop it, hand the radio back to NetworkManager, wait until it sees the network, join.
        If the join fails, the setup hotspot comes back with the same settings, so the phone can
        rejoin and try again, and the screen shows why it failed (v8.3).
        """
        if not ssid:
            raise NetError("No network name given")
        req = self._request()
        from_ap = self._unit_active() and req.get("iface") and (not iface or iface == req.get("iface"))
        if not from_ap:
            return super().wifi_connect(ssid, password, iface)
        ap_iface = req["iface"]
        self._busy_until = time.time() + self.JOIN_BUSY
        started = time.time()
        self._join = {"ssid": ssid, "ok": None, "reason": None, "started": started}
        try:
            saved_before = {c["name"] for c in self.saved_connections()}
            self._join_step("handing the radio back")
            seen, ready = self._handback(ap_iface, ssid)
            self._join_step("joining" if seen else "joining (not seen in the scan yet)", seen=seen, ready=ready)
            try:
                if not ready:
                    raise NetError("Error: device is not available")
                try:
                    self._join_profile(ssid, password, ap_iface)
                except NetError as e:
                    if "not available" not in str(e):
                        raise
                    self.sleep(5)                 # the radio came back a moment late: once more
                    self._join_step("joining, second try")
                    nmcli("--wait", "45", "connection", "up", ssid, "ifname", ap_iface, timeout=60)
            except NetError as e:
                reason = nm_reason(ap_iface, started) or {}
                if ready and reason.get("code") not in ("no-secrets", "ssid-not-found"):
                    # C&G #273: not a wrong password and not "not found": the radio may still have been
                    # settling after the hotspot. Once more, after a pause, before setup comes back.
                    self._join_step("joining, second try", first_error=str(e)[:200], first_reason=reason.get("code"))
                    self.sleep(5)
                    try:
                        nmcli("--wait", "45", "connection", "up", ssid, "ifname", ap_iface, timeout=60)
                        e = None
                    except NetError as e2:
                        e = e2
                        reason = nm_reason(ap_iface, started) or {}
                if e is None:
                    self._join_step("joined", ok=True, scan=_seen_words(ssid, getattr(self, "_seen_net", None), seen))
                    return
                words = reason.get("words") or _plain_error(str(e))
                scan = _seen_words(ssid, getattr(self, "_seen_net", None), seen)
                words = f"{words.rstrip('. ')}. {scan}"
                self._join_step("failed", ok=False, reason=words, nm_reason=reason.get("code"),
                                nm_error=str(e)[:200], scan=scan)
                if ssid not in saved_before:   # a failed first try must not count as "configured"
                    nmcli("connection", "delete", ssid, check=False)
                try:
                    self.hotspot_start(ap_iface, req.get("ssid"), req.get("password"),
                                       req.get("band") or "bg", share=bool(req.get("share")),
                                       max_clients=req.get("max_clients"))
                except NetError as e2:
                    log.warning("setup hotspot did not come back after a failed join: %s", e2)
                raise NetError(words)
            self._join_step("joined", ok=True)
        finally:
            self._busy_until = 0.0

    def _request(self):
        try:
            with open(self.request_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _unit_active(self):
        try:
            return _run(["systemctl", "is-active", self.UNIT], timeout=10, check=False).strip() == "active"
        except NetError:
            return False

    def hotspot_start(self, iface, ssid, password, band="bg", share=False, max_clients=None):
        self.hotspot_stop()
        req = {"iface": iface, "ssid": ssid, "password": password, "band": band,
               "channel": 6 if band != "a" else 36, "country": "ES", "share": bool(share),
               "max_clients": max_clients}   # C&G #281: hostapd max_num_sta + dnsmasq dhcp-lease-max
        tmp = self.request_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(req, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.request_path)
        nmcli("device", "set", iface, "managed", "no", check=False)
        try:
            _run(["systemctl", "start", self.UNIT], timeout=45)
        except NetError:
            nmcli("device", "set", iface, "managed", "yes", check=False)
            raise
        for _ in range(20):           # hostapd up and 10.42.0.1 on the interface
            if self._unit_active() and self._ip_of(iface):
                return
            time.sleep(0.5)
        if not self._unit_active():
            nmcli("device", "set", iface, "managed", "yes", check=False)
            raise NetError("The Wi-Fi hotspot (hostapd) did not start; the screen's Wi-Fi check line says why")

    def hotspot_stop(self):
        super().hotspot_stop()        # a NetworkManager hotspot left from an older image
        iface = self._request().get("iface")
        _run(["systemctl", "stop", self.UNIT], timeout=30, check=False)
        if iface:
            nmcli("device", "set", iface, "managed", "yes", check=False)

    def hotspot_status(self):
        if self._unit_active():
            iface = self._request().get("iface")
            if iface:
                return {"active": True, "iface": iface, "mode": "wifi",
                        "ip": self._ip_of(iface), "clients": self.hotspot_clients(iface)}
        return super().hotspot_status()

    def hotspot_clients(self, iface):
        clients = {c["mac"]: c for c in super().hotspot_clients(iface)}
        try:
            with open(self.LEASES, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 4:
                        c = clients.setdefault(parts[1], {"ip": parts[2], "mac": parts[1], "name": None})
                        c["name"] = parts[3] if parts[3] != "*" else c["name"]
        except OSError:
            pass
        return list(clients.values())

    def hotspot_diag(self, iface):
        d = hotspot_diag(iface, None)
        req = self._request()
        d["ap"] = "hostapd"
        d["security"], d["pmf"] = ("wpa-psk", "off") if req.get("password") else (None, None)
        return d


def hostapd_ready():
    return os.path.exists("/usr/sbin/hostapd") and os.path.exists(
        "/etc/systemd/system/" + HostapdNetworkManager.UNIT)


def get_backend(mock: bool, hotspot_backend: str = "auto"):
    """auto: hostapd for the hotspot when the image has it (the Pi), else NetworkManager."""
    if mock:
        return MockNetworkManager()
    if hotspot_backend == "hostapd" or (hotspot_backend == "auto" and hostapd_ready()):
        return HostapdNetworkManager()
    return NetworkManager()
