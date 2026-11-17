"""Write demo data SQL for the feedback box (invented, no real customers).

usage: python3 seed_demo.py > demo.sql
Demo device keys (local tests only, never used on a real box):
  box-cafe       demo-key-cafe-0000000001
  box-reception  demo-key-reception-000002
"""
import hashlib
import random
from datetime import datetime, timedelta, timezone

DEVICES = [
    ("box-cafe", "Demo cafe counter", "demo-key-cafe-0000000001"),
    ("box-reception", "Demo reception <desk>", "demo-key-reception-000002"),
]
WEIGHTS = {"happy": 6, "ok": 3, "sad": 1}

rnd = random.Random(213)
now = datetime.now(timezone.utc)
print("DELETE FROM ratings; DELETE FROM devices;")
for dev_id, name, key in DEVICES:
    h = hashlib.sha256(key.encode()).hexdigest()
    print(f"INSERT INTO devices (id, name, key_hash, last_at) VALUES ('{dev_id}', '{name}', '{h}', NULL);")
    seq = 0
    for day in range(7, -1, -1):
        for _ in range(rnd.randint(8, 25)):
            seq += 1
            r = rnd.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
            t = now - timedelta(days=day, minutes=rnd.randint(1, 600))
            if t > now:
                t = now
            ts = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            print(
                "INSERT INTO ratings (device_id, rating, seq, pressed_at, received_at) "
                f"VALUES ('{dev_id}', '{r}', {seq}, '{ts}', '{ts}');"
            )
    # live presses in the tests start well above the demo seqs
