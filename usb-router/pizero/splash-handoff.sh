#!/bin/bash
# C&G #209 v8.2: keep the boot splash up until the connection manager answers, then let go of the
# screen with the last frame left on it, so cog paints over the splash and no console shows between.
for _ in $(seq 120); do
  (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null && break
  sleep 1
done
/usr/bin/plymouth quit --retain-splash || true
