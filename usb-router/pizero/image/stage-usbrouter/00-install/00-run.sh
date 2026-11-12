#!/bin/bash -e
# pi-gen sub-stage, runs on the build host. ROOTFS_DIR is the image's root.
# files/usb-router is filled by build-image.sh (app, pizero, config, system).
rm -rf "${ROOTFS_DIR}/tmp/usb-router"
cp -r files/usb-router "${ROOTFS_DIR}/tmp/usb-router"

on_chroot <<CHROOT
set -e
/tmp/usb-router/pizero/install-pizero.sh --no-systemd
# the services block of install-pizero.sh, without --now: no systemd runs in a chroot
for s in dphys-swapfile bluetooth hciuart triggerhappy; do
  systemctl disable "\$s" 2>/dev/null || true
done
systemctl enable zramswap NetworkManager usb-router-connect.service usb-router-kiosk.service
systemctl disable getty@tty1.service 2>/dev/null || true
# no login on the player: the build-time user is locked and SSH stays off (ENABLE_SSH=0).
# Settings are changed in usb-router.conf on the boot partition, from any computer.
passwd -l "${FIRST_USER_NAME}"
usermod -s /usr/sbin/nologin "${FIRST_USER_NAME}"
rm -rf /tmp/usb-router
CHROOT
