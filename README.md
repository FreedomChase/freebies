# Freebies

Three small web tools in one Flask app, from ORIONPATH. No accounts, no cost.

- **Link shortener**: turn a long URL into a short one. Only public web addresses are accepted.
- **QR code maker**: generate a QR code for any text or URL and download it as a PNG. Nothing is stored.
- **Password generator**: made in the browser with the Web Crypto API; nothing is sent to the server.

## What the shortener keeps

The long URL, its short code, when it was made and how many times it was opened. No IP address
and no account. A visitor's "recent links" list is tied to a random token that lives only in
their browser; the database holds its SHA-256, never the token. Rate limits are counted in
process memory per client IP and are never written down.

## Run it

```bash
docker build -t freebies .
docker run -p 127.0.0.1:4003:4003 -v freebies-data:/data freebies
```

Then open `http://localhost:4003`. The image installs `requirements.lock.txt` with
`--require-hashes --only-binary=:all:` and serves with gunicorn (one worker, eight threads: the
rate limiter lives in that one process).

| Variable | Default | Meaning |
|---|---|---|
| `FREEBIES_DB_PATH` | `/data/freebies.db` in the image | SQLite file |
| `TRUST_PROXY_HOPS` | `0` | Reverse proxies in front of the app. `0` ignores `X-Forwarded-For`; set it to the real number of proxies, never more |
| `SHORTEN_LIMIT` / `SHORTEN_WINDOW_S` | `10` / `600` | Shortens allowed per client IP per window |
| `QR_LIMIT` / `QR_WINDOW_S` | `60` / `60` | QR codes allowed per client IP per window |

## Dependencies

`requirements.txt` holds the top-level pins. Regenerate the lock after changing it:

```bash
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.lock.txt requirements.txt
```

## Tests

```bash
pip install --require-hashes -r requirements.lock.txt -r requirements-dev.lock.txt
python -m pytest -q tests
```

Part of [ORIONPATH](https://orionpath.io).
