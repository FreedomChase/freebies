# Hardware for USB Router

## The USB stick

Any stick works, but boot and persistence speed depend almost entirely on
random-write performance, which cheap sticks are terrible at.

| Pick | Why |
|---|---|
| **Samsung FIT Plus 64 GB** or **SanDisk Ultra Fit 64 GB** (USB 3.1) | Tiny, can stay plugged in permanently, fast enough. 32 GB is plenty; 64 GB costs the same. |
| **Samsung BAR Plus 64 GB** | Metal body, better heat handling if the machine runs 24/7. |
| For a fleet: a **USB-to-M.2 / NVMe enclosure** with a cheap 128 GB SSD | Behaves like a real disk; persistence feels like an installed system. ~3x the price. |

Minimum 8 GB. Avoid USB 2.0 sticks and anything unbranded.

## Wi-Fi adapters (for the hotspot)

If the PC's own Wi-Fi card is used to get online, the stick needs a second
adapter to broadcast a hotspot. It must support **AP mode** with an **in-kernel
Linux driver**; many consumer dongles do not. These are known to work with
Debian 12's kernel without extra drivers:

| Adapter | Chipset / driver | Band | Notes |
|---|---|---|---|
| **ALFA AWUS036ACM** | MT7612U / `mt76` | 2.4 + 5 GHz, AC1200 | Best choice. Solid AP mode, external antennas, good range. |
| **ALFA AWUS036AXML** | MT7921AU / `mt7921u` | Wi-Fi 6 | Needs kernel 6.1+ (Debian 12 ok). Newer, more expensive. |
| **Panda PAU09** | RT5572 / `rt2800usb` | 2.4 + 5 GHz, N600 | Reliable budget option; the driver has supported AP mode for years. |
| **Panda PAU06** | RT5372 / `rt2800usb` | 2.4 GHz, N300 | Cheapest working option. |
| **TP-Link Archer T2U Plus / T3U** | RTL8821AU/RTL8812BU | AC | AP mode works but may need `rtl8821au-dkms` / `rtl88x2bu-dkms`; verify the hardware revision. |
| Edimax EW-7811Un **v1** | RTL8188CUS / `rtl8192cu` | 2.4 GHz, N150 | Tiny; AP mode works but only ~5 clients comfortably. v2 (RTL8188EUS) is worse. |

Rule of thumb: **MediaTek (mt76) or Ralink (rt2800usb) chipsets work; Realtek is
a gamble; Broadcom USB does not do AP mode.** When in doubt, search the chipset
plus "AP mode linux" before ordering, and check the revision printed on the box.

Check an adapter from the stick's terminal (Ctrl+Alt+T): `iw list | grep -A8 "Supported interface modes"` must list `* AP`.

Built-in laptop cards (Intel `iwlwifi`, most Qualcomm `ath9k`/`ath10k`) do AP
mode fine, so a laptop online over Ethernet needs no extra hardware.

## Ethernet

A **USB 3.0 Gigabit adapter with a Realtek RTL8153 or ASIX AX88179** chipset
(TP-Link UE300, Anker, UGREEN, Cable Matters) adds a wired port for either the
uplink or wired sharing. All are supported out of the box.

## Mobile broadband

USB 4G/5G dongles that present as a network card (Huawei E3372h **HiLink**, ZTE
MF833, most "USB Wi-Fi stick" style modems from carriers) just work: they appear
as Ethernet. Dongles in **Stick** mode (`E3372s`) go through ModemManager, which
is installed, but need an APN and sometimes a SIM PIN, so prefer HiLink.

## The PC

Anything x86-64 that boots from USB. Secure Boot may need to be disabled for the
live image. For a permanent router, a fanless mini-PC with two Ethernet ports
and an Intel Wi-Fi card (many N100 boxes) gives you wired WAN, wired LAN and a
hotspot with no extra adapters. Intel Wi-Fi cards only allow 5 GHz AP mode on
certain channels due to regulatory rules; leave `hotspot_band = bg` for those.

Raspberry Pi is a different (ARM) image and is not covered by `build-iso.sh`;
`scripts/install-local.sh` on Raspberry Pi OS gets you the same software.

## Shopping list for one unit

| Item | Approx. |
|---|---|
| Samsung FIT Plus 64 GB | £12 / $15 |
| ALFA AWUS036ACM (only needed when the uplink is Wi-Fi) | £35 / $40 |
| USB 3 Gigabit Ethernet adapter (optional) | £12 / $15 |

Prices are indicative; verify current pricing and the chipset revision before
ordering.
