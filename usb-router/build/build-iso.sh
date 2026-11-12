#!/usr/bin/env bash
# Build the USB Router ISO with Debian live-build.
#
#   ./build/build-iso.sh            build on this machine (Debian/Ubuntu, needs root + live-build)
#   ./build/build-iso.sh --docker   build inside a privileged Debian container (any Linux host with Docker)
#   ./build/build-iso.sh --clean    remove the work directory first
#
# Output: build/out/usb-router-<date>.iso  (hybrid ISO: dd it to a stick, or use make-usb.sh)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$HERE/work"
OUT="$HERE/out"
DIST="${DIST:-bookworm}"
ARCH="${ARCH:-amd64}"
MIRROR="${MIRROR:-http://deb.debian.org/debian/}"

CLEAN=0; DOCKER=0
for a in "$@"; do
  case "$a" in
    --clean) CLEAN=1 ;;
    --docker) DOCKER=1 ;;
    *) echo "unknown option $a"; exit 1 ;;
  esac
done

if [ "$DOCKER" -eq 1 ]; then
  echo "==> building inside Docker (debian:$DIST, privileged — live-build needs loop/chroot)"
  docker build -t usb-router-builder -f "$HERE/Dockerfile.build" "$HERE"
  exec docker run --rm -it --privileged \
    -v "$ROOT":/src -w /src \
    -e DIST="$DIST" -e ARCH="$ARCH" -e MIRROR="$MIRROR" \
    usb-router-builder ./build/build-iso.sh $( [ "$CLEAN" -eq 1 ] && echo --clean )
fi

[ "$(id -u)" -eq 0 ] || { echo "live-build needs root: sudo $0 $*"; exit 1; }
command -v lb >/dev/null || { echo "install live-build first: apt install live-build"; exit 1; }

[ "$CLEAN" -eq 1 ] && rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
cd "$WORK"

echo "==> staging config"
rm -rf config auto
cp -r "$HERE/live/config" config
STAGE="config/includes.chroot/opt/usb-router"
mkdir -p "$STAGE"
cp -r "$ROOT/app" "$ROOT/browser" "$ROOT/scripts" "$ROOT/system" "$ROOT/config" "$STAGE/"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +

echo "==> lb config"
lb config noauto \
  --distribution "$DIST" \
  --architectures "$ARCH" \
  --archive-areas "main contrib non-free non-free-firmware" \
  --mirror-bootstrap "$MIRROR" --mirror-binary "$MIRROR" \
  --binary-images iso-hybrid \
  --debian-installer none \
  --firmware-chroot true \
  --firmware-binary true \
  --apt-recommends false \
  --iso-application "USB Router" \
  --iso-volume "USBROUTER-LIVE" \
  --iso-publisher "freebies/usb-router" \
  --bootappend-live "boot=live components quiet splash persistence persistence-label=persistence hostname=usb-router username=kiosk user-default-groups=audio,cdrom,dip,floppy,video,plugdev,netdev,powermgmt,scanner,bluetooth,sudo locales=en_GB.UTF-8 keyboard-layouts=gb" \
  --bootloaders "syslinux,grub-efi" \
  --memtest none \
  --security true --updates true

echo "==> lb build (this takes 20-60 minutes and ~6 GB of disk)"
lb build 2>&1 | tee "$OUT/build.log"

ISO="$(ls -t "$WORK"/live-image-*.hybrid.iso 2>/dev/null | head -1 || true)"
[ -n "$ISO" ] || { echo "build failed — see $OUT/build.log"; exit 1; }
NAME="usb-router-$(date +%Y%m%d).iso"
mv "$ISO" "$OUT/$NAME"
sha256sum "$OUT/$NAME" > "$OUT/$NAME.sha256"
echo
echo "==> done: $OUT/$NAME"
echo "    write it:   sudo ./build/make-usb.sh $OUT/$NAME /dev/sdX"
