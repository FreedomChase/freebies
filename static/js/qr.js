// QR code maker: asks the server for a PNG; nothing is stored.
(function () {
  'use strict';
  const form = document.getElementById('qrForm');
  const text = document.getElementById('qrText');
  const size = document.getElementById('sizeRange');
  const sizeLabel = document.getElementById('sizeLabel');
  const btn = document.getElementById('generateBtn');
  const img = document.getElementById('qrImage');
  const placeholder = document.getElementById('qrPlaceholder');
  const download = document.getElementById('downloadBtn');
  const errorMsg = document.getElementById('errorMsg');
  let objUrl = null;
  let timer = null;

  function reset() {
    img.hidden = true;
    placeholder.hidden = false;
    download.hidden = true;
  }

  async function generate() {
    const value = text.value.trim();
    errorMsg.hidden = true;
    if (!value) { reset(); return; }
    btn.disabled = true;
    try {
      const res = await fetch('/api/qr?text=' + encodeURIComponent(value) + '&size=' + encodeURIComponent(size.value));
      if (!res.ok) {
        const j = await res.json().catch(function () { return {}; });
        errorMsg.textContent = j.error || 'Could not make the QR code.';
        errorMsg.hidden = false;
        return;
      }
      if (objUrl) URL.revokeObjectURL(objUrl);
      objUrl = URL.createObjectURL(await res.blob());
      img.src = objUrl;
      img.hidden = false;
      placeholder.hidden = true;
      download.href = objUrl;
      download.hidden = false;
    } catch (e) {
      errorMsg.textContent = 'No connection. Check your network and try again.';
      errorMsg.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  size.addEventListener('input', function () { sizeLabel.textContent = size.value + ' px'; });
  size.addEventListener('change', generate);
  text.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(generate, 600); });
  form.addEventListener('submit', function (e) { e.preventDefault(); generate(); });
})();
