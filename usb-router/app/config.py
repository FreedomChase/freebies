"""
Configuration for the USB Router connection manager.

Precedence (highest first):
  1. Environment variables  (USB_ROUTER_*)
  2. /config/usb-router.conf      -- the FAT partition on the stick, editable from Windows/macOS
     /boot/firmware/usb-router.conf -- the same on the Raspberry Pi Zero build (its FAT boot partition)
  3. /etc/usb-router/usb-router.conf -- defaults baked into the image
  4. Built-in defaults below

Runtime settings changed from the web UI (homepage, kiosk mode, hotspot name)
are saved to STATE_DIR/settings.json and override the .conf values.
"""

import configparser
import json
import os
import secrets
import string
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
    "hotspot_enabled": "false",       # share the uplink once online (opt-in; the setup hotspot is separate)
    "hotspot_ssid": "USB-Router",
    "hotspot_password": "",           # blank = a unique random password is generated per stick at first boot
    "hotspot_band": "bg",             # bg = 2.4 GHz (widest compatibility), a = 5 GHz
    "listen_host": "127.0.0.1",
    "listen_port": "8080",
    "lan_access": "true",             # serve the UI on the hotspot side too (phone-based setup)
    "admin_pin": "",                  # blank = a random 6-digit PIN is generated per stick and shown on screen only
    "check_url": "http://connectivitycheck.gstatic.com/generate_204",
    "check_fallback_url": "http://detectportal.firefox.com/success.txt",
    "mock": "false",                  # true = fake nmcli (for development on a laptop/CI)
}

CONF_PATHS = [
    "/etc/usb-router/usb-router.conf",
    "/config/usb-router.conf",
    "/boot/firmware/usb-router.conf",   # Raspberry Pi Zero build (pizero/)
]

STATE_DIR = Path(os.environ.get("USB_ROUTER_STATE_DIR", "/var/lib/usb-router"))


# Passwords we refuse to run a hotspot with (old defaults, common guesses).
WEAK_PASSWORDS = {"changeme123", "changeme", "password", "password1", "12345678", "123456789",
                  "usbrouter", "usb-router", "qwertyuiop", "letmein123"}
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/O/1/l/i: typed from a phone


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_weak_password(value) -> bool:
    v = (value or "").strip()
    return len(v) < 8 or v.lower() in WEAK_PASSWORDS


def generate_password(length=12) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def generate_pin() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


class Config:
    def __init__(self):
        self.values = dict(DEFAULTS)
        self._load_files()
        self._load_env()
        self.state_dir = Path(os.environ.get("USB_ROUTER_STATE_DIR", str(STATE_DIR)))
        self.settings_path = self.state_dir / "settings.json"
        self.secrets_path = self.state_dir / "secrets.json"
        self._load_settings()
        self.secrets = {}
        self._load_secrets()
        self.ensure_secrets()

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

    # -- per-stick secrets (generated at first boot, never shipped) ---------
    def _load_secrets(self):
        try:
            if self.secrets_path.exists():
                data = json.loads(self.secrets_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.secrets = {k: str(v) for k, v in data.items()}
        except (OSError, ValueError):
            self.secrets = {}

    def _write_secrets(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.secrets_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.secrets), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.secrets_path)
            return True
        except OSError:
            return False  # read-only media: secrets live for this boot only

    def ensure_secrets(self):
        """Generate what the config does not provide: hotspot password and admin PIN."""
        changed = False
        if not self.secrets.get("hotspot_password") or is_weak_password(self.secrets.get("hotspot_password")):
            self.secrets["hotspot_password"] = generate_password()
            changed = True
        if not (self.secrets.get("admin_pin") or "").isdigit() or len(self.secrets.get("admin_pin", "")) < 6:
            self.secrets["admin_pin"] = generate_pin()
            changed = True
        if changed:
            self._write_secrets()

    def hotspot_password(self) -> str:
        """The configured password if it is acceptable, else the generated per-stick one."""
        configured = self.get("hotspot_password").strip()
        if configured and not is_weak_password(configured):
            return configured
        return self.secrets["hotspot_password"]

    def hotspot_password_source(self) -> str:
        configured = self.get("hotspot_password").strip()
        if configured and not is_weak_password(configured):
            return "config"
        return "generated" if not configured else "generated (configured value is too weak)"

    def admin_pin(self) -> str:
        configured = self.get("admin_pin").strip()
        return configured if configured else self.secrets["admin_pin"]

    def rotate_secret(self, name: str):
        if name == "hotspot_password":
            self.secrets[name] = generate_password()
        elif name == "admin_pin":
            self.secrets[name] = generate_pin()
        else:
            raise ValueError("unknown secret")
        self._write_secrets()
        return self.secrets[name]

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
            if key == "hotspot_password":
                if not value:
                    continue  # blank in a form means "keep the current one"
                if is_weak_password(value):
                    raise ValueError("Hotspot password must be at least 8 characters and not a common default")
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

    def public(self, local: bool = False) -> dict:
        """
        Settings for the UI. Secrets (hotspot password, admin PIN) are included
        only for the screen itself (local=True); never for a phone on the hotspot.
        The fleet token is never returned.
        """
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
            "hotspot_password": self.hotspot_password() if local else None,
            "hotspot_password_source": self.hotspot_password_source(),
            "hotspot_band": self.get("hotspot_band"),
            "lan_access": self.flag("lan_access"),
            "admin_pin": self.admin_pin() if local else None,
            "admin_pin_source": "config" if self.get("admin_pin").strip() else "generated",
        }
