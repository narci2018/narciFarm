/* 投屏组件：WebSocket + WebCodecs(H.264/AVCC) 解码渲染 + 触控手势
 * 服务端二进制消息: [0x01][key u8][pts u64][AVCC payload] 帧 | [0x03][config payload]
 * 文本 JSON: {type:"session"|"device"|"error"|"stats", ...}
 * 支持 WebCodecs 不可用时自动退化为快照轮询模式。 */

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

class Mirror {
  /**
   * @param opts {getTargets:()=>string[], onGesture:(step)=>void, recording:()=>bool}
   */
  constructor(serial, canvas, opts = {}) {
    this.serial = serial;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.opts = opts;
    this.ws = null;
    this.decoder = null;
    this.configured = false;
    this.width = 0; this.height = 0;
    this.frames = 0; this.dropped = 0;
    this.fps = 0; this._fpsWin = [];
    this.closed = false;
    this.fallback = null;   // SnapshotMirror
    this._gesture = null;
  }

  async connect() {
    if (!('VideoDecoder' in window)) {
      this._startFallback('浏览器不支持 WebCodecs');
      return;
    }
    this.ws = new WebSocket(API.wsUrl(`/ws/mirror/${this.serial}?control=1`));
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const d = JSON.parse(ev.data);
        if (d.type === 'session') this._onSession(d.width, d.height);
        else if (d.type === 'device') this.deviceName = d.name;
        else if (d.type === 'injected') console.debug('[narci] 注入回执', d);
        else if (d.type === 'error') { toast(`投屏失败: ${d.msg}`, 'error'); this._startFallback(d.msg); }
        else if (d.type === 'stats') this.fps = d.fps;
      } else {
        this._onBinary(ev.data);
      }
    };
    this.ws.onclose = () => { if (!this.closed) this._startFallback('连接断开'); };
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
    if (t !== 1) return;                 // 0x02 预留
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

  // ---------- 输入 ----------

  _bindInput() {
    const cv = this.canvas;
    cv.oncontextmenu = (e) => e.preventDefault();

    cv.addEventListener('pointerdown', (e) => {
      if (this.fallback) return;
      cv.setPointerCapture(e.pointerId);
      const { nx, ny } = this._norm(e);
      this._gesture = { x0: nx, y0: ny, t0: performance.now(), moved: false };
    });
    cv.addEventListener('pointermove', (e) => {
      if (!this._gesture) return;
      const { nx, ny } = this._norm(e);
      const dx = nx - this._gesture.x0, dy = ny - this._gesture.y0;
      if (Math.hypot(dx, dy) > 0.012) this._gesture.moved = true;
      this._gesture.x1 = nx; this._gesture.y1 = ny;
    });
    cv.addEventListener('pointerup', (e) => {
      if (!this._gesture) return;
      const g = this._gesture; this._gesture = null;
      const { nx, ny } = this._norm(e);
      const dur = Math.max(60, Math.round(performance.now() - g.t0));
      if (g.moved) {
        this.send({ action: 'swipe', nx1: g.x0, ny1: g.y0, nx2: nx, ny2: ny, duration_ms: dur });
        this._record({ type: 'swipe', x_pct: +g.x0.toFixed(4), y_pct: +g.y0.toFixed(4), x2_pct: +nx.toFixed(4), y2_pct: +ny.toFixed(4), duration_ms: dur });
      } else {
        this.send({ action: 'tap', nx: +nx.toFixed(4), ny: +ny.toFixed(4) });
        this._record({ type: 'tap', x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4) });
      }
    });
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const { nx, ny } = this._norm(e);
      const delta = e.deltaY < 0 ? 1.0 : -1.0;
      this.send({ action: 'scroll', nx, ny, delta });
    }, { passive: false });
  }

  _norm(e) {
    const r = this.canvas.getBoundingClientRect();
    return {
      nx: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      ny: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }

  send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      if (!obj.targets && this.opts.getTargets) obj.targets = this.opts.getTargets();
      console.debug('[narci] 发送指令', obj);
      this.ws.send(JSON.stringify(obj));
    } else {
      console.warn('[narci] 指令未发送: WebSocket 未就绪', obj);
    }
  }

  _record(step) {
    if (this.opts.recording && this.opts.recording() && this.opts.onGesture) {
      this.opts.onGesture(step);
    }
  }

  key(nameOrCode) {
    this.send({ action: 'key', keycode: String(nameOrCode) });
    this._record({ type: 'key', keycode: String(nameOrCode) });
  }

  sendText(text) { this.send({ action: 'text', text }); }

  setClipboard(text) { this.send({ action: 'set_clipboard', text }); }

  requestStats() { this.send({ action: 'stats' }); }

  // ---------- 降级：快照轮询 ----------

  _startFallback(reason) {
    if (this.fallback || this.closed) return;
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
    cv.oncontextmenu = (e) => e.preventDefault();
    cv.onpointerdown = (e) => {
      cv.setPointerCapture(e.pointerId);
      const { nx, ny } = this._norm(e);
      this.gesture = { x0: nx, y0: ny, t0: performance.now(), moved: false };
    };
    cv.onpointermove = (e) => {
      if (!this.gesture) return;
      const { nx, ny } = this._norm(e);
      if (Math.hypot(nx - this.gesture.x0, ny - this.gesture.y0) > 0.02) this.gesture.moved = true;
      this.gesture.x1 = nx; this.gesture.y1 = ny;
    };
    cv.onpointerup = async (e) => {
      if (!this.gesture) return;
      const g = this.gesture; this.gesture = null;
      const { nx, ny } = this._norm(e);
      const dur = Math.max(60, Math.round(performance.now() - g.t0));
      try {
        if (g.moved) {
          const r = await API.post(`/api/devices/${encodeURIComponent(this.serial)}/input`,
            { action: 'swipe', params: { x_pct: g.x0, y_pct: g.y0, x2_pct: nx, y2_pct: ny, duration_ms: dur } });
          if (r && r.ok === false) { toast(r.error, 'error', 6000); return; }
          this._record({ type: 'swipe', x_pct: +g.x0.toFixed(4), y_pct: +g.y0.toFixed(4), x2_pct: +nx.toFixed(4), y2_pct: +ny.toFixed(4), duration_ms: dur });
        } else {
          const r = await API.post(`/api/devices/${encodeURIComponent(this.serial)}/input`,
            { action: 'tap', params: { x_pct: nx, y_pct: ny } });
          if (r && r.ok === false) { toast(r.error, 'error', 6000); return; }
          this._record({ type: 'tap', x_pct: +nx.toFixed(4), y_pct: +ny.toFixed(4) });
        }
      } catch (err) { toast(err.message, 'error'); }
    };
  }
  _record(step) {
    if (this.opts.recording && this.opts.recording() && this.opts.onGesture) this.opts.onGesture(step);
  }
  close() { this.closed = true; if (this.timer) clearTimeout(this.timer); }
}
