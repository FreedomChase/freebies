#!/bin/sh
# Runs inside node:22 (has python3): local D1 + demo data + wrangler dev on loopback + the endpoint test.
# Nothing leaves the container; no Cloudflare account is used (--local).
set -eu
cd /w
export WRANGLER_SEND_METRICS=false CI=1
npm init -y >/dev/null
npm i --no-audit --no-fund wrangler@4 >/dev/null 2>&1
echo "wrangler $(npx wrangler --version 2>/dev/null | tail -1)"
python3 test/seed_demo.py > /tmp/demo.sql
VIEW_KEY=demo-view-key-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
mkdir -p /w/out

wait_up() {  # $1 = port
  i=0
  until python3 -c 'import urllib.request,sys;urllib.request.urlopen("http://127.0.0.1:"+sys.argv[1]+"/health",timeout=2)' "$1" 2>/dev/null; do
    i=$((i+1)); [ $i -gt 60 ] && { echo "wrangler dev on $1 did not start"; tail -30 /tmp/dev$1.log; exit 1; }
    sleep 1
  done
}
# $1 = port, $2 = expected HTTP status, $3 = text the page must hold, $4 = file to save it to
page_check() {
  python3 - "$1" "$2" "$3" "$4" "$VIEW_KEY" <<'PY'
import sys, urllib.request, urllib.error
port, want, text, out, key = sys.argv[1:]
try:
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/count?k={key}", timeout=10); code, body = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode()
open(out, "w").write(body)
ok = code == int(want) and text in body
print(("PASS " if ok else "FAIL ") + f"count page {code} holds '{text}'")
sys.exit(0 if ok else 1)
PY
}

# Error state: a store with no tables (migrations not applied) -> 503 page, not a crash.
npx wrangler dev --local --ip 127.0.0.1 --port 8788 --persist-to /tmp/nostore --var "VIEW_KEY:$VIEW_KEY" >/tmp/dev8788.log 2>&1 &
NOSTORE=$!
wait_up 8788
page_check 8788 503 "Counts not available" /w/out/count-error.html
kill $NOSTORE

npx wrangler d1 migrations apply feedback-box --local >/tmp/mig.log 2>&1 || { cat /tmp/mig.log; exit 1; }
# Empty state: tables, no presses yet.
npx wrangler dev --local --ip 127.0.0.1 --port 8789 --var "VIEW_KEY:$VIEW_KEY" >/tmp/dev8789.log 2>&1 &
EMPTY=$!
wait_up 8789
page_check 8789 200 "No presses yet" /w/out/count-empty.html
kill $EMPTY
sleep 1
npx wrangler d1 execute feedback-box --local --file /tmp/demo.sql >/tmp/seed.log 2>&1 || { cat /tmp/seed.log; exit 1; }
npx wrangler dev --local --ip 127.0.0.1 --port 8787 --var "VIEW_KEY:$VIEW_KEY" >/tmp/dev.log 2>&1 &
i=0
until python3 -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8787/health",timeout=2)' 2>/dev/null; do
  i=$((i+1)); [ $i -gt 60 ] && { echo "wrangler dev did not start"; tail -30 /tmp/dev.log; exit 1; }
  sleep 1
done
BASE=http://127.0.0.1:8787 VIEW_KEY=$VIEW_KEY python3 test/test_endpoint.py
python3 -c "import urllib.request,sys;open('/w/out/count.html','w').write(urllib.request.urlopen('http://127.0.0.1:8787/count?k='+sys.argv[1]).read().decode())" "$VIEW_KEY"
