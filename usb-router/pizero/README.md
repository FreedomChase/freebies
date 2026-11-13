# USB Router on a Raspberry Pi Zero W

The same signage player as the PC stick, built for the **Raspberry Pi Zero W**
(ARMv6, one 1 GHz core, 512 MB RAM, 2.4 GHz Wi-Fi, mini-HDMI). It plugs into
the screen's HDMI port and runs from its micro-SD card. The connection
manager, the setup page, the admin PIN, the per-device hotspot password and the
fleet dashboard are the same code as the stick. Only the browser is different.

## What is different from the PC stick

| | PC stick | Pi Zero W |
|---|---|---|
| OS | Debian 12 live, from USB | Raspberry Pi OS Lite 32-bit (Bookworm), on the SD card |
| Browser | PyQt6 / Qt WebEngine (Chromium), under X + Openbox | **cog** (WPE WebKit) straight on the screen (DRM). No X, no window manager |
| Browser control | `browser/browser.py` | `pizero/kiosk.py` drives cog with `cogctl`, same API |
| Settings file | `USBROUTER` FAT partition, `/config/usb-router.conf` | the SD card's boot partition, `/boot/firmware/usb-router.conf` |
| Swap | none | zram (lz4, half the RAM); the SD-card swap file is turned off |
| Portrait screens | `rotation = left/right` | **not supported yet** (see below) |
| Keyboard shortcuts | F11, Ctrl+N, ... | none; cog has no toolbar or shortcuts |

Why cog: Qt WebEngine is not built for ARMv6, and Chromium is too heavy for
512 MB. Checked on the Raspbian Bookworm armhf archive: `python3-pyqt6.qtwebengine`
and `chromium-browser` have no ARMv6 package; `cog` 0.16.1 with WPE WebKit
2.38.5 does.

## Hardware

- **Raspberry Pi Zero W** (or Zero WH). A Zero 2 W also works and is much faster.
- **micro-SD card**, 8 GB or more, A1 rated (SanDisk Ultra / Samsung EVO).
- **mini-HDMI to HDMI** adapter or cable.
- **5 V / 2.5 A micro-USB power supply**. A monitor's USB port is not enough.
- Only one USB port (micro-USB OTG): sharing the connection when the Pi is on
  Wi-Fi needs a second Wi-Fi adapter on an OTG adapter or hub. Sharing is off by
  default anyway; the setup hotspot uses the built-in Wi-Fi while offline.

## Install

1. Write **Raspberry Pi OS Lite (32-bit)** to the SD card with Raspberry Pi
   Imager. In Imager's settings set a user, enable SSH and (optionally) the
   venue Wi-Fi.
2. Boot the Pi, connect over SSH, copy this `usb-router` folder to it, then:

   ```bash
   sudo ./pizero/install-pizero.sh
   ```

