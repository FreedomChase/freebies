#!/usr/bin/env bash
# Runs inside the armhf container started by qemu-test.sh. Do not run on a real machine.
set -uo pipefail
[ -f /.dockerenv ] || { echo "refusing: only inside the qemu-test.sh container"; exit 2; }
export DEBIAN_FRONTEND=noninteractive
EMU="qemu-arm-static -cpu arm1176"
FAIL=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAIL=1; }

echo "== system"
echo "arch $(dpkg --print-architecture), $(. /etc/os-release; echo "$PRETTY_NAME"), host cpu $(uname -m)"
$EMU /bin/true && pass "qemu runs as ARM1176 ($($EMU --version | head -1))" || { fail "qemu"; exit 1; }

echo "== install (install-pizero.sh --no-systemd)"
cp -r /src /tmp/usb-router
rm -rf /tmp/usb-router/build /tmp/usb-router/docs
if bash /tmp/usb-router/pizero/install-pizero.sh --no-systemd > /tmp/install.log 2>&1; then
  pass "install-pizero.sh"
else
  fail "install-pizero.sh (tail below)"; tail -20 /tmp/install.log; exit 1
fi
apt-get install -y -qq --no-install-recommends python3-pytest procps >/dev/null 2>&1
echo "cog $(dpkg-query -W -f='${Version}' cog), wpewebkit $(dpkg-query -W -f='${Version}' libwpewebkit-1.1-0), python3 $(dpkg-query -W -f='${Version}' python3), flask $(dpkg-query -W -f='${Version}' python3-flask)"

echo "== 1. pytest under ARM1176"
cd /tmp/usb-router
if USB_ROUTER_MOCK=1 $EMU /usr/bin/python3 -m pytest -q -p no:cacheprovider tests/ > /tmp/pytest.log 2>&1; then
  pass "pytest: $(tail -1 /tmp/pytest.log)"
else
  fail "pytest: $(tail -1 /tmp/pytest.log)"; grep -E "^(FAILED|ERROR)" /tmp/pytest.log | head -20
fi

echo "== 2. connection manager under ARM1176"
install -d -o kiosk /tmp/state
su kiosk -s /bin/sh -c "cd /opt/usb-router && USB_ROUTER_MOCK=1 USB_ROUTER_STATE_DIR=/tmp/state \
  exec $EMU /usr/bin/python3 app/server.py" > /tmp/server.log 2>&1 &
