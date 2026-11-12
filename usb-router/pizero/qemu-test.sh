#!/usr/bin/env bash
# Test the Pi Zero W build on a Linux box, without a Pi.
#
#   ./pizero/qemu-test.sh /path/to/qemu-arm-static
#
# Runs a throwaway Raspbian Bookworm armhf container (the ARMv6 build of Debian
# that Raspberry Pi OS 32-bit is made from), installs the player with
# install-pizero.sh, then runs everything that matters under qemu emulating the
# Pi Zero's own CPU (ARM1176, ARMv6 + VFP): an ARMv7-only instruction anywhere
# crashes the run instead of passing silently.
#
#   1. the pytest suite, on the system python3 and Flask from apt
#   2. the connection manager (mock NetworkManager) on :8080
#   3. cog with its headless platform loading the setup page and the
#      "content unavailable" page; the server log must show WebKit fetching
#      the page, its stylesheet and its script
#   4. the kiosk controller against the live manager, with headless cog
#   5. memory: cog + WebKit processes run once natively (not emulated) and
#      their RSS is summed, as an estimate for the 512 MB board
#
# Needs docker that can run linux/arm/v6 images (an arm64 host runs them
# natively; an x86 host needs binfmt for arm) and a static qemu-arm for the
# host (Debian/Ubuntu: apt-get download qemu-user-static; dpkg -x ...).
# Demo data only. Nothing leaves the container; it is removed at the end.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
QEMU="${1:?usage: $0 /path/to/qemu-arm-static}"
IMAGE="${PIZERO_TEST_IMAGE:-balenalib/raspberry-pi-debian:bookworm}"
[ -x "$QEMU" ] || { echo "not executable: $QEMU"; exit 2; }
exec docker run --rm --platform linux/arm/v6 \
  -v "$HERE":/src:ro -v "$(readlink -f "$QEMU")":/usr/local/bin/qemu-arm-static:ro \
  "$IMAGE" bash /src/pizero/qemu-test-inside.sh
