// Fills in the "content unavailable" screen from its query string: u (URL), s (screen ID), r (retry seconds).
(function () {
  var q = new URLSearchParams(location.search);
  var left = parseInt(q.get("r") || "0", 10) || 0;
  document.getElementById("u").textContent = q.get("u") || "";
  document.getElementById("s").textContent = q.get("s") || "?";
  var r = document.getElementById("r");
  function tick() {
    r.textContent = left > 0 ? "Retrying in " + left + " s" : "Retrying";
    if (left > 0) { left -= 1; setTimeout(tick, 1000); }
  }
  tick();
})();
