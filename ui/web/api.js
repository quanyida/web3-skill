// token-research/web/api.js — the only place that talks to the server.
// Every figure on the screen comes through here; no data is hardcoded in the page.

const BASE = (window.TR_API_BASE || '').replace(/\/$/, '');

async function req(path, params) {
  const u = new URL(BASE + path, window.location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, v);
  });
  const r = await fetch(u, { headers: { Accept: 'application/json' } });
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (e) { /* not json */ }
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

window.API = {
  health: () => req('/api/health'),
  stats: () => req('/api/stats'),
  objects: (p) => req('/api/objects', p),
  object: (name, p) => req('/api/objects/' + encodeURIComponent(name), p),
  history: (name, p) => req('/api/objects/' + encodeURIComponent(name) + '/history', p),
  reports: (p) => req('/api/reports', p),
  report: (id) => req('/api/reports/' + encodeURIComponent(id)),
};
