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
npx wrangler d1 migrations apply feedback-box --local >/tmp/mig.log 2>&1 || { cat /tmp/mig.log; exit 1; }
npx wrangler d1 execute feedback-box --local --file /tmp/demo.sql >/tmp/seed.log 2>&1 || { cat /tmp/seed.log; exit 1; }
VIEW_KEY=demo-view-key-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
npx wrangler dev --local --ip 127.0.0.1 --port 8787 --var "VIEW_KEY:$VIEW_KEY" >/tmp/dev.log 2>&1 &
i=0
until python3 -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8787/health",timeout=2)' 2>/dev/null; do
  i=$((i+1)); [ $i -gt 60 ] && { echo "wrangler dev did not start"; tail -30 /tmp/dev.log; exit 1; }
  sleep 1
done
BASE=http://127.0.0.1:8787 VIEW_KEY=$VIEW_KEY python3 test/test_endpoint.py
python3 -c "import urllib.request,sys;open('/w/out/count.html','w').write(urllib.request.urlopen('http://127.0.0.1:8787/count?k='+sys.argv[1]).read().decode())" "$VIEW_KEY"
