/* 轻量 API 封装 + 通用 UI 工具 */
const AuthToken = {
  get() { try { return localStorage.getItem("narciFarm.token"); } catch (e) { return null; } },
  set(t) { try { localStorage.setItem("narciFarm.token", t); } catch (e) {} },
  clear() { try { localStorage.removeItem("narciFarm.token"); } catch (e) {} },
};

const API = {
  async get(url) {
    const r = await fetch(url, { headers: AuthToken.get() ? { Authorization: "Bearer " + AuthToken.get() } : {} });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: Object.assign(
        body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
        AuthToken.get() ? { Authorization: "Bearer " + AuthToken.get() } : {}),
      body: body instanceof FormData ? body : JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async put(url, body) {
    const r = await fetch(url, { method: 'PUT', headers: Object.assign({ 'Content-Type': 'application/json' }, AuthToken.get() ? { Authorization: "Bearer " + AuthToken.get() } : {}), body: JSON.stringify(body || {}) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async del(url) {
    const r = await fetch(url, { headers: AuthToken.get() ? { Authorization: "Bearer " + AuthToken.get() } : {} });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  wsUrl(path) {
    return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
  },
  wsAuthed(path) {
    const t = AuthToken.get();
    return t ? `${this.wsUrl(path)}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}` : this.wsUrl(path);
  },
};

function toast(msg, type = 'info', ms = 3500) {
  const box = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}

/* 浏览器侧诊断：把点击/页面错误上报到服务端日志，用于定位“点了没反应”类问题。
   完全静默，失败绝不干扰业务；相同上报 3 秒内去重。 */
const Diag = {
  _last: {},
  press(kind) {
    try {
      const cv = document.getElementById('mm-canvas');
      const r = cv ? cv.getBoundingClientRect() : null;
      let fb = false;
      try { fb = !!(State.mirror && State.mirror.fallback); } catch (e) { /* app.js 可能未加载 */ }
      this.send('press', {
        kind,
        canvas: r ? {
          visible: !!(r.width && r.height),
          w: Math.round(r.width), h: Math.round(r.height),
          at: Math.round(r.left) + ',' + Math.round(r.top),
        } : null,
        fallback: fb,
      });
    } catch (e) { /* ignore */ }
  },
  send(path, payload) {
    try {
      const key = path + ':' + JSON.stringify(payload);
      const now = Date.now();
      if (this._last[key] && now - this._last[key] < 3000) return;
      this._last[key] = now;
      const body = JSON.stringify({
        ua: navigator.userAgent,
        path,
        payload: payload || {},
        url: String(location.pathname + location.search).slice(0, 120),
      });
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/diag', new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/api/diag', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
      }
    } catch (e) { /* 诊断本身绝不干扰业务 */ }
  },
};

if (typeof window !== 'undefined') {
  window.addEventListener('error', (e) => {
    Diag.send('jserror', {
      msg: String((e && e.message) || '').slice(0, 100),
      src: String((e && e.filename) || '').split('/').pop().slice(0, 60),
      line: e && e.lineno,
    });
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e && e.reason;
    Diag.send('jsrejection', { msg: String((r && r.message) || r || '').slice(0, 100) });
  });
}
