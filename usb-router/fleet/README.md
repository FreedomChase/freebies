# Fleet dashboard

A single-file Flask app that shows every screen in the display network and
lets you change each screen's content URL, rotation and reload interval, or
reload / restart / reboot it. Screens report in every `fleet_interval` seconds
and apply changes in the reply.

```bash
pip install flask
export FLEET_TOKEN="$(openssl rand -hex 16)"          # screens -> dashboard; keep it
export FLEET_ADMIN_PASSWORD="$(openssl rand -base64 12)"  # your login
python3 fleet/server.py
# dashboard: http://<host>:4010/   (any username, the password above)
```

There are no default secrets. The server refuses to start unless `FLEET_TOKEN`
(16+ characters) and `FLEET_ADMIN_PASSWORD` (8+ characters) are both set.

Then on every stick (`usb-router.conf` on the USBROUTER drive):

```ini
fleet_url = https://signage.example.com
fleet_token = <the same FLEET_TOKEN>
```

Put it behind HTTPS (Caddy, nginx, Cloudflare Tunnel) before using it over the
internet. The database is SQLite (`fleet/fleet.db`).

| Env | Default | |
|---|---|---|
| `FLEET_TOKEN` | none | Shared secret the screens send. Required, 16+ chars. |
| `FLEET_ADMIN_PASSWORD` | none | Dashboard login (HTTP basic). Required, 8+ chars. |
| `FLEET_PORT` | 4010 | |
| `FLEET_DB_PATH` | `fleet/fleet.db` | |
| `FLEET_OFFLINE_AFTER` | 180 | Seconds without a heartbeat before a screen shows as offline |

API, if you would rather drive it from your own system:

- `POST /api/heartbeat` (screen, Bearer token) body as in `app/fleet_client.py`; reply is the settings to apply.
- `GET /api/screens` (admin)
- `POST /api/screens/<id>` (admin) `{name, group_name, homepage, rotation, reload_every, kiosk, allow_exit, action}`
- `POST /api/groups/<group>` (admin) same body, applied to every screen in the group.
