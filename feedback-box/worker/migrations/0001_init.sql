-- Feedback box (C&G #213)
CREATE TABLE IF NOT EXISTS devices (
  id       TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,   -- sha256(device key), hex
  last_at  INTEGER                 -- ms epoch of the last stored press
);

CREATE TABLE IF NOT EXISTS ratings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL REFERENCES devices(id),
  rating      TEXT NOT NULL CHECK (rating IN ('happy','ok','sad','test')),
  seq         INTEGER NOT NULL,     -- the box's own press counter, so a resend is not a second vote
  pressed_at  TEXT NOT NULL,        -- ISO time, arrival time minus the box's age_s
  received_at TEXT NOT NULL,
  UNIQUE (device_id, seq)
);
CREATE INDEX IF NOT EXISTS ratings_pressed ON ratings (pressed_at);
