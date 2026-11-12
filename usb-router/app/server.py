#!/usr/bin/env python3
"""
USB Router connection manager — the web page the stick shows at boot.

  GET  /                    the UI (served inside the custom browser)
  GET  /api/status          everything the UI needs, in one call
  GET  /api/wifi/scan       nearby networks
  POST /api/wifi/connect    {ssid, password}
  POST /api/wifi/forget     {ssid}
  POST /api/disconnect      {iface}
  POST /api/ethernet        {iface}      retry a wired connection
  GET  /api/check           live internet / captive-portal check
  GET  /api/hotspot         hotspot status + candidate adapters
  POST /api/hotspot         {action: start|stop|restart, iface?, mode?}
  GET  /api/settings        homepage, kiosk, hotspot settings
  POST /api/settings        update any of the above
  POST /api/browser/go      {url?}  tells the browser to leave the connect screen
  GET  /api/browser/poll    the browser asks whether it should navigate somewhere
  POST /api/browser/report  {url}   the browser says what it is showing (for the fleet)
  GET  /api/qr?kind=wifi|ui PNG QR code for the setup hotspot / setup page
  POST /api/system          {action: reload|restart-browser|reboot|shutdown}
  GET  /api/display         rotation etc. for the X session

Run on a laptop for development:
  USB_ROUTER_MOCK=1 python3 server.py
"""

import functools
import hmac
import io
import ipaddress
import logging
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, abort, jsonify, render_template, request, send_file  # noqa: E402

from config import Config  # noqa: E402
from fleet_client import FleetClient  # noqa: E402
from netman import NetError, get_backend, internet_check  # noqa: E402
from router import Router  # noqa: E402

try:
    import qrcode  # python3-qrcode on the image
except ImportError:  # pragma: no cover
    qrcode = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("usb-router")

cfg = Config()
MOCK = cfg.flag("mock") or os.environ.get("USB_ROUTER_MOCK") == "1"
nm = get_backend(MOCK)
router = Router(nm, cfg)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False

# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------
_state = {
    "last_check": {"online": False, "captive_portal": False, "portal_url": None, "verdict": "unknown", "at": 0},
    "browser_target": None,     # URL the browser should navigate to, consumed once
    "browser_action": None,     # reload | restart, consumed once
    "current_url": None,        # what the browser last reported showing
    "provisioned": [],          # Wi-Fi profiles created from the config at startup
}
_state_lock = threading.Lock()


def provision_wifi():
    """Zero-touch: create auto-connect profiles for the Wi-Fi networks in the config."""
    created = []
    for prio, (ssid, password) in enumerate(reversed(cfg.provisioned_wifi())):
        try:
            if nm.ensure_profile(ssid, password, priority=prio + 10):
                created.append(ssid)
        except NetError as e:
            log.warning("could not provision %s: %s", ssid, e)
    with _state_lock:
        _state["provisioned"] = created
    if created:
        log.info("provisioned Wi-Fi profiles: %s", ", ".join(created))


def run_check(force=False):
    with _state_lock:
        fresh = time.time() - _state["last_check"]["at"] < 4
        if fresh and not force:
            return dict(_state["last_check"])
    result = internet_check(cfg.get("check_url"), cfg.get("check_fallback_url"),
                            mock_backend=nm if MOCK else None)
    result["at"] = time.time()
    with _state_lock:
        _state["last_check"] = result
    return dict(result)


def is_online():
    return run_check()["online"]


def system_action(action):
    """Used by the UI, the API and the fleet dashboard."""
    if action in ("reload", "restart-browser"):
        with _state_lock:
            _state["browser_action"] = "reload" if action == "reload" else "restart"
        return
    if action in ("reboot", "shutdown"):
        if MOCK:
            log.info("mock: would %s", action)
            return
        cmd = ["systemctl", "reboot" if action == "reboot" else "poweroff"]
        threading.Timer(1.0, lambda: subprocess.run(cmd, check=False)).start()
        return
    raise ValueError(f"unknown action {action!r}")


def fleet_status():
    check = run_check()
    try:
        uplink = router.uplink()
    except NetError:
        uplink = None
    with _state_lock:
        current = _state["current_url"]
    return {"online": check["online"], "uplink": uplink, "hotspot": router.status(check["online"]),
            "current_url": current}


fleet = FleetClient(cfg, fleet_status, system_action)
# --------------------------------------------------------------------------
# Access control: the UI is for the local screen; optional PIN on the LAN side
# --------------------------------------------------------------------------
def _is_local():
    try:
        return ipaddress.ip_address(request.remote_addr).is_loopback
    except ValueError:
        return False


