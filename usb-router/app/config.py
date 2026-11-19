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
import re
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
    "hotspot_ssid": "Display Player",
    "hotspot_password": "",           # blank = a unique random password is generated per stick at first boot
    "hotspot_band": "bg",             # bg = 2.4 GHz (widest compatibility), a = 5 GHz
    "hotspot_backend": "auto",        # auto = hostapd when the image has it (the Pi, S&D #193), else NetworkManager
    "listen_host": "127.0.0.1",
    "listen_port": "8080",
    "lan_access": "true",             # serve the UI on the hotspot side too (phone-based setup)
    "captive_portal": "true",         # phones on the setup hotspot open the setup page by themselves (S&D #193)
    "admin_pin": "",                  # blank = the image's default PIN (DEFAULT_PIN_PATHS), else a random 6-digit PIN per stick
    "check_url": "http://connectivitycheck.gstatic.com/generate_204",
    "check_fallback_url": "http://detectportal.firefox.com/success.txt",
    "mock": "false",                  # true = fake nmcli (for development on a laptop/CI)
}

# One default PIN for every box, written at image build from the node vault
# (DISPLAY_PLAYER_DEFAULT_PIN, S&D #193). Never in git. A per-box New PIN overrides it.
# names the settings page uses for a key, for the "saved here beats the file" line
OVERRIDE_LABELS = {"homepage": "Content URL", "hotspot_ssid": "Hotspot name", "hotspot_password": "Hotspot password",
                   "reload_every": "Reload content every", "device_name": "Screen name", "fleet_url": "Dashboard URL"}

DEFAULT_PIN_PATHS = ["/etc/usb-router/default-pin"]

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


