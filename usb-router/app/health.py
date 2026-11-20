"""
C&G #270: is the screen rebooting, and is its power too weak?

Shaun's v8.8 screen looped black <-> plain blue on site and nothing on it said whether the Pi was
rebooting (a brown-out from a weak supply), the browser was restarting, or the HDMI was dropping.
This module gives the :8080 page three facts that tell those apart without SSH:

  uptime        seconds since the Pi itself booted (/proc/uptime), and since this app started
  boot count    how many times the Pi has started since the image was flashed, counted once per
                kernel boot_id in the state dir (no clock needed: a Pi Zero has none until NTP)
  power         the firmware's get_throttled bits: under-voltage / throttled now and since boot,
                plus the last boot on which under-voltage was ever seen (kept in the state file,
                because a brown-out that resets the Pi clears the firmware's "since boot" bits)

Where the power bits come from, checked in the shipped v8.8 image (kernel 6.12.115+rpt-rpi-v6):
  1. `vcgencmd get_throttled`: needs /dev/vchiq (0660, group video), so install-pizero.sh puts
     kiosk in video. The old /sys/.../soc:firmware/get_throttled file is NOT in this kernel.
  2. the rpi_volt hwmon device's in0_lcrit_alarm (raspberrypi-hwmon.ko, world-readable): now only;
     plus "Undervoltage detected!" in this boot's kernel log (kiosk is in systemd-journal) for "since boot".

Reads are a /proc read and one vcgencmd call (cached); the state file is written once per boot and when a new
under-voltage boot is first seen, so the SD card is not worn by polling.
"""

import glob
import json
import os
import subprocess
import time

BOOT_ID = "/proc/sys/kernel/random/boot_id"
UPTIME = "/proc/uptime"
HWMON = "/sys/class/hwmon/hwmon*"
UV_LOG = "Undervoltage detected!"     # raspberrypi-hwmon's kernel message (read from the v8.8 module)
STATE_NAME = "boots.json"

# get_throttled bits (Raspberry Pi firmware docs)
BITS = {
    0: "under_voltage_now",
    1: "freq_capped_now",
    2: "throttled_now",
    3: "soft_temp_limit_now",
    16: "under_voltage_since_boot",
    17: "freq_capped_since_boot",
    18: "throttled_since_boot",
    19: "soft_temp_limit_since_boot",
}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# -- uptime --------------------------------------------------------------------------------------
def system_uptime(path=UPTIME):
    """Seconds since the kernel booted, or None."""
    raw = _read(path)
    try:
        return int(float(raw.split()[0]))
    except (AttributeError, IndexError, ValueError):
        return None


# -- boot count ----------------------------------------------------------------------------------
def _load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("count"), int) else None
    except (OSError, ValueError):
        return None


