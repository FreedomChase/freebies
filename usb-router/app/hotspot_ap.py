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
# v8.1 (C&G #209): NetworkManager lets go of the interface for hostapd, so it can no longer scan.
# This helper scans as root before hostapd starts (and again when the app touches SCAN_REQUEST),
# and writes what it saw to SCAN_FILE (0644) for the app's network list.
SCAN_FILE = os.path.join(RUN_DIR, "scan.json")
SCAN_REQUEST = os.path.join(STATE_DIR, "scan-request")
RESCAN_MIN_GAP = 30
UNIT = "usb-router-hotspot.service"
ADDR, PREFIX = "10.42.0.1", 24
DHCP_RANGE = "10.42.0.10,10.42.0.100,255.255.255.0,2h"
NFT_TABLE = "usbrouter_ap"
# C&G #281: guests on the sharing hotspot reach the internet only, never the venue's network or each other. The same
# list is in scripts/usb-router-lan-guard (the NetworkManager path); a test keeps the two equal.
GUARD_PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")

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
            "channel": channel, "country": _country(req.get("country")), "share": bool(req.get("share")),
            "max_clients": _max_clients(req.get("max_clients"))}


MAX_CLIENTS = 10   # C&G #281: never more phones than this, whatever the request says


def _max_clients(value):
    """1..MAX_CLIENTS; missing or bad = MAX_CLIENTS (the cap always applies)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return MAX_CLIENTS
    return max(1, min(n, MAX_CLIENTS))


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
        # C&G #281: phones on the hotspot cannot reach each other over the air
        "ap_isolate=1",
        # C&G #281: the 11th phone is refused at association (hostapd answers "AP is full")
        f"max_num_sta={v.get('max_clients', MAX_CLIENTS)}",
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
        # C&G #281: and no address for more than the cap, if one slips past hostapd
        f"dhcp-lease-max={v.get('max_clients', MAX_CLIENTS)}",
        "dhcp-authoritative",
        "log-dhcp",
    ]
    if v["share"]:
        lines.append("server=1.1.1.1")
        lines.append("server=9.9.9.9")
    lines += captive_lines(captive_text, v["iface"])
    return "\n".join(lines) + "\n"


def _iw_ssid(raw):
    """iw prints non-printable SSID bytes as \\xNN; undo that and decode as UTF-8."""
    out, i = bytearray(), 0
    b = raw.encode("utf-8", "surrogateescape")
    while i < len(b):
        if b[i:i + 2] == b"\\x" and i + 4 <= len(b):
            try:
                out.append(int(b[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        out.append(b[i])
        i += 1
    return out.decode("utf-8", "replace")


def parse_iw_scan(text):
    """`iw dev X scan` output -> the app's network list (strongest BSSID per SSID, hidden ones dropped)."""
    nets, cur = [], None
    for line in (text or "").splitlines():
        m = re.match(r"^BSS ([0-9a-fA-F:]{17})", line)
        if m:
            cur = {"bssid": m.group(1).lower(), "ssid": "", "dbm": -100.0, "freq": 0, "rsn": False,
                   "wpa": False, "privacy": False, "sae": False, "psk": False, "in_use": False}
            nets.append(cur)
            continue
        if cur is None:
            continue
        t = line.strip()
        if t.startswith("SSID:"):
            cur["ssid"] = _iw_ssid(t[5:].strip())
        elif t.startswith("signal:"):
            try:
                cur["dbm"] = float(t.split()[1])
            except (IndexError, ValueError):
                pass
        elif t.startswith("freq:"):
            try:
                cur["freq"] = int(float(t.split()[1]))
            except (IndexError, ValueError):
                pass
        elif t.startswith("capability:") and "Privacy" in t:
            cur["privacy"] = True
        elif t.startswith("RSN:"):
            cur["rsn"] = True
        elif t.startswith("WPA:"):
            cur["wpa"] = True
        elif "Authentication suites:" in t:
            cur["sae"] = cur["sae"] or "SAE" in t
            cur["psk"] = cur["psk"] or bool(re.search(r"\bPSK\b", t))
    best = {}
    for n in nets:
        if not n["ssid"]:
            continue
        signal = max(0, min(100, int(round(2 * (n["dbm"] + 100)))))
        if n["rsn"] and n["sae"]:
            sec = "WPA2 WPA3" if n["psk"] else "WPA3"
        elif n["rsn"]:
            sec = "WPA2"
        elif n["wpa"]:
            sec = "WPA1"
        elif n["privacy"]:
            sec = "WEP"
        else:
            sec = ""
        entry = {"ssid": n["ssid"], "signal": signal, "security": sec or "open", "secured": bool(sec),
                 "band": "5" if n["freq"] >= 4900 else "2.4", "in_use": False, "bssid": n["bssid"]}
        if n["ssid"] not in best or signal > best[n["ssid"]]["signal"]:
            best[n["ssid"]] = entry
    return sorted(best.values(), key=lambda e: -e["signal"])


