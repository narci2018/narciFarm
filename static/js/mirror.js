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

function annexbToAvcc(payload) {
  // 部分设备(如本例红米)的帧负载是 Annex-B(起始码), WebCodecs avcC 描述需要 AVCC(长度前缀)
  const nals = [];
  let start = -1;
  for (let p = 0; p < payload.length; p++) {
    if (payload[p] === 0 && payload[p+1] === 0 && payload[p+2] === 1) {
      if (start !== -1) nals.push(payload.subarray(start, p));
      start = p + 3; p += 2;
    } else if (payload[p] === 0 && payload[p+1] === 0 && payload[p+2] === 0 && payload[p+3] === 1) {
      if (start !== -1) nals.push(payload.subarray(start, p));
      start = p + 4; p += 3;
    }
  }
  if (start !== -1) nals.push(payload.subarray(start));
  let total = 0;
  for (const n of nals) total += 4 + n.length;
  const out = new Uint8Array(total);
  let o = 0;
  for (const n of nals) {
    out[o++] = (n.length >>> 24) & 0xFF; out[o++] = (n.length >>> 16) & 0xFF;
    out[o++] = (n.length >>> 8) & 0xFF; out[o++] = n.length & 0xFF;
    out.set(n, o); o += n.length;
  }
  return out;
}

function toAvccIfNeeded(payload) {
  if (payload.length < 5) return payload;
  const b = payload;
  const isAnnexB = (b[0] === 0 && b[1] === 0 && b[2] === 1) ||
                   (b[0] === 0 && b[1] === 0 && b[2] === 0 && b[3] === 1);
  return isAnnexB ? annexbToAvcc(payload) : payload;
}

function splitAnnexB(payload) {
  // 按起始码 00 00 01 / 00 00 00 01 切分(部分设备的 SPS/PPS 配置包是 Annex-B 格式)
  const nals = [];
  let start = -1;
  for (let p = 0; p < payload.byteLength; p++) {
    if (payload[p] === 0 && payload[p+1] === 0 && payload[p+2] === 1) {
      if (start !== -1) nals.push(payload.subarray(start, p));
      start = p + 3; p += 2;
    } else if (payload[p] === 0 && payload[p+1] === 0 && payload[p+2] === 0 && payload[p+3] === 1) {
      if (start !== -1) nals.push(payload.subarray(start, p));
      start = p + 4; p += 3;
    }
  }
  if (start !== -1) nals.push(payload.subarray(start));
  return nals;
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
    // 直连优先: 远程设备直连所属 Agent(省中心带宽), 失败回退中心中继
    const useDirect = this.opts.direct !== false && localStorage.getItem('narciFarm.direct') !== '0';
    if (useDirect) {
      try {
        const r = await API.get(`/api/devices/${encodeURIComponent(this.serial)}/direct_token`);
        const base = r.agent_url.startsWith('https') ? 'wss://' + r.agent_url.slice(8)
                                                       : 'ws://' + r.agent_url.slice(7);
        this._setupWs(`${base}/agent/mirror/${this.serial}?direct=${encodeURIComponent(r.token)}`);
        this._wsMode = 'direct';
        return;
      } catch (e) {
        console.warn('[narci] 直连失败, 回退中心中继:', e.message);
      }
    }
    const q = (this.opts.quality || 'medium');
    this._setupWs(API.wsAuthed(`/ws/mirror/${this.serial}?control=1&quality=${q}`));
    this._wsMode = 'relay';
  }

  _setupWs(url) {
    this.ws = new WebSocket(url);
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
    if (this.opts.onReady) this.opts.onReady();
  }

  _onBinary(buf) {
    const u8 = new Uint8Array(buf);
    const t = u8[0];
    if (t === 3) {                       // codec config (SPS/PPS, AVCC)
      this._configure(u8.subarray(1));
      return;
    }
    if (t !== 1) return;
    const dv = new DataView(buf);
    const key = dv.getUint8(1);
    const pts = Number(dv.getBigUint64(2));
    const payload = u8.subarray(10);
    // 兜底: 首个关键帧内联 SPS/PPS 时(部分编码器不单独发配置包), 从中提取并配置解码器
    if (!this.configured && key) {
      this._configure(payload);
    }
    if (!this.configured || !this.decoder || this.decoder.state !== 'configured') {
      this.dropped++; return;
    }
    // 解码积压时跳非关键帧（保证画面延迟有界）
    if (this.decoder.decodeQueueSize > 12 && !key) {
      this.dropped++;
      return;
    }
    const chunkData = toAvccIfNeeded(payload);
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta', timestamp: pts, data: chunkData,
      }));
    } catch (e) {
      if (!this._errLogged) {
        this._errLogged = true;
        console.warn('[narci] 解码异常:', String(e), '| key=', key, 'pts=', pts, 'len=', payload.length);
      }
      this.dropped++;
    }
  }

  _configure(payload) {
    let nals = splitAvcc(payload);
    let sps = nals.find(n => (n[0] & 0x1f) === 7);
    let pps = nals.find(n => (n[0] & 0x1f) === 8);
    if (!sps || !pps) {           // AVCC 里找不到 → 尝试 Annex-B(部分设备配置包格式)
      nals = splitAnnexB(payload);
      sps = nals.find(n => (n[0] & 0x1f) === 7);
      pps = nals.find(n => (n[0] & 0x1f) === 8);
    }
    if (!sps || !pps) return;
    const codec = 'avc1.' + [...sps.slice(1, 4)].map(b => b.toString(16).padStart(2, '0')).join('');
    if (this.decoder) { try { this.decoder.close(); } catch (e) {} }
    this.configured = false;
    try {
      // WebCodecs: 构造函数只接受 init; 配置必须通过 configure() 单独调用
      this.decoder = new VideoDecoder({
        output: (vf) => this._draw(vf),
        error: (e) => { console.warn('decoder error', e); this._startFallback('解码失败: ' + e.message); },
      });
      this.decoder.configure({ codec, description: buildAvcC(sps, pps), optimizeForLatency: true });
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
    const moveGesture = (e) => {
      if (!this._gesture) return;
      const { nx, ny } = this._norm(e);
      if (Math.hypot(nx - this._gesture.x0, ny - this._gesture.y0) > 0.012) this._gesture.moved = true;
      this._gesture.x1 = nx; this._gesture.y1 = ny;
    };

    // PointerEvent 优先(含移动追踪, 拖拽=滑动); 不支持的环境回退鼠标路径。两路径互斥, 避免双重手势。
    if ('PointerEvent' in window) {
      cv.addEventListener('pointerdown', (e) => {
        if (this.fallback) return;
        try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 捕获失败不丢弃手势 */ }
        const { nx, ny } = this._norm(e);
        startGesture(nx, ny);
        Diag.press('pointer');
      });
      cv.addEventListener('pointermove', moveGesture);
      cv.addEventListener('pointerup', (e) => {
        if (!this._gesture) return;
        this._endGesture(e);
      });
      cv.addEventListener('pointercancel', () => { this._gesture = null; });
    } else {
      cv.addEventListener('mousedown', (e) => {
        if (this.fallback) return;
        const { nx, ny } = this._norm(e);
        startGesture(nx, ny);
        Diag.press('mouse');
      });
      cv.addEventListener('mousemove', moveGesture);
      cv.addEventListener('mouseup', (e) => {
        if (!this._gesture) return;
        this._endGesture(e);
      });
    }
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      // 节流: 一次滚动只发一条 swipe（高频率 wheel 事件会刷屏）
      const now = performance.now();
      if (now - (this._lastWheelAt || 0) < 350) return;
      this._lastWheelAt = now;
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