def _save(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_start(state_dir, boot_id_path=None):
    """Called once when the app starts. A new kernel boot_id = one more boot; the same boot_id = the
    app itself restarted (systemd Restart=always). Returns the record, with saved=False if the state
    dir could not be written (the page then says the count is not kept)."""
    path = os.path.join(state_dir, STATE_NAME)
    boot_id = _read(boot_id_path or BOOT_ID)
    rec = _load(path)
    was_corrupt = rec is None and os.path.exists(path)
    if rec is None:
        rec = {"count": 0, "boot_id": None, "app_starts": 0, "uv_boot": None}
    if boot_id is None or rec.get("boot_id") != boot_id:
        rec["count"] += 1
        rec["boot_id"] = boot_id
        rec["app_starts"] = 1
    else:
        rec["app_starts"] = int(rec.get("app_starts") or 0) + 1
    if was_corrupt:
        rec["restarted_count"] = True   # the old file was unreadable: the count starts again here
    rec["saved"] = _save(path, {k: v for k, v in rec.items() if k != "saved"})
    return rec


def note_under_voltage(state_dir, rec):
    """Remember the boot on which under-voltage was seen, so it survives the brown-out reset."""
    if rec.get("uv_boot") == rec.get("count"):
        return rec
    rec["uv_boot"] = rec.get("count")
    rec["saved"] = _save(os.path.join(state_dir, STATE_NAME), {k: v for k, v in rec.items() if k != "saved"})
    return rec


# -- power ---------------------------------------------------------------------------------------
def decode_throttled(value):
    """int -> {flag: bool} for the bits the firmware documents."""
    return {name: bool(value >> bit & 1) for bit, name in BITS.items()}


def parse_throttled(text):
    """'throttled=0x50005' (vcgencmd) or '50005' / '0x50005' (bare hex) -> int, or None."""
    if not text:
        return None
    t = text.strip().split("=")[-1].strip()
    try:
        return int(t, 16)
    except ValueError:
        return None


def _hwmon_under_voltage(pattern=HWMON):
    """Fallback: the rpi_volt hwmon device's alarm (under-voltage NOW only), or None."""
    for d in sorted(glob.glob(pattern)):
        if _read(os.path.join(d, "name")) == "rpi_volt":
            v = _read(os.path.join(d, "in0_lcrit_alarm"))
            if v in ("0", "1"):
                return v == "1"
    return None


def _vcgencmd():
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_throttled(r.stdout) if r.returncode == 0 else None


def _kernel_log_under_voltage():
    """True if this boot's kernel log has the hwmon driver's under-voltage line, False if not, None if
    the journal cannot be read."""
    try:
        r = subprocess.run(["journalctl", "-k", "-b", "-q", "--no-pager", "-o", "cat", "--grep", UV_LOG],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return UV_LOG in r.stdout
    return False if r.returncode == 1 and not r.stderr.strip() else None   # 1 + no error = no match


def read_power(vcgencmd=_vcgencmd, hwmon=HWMON, kernel_log=_kernel_log_under_voltage):
    """{source, raw, flags...}: the firmware bits from vcgencmd; else the hwmon alarm (now) with the kernel
    log (since boot); else source=None (not a Raspberry Pi, or nothing readable)."""
    value = vcgencmd() if vcgencmd else None
    if value is not None:
        return {"source": "vcgencmd", "raw": hex(value), **decode_throttled(value)}
    uv = _hwmon_under_voltage(hwmon)
    if uv is None:
        return {"source": None, "raw": None}
    since = kernel_log() if kernel_log else None
    out = {"source": "hwmon", "raw": None, "under_voltage_now": uv}
    if since is not None:
        out["under_voltage_since_boot"] = bool(since or uv)
    return out


# -- the page's view -----------------------------------------------------------------------------
def report(rec, power, sys_up, app_started, now=None, browser_starts=None):
    """What /api/status carries as `health`, with `words` for the page (plain, no jargon)."""
    now = time.time() if now is None else now
    app_up = max(0, int(now - app_started))
    uv_now = bool(power.get("under_voltage_now"))
    uv_boot = bool(power.get("under_voltage_since_boot"))
    throttled = bool(power.get("throttled_now") or power.get("throttled_since_boot"))
    count = rec.get("count")
    if uv_now:
        power_words, warn = "Power too low right now: use a 5 V 2.5 A charger, not the TV's USB port.", True
    elif uv_boot:
        power_words, warn = "Power dipped too low since this start: use a 5 V 2.5 A charger, not the TV's USB port.", True
    elif rec.get("uv_boot") and rec.get("uv_boot") != count:
        power_words, warn = f"Power was too low on start {rec['uv_boot']}: check the charger.", True
    elif power.get("source") is None:
        power_words, warn = "Power check not available on this device.", False
    else:
        power_words, warn = "Power OK.", False
    if throttled and not (uv_now or uv_boot):
        power_words += " Slowed down for heat."
    parts = [f"On for {fmt_duration(sys_up)}" if sys_up is not None else "Time on not known"]
    if count:
        parts.append(f"start {count} since flashing" if rec.get("saved", True)
                     else f"start {count} (count not saved)")
    if (rec.get("app_starts") or 1) > 1:
        parts.append(f"settings page restarted {rec['app_starts'] - 1}x")
        warn = True
    if browser_starts and browser_starts > 1:
        parts.append(f"browser restarted {browser_starts - 1}x")
        warn = True
    return {
        "uptime": sys_up,
        "app_uptime": app_up,
        "boot_count": count,
        "boot_count_saved": rec.get("saved", True),
        "app_starts": rec.get("app_starts"),
        "browser_starts": browser_starts,
        "uv_boot": rec.get("uv_boot"),
        "power": power,
        "warn": warn,
        "words": " · ".join(parts) + ". " + power_words,
    }


def fmt_duration(s):
    s = int(s)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} days"