def host_allowed(host):
    """
    DNS rebinding guard (S&D #183). The kiosk browser shows third-party pages; one of them can
    re-resolve its own domain to 127.0.0.1 and then read this UI (which shows the admin PIN to the
    screen) as same-origin. Its requests come from 127.0.0.1, so only the Host header still names
    the attacker. Every real client reaches this server by an IP address (127.0.0.1 for the screen,
    10.42.0.1 for phones on the hotspot, the LAN address with lan_access) or by localhost; a
    rebinding attack needs a DNS name. So: allow localhost and IP literals, refuse every other name.
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):                     # [IPv6] or [IPv6]:port
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@app.before_request
def refuse_foreign_host():
    if not host_allowed(request.headers.get("Host")):
        log.warning("refused request for Host %r from %s (DNS rebinding guard)",
                    (request.headers.get("Host") or "")[:100], request.remote_addr)
        abort(403)


# Brute-force protection for the PIN: per-address failure count with escalating lockout.
_pin_failures = {}
_pin_lock = threading.Lock()
PIN_FREE_ATTEMPTS = 5


def _pin_locked(addr):
    with _pin_lock:
        count, until = _pin_failures.get(addr, (0, 0))
        return max(0, int(until - time.time()))


def _pin_failed(addr):
    with _pin_lock:
        count, _ = _pin_failures.get(addr, (0, 0))
        count += 1
        over = count - PIN_FREE_ATTEMPTS  # 6th wrong PIN locks for 30 s, then 60, 120, ... up to 15 min
        lock = min(900, 30 * 2 ** (over - 1)) if over >= 1 else 0
        _pin_failures[addr] = (count, time.time() + lock)
        return lock


def _pin_ok(addr):
    with _pin_lock:
        _pin_failures.pop(addr, None)


def pin_valid(given):
    return bool(given) and hmac.compare_digest(str(given), cfg.admin_pin())


def admin_required(fn):
    """
    Every change needs the admin PIN (header X-Admin-Pin), from the screen itself
    and from any phone on the hotspot alike. The PIN is shown only on the physical
    screen's page; the page on the screen fills it in for its own requests.
    """
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not _is_local() and not cfg.flag("lan_access"):
            abort(403)
        addr = request.remote_addr or "?"
        wait = _pin_locked(addr)
        if wait:
            return jsonify({"ok": False, "error": f"Too many wrong PINs; try again in {wait} s"}), 429
        given = request.headers.get("X-Admin-Pin", "")
        if not pin_valid(given):
            lock = _pin_failed(addr) if given else 0
            msg = "Admin PIN required (it is shown on the screen)" if not given else "Wrong admin PIN"
            if lock:
                msg += f"; locked for {lock} s"
            return jsonify({"ok": False, "error": msg, "pin_required": True}), 401
        _pin_ok(addr)
        return fn(*a, **kw)
    return wrapper


def body():
    return request.get_json(silent=True) or request.form.to_dict() or {}


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", settings=cfg.public(local=_is_local()), mock=MOCK)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "backend": nm.name})


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    try:
        devices = nm.devices()
    except NetError as e:
        return fail(e, 500)
    check = run_check(force=request.args.get("force") == "1")
    uplink = router.uplink(devices)
    return jsonify({
        "ok": True,
        "online": check["online"],
        "captive_portal": check["captive_portal"],
        "portal_url": check["portal_url"],
        "verdict": check["verdict"],
        "uplink": uplink,
        "devices": devices,
        "hotspot": router.status(check["online"], local=_is_local()),
        "settings": cfg.public(local=_is_local()),
        "local": _is_local(),
        "device": {"id": cfg.device_id(), "name": cfg.get("device_name"), "provisioned": _state["provisioned"]},
        "fleet": fleet.info(),
        "current_url": _state["current_url"],
        "backend": nm.name,
    })


@app.get("/api/check")
def api_check():
    return jsonify({"ok": True, **run_check(force=True)})


# --------------------------------------------------------------------------
# Wi-Fi
# --------------------------------------------------------------------------
@app.get("/api/wifi/scan")
def api_wifi_scan():
    try:
        nets = nm.wifi_scan(rescan=request.args.get("rescan", "1") == "1")
        saved = {c["name"] for c in nm.saved_connections()}
        for n in nets:
            n["saved"] = n["ssid"] in saved
        return jsonify({"ok": True, "networks": nets})
    except NetError as e:
        return fail(e, 500)


@app.post("/api/wifi/connect")
@admin_required
def api_wifi_connect():
    data = body()
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password") or None
    if not ssid:
        return fail("Choose a network")
    if password and len(password) < 8:
        return fail("Wi-Fi passwords are at least 8 characters")
    try:
        nm.wifi_connect(ssid, password, data.get("iface"))
    except NetError as e:
        return fail(e, 502)
    check = run_check(force=True)
    return jsonify({"ok": True, "ssid": ssid, **check})


@app.post("/api/wifi/forget")
@admin_required
def api_wifi_forget():
    ssid = (body().get("ssid") or "").strip()
    if not ssid:
        return fail("No network given")
    try:
        nm.wifi_forget(ssid)
    except NetError as e:
        return fail(e, 502)
    return jsonify({"ok": True})


@app.post("/api/disconnect")
@admin_required
def api_disconnect():
    iface = (body().get("iface") or "").strip()
    if not iface:
        return fail("No interface given")
    try:
        nm.disconnect(iface)
    except NetError as e:
        return fail(e, 502)
    return jsonify({"ok": True})


@app.post("/api/ethernet")
@admin_required
def api_ethernet():
    iface = (body().get("iface") or "").strip()
    if not iface:
        return fail("No interface given")
    try:
        nm.ethernet_connect(iface)
    except NetError as e:
        return fail(e, 502)
    return jsonify({"ok": True, **run_check(force=True)})


# --------------------------------------------------------------------------
# Hotspot / router
# --------------------------------------------------------------------------
@app.get("/api/hotspot")
def api_hotspot():
    try:
        return jsonify({"ok": True, "hotspot": router.status(run_check()["online"], local=_is_local()),
                        "candidates": router.candidates()})
    except NetError as e:
        return fail(e, 500)


@app.post("/api/hotspot")
@admin_required
def api_hotspot_action():
    data = body()
    action = data.get("action", "start")
    try:
        if action == "start":
            status = router.start(data.get("iface") or None, data.get("mode") or None)
        elif action == "stop":
            status = router.stop()
        elif action == "restart":
            status = router.restart()
        else:
            return fail("Unknown action")
    except NetError as e:
        return fail(e, 502)
    return jsonify({"ok": True, "hotspot": router.status(run_check()["online"], local=_is_local())})


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@app.get("/api/settings")
def api_settings():
    return jsonify({"ok": True, "settings": cfg.public(local=_is_local())})


@app.post("/api/settings")
@admin_required
def api_settings_update():
    try:
        changed = cfg.save(body())
    except ValueError as e:
        return fail(e)
    hotspot_keys = {"hotspot_ssid", "hotspot_password", "hotspot_band"}
    if hotspot_keys & set(changed) and router.status()["active"]:
        try:
            router.restart()
        except NetError as e:
            log.warning("hotspot restart after settings change failed: %s", e)
    if "hotspot_enabled" in changed or "setup_hotspot" in changed:
        if changed.get("hotspot_enabled") == "true" or changed.get("setup_hotspot") == "true":
            router.auto_disabled = False
        elif changed.get("hotspot_enabled") == "false":
            try:
                router.stop()
            except NetError:
                pass
    if "rotation" in changed:
        apply_rotation(changed["rotation"])
    if "fleet_url" in changed or "fleet_token" in changed:
        fleet.reset()
    return jsonify({"ok": True, "changed": changed, "settings": cfg.public(local=_is_local())})


def apply_rotation(rotation):
    """Rotate the live X screen (the session script also applies it at start)."""
    if MOCK or not os.environ.get("DISPLAY", "") and not os.path.exists("/tmp/.X11-unix/X0"):
        return
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":0"))
    try:
        subprocess.run(["xrandr", "--output", primary_output(env), "--rotate", rotation],
                       env=env, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def primary_output(env):
    try:
        out = subprocess.run(["xrandr", "--query"], env=env, capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if " connected" in line:
                return line.split()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "HDMI-1"


@app.get("/api/display")
def api_display():
    return jsonify({"ok": True, "rotation": cfg.get("rotation"), "device_id": cfg.device_id()})


# --------------------------------------------------------------------------
# System actions (local screen, fleet dashboard, or LAN with PIN)
# --------------------------------------------------------------------------
@app.post("/api/system")
@admin_required
def api_system():
    action = (body().get("action") or "").strip()
    try:
        system_action(action)
    except ValueError as e:
        return fail(e)
    return jsonify({"ok": True, "action": action})


@app.post("/api/fleet/ping")
@admin_required
def api_fleet_ping():
    """Send a heartbeat now (used by the Save button next to the fleet URL)."""
    if not cfg.get("fleet_url"):
        return fail("No fleet URL configured")
    if not cfg.get("fleet_token"):
        return fail("A fleet token is required before the screen will talk to a dashboard")
    result = fleet.heartbeat()
    if result is None:
        return fail(f"Dashboard unreachable: {fleet.last_error}", 502)
    return jsonify({"ok": True, **result, "fleet": fleet.info()})


# --------------------------------------------------------------------------
# Phone setup window and secret rotation
# --------------------------------------------------------------------------
@app.post("/api/setup-mode")
def api_setup_mode():
    """
    Re-open phone setup from the screen itself (Ctrl+N in the browser, or the
    button on the local page). Loopback only: it needs someone at the screen.
    The hotspot only actually starts while the screen is offline.
    """
    if not _is_local():
        abort(403)
    action = (body().get("action") or "open").strip()
    if action == "close":
        router.setup_until = 0.0
        if router.role == "setup":
            router.stop(manual=False)
    else:
        router.open_setup_window()
    return jsonify({"ok": True, "hotspot": router.status(run_check()["online"], local=True)})


@app.post("/api/secrets/rotate")
@admin_required
def api_secrets_rotate():
    """New random hotspot password or admin PIN (shown on the screen afterwards)."""
    name = (body().get("name") or "").strip()
    try:
        cfg.rotate_secret(name)
    except ValueError as e:
        return fail(e)
    if name == "hotspot_password" and router.status()["active"]:
        try:
            router.restart()
        except NetError as e:
            log.warning("hotspot restart after password rotation failed: %s", e)
    return jsonify({"ok": True, "settings": cfg.public(local=_is_local())})


# --------------------------------------------------------------------------
# QR codes for phone-based setup
# --------------------------------------------------------------------------
@app.get("/api/qr")
def api_qr():
    kind = request.args.get("kind", "wifi")
    if kind == "wifi":
        if not _is_local():
            abort(403)  # the QR codes carry the hotspot password: screen only
        ssid, pw = cfg.get("hotspot_ssid"), cfg.hotspot_password()
        def esc(v):  # WIFI: QR format escapes \ ; , :
            return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace(":", "\\:")
        data = f"WIFI:T:{'WPA' if pw else 'nopass'};S:{esc(ssid)};" + (f"P:{esc(pw)};" if pw else "") + ";"
    elif kind == "ui":
        if not _is_local():
            abort(403)
        data = router.status().get("ui_url") or f"http://10.42.0.1:{cfg.get('listen_port')}"
    else:
        return fail("kind must be wifi or ui")
    if qrcode is None:
        return fail("qrcode library not installed", 501)
    img = qrcode.make(data, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=30)


# --------------------------------------------------------------------------
# Browser hand-off
# --------------------------------------------------------------------------
@app.post("/api/browser/go")
@admin_required
def api_browser_go():
    """The connect page calls this when the user clicks 'Start browsing'."""
    url = (body().get("url") or cfg.resolve_url()).strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    with _state_lock:
        _state["browser_target"] = url
    return jsonify({"ok": True, "url": url})


@app.post("/api/browser/report")
def api_browser_report():
    if not _is_local():
        abort(403)
    with _state_lock:
        _state["current_url"] = (body().get("url") or "")[:2000] or None
    return jsonify({"ok": True})


@app.get("/api/browser/poll")
def api_browser_poll():
    """
    The custom browser polls this while showing the connect screen.
    Returns the URL to navigate to (once), plus whether we are online so the
    browser can unlock its toolbar.
    """
    check = run_check()
    with _state_lock:
        target = _state["browser_target"]
        action = _state["browser_action"]
        _state["browser_target"] = None
        _state["browser_action"] = None
    return jsonify({
        "ok": True,
        "online": check["online"],
        "captive_portal": check["captive_portal"],
        "portal_url": check["portal_url"],
        "navigate": target,
        "action": action,
        "homepage": cfg.resolve_url(),
        "kiosk": cfg.flag("kiosk"),
        "allow_exit": cfg.flag("allow_exit"),
        "reload_every": int(cfg.get("reload_every") or 0),
        "device_id": cfg.device_id(),
    })


# --------------------------------------------------------------------------
def main():
    host = cfg.get("listen_host")
    if cfg.flag("lan_access"):
        host = "0.0.0.0"
    port = int(cfg.get("listen_port"))
    provision_wifi()
    router.run_keeper(is_online)
    fleet.run()
    log.info("device %s; hotspot password: %s; admin PIN: %s (both shown on the screen's own page)",
             cfg.device_id(), cfg.hotspot_password_source(), "from config" if cfg.get("admin_pin").strip() else "generated")
    log.info("USB Router connection manager on http://%s:%s (backend=%s)", host, port, nm.name)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
