/* 投屏组件（v0.3.0 重构）：
 * - 视频: WS 单向推流 → WebCodecs 解码（失败自动退化快照轮询）
 * - 操作: 统一走 HTTP /api/input（与批量/群发同一条可靠路径，逐台留痕）
 * - 反馈: 点击处涟漪标记 + 状态行 + 控制台，链路任一环断开都可见
 *
 * 服务端二进制消息: [0x01][key u8][pts u64][AVCC payload] 帧 | [0x03][config payload]
 * 文本 JSON: {type:"session"|"device"|"error"|"stats", ...}
 */
/* 投屏组件（v0.3.0 重构）：
 * - 视频: WS 单向推流 → WebCodecs 解码（失败自动退化快照轮询）
 * - 操作: 统一走 HTTP /api/input（与批量/群发同一条可靠路径，逐台留痕）
 * - 反馈: 点击处涟漪标记 + 状态行 + 控制台，链路任一环断开都可见
 *
 * 服务端二进制消息: [0x01][key u8][pts u64][AVCC payload] 帧 | [0x03][config payload]
 * 文本 JSON: {type:"session"|"device"|"error"|"stats", ...}
 */

function ELAPSED_LOG(elapsed_ms) {
  const s = `${(elapsed_ms/1000).toFixed(2)}s`;
  const txt = document.getElementById('mm-inject-status');
  if (txt) {
    const hasOld = txt.textContent.includes('加载耗时') || txt.textContent.includes('加载中');
    txt.textContent = hasOld ? txt.textContent.replace(/加载[中耗时][^)]*\)/, '') : txt.textContent;
    txt.textContent += `（加载耗时 ${s}）`;
  }
  console.log('[镜像加载耗时] ' + s);
}

function splitAvcc(payload) {
  const nals = [];
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  let off = 0;
  while (off + 4 <= payload.byteLength) {
    const n = dv.getUint32(off);
    off += 4;
    if (n === 0 || off + n > payload.byteLength) break;
    nals.push(payload.subarray(off, off + n));
    off += n;
  }
  return nals;
}

function buildAvcC(sps, pps) {
  const out = [0x01, sps[1], sps[2], sps[3], 0xFF, 0xE1,
    (sps.length >> 8) & 0xFF, sps.length & 0xFF, ...sps,
    0x01, (pps.length >> 8) & 0xFF, pps.length & 0xFF, ...pps];
  return new Uint8Array(out).buffer;
}

function rippleAt(stage, clientX, clientY) {
  const r = stage.getBoundingClientRect();
  const el = document.createElement('div');
  el.className = 'ripple';
  el.style.left = (clientX - r.left) + 'px';
  el.style.top = (clientY - r.top) + 'px';
  stage.appendChild(el);
  setTimeout(() => el.remove(), 550);
}

/* 统一注入入口: HTTP, 无状态, 逐台回执 */
async function sendInput(targets, action, params, onFeedback) {
  console.debug('[narci] 发送注入', { action, params, targets });
  try {
    const r = await API.post('/api/input', { serials: targets, action, params });
    if (onFeedback) onFeedback(action, targets, r);
    if (r && r.ok === false) {
      const failed = Object.entries(r.results || {}).filter(([, v]) => !v.ok);
      toast(`注入失败: ${failed.map(([s, v]) => `${s}: ${v.error}`).join('；').slice(0, 160)}`, 'error', 6000);
    } else if (r && r.results) {
      // 服务端附带的提醒（如“设备熄屏已自动唤醒”）必须透出，否则用户以为点了没反应
      const notes = [...new Set(Object.values(r.results).map(v => v && v.note).filter(Boolean))];
      notes.forEach(n => toast(n, 'warn', 7000));
    }
    return r;
  } catch (e) {
    toast(`注入请求失败: ${e.message}`, 'error', 6000);
    if (onFeedback) onFeedback(action, targets, { ok: false, error: e.message });
    return null;
  }
}

class Mirror {
  /**
   * @param opts {getTargets:()=>string[], onGesture:(step)=>void, recording:()=>bool,
   *              onInjectFeedback:(action,targets,results)=>void}
   */
  constructor(serial, canvas, opts = {}) {
    this.serial = serial;
    this.canvas = canvas;
    this.stage = canvas.parentElement;
    this.ctx = canvas.getContext('2d');
    this.opts = opts;
    this.ws = null;
    this.decoder = null;
    this.configured = false;
    this.width = 0; this.height = 0;
    this.frames = 0; this.dropped = 0;
    this.fps = 0; this._fpsWin = [];
    this.closed = false;
    this.fallback = null;
    this._gesture = null;
  }

