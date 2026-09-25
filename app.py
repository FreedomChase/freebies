"""
Gentle Push — Freebies hub
===========================
Three simple free tools:
  /links    — Link shortener  (SQLite, 6-char codes, click tracking)
  /qr       — QR code maker   (qrcode + Pillow, PNG served inline)
  /password — Password gen    (client-side JS, Flask only serves HTML)
"""

import io
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timezone
from urllib.parse import urlparse

import qrcode
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file
from flask_cors import CORS

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'tts_app', '.env'))

PORT       = int(os.getenv("FREEBIES_PORT", 4003))
SECRET_KEY = os.getenv("SECRET_KEY", "freebies-secret")
DB_PATH    = os.getenv("FREEBIES_DB_PATH") or os.path.join(os.path.dirname(__file__), "freebies.db")

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)


# ── Database (SQLite — zero extra deps) ───────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT    UNIQUE NOT NULL,
            url        TEXT    NOT NULL,
            ip         TEXT,
            created_at TEXT    NOT NULL,
            clicks     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def caller_ip():
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def is_safe_url(url: str) -> bool:
    """Only allow http/https; block localhost/private ranges to prevent SSRF."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = (p.hostname or "").lower()
        # Block private / loopback hosts
        blocked = ("localhost", "127.", "0.0.0.0", "::1", "10.", "192.168.", "169.254.", "172.")
        if any(host == b or host.startswith(b) for b in blocked):
            return False
        return bool(host)
    except Exception:
        return False


def gen_code(length: int = 6) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ── UI routes ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect("/links")


@app.route("/links")
def links_page():
    return render_template("links.html")


@app.route("/qr")
def qr_page():
    return render_template("qr.html")


@app.route("/password")
def password_page():
    return render_template("password.html")


# ── Link shortener API ────────────────────────────────────────────────────────
@app.route("/api/shorten", methods=["POST"])
def api_shorten():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not is_safe_url(url):
        return jsonify({"error": "That doesn't look like a valid public URL"}), 400
    if len(url) > 2048:
        return jsonify({"error": "URL is too long (max 2048 chars)"}), 400

    db = get_db()
    # Reuse existing code for same URL (idempotent)
    existing = db.execute("SELECT code FROM links WHERE url = ?", (url,)).fetchone()
    if existing:
        code = existing["code"]
    else:
        for _ in range(10):
            code = gen_code()
            try:
                db.execute(
                    "INSERT INTO links (code, url, ip, created_at) VALUES (?, ?, ?, ?)",
                    (code, url, caller_ip(), datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                break
            except sqlite3.IntegrityError:
                continue
        else:
            return jsonify({"error": "Could not generate a unique code. Try again."}), 500

    base = request.host_url.rstrip("/")
    return jsonify({"code": code, "short_url": f"{base}/s/{code}", "original_url": url})


@app.route("/api/recent-links")
def api_recent_links():
    """Return the last 10 links created by this IP."""
    ip = caller_ip()
    db = get_db()
    rows = db.execute(
        "SELECT code, url, clicks, created_at FROM links WHERE ip = ? ORDER BY id DESC LIMIT 10",
        (ip,)
    ).fetchall()

    base = request.host_url.rstrip("/")
    return jsonify([
        {
            "code": r["code"],
            "short_url": f"{base}/s/{r['code']}",
            "original_url": r["url"],
            "clicks": r["clicks"],
            "created_at": r["created_at"],
        }
        for r in rows
    ])


@app.route("/s/<code>")
def short_redirect(code):
    if not re.fullmatch(r"[A-Za-z0-9]{4,12}", code):
        abort(404)
    db = get_db()
    row = db.execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
    db.commit()
    return redirect(row["url"], code=302)


# ── QR code API ───────────────────────────────────────────────────────────────
@app.route("/api/qr")
def api_qr():
    text = (request.args.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text query param required"}), 400
    if len(text) > 1000:
        return jsonify({"error": "Text too long (max 1000 chars)"}), 400

    size = min(max(int(request.args.get("size", 300)), 100), 1000)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0A0A14", back_color="#ffffff")
    img = img.resize((size, size))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", as_attachment=False, download_name="qrcode.png")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Ensure DB is ready
    get_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)
