"""Integration test for the feedback box Worker, run against `wrangler dev` with the demo data.

usage: BASE=http://127.0.0.1:8787 VIEW_KEY=... python3 test_endpoint.py
Exit 0 = every check passed. Each check prints PASS/FAIL.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8787")
VIEW_KEY = os.environ["VIEW_KEY"]
CAFE = "demo-key-cafe-0000000001"
RECEPTION = "demo-key-reception-000002"
fails = 0


def req(method, path, body=None, key=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("content-type", "application/json")
    if key:
        r.add_header("authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        fails += 1


s, b, _ = req("GET", "/health")
check("health 200", s == 200 and json.loads(b)["ok"] is True, f"{s} {b}")

s, b, _ = req("POST", "/api/rating", {"rating": "happy", "seq": 1000})
check("no key -> 401", s == 401, s)
s, b, _ = req("POST", "/api/rating", {"rating": "happy", "seq": 1000}, key="wrong-key-but-long-enough")
check("unknown key -> 401", s == 401, s)
s, b, _ = req("GET", "/api/rating", key=CAFE)
check("GET on rating -> 405", s == 405, s)
s, b, _ = req("POST", "/api/rating", raw=b"not json", key=CAFE)
check("bad json -> 400", s == 400, s)
s, b, _ = req("POST", "/api/rating", {"rating": "great", "seq": 1000}, key=CAFE)
check("unknown rating -> 400", s == 400 and "rating" in b, f"{s} {b}")
s, b, _ = req("POST", "/api/rating", {"rating": "happy", "seq": 0}, key=CAFE)
check("seq 0 -> 400", s == 400 and "seq" in b, f"{s} {b}")
s, b, _ = req("POST", "/api/rating", {"rating": "happy", "seq": 1.5}, key=CAFE)
check("seq 1.5 -> 400", s == 400, f"{s} {b}")


def count_week(page, box, label):
    # the 7-day number for one rating inside one box's card
    card = page.split(box, 1)[1].split("</section>", 1)[0]
    m = re.search(r'<span class="lbl">' + label + r"</span>.*?<b>(\d+)</b>", card, re.S)
    return int(m.group(1))


s, page, _ = req("GET", "/count?k=" + VIEW_KEY)
before = count_week(page, "Demo cafe counter", "Sad")

s, b, _ = req("POST", "/api/rating", {"rating": "sad", "seq": 5001, "age_s": 30}, key=CAFE)
check("good press -> 201", s == 201, f"{s} {b}")
s, b, _ = req("POST", "/api/rating", {"rating": "sad", "seq": 5001}, key=CAFE)
check("resend same seq -> 200 duplicate", s == 200 and json.loads(b).get("duplicate") is True, f"{s} {b}")
s, b, _ = req("POST", "/api/rating", {"rating": "sad", "seq": 5002}, key=CAFE)
check("second press within 2 s -> 429", s == 429, f"{s} {b}")
s, b, _ = req("POST", "/api/rating", {"rating": "test", "seq": 7001}, key=RECEPTION)
check("other box not slowed by the first -> 201", s == 201, f"{s} {b}")
time.sleep(2.2)
s, b, _ = req("POST", "/api/rating", {"rating": "sad", "seq": 5002, "age_s": 99999999}, key=CAFE)
check("press after 2 s, silly age -> 201", s == 201, f"{s} {b}")

s, page, h = req("GET", "/count?k=" + VIEW_KEY)
check("count page 200", s == 200, s)
after = count_week(page, "Demo cafe counter", "Sad")
check("count went up by exactly 2 (dup not counted)", after == before + 2, f"{before} -> {after}")
check("box name is escaped", "Demo reception &lt;desk&gt;" in page and "<desk>" not in page)
check("test presses shown apart", "Test presses (BOOT button): 1" in page, "")
check("noindex header", h.get("x-robots-tag") == "noindex" or h.get("X-Robots-Tag") == "noindex", h)

s, b, _ = req("GET", "/count")
check("count without key -> 404", s == 404, s)
s, b, _ = req("GET", "/count?k=" + VIEW_KEY[:-1] + "x")
check("count wrong key -> 404", s == 404, s)
s, b, _ = req("GET", "/nope")
check("unknown path -> 404", s == 404, s)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
