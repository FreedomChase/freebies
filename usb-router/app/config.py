"""
Configuration for the USB Router connection manager.

Precedence (highest first):
  1. Environment variables  (USB_ROUTER_*)
  2. /config/usb-router.conf      -- the FAT partition on the stick, editable from Windows/macOS
  3. /etc/usb-router/usb-router.conf -- defaults baked into the image
  4. Built-in defaults below

Runtime settings changed from the web UI (homepage, kiosk mode, hotspot name)
are saved to STATE_DIR/settings.json and override the .conf values.
"""

import configparser
import json
import os
from pathlib import Path

DEFAULTS = {
    # -- what the screen shows
    "homepage": "https://duckduckgo.com",  # may contain {device_id} and {device_name}
    "kiosk": "true",                  # true = browser goes fullscreen with no toolbar once online
    "allow_exit": "false",            # false = hide the Quit button in the browser
    "reload_every": "0",              # minutes; 0 = never reload the page on a timer
    "rotation": "normal",             # normal | left | right | inverted (portrait screens)
    # -- identity (for the display network)
    "device_id": "",                  # blank = derived from the machine's hardware
    "device_name": "",                # e.g. "Lobby left"; shown in the fleet dashboard
    "fleet_url": "",                  # e.g. https://signage.example.com/fleet  (see fleet/)
    "fleet_token": "",                # shared secret sent as Authorization: Bearer
    "fleet_interval": "60",           # seconds between heartbeats
    # -- zero-touch Wi-Fi: profiles created at boot, connected automatically
    "wifi_ssid": "", "wifi_password": "",
    "wifi_ssid_2": "", "wifi_password_2": "",
    "wifi_ssid_3": "", "wifi_password_3": "",
    # -- phone-based setup when there is no keyboard: hotspot while offline
    "setup_hotspot": "true",
    "hotspot_enabled": "true",        # start sharing the uplink automatically once online
    "hotspot_ssid": "USB-Router",
    "hotspot_password": "changeme123",  # 8+ chars (WPA2)
    "hotspot_band": "bg",             # bg = 2.4 GHz (widest compatibility), a = 5 GHz
    "listen_host": "127.0.0.1",
    "listen_port": "8080",
    "lan_access": "true",             # serve the UI on the hotspot side too (phone-based setup)
    "admin_pin": "",                  # required for LAN access to admin actions when lan_access=true
    "check_url": "http://connectivitycheck.gstatic.com/generate_204",
    "check_fallback_url": "http://detectportal.firefox.com/success.txt",
    "mock": "false",                  # true = fake nmcli (for development on a laptop/CI)
}

CONF_PATHS = [
    "/etc/usb-router/usb-router.conf",
    "/config/usb-router.conf",
]

