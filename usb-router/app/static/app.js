/* USB Router connection manager — front-end.
   Plain JS, no build step. Talks to the Flask API on the same origin. */
(() => {
  const $ = (id) => document.getElementById(id);

  /* Admin PIN: every change needs it. The screen's own page receives it from the
     server (loopback only) and fills it in; a phone on the hotspot is asked for
     the PIN that is printed on the screen. */
  let adminPin = "";
  try { adminPin = sessionStorage.getItem("adminPin") || ""; } catch (_) { /* private mode */ }
  const rememberPin = (pin) => { adminPin = pin; try { sessionStorage.setItem("adminPin", pin); } catch (_) { /* ignore */ } };
  const askPin = () => {
    const pin = prompt("Enter the admin PIN:");
    if (pin && pin.trim()) { rememberPin(pin.trim()); return true; }
    return false;
  };

  const api = async (path, opts = {}, retried = false) => {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(adminPin ? { "X-Admin-Pin": adminPin } : {}), ...(opts.headers || {}) },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (res.status === 401 && data.pin_required && !retried && askPin()) return api(path, opts, true);
    if (!res.ok || data.ok === false) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  };

  let toastTimer;
  const toast = (msg, kind = "") => {
    const t = $("toast");
    t.textContent = msg; t.className = `toast ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
  };
  const busy = (btn, on) => { btn.disabled = on; btn.dataset.label ??= btn.textContent; btn.textContent = on ? "Working…" : btn.dataset.label; };

  const state = { status: null, networks: [], selected: null, hotspotCandidates: [] };

  /* ---------------- status ---------------- */
  const renderStatus = (s) => {
    const pill = $("status-pill"), text = $("status-text");
    const up = s.uplink;
    const via = up ? (up.type === "wifi" ? `Wi-Fi “${up.connection}”` : up.type === "ethernet" ? "Ethernet" : up.type.toUpperCase()) : null;
    if (s.online) {
      pill.className = "pill pill-online"; text.textContent = `Online via ${via || "network"}`;
      $("online-card").classList.remove("hidden");
      $("online-detail").textContent = `Connected through ${via || "the network"}${up && up.ip ? ` (${up.ip})` : ""}.`;
    } else if (s.captive_portal) {
      pill.className = "pill pill-portal"; text.textContent = "Sign-in required";
      $("online-card").classList.add("hidden");
    } else {
      pill.className = "pill pill-offline"; text.textContent = up ? "Connected, no internet" : "Not connected";
      $("online-card").classList.add("hidden");
    }
    $("portal-banner").classList.toggle("hidden", !s.captive_portal);
    $("online-homepage").textContent = s.settings.resolved_homepage;
    // C&G #270: the screen's own content check, so a phone can see why the TV says "Content unavailable"
    const cc = s.content_check;
    $("content-check").classList.toggle("hidden", !cc);
    $("content-check").textContent = cc ? `The screen could not open it: ${cc.failed} (tried ${cc.fails} ${cc.fails === 1 ? "time" : "times"}). Tap Show content to open it anyway.` : "";
    $("device-label").textContent = (s.device.name ? `${s.device.name} · ` : "") + s.device.id;
    if (s.local && s.settings.admin_pin) rememberPin(s.settings.admin_pin);
    renderGate(s);
    renderPin(s);
    renderSetup(s);
    renderSplash(s);
    renderFleet(s);
    $("portal-open").onclick = () => api("/api/browser/go", { method: "POST", body: { url: s.portal_url || "http://neverssl.com" } })
      .catch((e) => toast(e.message, "bad"));
    renderDevices(s.devices, up);
    renderHotspot(s.hotspot);
    $("backend-note").textContent = s.backend === "mock" ? "Demo mode: fake networks" : "";
  };

  const refreshStatus = async (force = false) => {
    try {
      state.status = await api(`/api/status${force ? "?force=1" : ""}`);
      renderStatus(state.status);
      if (state.status.power === "shutdown") showPowerOff();
    } catch (e) {
      console.error("status refresh failed", e);
      // C&G #209: say what failed, so a photo of the screen names it
      $("status-text").textContent = `Status check failed: ${(e && e.message) || e}`.slice(0, 120);
    }
  };

  /* ---------------- PIN gate: a phone types the PIN before anything else (S&D #193) ---------------- */
  let pinVerified = false, gateChecking = false;
  const checkPin = async (pin) => {
    const res = await fetch("/api/pin/check", { method: "POST", headers: { "Content-Type": "application/json", "X-Admin-Pin": pin } });
    let data = {};
    try { data = await res.json(); } catch (_) { /* ignore */ }
    return { ok: res.ok && data.ok !== false, error: data.error || `Request failed (${res.status})` };
  };
  const setGate = (on) => {
    $("pin-gate").classList.toggle("hidden", !on);
    document.body.classList.toggle("pin-locked", on);
    if (on) setTimeout(() => $("pin-gate-input").focus(), 50);
  };
  const renderGate = async (s) => {
    if (s.local || pinVerified) { setGate(false); return; }
    if (gateChecking) return;
    if (adminPin) {            // a PIN from earlier in this tab: check it quietly first
      gateChecking = true;
      const r = await checkPin(adminPin).catch(() => ({ ok: false }));
      gateChecking = false;
      if (r.ok) { pinVerified = true; setGate(false); return; }
      rememberPin("");
    }
    setGate(true);
  };
  $("pin-gate-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const pin = $("pin-gate-input").value.trim();
    if (!pin) return;
    busy($("pin-gate-go"), true);
    const r = await checkPin(pin).catch((e) => ({ ok: false, error: e.message }));
    busy($("pin-gate-go"), false);
    if (r.ok) { rememberPin(pin); pinVerified = true; $("pin-gate-msg").textContent = ""; setGate(false); refreshStatus(true); }
    else { $("pin-gate-msg").textContent = r.error; $("pin-gate-input").value = ""; $("pin-gate-input").focus(); }
  };

  /* ---------------- admin PIN panel (screen only) ---------------- */
  let pinLocal = true;
  const renderPin = (s) => {
    const box = $("pin-card");
    // C&G #254 v8.7: a phone gets this card too, for the phone-setup window buttons only (CSS hides the PIN)
    box.classList.remove("hidden");
    box.classList.toggle("remote", !s.local);
    // C&G #270 (dev4's raise on #254): a phone sees only the setup-window buttons here, so name it for that
    $("pin-title").textContent = s.local ? "Admin PIN" : "Phone setup";
    pinLocal = !!s.local;
    $("lan-hint").textContent = s.lan_url ? `From a phone on the same Wi-Fi: ${s.lan_url}` : "";
    const isDefault = s.settings.admin_pin_source === "default";   // the shared default PIN is never displayed
    $("pin-value").textContent = isDefault ? "Default PIN" : (s.settings.admin_pin || "");
    $("pin-source").textContent = { config: "set in usb-router.conf", new: "new PIN for this screen",
      default: "same on every screen, not shown", generated: "generated for this stick" }[s.settings.admin_pin_source] || "";
    $("pin-hint").textContent = isDefault
      ? "Every change to this screen, from here or from a phone on its hotspot, needs the default admin PIN. New PIN gives this screen its own PIN, shown here."
      : "Every change to this screen, from here or from a phone on its hotspot, needs this PIN. It is only ever shown on this screen.";
    $("hs-pass-source").textContent = s.settings.hotspot_password_source === "config" ? "" : `(${s.settings.hotspot_password_source})`;
    const h = s.hotspot;
    $("setup-open").classList.toggle("hidden", !!h.setup_window_until || s.online);
    $("setup-close").classList.toggle("hidden", !h.setup_window_until);
    $("setup-window-note").textContent = h.setup_window_until
      ? `Phone setup is allowed for another ${Math.max(0, Math.round((h.setup_window_until * 1000 - Date.now()) / 60000))} min while offline.`
      : s.online ? "Phone setup is off while the screen is online."
      : h.configured ? "This screen already has Wi-Fi configured, so it does not broadcast for phone setup unless you allow it."
      : "Unconfigured and offline: the setup hotspot starts about 25 s after boot.";
  };
  $("phone-more").onclick = () => {
    const on = document.body.classList.toggle("phone-more");
    $("phone-more").setAttribute("aria-expanded", on ? "true" : "false");
    $("phone-more").textContent = on ? "Fewer settings" : "More settings: content, rotation, sharing, power";
    if (on) $("settings-card").scrollIntoView({ block: "start", behavior: "smooth" });
  };
  $("setup-open").onclick = async () => { try { await api("/api/setup-mode", { method: "POST", body: { action: "open" } }); toast("Phone setup allowed for 15 minutes", "ok"); } catch (e) { toast(e.message, "bad"); } refreshStatus(true); };
  $("setup-close").onclick = async () => {
    // C&G #270 (dev4's raise on #254): from a phone on the hotspot this drops that phone's own link
    if (!pinLocal && !window.confirm("Stop phone setup? The screen's hotspot closes, so this phone loses its link to the screen.")) return;
    try { await api("/api/setup-mode", { method: "POST", body: { action: "close" } }); } catch (e) { toast(e.message, "bad"); } refreshStatus(true); };
  document.querySelectorAll("[data-rotate]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Generate a new ${b.dataset.rotate === "admin_pin" ? "admin PIN" : "hotspot password"} for this screen?`)) return;
      busy(b, true);
      try { const r = await api("/api/secrets/rotate", { method: "POST", body: { name: b.dataset.rotate } }); if (r.settings.admin_pin) rememberPin(r.settings.admin_pin); fillSettings(r.settings); toast("Done", "ok"); }
      catch (e) { toast(e.message, "bad"); }
      busy(b, false); refreshStatus(true);
    };
  });

  /* ---------------- phone setup: QR first (S&D #193) ---------------- */
  let setupShown = false, phoneScrolled = false;
  const qrPending = {};
  const qrLoaded = (id) => { const im = $(id); return !!(im && im.getAttribute("src") && im.complete && im.naturalWidth > 0); };
  const loadQr = (id, kind) => {
    const im = $(id);
    if (!im || qrPending[id]) return;
    qrPending[id] = true;
    im.onload = () => { qrPending[id] = false; };
    im.onerror = () => { setTimeout(() => { qrPending[id] = false; loadQr(id, kind); }, 2000); };
    im.src = `/api/qr?kind=${kind}&t=${Date.now()}`;
  };
  const renderSetup = (s) => {
    const h = s.hotspot;
    // only while unconfigured or during "Allow phone setup" (not a hotspot someone started by hand)
    const setupMode = h.role === "setup" || !h.configured || !!h.setup_window_until;
    const show = !s.online && h.active && h.mode === "wifi" && setupMode;
    $("setup-card").classList.toggle("hidden", !(show && s.local));
    $("setup-phone").classList.toggle("hidden", !(show && !s.local));
    document.body.classList.toggle("setup-mode", show && s.local);
    document.body.classList.toggle("phone-setup", show && !s.local);
    // C&G #209 v8.4: a QR that failed to load (the radio was switching after a failed join) showed
    // as a blank white box for good. Every render now re-asks for a code that is not on screen.
    if (show && s.local && (!setupShown || !qrLoaded("qr-wifi") || !qrLoaded("qr-ui"))) {
      setupShown = true;
      if (!qrLoaded("qr-wifi")) loadQr("qr-wifi", "wifi");
      if (!qrLoaded("qr-ui")) loadQr("qr-ui", "ui");
    }
    if (!show && setupShown) { setupShown = false; ["qr-wifi", "qr-ui"].forEach((id) => { if (!qrPending[id]) $(id).removeAttribute("src"); }); }
    if (show && !s.local && !phoneScrolled) {
      phoneScrolled = true;
      $("setup-phone").scrollIntoView({ block: "start" });   // the line, then the Wi-Fi list right under it
    }
    $("setup-ssid").textContent = h.ssid || "";
    // no hotspot password in text anywhere (Shaun, 17 Nov): the QR carries it
    // C&G #209 v8.5: and none after a failed join either (the QR card then shows only the reason)
    const showPin = s.local && s.settings.admin_pin_source !== "default" && !!s.settings.admin_pin
      && !(s.join && s.join.ok === false);
    $("setup-pin-wrap").classList.toggle("hidden", !showPin);
    $("setup-pin").textContent = showPin ? s.settings.admin_pin : "";
    $("setup-url").textContent = h.ui_url || ""; $("setup-id").textContent = s.device.id;
    renderJoin(s, show);
    renderConf(s, show);
    renderConfWifi(s);
    $("setup-diag").textContent = s.local ? [fmtDiag(h.diag), fmtJoin(s.join)].filter(Boolean).join(" · ") : "";
    // the clean screen hides it; it comes back when a phone was refused, or with the D key
    $("setup-diag").classList.toggle("show", s.local && (diagKey || !!(h.diag && h.diag.refused)));
  };
  /* P&T #1527: usb-router.conf problems. The settings page lists them all; the setup QR card
     on the screen says how many and the first one, so a TV across the room still shows it. */
  const renderConf = (s, setupShowing) => {
    const list = (s.settings && s.settings.conf_problems) || [];
    $("conf-banner").classList.toggle("hidden", !list.length || (s.local && setupShowing));
    $("conf-list").innerHTML = list.map((p) => `<li>${esc(p)}</li>`).join("");
    const line = $("setup-conf");
    line.classList.toggle("hidden", !(s.local && list.length));
    if (list.length) line.innerHTML = `<strong>Check the config file:</strong> ${esc(list[0])}`
      + (list.length > 1 ? ` <span class="muted">(+${list.length - 1} more on the settings page)</span>` : "");
  };
  /* C&G #270: what became of the Wi-Fi in usb-router.conf (read? seen? why not joined?), on the setup
     card, so a screen that comes up on setup says why the file's network did not join. */
  const renderConfWifi = (s) => {
    const list = (s.conf_wifi || []).filter((w) => w.state !== "joined");
    const line = $("setup-confwifi");
    line.classList.toggle("hidden", !list.length);
    line.innerHTML = list.map((w) => w.ssid
      ? `<strong>From usb-router.conf:</strong> ${esc(w.ssid)}: ${esc(w.words)}`
      : `<strong>From usb-router.conf:</strong> ${esc(w.words)}`).join("<br>");
  };
  /* ---------------- venue join on the screen (C&G #209 v8.3) ----------------
     The phone drops off when the hotspot stops, so the screen is where the join is seen:
     the step and the exact name while it runs, and NetworkManager's reason when it fails. */
  const fmtJoin = (j) => !j ? "" : `join: "${j.ssid}" ${j.step}${j.nm_reason ? ` (${j.nm_reason})` : ""}`
    + (j.trail && j.trail.length ? ` · radio: ${j.trail.join(" → ")}` : "");
  const renderJoin = (s, setupShowing) => {
    const j = s.join, banner = $("join-banner"), line = $("setup-join");
    const running = j && j.ok === null, failed = j && j.ok === false;
    // the screen shows it on the QR card while setup is up; a phone back on the hotspot sees the banner
    banner.classList.toggle("hidden", !j || (s.local && setupShowing));
    banner.classList.toggle("bad", !!failed);
    if (j) banner.innerHTML = running ? `Joining <strong>${esc(j.ssid)}</strong>: ${esc(j.step)}…`
      : failed ? `<strong>${esc(j.ssid)}</strong> did not join: ${esc(j.reason || "unknown reason")}.`
        + (j.trail && j.trail.length ? `<br><small class="muted">radio: ${esc(j.trail.join(" → "))}</small>` : "")
      : `Joined <strong>${esc(j.ssid)}</strong>.`;
    line.classList.toggle("hidden", !(s.local && failed));
    if (failed) line.innerHTML = `Last try: <strong>${esc(j.ssid)}</strong> did not join: ${esc(j.reason || "unknown reason")}. Scan the code to try again.`;
    renderJoinScreen(s, setupShowing);
  };
  /* C&G #209 v8.5: on the screen a join shows only a clean full-screen state (icon, the venue's
     name, a bar), never the admin page under it. It covers: the join running; a failure until the
     setup QR is back (then the QR card says why); a join that worked until the content opens.
     Bounded in time so a fault is never hidden for good, and the D key shows the page. */
  const JOIN_HOLD = 90000;
  let joinKeyShow = false, joinFillTimer = null;
  const joinFill = (p) => { const el = $("join-screen"); if (el) el.style.setProperty("--join-p", `${p}%`); };
  const renderJoinScreen = (s, setupShowing) => {
    const el = $("join-screen");
    if (!el) return;
    const j = s.join, running = j && j.ok === null, failed = j && j.ok === false;
    const recent = j && Date.now() / 1000 - (j.at || 0) < JOIN_HOLD / 1000;
    const on = !!(s.local && j && !setupShowing && !joinKeyShow && (running || recent));
    el.classList.toggle("hidden", !on);
    document.body.classList.toggle("joining", on);
    el.classList.toggle("bad", !!(on && failed));
    if (!on) { clearInterval(joinFillTimer); joinFillTimer = null; joinFill(10); return; }
    $("join-text").textContent = running ? `Connecting to ${j.ssid}…`
      : failed ? `${j.ssid} did not connect` : `Connected to ${j.ssid}`;
    const why = j.reason || "unknown reason";
    const sub = failed ? `${why[0].toUpperCase()}${why.slice(1)}. Opening setup again…`
      : running ? "" : "Opening your content…";
    $("join-sub").textContent = sub; $("join-sub").classList.toggle("hidden", !sub);
    const lan = !running && !failed && s.lan_url;
    $("join-lan").classList.toggle("hidden", !lan);
    if (lan && $("join-lan-url").textContent !== s.lan_url) {
      $("join-lan-url").textContent = s.lan_url; $("join-lan-ssid").textContent = j.ssid;
      $("qr-lan").src = `/api/qr?kind=lan&t=${Date.now()}`;
    }
    if (running && !joinFillTimer) {
      joinFillTimer = setInterval(() => {
        const now = parseFloat($("join-screen").style.getPropertyValue("--join-p")) || 10;
        joinFill(Math.min(90, now + (90 - now) * 0.05));
      }, 1000);
    }
    if (!running) { clearInterval(joinFillTimer); joinFillTimer = null; if (!failed) joinFill(100); }
  };
  /* ---------------- boot splash (C&G #209 v8.2) ----------------
     On the screen only. It stays until there is something to show: the QR setup screen; the
     content (online: the kiosk navigates away, so it only goes after ONLINE_HOLD in case the kiosk
     stays here); a hotspot error; the D key; or SPLASH_MAX, after which the page shows as it stands
     so a fault is never hidden for good. */
  const SPLASH_MAX = 240000, ONLINE_HOLD = 20000, splashStart = Date.now();
  let onlineSince = null;
  // v8.4 progress bar: Plymouth fills it to 60 % with the boot; the page creeps on towards 95 %
  // while the connection manager starts, and fills it before the fade (no real percentage exists here)
  const bootFill = (p) => { const el = $("boot-splash"); if (el) el.style.setProperty("--boot-p", `${p}%`); };
  if ($("boot-splash")) {
    bootFill(60);
    const creep = setInterval(() => {
      const el = $("boot-splash");
      if (!el || el.classList.contains("gone")) { clearInterval(creep); return; }
      const now = parseFloat(el.style.getPropertyValue("--boot-p")) || 60;
      bootFill(Math.min(95, now + (95 - now) * 0.06));
    }, 1000);
  }
  const hideSplash = () => {
    const el = $("boot-splash");
    if (!el || el.classList.contains("gone")) return;
    bootFill(100);
    el.classList.add("gone");
    document.body.classList.remove("booting");
    setTimeout(() => el.remove(), 1000);
  };
  const renderSplash = (s) => {
    if (!$("boot-splash")) return;
    if (s.online) { onlineSince = onlineSince || Date.now(); } else { onlineSince = null; }
    if (document.body.classList.contains("setup-mode") || (s.hotspot && s.hotspot.last_error)
        || (onlineSince && Date.now() - onlineSince > ONLINE_HOLD) || Date.now() - splashStart > SPLASH_MAX) {
      hideSplash();
    }
  };
  if ($("boot-splash")) setTimeout(hideSplash, SPLASH_MAX + 1000);
  let diagKey = false;
  document.addEventListener("keydown", (e) => {
    if ((e.key === "d" || e.key === "D") && $("boot-splash")) hideSplash();
    if ((e.key === "d" || e.key === "D") && document.body.classList.contains("joining")) {
      joinKeyShow = true; $("join-screen").classList.add("hidden"); document.body.classList.remove("joining");
    }
    if ((e.key === "d" || e.key === "D") && document.body.classList.contains("setup-mode")
        && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "")) {
      diagKey = !diagKey; $("setup-diag").classList.toggle("show", diagKey);
    }
  });

  /* ---------------- fleet ---------------- */
  const renderFleet = (s) => {
    const f = s.fleet, tag = $("fleet-state");
    if (!f.configured) { tag.textContent = "not configured"; tag.className = "tag"; return; }
    if (f.last_contact) { tag.textContent = `last contact ${Math.round(Date.now() / 1000 - f.last_contact)} s ago`; tag.className = "tag ok"; }
    else { tag.textContent = f.last_error ? "unreachable" : "connecting…"; tag.className = "tag"; }
  };
  $("fleet-save").onclick = async (e) => {
    busy(e.target, true); $("fleet-msg").textContent = "";
    const body = { fleet_url: $("set-fleet-url").value, fleet_interval: $("set-fleet-interval").value };
    if ($("set-fleet-token").value) body.fleet_token = $("set-fleet-token").value;
    try {
      await api("/api/settings", { method: "POST", body });
      if (body.fleet_url) { const r = await api("/api/fleet/ping", { method: "POST" }); $("fleet-msg").textContent = "Heartbeat sent" + (Object.keys(r.changed || {}).length ? `, dashboard updated: ${Object.keys(r.changed).join(", ")}` : "."); }
      else $("fleet-msg").textContent = "Saved.";
      $("set-fleet-token").value = "";
    } catch (err) { $("fleet-msg").textContent = err.message; }
    busy(e.target, false); refreshStatus();
  };

  /* ---------------- system actions ---------------- */
  // C&G #209: after Shut down, phone and screen both say when it is safe to unplug
  const showPowerOff = () => { $("power-off").classList.remove("hidden"); document.body.classList.add("powering-off"); };
  document.querySelectorAll("[data-action]").forEach((b) => {
    b.onclick = async () => {
      const a = b.dataset.action;
      if (a === "reboot" && !confirm("Reboot this screen now?")) return;
      if (a === "shutdown" && !confirm("Shut this screen down now? It stays off until the power is unplugged and plugged back in.")) return;
      busy(b, true);
      try {
        await api("/api/system", { method: "POST", body: { action: a } });
        if (a === "shutdown") showPowerOff(); else toast(a === "reboot" ? "Rebooting…" : "Done", "ok");
      }
      catch (e) { toast(e.message, "bad"); }
      busy(b, false);
    };
  });

  /* ---------------- devices ---------------- */
  const renderDevices = (devices, up) => {
    const ul = $("device-list"); ul.innerHTML = "";
    if (!devices.length) { ul.innerHTML = `<li class="muted">No network adapters found.</li>`; return; }
    for (const d of devices) {
      const li = document.createElement("li");
      if (up && d.iface === up.iface) li.classList.add("in-use");
      const nice = { wifi: "Wi-Fi", ethernet: "Ethernet", gsm: "Mobile (4G/5G)", cdma: "Mobile", wwan: "Mobile" }[d.type] || d.type;
      li.innerHTML = `<span class="net-name">${nice} <span class="muted">${d.iface}</span></span>
        <span class="net-meta">${d.connection ? `<span class="tag">${esc(d.connection)}</span>` : ""}
        <span class="tag ${d.state.startsWith("connected") ? "ok" : ""}">${esc(d.state)}</span>${d.ip ? `<span>${d.ip}</span>` : ""}</span>`;
      if (d.type === "ethernet" && !d.state.startsWith("connected") && d.state !== "unavailable") {
        const b = document.createElement("button"); b.className = "btn btn-ghost"; b.textContent = "Connect";
        b.onclick = async () => { busy(b, true); try { await api("/api/ethernet", { method: "POST", body: { iface: d.iface } }); await refreshStatus(true); } catch (e) { toast(e.message, "bad"); } busy(b, false); };
        li.appendChild(b);
      } else if (d.state.startsWith("connected") && d.connection !== "usb-router-hotspot" && !String(d.connection || "").startsWith("usb-router-share-")) {
        const b = document.createElement("button"); b.className = "btn btn-ghost"; b.textContent = "Disconnect";
        b.onclick = async () => { busy(b, true); try { await api("/api/disconnect", { method: "POST", body: { iface: d.iface } }); await refreshStatus(true); await scan(false); } catch (e) { toast(e.message, "bad"); } busy(b, false); };
        li.appendChild(b);
      }
      ul.appendChild(li);
    }
  };

  /* ---------------- wifi ---------------- */
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bars = (sig) => { const n = sig > 75 ? 4 : sig > 50 ? 3 : sig > 25 ? 2 : 1; return `<span class="signal">${[1, 2, 3, 4].map((i) => `<i class="${i <= n ? "on" : ""}"></i>`).join("")}</span>`; };

  const renderNetworks = () => {
    const ul = $("wifi-list"); ul.innerHTML = "";
    if (!state.networks.length) { ul.innerHTML = `<li class="muted">No Wi-Fi networks found. Is a Wi-Fi adapter plugged in?</li>`; return; }
    for (const n of state.networks) {
      const li = document.createElement("li"); li.className = "selectable";
      if (n.in_use) li.classList.add("in-use");
      if (state.selected && state.selected.ssid === n.ssid) li.classList.add("selected");
      li.innerHTML = `<span class="net-name">${esc(n.ssid)}</span>
        <span class="net-meta">${n.in_use ? `<span class="tag ok">connected</span>` : n.saved ? `<span class="tag">saved</span>` : ""}
        <span>${n.secured ? "🔒" : "open"}</span><span>${n.band} GHz</span>${bars(n.signal)}</span>`;
      li.onclick = () => selectNetwork(n);
      ul.appendChild(li);
    }
  };

  const selectNetwork = (n) => {
    state.selected = n; renderNetworks();
    const form = $("wifi-form"); form.classList.remove("hidden");
    $("wifi-form-ssid").textContent = n.ssid;
    $("wifi-error").textContent = "";
    const pw = $("wifi-password");
    pw.value = ""; pw.classList.toggle("hidden", !n.secured || n.saved);
    pw.placeholder = n.saved ? "Saved password will be used" : "Wi-Fi password";
    $("wifi-show").classList.toggle("hidden", !n.secured || n.saved);
    $("wifi-forget").classList.toggle("hidden", !n.saved);
    if (n.secured && !n.saved) pw.focus();
  };

  const scan = async (rescan = true) => {
    const b = $("wifi-rescan"); busy(b, true);
    try {
      const data = await api(`/api/wifi/scan?rescan=${rescan ? 1 : 0}`);
      state.networks = data.networks; renderNetworks();
    } catch (e) {
      $("wifi-list").innerHTML = `<li class="muted">${esc(e.message)}</li>`;
    }
    busy(b, false);
  };

  const connect = async (ssid, password) => {
    const btn = $("wifi-form").querySelector("button[type=submit]"); busy(btn, true);
    $("wifi-error").textContent = "";
    // on the screen's own hotspot the answer never comes back: say so before it drops (C&G #209 v8.3)
    const dropping = document.body.classList.contains("phone-setup");
    $("wifi-drop-ssid").textContent = (state.status && state.status.hotspot && state.status.hotspot.ssid) || "screen's";
    $("wifi-drop").classList.toggle("hidden", !dropping);
    try {
      const r = await api("/api/wifi/connect", { method: "POST", body: { ssid, password: password || null } });
      toast(r.online ? `Connected to ${ssid}` : r.captive_portal ? `Joined ${ssid} — sign in to get online` : `Joined ${ssid}, checking internet…`, r.online ? "ok" : "");
      $("wifi-form").classList.add("hidden"); state.selected = null;
      await refreshStatus(true); await scan(false);
    } catch (e) {
      $("wifi-error").textContent = e.message;
      // if the stale saved profile failed, let the user type a password
      if (state.selected && state.selected.saved) { state.selected.saved = false; selectNetwork(state.selected); $("wifi-error").textContent = e.message; }
    }
    busy(btn, false);
  };

  $("wifi-form").onsubmit = (e) => { e.preventDefault(); if (state.selected) connect(state.selected.ssid, $("wifi-password").value); };
  $("wifi-cancel").onclick = () => { $("wifi-form").classList.add("hidden"); state.selected = null; renderNetworks(); };
  $("wifi-show").onclick = () => { const p = $("wifi-password"); p.type = p.type === "password" ? "text" : "password"; $("wifi-show").textContent = p.type === "password" ? "Show" : "Hide"; };
  $("wifi-forget").onclick = async () => {
    if (!state.selected) return;
    try { await api("/api/wifi/forget", { method: "POST", body: { ssid: state.selected.ssid } }); toast(`Forgot ${state.selected.ssid}`); $("wifi-form").classList.add("hidden"); state.selected = null; await refreshStatus(true); await scan(false); }
    catch (e) { toast(e.message, "bad"); }
  };
  $("wifi-rescan").onclick = () => scan(true);
  $("hidden-form").onsubmit = (e) => { e.preventDefault(); state.selected = { ssid: $("hidden-ssid").value.trim(), secured: !!$("hidden-pass").value, saved: false }; connect(state.selected.ssid, $("hidden-pass").value); };

  /* ---------------- hotspot ---------------- */
  /* one line of Wi-Fi facts for the screen, so a failed phone join tells us why (S&D #193) */
  const fmtDiag = (d) => {
    if (!d) return "";
    if (d.error) return `Wi-Fi check: ${d.error}`;
    const sec = d.security === "wpa-psk" ? "WPA2" : (d.security || "?");
    return [
      `Wi-Fi check: on ${d.iface || "?"}`,
      d.channel ? `channel ${d.channel}` : "channel ?",
      `country ${d.country || "?"}`,
      `${sec}, PMF ${d.pmf || "?"}`,
      `access point ${d.ap || "?"}`,
      `chip handshake ${d.fw_offload || "?"}`,
      `${d.stations ?? "?"} phone${d.stations === 1 ? "" : "s"} joined`,
      `last: ${d.last_event || "nothing yet"}`,
      // the Pi has no clock battery: until it reaches internet time, log times are not real
      d.clock === "internet" ? "clock: internet time" : `clock: ${d.clock || "?"} (log times are not real)`,
    ].join(" · ");
  };

  const renderHotspot = (h) => {
    const box = $("hotspot-status");
    box.classList.toggle("active", h.active);
    $("hs-diag").textContent = fmtDiag(h.diag);
    if (h.active) {
      box.innerHTML = h.mode === "wifi"
        ? `Sharing as Wi-Fi network <strong>${esc(h.ssid)}</strong> on ${esc(h.iface)} · router address ${h.ip || "10.42.0.1"}`
        : `Sharing by cable on ${esc(h.iface)} · router address ${h.ip || "10.42.0.1"}`;
    } else {
      box.innerHTML = h.last_error ? `<span class="error">${esc(h.last_error)}</span>` : (h.enabled ? "Will start sharing once online." : "Not sharing.");
    }
    $("hs-start").classList.toggle("hidden", h.active);
    $("hs-stop").classList.toggle("hidden", !h.active);
    const ul = $("hs-clients"); ul.innerHTML = "";
    for (const c of h.clients || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="net-name">${esc(c.name || "Device")}</span><span class="net-meta">${c.ip} · ${c.mac}</span>`;
      ul.appendChild(li);
    }
    if (h.active && !(h.clients || []).length) ul.innerHTML = `<li class="muted">No devices connected yet.</li>`;
  };

  const loadCandidates = async () => {
    try {
      const d = await api("/api/hotspot");
      state.hotspotCandidates = d.candidates;
      const sel = $("hs-iface"); const cur = sel.value; sel.innerHTML = `<option value="">Automatic</option>`;
      for (const c of d.candidates) {
        const o = document.createElement("option"); o.value = c.iface;
        o.textContent = `${c.iface} (${c.mode === "wifi" ? (c.ap_capable ? "Wi-Fi hotspot" : "Wi-Fi, no AP mode") : "Ethernet cable"})`;
        o.disabled = c.mode === "wifi" && !c.ap_capable; sel.appendChild(o);
      }
      sel.value = cur;
    } catch (_) { /* ignore */ }
  };

  const hotspotAction = async (action, btn) => {
    busy(btn, true); $("hs-error").textContent = "";
    try {
      const iface = $("hs-iface").value || undefined;
      const cand = state.hotspotCandidates.find((c) => c.iface === iface);
      const r = await api("/api/hotspot", { method: "POST", body: { action, iface, mode: cand ? cand.mode : undefined } });
      renderHotspot(r.hotspot); toast(action === "stop" ? "Stopped sharing" : "Sharing started", "ok");
    } catch (e) { $("hs-error").textContent = e.message; }
    busy(btn, false); refreshStatus();
  };
  $("hs-start").onclick = (e) => hotspotAction("start", e.target);
  $("hs-stop").onclick = (e) => hotspotAction("stop", e.target);
  $("hs-apply").onclick = async (e) => {
    busy(e.target, true);
    try {
      await api("/api/settings", { method: "POST", body: { hotspot_ssid: $("hs-ssid").value, hotspot_password: $("hs-pass").value, hotspot_band: $("hs-band").value } });
      toast("Hotspot settings saved", "ok");
    } catch (err) { $("hs-error").textContent = err.message; }
    busy(e.target, false); refreshStatus();
  };
  $("setup-hotspot").onchange = async (e) => {
    try { await api("/api/settings", { method: "POST", body: { setup_hotspot: e.target.checked } }); } catch (err) { toast(err.message, "bad"); }
    refreshStatus();
  };
  $("hotspot-enabled").onchange = async (e) => {
    try { await api("/api/settings", { method: "POST", body: { hotspot_enabled: e.target.checked } }); } catch (err) { toast(err.message, "bad"); }
    refreshStatus();
  };

  /* ---------------- settings ---------------- */
  const fillSettings = (s) => {
    $("hs-ssid").value = s.hotspot_ssid; $("hs-pass").value = ""; $("hs-band").value = s.hotspot_band;
    $("hs-pass").placeholder = "(unchanged; blank keeps it)";   // never shown in text (S&D #193)
    $("hotspot-enabled").checked = s.hotspot_enabled; $("setup-hotspot").checked = s.setup_hotspot;
    $("set-homepage").value = s.homepage; $("set-kiosk").checked = s.kiosk; $("set-exit").checked = s.allow_exit;
    $("set-name").value = s.device_name; $("set-rotation").value = s.rotation; $("set-reload").value = s.reload_every;
    $("set-fleet-url").value = s.fleet_url; $("set-fleet-interval").value = s.fleet_interval;
  };
  $("set-save").onclick = async (e) => {
    busy(e.target, true);
    try {
      const r = await api("/api/settings", { method: "POST", body: { homepage: $("set-homepage").value, kiosk: $("set-kiosk").checked, allow_exit: $("set-exit").checked,
        device_name: $("set-name").value, rotation: $("set-rotation").value, reload_every: $("set-reload").value } });
      fillSettings(r.settings); $("set-msg").textContent = "Saved."; setTimeout(() => ($("set-msg").textContent = ""), 2000);
    } catch (err) { toast(err.message, "bad"); }
    busy(e.target, false);
  };

  /* ---------------- browser hand-off ---------------- */
  const go = async () => {
    const url = $("go-url").value.trim();
    try { await api("/api/browser/go", { method: "POST", body: url ? { url } : {} }); }
    catch (e) { toast(e.message, "bad"); }
  };
  $("go-browse").onclick = go;
  $("go-url").onkeydown = (e) => { if (e.key === "Enter") go(); };

  /* ---------------- boot ---------------- */
  (async () => {
    await refreshStatus(true);
    if (state.status) fillSettings(state.status.settings);
    await Promise.all([scan(true), loadCandidates()]);
    setInterval(refreshStatus, 5000);
    setInterval(loadCandidates, 15000);
    setInterval(() => scan(false), 30000);
  })();
})();
