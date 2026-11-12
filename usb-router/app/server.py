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

Run on a laptop for development:
  USB_ROUTER_MOCK=1 python3 server.py
"""

import functools
import ipaddress
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, abort, jsonify, render_template, request  # noqa: E402

from config import Config  # noqa: E402
from netman import NetError, get_backend, internet_check  # noqa: E402
from router import Router  # noqa: E402

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
    "browser_seen_online": False,
}
_state_lock = threading.Lock()


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


# --------------------------------------------------------------------------
# Access control: the UI is for the local screen; optional PIN on the LAN side
# --------------------------------------------------------------------------
def _is_local():
    try:
        return ipaddress.ip_address(request.remote_addr).is_loopback
    except ValueError:
        return False


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if _is_local():
            return fn(*a, **kw)
        if not cfg.flag("lan_access"):
            abort(403)
        pin = cfg.get("admin_pin")
        if pin and request.headers.get("X-Admin-Pin", "") != pin:
            return jsonify({"ok": False, "error": "Admin PIN required"}), 401
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
    return render_template("index.html", settings=cfg.public(), mock=MOCK)


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
        "hotspot": router.status(),
        "settings": cfg.public(),
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
        return jsonify({"ok": True, "hotspot": router.status(), "candidates": router.candidates()})
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
    return jsonify({"ok": True, "hotspot": status})


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@app.get("/api/settings")
def api_settings():
    return jsonify({"ok": True, "settings": cfg.public()})


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
    if "hotspot_enabled" in changed:
        if changed["hotspot_enabled"] == "true":
            router.auto_disabled = False
        else:
            try:
                router.stop()
            except NetError:
                pass
    return jsonify({"ok": True, "changed": changed, "settings": cfg.public()})


# --------------------------------------------------------------------------
# Browser hand-off
# --------------------------------------------------------------------------
@app.post("/api/browser/go")
@admin_required
def api_browser_go():
    """The connect page calls this when the user clicks 'Start browsing'."""
    url = (body().get("url") or cfg.get("homepage")).strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    with _state_lock:
        _state["browser_target"] = url
    return jsonify({"ok": True, "url": url})


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
        _state["browser_target"] = None
    return jsonify({
        "ok": True,
        "online": check["online"],
        "captive_portal": check["captive_portal"],
        "portal_url": check["portal_url"],
        "navigate": target,
        "homepage": cfg.get("homepage"),
        "kiosk": cfg.flag("kiosk"),
        "allow_exit": cfg.flag("allow_exit"),
    })


# --------------------------------------------------------------------------
def main():
    host = cfg.get("listen_host")
    if cfg.flag("lan_access"):
        host = "0.0.0.0"
    port = int(cfg.get("listen_port"))
    router.run_keeper(is_online)
    log.info("USB Router connection manager on http://%s:%s (backend=%s)", host, port, nm.name)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
