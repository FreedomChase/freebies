// Feedback box endpoint (C&G #213): a Cloudflare Worker with a D1 database.
//   POST /api/rating   one button press from a box (Bearer device key)
//   GET  /count?k=...  the count page for Shaun (view key)
//   GET  /health       liveness
// Keys are never stored in clear: devices.key_hash is sha256(key) in hex,
// VIEW_KEY is a Worker secret.

const RATINGS = ["happy", "ok", "sad", "test"];
const MIN_GAP_MS = 2000;           // server-side flood guard; the box itself allows 1 press per 3 s
const MAX_AGE_S = 7 * 24 * 3600;   // a queued press older than this is stamped at its arrival time

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/health") return json({ ok: true });
      if (url.pathname === "/api/rating") {
        if (request.method !== "POST") return json({ error: "method" }, 405);
        return await postRating(request, env);
      }
      if (url.pathname === "/count" && request.method === "GET") {
        return await countPage(url, env);
      }
      return json({ error: "not found" }, 404);
    } catch (err) {
      console.error(err && err.stack ? err.stack : String(err));
      return json({ error: "server" }, 500);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameLength(a, b) {
  // constant-time compare for two strings of the same length
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function postRating(request, env) {
  const auth = request.headers.get("authorization") || "";
  const m = auth.match(/^Bearer ([A-Za-z0-9_-]{16,128})$/);
  if (!m) return json({ error: "auth" }, 401);
  const device = await env.DB.prepare("SELECT id, last_at FROM devices WHERE key_hash = ?1")
    .bind(await sha256hex(m[1]))
    .first();
  if (!device) return json({ error: "auth" }, 401);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "body" }, 400);
  }
  const rating = typeof body.rating === "string" ? body.rating : "";
  const seq = Number(body.seq);
  if (!RATINGS.includes(rating)) return json({ error: "rating" }, 400);
  if (!Number.isInteger(seq) || seq < 1 || seq > 2 ** 31) return json({ error: "seq" }, 400);

  // A resend of a press already stored (the box lost the reply) is a success, not a second vote.
  const dup = await env.DB.prepare("SELECT 1 FROM ratings WHERE device_id = ?1 AND seq = ?2")
    .bind(device.id, seq)
    .first();
  if (dup) return json({ ok: true, duplicate: true });

  const now = Date.now();
  if (device.last_at && now - device.last_at < MIN_GAP_MS) {
    return json({ error: "too fast" }, 429);
  }

  let age = Number(body.age_s);
  if (!Number.isFinite(age) || age < 0 || age > MAX_AGE_S) age = 0;
  const pressedAt = new Date(now - Math.round(age) * 1000).toISOString();

  await env.DB.batch([
    env.DB.prepare(
      "INSERT OR IGNORE INTO ratings (device_id, rating, seq, pressed_at, received_at) VALUES (?1, ?2, ?3, ?4, ?5)"
    ).bind(device.id, rating, seq, pressedAt, new Date(now).toISOString()),
    env.DB.prepare("UPDATE devices SET last_at = ?1 WHERE id = ?2").bind(now, device.id),
  ]);
  return json({ ok: true }, 201);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function countPage(url, env) {
  const k = url.searchParams.get("k") || "";
  if (!env.VIEW_KEY || !sameLength(k, env.VIEW_KEY)) {
    return new Response("Not found", { status: 404, headers: { "content-type": "text/plain" } });
  }
  const since = (days) => new Date(Date.now() - days * 86400000).toISOString();
  let rows;
  try {
    rows = await env.DB.prepare(
      `SELECT d.name AS name, r.rating AS rating,
              SUM(CASE WHEN r.pressed_at >= ?1 THEN 1 ELSE 0 END) AS today,
              COUNT(*) AS week
         FROM ratings r JOIN devices d ON d.id = r.device_id
        WHERE r.pressed_at >= ?2
        GROUP BY d.name, r.rating`
    )
      .bind(since(1), since(7))
      .all();
  } catch (err) {
    console.log("count query failed: " + err);
    return page(
      `<h1>Feedback counts, last 7 days</h1><section class="card warn"><h2>Counts not available</h2><p>The count store did not answer. Presses are not lost: each box keeps them and sends them again. Reload in a minute.</p></section>`,
      503
    );
  }

  const boxes = {};
  for (const r of rows.results || []) {
    boxes[r.name] = boxes[r.name] || { happy: [0, 0], ok: [0, 0], sad: [0, 0], test: [0, 0] };
    boxes[r.name][r.rating] = [r.today, r.week];
  }
  const label = { happy: "Happy", ok: "OK", sad: "Sad" };
  const colour = { happy: "#2F9E44", ok: "#E8A317", sad: "#D64545" };

  let cards = "";
  const names = Object.keys(boxes).sort();
  for (const name of names) {
    const b = boxes[name];
    const total = b.happy[1] + b.ok[1] + b.sad[1];
    let bars = "";
    for (const key of ["happy", "ok", "sad"]) {
      const pct = total ? Math.round((b[key][1] * 100) / total) : 0;
      bars += `<div class="row"><span class="lbl">${label[key]}</span>
        <span class="bar"><span style="width:${pct}%;background:${colour[key]}"></span></span>
        <b>${b[key][1]}</b><span class="today">${b[key][0]} last 24 h</span></div>`;
    }
    const test = b.test[1] ? `<p class="small">Test presses (BOOT button): ${b.test[1]}, not counted above.</p>` : "";
    cards += `<section class="card"><h2>${esc(name)}</h2><p class="small">${total} presses in 7 days</p>${bars}${test}</section>`;
  }
  if (!cards) cards = `<section class="card"><h2>No presses yet</h2><p>No box has sent a press in the last 7 days. Press a button on a box and reload.</p></section>`;

  return page(`<h1>Feedback counts, last 7 days</h1>${cards}`, 200);
}

function page(inner, status) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Feedback Counts</title><style>
body{margin:0;background:#FBF8F2;color:#14213D;font-family:"DejaVu Sans",Verdana,sans-serif;font-size:19px;line-height:1.6}
main{max-width:720px;margin:0 auto;padding:24px 16px}
h1{font-size:30px;line-height:1.2;margin:0 0 16px}h2{margin:0;font-size:24px;line-height:1.25}
.card{background:#fff;border-left:12px solid #1F3A68;border-radius:10px;padding:16px 20px;margin:0 0 18px}
.card.warn{border-left-color:#D64545}
.row{display:grid;grid-template-columns:80px 1fr 50px 90px;gap:10px;align-items:center;margin:6px 0}
.bar{background:#EEE;border-radius:6px;height:22px;overflow:hidden}.bar span{display:block;height:100%}
.today,.small{color:#4A5568;font-size:15px}
@media (max-width:480px){
.card{border-left-width:6px;padding:16px}
.row{grid-template-columns:auto auto 1fr;grid-template-areas:"lbl num today" "bar bar bar";gap:2px 10px;margin:10px 0}
.lbl{grid-area:lbl}.row b{grid-area:num}.today{grid-area:today;text-align:right}.bar{grid-area:bar;height:12px}
}
</style></head><body><main>${inner}</main></body></html>`;
  return new Response(html, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-robots-tag": "noindex",
      "referrer-policy": "no-referrer",
    },
  });
}
