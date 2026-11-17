#!/bin/bash
# C&G #209 v8.2: boot like a set-top box. From power-on to the setup screen the screen shows no
# text, cursor, rainbow or console: black, then the ORIONPATH splash (Plymouth), then the page's
# own splash fading into the QR screen. The D key on the page still shows the diagnostics.
#
#   quiet-boot.sh ROOT BOOT     ROOT = the root file system (/ on the device, a mount when building
#                               an image), BOOT = the FAT boot partition (cmdline.txt, config.txt)
#
# Plymouth itself must already be installed (apt-get install --no-install-recommends plymouth).
# Safe to run twice: every edit checks first.
set -eu
ROOT=${1:?root file system}
BOOT=${2:?boot partition}
HERE=$(cd "$(dirname "$0")" && pwd)

[ -x "$ROOT/usr/bin/plymouth" ] || { echo "quiet-boot: plymouth is not installed in $ROOT" >&2; exit 1; }

# 1. the theme, as the default
T="$ROOT/usr/share/plymouth/themes/orionpath"
install -d -m 755 "$T"
for f in orionpath.plymouth orionpath.script bg.png text.png track.png fill.png; do
  install -m 644 "$HERE/splash/$f" "$T/$f"
done
rm -f "$T/logo.png"   # v8.2-v8.3 had a logo; v8.4 has none (Shaun 17 Nov 09:53 CET)
install -d -m 755 "$ROOT/etc/plymouth"
install -m 644 "$HERE/splash/plymouthd.conf" "$ROOT/etc/plymouth/plymouthd.conf"
# plymouth-set-default-theme only writes this link (without -R it leaves the initramfs alone)
ln -sfn orionpath/orionpath.plymouth "$ROOT/usr/share/plymouth/themes/default.plymouth"

# 2. the handoff: splash stays until the connection manager answers, then cog paints over it
install -d -m 755 "$ROOT/opt/usb-router/pizero"
# on the device this script may run from /opt/usb-router itself: install refuses a file onto itself
[ "$HERE/splash-handoff.sh" -ef "$ROOT/opt/usb-router/pizero/splash-handoff.sh" ] \
  || install -m 755 "$HERE/splash-handoff.sh" "$ROOT/opt/usb-router/pizero/splash-handoff.sh"
install -m 644 "$HERE/usb-router-splash.service" "$ROOT/etc/systemd/system/usb-router-splash.service"
install -m 644 "$HERE/usb-router-kiosk.service" "$ROOT/etc/systemd/system/usb-router-kiosk.service"
install -d -m 755 "$ROOT/etc/systemd/system/multi-user.target.wants"
ln -sfn /etc/systemd/system/usb-router-splash.service "$ROOT/etc/systemd/system/multi-user.target.wants/usb-router-splash.service"
install -d -m 755 "$ROOT/etc/systemd/system/plymouth-quit.service.d"
install -m 644 "$HERE/plymouth-quit-retain.conf" "$ROOT/etc/systemd/system/plymouth-quit.service.d/usb-router.conf"
# no splash on the way down: the page's "Safe to unplug" screen must stay the last picture
for u in plymouth-poweroff plymouth-reboot plymouth-halt plymouth-kexec; do
  ln -sfn /dev/null "$ROOT/etc/systemd/system/$u.service"
done
# nothing logs in on the screen's console
ln -sfn /dev/null "$ROOT/etc/systemd/system/getty@tty1.service"

# 3. the kernel and systemd say nothing on the screen (console moves to tty3, never shown)
C="$BOOT/cmdline.txt"
line=$(head -n1 "$C")
line=$(printf '%s' "$line" | sed -E 's/(^| )console=tty1( |$)/\1console=tty3\2/')
for w in quiet splash loglevel=3 logo.nologo vt.global_cursor_default=0 systemd.show_status=false \
         rd.udev.log_level=3 udev.log_level=3 plymouth.ignore-serial-consoles; do
  case " $line " in *" $w "*) ;; *) line="$line $w" ;; esac
done
case " $line " in *" console=tty3 "*) ;; *) line="$line console=tty3" ;; esac
printf '%s\n' "$line" > "$C.new" && mv "$C.new" "$C"

# 4. no rainbow square from the firmware
F="$BOOT/config.txt"
grep -q '^disable_splash=1' "$F" || printf '\n[all]\n# C&G #209 v8.2: no rainbow square at power-on\ndisable_splash=1\n' >> "$F"

echo "quiet-boot: done ($C: $(head -n1 "$C"))"
