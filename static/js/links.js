// Link shortener. The visitor's list is keyed by a random token kept only in this browser.
(function () {
  'use strict';
  const KEY = 'orionpath-freebies-owner';

  function ownerToken() {
    let t = null;
    try { t = localStorage.getItem(KEY); } catch (e) { /* storage blocked */ }
    if (!t || !/^[A-Za-z0-9_-]{32,64}$/.test(t)) {
      const bytes = new Uint8Array(32);
      crypto.getRandomValues(bytes);
      t = btoa(String.fromCharCode.apply(null, bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      try { localStorage.setItem(KEY, t); } catch (e) { /* list lasts this visit only */ }
    }
    return t;
  }

  const owner = ownerToken();
  const form = document.getElementById('shortenForm');
  const urlInput = document.getElementById('urlInput');
  const btn = document.getElementById('shortenBtn');
  const result = document.getElementById('resultBox');
  const shortInput = document.getElementById('shortUrlInput');
  const copyBtn = document.getElementById('copyBtn');
  const errorMsg = document.getElementById('errorMsg');
  const original = document.getElementById('originalPreview');
  const recentCard = document.getElementById('recentCard');
  const recentList = document.getElementById('recentList');

  function showError(msg) { errorMsg.textContent = msg; errorMsg.hidden = false; }

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    const url = urlInput.value.trim();
    if (!url) { showError('Paste a link first.'); return; }
    btn.disabled = true;
    errorMsg.hidden = true;
    result.hidden = true;
    try {
      const res = await fetch('/api/shorten', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url, owner: owner }),
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) { showError(data.error || 'Something went wrong. Try again.'); return; }
      shortInput.value = data.short_url;
      original.textContent = 'Opens ' + data.original_url;
      copyBtn.textContent = 'Copy';
      result.hidden = false;
      loadRecent();
    } catch (err) {
      showError('No connection. Check your network and try again.');
    } finally {
      btn.disabled = false;
    }
  });

  copyBtn.addEventListener('click', function () {
    navigator.clipboard.writeText(shortInput.value).then(function () {
      copyBtn.textContent = 'Copied';
      setTimeout(function () { copyBtn.textContent = 'Copy'; }, 2000);
    });
  });

  async function loadRecent() {
    let data = [];
    try {
      const res = await fetch('/api/recent-links', { headers: { 'X-Owner-Token': owner } });
      data = res.ok ? await res.json() : [];
    } catch (e) { data = []; }
    recentList.replaceChildren();
    recentCard.hidden = data.length === 0;
    data.forEach(function (l) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = l.short_url;
      a.textContent = l.short_url.replace(location.origin, '');
      a.rel = 'noopener';
      const to = document.createElement('span');
      to.className = 'to';
      to.textContent = l.original_url;
      const clicks = document.createElement('span');
      clicks.className = 'clicks';
      clicks.textContent = l.clicks + (l.clicks === 1 ? ' open' : ' opens');
      li.append(a, to, clicks);
      recentList.append(li);
    });
  }

  loadRecent();
})();
