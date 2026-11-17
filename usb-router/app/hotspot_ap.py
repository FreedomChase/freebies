#!/usr/bin/env python3
"""
The setup hotspot on the Raspberry Pi with hostapd + dnsmasq, not NetworkManager (S&D #193).

On the Pi Zero W (brcmfmac) an iPhone could not join NetworkManager's access point at all
(v5 and v6: "Unable to join", 0 phones joined, nothing in the log). hostapd is the access
point the Pi community runs on this chip, and it logs every auth/assoc/handshake step, so a
failure names itself on the screen's Wi-Fi check line.

Split by privilege:
  the app (user kiosk)   writes /var/lib/usb-router/hotspot-ap.json (0600) and asks systemd,
                         through polkit, to start/stop usb-router-hotspot.service
  this file, as root     `hotspot_ap.py run` (the unit's ExecStart): validates the JSON, writes
                         its own hostapd/dnsmasq configs under /run, sets 10.42.0.1/24 on the
                         interface, runs hostapd and dnsmasq, and undoes it all on stop

Nothing the app writes reaches a root config unchecked: the interface, SSID, password and
channel are validated, and from the captive-portal file only interface-name= lines for the
same interface are copied (a dnsmasq dhcp-script= line there would run as root).
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

STATE_DIR = os.environ.get("USB_ROUTER_STATE_DIR", "/var/lib/usb-router")
REQUEST = os.path.join(STATE_DIR, "hotspot-ap.json")
CAPTIVE = os.path.join(STATE_DIR, "captive-dns.conf")
RUN_DIR = "/run/usb-router-ap"
LEASES = os.path.join(RUN_DIR, "leases")
UNIT = "usb-router-hotspot.service"
ADDR, PREFIX = "10.42.0.1", 24
DHCP_RANGE = "10.42.0.10,10.42.0.100,255.255.255.0,2h"
NFT_TABLE = "usbrouter_ap"

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
CAPTIVE_LINE = re.compile(r"^interface-name=([A-Za-z0-9.-]{1,253}),([A-Za-z0-9_.-]{1,15})/4$")
CHANNELS = {"bg": set(range(1, 14)), "a": {36, 40, 44, 48}}


class Invalid(ValueError):
    pass


def validate(req):
    """The request as the app wrote it -> clean values, or Invalid."""
    if not isinstance(req, dict):
        raise Invalid("request is not an object")
    iface = str(req.get("iface") or "")
    if not IFACE_RE.match(iface):
        raise Invalid(f"bad interface {iface!r}")
    ssid = str(req.get("ssid") or "")
    if not (1 <= len(ssid.encode("utf-8")) <= 32):
        raise Invalid("SSID must be 1-32 bytes")
    password = str(req.get("password") or "")
    if not (8 <= len(password) <= 63) or any(not (32 <= ord(c) <= 126) for c in password):
        raise Invalid("password must be 8-63 printable ASCII characters")
    band = "a" if req.get("band") == "a" else "bg"
    try:
        channel = int(req.get("channel") or (6 if band == "bg" else 36))
    except (TypeError, ValueError):
        raise Invalid("bad channel")
    if channel not in CHANNELS[band]:
        raise Invalid(f"channel {channel} is not allowed on band {band}")
    return {"iface": iface, "ssid": ssid, "password": password, "band": band,
            "channel": channel, "country": _country(req.get("country")), "share": bool(req.get("share"))}


def _country(value):
    v = str(value or "").upper()
    return v if re.match(r"^[A-Z]{2}$", v) else "ES"


def hostapd_conf(v):
    """Plain WPA2-PSK, CCMP only, no PMF, no WPA3: the most compatible AP there is."""
    return "\n".join([
        "# written by usb-router hotspot_ap.py (S&D #193); regenerated at every start",
        f"interface={v['iface']}",
        "driver=nl80211",
        f"ssid2={v['ssid'].encode('utf-8').hex()}",
        "utf8_ssid=1",
        f"country_code={v['country']}",
        "ieee80211d=1",
        f"hw_mode={'a' if v['band'] == 'a' else 'g'}",
        f"channel={v['channel']}",
        "ieee80211n=1",
        "wmm_enabled=1",
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
        "macaddr_acl=0",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        "ieee80211w=0",
        f"wpa_passphrase={v['password']}",
        # every join step to stdout, which systemd puts in the journal for the Wi-Fi check line
        "logger_syslog=0",
        "logger_stdout=-1",
        "logger_stdout_level=1",
        "",
    ])


def captive_lines(text, iface):
    """Only interface-name=<probe host>,<iface>/4 lines survive into the root dnsmasq config."""
    out = []
    for line in (text or "").splitlines():
        m = CAPTIVE_LINE.match(line.strip())
        if m and m.group(2) == iface:
            out.append(line.strip())
    return out


def dnsmasq_conf(v, captive_text=""):
    lines = [
        "# written by usb-router hotspot_ap.py (S&D #193); regenerated at every start",
        f"interface={v['iface']}",
        "bind-interfaces",
        "except-interface=lo",
        f"listen-address={ADDR}",
        "no-resolv",
        "no-hosts",
        "no-poll",
        f"dhcp-range={DHCP_RANGE}",
        f"dhcp-option=option:router,{ADDR}",
        f"dhcp-option=option:dns-server,{ADDR}",
        f"dhcp-leasefile={LEASES}",
        "dhcp-authoritative",
        "log-dhcp",
    ]
    if v["share"]:
        lines.append("server=1.1.1.1")
        lines.append("server=9.9.9.9")
    lines += captive_lines(captive_text, v["iface"])
    return "\n".join(lines) + "\n"


def _write(path, text, mode):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _sh(*args, check=False):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:   # a missing tool must not stop the hotspot
        if check:
            raise RuntimeError(f"{args[0]}: {e}")
        return subprocess.CompletedProcess(list(args), 127, "", str(e))
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip() or r.returncode}")
    return r


def _which(name):
    for d in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return name


def _down(iface, share):
    if iface:
        _sh("ip", "addr", "flush", "dev", iface)
    if share:
        _sh(_which("nft"), "delete", "table", "ip", NFT_TABLE)


def run():
    try:
        with open(REQUEST, encoding="utf-8") as f:
            v = validate(json.load(f))
    except (OSError, ValueError) as e:
        print(f"usb-router hotspot: no valid request in {REQUEST}: {e}", flush=True)
        return 2
    try:
        with open(CAPTIVE, encoding="utf-8") as f:
            captive = f.read()
    except OSError:
        captive = ""
    os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
    os.chmod(RUN_DIR, 0o755)
    hconf, dconf = os.path.join(RUN_DIR, "hostapd.conf"), os.path.join(RUN_DIR, "dnsmasq.conf")
    _write(hconf, hostapd_conf(v), 0o600)
    _write(dconf, dnsmasq_conf(v, captive), 0o644)
    if not os.path.exists(LEASES):
        _write(LEASES, "", 0o644)
    iface = v["iface"]
    _sh(_which("rfkill"), "unblock", "wifi")
    _sh(_which("iw"), "dev", iface, "set", "power_save", "off")
    _sh("ip", "addr", "flush", "dev", iface)
    print(f"usb-router hotspot: hostapd on {iface}, channel {v['channel']}, country {v['country']}, "
          f"WPA2-PSK CCMP, PMF off; dhcp {DHCP_RANGE}", flush=True)
    procs = []

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for p in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        _down(iface, v["share"])

    signal.signal(signal.SIGTERM, lambda *a: (stop(), sys.exit(0)))
    hostapd = subprocess.Popen([_which("hostapd"), hconf])
    procs.append(hostapd)
    # hostapd brings the interface up in AP mode; the address goes on after that
    for _ in range(50):
        if hostapd.poll() is not None:
            break
        if "UP" in _sh("ip", "-o", "link", "show", iface).stdout.split("<", 1)[-1].split(">", 1)[0]:
            break
        time.sleep(0.2)
    if hostapd.poll() is not None:
        print(f"usb-router hotspot: hostapd exited ({hostapd.returncode})", flush=True)
        _down(iface, v["share"])
        return 1
    _sh("ip", "addr", "add", f"{ADDR}/{PREFIX}", "dev", iface)
    if v["share"]:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1\n")
        nft = _which("nft")
        _sh(nft, "add", "table", "ip", NFT_TABLE)
        _sh(nft, "add", "chain", "ip", NFT_TABLE, "post", "{ type nat hook postrouting priority 100 ; }")
        _sh(nft, "add", "rule", "ip", NFT_TABLE, "post", "ip", "saddr", f"{ADDR.rsplit('.', 1)[0]}.0/{PREFIX}",
            "oifname", "!=", iface, "masquerade")
    dnsmasq = subprocess.Popen([_which("dnsmasq"), "-k", f"--conf-file={dconf}", "--pid-file="])
    procs.append(dnsmasq)
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    dead = next(p for p in procs if p.poll() is not None)
    print(f"usb-router hotspot: {'hostapd' if dead is hostapd else 'dnsmasq'} exited ({dead.returncode})",
          flush=True)
    stop()
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "run":
        sys.exit(run())
    print("usage: hotspot_ap.py run   (as root, from usb-router-hotspot.service)", file=sys.stderr)
    sys.exit(2)