  targets() {
    if (this.opts.getTargets) return this.opts.getTargets();
    return [this.serial];
  }

  inject(action, params) {
    return sendInput(this.targets(), action, params, this.opts.onInjectFeedback);
  }

  async connect() {
    Diag.send('open', { mode: 'realtime' });
    // 立即标记连接开始，供后续的 sessionready 计算耗时用
    this._connectAt = performance.now();
    if (!('VideoDecoder' in window)) {
      this._startFallback('浏览器不支持 WebCodecs');
      return;
    }
    this.ws = new WebSocket(API.wsUrl(`/ws/mirror/${this.serial}?control=1`));
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const d = JSON.parse(ev.data);
        if (d.type === 'session') {
          this._onSession(d.width, d.height);
          // 记录连接开始时间（connect() 已在开头记录 _connectAt）
          if (d.width > 0 && d.height > 0) {
            this._readyAt = performance.now();
            const elapsed_ms = this._readyAt - (this._connectAt || 0);
            const elapsed_s = (elapsed_ms / 1000);
            Diag.send('sessionready', { width: d.width, height: d.height, elapsed: elapsed_s.toFixed(1), at: this._readyAt });
            if (window.Analytics) {
              window.Analytics.capture('mirror_ready', {
                elapsed_ms: Math.round(elapsed_ms),
                width: d.width, height: d.height,
              });
            }
            if (typeof ELAPSED_LOG === 'function') {
              const elapsed_ms = this._readyAt - (this._connectAt || 0);
              ELAPSED_LOG(elapsed_ms);
            }
          }
        }
        else if (d.type === 'device') this.deviceName = d.name;
        else if (d.type === 'error') { toast(`投屏提示: ${d.msg}`, 'error', 6000); this._startFallback(d.msg); }
        else if (d.type === 'stats') this.fps = d.fps;
      } else {
        this._onBinary(ev.data);
      }
    };
    this.ws.onclose = (ev) => {
      Diag.send('wsclose', { code: ev && ev.code, reason: (ev && ev.reason) || '' });
      if (!this.closed) this._startFallback('连接断开');
    };
    this.ws.onerror = () => {};
    this._bindInput();
  }

  _onSession(w, h) {
    if (w > 0 && h > 0) {
      this.width = w; this.height = h;
      this.canvas.width = w; this.canvas.height = h;
    }
  }

  _onBinary(buf) {
    const u8 = new Uint8Array(buf);
    const t = u8[0];
    if (t === 3) {                       // codec config (SPS/PPS, AVCC)
      this._configure(u8.subarray(1));
      return;
    }
    if (t !== 1) return;
    if (!this.configured || !this.decoder || this.decoder.state !== 'configured') {
      this.dropped++; return;
    }
    const dv = new DataView(buf);
    const key = dv.getUint8(1);
    const pts = Number(dv.getBigUint64(2));
    const payload = u8.subarray(10);
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta', timestamp: pts, data: payload,
      }));
    } catch (e) {
      this.dropped++;
    }
  }

  _configure(payload) {
    const nals = splitAvcc(payload);
    const sps = nals.find(n => (n[0] & 0x1f) === 7);
    const pps = nals.find(n => (n[0] & 0x1f) === 8);
    if (!sps || !pps) return;
    const codec = 'avc1.' + [...sps.slice(1, 4)].map(b => b.toString(16).padStart(2, '0')).join('');
    if (this.decoder) { try { this.decoder.close(); } catch (e) {} }
    this.configured = false;
    try {
      this.decoder = new VideoDecoder({
        output: (vf) => this._draw(vf),
        error: (e) => { console.warn('decoder error', e); this._startFallback('解码失败: ' + e.message); },
      }, { codec, description: buildAvcC(sps, pps), optimizeForLatency: true });
      this.configured = true;
    } catch (e) {
      this._startFallback('解码配置失败: ' + e.message);
    }
  }

  _draw(frame) {
    if (this.closed) { frame.close(); return; }
    if (frame.displayWidth !== this.canvas.width || frame.displayHeight !== this.canvas.height) {
      this.canvas.width = frame.displayWidth;
      this.canvas.height = frame.displayHeight;
      this.width = frame.displayWidth; this.height = frame.displayHeight;
    }
    this.ctx.drawImage(frame, 0, 0);
    frame.close();
    this.frames++;
    const now = performance.now();
    this._fpsWin.push(now);
    while (this._fpsWin.length && now - this._fpsWin[0] > 2000) this._fpsWin.shift();
    this.fps = (this._fpsWin.length / 2).toFixed(1);
  }

  // ---------- 输入: 全部经 HTTP /api/input ----------

  _bindInput() {
    const cv = this.canvas;
    cv.style.touchAction = 'none';
    cv.oncontextmenu = (e) => e.preventDefault();

    const startGesture = (nx, ny) => {
      this._gesture = { x0: nx, y0: ny, t0: performance.now(), moved: false };
    };
    // PointerEvent 优先；同时常备鼠标兜底（带去重），覆盖不支持/异常的浏览器环境
    if ('PointerEvent' in window) {
      cv.addEventListener('pointerdown', (e) => {
        if (this.fallback) return;
        // 部分环境 setPointerCapture 会抛错：绝不能因此丢弃整个手势
        try { cv.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
        const { nx, ny } = this._norm(e);
        startGesture(nx, ny);
        Diag.press('pointer');
      });
      cv.addEventListener('pointercancel', () => { this._gesture = null; });
    }
    cv.addEventListener('mousedown', (e) => {
      if (this.fallback) return;
      if (Date.now() - (this._pointerHandledAt || 0) < 300) return; // 同一次点击已被 Pointer 路径处理
      const { nx, ny } = this._norm(e);
      startGesture(nx, ny);
      Diag.press('mouse');
    });
    cv.addEventListener('mouseup', (e) => {
      if (!this._gesture) return;
      if (Date.now() - (this._pointerHandledAt || 0) < 300) return;
      this._endGesture(e);
    });
    cv.addEventListener('pointerup', (e) => {
      if (!this._gesture) return;
      this._pointerHandledAt = Date.now();
      this._endGesture(e);
    });
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const { nx, ny } = this._norm(e);
      const dir = e.deltaY < 0 ? -1 : 1;   // 上滚 = 内容上移
      const dy = dir * 0.18;
      this.inject('swipe', {
        x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4),
        x2_pct: +nx.toFixed(4), y2_pct: +Math.max(0, Math.min(1, ny + dy)).toFixed(4),
        duration_ms: 120,
      });
    }, { passive: false });
  }

  _endGesture(e) {
    const g = this._gesture; this._gesture = null;
    const { nx, ny } = this._norm(e);
    const dur = Math.max(60, Math.round(performance.now() - g.t0));
    rippleAt(this.stage, e.clientX, e.clientY);
    if (g.moved) {
      const p = {
        x_pct: +g.x0.toFixed(4), y_pct: +g.y0.toFixed(4),
        x2_pct: +nx.toFixed(4), y2_pct: +ny.toFixed(4), duration_ms: dur,
      };
      this.inject('swipe', p);
      this._record(Object.assign({ type: 'swipe' }, p));
    } else {
      const p = { x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4) };
      this.inject('tap', p);
      this._record(Object.assign({ type: 'tap' }, p));
    }
  }

  _norm(e) {
    const r = this.canvas.getBoundingClientRect();
    return {
      nx: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      ny: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }

  _record(step) {
    if (this.opts.recording && this.opts.recording() && this.opts.onGesture) {
      this.opts.onGesture(step);
    }
  }

  key(nameOrCode) {
    this.inject('key', { keycode: String(nameOrCode) });
    this._record({ type: 'key', keycode: String(nameOrCode) });
  }

  sendText(text) { this.inject('text', { text }); }

  /* 中文等非 ASCII 文本: 写入设备剪贴板并立即执行粘贴（需手机焦点在输入框、实时投屏模式） */
  pasteText(text) {
    if (this.fallback || !this.ws || this.ws.readyState !== 1) return false;
    this.ws.send(JSON.stringify({ action: 'set_clipboard', text, paste: true }));
    return true;
  }

  setClipboard(text) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ action: 'set_clipboard', text }));
    }
  }

  requestStats() { this.send({ action: 'stats' }); }

  send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  // ---------- 降级: 快照轮询(操作同样走 HTTP) ----------

  _startFallback(reason) {
    if (this.fallback || this.closed) return;
    Diag.send('fallback', { reason: String(reason || '').slice(0, 80) });    if (window.Analytics) {
      window.Analytics.capture('mirror_fallback', {
                reason: String(reason || '').slice(0, 80),
                elapsed_ms: Math.round(performance.now() - (this._connectAt || performance.now())),
              });
            }
            if (typeof ELAPSED_LOG === 'function') {
              const elapsed_ms = performance.now() - (this._connectAt || performance.now());
              ELAPSED_LOG(elapsed_ms);
            }
    console.warn('mirror fallback:', reason);
    this.fallback = new SnapshotMirror(this.serial, this.canvas, this.opts);
    this.fallback.start();
    if (this.ws) { try { this.ws.close(); } catch (e) {} }
    if (this.opts.onFallback) this.opts.onFallback(reason);
  }

  close() {
    this.closed = true;
    if (this.ws) { try { this.ws.close(); } catch (e) {} }
    if (this.decoder && this.decoder.state !== 'closed') { try { this.decoder.close(); } catch (e) {} }
    if (this.fallback) this.fallback.close();
  }
}

