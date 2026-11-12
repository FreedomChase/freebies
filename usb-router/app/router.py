"""
Router / hotspot orchestration.

Once the stick has an uplink (Wi-Fi, Ethernet, or a 4G dongle) it can share
that uplink out of another adapter so phones, laptops and TVs can use it:

  uplink = Ethernet  ->  built-in Wi-Fi card runs the hotspot
  uplink = Wi-Fi     ->  a second Wi-Fi adapter (AP-capable) runs the hotspot,
                         or a free Ethernet port shares the connection by cable

NetworkManager's "shared" IPv4 method handles DHCP (dnsmasq), DNS forwarding
and NAT masquerading for us, so this module only has to decide *which*
interface to use and keep the hotspot alive.
"""

import logging
import threading
import time

from config import is_weak_password
from netman import NetError

log = logging.getLogger("usb-router.router")


class Router:
    def __init__(self, backend, config):
        self.nm = backend
        self.cfg = config
        self.lock = threading.Lock()
        self.last_error = None
        self.auto_disabled = False  # set when the user manually stops the hotspot
        self.role = None            # why the hotspot is up: "setup" | "share" | "manual"
        self.setup_until = 0.0      # Ctrl+N / "Allow phone setup" re-opens setup mode until this time
        self.booted = time.time()

    SETUP_WINDOW = 15 * 60

    # -- discovery ---------------------------------------------------------
    def uplink(self, devices=None):
        """The device currently carrying our internet connection, if any."""
        devices = devices if devices is not None else self.nm.devices()
        status = self.nm.hotspot_status()
        for d in devices:
            if d["state"].startswith("connected") and d["iface"] != status.get("iface") \
                    and d["type"] in ("wifi", "ethernet", "gsm", "cdma", "wwan", "usb"):
                return d
        return None

    def candidates(self, devices=None):
        """Adapters that could run the hotspot/sharing right now."""
        devices = devices if devices is not None else self.nm.devices()
        up = self.uplink(devices)
        out = []
        for d in devices:
            if up and d["iface"] == up["iface"]:
                continue
            if d["type"] == "wifi":
                if d["state"] in ("unmanaged",):
                    continue
                out.append({"iface": d["iface"], "mode": "wifi",
                            "ap_capable": self.nm.wifi_ap_capable(d["iface"])})
            elif d["type"] == "ethernet" and d["state"] not in ("unmanaged",):
                out.append({"iface": d["iface"], "mode": "ethernet", "ap_capable": True})
        # prefer AP-capable Wi-Fi, then Ethernet
        out.sort(key=lambda c: (c["mode"] != "wifi" or not c["ap_capable"], c["mode"] != "wifi"))
        return out

    def is_configured(self):
        """A screen counts as configured once it has any Wi-Fi to try (config or saved)."""
        if self.cfg.provisioned_wifi():
            return True
        try:
            for c in self.nm.saved_connections():
                if c["name"] == self.nm.HOTSPOT_CON or c["name"].startswith(self.nm.SHARE_CON):
                    continue
                if c["type"] in ("802-11-wireless", "wifi"):
                    return True
        except NetError:
            pass
        return False

    def open_setup_window(self, seconds=None):
        """Let the setup hotspot come back for a while (keyboard user pressed Ctrl+N)."""
        self.setup_until = time.time() + (seconds or self.SETUP_WINDOW)
        self.auto_disabled = False
        return self.setup_until

    def setup_window_open(self):
        return time.time() < self.setup_until

    def desired(self, online, grace=25):
        """
        What the keeper wants right now:
          share  online and the operator turned sharing on
          setup  offline, phone setup allowed, and the screen is unconfigured
                 (or someone re-opened setup mode from the keyboard)
          off    everything else; in particular once the screen is online the
                 setup hotspot goes away
        """
        if self.auto_disabled:
            return "off"
        if online:
            return "share" if self.cfg.flag("hotspot_enabled") else "off"
        if not self.cfg.flag("setup_hotspot"):
            return "off"
        if time.time() - self.booted < grace and not self.setup_window_open():
            return "off"  # give a provisioned Wi-Fi a chance to connect first
        if not self.is_configured() or self.setup_window_open():
            return "setup"
        return "off"

    def tick(self, online):
        """One keeper step. Returns the action taken (for tests/logging)."""
        want = self.desired(online)
        active = self.nm.hotspot_status()["active"]
        if active and self.role is None:
            # Up before this process started: the app restarted or crashed while
            # NetworkManager kept the hotspot running, so nobody here knows why it is up.
            # Adopt it as the keeper's own (share if sharing is wanted now, otherwise
            # setup), so the rules below still switch it off once the screen is online.
            self.role = "share" if want == "share" else "setup"
            log.info("adopted a hotspot that was already up (role=%s)", self.role)
        if want in ("share", "setup") and not active:
            try:
                self.start(role=want)
                return "started-" + want
            except NetError:
                return "failed"
        if active and self.role == "setup" and want != "setup":
            self.stop(manual=False)
            return "stopped-setup"
        if active and self.role == "share" and want == "off":
            self.stop(manual=False)
            return "stopped-share"
        return "noop"

    # -- control -----------------------------------------------------------
    def start(self, iface=None, mode=None, role="manual"):
        password = self.cfg.hotspot_password()
        if is_weak_password(password):
            self.last_error = "Refusing to start the hotspot with an empty or default password"
            raise NetError(self.last_error)
        with self.lock:
            devices = self.nm.devices()
            cands = self.candidates(devices)
            chosen = None
            if iface:
                chosen = next((c for c in cands if c["iface"] == iface), None)
                if not chosen:
                    raise NetError(f"{iface} is in use as the internet uplink or is not available")
            else:
                chosen = next((c for c in cands if c["mode"] == "wifi" and c["ap_capable"]), None) \
                    or next((c for c in cands if c["mode"] == "ethernet"), None)
            if not chosen:
                raise NetError("No spare adapter for the hotspot. Plug in a second Wi-Fi adapter or an Ethernet cable.")
            mode = mode or chosen["mode"]
            try:
                if mode == "wifi":
                    if not chosen["ap_capable"]:
                        raise NetError(f"{chosen['iface']} does not support access-point mode")
                    self.nm.hotspot_start(chosen["iface"], self.cfg.get("hotspot_ssid"),
                                          password, self.cfg.get("hotspot_band"))
                else:
                    self.nm.share_ethernet_start(chosen["iface"])
                self.last_error = None
                self.auto_disabled = False
                self.role = role
                log.info("hotspot started on %s (%s, role=%s)", chosen["iface"], mode, role)
            except NetError as e:
                self.last_error = str(e)
                log.warning("hotspot failed: %s", e)
                raise
            return self.status()

    def stop(self, manual=True):
        with self.lock:
            status = self.nm.hotspot_status()
            if status["active"]:
                if status["mode"] == "ethernet":
                    self.nm.share_ethernet_stop(status["iface"])
                else:
                    self.nm.hotspot_stop()
            self.role = None
            if manual:
                self.auto_disabled = True
            return self.status()

    def restart(self):
        self.stop(manual=False)
        return self.start()

    def status(self, online=None, local=False):
        s = self.nm.hotspot_status()
        s["ssid"] = self.cfg.get("hotspot_ssid") if s["mode"] == "wifi" else None
        # the password is shown on the physical screen (and in its QR code), never sent to the LAN side
        s["password"] = self.cfg.hotspot_password() if (s["mode"] == "wifi" and local) else None
        s["last_error"] = self.last_error
        s["enabled"] = self.cfg.flag("hotspot_enabled") and not self.auto_disabled
        s["role"] = self.role if s["active"] else None
        # "setup" = we are broadcasting without an uplink so a phone can configure us
        s["setup"] = bool(s["active"] and online is False and self.role in ("setup", "manual", None))
        s["setup_window_until"] = self.setup_until if self.setup_window_open() else None
        s["configured"] = self.is_configured()
        s["ui_url"] = f"http://{s['ip'] or '10.42.0.1'}:{self.cfg.get('listen_port')}" if s["active"] else None
        return s

    def wants_setup_hotspot(self):
        return self.cfg.flag("setup_hotspot") and not self.auto_disabled

    # -- background keeper -------------------------------------------------
    def run_keeper(self, is_online, interval=10):
        """Drive the hotspot from desired(): share when online and asked to, setup when offline
        and unconfigured, otherwise off. See desired()/tick()."""
        def loop():
            while True:
                try:
                    self.tick(is_online())
                except Exception as e:  # never let the keeper die
                    log.exception("keeper error: %s", e)
                time.sleep(interval)
        threading.Thread(target=loop, name="hotspot-keeper", daemon=True).start()
