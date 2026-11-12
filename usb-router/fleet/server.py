#!/usr/bin/env python3
"""
Fleet dashboard — one page that shows every screen in the display network and
lets you change what each one shows.

Screens POST a heartbeat to /api/heartbeat (see app/fleet_client.py). The reply
carries any settings you changed here, so a screen picks up a new content URL,
rotation, or a reboot request within one heartbeat interval.

    FLEET_TOKEN=supersecret FLEET_ADMIN_PASSWORD=letmein python3 fleet/server.py
    -> dashboard on http://0.0.0.0:4010/   (put it behind HTTPS in production)

Set the same FLEET_TOKEN in usb-router.conf (fleet_token) on every stick and
fleet_url = https://your-host/  (no trailing path needed).
"""

import hmac
import json
import os
import sqlite3
import time
from functools import wraps

from flask import Flask, Response, abort, g, jsonify, render_template, request

PORT = int(os.environ.get("FLEET_PORT", 4010))
TOKEN = os.environ.get("FLEET_TOKEN", "")                  # screens -> dashboard
ADMIN_PASSWORD = os.environ.get("FLEET_ADMIN_PASSWORD", "")  # people -> dashboard (HTTP basic)
DB_PATH = os.environ.get("FLEET_DB_PATH", os.path.join(os.path.dirname(__file__), "fleet.db"))
OFFLINE_AFTER = int(os.environ.get("FLEET_OFFLINE_AFTER", 180))  # seconds without heartbeat

app = Flask(__name__, template_folder="templates")
EDITABLE = ("name", "homepage", "kiosk", "reload_every", "rotation", "allow_exit", "group_name", "notes")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("""CREATE TABLE IF NOT EXISTS screens (
            device_id TEXT PRIMARY KEY,
            name TEXT, group_name TEXT, notes TEXT,
            homepage TEXT, kiosk INTEGER, reload_every INTEGER, rotation TEXT, allow_exit INTEGER,
            pending_action TEXT,
            last_seen REAL, first_seen REAL, last_report TEXT)""")
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def screen_token_ok():
    auth = request.headers.get("Authorization", "")
    return bool(TOKEN) and hmac.compare_digest(auth, "Bearer " + TOKEN)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not ADMIN_PASSWORD:
            return fn(*a, **kw)  # explicitly unprotected (dev only)
        auth = request.authorization
        if not auth or not hmac.compare_digest(auth.password or "", ADMIN_PASSWORD):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="fleet"'})
        return fn(*a, **kw)
    return wrapper


def row_to_dict(r):
    d = dict(r)
    d["online"] = bool(d["last_seen"] and time.time() - d["last_seen"] < OFFLINE_AFTER)
    d["report"] = json.loads(d.pop("last_report") or "{}")
    d["kiosk"] = None if d["kiosk"] is None else bool(d["kiosk"])
    d["allow_exit"] = None if d["allow_exit"] is None else bool(d["allow_exit"])
    return d


# ---------------------------------------------------------------- screens --
@app.post("/api/heartbeat")
def heartbeat():
    if not screen_token_ok():
        abort(401)
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id") or "").strip()[:64]
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    now = time.time()
    conn = db()
    row = conn.execute("SELECT * FROM screens WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO screens(device_id, name, first_seen, last_seen, last_report) VALUES (?,?,?,?,?)",
                     (device_id, data.get("name") or "", now, now, json.dumps(data)))
        row = conn.execute("SELECT * FROM screens WHERE device_id=?", (device_id,)).fetchone()
    else:
        conn.execute("UPDATE screens SET last_seen=?, last_report=? WHERE device_id=?", (now, json.dumps(data), device_id))
    # the reply: only the settings an operator set here (NULL = leave the screen alone)
    reply = {}
    for key in ("homepage", "kiosk", "reload_every", "rotation", "allow_exit"):
        if row[key] is not None:
            reply[key] = bool(row[key]) if key in ("kiosk", "allow_exit") else row[key]
    if row["name"]:
        reply["name"] = row["name"]
    if row["pending_action"]:
        reply["action"] = row["pending_action"]
        conn.execute("UPDATE screens SET pending_action=NULL WHERE device_id=?", (device_id,))
    conn.commit()
    return jsonify(reply)


# ---------------------------------------------------------------- people ---
@app.get("/")
@admin_required
def index():
    return render_template("index.html")


@app.get("/api/screens")
@admin_required
def screens():
    rows = db().execute("SELECT * FROM screens ORDER BY group_name, name, device_id").fetchall()
    return jsonify({"screens": [row_to_dict(r) for r in rows], "offline_after": OFFLINE_AFTER})


@app.post("/api/screens/<device_id>")
@admin_required
def update_screen(device_id):
    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    for key in EDITABLE:
        if key in data:
            value = data[key]
            if key in ("kiosk", "allow_exit"):
                value = None if value in (None, "") else (1 if value in (True, "true", 1, "1") else 0)
            elif key == "reload_every":
                value = None if value in (None, "") else int(value)
            elif key == "rotation" and value not in (None, "", "normal", "left", "right", "inverted"):
                return jsonify({"error": "bad rotation"}), 400
            else:
                value = (str(value).strip() or None) if value is not None else None
            sets.append(f"{key}=?")
            vals.append(value)
    if "action" in data:
        if data["action"] not in (None, "", "reload", "restart-browser", "reboot", "shutdown"):
            return jsonify({"error": "bad action"}), 400
        sets.append("pending_action=?")
        vals.append(data["action"] or None)
    if not sets:
        return jsonify({"error": "nothing to change"}), 400
    vals.append(device_id)
    conn = db()
    cur = conn.execute(f"UPDATE screens SET {', '.join(sets)} WHERE device_id=?", vals)
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "unknown screen"}), 404
    row = conn.execute("SELECT * FROM screens WHERE device_id=?", (device_id,)).fetchone()
    return jsonify({"screen": row_to_dict(row)})


@app.post("/api/screens/<device_id>/delete")
@admin_required
def delete_screen(device_id):
    conn = db()
    conn.execute("DELETE FROM screens WHERE device_id=?", (device_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/groups/<group_name>")
@admin_required
def update_group(group_name):
    """Apply the same change to every screen in a group (e.g. a new URL for all lobby screens)."""
    data = request.get_json(silent=True) or {}
    conn = db()
    ids = [r["device_id"] for r in conn.execute("SELECT device_id FROM screens WHERE group_name=?", (group_name,))]
    for device_id in ids:
        with app.test_request_context(json=data):
            update_screen.__wrapped__(device_id)  # reuse validation without the auth wrapper
    return jsonify({"updated": ids})


if __name__ == "__main__":
    if not TOKEN:
        print("WARNING: FLEET_TOKEN is empty; screens cannot authenticate. Set FLEET_TOKEN.")
    if not ADMIN_PASSWORD:
        print("WARNING: FLEET_ADMIN_PASSWORD is empty; the dashboard is open to anyone who can reach it.")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
