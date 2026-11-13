#!/usr/bin/env bash
# Install the USB Router signage player on a Raspberry Pi Zero W
# (ARMv6, 512 MB) running Raspberry Pi OS Lite 32-bit (Bookworm).
#
#   sudo ./pizero/install-pizero.sh                install, enable services, take over tty1
#   sudo ./pizero/install-pizero.sh --no-kiosk     the connection manager only (no screen)
#   sudo ./pizero/install-pizero.sh --no-systemd   files and packages only (image build, container test)
#
# Run from the usb-router folder or anywhere inside it. Needs network for apt.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KIOSK=1; SYSTEMD=1
for a in "$@"; do
  case "$a" in
    --no-kiosk) KIOSK=0 ;;
    --no-systemd) SYSTEMD=0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
ARCH=$(dpkg --print-architecture)
[ "$ARCH" = "armhf" ] || echo "warning: architecture is $ARCH; this script is meant for Raspberry Pi OS 32-bit (armhf)"

echo "==> packages"
apt-get update
# the connection manager: same as the PC stick, all from apt (python3-flask is 2.2 on Bookworm; fine)
PKGS="network-manager dnsmasq-base iw wireless-regdb python3 python3-flask python3-qrcode python3-pil curl zram-tools"
# the screen: cog (WPE WebKit) on DRM, no X server, no window manager
[ "$KIOSK" -eq 1 ] && PKGS="$PKGS cog libgles2 libegl1 dbus-user-session fonts-dejavu-core"
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $PKGS

echo "==> users"
# kiosk runs the connection manager: drives NetworkManager (netdev) and holds the PIN and passwords
id kiosk >/dev/null 2>&1 || useradd -m -s /bin/bash kiosk
for g in netdev plugdev; do
  getent group "$g" >/dev/null && usermod -aG "$g" kiosk
done
# signage runs cog, which shows third-party web pages: the screen and input only.
# It talks to the manager over http://127.0.0.1:8080 like any browser; it cannot read
# /var/lib/usb-router (secrets) or drive NetworkManager.
id signage >/dev/null 2>&1 || useradd -m -s /bin/bash signage
for g in video render input audio; do
  getent group "$g" >/dev/null && usermod -aG "$g" signage
done

echo "==> files"
install -d /opt/usb-router /etc/usb-router /var/lib/usb-router
rm -rf /opt/usb-router/app /opt/usb-router/pizero
cp -r "$HERE/app" "$HERE/pizero" /opt/usb-router/
find /opt/usb-router -name __pycache__ -prune -exec rm -rf {} +
chown -R kiosk:netdev /var/lib/usb-router
chmod 750 /var/lib/usb-router
# settings live on the FAT boot partition, editable from any computer with the SD card in a reader
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
if [ ! -f "$BOOT/usb-router.conf" ] && [ ! -f /etc/usb-router/usb-router.conf ]; then
  cp "$HERE/config/usb-router.conf.example" "$BOOT/usb-router.conf"
  echo "    wrote $BOOT/usb-router.conf (edit it: homepage, wifi_ssid, fleet_url ...)"
fi
# FAT has no file owners: with Pi OS's 'defaults' every user reads usb-router.conf, and it holds the
# Wi-Fi passwords, the PIN and the fleet token. Mount the boot partition root:kiosk, files 0640, dirs
# 0750: the manager (kiosk) reads it, signage (the browser, shows third-party pages) cannot.
KIOSK_GID=$(getent group kiosk | cut -d: -f3)
if [ -n "$KIOSK_GID" ] && awk -v m="$BOOT" '$2==m && $3=="vfat"{f=1} END{exit !f}' /etc/fstab 2>/dev/null; then
  awk -v m="$BOOT" -v g="$KIOSK_GID" 'BEGIN{OFS="\t"}
    $2==m && $3=="vfat" { n=split($4,o,","); s="";
      for(i=1;i<=n;i++) if(o[i] !~ /^(uid|gid|umask|fmask|dmask)=/) s=s (s==""?"":",") o[i];
      $4=s (s==""?"":",") "uid=0,gid=" g ",fmask=0137,dmask=0027" } {print}' /etc/fstab > /etc/fstab.usb-router
  cat /etc/fstab.usb-router > /etc/fstab; rm -f /etc/fstab.usb-router
  echo "    $BOOT mounts root:kiosk 0640 (fstab), so signage cannot read usb-router.conf"
  if [ "$SYSTEMD" -eq 1 ] && mountpoint -q "$BOOT"; then
    systemctl daemon-reload
    umount "$BOOT" && mount "$BOOT" || echo "warning: remount of $BOOT failed; the new mask applies at next boot"
  fi
else
  echo "warning: no vfat line for $BOOT in /etc/fstab: every user can read $BOOT/usb-router.conf (passwords, PIN, fleet token)"
fi
install -m 644 "$HERE/system/usb-router-connect.service" /etc/systemd/system/
install -d /etc/polkit-1/rules.d /etc/NetworkManager/conf.d
install -m 644 "$HERE/system/50-usb-router-networkmanager.rules" /etc/polkit-1/rules.d/
install -m 644 "$HERE/system/usb-router-networkmanager.conf" /etc/NetworkManager/conf.d/usb-router.conf
[ "$KIOSK" -eq 1 ] && install -m 644 "$HERE/pizero/usb-router-kiosk.service" /etc/systemd/system/

echo "==> memory (512 MB)"
# compressed swap in RAM instead of a swap file on the SD card
cat > /etc/default/zramswap <<'ZRAM'
# usb-router (pizero): half the RAM as lz4-compressed swap
ALGO=lz4
PERCENT=50
PRIORITY=100
ZRAM

if [ "$SYSTEMD" -eq 1 ]; then
  echo "==> services"
  # nothing on a signage player needs these; they cost RAM on a 512 MB board
  for s in dphys-swapfile bluetooth hciuart triggerhappy; do
    systemctl disable --now "$s" 2>/dev/null || true
  done
  systemctl daemon-reload
  systemctl enable --now zramswap NetworkManager usb-router-connect.service
  if [ "$KIOSK" -eq 1 ]; then
    systemctl disable getty@tty1.service 2>/dev/null || true
    systemctl enable usb-router-kiosk.service
    echo "    the kiosk takes over the screen at next boot (or: systemctl start usb-router-kiosk)"
  fi
fi

if [ -f "$BOOT/config.txt" ] && ! grep -q '^dtoverlay=vc4-kms-v3d' "$BOOT/config.txt"; then
  echo "warning: $BOOT/config.txt has no dtoverlay=vc4-kms-v3d; cog needs the KMS driver for the screen"
fi

echo
echo "Done. Web UI: http://127.0.0.1:8080   Settings: $BOOT/usb-router.conf"
echo "Logs: journalctl -u usb-router-connect -u usb-router-kiosk -f"
