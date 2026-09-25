# Freebies

Three small web tools in one Flask app. No accounts, no tracking, no cost.

- **Link shortener** — turn a long URL into a short one. Destinations are validated
  before they are stored, so the shortener cannot be pointed at private addresses.
- **QR code maker** — generate a QR code for any text or URL and download it.
- **Password generator** — generate a password, in the browser, without sending
  anything anywhere.

Built because these are the three things people search for, install a browser
extension to get, and then have their clipboard read by it.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Or with Docker:

```bash
docker build -t freebies .
docker run -p 4003:4003 freebies
```

Then open `http://localhost:4003`.

## Notes

The SQLite database is created on first run and is not committed — short links are
runtime state, not source. `is_safe_url()` blocks private and loopback destinations
before a link is stored, which is the part most shorteners skip.

Part of [ORIONPATH](https://orionpath.io).
