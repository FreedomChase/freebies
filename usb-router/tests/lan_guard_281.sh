#!/bin/sh
# C&G #281: a phone on the sharing hotspot reaches the internet but not the venue's network, with REAL nft.
# Network namespaces: phone (10.42.0.50) -- stick (hotspot hs0 10.42.0.1 | uplink up0 192.168.50.2) -- venue
# (192.168.50.1 = a venue host, plus 203.0.113.1 = "the internet"). The stick masquerades like the real share path,
# then loads the guard twice: the shell script (NetworkManager path) and hotspot_ap.guard_rules (hostapd path).
# Root only, Linux only, throwaway box (namespaces lg281-*). Usage: sudo sh lan_guard_281.sh [python]
set -u
PY=${1:-python3}
HERE=$(cd "$(dirname "$0")/.." && pwd)
cleanup() { for n in $(ip netns list | awk '/^lg281-/{print $1}'); do ip netns del "$n"; done; }
trap cleanup EXIT
cleanup 2>/dev/null
for n in phone stick venue; do ip netns add lg281-$n; ip -n lg281-$n link set lo up; done
ip link add hs0 netns lg281-stick type veth peer name ph0 netns lg281-phone
ip link add up0 netns lg281-stick type veth peer name vn0 netns lg281-venue
ip -n lg281-phone addr add 10.42.0.50/24 dev ph0; ip -n lg281-phone link set ph0 up
ip -n lg281-phone route add default via 10.42.0.1
ip -n lg281-stick addr add 10.42.0.1/24 dev hs0; ip -n lg281-stick link set hs0 up
ip -n lg281-stick addr add 192.168.50.2/24 dev up0; ip -n lg281-stick link set up0 up
ip -n lg281-stick route add default via 192.168.50.1
ip -n lg281-venue addr add 192.168.50.1/24 dev vn0; ip -n lg281-venue addr add 203.0.113.1/32 dev lo
ip -n lg281-venue link set vn0 up
ip netns exec lg281-stick sh -c 'echo 1 > /proc/sys/net/ipv4/ip_forward'
ip netns exec lg281-stick nft -f - <<'EOF'
table ip share {
  chain post {
    type nat hook postrouting priority 100;
    ip saddr 10.42.0.0/24 oifname != "hs0" masquerade
  }
}
EOF
reach() { ip netns exec lg281-phone ping -c1 -W1 "$1" >/dev/null 2>&1 && echo REACHES || echo blocked; }
stick() { ip netns exec lg281-phone ping -c1 -W1 10.42.0.1 >/dev/null 2>&1 && echo REACHES || echo blocked; }
fail=0
check() {  # label internet venue stick
  echo "$1: internet 203.0.113.1 $2 · venue host 192.168.50.1 $3 · the stick 10.42.0.1 $4"
  [ "$2" = REACHES ] && [ "$3" = blocked ] && [ "$4" = REACHES ] || fail=1
}
echo "no guard: internet $(reach 203.0.113.1) · venue host $(reach 192.168.50.1)   (what a guest could do before)"
ip netns exec lg281-stick sh "$HERE/scripts/usb-router-lan-guard" up hs0
check "NM path guard (scripts/usb-router-lan-guard)" "$(reach 203.0.113.1)" "$(reach 192.168.50.1)" "$(stick)"
ip netns exec lg281-stick sh "$HERE/scripts/usb-router-lan-guard" down hs0
echo "after guard down: venue host $(reach 192.168.50.1)"
"$PY" -I - "$HERE/app" <<'PYEOF' | ip netns exec lg281-stick nft -f -
import sys; sys.path.insert(0, sys.argv[1]); import hotspot_ap
print(hotspot_ap.guard_rules("hs0"))
PYEOF
check "hostapd path guard (hotspot_ap.guard_rules)" "$(reach 203.0.113.1)" "$(reach 192.168.50.1)" "$(stick)"
[ "$fail" -eq 0 ] && echo "PASS: guests reach the internet and the stick, never the venue network" && exit 0
echo "FAIL"; exit 1
