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
PKGS="network-manager dnsmasq-base iw wireless-regdb python3 python3-flask curl"
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
install -m 644 "$HERE/system/50-usb-router-networkmanager.rules" /etc/polkit-1/rules.d/
install -m 644 "$HERE/system/usb-router-networkmanager.conf" /etc/NetworkManager/conf.d/usb-router.conf
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
