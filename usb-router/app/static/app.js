/* USB Router connection manager — front-end.
   Plain JS, no build step. Talks to the Flask API on the same origin. */
(() => {
  const $ = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
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
      $("status-text").textContent = "Connection manager unreachable";
    }
  };

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
  $("hotspot-enabled").onchange = async (e) => {
    try { await api("/api/settings", { method: "POST", body: { hotspot_enabled: e.target.checked } }); } catch (err) { toast(err.message, "bad"); }
    refreshStatus();
  };

  /* ---------------- settings ---------------- */
  const fillSettings = (s) => {
    $("hs-ssid").value = s.hotspot_ssid; $("hs-pass").value = s.hotspot_password; $("hs-band").value = s.hotspot_band;
    $("hotspot-enabled").checked = s.hotspot_enabled;
    $("set-homepage").value = s.homepage; $("set-kiosk").checked = s.kiosk; $("set-exit").checked = s.allow_exit;
  };
  $("set-save").onclick = async (e) => {
    busy(e.target, true);
    try {
      const r = await api("/api/settings", { method: "POST", body: { homepage: $("set-homepage").value, kiosk: $("set-kiosk").checked, allow_exit: $("set-exit").checked } });
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
