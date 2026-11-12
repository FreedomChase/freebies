# Fleet dashboard

A single-file Flask app that shows every screen in the display network and
lets you change each screen's content URL, rotation and reload interval, or
reload / restart / reboot it. Screens report in every `fleet_interval` seconds
and apply changes in the reply.

```bash
pip install flask
FLEET_TOKEN=$(openssl rand -hex 16) FLEET_ADMIN_PASSWORD=choose-one python3 fleet/server.py
# dashboard: http://<host>:4010/   (any username, the password above)
```

Then on every stick (`usb-router.conf` on the USBROUTER drive):

```ini
fleet_url = https://signage.example.com
fleet_token = <the FLEET_TOKEN above>
```

Put it behind HTTPS (Caddy, nginx, Cloudflare Tunnel) before using it over the
internet. The database is SQLite (`fleet/fleet.db`).

| Env | Default | |
|---|---|---|
| `FLEET_TOKEN` | empty | Shared secret the screens send. Required. |
| `FLEET_ADMIN_PASSWORD` | empty | Dashboard login (HTTP basic). Empty = open, dev only. |
| `FLEET_PORT` | 4010 | |
| `FLEET_DB_PATH` | `fleet/fleet.db` | |
| `FLEET_OFFLINE_AFTER` | 180 | Seconds without a heartbeat before a screen shows as offline |

API, if you would rather drive it from your own system:

- `POST /api/heartbeat` (screen, Bearer token) body as in `app/fleet_client.py`; reply is the settings to apply.
- `GET /api/screens` (admin)
- `POST /api/screens/<id>` (admin) `{name, group_name, homepage, rotation, reload_every, kiosk, allow_exit, action}`
- `POST /api/groups/<group>` (admin) same body, applied to every screen in the group.
