// Fills in the "content unavailable" screen from its query string: u (URL), s (screen ID), r (retry seconds);
// C&G #273: t (heading) and w (the reason, in plain words); no r means no countdown, the screen says "Waiting".
(function () {
  var q = new URLSearchParams(location.search);
  var counting = q.get("r") !== null;
  var left = parseInt(q.get("r") || "0", 10) || 0;
  if (q.get("t")) { document.getElementById("t").textContent = q.get("t"); document.title = q.get("t"); }
  var w = document.getElementById("w");
  if (q.get("w")) { w.textContent = q.get("w"); w.classList.remove("hidden"); }
  document.getElementById("u").textContent = q.get("u") || "";
  document.getElementById("s").textContent = q.get("s") || "?";
  var r = document.getElementById("r");
  function tick() {
    if (!counting) { r.textContent = "Waiting"; return; }
    r.textContent = left > 0 ? "Retrying in " + left + " s" : "Retrying";
    if (left > 0) { left -= 1; setTimeout(tick, 1000); }
  }
  tick();
})();
