# Hardware for a display network

## The one thing to know first

**A monitor or LCD panel cannot boot a USB stick.** It has no CPU. The software
on this stick needs a small computer plugged into the screen's HDMI port. So
"the USB device to order" is really two things:

1. a **HDMI compute stick** (a PC the size of a USB stick) for each screen, and
2. the **USB flash drive** that carries this software, one per screen or one
   per installer, depending on how you deploy (see below).

Commercial displays with a built-in player (Samsung Tizen, LG webOS, Philips
Android SoC) cannot run this either. For those, point their own browser at the
same content URL, and use the fleet dashboard only for the compute-stick screens.

## HDMI compute sticks (one per screen)

Fanless x86-64, HDMI out, Wi-Fi, at least 4 GB RAM. All of these boot Debian
from USB and have Intel Wi-Fi (`iwlwifi`), which works out of the box and can
run the setup hotspot.

| Model | Notes |
|---|---|
| **MeLE PCG02 Pro / PCG02 (N4000, N5105, N100)** | Fanless, 8 GB RAM on the Pro, two USB-A ports plus USB-C power, runs cool 24/7. Best value. |
| **Azulle Access4 / Access Pro** | Same class, Ethernet port on the Pro models (handy for wired venues). Pricier. |
| **Intel Compute Stick STK2m3W64CC** | Discontinued; fine second-hand. Avoid the Atom "STK1" models (2 GB RAM is too little for Chromium). |
| **Mini PC with HDMI (Beelink, GMKtec N100)** | Not stick-shaped, but with 2 Ethernet ports and VESA-mountable; use when a screen needs wired LAN or sharing. |

Order with the stick:

- A **5 V/3 A USB power supply** per screen. Monitor USB ports give 0.9 A and
  will brown out the stick. TVs with a USB 3 port sometimes manage; do not rely on it.
- A short **HDMI extender cable** if the stick will not fit behind the screen.
- In the stick's BIOS set **Power On After Power Loss / AC Recovery = On** so
  screens come back after a power cut without anyone pressing a button.

## USB flash drives

| Pick | Why |
|---|---|
| **Samsung FIT Plus 32/64 GB** or **SanDisk Ultra Fit** | Tiny, stays plugged in behind the screen, fast random writes. |
| Anything branded, USB 3, 16 GB+ | Works; cheaper sticks boot slower and wear faster. |

Minimum 8 GB. Avoid USB 2.0 and unbranded drives.

### Two ways to deploy

- **Boot from the USB drive** (simplest): one flash drive per screen stays
  plugged into the compute stick permanently. Nothing on the compute stick is
  touched.
- **Install to the compute stick's internal storage** (cleaner): boot once from
  the flash drive, open a terminal (Ctrl+Alt+T) and run
  `sudo /opt/usb-router/build/make-usb.sh /config/usb-router.iso /dev/mmcblk0`
  (put the ISO on the `USBROUTER` partition first; make it 2 GB with the third
  argument of `make-usb.sh`). The compute stick then boots on its own and the
  flash drive moves on to the next screen.

## Wi-Fi adapters

For a signage screen the built-in Intel card does everything: connects to the
venue Wi-Fi, and broadcasts the setup hotspot while offline. You only need an
extra adapter when a screen should also **share** internet to nearby devices
while itself online over Wi-Fi (one card cannot do both). Then add an
AP-capable USB adapter with an in-kernel driver:

| Adapter | Chipset / driver | Notes |
|---|---|---|
| **ALFA AWUS036ACM** | MT7612U / `mt76` | Best. Dual band, external antennas. |
| **Panda PAU09** | RT5572 / `rt2800usb` | Reliable budget choice. |
| **Panda PAU06** | RT5372 / `rt2800usb` | Cheapest working option, 2.4 GHz only. |

MediaTek (`mt76`) and Ralink (`rt2800usb`) work; Realtek is a gamble; Broadcom
USB does not do AP mode. Check with `iw list | grep -A8 "Supported interface modes"`
on the stick; it must list `* AP`. Verify the chipset revision before ordering.

## Wired and mobile

- **USB 3 Gigabit adapter** (RTL8153 or AX88179: TP-Link UE300, UGREEN, Anker)
  for venues that give you a cable. Supported out of the box.
- **4G/5G USB dongle in HiLink mode** (Huawei E3372h, ZTE MF833) for screens
  with no venue network. Appears as Ethernet; no setup needed beyond the SIM.

## Screens

Any HDMI display. For portrait, set `rotation = left` or `right`. Panels with
speakers get audio over HDMI automatically. If a screen has a "no signal"
power-down timer, turn it off; the stick never blanks the output but some
displays sleep anyway during the boot's black frames.

## Shopping list per screen

| Item | Approx. |
|---|---|
| MeLE PCG02 Pro (N4000/N5105) | £100-£150 / $120-$180 |
| 5 V/3 A USB power supply | £8 / $10 |
| Samsung FIT Plus 32 GB | £8 / $10 |
| ALFA AWUS036ACM (only for screens that must also share internet over Wi-Fi) | £35 / $40 |

Prices are indicative; check current pricing and that the compute stick ships
with the RAM you expect.
