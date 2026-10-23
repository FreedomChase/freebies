"""
ORIONPATH Freebies
==================
Three small free tools in one Flask app:
  /links    — Link shortener  (SQLite, 6-char codes, click counts)
  /qr       — QR code maker   (qrcode + Pillow, PNG served inline)
  /password — Password gen    (client-side Web Crypto; Flask only serves HTML)

Privacy model: no accounts and no IP addresses in the database. A visitor's
"recent links" are tied to a random owner token that lives only in their
browser; the server keeps its SHA-256, never the token itself.
"""

import hashlib
import io
import ipaddress
import os
import re
import secrets
import socket
import sqlite3
import string
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import qrcode
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
OWNER_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def _parse_ip(host: str):
    """Parse a host as an IP the way a browser would: dotted, decimal, hex or IPv6."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    try:
        # inet_aton accepts the legacy forms browsers still resolve:
        # 2130706433, 0x7f000001, 127.1, 0177.0.0.1
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def is_safe_url(url: str) -> bool:
    """Only public http(s) destinations: no loopback, private, link-local or reserved hosts."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or p.username or p.password:
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        return False
    ip = _parse_ip(host)
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return False
    elif "." not in host:
        return False
    return True


def gen_code(length: int = 6) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Sliding-window limit per client IP, held in memory only (never stored)."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DB_PATH=os.getenv("FREEBIES_DB_PATH")
        or os.path.join(os.path.dirname(__file__), "freebies.db"),
        # Number of reverse proxies in front of the app. 0 = trust nothing, use the socket.
        TRUST_PROXY_HOPS=int(os.getenv("TRUST_PROXY_HOPS", "0")),
        SHORTEN_LIMIT=int(os.getenv("SHORTEN_LIMIT", "10")),
        SHORTEN_WINDOW_S=int(os.getenv("SHORTEN_WINDOW_S", "600")),
        QR_LIMIT=int(os.getenv("QR_LIMIT", "60")),
        QR_WINDOW_S=int(os.getenv("QR_WINDOW_S", "60")),
    )
    if config:
        app.config.update(config)

    hops = app.config["TRUST_PROXY_HOPS"]
    if hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    shorten_limiter = RateLimiter(app.config["SHORTEN_LIMIT"], app.config["SHORTEN_WINDOW_S"])
    qr_limiter = RateLimiter(app.config["QR_LIMIT"], app.config["QR_WINDOW_S"])

    def get_db():
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                code       TEXT    UNIQUE NOT NULL,
                url        TEXT    NOT NULL,
                owner      TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                clicks     INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS links_owner ON links(owner)")
        conn.commit()
        return conn

    def client_ip() -> str:
        # ProxyFix has already rewritten remote_addr when proxies are trusted.
        return request.remote_addr or "unknown"

    def too_many():
        return jsonify({"error": "Too many requests. Wait a few minutes and try again."}), 429

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    # ── UI routes ──────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return redirect("/links")

    @app.route("/links")
    def links_page():
        return render_template("links.html", active="links")

    @app.route("/qr")
    def qr_page():
        return render_template("qr.html", active="qr")

    @app.route("/password")
    def password_page():
        return render_template("password.html", active="password")

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    # ── Link shortener API ─────────────────────────────────────────────────────
    @app.route("/api/shorten", methods=["POST"])
    def api_shorten():
        data = request.get_json(silent=True) or {}
        url = str(data.get("url") or "").strip()
        owner = str(data.get("owner") or "")

        if not OWNER_RE.fullmatch(owner):
            return jsonify({"error": "Missing visitor token. Reload the page."}), 400
        if not url:
            return jsonify({"error": "URL is required"}), 400
        if len(url) > 2048:
            return jsonify({"error": "URL is too long (max 2048 chars)"}), 400
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if not is_safe_url(url):
            return jsonify({"error": "That doesn't look like a valid public URL"}), 400
        if not shorten_limiter.allow(client_ip()):
            return too_many()

        db = get_db()
        mine = owner_hash(owner)
        existing = db.execute(
            "SELECT code FROM links WHERE url = ? AND owner = ?", (url, mine)
        ).fetchone()
        if existing:
            code = existing["code"]
        else:
            for _ in range(10):
                code = gen_code()
                try:
                    db.execute(
                        "INSERT INTO links (code, url, owner, created_at) VALUES (?, ?, ?, ?)",
                        (code, url, mine, datetime.now(timezone.utc).isoformat()),
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
        """The last 10 links made with this browser's owner token."""
        owner = request.headers.get("X-Owner-Token", "")
        if not OWNER_RE.fullmatch(owner):
            return jsonify([])
        rows = get_db().execute(
            "SELECT code, url, clicks, created_at FROM links WHERE owner = ? ORDER BY id DESC LIMIT 10",
            (owner_hash(owner),),
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

    # ── QR code API ────────────────────────────────────────────────────────────
    @app.route("/api/qr")
    def api_qr():
        text = (request.args.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text query param required"}), 400
        if len(text) > 1000:
            return jsonify({"error": "Text too long (max 1000 chars)"}), 400
        try:
            size = int(request.args.get("size", 300))
        except ValueError:
            return jsonify({"error": "size must be a whole number"}), 400
        size = min(max(size, 100), 1000)
        if not qr_limiter.allow(client_ip()):
            return too_many()

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(text)
        qr.make(fit=True)

        img = qr.make_image(fill_color="#052444", back_color="#ffffff")
        img = img.resize((size, size))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png", as_attachment=False, download_name="qrcode.png")

    return app


app = create_app()
