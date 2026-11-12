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
    const pin = prompt("Enter the 6-digit admin PIN shown on the screen's setup page:");
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
    $("device-label").textContent = (s.device.name ? `${s.device.name} · ` : "") + s.device.id;
    if (s.local && s.settings.admin_pin) rememberPin(s.settings.admin_pin);
    renderPin(s);
    renderSetup(s);
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
    } catch (e) {
      console.error("status refresh failed", e);
      $("status-text").textContent = "Connection manager unreachable";
    }
  };

  /* ---------------- admin PIN panel (screen only) ---------------- */
  const renderPin = (s) => {
    const box = $("pin-card");
    if (!s.local) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    $("pin-value").textContent = s.settings.admin_pin || "";
    $("pin-source").textContent = s.settings.admin_pin_source === "config" ? "set in usb-router.conf" : "generated for this stick";
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
  $("setup-open").onclick = async () => { try { await api("/api/setup-mode", { method: "POST", body: { action: "open" } }); toast("Phone setup allowed for 15 minutes", "ok"); } catch (e) { toast(e.message, "bad"); } refreshStatus(true); };
  $("setup-close").onclick = async () => { try { await api("/api/setup-mode", { method: "POST", body: { action: "close" } }); } catch (e) { toast(e.message, "bad"); } refreshStatus(true); };
  document.querySelectorAll("[data-rotate]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Generate a new ${b.dataset.rotate === "admin_pin" ? "admin PIN" : "hotspot password"} for this screen?`)) return;
      busy(b, true);
      try { const r = await api("/api/secrets/rotate", { method: "POST", body: { name: b.dataset.rotate } }); if (r.settings.admin_pin) rememberPin(r.settings.admin_pin); fillSettings(r.settings); toast("Done", "ok"); }
      catch (e) { toast(e.message, "bad"); }
      busy(b, false); refreshStatus(true);
    };
  });

  /* ---------------- phone setup ---------------- */
  let setupShown = false;
  const renderSetup = (s) => {
    const h = s.hotspot;
    const show = !s.online && h.active && h.mode === "wifi";
    $("setup-card").classList.toggle("hidden", !show);
    $("setup-phone-note").classList.toggle("hidden", s.local);
    $("qr-wrap").classList.toggle("hidden", !s.local);
    if (show && s.local && !setupShown) {
      setupShown = true;
      $("qr-wifi").src = `/api/qr?kind=wifi&t=${Date.now()}`;
      $("qr-ui").src = `/api/qr?kind=ui&t=${Date.now()}`;
    }
    if (!show) setupShown = false;
    $("setup-ssid").textContent = h.ssid || "";
    $("setup-pass").textContent = s.local ? (h.password || "") : "(shown on the screen)";
    $("setup-pin").textContent = s.local ? (s.settings.admin_pin || "") : "(shown on the screen)";
    $("setup-url").textContent = h.ui_url || ""; $("setup-id").textContent = s.device.id;
  };

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
  document.querySelectorAll("[data-action]").forEach((b) => {
    b.onclick = async () => {
      const a = b.dataset.action;
      if (a === "reboot" && !confirm("Reboot this screen now?")) return;
      busy(b, true);
      try { await api("/api/system", { method: "POST", body: { action: a } }); toast(a === "reboot" ? "Rebooting…" : "Done", "ok"); }
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
  const renderHotspot = (h) => {
    const box = $("hotspot-status");
    box.classList.toggle("active", h.active);
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
    $("hs-ssid").value = s.hotspot_ssid; $("hs-pass").value = s.hotspot_password || ""; $("hs-band").value = s.hotspot_band;
    $("hs-pass").placeholder = s.hotspot_password ? "" : "(shown on the screen; blank keeps it)";
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
