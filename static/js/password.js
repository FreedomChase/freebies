// Password generator. All randomness comes from the Web Crypto API; nothing leaves the browser.
(function () {
  'use strict';
  const SETS = {
    optUpper: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
    optLower: 'abcdefghijklmnopqrstuvwxyz',
    optDigits: '0123456789',
    optSymbols: '!@#$%^&*()_+-=[]{}|;:,.<>?',
  };
  const out = document.getElementById('pwText');
  const meter = document.getElementById('strengthMeter');
  const label = document.getElementById('strengthLabel');
  const length = document.getElementById('lengthRange');
  const lengthVal = document.getElementById('lengthVal');
  const copyBtn = document.getElementById('copyBtn');
  const batchList = document.getElementById('batchList');

  // Unbiased integer in [0, max) by rejection sampling.
  function secureRandom(max) {
    const arr = new Uint32Array(1);
    const limit = Math.floor(0x100000000 / max) * max;
    do { crypto.getRandomValues(arr); } while (arr[0] >= limit);
    return arr[0] % max;
  }

  function chosen() {
    return Object.keys(SETS).filter(function (id) { return document.getElementById(id).checked; });
  }

  function makePassword(n) {
    let ids = chosen();
    if (ids.length === 0) ids = ['optLower'];
    const all = ids.map(function (id) { return SETS[id]; }).join('');
    const chars = ids.map(function (id) { return SETS[id][secureRandom(SETS[id].length)]; });
    while (chars.length < n) chars.push(all[secureRandom(all.length)]);
    for (let i = chars.length - 1; i > 0; i--) {
      const j = secureRandom(i + 1);
      const t = chars[i]; chars[i] = chars[j]; chars[j] = t;
    }
    return chars.join('');
  }

  function strength(pw) {
    let s = 0;
    if (pw.length >= 12) s++;
    if (pw.length >= 16) s++;
    if (pw.length >= 24) s++;
    if (/[A-Z]/.test(pw)) s++;
    if (/[a-z]/.test(pw)) s++;
    if (/[0-9]/.test(pw)) s++;
    if (/[^A-Za-z0-9]/.test(pw)) s++;
    return s;
  }

  function show(pw) {
    out.textContent = pw;
    const s = strength(pw);
    meter.value = s;
    label.textContent = 'Strength: ' + (s <= 2 ? 'weak' : s <= 4 ? 'fair' : s <= 5 ? 'strong' : 'excellent');
  }

  function copy(text, button, done) {
    navigator.clipboard.writeText(text).then(function () {
      const before = button.textContent;
      button.textContent = done;
      setTimeout(function () { button.textContent = before; }, 1500);
    });
  }

  function batch() {
    batchList.replaceChildren();
    for (let i = 0; i < 8; i++) {
      const pw = makePassword(Number(length.value));
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'batch-pw';
      b.textContent = pw;
      b.addEventListener('click', function () { copy(pw, b, 'Copied'); });
      li.append(b);
      batchList.append(li);
    }
  }

  length.addEventListener('input', function () { lengthVal.textContent = length.value; show(makePassword(Number(length.value))); });
  Object.keys(SETS).forEach(function (id) {
    document.getElementById(id).addEventListener('change', function () { show(makePassword(Number(length.value))); });
  });
  document.getElementById('generateBtn').addEventListener('click', function () { show(makePassword(Number(length.value))); });
  document.getElementById('batchBtn').addEventListener('click', batch);
  copyBtn.addEventListener('click', function () {
    if (out.textContent && out.textContent !== 'Press Generate') copy(out.textContent, copyBtn, 'Copied');
  });

  show(makePassword(Number(length.value)));
  batch();
})();
