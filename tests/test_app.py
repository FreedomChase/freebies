import re
import sqlite3
from pathlib import Path

import pytest

from app import create_app, is_safe_url

OWNER_A = "a" * 43
OWNER_B = "b" * 43
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def make_client(tmp_path):
    def _make(**config):
        cfg = {"DB_PATH": str(tmp_path / "t.db"), "TESTING": True}
        cfg.update(config)
        app = create_app(cfg)
        return app.test_client(), cfg["DB_PATH"]
    return _make


@pytest.fixture
def client(make_client):
    return make_client()[0]


def shorten(client, url, owner=OWNER_A, **kw):
    return client.post("/api/shorten", json={"url": url, "owner": owner}, **kw)


@pytest.mark.parametrize("path", ["/links", "/qr", "/password", "/healthz"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_index_redirects_to_links(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/links")


def test_shorten_and_redirect(client):
    r = shorten(client, "https://orionpath.io/")
    assert r.status_code == 200
    code = r.get_json()["code"]
    r = client.get(f"/s/{code}")
    assert r.status_code == 302 and r.headers["Location"] == "https://orionpath.io/"


def test_shorten_needs_owner_token(client):
    r = client.post("/api/shorten", json={"url": "https://orionpath.io/"})
    assert r.status_code == 400


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://foo.localhost/", "http://printer.local/",
    "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/", "http://172.31.255.255/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://2130706433/", "http://0x7f000001/", "http://127.1/", "http://0177.0.0.1/",
    "http://0.0.0.0/", "http://100.64.0.1/", "http://[fd00::1]/", "http://224.0.0.1/",
    "javascript:alert(1)", "ftp://x.org/", "http://user:pw@example.com/", "https://intranet/",
])
def test_rejects_non_public_destinations(client, url):
    assert shorten(client, url).status_code == 400


@pytest.mark.parametrize("url", ["https://example.com/", "http://172.217.1.1/", "https://8.8.8.8/x", "example.org/path"])
def test_accepts_public_destinations(client, url):
    assert shorten(client, url).status_code == 200


def test_is_safe_url_172_only_blocks_the_private_block():
    assert is_safe_url("http://172.15.0.1/") and is_safe_url("http://172.32.0.1/")
    assert not is_safe_url("http://172.16.0.1/")


def test_recent_links_isolated_by_owner_token(client):
    shorten(client, "https://orionpath.io/a", owner=OWNER_A)
    mine = client.get("/api/recent-links", headers={"X-Owner-Token": OWNER_A}).get_json()
    theirs = client.get("/api/recent-links", headers={"X-Owner-Token": OWNER_B}).get_json()
    none = client.get("/api/recent-links").get_json()
    assert [l["original_url"] for l in mine] == ["https://orionpath.io/a"]
    assert theirs == [] and none == []


def test_spoofed_forwarded_for_reveals_nothing(client):
    shorten(client, "https://orionpath.io/secret", owner=OWNER_A,
            headers={"X-Forwarded-For": "203.0.113.7"})
    r = client.get("/api/recent-links", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.get_json() == []


def test_no_ip_or_raw_token_in_database(make_client):
    client, db_path = make_client()
    shorten(client, "https://orionpath.io/", environ_base={"REMOTE_ADDR": "198.51.100.9"})
    cols = [r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(links)")]
    dump = "\n".join(sqlite3.connect(db_path).iterdump())
    assert "ip" not in cols
    assert "198.51.100.9" not in dump and OWNER_A not in dump


def test_no_cors_headers(client):
    r = client.get("/api/recent-links", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    r = client.options("/api/shorten", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "POST"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_security_headers(client):
    r = client.get("/links")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "style-src 'self'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_shorten_rate_limited_per_ip(make_client):
    client, _ = make_client(SHORTEN_LIMIT=3, SHORTEN_WINDOW_S=600)
    codes = [shorten(client, f"https://example.com/{i}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    other = shorten(client, "https://example.com/x", environ_base={"REMOTE_ADDR": "198.51.100.20"})
    assert other.status_code == 200


def test_forwarded_for_ignored_unless_proxy_trusted(make_client):
    client, _ = make_client(SHORTEN_LIMIT=1)
    assert shorten(client, "https://example.com/1", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    # A new spoofed address must not buy a fresh allowance.
    assert shorten(client, "https://example.com/2", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_forwarded_for_used_behind_one_trusted_proxy(make_client):
    client, _ = make_client(SHORTEN_LIMIT=1, TRUST_PROXY_HOPS=1)
    assert shorten(client, "https://example.com/1", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert shorten(client, "https://example.com/2", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_qr_png(client):
    r = client.get("/api/qr?text=hello&size=300")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == b"\x89PNG"


@pytest.mark.parametrize("query", ["size=abc", "size=", "size=1.5"])
def test_qr_bad_size_is_400(client, query):
    assert client.get(f"/api/qr?text=hello&{query}").status_code == 400


def test_qr_limits(client):
    assert client.get("/api/qr").status_code == 400
    assert client.get("/api/qr?text=" + "a" * 1001).status_code == 400


def test_qr_rate_limited(make_client):
    client, _ = make_client(QR_LIMIT=2)
    codes = [client.get("/api/qr?text=hi").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_unknown_short_code_404(client):
    assert client.get("/s/zzzzzz").status_code == 404
    assert client.get("/s/../etc").status_code == 404


def test_templates_have_no_inline_css_or_js():
    for page in (ROOT / "templates").glob("*.html"):
        html = page.read_text()
        assert "<style" not in html and "style=" not in html, page.name
        assert not re.search(r"\son[a-z]+=", html), page.name
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), page.name


def test_no_dotenv_or_tts_coupling():
    src = (ROOT / "app.py").read_text()
    assert "dotenv" not in src and "tts_app" not in src and "flask_cors" not in src


def test_dockerfile_uses_gunicorn_and_hashes():
    docker = (ROOT / "Dockerfile").read_text()
    assert "gunicorn" in docker and "--require-hashes" in docker
    assert "app.run" not in (ROOT / "app.py").read_text()
