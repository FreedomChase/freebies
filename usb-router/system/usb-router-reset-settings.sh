#!/bin/sh
# C&G #270: put a file called reset-settings.txt on the boot drive (the one you edit usb-router.conf on)
# and the next start forgets every setting saved on the screen or a phone (settings.json), once, so
# usb-router.conf counts again. The file is renamed reset-settings.done, so it does not repeat.
# Notepad's "reset-settings.txt.txt" and any upper/lower case count too (FAT does not care).
# usage: usb-router-reset-settings.sh [STATE_DIR [BOOT_DIR...]]
STATE=${1:-/var/lib/usb-router}
[ $# -gt 0 ] && shift
[ $# -gt 0 ] || set -- /boot/firmware /config
for d in "$@"; do
  [ -d "$d" ] || continue
  for f in $(find "$d" -maxdepth 1 -type f -iname 'reset-settings.txt*' 2>/dev/null); do
    rm -f "$STATE/settings.json"
    mv -f "$f" "$d/reset-settings.done" 2>/dev/null || rm -f "$f"
    echo "usb-router: $(basename "$f") found on $d, settings saved on the screen cleared"
  done
done
exit 0
