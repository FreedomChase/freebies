#!/usr/bin/env bash
# Build the flashable Pi Zero W image with pi-gen, on an arm64 Debian 12 box.
# armhf runs natively on arm64 (Neoverse N1 has AArch32), so no qemu is needed.
#
#   sudo ./pizero/image/build-image.sh <out dir>
#
# Output: <out dir>/*-usbrouter.img.xz, its .sha256, and build.log.
set -euo pipefail
PIGEN_TAG=2026-10-06-raspios-bookworm-armhf
PIGEN_SHA=4f615a1f3332d54d61fc5d764e5ab30754aee137
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"          # the usb-router folder
OUT="$(mkdir -p "${1:?usage: build-image.sh <out dir>}" && cd "$1" && pwd)"
W=/srv/pi-gen

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "build on arm64: armhf runs natively there"; exit 1; }
[ "$(getconf PAGESIZE)" = 4096 ] || { echo "pi-gen needs a 4k page kernel for armhf"; exit 1; }

echo "==> build dependencies"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  quilt parted coreutils qemu-user-static debootstrap zerofree zip dosfstools e2fsprogs \
  libcap2-bin libarchive-tools grep rsync xz-utils curl xxd file git kmod bc gpg pigz \
  arch-test ca-certificates
arch-test -n armhf || { echo "this CPU cannot run armhf natively"; exit 1; }

echo "==> pi-gen $PIGEN_TAG"
rm -rf "$W"
git clone --quiet --depth 1 --branch "$PIGEN_TAG" https://github.com/RPi-Distro/pi-gen "$W"
[ "$(git -C "$W" rev-parse HEAD)" = "$PIGEN_SHA" ] || { echo "pi-gen tag moved: expected $PIGEN_SHA"; exit 1; }

echo "==> our stage"
cp -r "$HERE/stage-usbrouter" "$W/"
F="$W/stage-usbrouter/00-install/files/usb-router"
mkdir -p "$F"
cp -r "$SRC/app" "$SRC/pizero" "$SRC/config" "$SRC/system" "$F/"
rm -rf "$F/pizero/image"
find "$F" -name __pycache__ -prune -exec rm -rf {} +
touch "$W/stage2/SKIP_IMAGES"            # export only our image, not plain Lite
PASS="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
sed "s|@PASS@|$PASS|" "$HERE/pigen-config" > "$W/config"
unset PASS

echo "==> build (about an hour)"
cd "$W"
./build.sh 2>&1 | tee "$OUT/build.log"

cp deploy/*-usbrouter.img.xz "$OUT/"
cd "$OUT"
sha256sum ./*-usbrouter.img.xz | tee "$(ls ./*-usbrouter.img.xz | head -1).sha256"
ls -l "$OUT"
