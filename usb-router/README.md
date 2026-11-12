# USB Router

A bootable USB stick that turns any PC or laptop into an internet router with a
built-in browser.

Plug it in, boot from it, and the screen shows a connection page. Pick a Wi-Fi
network (or plug in a cable or a 4G dongle). The moment the stick is online it
shares that connection out of a spare adapter as a Wi-Fi hotspot or over an
Ethernet cable, and a custom browser opens on your chosen home page. Press F11
for fullscreen, or set kiosk mode and it goes fullscreen on its own.

Nothing is installed on the PC. Pull the stick out and the machine is untouched.

![The connection page, online and sharing](docs/connection-page.png)

```
┌──────────────────────────────────────────────────────────────────────┐
│  USB stick (Debian live)                                             │
│                                                                      │
│   NetworkManager ◄──── app/server.py (Flask, port 8080) ◄── browser  │
│   │  uplink: wifi / ethernet / 4g     │ web page: scan, connect,     │
│   │  hotspot: ipv4.method=shared      │ hotspot, settings            │
│   │  (dnsmasq DHCP + NAT)             │                              │
│   └── wlan1 / eth1 ──► phones, laptops, TVs      browser/browser.py  │
│                                                  (Qt WebEngine)      │
└──────────────────────────────────────────────────────────────────────┘
```

## What is in this folder

| Path | What it is |
|------|------------|
| `app/` | The connection-manager web app (Flask). Scans Wi-Fi, connects, detects captive portals, runs the hotspot, stores settings. |
| `browser/browser.py` | The custom browser (PyQt6 / Qt WebEngine, Chromium inside). Tabs, address bar, F11 fullscreen, HTML5 fullscreen, kiosk mode. |
| `system/` | systemd units, polkit rule and NetworkManager config that glue it together. |
| `scripts/` | The X session that starts the browser, and `install-local.sh` for installing on an existing Debian box instead of a stick. |
| `build/` | `build-iso.sh` builds the bootable image with Debian live-build. `make-usb.sh` writes it to a stick and adds the persistence and config partitions. |
| `config/usb-router.conf.example` | All settings, with comments. |
| `tests/` | pytest suite that runs against a fake NetworkManager. |
| `HARDWARE.md` | Which USB sticks and Wi-Fi adapters to order, and why. |

## How it behaves at boot

1. The PC boots from the stick (Debian 12 live, BIOS and UEFI).
2. NetworkManager starts. An Ethernet cable or a known 4G dongle connects on its own.
3. The connection manager (`app/server.py`) starts on `http://127.0.0.1:8080`.
4. The `kiosk` user is logged in on tty1, X starts, and the browser opens the
   connection page with no toolbar.
5. You pick a Wi-Fi network and type the password. The page checks for real
   internet (a `204` from a connectivity URL). If a hotel or café login page is
   in the way, it says so and offers to open it.
6. Once online:
   - the hotspot starts on a spare adapter (if `hotspot_enabled`), and
   - the browser unlocks its toolbar and goes to the home page, or straight to
     fullscreen if `kiosk = true`.
7. Saved networks, settings and downloads persist on the stick between boots.

`Ctrl+N` in the browser brings the connection page back at any time.

### Which adapter becomes the hotspot

| Internet comes from | Hotspot runs on |
|---|---|
| Ethernet cable | the PC's built-in Wi-Fi card |
| Wi-Fi | a second, AP-capable USB Wi-Fi adapter, otherwise a free Ethernet port (wired sharing) |
| 4G/5G USB dongle | the built-in Wi-Fi card |

A single Wi-Fi card cannot usually be a client and an access point at the same
time, so a laptop with one card that is online over Wi-Fi needs the USB adapter
from `HARDWARE.md` to share. The page tells you when that is the case.

NetworkManager's shared mode does the DHCP (dnsmasq), DNS forwarding and NAT.
Hotspot clients get `10.42.0.x`; the stick is `10.42.0.1`.

## Build the stick

You need a Linux machine (or Docker) and a stick of at least 8 GB.

```bash
# 1. Build the ISO (20-60 min, ~6 GB scratch). Either:
sudo apt install live-build && sudo ./build/build-iso.sh
#    or, on any Linux with Docker:
./build/build-iso.sh --docker

# 2. Write it. THIS ERASES THE STICK. Find the device with lsblk first.
sudo ./build/make-usb.sh build/out/usb-router-YYYYMMDD.iso /dev/sdX
```

`make-usb.sh` lays the stick out as:

| Partition | Label | Purpose |
|---|---|---|
| 1-2 | (ISO) | read-only live system |
| 3 | `persistence` | ext4, saved Wi-Fi networks, settings, downloads |
| 4 | `USBROUTER` | FAT32, `usb-router.conf`, visible on Windows and macOS |

