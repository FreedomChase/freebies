#!/bin/sh
# C&G #281: the 11th phone gets no address. REAL dnsmasq with the config hotspot_ap.dnsmasq_conf() writes,
# 11 DHCP clients (busybox udhcpc), each in its own network namespace on one bridge. Root only, Linux only;
# run on a throwaway box (it makes and removes namespaces named dp281-*). Usage: sudo sh dhcp_cap_281.sh [python]
set -u
PY=${1:-python3}
HERE=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d)
cleanup() { [ -f "$W/pid" ] && kill "$(cat "$W/pid")" 2>/dev/null; for n in $(ip netns list | awk '/^dp281-/{print $1}'); do ip netns del "$n"; done; rm -rf "$W"; }
trap cleanup EXIT
cleanup 2>/dev/null; W=$(mktemp -d)
ip netns add dp281-ap
ip -n dp281-ap link add br0 type bridge forward_delay 0 stp_state 0 && ip -n dp281-ap addr add 10.42.0.1/24 dev br0 && ip -n dp281-ap link set br0 up
for i in $(seq 1 11); do
  ip netns add dp281-c$i
  ip link add c$i type veth peer name p$i
  ip link set c$i netns dp281-c$i && ip link set p$i netns dp281-ap
  ip -n dp281-ap link set p$i master br0 && ip -n dp281-ap link set p$i up && ip -n dp281-c$i link set c$i up
done
"$PY" -I - "$HERE/app" "$W" <<'PYEOF'
import sys; sys.path.insert(0, sys.argv[1]); import hotspot_ap
v = hotspot_ap.validate({"iface": "br0", "ssid": "Display Player", "password": "Str0ng-pass-281", "share": True, "max_clients": 10})
conf = hotspot_ap.dnsmasq_conf(v).replace(hotspot_ap.LEASES, sys.argv[2] + "/leases")
open(sys.argv[2] + "/dnsmasq.conf", "w").write(conf)
print("cap in config:", [l for l in conf.splitlines() if "lease-max" in l])
PYEOF
ip netns exec dp281-ap dnsmasq --conf-file="$W/dnsmasq.conf" --pid-file="$W/pid" --log-facility="$W/log" || exit 2
sleep 2
got=0
for i in $(seq 1 11); do
  if ip netns exec dp281-c$i busybox udhcpc -i c$i -n -q -t 4 -T 2 -s /bin/true >/dev/null 2>&1; then got=$((got+1)); r=leased; else r=REFUSED; fi
  echo "phone $i: $r"
done
echo "leases in file: $(wc -l < "$W/leases")"
grep -m1 -i 'no address available\|lease limit' "$W/log" || true
[ "$got" -eq 10 ] && echo "PASS: 10 phones leased, the 11th refused" && exit 0
echo "FAIL: $got phones leased"; exit 1
