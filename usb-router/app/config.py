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
    "homepage": "https://duckduckgo.com",
    "kiosk": "false",                 # true = browser goes fullscreen with no toolbar once online
    "allow_exit": "true",             # false = hide the Quit button in the browser
    "hotspot_enabled": "true",        # start sharing the uplink automatically once online
    "hotspot_ssid": "USB-Router",
    "hotspot_password": "changeme123",  # 8+ chars (WPA2)
    "hotspot_band": "bg",             # bg = 2.4 GHz (widest compatibility), a = 5 GHz
    "listen_host": "127.0.0.1",
    "listen_port": "8080",
    "lan_access": "false",            # true = also serve the UI on the hotspot LAN side
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
                "hotspot_ssid", "hotspot_password", "hotspot_band")

    def save(self, updates: dict) -> dict:
        changed = {}
        for key, value in updates.items():
            if key not in self.EDITABLE:
                continue
            value = str(value).strip()
            if key in ("kiosk", "allow_exit", "hotspot_enabled"):
                value = "true" if _truthy(value) else "false"
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

    def public(self) -> dict:
        """Settings safe to show in the UI (no admin PIN)."""
        return {
            "homepage": self.get("homepage"),
            "kiosk": self.flag("kiosk"),
            "allow_exit": self.flag("allow_exit"),
            "hotspot_enabled": self.flag("hotspot_enabled"),
            "hotspot_ssid": self.get("hotspot_ssid"),
            "hotspot_password": self.get("hotspot_password"),
            "hotspot_band": self.get("hotspot_band"),
            "lan_access": self.flag("lan_access"),
        }
