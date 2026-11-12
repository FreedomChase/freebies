"""
Thin wrapper around nmcli (NetworkManager) for the connection manager.

Everything that touches the network goes through this module so the Flask app
stays testable: set USB_ROUTER_MOCK=1 and a fake NetworkManager is used instead.

nmcli terse output (-t) separates fields with ':' and escapes literal colons
as '\\:' so we split on unescaped colons only.
"""

import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

NMCLI_TIMEOUT = 40


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

    def ensure_profile(self, ssid, password=None, priority=10):
        """Create a saved, auto-connecting profile (zero-touch provisioning). No-op if it exists."""
        if ssid in {c["name"] for c in self.saved_connections()}:
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

    def hotspot_start(self, iface, ssid, password, band="bg"):
        self.hotspot_stop()
        args = ["connection", "add", "type", "wifi", "ifname", iface,
                "con-name", self.HOTSPOT_CON, "autoconnect", "no", "ssid", ssid,
                "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
                "ipv4.method", "shared", "ipv6.method", "ignore"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                     "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp"]
        nmcli(*args)
        nmcli("connection", "up", self.HOTSPOT_CON, timeout=45)

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

    def ensure_profile(self, ssid, password=None, priority=10):
        if ssid in self.saved:
            return False
        self.saved.add(ssid)
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

    def hotspot_start(self, iface, ssid, password, band="bg"):
        self.hotspot = {"iface": iface, "ssid": ssid, "mode": "wifi"}

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


def get_backend(mock: bool):
    return MockNetworkManager() if mock else NetworkManager()
