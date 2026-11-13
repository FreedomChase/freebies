#!/usr/bin/env bash
# The boot-partition mask that install-pizero.sh writes to /etc/fstab, exercised on a real vfat mount:
# the manager's group reads usb-router.conf, any other user cannot. Linux host, root (loop mount).
# It creates no users: numeric ids stand in for kiosk and signage (setpriv).
#   sudo ./pizero/fat-mask-test.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo): it loop-mounts a small vfat image"; exit 2; }
KG=64900; SIG=64901            # stand-ins: the kiosk group, the signage user (not in that group)
OPTS="uid=0,gid=$KG,fmask=0137,dmask=0027"   # the same string install-pizero.sh appends
grep -q "uid=0,gid=\" g \",fmask=0137,dmask=0027" "$(dirname "$0")/install-pizero.sh" \
  || { echo "FAIL  install-pizero.sh no longer writes $OPTS (keep this test in step)"; exit 1; }
FAIL=0; pass() { echo "PASS  $*"; }; fail() { echo "FAIL  $*"; FAIL=1; }
W=$(mktemp -d); chmod 755 "$W"; trap 'umount "$W/m" 2>/dev/null; rm -rf "$W"' EXIT
truncate -s 8M "$W/boot.img" && mkfs.vfat -n BOOTFS "$W/boot.img" >/dev/null && mkdir "$W/m" || { echo "FAIL  mkfs.vfat"; exit 1; }

# 1. Pi OS default mount: the hole dev4 found on S&D 182
mount -o loop,defaults "$W/boot.img" "$W/m" && printf 'wifi_password = s3cret\n' > "$W/m/usb-router.conf"; umount "$W/m"
mount -o loop,defaults "$W/boot.img" "$W/m"
setpriv --reuid=$SIG --regid=$SIG --clear-groups cat "$W/m/usb-router.conf" >/dev/null 2>&1 \
  && pass "red control: with 'defaults' another user reads the conf" || fail "red control: defaults already blocks (test is not testing)"
umount "$W/m"

# 2. the installer's mask
mount -o "loop,defaults,$OPTS" "$W/boot.img" "$W/m" || { echo "FAIL  mount with $OPTS"; exit 1; }
echo "      $(stat -c '%A %u:%g %n' "$W/m/usb-router.conf")"
setpriv --reuid=$SIG --regid=$SIG --clear-groups cat "$W/m/usb-router.conf" >/dev/null 2>&1 \
  && fail "signage stand-in reads the conf" || pass "signage stand-in cannot read the conf"
setpriv --reuid=$SIG --regid=$SIG --clear-groups ls "$W/m" >/dev/null 2>&1 \
  && fail "signage stand-in lists the boot partition" || pass "signage stand-in cannot list the boot partition"
setpriv --reuid=64902 --regid=64902 --groups=$KG cat "$W/m/usb-router.conf" 2>/dev/null | grep -q s3cret \
  && pass "a member of the kiosk group reads the conf" || fail "kiosk group cannot read the conf"
setpriv --reuid=64902 --regid=64902 --groups=$KG sh -c "echo x >> '$W/m/usb-router.conf'" 2>/dev/null \
  && fail "kiosk group can write the conf" || pass "kiosk group cannot write the conf (root edits it, or a card reader)"
cat "$W/m/usb-router.conf" | grep -q s3cret && pass "root reads the conf" || fail "root cannot read the conf"
[ $FAIL -eq 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $FAIL