def scan(iface, ap=False, tries=3):
    """One root scan of iface; ap=True scans while hostapd runs (iw ... scan ap-force). -> (nets, error)."""
    args = [_which("iw"), "dev", iface, "scan"] + (["ap-force"] if ap else [])
    err = "no scan"
    for i in range(tries):
        r = _sh(*args)
        if r.returncode == 0:
            return parse_iw_scan(r.stdout), None
        err = (r.stderr or r.stdout or str(r.returncode)).strip()[:160]
        time.sleep(1.5 * (i + 1))          # -16 (busy) right after a mode change is normal
    return [], err


def write_scan(nets, mode, error=None, path=None):
    _write(path or SCAN_FILE, json.dumps({"at": time.time(), "mode": mode, "error": error,
                                          "networks": nets}), 0o644)


def _scan_requested(last_handled):
    """mtime of the app's request file if it is newer than the last one handled, else None."""
    try:
        m = os.stat(SCAN_REQUEST).st_mtime
    except OSError:
        return None
    return m if m > last_handled else None


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


def guard_rules(iface):
    """C&G #281: the forward filter that keeps guests off private addresses (the stick itself is INPUT, not forward)."""
    return (f"table ip {NFT_TABLE} {{\n  chain guard {{\n    type filter hook forward priority -10; policy accept;\n"
            f"    iifname \"{iface}\" ip daddr {{ {', '.join(GUARD_PRIVATE)} }} counter drop\n  }}\n}}\n")


def _down(iface, share):
    if iface:
        _sh("ip", "addr", "flush", "dev", iface)
        # C&G #209 v8.4: hand the radio back in station mode. brcmfmac can stay in AP mode after
        # hostapd exits, and NetworkManager then calls wlan0 "not available" for a venue join.
        _sh("ip", "link", "set", iface, "down")
        _sh(_which("iw"), "dev", iface, "set", "type", "managed")
        _sh("ip", "link", "set", iface, "up")
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
    # scan in station mode first: once hostapd has the radio, NetworkManager cannot (C&G #209)
    _sh("ip", "link", "set", iface, "up")
    nets, err = scan(iface)
    write_scan(nets, "station", err)
    print(f"usb-router hotspot: scan before the AP: {len(nets)} networks" + (f" ({err})" if err else ""),
          flush=True)
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
        try:
            g = subprocess.run([nft, "-f", "-"], input=guard_rules(iface), text=True, capture_output=True, timeout=20)
            err = "" if g.returncode == 0 else (g.stderr.strip() or f"nft exit {g.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            err = str(e)
        if err:   # fail closed: no guard, no sharing (the hotspot stays up for the setup page, without internet)
            _sh(nft, "delete", "table", "ip", NFT_TABLE)
            print(f"usb-router hotspot: LAN guard on {iface} FAILED ({err}); sharing stopped", flush=True)
        else:
            print(f"usb-router hotspot: LAN guard on {iface}: guests reach the internet only", flush=True)
    dnsmasq = subprocess.Popen([_which("dnsmasq"), "-k", f"--conf-file={dconf}", "--pid-file="])
    procs.append(dnsmasq)
    handled = _scan_requested(0) or 0.0
    last_scan = time.time()
    while all(p.poll() is None for p in procs):
        time.sleep(1)
        asked = _scan_requested(handled)
        if asked and time.time() - last_scan >= RESCAN_MIN_GAP:
            handled, last_scan = asked, time.time()
            fresh, err = scan(iface, ap=True, tries=1)
            if fresh:                  # an empty AP-mode scan keeps the list from before the AP
                write_scan(fresh, "ap")
            print(f"usb-router hotspot: rescan with the AP up: {len(fresh)} networks"
                  + (f" ({err})" if err else ""), flush=True)
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
