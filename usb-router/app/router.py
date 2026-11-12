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

from netman import NetError

log = logging.getLogger("usb-router.router")


class Router:
    def __init__(self, backend, config):
        self.nm = backend
        self.cfg = config
        self.lock = threading.Lock()
        self.last_error = None
        self.auto_disabled = False  # set when the user manually stops the hotspot

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

    # -- control -----------------------------------------------------------
    def start(self, iface=None, mode=None):
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
                                          self.cfg.get("hotspot_password"), self.cfg.get("hotspot_band"))
                else:
                    self.nm.share_ethernet_start(chosen["iface"])
                self.last_error = None
                self.auto_disabled = False
                log.info("hotspot started on %s (%s)", chosen["iface"], mode)
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
            if manual:
                self.auto_disabled = True
            return self.status()

    def restart(self):
        self.stop(manual=False)
        return self.start()

    def status(self, online=None):
        s = self.nm.hotspot_status()
        s["ssid"] = self.cfg.get("hotspot_ssid") if s["mode"] == "wifi" else None
        s["password"] = self.cfg.get("hotspot_password") if s["mode"] == "wifi" else None
        s["last_error"] = self.last_error
        s["enabled"] = self.cfg.flag("hotspot_enabled") and not self.auto_disabled
        # "setup" = we are broadcasting without an uplink so a phone can configure us
        s["setup"] = bool(s["active"] and online is False)
        s["ui_url"] = f"http://{s['ip'] or '10.42.0.1'}:{self.cfg.get('listen_port')}" if s["active"] else None
        return s

    def wants_setup_hotspot(self):
        return self.cfg.flag("setup_hotspot") and not self.auto_disabled

    # -- background keeper -------------------------------------------------
    def run_keeper(self, is_online, interval=10, grace=25):
        """
        Keep the hotspot up:
          online  + hotspot_enabled  -> share the internet connection
          offline + setup_hotspot    -> broadcast anyway (after a grace period, so a
                                        provisioned Wi-Fi gets a chance to connect first)
                                        and serve the setup page to phones
        """
        started = time.time()

        def loop():
            while True:
                try:
                    online = is_online()
                    active = self.nm.hotspot_status()["active"]
                    want = (online and self.cfg.flag("hotspot_enabled") and not self.auto_disabled) or \
                           (not online and self.wants_setup_hotspot() and time.time() - started > grace)
                    if want and not active:
                        try:
                            self.start()
                        except NetError:
                            pass  # already stored in last_error; retry next tick
                except Exception as e:  # never let the keeper die
                    log.exception("keeper error: %s", e)
                time.sleep(interval)
        threading.Thread(target=loop, name="hotspot-keeper", daemon=True).start()
