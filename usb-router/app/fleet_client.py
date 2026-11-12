"""
Fleet client: lets a central dashboard (see ../fleet/) see every screen and
change what it shows without touching the stick.

Every `fleet_interval` seconds the screen POSTs a heartbeat to
    {fleet_url}/api/heartbeat
and applies whatever the reply says:
    {"homepage": "...", "kiosk": true, "reload_every": 30, "rotation": "left",
     "action": "reload" | "reboot" | "restart-browser" | null,
     "name": "Lobby left"}
Only keys present in the reply are applied; the token is sent as a Bearer header.
"""

import json
import logging
import platform
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("usb-router.fleet")

REMOTE_KEYS = ("homepage", "kiosk", "reload_every", "rotation", "allow_exit", "device_name")


class FleetClient:
    def __init__(self, cfg, status_fn, on_action):
        self.cfg = cfg
        self.status_fn = status_fn      # -> dict with online/uplink/hotspot/current_url
        self.on_action = on_action      # callable(action: str)
        self.last_contact = None
        self.last_error = None
        self.started = time.time()

    def enabled(self):
        """The client only runs with both a URL and a token; there is no default token."""
        return bool(self.cfg.get("fleet_url")) and bool(self.cfg.get("fleet_token"))

    def reset(self):
        self.last_contact = None
        self.last_error = None if self.enabled() else self._disabled_reason()

    def _disabled_reason(self):
        if not self.cfg.get("fleet_url"):
            return None
        if not self.cfg.get("fleet_token"):
            return "fleet_token is not set; refusing to contact the dashboard without one"
        return None

    def info(self):
        return {"configured": bool(self.cfg.get("fleet_url")), "enabled": self.enabled(),
                "last_contact": self.last_contact, "last_error": self.last_error or self._disabled_reason(),
                "url": self.cfg.get("fleet_url")}

    def heartbeat(self):
        base = self.cfg.get("fleet_url").rstrip("/")
        if not base or not self.enabled():
            self.last_error = self._disabled_reason()
            return None
        status = self.status_fn()
        payload = {
            "device_id": self.cfg.device_id(),
            "name": self.cfg.get("device_name"),
            "online": status.get("online"),
            "uplink": status.get("uplink"),
            "hotspot": {k: status.get("hotspot", {}).get(k) for k in ("active", "mode", "iface", "setup")},
            "current_url": status.get("current_url"),
            "homepage": self.cfg.resolve_url(),
            "kiosk": self.cfg.flag("kiosk"),
            "rotation": self.cfg.get("rotation"),
            "uptime": int(time.time() - self.started),
            "host": platform.node(),
            "version": "1.0",
        }
        req = urllib.request.Request(base + "/api/heartbeat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "usb-router/1.0"})
        req.add_header("Authorization", "Bearer " + self.cfg.get("fleet_token"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                reply = json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = str(e)
            return None
        self.last_contact = time.time()
        self.last_error = None
        return self.apply(reply)

    def apply(self, reply):
        if not isinstance(reply, dict):
            return {}
        updates = {}
        for key in REMOTE_KEYS:
            if key in reply and reply[key] is not None:
                updates["device_name" if key == "name" else key] = reply[key]
        if "name" in reply and reply["name"]:
            updates["device_name"] = reply["name"]
        changed = {}
        if updates:
            try:
                changed = self.cfg.save(updates)
            except ValueError as e:
                self.last_error = f"rejected remote setting: {e}"
        action = reply.get("action")
        if action:
            try:
                self.on_action(action)
            except Exception as e:  # noqa: BLE001
                log.warning("fleet action %s failed: %s", action, e)
        return {"changed": changed, "action": action}

    def run(self):
        if self.cfg.get("fleet_url") and not self.cfg.get("fleet_token"):
            log.warning("fleet_url is set but fleet_token is empty: fleet client disabled until a token is configured")

        def loop():
            while True:
                try:
                    if self.enabled():
                        self.heartbeat()
                except Exception as e:  # noqa: BLE001
                    log.exception("fleet heartbeat error: %s", e)
                time.sleep(max(10, int(self.cfg.get("fleet_interval") or 60)))
        threading.Thread(target=loop, name="fleet-heartbeat", daemon=True).start()
