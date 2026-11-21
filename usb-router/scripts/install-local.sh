#!/usr/bin/env bash
# Install USB Router onto an existing Debian/Ubuntu machine (for testing without
# building the ISO, or for a mini PC / Raspberry Pi that should behave like the stick).
#
#   sudo ./scripts/install-local.sh            install + enable services
#   sudo ./scripts/install-local.sh --no-x     install the web app only (no browser autostart)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
NO_X=0; [ "${1:-}" = "--no-x" ] && NO_X=1

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "==> packages"
apt-get update
PKGS="network-manager dnsmasq-base nftables iw wireless-regdb python3 python3-flask python3-qrcode python3-pil curl"
[ "$NO_X" -eq 0 ] && PKGS="$PKGS xserver-xorg xinit openbox unclutter-xfixes x11-xserver-utils python3-pyqt6 python3-pyqt6.qtwebengine fonts-dejavu fonts-noto-core"
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $PKGS

echo "==> user"
id kiosk >/dev/null 2>&1 || useradd -m -s /bin/bash kiosk
usermod -aG netdev,video,audio,plugdev kiosk

echo "==> files"
install -d /opt/usb-router /etc/usb-router /var/lib/usb-router
cp -r "$HERE/app" "$HERE/browser" "$HERE/scripts" /opt/usb-router/
chmod +x /opt/usb-router/scripts/usb-router-session
[ -f /etc/usb-router/usb-router.conf ] || cp "$HERE/config/usb-router.conf.example" /etc/usb-router/usb-router.conf
chown -R kiosk:netdev /var/lib/usb-router
install -m 644 "$HERE/system/usb-router-connect.service" /etc/systemd/system/
install -m 644 "$HERE/system/config.mount" /etc/systemd/system/
# 40-, before Debian's pkla compat rule (49-), which refuses outside a session (S&D #191)
rm -f /etc/polkit-1/rules.d/50-usb-router-networkmanager.rules
install -m 644 "$HERE/system/40-usb-router-networkmanager.rules" /etc/polkit-1/rules.d/
install -m 644 "$HERE/system/usb-router-networkmanager.conf" /etc/NetworkManager/conf.d/usb-router.conf
# C&G #281: the LAN guard for the sharing hotspot (guests reach the internet only), loaded by NetworkManager as root
install -d /etc/NetworkManager/dispatcher.d
install -m 755 "$HERE/system/usb-router-nm-dispatcher" /etc/NetworkManager/dispatcher.d/50-usb-router-lan-guard
chmod 755 /opt/usb-router/scripts/usb-router-lan-guard
install -d /etc/systemd/journald.conf.d
install -m 644 "$HERE/system/usb-router-journald.conf" /etc/systemd/journald.conf.d/usb-router.conf
# captive portal (S&D #193): NetworkManager's hotspot dnsmasq reads the app's file through this link
install -d /etc/NetworkManager/dnsmasq-shared.d
[ -e /var/lib/usb-router/captive-dns.conf ] || : > /var/lib/usb-router/captive-dns.conf
chown kiosk:netdev /var/lib/usb-router/captive-dns.conf 2>/dev/null || true
chmod 644 /var/lib/usb-router/captive-dns.conf
ln -sfn /var/lib/usb-router/captive-dns.conf /etc/NetworkManager/dnsmasq-shared.d/usb-router-captive.conf
install -m 644 -o kiosk -g kiosk "$HERE/scripts/bash_profile" /home/kiosk/.bash_profile

echo "==> services"
systemctl daemon-reload
systemctl enable --now NetworkManager usb-router-connect.service
systemctl enable config.mount 2>/dev/null || true

if [ "$NO_X" -eq 0 ]; then
  echo "==> autologin on tty1"
  install -d /etc/systemd/system/getty@tty1.service.d
  cat > /etc/systemd/system/getty@tty1.service.d/override.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin kiosk --noclear %I $TERM
UNIT
  systemctl daemon-reload
  systemctl restart getty@tty1 || true
fi

echo
echo "Done. Web UI: http://127.0.0.1:8080"
echo "Browser:    sudo -u kiosk DISPLAY=:0 python3 /opt/usb-router/browser/browser.py"