def _unquote(value) -> str:
    """C&G #254: 'wifi_password = "secret"' in usb-router.conf means secret, not the quotes too."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def generate_pin() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


_OPTION_LINE = re.compile(r"(?P<key>.*?)\s*(?P<vi>[=:])\s*(?P<value>.*)$")


def _scan_lines(text):
    """P&T #1527/#1528: what configparser does not tell us, line by line, read the way it reads
    them. Returns a list of (line number, section, key, kind) for every setting line, kind:
      "set"      key = value
      "url"      a web address alone on a line ("https://..." parses as a key called https)
      "joined"   an indented line, which configparser adds to the value of the setting above;
                 key is that setting and the line number is the indented one
    Lines with no = or : are not listed (configparser reports those itself)."""
    out, section, current = [], "usb-router", None
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue                                    # blank and comment lines keep a value open
        if line[0] in " \t" and current is not None:
            out.append((n, section, current, "joined"))
            continue
        current = None
        if stripped.startswith("["):
            section = stripped.strip("[]").strip().lower()
            continue
        m = _OPTION_LINE.match(stripped)
        if not m or not m.group("key").strip():
            continue
        key = m.group("key").strip().lower()
        if key in ("http", "https") and m.group("vi") == ":" and m.group("value").startswith("//"):
            out.append((n, section, key, "url"))
        else:
            out.append((n, section, key, "set"))
        current = key
    return out


def _lines(numbers):
    numbers = list(numbers)
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(str(n) for n in numbers)


def _read_conf(path):
    """C&G #254 v8.7: read usb-router.conf the way people actually save it on a laptop.

    utf-8-sig: Windows Notepad may write a BOM, which made the whole file unreadable.
    interpolation=None: a '%' in a Wi-Fi password is just a character.
    A file with no [usb-router] header is read as if it had one.
    cp1252 fallback: an old Notepad 'ANSI' save with an accented character.
    P&T #1527: strict=False, so a setting written twice is read (the LAST one wins) instead of
    the whole file being dropped, and a line that is not a setting is skipped, not fatal.

    Returns (parser or None, problems). Problems are plain sentences for the setup screen,
    most important first (the TV shows only the first). P&T #1528: they name a line by its
    number and a setting only when it is one the player knows; nothing else typed in the file
    is ever repeated, because the file holds Wi-Fi passwords and a password pasted on the
    wrong line would otherwise be shown on the TV.
    """
    name = os.path.basename(path)
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None, [(0, f"{name} could not be opened, so none of its settings are used.")]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None, [(0, f"{name} could not be read (unknown text encoding), so none of its settings are used.")]
    bad_lines = []
    for body, offset in ((text, 0), ("[usb-router]\n" + text, 1)):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(body, source=path)
        except configparser.MissingSectionHeaderError:
            continue
        except configparser.ParsingError as e:
            bad_lines = [n - offset for n, _ in e.errors]   # the good lines are still in the parser
        except configparser.Error:
            return None, [(0, f"{name} could not be read, so none of its settings are used.")]
        break
    else:
        return None, [(0, f"{name} could not be read, so none of its settings are used.")]

    scan = [entry for entry in _scan_lines(text) if entry[1] == "usb-router"]
    ranked = []   # (rank, first line, sentence): rank 0 changes a setting the player uses
    by_key = {}
    for n, _, key, kind in scan:
        if kind == "set" and key in DEFAULTS:
            by_key.setdefault(key, []).append(n)
    for key, lines in by_key.items():
        if len(lines) > 1:
            ranked.append((0, lines[0], f"{name}: {key} is set {len(lines)} times ({_lines(lines)}); "
                                        f"the last one, line {lines[-1]}, is used."))
    joined = {}
    for n, _, key, kind in scan:
        if kind == "joined" and key in DEFAULTS:
            joined.setdefault(key, []).append(n)
    for key, lines in joined.items():
        one = len(lines) == 1
        ranked.append((1, lines[0], f"{name}, {_lines(lines)}: {'starts' if one else 'start'} with a space, "
                                    f"so {'it is' if one else 'they are'} added to {key} above it and {key} "
                                    f"is wrong. Remove the space at the start."))
    urls = [n for n, _, _, kind in scan if kind == "url"]
    if urls:
        ranked.append((2, urls[0], f"{name}, {_lines(urls)}: a web address on its own, not a setting, so it "
                                   f"is ignored. To show it, write homepage = in front of it."))
    if bad_lines:
        ranked.append((2, bad_lines[0], f"{name}, {_lines(bad_lines)}: not a setting (no = sign), so it was skipped."))
    unknown = sorted({n for n, _, key, kind in scan if kind == "set" and key not in DEFAULTS})
    if unknown:
        ranked.append((3, unknown[0], f"{name}, {_lines(unknown)}: not a setting the player knows, so it "
                                      f"is ignored. Check the spelling of the name before the = sign."))
    return parser, [(rank, sentence) for rank, _, sentence in sorted(ranked)]


class Config:
    def __init__(self):
        self.values = dict(DEFAULTS)
        self.problems = []        # P&T #1527: shown on the setup screen and the settings page
        self.file_values = {}     # what usb-router.conf set, to tell when a saved setting beats it
        self._load_files()
        self._load_env()
        self.state_dir = Path(os.environ.get("USB_ROUTER_STATE_DIR", str(STATE_DIR)))
        self.settings_path = self.state_dir / "settings.json"
        self.secrets_path = self.state_dir / "secrets.json"
        self._load_settings()
        self._migrate()
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
            parser, problems = _read_conf(path)
            self.problems += problems
            if parser is None:
                continue
            section = parser["usb-router"] if parser.has_section("usb-router") else parser["DEFAULT"]
            for key in DEFAULTS:
                if key in section:
                    self.values[key] = section[key]
                    self.file_values[key] = section[key]

    def _load_env(self):
        for key in DEFAULTS:
            env_key = "USB_ROUTER_" + key.upper()
            if env_key in os.environ:
                self.values[key] = os.environ[env_key]

    # C&G #209 v8.5: the hotspot was called USB-Router until 17 Nov (Shaun: "we can change to
    # Display Player"). A conf or saved setting that still holds that old default is read as the
    # new name, in memory only, so no file on an installed screen has to be edited. Any other
    # name someone chose is kept.
    OLD_SSIDS = ("USB-Router",)

    def _migrate(self):
        if self.values.get("hotspot_ssid", "").strip() in self.OLD_SSIDS:
            self.values["hotspot_ssid"] = DEFAULTS["hotspot_ssid"]

    def _load_settings(self):
        self.saved_keys = set()
        try:
            if self.settings_path.exists():
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                for key, value in saved.items():
                    if key in DEFAULTS:
                        self.values[key] = str(value)
                        self.saved_keys.add(key)
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

    def default_pin(self) -> str:
        """The image's shared default PIN, or "" when the image has none."""
        extra = os.environ.get("USB_ROUTER_DEFAULT_PIN_FILE")
        for path in ([extra] if extra else []) + DEFAULT_PIN_PATHS:
            try:
                pin = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if pin.isdigit() and len(pin) >= 6:
                return pin
        return ""

    def admin_pin_source(self) -> str:
        """config (usb-router.conf) > new (New PIN on this box) > default (image) > generated."""
        if self.get("admin_pin").strip():
            return "config"
        if self.secrets.get("admin_pin_new") == "1":
            return "new"
        if self.default_pin():
            return "default"
        return "generated"

    def admin_pin(self) -> str:
        source = self.admin_pin_source()
        if source == "config":
            return self.get("admin_pin").strip()
        if source == "default":
            return self.default_pin()
        return self.secrets["admin_pin"]

    def rotate_secret(self, name: str):
        if name == "hotspot_password":
            self.secrets[name] = generate_password()
        elif name == "admin_pin":
            self.secrets[name] = generate_pin()
            self.secrets["admin_pin_new"] = "1"   # this box now has its own PIN, not the default
        else:
            raise ValueError("unknown secret")
        self._write_secrets()
        # a value from usb-router.conf or an earlier save outranks the generated one, so
        # without this the new password/PIN never took effect (C&G #208)
        if self.values.get(name, "").strip():
            self.values[name] = ""
            self._persist({name: ""})
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
            self._persist(changed)
        return changed

    def _persist(self, changed: dict):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            current = {}
            if self.settings_path.exists():
                current = json.loads(self.settings_path.read_text(encoding="utf-8"))
            current.update(changed)
            self.settings_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass  # read-only media: settings live for this session only

    def conf_problems(self) -> list:
        """P&T #1527: what is wrong with usb-router.conf, plus each setting in it that a Save on
        this screen now overrides (C&G #254: the file said one homepage, the screen showed another)."""
        ranked = list(self.problems)   # (rank, sentence); the TV shows only the first (P&T #1528)
        for key in sorted(self.saved_keys):
            if key in self.file_values and _unquote(self.file_values[key]) != _unquote(self.values[key]):
                label = OVERRIDE_LABELS.get(key, key)
                ranked.append((0.5, f"{label} was saved on this screen, so {key} in usb-router.conf is not "
                                    f"used. Change it here: editing the file will not change it."))
        return [sentence for _, sentence in sorted(ranked, key=lambda r: r[0])]

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
            ssid = _unquote(self.get("wifi_ssid" + suffix))
            if ssid:
                out.append((ssid, _unquote(self.get("wifi_password" + suffix)) or None))
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
            # to the screen's own page only, which sends it as X-Admin-Pin (a page in the kiosk browser cannot
            # read this cross-origin). The page does not DISPLAY it when the source is "default" (S&D #193).
            "admin_pin": self.admin_pin() if local else None,
            "admin_pin_source": self.admin_pin_source(),
            "conf_problems": self.conf_problems(),
        }
