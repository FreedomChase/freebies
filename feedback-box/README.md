# Feedback box

Three buttons on a counter: happy, OK, sad. Each press goes over Wi-Fi to a small server, and a count page
shows the totals. Built for the ESP32-WROOM-32 DevKit (38 pin, CP2102, USB-C). Prototype, C&G #213.

## What is in this folder

| Folder | What |
|---|---|
| `firmware/` | The box. PlatformIO + Arduino. `tools/build.sh` builds it in a container. |
| `worker/` | The server: a Cloudflare Worker with a D1 database. Not deployed yet. |

## Try it today with only the board (no buttons needed)

The **BOOT** button on the board sends a **test** press. The count page shows test presses on their own line,
apart from the real counts.

## Flashing the board from a Windows laptop

1. Download **esptool** for Windows from Espressif:
   `https://github.com/espressif/esptool/releases/download/v5.6.0/esptool-v5.6.0-windows-amd64.zip`.
   Unzip it. Inside is `esptool.exe`.
2. Put `feedback-box-v1.bin` in the same folder as `esptool.exe`.
3. Plug the board in with a USB-C **data** cable (some cables only charge).
4. Find the port: open **Device Manager**, then **Ports (COM & LPT)**. Look for
   **Silicon Labs CP210x USB to UART Bridge (COM5)**. The number may be different.
   If it is not there, Windows needs the CP210x driver from Silicon Labs ("CP210x VCP driver").
5. Open a terminal in that folder and run, with your COM number:

       esptool.exe --chip esp32 --port COM5 erase-flash
       esptool.exe --chip esp32 --port COM5 --baud 460800 write-flash 0x0 feedback-box-v1.bin

   If it says **Failed to connect**: run it again and hold the **BOOT** button while it says `Connecting...`.
6. Press **EN** (the reset button). The board starts the feedback box.

Check the file first (optional): `certutil -hashfile feedback-box-v1.bin SHA256` must print the value in
`SHA256SUMS.txt`.

## Setting it up (Wi-Fi, server, key)

1. After flashing, the blue LED **blinks slowly**: setup mode.
2. On a phone, join the Wi-Fi called **Feedback-Box-xxxx**. The setup page opens by itself
   (if not, open `http://192.168.4.1`).
3. Tap **Configure WiFi**, pick the shop's Wi-Fi, type its password, and fill in:
   - **Server address**: the address of the server (for example `https://feedback-box.<account>.workers.dev`)
   - **Box key**: the key made for this box (see "Adding a box" below)
4. Save. The LED stops blinking when the box is on the Wi-Fi.

To start setup again: **hold BOOT for 5 seconds** while the box is running. The LED flickers and the box restarts
in setup mode. (Do not hold BOOT while plugging in: that is the flashing mode.)

## What the LED means

| LED | Meaning |
|---|---|
| One long flash | Thank you: the press is saved |
| Slow blink | Setup mode |
| Three quick flashes every 4 s | The server does not know this box's key |
| Off | Ready |

## Wiring the real buttons

Each button connects between a pin and **GND**. No resistors: the board's own pull-ups are used.

| Button | Pin on the board |
|---|---|
| Happy | **25** (may be labelled D25 or IO25) |
| OK | **26** |
| Sad | **27** |
| all three | **GND** (the board has more than one GND pin) |

On each button's microswitch, clip one wire to **COM** and one to **NO**. If the switch has only two tabs, either way
round works. A press sends one rating; presses within 3 seconds of the last one are ignored, so one person
counts once.

## Parts (prices read from the product pages, 17 Nov 2026, BerryBase, EUR, before shipping)

| Part | Qty | Price | Page |
|---|---|---|---|
| Arcade Button, 30mm, transparent (colours: green, yellow, red...) | 3 | 1.40 each | berrybase.de/en/arcade-button-30mm-transparent/color-yellow |
| 10pin Jumper / Dupont Cable Female - crocodile clip, 20cm | 1 | 3.90 | berrybase.de/en/10pin-jumper-dupont-cable-female-crocodile-clip-separable-20cm |
| Universal housing, Euro Box, small, 95x135x45mm, grey | 1 | 6.90 | berrybase.de/en/universal-housing-euro-box-small-95x135x45mm-grey |
| **Total** | | **15.00** | shipping to Spain not checked |

The crocodile clips grip the button tabs and the other end plugs onto the board's pins, so there is no soldering
and no crimping. The box needs three 30 mm holes (a 30 mm hole saw or step drill), plus one for the USB cable.
The ESP32 DevKit itself is already at the office.

## How it behaves

- Every press gets a number that is kept across restarts. The box keeps up to 64 presses in its memory when the
  Wi-Fi is down and sends them, oldest first, when it is back. If the reply is lost and the box sends a press
  again, the server counts it once.
- A press older than 64 waiting ones is dropped (the oldest goes first).
- The time of a press is worked out from how long it waited, as long as the box was not restarted in between;
  after a restart, waiting presses are stamped with the time they arrive.
- HTTPS is checked against 150 root certificates built into the firmware (Mozilla's list, Espressif's list of
  retired roots that servers still use, and GlobalSign Root CA). Cloudflare's own sites need that last one.
- No Wi-Fi password, server address or key is in the code. They live in the board's memory after setup.

## The server (worker/)

- `POST /api/rating` with `Authorization: Bearer <box key>` and `{"rating": "happy|ok|sad|test", "seq": N, "age_s": N}`.
  201 stored, 200 already stored (resend), 400 bad body, 401 unknown key, 429 a second press from one box within 2 s.
- `GET /count?k=<view key>`: the count page. Any other key: 404.
- Keys are stored as SHA-256 hashes only.

### Running it locally with demo data

    cd worker && sh test/run_local.sh      # inside node:22; see the script header

It makes a local database, fills it with invented presses for two demo boxes, starts `wrangler dev` on
127.0.0.1, and runs `test/test_endpoint.py` (21 checks).

### Going live (needs Shaun's word: it is a new public endpoint)

    wrangler d1 create feedback-box                 # put the id in wrangler.toml
    wrangler d1 migrations apply feedback-box --remote
    wrangler secret put VIEW_KEY                    # a long random string; the count page link uses it
    wrangler deploy

### Adding a box

Make a key: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. Store only its hash:

    wrangler d1 execute feedback-box --remote --command \
      "INSERT INTO devices (id, name, key_hash) VALUES ('box-1', 'Office counter', '<sha256 of the key>')"

Type the key itself into the box's setup page.

## Not done in this prototype

- Not run on a real board yet (built and checked on the server only).
- No over-the-air updates: a new version means flashing over USB again.
- The setup Wi-Fi has no password while it is open. Anyone nearby could set the box up during those minutes.
- Secure boot and flash encryption are off (they are one-way switches; for a product, not a prototype).