SERVER=$!
for i in $(seq 1 90); do curl -fs http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break; sleep 1; done
curl -fs http://127.0.0.1:8080/healthz >/dev/null && pass "server up after ${i}s, /healthz 200" || { fail "server"; tail -20 /tmp/server.log; exit 1; }
# fetch first, then grep: with pipefail, grep -q closing the pipe early makes curl exit 23 (SIGPIPE) at random
setup=$(curl -fs http://127.0.0.1:8080/); rc=$?
[ $rc -eq 0 ] && grep -q "USB Router" <<<"$setup" && pass "setup page served" || fail "setup page (curl rc $rc, $(printf %s "$setup" | wc -c) bytes)"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/static/unavailable.html?u=x&s=pz&r=5")
[ "$code" = 200 ] && pass "unavailable.html 200" || fail "unavailable.html $code"

echo "== 3. cog (WPE WebKit) headless, every process under ARM1176"
# cog starts WPEWebProcess and WPENetworkProcess itself; a child's execve would run
# natively, so in this throwaway container each one is wrapped to run under qemu too.
for b in $(find /usr/lib/arm-linux-gnueabihf/wpe-webkit-1.1 -maxdepth 1 -type f -name 'WPE*Process'); do
  mv "$b" "$b.real"
  printf '#!/bin/sh\nexec qemu-arm-static -cpu arm1176 %s.real "$@"\n' "$b" > "$b"; chmod 755 "$b"
done
ls /usr/lib/arm-linux-gnueabihf/wpe-webkit-1.1/ | tr '\n' ' '; echo
export XDG_RUNTIME_DIR=/tmp/xdg; install -d -m 700 -o kiosk /tmp/xdg
: > /tmp/cog.log
su kiosk -s /bin/sh -c "XDG_RUNTIME_DIR=/tmp/xdg timeout 240 dbus-run-session -- $EMU /usr/bin/cog --platform=headless \
  'http://127.0.0.1:8080/static/unavailable.html?u=https%3A%2F%2Fsignage.example.com&s=pz-1&r=5'" >> /tmp/cog.log 2>&1 &
COG=$!
for i in $(seq 1 240); do grep -q "GET /static/unavailable.js" /tmp/server.log && break; sleep 1; done
grep -q "GET /static/unavailable.html" /tmp/server.log && pass "WebKit fetched unavailable.html" || fail "no unavailable.html fetch"
grep -q "GET /static/style.css" /tmp/server.log && pass "WebKit fetched style.css (HTML parsed)" || fail "no style.css fetch"
grep -q "GET /static/unavailable.js" /tmp/server.log && pass "WebKit fetched unavailable.js after ${i}s" || fail "no unavailable.js fetch"
ps -eo args | grep -E "qemu-arm-static -cpu arm1176 .*(cog|WPEWebProcess|WPENetworkProcess)" | grep -v grep | sed 's/^/      /' | cut -c1-120
kill $COG 2>/dev/null; pkill -f WPEWebProcess; pkill -f WPENetworkProcess; pkill -f "bin/cog" ; sleep 2

echo "== 4. kiosk controller + headless cog against the live manager"
: > /tmp/kiosk.log
su kiosk -s /bin/sh -c "XDG_RUNTIME_DIR=/tmp/xdg USB_ROUTER_COG_PLATFORM=headless timeout 300 dbus-run-session -- \
  $EMU /usr/bin/python3 /opt/usb-router/pizero/kiosk.py --once" > /tmp/kiosk.log 2>&1
rc=$?
grep -q "starting cog" /tmp/kiosk.log && pass "kiosk started cog: $(grep -o 'starting cog: .*' /tmp/kiosk.log | head -1 | cut -c1-110)" || fail "kiosk did not start cog"
[ $rc -eq 0 ] && pass "kiosk --once exit 0" || { fail "kiosk exit $rc"; tail -15 /tmp/kiosk.log; }
# cogctl reaches the running cog over the session bus (open a URL, then ping)
su kiosk -s /bin/sh -c "XDG_RUNTIME_DIR=/tmp/xdg timeout 240 dbus-run-session -- sh -c '\
  $EMU /usr/bin/cog --platform=headless http://127.0.0.1:8080/ & sleep 60; \
  $EMU /usr/bin/cogctl ping; rc=\$?; echo COGCTL_PING_RC=\$rc; [ \$rc -eq 0 ] && echo COGCTL_PING_OK; \
  $EMU /usr/bin/cogctl open \"http://127.0.0.1:8080/static/unavailable.html?u=cogctl&s=pz&r=1\" && echo COGCTL_OPEN_OK; \
  sleep 30; $EMU /usr/bin/cogctl quit'" > /tmp/cogctl.log 2>&1
# kiosk.py never calls ping (only open/reload); cog 0.16.1 ping exits 1 silently while open works, so report it, do not fail on it
echo "INFO  cogctl ping $(grep -o "COGCTL_PING_RC=[0-9]*" /tmp/cogctl.log) (not used by kiosk.py)"
grep -q COGCTL_OPEN_OK /tmp/cogctl.log && pass "cogctl open accepted" || fail "cogctl open"
grep -q "unavailable.html?u=cogctl" /tmp/server.log && pass "cog loaded the URL sent by cogctl" || fail "cogctl URL not loaded"
pkill -f WPEWebProcess; pkill -f WPENetworkProcess; sleep 1

echo "== 5. memory estimate (native, not emulated; headless, no GPU)"
for b in /usr/lib/arm-linux-gnueabihf/wpe-webkit-1.1/WPE*Process.real; do mv "$b" "${b%.real}"; done
su kiosk -s /bin/sh -c "XDG_RUNTIME_DIR=/tmp/xdg timeout 60 dbus-run-session -- /usr/bin/cog --platform=headless --web-mem-limit=200 http://127.0.0.1:8080/" >/dev/null 2>&1 &
sleep 25
ps -eo rss,comm | awk '/cog|WPEWebProcess|WPENetworkProc/ {s+=$1; printf "      %-22s %6d KiB\n", $2, $1} END {printf "      total                  %6d KiB (%.0f MiB)\n", s, s/1024}'
SRV_RSS=$(ps -o rss= -p "$(pgrep -f 'app/server.py' | tail -1)" 2>/dev/null || echo 0)
echo "      connection manager (emulated; host-side RSS, upper bound) ${SRV_RSS} KiB"
pkill -f "bin/cog"; kill $SERVER 2>/dev/null

echo
[ $FAIL -eq 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $FAIL