3. Edit `/boot/firmware/usb-router.conf` (or the `usb-router.conf` file on the
   card's boot partition, from any computer): `homepage`, `wifi_ssid`,
   `wifi_password`, `fleet_url`, `fleet_token`. The keys are the same as the
   stick's (`config/usb-router.conf.example`).
   The file holds passwords, so the installer mounts the boot partition
   root:kiosk with files 0640 (`/etc/fstab`): on the Pi, edit it with `sudo`.
   The browser user (`signage`) cannot read it. `sudo ./pizero/fat-mask-test.sh`
   checks that mask on a real vfat mount (any Linux box).
4. Reboot. The screen shows the setup page, then your content once online.

For a batch: set one card up, then copy the card image to the others. The
admin PIN and the hotspot password are generated at first boot and kept in
`/var/lib/usb-router/secrets.json`, so **delete that file before you copy the
image**, or every screen gets the same PIN and password.

Options: `--no-kiosk` installs the connection manager only (no screen);
`--no-systemd` installs files and packages only (image builds, the container test).

## What runs

| Service | What it is |
|---|---|
| `usb-router-connect` | the connection manager on `127.0.0.1:8080` (unchanged from the stick) |
| `usb-router-kiosk` | `pizero/kiosk.py` on tty1, which starts cog on the screen and drives it |
| `zramswap` | compressed swap in RAM |

The kiosk does what `browser.py` does for signage: setup page until online,
then the content URL fullscreen; a homepage changed from the settings page or
the dashboard is opened at once; scheduled reloads; reload when the network
comes back; restart on request or when cog dies. When the content URL does not
answer, the screen shows "Content unavailable" with the URL and the screen ID
(`app/static/unavailable.html`) and retries after 5, 10, 20, 40, then every 60 s.
cog's WebKit memory limit is 200 MiB (`USB_ROUTER_COG_MEM` in the service to change).

Measured on Raspbian Bookworm armhf (native, headless, no GPU), showing the
"unavailable" page: cog 29 MiB, WebKit network process 30 MiB, WebKit web
process 145 MiB, about 204 MiB in all; the connection manager is under 70 MiB.
Keep the GPU share small so this fits: the KMS driver takes its memory from CMA,
so leave `gpu_mem` unset (or 64 at most) in `config.txt` rather than giving the
GPU half the RAM as some signage images do.

### Memory at 1080p: an estimate, not yet measured on a real Pi

| Part | MiB | Source |
|---|---|---|
| GPU firmware share (`gpu_mem`) | 64 | assumed, the setting above |
| Kernel and base Pi OS Lite | 70 | assumed |
| Connection manager | 70 | measured upper bound (under emulation) |
| cog + WebKit, one simple page | 204 | measured (headless, no screen) |
| Screen buffers, 1920 x 1080 x 4 bytes x 3 | 24 | arithmetic |
| WebKit GPU tiles at 1080p | 30 to 60 | assumed |
| **Total** | **460 to 490** | of 512 |

So 1080p is tight on paper: a heavy content page could push it into swap
(zram softens this). If the first real-Pi check shows too little free memory,
run the screen at 720p by adding `video=HDMI-A-1:1280x720@60` to `cmdline.txt`
(screen buffers drop to about 11 MiB and WebKit tiles shrink with them).
The first check on real hardware: `free -m` with the content page showing at 1080p.

Logs: `journalctl -u usb-router-connect -u usb-router-kiosk -f`

## Test it without a Pi

```bash
# the controller logic, anywhere:
python3 -m pytest tests/test_pizero_kiosk.py

# the whole build on ARMv6, in a throwaway container (docker; arm64 host, or x86 with binfmt):
apt-get download qemu-user-static && dpkg -x qemu-user-static_*.deb qemu
./pizero/qemu-test.sh qemu/usr/bin/qemu-arm-static
```

`qemu-test.sh` installs the player into a Raspbian Bookworm armhf container
with `install-pizero.sh`, then runs the test suite, the connection manager,
headless cog (all of its processes) and `cogctl` under qemu emulating the Pi
Zero's ARM1176 CPU, so an ARMv7-only instruction fails the run. It ends with a
memory estimate of cog and WebKit.

## Not done yet / not verified

- **Not run on a real Pi Zero W.** No screen output, Wi-Fi, hotspot, or boot
  was tested on hardware. cog's DRM output, the tty1 session and the brcmfmac
  Wi-Fi access point are standard on Raspberry Pi OS but unproven here.
- **Portrait rotation**: cog 0.16.1's DRM plug-in has no rotation setting. A
  screen set to `left`/`right`/`inverted` is shown unrotated, with a warning in
  the log. Two candidates for a hardware test: the kernel's
  `video=HDMI-A-1:...,rotate=90` option, or cog under the `cage` compositor with
  `wlr-randr --transform` (both are in the archive).
- **Speed**: one ARMv6 core renders slowly. Expect static pages, images and
  simple slideshows to work; heavy JavaScript and video will struggle (use a
  Zero 2 W for those).
- **No ready-made SD image**: install on top of Raspberry Pi OS Lite as above.