Edit `usb-router.conf` on the `USBROUTER` drive from any computer to change the
home page, hotspot name and password, kiosk mode, and so on. Changes made on the
connection page itself are saved to the persistence partition and win over the
file.

Then boot the target PC from USB (usually F12, F2, Esc or Del at power-on to
pick the boot device; Secure Boot may need to be off for the live image).

## Run it without a stick

For development, or to make a mini-PC behave like the stick:

```bash
# Web app only, with a fake NetworkManager (works on any OS with Python 3):
pip install -r requirements-dev.txt
USB_ROUTER_MOCK=1 python3 app/server.py          # http://127.0.0.1:8080

# Browser against that page (needs PyQt6 + PyQt6-WebEngine):
pip install -r requirements-browser.txt
python3 browser/browser.py                       # or: browser/browser.py --kiosk

# Full install on an existing Debian 12 / Ubuntu 22.04+ machine:
sudo ./scripts/install-local.sh                  # add --no-x for the web app only
```

Tests:

```bash
USB_ROUTER_MOCK=1 python3 -m pytest tests/
```

## Configuration

All keys, with defaults, are in `config/usb-router.conf.example`. The ones
people change:

| Key | Default | Meaning |
|---|---|---|
| `homepage` | `https://duckduckgo.com` | Page the browser opens when online |
| `kiosk` | `false` | Fullscreen, no toolbar, once online. F11 or Esc reveals the toolbar |
| `allow_exit` | `true` | Show the Quit button / allow Ctrl+Q |
| `hotspot_enabled` | `true` | Start sharing automatically when online |
| `hotspot_ssid` / `hotspot_password` | `USB-Router` / `changeme123` | Hotspot name and WPA2 password (8+ chars) |
| `hotspot_band` | `bg` | `bg` = 2.4 GHz, `a` = 5 GHz |
| `lan_access` | `false` | Also serve the page to hotspot clients on `http://10.42.0.1:8080` |
| `admin_pin` | empty | Required (header `X-Admin-Pin`) for changes made from other devices when `lan_access` is on |

Environment variables `USB_ROUTER_<KEY>` override the file, which is how the
tests and the mock mode work.

## API

The browser and the page both use the same JSON API, so you can script it too.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Online state, uplink, devices, hotspot, settings in one call |
| GET | `/api/wifi/scan?rescan=1` | Nearby networks (strongest BSSID per SSID) |
| POST | `/api/wifi/connect` | `{ssid, password}` |
| POST | `/api/wifi/forget` | `{ssid}` |
| POST | `/api/disconnect` | `{iface}` |
| POST | `/api/ethernet` | `{iface}` retry a wired connection |
| GET | `/api/check` | Fresh internet / captive-portal check |
| GET, POST | `/api/hotspot` | Status + candidate adapters; `{action: start/stop/restart, iface?}` |
| GET, POST | `/api/settings` | Read / change settings |
| POST | `/api/browser/go` | `{url?}` tell the browser to leave the connect screen |
| GET | `/api/browser/poll` | What the browser polls every 2 s |

Everything that changes state is loopback-only unless `lan_access` is on.

## Browser keyboard shortcuts

| Keys | Action |
|---|---|
| F11 | Toggle fullscreen (also leaves kiosk mode) |
| Esc | Leave HTML5 / window fullscreen, or stop loading |
| Ctrl+L | Address bar |
| Ctrl+T / Ctrl+W | New / close tab |
| Ctrl+N | Connection page |
| Ctrl+R, F5 | Reload |
| Alt+Left / Alt+Right | Back / forward |
| Ctrl+= / Ctrl+- / Ctrl+0 | Zoom |
| Ctrl+Q | Quit (if `allow_exit`) |
| Ctrl+Alt+T | Terminal (for support) |

## Security notes

- The web app runs as the unprivileged `kiosk` user. It talks to NetworkManager
  over D-Bus; a polkit rule grants the `netdev` group that right, nothing else.
- Change `hotspot_password` before handing a stick to anyone. The default is in
  this README.
- `lan_access` is off by default. Turn it on only with an `admin_pin` set.
- The browser refuses to bypass TLS certificate errors.
- The persistence partition is not encrypted. Wi-Fi passwords are stored on it
  (by NetworkManager, root-readable only). Treat the stick like a laptop.

## What was and was not verified

- The web app and API were exercised end to end against the mock backend
  (pytest, and a headless Chromium run through connect, hotspot and captive-portal
  flows).
- `browser/browser.py` compiles but needs a display and PyQt6 to run; it was
  not run in this environment.
- The ISO was not built here. `build-iso.sh` is a standard live-build recipe;
  expect to iterate once on the first build (mirror hiccups, package renames).

Part of [freebies](../README.md).
