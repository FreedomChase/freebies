#!/usr/bin/env bash
# Write the ISO to a USB stick and add the two extra partitions the stick uses:
#
#   partition 1-2  the ISO (read-only live system, BIOS + UEFI bootable)
#   partition 3    "persistence" (ext4)  saved Wi-Fi networks, settings, downloads
#   partition 4    "USBROUTER"   (FAT32) usb-router.conf — editable from Windows/macOS
#
#   sudo ./build/make-usb.sh build/out/usb-router-YYYYMMDD.iso /dev/sdX [config-size-MB]
#
# THIS ERASES THE WHOLE STICK. It refuses to touch anything that looks like a system disk.
set -euo pipefail
ISO="${1:-}"; DEV="${2:-}"; CFG_MB="${3:-256}"
[ -n "$ISO" ] && [ -n "$DEV" ] || { echo "usage: $0 <iso> </dev/sdX> [config-size-MB]"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -f "$ISO" ] || { echo "no such ISO: $ISO"; exit 1; }
[ -b "$DEV" ] || { echo "not a block device: $DEV"; exit 1; }
case "$DEV" in /dev/sd[a-z]|/dev/nvme[0-9]n[0-9]|/dev/mmcblk[0-9]) ;; *) echo "refusing: $DEV is a partition or unusual device"; exit 1 ;; esac

# safety: never write to a disk that holds a mounted filesystem (e.g. the one you booted from)
if lsblk -nro MOUNTPOINT "$DEV" | grep -q .; then
  echo "refusing: $DEV has mounted partitions:"; lsblk "$DEV"; exit 1
fi
HERE="$(cd "$(dirname "$0")/.." && pwd)"
for t in sgdisk partprobe mkfs.ext4 mkfs.vfat; do command -v "$t" >/dev/null || { echo "missing tool: $t (apt install gdisk parted e2fsprogs dosfstools)"; exit 1; }; done

echo "About to ERASE $DEV:"; lsblk -o NAME,SIZE,MODEL,TRAN "$DEV"
read -r -p "Type the device name ($DEV) to confirm: " CONFIRM
[ "$CONFIRM" = "$DEV" ] || { echo "aborted"; exit 1; }

echo "==> writing ISO"
dd if="$ISO" of="$DEV" bs=4M status=progress conv=fsync
sync; partprobe "$DEV"; sleep 2

echo "==> adding partitions"
# the hybrid ISO ends after its own partitions; everything after is free space
sgdisk -e "$DEV" >/dev/null 2>&1 || true
END_MB=$(( $(blockdev --getsize64 "$DEV") / 1024 / 1024 ))
sgdisk -n 3:0:-"${CFG_MB}M" -t 3:8300 -c 3:persistence "$DEV"
sgdisk -n 4:0:0 -t 4:0700 -c 4:USBROUTER "$DEV"
partprobe "$DEV"; sleep 2
P3="$(lsblk -nrpo NAME "$DEV" | sed -n '4p')"; P4="$(lsblk -nrpo NAME "$DEV" | sed -n '5p')"
[ -b "$P3" ] && [ -b "$P4" ] || { echo "could not find new partitions"; lsblk "$DEV"; exit 1; }

echo "==> formatting $P3 (persistence) and $P4 (USBROUTER)"
mkfs.ext4 -q -F -L persistence "$P3"
mkfs.vfat -F 32 -n USBROUTER "$P4" >/dev/null

MNT="$(mktemp -d)"
mount "$P3" "$MNT"; echo "/ union" > "$MNT/persistence.conf"; umount "$MNT"
mount "$P4" "$MNT"
cp "$HERE/config/usb-router.conf.example" "$MNT/usb-router.conf"
cp "$HERE/README.md" "$MNT/README.md"
umount "$MNT"; rmdir "$MNT"; sync

echo
echo "==> done. Boot a PC from $DEV. Edit usb-router.conf on the USBROUTER drive to set the homepage,"
echo "    hotspot name/password and kiosk mode. Disk size used: ${END_MB} MB total."
