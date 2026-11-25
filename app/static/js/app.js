(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const BASE = document.documentElement.dataset.base || '';
  window.ft = {
    $, BASE,
    async api(method, url, body, raw) {
      const opt = { method, headers: {}, credentials: 'same-origin' };
      if (body !== undefined && !(body instanceof FormData)) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
      if (body instanceof FormData) opt.body = body;
      const r = await fetch(BASE + url, opt);
      if (raw) return r;
      let data = null; try { data = await r.json(); } catch (e) { /* no body */ }
      if (!r.ok) {
        let d = data && data.detail;
        if (Array.isArray(d)) d = d.map((x) => x.msg).join('; ');
        else if (d && typeof d === 'object') d = JSON.stringify(d);
        if (r.status === 401) location.href = BASE + '/login';
        throw new Error(d || ('Request failed (' + r.status + ')'));
      }
      return data;
    },
    toast(msg, bad) {
      const t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status');
      if (bad) t.style.borderColor = 'var(--bad)'; t.textContent = msg; document.body.appendChild(t);
      setTimeout(() => t.remove(), 4500);
    },
    h(tag, attrs, ...kids) {
      const el = document.createElement(tag);
      for (const [k, v] of Object.entries(attrs || {})) {
        if (v === false || v == null) continue;
        if (k === 'class') el.className = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : v);
      }
      for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
      return el;
    },
    typing(e) { const t = e.target; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); },
  };

  document.querySelectorAll('[data-appearance]').forEach((sel) => {
    sel.addEventListener('change', async () => {
      const kind = sel.dataset.appearance;
      document.documentElement.dataset[kind] = sel.value;
      document.querySelectorAll(`[data-appearance="${kind}"]`).forEach((o) => { o.value = sel.value; });
      if (!$('.nav a')) return; // login page: nothing to persist
      try { await ft.api('POST', '/ui/appearance', { [kind]: sel.value }); } catch (e) { ft.toast(e.message, true); }
    });
  });

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register(BASE + '/sw.js', { scope: BASE + '/' }).catch(() => {});
  }
})();