class SnapshotMirror {
  constructor(serial, canvas, opts = {}) {
    this.serial = serial; this.canvas = canvas; this.opts = opts;
    this.stage = canvas.parentElement;
    this.img = new Image();
    this.timer = null;
    this.gesture = null;
    this._bind();
  }
  start() {
    const tick = async () => {
      if (this.closed) return;
      try {
        const url = `/api/devices/${encodeURIComponent(this.serial)}/screenshot.png?t=${Date.now()}`;
        await new Promise((res, rej) => { this.img.onload = res; this.img.onerror = rej; this.img.src = url; });
        const cv = this.canvas;
        cv.width = this.img.naturalWidth || cv.width;
        cv.height = this.img.naturalHeight || cv.height;
        cv.getContext('2d').drawImage(this.img, 0, 0);
      } catch (e) { /* 设备可能离线 */ }
      this.timer = setTimeout(tick, 1500);
    };
    tick();
  }
  _norm(e) {
    const r = this.canvas.getBoundingClientRect();
    return {
      nx: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      ny: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }
  _bind() {
    const cv = this.canvas;
    cv.style.touchAction = 'none';
    cv.oncontextmenu = (e) => e.preventDefault();
    const startGesture = (nx, ny) => {
      this.gesture = { x0: nx, y0: ny, t0: performance.now(), moved: false };
    };
    const endGesture = (e) => {
      const g = this.gesture; this.gesture = null;
      const { nx, ny } = this._norm(e);
      const dur = Math.max(60, Math.round(performance.now() - g.t0));
      rippleAt(this.stage, e.clientX, e.clientY);
      if (g.moved) {
        sendInput(this.targets(), 'swipe', {
          x_pct: +g.x0.toFixed(4), y_pct: +g.y0.toFixed(4),
          x2_pct: +nx.toFixed(4), y2_pct: +ny.toFixed(4), duration_ms: dur,
        }, this.opts.onInjectFeedback);
        this._record({ type: 'swipe', x_pct: +g.x0.toFixed(4), y_pct: +g.y0.toFixed(4), x2_pct: +nx.toFixed(4), y2_pct: +ny.toFixed(4), duration_ms: dur });
      } else {
        sendInput(this.targets(), 'tap', { x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4) }, this.opts.onInjectFeedback);
        this._record({ type: 'tap', x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4) });
      }
    };
    // Pointer 优先 + 鼠标兜底（去重）；setPointerCapture 失败不丢弃手势
    if ('PointerEvent' in window) {
      cv.onpointerdown = (e) => {
        try { cv.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
        const { nx, ny } = this._norm(e);
        startGesture(nx, ny);
        Diag.press('pointer');
      };
      cv.onpointercancel = () => { this.gesture = null; };
      cv.onpointermove = (e) => {
        if (!this.gesture) return;
        const { nx, ny } = this._norm(e);
        if (Math.hypot(nx - this.gesture.x0, ny - this.gesture.y0) > 0.02) this.gesture.moved = true;
        this.gesture.x1 = nx; this.gesture.y1 = ny;
      };
      cv.onpointerup = (e) => {
        if (!this.gesture) return;
        this._pointerHandledAt = Date.now();
        endGesture(e);
      };
    }
    cv.onmousedown = (e) => {
      if (Date.now() - (this._pointerHandledAt || 0) < 300) return;
      const { nx, ny } = this._norm(e);
      startGesture(nx, ny);
      Diag.press('mouse');
    };
    cv.onmouseup = (e) => {
      if (!this.gesture) return;
      if (Date.now() - (this._pointerHandledAt || 0) < 300) return;
      endGesture(e);
    };
  }
  targets() {
    if (this.opts.getTargets) return this.opts.getTargets();
    return [this.serial];
  }
  _record(step) {
    if (this.opts.recording && this.opts.recording() && this.opts.onGesture) this.opts.onGesture(step);
  }
  close() { this.closed = true; if (this.timer) clearTimeout(this.timer); }
}