STATE_DIR = Path(os.environ.get("USB_ROUTER_STATE_DIR", "/var/lib/usb-router"))


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self):
        self.values = dict(DEFAULTS)
        self._load_files()
        self._load_env()
        self.state_dir = STATE_DIR
        self.settings_path = self.state_dir / "settings.json"
        self._load_settings()

    # -- loading -----------------------------------------------------------
    def _load_files(self):
        extra = os.environ.get("USB_ROUTER_CONF")
        paths = CONF_PATHS + ([extra] if extra else [])
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error:
                continue
            section = parser["usb-router"] if parser.has_section("usb-router") else parser["DEFAULT"]
            for key in DEFAULTS:
                if key in section:
                    self.values[key] = section[key]

    def _load_env(self):
        for key in DEFAULTS:
            env_key = "USB_ROUTER_" + key.upper()
            if env_key in os.environ:
                self.values[key] = os.environ[env_key]

    def _load_settings(self):
        try:
            if self.settings_path.exists():
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                for key, value in saved.items():
                    if key in DEFAULTS:
                        self.values[key] = str(value)
        except (OSError, ValueError):
            pass

    # -- saving ------------------------------------------------------------
    EDITABLE = ("homepage", "kiosk", "allow_exit", "hotspot_enabled",
                "hotspot_ssid", "hotspot_password", "hotspot_band",
                "reload_every", "rotation", "device_name", "fleet_url", "fleet_token",
                "fleet_interval", "setup_hotspot")

    def save(self, updates: dict) -> dict:
        changed = {}
        for key, value in updates.items():
            if key not in self.EDITABLE:
                continue
            value = str(value).strip()
            if key in ("kiosk", "allow_exit", "hotspot_enabled", "setup_hotspot"):
                value = "true" if _truthy(value) else "false"
            if key in ("reload_every", "fleet_interval"):
                if not value.isdigit():
                    raise ValueError(f"{key} must be a whole number")
                if key == "fleet_interval" and int(value) < 10:
                    value = "10"
            if key == "rotation" and value not in ("normal", "left", "right", "inverted"):
                raise ValueError("rotation must be normal, left, right or inverted")
            if key == "fleet_url" and value and not value.lower().startswith(("http://", "https://")):
                raise ValueError("fleet_url must start with http:// or https://")
            if key == "hotspot_password" and value and len(value) < 8:
                raise ValueError("Hotspot password must be at least 8 characters")
            if key == "hotspot_ssid" and not (1 <= len(value.encode()) <= 32):
                raise ValueError("Hotspot name must be 1-32 bytes")
            if key == "homepage" and not value.lower().startswith(("http://", "https://")):
                value = "https://" + value
            self.values[key] = value
            changed[key] = value
        if changed:
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                current = {}
                if self.settings_path.exists():
                    current = json.loads(self.settings_path.read_text(encoding="utf-8"))
                current.update(changed)
                self.settings_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
            except (OSError, ValueError):
                pass  # read-only media: settings live for this session only
        return changed

    # -- accessors ---------------------------------------------------------
    def get(self, key: str) -> str:
        return self.values[key]

    def flag(self, key: str) -> bool:
        return _truthy(self.values[key])

    # -- identity ----------------------------------------------------------
    def device_id(self) -> str:
        """Stable per-stick ID: configured, else from the machine-id / first MAC."""
        configured = self.get("device_id").strip()
        if configured:
            return configured
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                mid = Path(path).read_text().strip()
                if mid:
                    return "scr-" + mid[:8]
            except OSError:
                pass
        try:
            for iface in sorted(os.listdir("/sys/class/net")):
                if iface == "lo":
                    continue
                mac = Path(f"/sys/class/net/{iface}/address").read_text().strip().replace(":", "")
                if mac and mac != "000000000000":
                    return "scr-" + mac[-8:]
        except OSError:
            pass
        return "scr-unknown"

    def resolve_url(self, url: str | None = None) -> str:
        """Expand {device_id} / {device_name} placeholders in the homepage."""
        url = url if url is not None else self.get("homepage")
        name = self.get("device_name") or self.device_id()
        return url.replace("{device_id}", self.device_id()).replace("{device_name}", name)

    def provisioned_wifi(self):
        """[(ssid, password), ...] from the config file, for zero-touch setup."""
        out = []
        for suffix in ("", "_2", "_3"):
            ssid = self.get("wifi_ssid" + suffix).strip()
            if ssid:
                out.append((ssid, self.get("wifi_password" + suffix) or None))
        return out

    def public(self) -> dict:
        """Settings safe to show in the UI (no admin PIN, no fleet token)."""
        return {
            "homepage": self.get("homepage"),
            "resolved_homepage": self.resolve_url(),
            "device_id": self.device_id(),
            "device_name": self.get("device_name"),
            "reload_every": int(self.get("reload_every") or 0),
            "rotation": self.get("rotation"),
            "fleet_url": self.get("fleet_url"),
            "fleet_configured": bool(self.get("fleet_url")),
            "fleet_interval": int(self.get("fleet_interval") or 60),
            "setup_hotspot": self.flag("setup_hotspot"),
            "provisioned_wifi": [s for s, _ in self.provisioned_wifi()],
            "kiosk": self.flag("kiosk"),
            "allow_exit": self.flag("allow_exit"),
            "hotspot_enabled": self.flag("hotspot_enabled"),
            "hotspot_ssid": self.get("hotspot_ssid"),
            "hotspot_password": self.get("hotspot_password"),
            "hotspot_band": self.get("hotspot_band"),
            "lan_access": self.flag("lan_access"),
        }
