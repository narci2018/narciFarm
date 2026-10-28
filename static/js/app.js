/* narciFarm 主控制器：设备/分组/总览/广播/批量/场景/调度/控制台/截图/日志 */

const State = {
  devices: [],
  groups: [],
  scenarios: [],
  schedules: [],
  apps: [],
  selected: new Set(),
  settings: {},
  settingsLoaded: false,
  currentScenario: null,
  mirror: null,
  lastShotAt: {},
};

const $ = (id) => document.getElementById(id);

window.appSelectedDevices = () => State.devices.filter(d => State.selected.has(d.serial)).map(d => d.serial);

// 使用分析是 ES module（defer 执行）：等它就绪且设置已加载后再初始化
let analyticsBooted = false;
function bootAnalytics() {
  if (analyticsBooted || !window.Analytics || !State.settingsLoaded) return;
  analyticsBooted = true;
  window.Analytics.init(State.settings);
}
document.addEventListener('narci:analytics-ready', bootAnalytics);

// ---------- 初始化 ----------

async function init() {
  try { State.settings = await API.get('/api/settings'); } catch (e) {}
  // 使用分析：默认关闭，用户勾选「使用分析」后才初始化（见 analytics.js）
  State.settingsLoaded = true;
  bootAnalytics();
  $('st-adb').classList.toggle('on', !!State.settings.adb_ok);
  $('st-jar').classList.toggle('on', !!State.settings.server_jar_ok);
  if (!State.settings.adb_ok) toast('未找到 adb，请设置 NARCI_ADB 环境变量', 'error');
  if (!State.settings.server_jar_ok) toast('缺少 scrcpy-server.jar，运行 scripts/fetch_scrcpy_server.py', 'error', 8000);

  // 同步版本号（若服务端 __version__ 已更新则会自动生效）
  try { const v = await API.get('/api/version'); State.version = String(v.version || '?'); $('st-version').textContent = State.version; } catch (e) { $('st-version').textContent = '?'; }

  applyTheme((function () { try { return localStorage.getItem('narciFarm.theme'); } catch (e) { return null; } })() || 'dark');
  $('btn-theme').onclick = () => applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  bindTabs();
  bindSidebar();
  bindOverview();
  bindBroadcast();
  bindBatch();
  bindScenario();
  $('tpl-upload').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    await API.post('/api/templates/upload', fd);
    toast('模板已上传', 'ok');
    loadTemplates();
    e.target.value = '';
  };
  bindSchedule();
  bindConsole();
  bindApps();
  bindDevTools();
  bindCaptures();
  bindModal();
  connectEventStream();

  await Promise.all([refreshDevices(), refreshGroups(), refreshScenarios(), refreshSchedules()]);
  if (window.Analytics) {
    window.Analytics.capture('app_loaded', {
      devices: State.devices.length,
      online: State.devices.filter(d => d.state === 'device').length,
    });
  }
  setInterval(() => refreshDevices(true), 3000);
  setInterval(refreshRuns, 5000);
  setInterval(snapshotTicker, 1000);
  setInterval(pollStreamStats, 3000);
}

// ---------- 事件流 ----------

function connectEventStream() {
  const logView = $('log-view');
  const ws = new WebSocket(API.wsUrl('/ws/events'));
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    const line = document.createElement('div');
    line.className = `log-${d.level}`;
    line.textContent = `[${new Date(d.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}] ${d.msg}`;
    logView.appendChild(line);
    while (logView.childElementCount > 500) logView.firstChild.remove();
    if ($('log-autoscroll').checked) logView.scrollTop = logView.scrollHeight;
    if (d.level === 'error') toast(d.msg.slice(0, 120), 'error');
  };
  ws.onclose = () => setTimeout(connectEventStream, 3000);
}

// ---------- 设备与分组 ----------

async function refreshDevices(quiet = false) {
  try {
    State.devices = await API.get('/api/devices');
    const online = State.devices.filter(d => d.state === 'device').length;
    $('st-online').textContent = online;
    renderDeviceList();
    renderDeviceGrid();
    fillDeviceSelects();
  } catch (e) {
    if (!quiet) toast(e.message, 'error');
  }
}

function renderDeviceList() {
  const ul = $('device-list');
  ul.innerHTML = '';
  for (const d of State.devices) {
    const li = document.createElement('li');
    li.innerHTML = `
      <input type="checkbox" ${State.selected.has(d.serial) ? 'checked' : ''}>
      <span class="dstate ${d.state}"></span>
      <span class="dserial" title="${esc(d.serial)}${d.notes ? ' · ' + esc(d.notes) : ''}">${esc(d.serial)}</span>
      <span class="dmodel">${esc(d.model || d.android_version || '')}</span>
      ${d.notes ? `<span class="dnote">${esc(d.notes)}</span>` : ''}`;
    li.querySelector('input').onchange = (e) => {
      if (e.target.checked) State.selected.add(d.serial); else State.selected.delete(d.serial);
    };
    const noteBtn = document.createElement('button');
    noteBtn.className = 'btn small';
    noteBtn.textContent = '✎';
    noteBtn.title = '编辑备注';
    noteBtn.onclick = async (e) => {
      e.stopPropagation();
      const v = await showPrompt({ title: '设备备注', message: d.serial, defaultValue: d.notes || '' });
      if (v === null) return;
      await API.put(`/api/devices/${encodeURIComponent(d.serial)}`, { notes: v });
      toast('备注已保存', 'ok');
      refreshDevices(true);
    };
    li.appendChild(noteBtn);
    ul.appendChild(li);
  }
}

function renderDeviceGrid() {
  const grid = $('device-grid');
  const existing = new Map([...grid.children].map(c => [c.dataset.serial, c]));
  for (const d of State.devices) {
    let card = existing.get(d.serial);
    if (!card) {
      card = document.createElement('div');
      card.className = 'device-card';
      card.innerHTML = `
        <img class="shot" alt="snapshot">
        <div class="meta"><span class="dstate"></span><span class="name"></span></div>`;
      card.querySelector('.shot').onclick = () => openMirror(d.serial);
      grid.appendChild(card);
    }
    card.dataset.serial = d.serial;
    card.classList.toggle('offline', d.state !== 'device');
    card.querySelector('.name').textContent = `${d.serial}${d.model ? ' · ' + d.model : ''}`;
    const dot = card.querySelector('.dstate');
    dot.className = `dstate ${d.state}`;
    existing.delete(d.serial);
  }
  existing.forEach(c => c.remove());
}

async function pollStreamStats() {
  for (const d of State.devices) {
    if (d.state !== 'device') continue;
    const card = document.querySelector(`.device-card[data-serial="${CSS.escape(d.serial)}"]`);
    if (!card) continue;
    let badge = card.querySelector('.fps-badge');
    try {
      const st = await API.get(`/api/devices/${encodeURIComponent(d.serial)}/stream_stats`);
      if (st.active) {
        if (!badge) {
          badge = document.createElement('span');
          badge.className = 'fps-badge';
          card.querySelector('.meta').appendChild(badge);
        }
        badge.textContent = `▶ ${st.fps}fps`;
      } else if (badge) badge.remove();
    } catch (e) { if (badge) badge.remove(); }
  }
}

function snapshotTicker() {
  if (!$('tab-overview').classList.contains('active')) return;
  if (document.hidden) return;
  if (!$('ov-autorefresh').checked) return;
  const interval = (State.settings.snapshot_interval || 4) * 1000;
  const now = Date.now();
  for (const d of State.devices) {
    if (d.state !== 'device') continue;
    if (now - (State.lastShotAt[d.serial] || 0) < interval) continue;
    State.lastShotAt[d.serial] = now + Math.random() * 600;
    const card = document.querySelector(`.device-card[data-serial="${CSS.escape(d.serial)}"] img`);
    if (card) card.src = `/api/devices/${encodeURIComponent(d.serial)}/screenshot.png?t=${Date.now()}`;
  }
}

function bindOverview() {
  $('btn-refresh').onclick = () => refreshDevices().then(() => toast('设备已刷新', 'ok'));
  $('btn-scan').onclick = async () => {
    toast('正在扫描局域网 5555 端口...');
    try {
      const r = await API.post('/api/devices/scan_lan', {});
      toast(`扫描完成，发现 ${r.found.length} 台: ${r.found.join(', ') || '无'}`, r.found.length ? 'ok' : 'info', 6000);
      refreshDevices(true);
    } catch (e) { toast(e.message, 'error'); }
  };
  $('btn-shutdown').onclick = async () => {
    const ok = await showConfirm({
      title: '关闭服务', icon: '⏻',
      message: '确定关闭 narciFarm 服务？投屏会话与后台任务将一并停止。',
      okText: '确定关闭', danger: true,
    });
    if (!ok) return;
    try {
      await API.post('/api/shutdown');
      document.body.innerHTML = '<div class="shutdown-page">' +
        '<div class="shutdown-icon">⏻</div>' +
        '<div class="shutdown-title">服务已关闭</div>' +
        '<div class="shutdown-sub">此页面不再可用。如需再次使用，请重新运行 narciFarm.exe。</div></div>';
    } catch (e) {
      toast(e.message.replace(/^\d+ /, ''), 'error', 6000);
    }
  };
}

function fillDeviceSelects() {
  const online = State.devices.filter(d => d.state === 'device');
  for (const [selId] of [['bc-master'], ['cs-device'], ['am-device'], ['dt-device']]) {
    const sel = $(selId);
    if (!sel) continue;
    const cur = sel.value;
    sel.innerHTML = online.map(d => `<option value="${esc(d.serial)}">${esc(d.serial)} ${esc(d.model || '')}</option>`).join('');
    if (cur && online.some(d => d.serial === cur)) sel.value = cur;
  }
  const sd = $('sd-group');
  if (sd) {
    const cur = sd.value;
    sd.innerHTML = State.groups.map(g => `<option value="${g.id}">${esc(g.name)} (${g.members.length})</option>`).join('');
    if (cur) sd.value = cur;
  }
}

async function refreshGroups() {
  State.groups = await API.get('/api/groups');
  const ul = $('group-list');
  ul.innerHTML = '';
  for (const g of State.groups) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="gname">${esc(g.name)}</span>
      <span class="gcount">${g.members.length} 台</span>
      <button class="btn small danger">删</button>`;
    li.querySelector('button').onclick = async () => {
      await API.del(`/api/groups/${g.id}`);
      refreshGroups();
    };
    ul.appendChild(li);
  }
  const asg = $('assign-group');
  asg.innerHTML = State.groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('');
}

function bindSidebar() {
  $('btn-add-group').onclick = async () => {
    const name = $('group-name').value.trim();
    if (!name) return toast('请输入分组名', 'error');
    await API.post('/api/groups', { name });
    $('group-name').value = '';
    refreshGroups();
  };
  $('btn-assign').onclick = async () => {
    const gid = $('assign-group').value;
    if (!gid) return toast('请先创建分组', 'error');
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    const g = State.groups.find(x => String(x.id) === String(gid));
    const merged = [...new Set([...(g ? g.members : []), ...serials])];
    await API.post(`/api/groups/${gid}/members`, { serials: merged });
    toast(`已将 ${serials.length} 台设备加入分组`, 'ok');
    refreshGroups();
  };
}

// ---------- Tabs ----------

function bindTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tabpane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      $(`tab-${tab.dataset.tab}`).classList.add('active');
      if (tab.dataset.tab === 'captures') loadCaptures();
      if (tab.dataset.tab === 'apps') loadApps();
      if (tab.dataset.tab === 'devtools') refreshDevTools();
      if (tab.dataset.tab === 'scenario') loadTemplates();
      if (tab.dataset.tab === 'schedule') refreshSchedules();
    };
  });
}

// ---------- 群控广播 ----------

function bindBroadcast() {
  $('bc-open').onclick = () => {
    const serial = $('bc-master').value;
    if (!serial) return toast('没有在线设备', 'error');
    openMirror(serial, { defaultBroadcast: true });
  };
  $('bc-send-text').onclick = () => runBroadcast('/api/broadcast', { action: 'text', params: { text: $('bc-text').value } });
  $('bc-send-key').onclick = () => runBroadcast('/api/broadcast', { action: 'key', params: { keycode: $('bc-key').value } });
  $('bc-stayon').onclick = async () => {
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    await Promise.all(serials.map(s => API.post(`/api/devices/${encodeURIComponent(s)}/stay_awake`, { on: true }).catch(() => {})));
    toast(`已对 ${serials.length} 台设备开启 stay awake`, 'ok');
  };
}

async function runBroadcast(url, body) {
  const serials = window.appSelectedDevices();
  if (!serials.length) return toast('请先勾选设备', 'error');
  try {
    const r = await API.post(url, { ...body, targets: serials });
    if (window.Analytics) {
      window.Analytics.capture('broadcast_sent', { action: body.action, targets: serials.length, ok: r.ok !== false });
    }
    let text = `发送 ${r.sent}/${serials.length}` + (Object.keys(r.failed).length ? `，失败: ${JSON.stringify(r.failed)}` : '');
    const notes = [...new Set(Object.values(r.notes || {}))];
    if (notes.length) text += `\n${notes.join('\n')}`;
    $('bc-result').textContent = text;
    notes.forEach(n => toast(n, 'warn', 7000));
  } catch (e) { toast(e.message, 'error'); }
}

// ---------- 批量操作 ----------

function bindBatch() {
  $('batch-install').onclick = async () => {
    const f = $('batch-apk').files[0];
    if (!f) return toast('请选择 APK 文件', 'error');
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    const box = $('batch-install-result');
    box.textContent = `上传 ${f.name} (${(f.size / 1048576).toFixed(1)}MB)...`;
    for (const s of serials) {
      const fd = new FormData();
      fd.append('apk', f);
      try {
        const r = await API.post(`/api/devices/${encodeURIComponent(s)}/install`, fd);
        box.textContent += `\n[${s}] ${r.out || 'OK'}`;
      } catch (e) { box.textContent += `\n[${s}] 失败: ${e.message}`; }
    }
  };
  $('batch-push').onclick = async () => {
    const f = $('batch-file').files[0];
    const remote = $('batch-remote').value.trim();
    if (!f || !remote) return toast('请选择文件并填写远端路径', 'error');
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    const box = $('batch-push-result');
    box.textContent = '推送中...';
    for (const s of serials) {
      const fd = new FormData();
      fd.append('file', f);
      fd.append('remote', remote.endsWith('/') ? remote + f.name : remote + '/' + f.name);
      try {
        await API.post(`/api/devices/${encodeURIComponent(s)}/push`, fd);
        box.textContent += `\n[${s}] OK`;
      } catch (e) { box.textContent += `\n[${s}] 失败: ${e.message}`; }
    }
  };
  $('batch-shell').onclick = async () => {
    const cmd = $('batch-shell-cmd').value.trim();
    if (!cmd) return;
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    const box = $('batch-shell-result');
    box.textContent = '执行中...';
    await Promise.all(serials.map(async (s) => {
      try {
        const r = await API.post(`/api/devices/${encodeURIComponent(s)}/shell`, { cmd });
        box.textContent += `\n===== [${s}] rc=${r.rc}\n${r.out.slice(0, 800)}`;
      } catch (e) { box.textContent += `\n[${s}] 失败: ${e.message}`; }
    }));
  };
  $('batch-shot').onclick = async () => {
    const serials = window.appSelectedDevices();
    if (!serials.length) return toast('请先勾选设备', 'error');
    const ok = await Promise.all(serials.map(s =>
      API.post(`/api/devices/${encodeURIComponent(s)}/screenshot`).then(() => s).catch(() => null)));
    const done = ok.filter(Boolean);
    toast(`截图完成 ${done.length}/${serials.length}，已存入截图库`, 'ok');
  };
}

// ---------- 场景 ----------

async function refreshScenarios() {
  State.scenarios = await ScenarioEditor.loadList();
  const ul = $('sc-list');
  ul.innerHTML = '';
  for (const sc of State.scenarios) {
    const li = document.createElement('li');
    if (State.currentScenario && State.currentScenario.id === sc.id) li.classList.add('active');
    li.innerHTML = `<span>#${sc.id} ${esc(sc.name)}</span><span class="spacer"></span><button class="btn small danger">删</button>`;
    li.onclick = async (e) => {
      if (e.target.tagName === 'BUTTON') {
        await API.del(`/api/scenarios/${sc.id}`);
        if (State.currentScenario && State.currentScenario.id === sc.id) State.currentScenario = null;
        refreshScenarios();
        return;
      }
      State.currentScenario = sc;
      ScenarioEditor.open(sc);
      refreshScenarios();
    };
    ul.appendChild(li);
  }
  const sdSel = $('sd-scenario');
  if (sdSel) {
    const cur = sdSel.value;
    sdSel.innerHTML = State.scenarios.map(s => `<option value="${s.id}">#${s.id} ${esc(s.name)}</option>`).join('');
    if (cur) sdSel.value = cur;
  }
}
window.appRefreshScenarios = refreshScenarios;

// ---------- 图像模板管理 ----------

async function loadTemplates() {
  try {
    const r = await API.get('/api/templates');
    const ul = $('tpl-list');
    ul.innerHTML = '';
    for (const t of r.templates) {
      const li = document.createElement('li');
      li.innerHTML = `<a href="/api/templates/${encodeURIComponent(t.name)}/preview" target="_blank" style="flex:1">${esc(t.name)}</a>
        <span class="hint">${t.width}x${t.height}</span>
        <button class="btn small danger">删</button>`;
      li.querySelector('button').onclick = async () => {
        await API.del(`/api/templates/${encodeURIComponent(t.name)}`);
        loadTemplates();
      };
      ul.appendChild(li);
    }
    if (r.templates.length === 0) {
      ul.innerHTML = '<li class="hint">暂无模板(上传截图区域作为匹配目标)</li>';
    }
    ul.innerHTML += r.ocr_available
      ? '<li class="hint">OCR 可用</li>'
      : '<li class="hint" style="color:var(--warn)">OCR 需系统安装 Tesseract</li>';
  } catch (e) { toast(e.message, 'error'); }
}

function parseVars(text) {
  const vars = {};
  for (const line of (text || '').split(String.fromCharCode(10))) {
    const m = line.match(/^\s*([\w一-龥]+)\s*=\s*(.+)$/);
    if (m) vars[m[1]] = m[2].trim();
  }
  return vars;
}

function bindScenario() {
  $('sc-new').onclick = () => { State.currentScenario = null; ScenarioEditor.open(null); };
  $('sc-add').onclick = () => ScenarioEditor.addStep($('sc-add-type').value);
  $('sc-save').onclick = () => ScenarioEditor.save().then(refreshScenarios).catch(e => toast(e.message, 'error'));
  $('sc-run').onclick = () => ScenarioEditor.run();
  $('sc-apply-raw').onclick = () => ScenarioEditor.applyRaw();
  $('sc-abort').onclick = async () => {
    const runs = await API.get('/api/runs');
    const active = runs.filter(r => r.status === 'running');
    for (const r of active) await API.post(`/api/runs/${r.run_id}/abort`);
    toast(active.length ? `已请求中止 ${active.length} 个运行` : '没有进行中的运行', 'ok');
  };
  $('sc-import').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const arr = JSON.parse(await f.text());
      if (!Array.isArray(arr)) throw new Error('必须是步骤数组');
      $('sc-editor-raw').value = JSON.stringify(arr, null, 2);
      ScenarioEditor.applyRaw();
    } catch (err) { toast(`导入失败: ${err.message}`, 'error'); }
    e.target.value = '';
  };
  $('sc-export').onclick = () => {
    const blob = new Blob([JSON.stringify(ScenarioEditor.steps, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `narci_scenario_${Date.now()}.json`;
    a.click();
  };
}

async function refreshRuns() {
  if (!$('tab-scenario').classList.contains('active')) return;
  try {
    const runs = await API.get('/api/runs');
    const box = $('sc-runs');
    if (!runs.length) { box.textContent = '暂无运行'; return; }
    box.textContent = runs.map(r => {
      const devs = Object.entries(r.devices).map(([s, v]) => `${s}:${v.status}(${v.done || 0})`).join('  ');
      return `[${r.run_id.slice(0, 8)}] ${r.status} r${r.repeat}  ${devs}`;
    }).join('\n');
  } catch (e) {}
}

// ---------- 调度 ----------

async function refreshSchedules() {
  try {
    State.schedules = await API.get('/api/schedules');
  } catch (e) { return; }
  const tb = $('sd-table').querySelector('tbody');
  tb.innerHTML = '';
  const scen = Object.fromEntries(State.scenarios.map(s => [s.id, s.name]));
  for (const s of State.schedules) {
    const tr = document.createElement('tr');
    const target = JSON.parse(s.target || '{}');
    const g = State.groups.find(x => x.id === target.group_id);
    tr.innerHTML = `
      <td>${s.id}</td><td>${esc(s.name)}</td><td>${esc(scen[s.scenario_id] || s.scenario_id)}</td>
      <td>${g ? esc(g.name) : (target.serials || []).length + ' 台'}</td>
      <td>${s.kind === 'daily' ? '每天' : '间隔'}</td><td>${esc(s.value)}</td>
      <td>${esc(s.last_run || '-')}</td>
      <td>${s.enabled ? '✅启用' : '⏸停用'}</td>
      <td>
        <button class="btn small" data-act="toggle">${s.enabled ? '停用' : '启用'}</button>
        <button class="btn small danger" data-act="del">删</button>
      </td>`;
    tr.querySelector('[data-act="toggle"]').onclick = async () => {
      await API.put(`/api/schedules/${s.id}`, { enabled: s.enabled ? 0 : 1 });
      refreshSchedules();
    };
    tr.querySelector('[data-act="del"]').onclick = async () => {
      await API.del(`/api/schedules/${s.id}`);
      refreshSchedules();
    };
    tb.appendChild(tr);
  }
}

function bindSchedule() {
  $('sd-kind').onchange = () => {
    const k = $('sd-kind').value;
    $('sd-value').placeholder = k === 'cron' ? '*/5 * * * *' : (k === 'daily' ? '09:30' : '3600');
  };
  $('sd-add').onclick = async () => {
    const scenarioId = parseInt($('sd-scenario').value);
    if (!scenarioId) return toast('请先创建场景', 'error');
    const gid = $('sd-group').value;
    if (!gid) return toast('请先创建分组作为调度目标', 'error');
    try {
      await API.post('/api/schedules', {
        name: $('sd-name').value.trim() || `调度${Date.now() % 10000}`,
        scenario_id: scenarioId,
        target: { group_id: parseInt(gid) },
        kind: $('sd-kind').value,
        value: $('sd-value').value.trim() || '3600',
        enabled: $('sd-enabled').checked ? 1 : 0,
      });
      toast('调度已添加', 'ok');
      refreshSchedules();
    } catch (e) { toast(e.message, 'error'); }
  };
}

// ---------- 控制台 ----------

function bindConsole() {
  const out = $('cs-out');
  const run = async () => {
    const serial = $('cs-device').value;
    const cmd = $('cs-cmd').value.trim();
    if (!serial || !cmd) return;
    out.textContent += `\n$ ${cmd}\n`;
    try {
      const r = await API.post(`/api/devices/${encodeURIComponent(serial)}/shell`, { cmd, timeout: 30 });
      out.textContent += (r.out || '(无输出)') + `\n[rc=${r.rc}]\n`;
    } catch (e) { out.textContent += `错误: ${e.message}\n`; }
    out.scrollTop = out.scrollHeight;
  };
  $('cs-run').onclick = run;
  $('cs-cmd').onkeydown = (e) => { if (e.key === 'Enter') run(); };
}

// ---------- 应用管理 ----------

function bindApps() {
  $('am-reload').onclick = loadApps;
  $('am-search').oninput = renderApps;
  $('am-device').onchange = loadApps;
}

async function loadApps() {
  const serial = $('am-device').value;
  if (!serial) return;
  const tb = $('am-table').querySelector('tbody');
  tb.innerHTML = '<tr><td colspan="3" class="hint">加载中…（首次约 3~5 秒）</td></tr>';
  try {
    const r = await API.get(`/api/devices/${encodeURIComponent(serial)}/apps`);
    State.apps = r.apps;
    renderApps();
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="3" class="hint">加载失败: ${esc(e.message)}</td></tr>`;
  }
}

function renderApps() {
  const tb = $('am-table').querySelector('tbody');
  const q = ($('am-search').value || '').toLowerCase();
  const list = (State.apps || []).filter(a => a.toLowerCase().includes(q));
  if (!list.length) {
    tb.innerHTML = '<tr><td colspan="3" class="hint">无应用（先加载列表）</td></tr>';
    return;
  }
  tb.innerHTML = list.map((p, i) =>
    `<tr><td>${i + 1}</td><td class="dserial">${esc(p)}</td>
     <td style="white-space:nowrap">
       <button class="btn small" data-act="launch" data-pkg="${esc(p)}">启动</button>
       <button class="btn small" data-act="stop" data-pkg="${esc(p)}">停止</button>
       <button class="btn small danger" data-act="uninstall" data-pkg="${esc(p)}">卸载</button>
     </td></tr>`).join('');
  tb.querySelectorAll('button[data-act]').forEach(btn => {
    btn.onclick = async () => {
      const pkg = btn.dataset.pkg, act = btn.dataset.act;
      const serial = $('am-device').value;
      if (act === 'uninstall' && !confirm(`确认在 ${serial} 上卸载 ${pkg}？`)) return;
      try {
        const r = await API.post(`/api/devices/${encodeURIComponent(serial)}/apps/${encodeURIComponent(pkg)}/${act}`);
        toast(`[${pkg}] ${act} 成功${r.out ? ': ' + r.out : ''}`, 'ok');
      } catch (e) { toast(`${pkg} ${act} 失败: ${e.message}`, 'error', 6000); }
    };
  });
}

// ---------- 截图库 ----------

async function loadCaptures() {
  try {
    const files = await API.get('/api/captures');
    const grid = $('capture-grid');
    grid.innerHTML = files.map(f =>
      `<div><a href="${f.url}" target="_blank"><img loading="lazy" src="${f.url}"></a>
       <div class="cap">${esc(f.file)} (${(f.size / 1024).toFixed(0)}KB)</div></div>`).join('')
      || '<span class="hint">还没有截图</span>';
  } catch (e) { toast(e.message, 'error'); }
}

function bindCaptures() {
  $('cp-reload').onclick = loadCaptures;
}

// ---------- 投屏模态窗口 ----------

function openMirror(serial, opts = {}) {
  if (window.Analytics) {
    // 关键旅程起点：回放里可直接定位“打开投屏 → 出画面”这段耗时
    window.Analytics.capture('mirror_opened', {
      broadcast: !!opts.defaultBroadcast,
      selected: window.appSelectedDevices().length,
    });
  }
  $('mirror-modal').classList.remove('hidden');
  $('mm-title').textContent = `投屏 ${serial}`;
  $('mm-broadcast').checked = !!opts.defaultBroadcast;
  $('mm-inject-status').textContent = '就绪';
  $('mm-inject-status').className = 'hint';
  const canvas = $('mm-canvas');
  const recInd = $('mm-record-ind');
  const recorded = [];
  let videoRecorder = null;
  let videoChunks = [];

  const getTargets = () => {
    if (!$('mm-broadcast').checked) return [serial];
    const sel = window.appSelectedDevices();
    return [...new Set([serial, ...sel])];
  };
  const recording = () => $('mm-record').checked;

  const quality = $('mm-quality').value || localStorage.getItem(`narciFarm.quality.${serial}`) || 'medium';
  localStorage.setItem(`narciFarm.quality.${serial}`, quality);
  State.mirror = new Mirror(serial, canvas, {
    quality,
    getTargets,
    recording,
    onGesture: (step) => {
      recorded.push(step);
      recInd.textContent = `已录制 ${recorded.length} 步`;
      ScenarioEditor.steps.push(step);
      ScenarioEditor._syncRaw();
      ScenarioEditor.render();
    },
    onFallback: (reason) => {
      hideLoading();
      recInd.textContent = `快照模式(${reason.slice(0, 20)})`;
      const mb = $('mm-mode');
      if (mb) { mb.textContent = '快照模式（延迟高）'; mb.className = 'hint warn'; }
    },
    onReady: () => {
      hideLoading();
      const mb = $('mm-mode');
      if (mb) { mb.textContent = '实时'; mb.className = 'hint ok'; }
    },
    onInjectFeedback: (action, targets, results) => {
      const st = $('mm-inject-status');
      if (!st) return;
      const notes = Object.values((results && results.results) || {}).map(v => v && v.note).filter(Boolean);
      if (results && results.ok) {
        st.textContent = `✓ ${action} → ${targets.length} 台` + (notes.length ? '（已自动唤醒熄屏设备）' : '');
        st.className = notes.length ? 'hint warn' : 'hint ok';
      } else {
        st.textContent = `✗ ${action} 失败（详见日志页）`;
        st.className = 'hint bad';
      }
    },
  });
  State.mirror.connect();

  // ===== 弹窗加载遮罩（连接中可见：旋转指示 + 秒表 + 取消） =====
  const loadingEl = $('mm-loading');
  const loadingText = $('mm-loading-text');
  const openAt = performance.now();
  loadingEl.classList.remove('hidden');
  loadingText.textContent = '正在连接投屏… 0.0s';
  const elapsedTimer = setInterval(() => {
    loadingText.textContent = `正在连接投屏… 已等待 ${((performance.now() - openAt) / 1000).toFixed(1)}s`;
  }, 200);
  const hideLoading = () => { clearInterval(elapsedTimer); loadingEl.classList.add('hidden'); };
  $('mm-loading-cancel').onclick = () => {
    hideLoading();
    if (State.mirror) { State.mirror.close(); State.mirror = null; }
    $('mirror-modal').classList.add('hidden');
  };

  // ===== 弹窗移动/缩放（document 级 mouse 事件 + 专用手柄，全环境可靠） =====
  const mv = $('mirror-modal');
  const bar = mv.querySelector('.mirror-bar');
  const rz = $('mm-resize-handle');
  let winDrag = null;
  bar.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('button, input, select, label, a, textarea')) return;
    const r = mv.getBoundingClientRect();
    winDrag = { mode: 'move', dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  });
  rz.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const r = mv.getBoundingClientRect();
    winDrag = { mode: 'resize', sx: e.clientX, sy: e.clientY, sw: r.width, sh: r.height };
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!winDrag) return;
    if (winDrag.mode === 'move') {
      const x = Math.min(Math.max(e.clientX - winDrag.dx, 0), window.innerWidth - 60);
      const y = Math.min(Math.max(e.clientY - winDrag.dy, 0), window.innerHeight - 40);
      mv.style.left = x + 'px';
      mv.style.top = y + 'px';
      mv.style.transform = 'none';
    } else {
      const w = Math.min(Math.max(winDrag.sw + (e.clientX - winDrag.sx), 380), window.innerWidth * 0.96);
      const h = Math.min(Math.max(winDrag.sh + (e.clientY - winDrag.sy), 260), window.innerHeight * 0.92);
      mv.style.width = w + 'px';
      mv.style.height = h + 'px';
    }
  });
  document.addEventListener('mouseup', () => { winDrag = null; });

  // ===== 拖拽 APK 到投屏窗口直接安装 =====
  const dropTarget = $('mirror-modal');
  dropTarget.addEventListener('dragover', (e) => { e.preventDefault(); dropTarget.classList.add('drop-hint'); });
  dropTarget.addEventListener('dragleave', () => dropTarget.classList.remove('drop-hint'));
  dropTarget.addEventListener('drop', async (e) => {
    e.preventDefault();
    dropTarget.classList.remove('drop-hint');
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (!f.name.toLowerCase().endsWith('.apk')) return toast('仅支持拖入 APK 文件', 'error');
    const fd = new FormData();
    fd.append('apk', f);
    toast(`正在安装 ${f.name} …`, 'info');
    try {
      const r = await API.post(`/api/devices/${encodeURIComponent(serial)}/install`, fd);
      toast(`安装完成: ${(r.out || 'OK').trim()}`, 'ok', 6000);
    } catch (err) { toast(`安装失败: ${err.message}`, 'error', 6000); }
  });

  // ===== 其余工具条绑定（含关闭/加载遮罩收口） =====
  const closeMirror = () => {
    hideLoading();
    if (videoRecorder && videoRecorder.state !== 'inactive') videoRecorder.stop();
    if (State.mirror) { State.mirror.close(); State.mirror = null; }
    $('mirror-modal').classList.add('hidden');
  };
  $('mm-close').onclick = closeMirror;
  $('mm-quality').onchange = () => localStorage.setItem(`narciFarm.quality.${serial}`, $('mm-quality').value);
  $('mm-screen-on').onclick = () => State.mirror && State.mirror.send({ action: 'screen_power', on: true });
  $('mm-screen-off').onclick = () => State.mirror && State.mirror.send({ action: 'screen_power', on: false });

  // ===== 窗口控制: 最小化 / 最大化 =====
  const pill = $('mm-minimized-pill');
  $('mm-min').onclick = () => {
    pill.querySelector('#mm-pill-title').textContent = `投屏 ${serial}`;
    pill.classList.remove('hidden');
    $('mirror-modal').classList.add('hidden');
  };
  $('mm-pill-restore').onclick = () => {
    pill.classList.add('hidden');
    $('mirror-modal').classList.remove('hidden');
    mv.classList.remove('maximized');
  };
  $('mm-max').onclick = () => {
    mv.classList.toggle('maximized');
    $('mm-max').textContent = mv.classList.contains('maximized') ? '❐' : '▢';
  };
  // pill 帧率同步
  setInterval(() => {
    const pf = $('mm-pill-fps');
    if (pf && !pill.classList.contains('hidden') && State.mirror) {
      pf.textContent = State.mirror.fallback ? '快照模式' : `${State.mirror.fps} fps`;
    }
  }, 2000);
  document.querySelectorAll('.mirror-subbar [data-key]').forEach(btn => {
    btn.onclick = () => State.mirror && State.mirror.key(btn.dataset.key);
  });
  $('mm-send-text').onclick = async () => {
    const t = $('mm-text').value;
    if (!t || !State.mirror) return;
    if (/[^ -]/.test(t)) {
      if (!State.mirror.pasteText(t)) return toast('粘贴需要实时投屏模式', 'error');
      toast('中文文本已通过剪贴板粘贴（需焦点在手机输入框）', 'ok');
    } else {
      State.mirror.sendText(t);
      toast(`已发送到 ${getTargets().length} 台设备`, 'ok');
    }
  };
  $('mm-paste').onclick = async () => {
    if (!State.mirror) return;
    let text = $('mm-text').value;
    if (!text) { try { text = await navigator.clipboard.readText(); } catch (e) {} }
    if (!text) return toast('请先填写文本或复制内容到 PC 剪贴板', 'error');
    if (State.mirror.fallback) return toast('粘贴需要实时投屏模式（快照模式无控制通道）', 'error');
    if (State.mirror.pasteText(text)) toast('已写入手机剪贴板并执行粘贴（需焦点在输入框）', 'ok');
  };

  $('mm-clipboard').onclick = () => {
    const t = $('mm-text').value;
    if (t && State.mirror) State.mirror.setClipboard(t);
  };
  $('mm-shot').onclick = async () => {
    try {
      const r = await API.post(`/api/devices/${encodeURIComponent(serial)}/screenshot`);
      toast(`截图已保存: ${r.file}`, 'ok');
    } catch (e) { toast(e.message, 'error'); }
  };
  $('mm-record-video').onclick = () => {
    if (videoRecorder) { videoRecorder.stop(); return; }
    if (!canvas.captureStream) return toast('浏览器不支持录像', 'error');
    const stream = canvas.captureStream(30);
    const mime = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
      .find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m));
    if (!mime) return toast('浏览器不支持 MediaRecorder', 'error');
    videoChunks = [];
    videoRecorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 8000000 });
    videoRecorder.ondataavailable = (e) => { if (e.data.size) videoChunks.push(e.data); };
    videoRecorder.onstop = () => {
      const blob = new Blob(videoChunks, { type: 'video/webm' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `narci_${serial}_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
      a.click();
      videoRecorder = null;
      $('mm-record-video').textContent = '⏺ 录像';
      toast('录像已保存为 webm', 'ok');
    };
    videoRecorder.start(1000);
    $('mm-record-video').textContent = '⏹ 停止录像';
    toast('录像中…再次点击停止', 'info');
  };
}

function bindModal() {
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('mirror-modal').classList.contains('hidden')) {
      $('mm-close').click();
    }
  });
}


/* ---------- 主题(默认深色科技风, 可切浅色) ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('narciFarm.theme', t); } catch (e) {}
  const btn = $('btn-theme');
  if (btn) {
    btn.textContent = t === 'light' ? '☀️' : '🌙';
    btn.title = t === 'light' ? '切换为深色主题' : '切换为浅色主题';
  }
}

/* ---------- 自定义确认对话框(替代原生 confirm) ---------- */
function showConfirm(opts) {
  const { title = '确认操作', message = '', okText = '确定', cancelText = '取消', danger = false, icon = '⚠️' } = opts || {};
  return new Promise((resolve) => {
    const modal = $('confirm-modal');
    $('confirm-icon').textContent = icon;
    $('confirm-title').textContent = title;
    $('confirm-msg').textContent = message;
    $('confirm-ok').textContent = okText;
    $('confirm-cancel').textContent = cancelText;
    $('confirm-ok').className = 'btn ' + (danger ? 'danger-solid' : 'primary');
    modal.classList.remove('hidden');
    const done = (v) => { modal.classList.add('hidden'); resolve(v); };
    $('confirm-ok').onclick = () => done(true);
    $('confirm-cancel').onclick = () => done(false);
    modal.onclick = (e) => { if (e.target === modal) done(false); };
  });
}


/* ---------- 输入对话框 ---------- */
function showPrompt(opts) {
  const { title = '输入', message = '', defaultValue = '', okText = '确定' } = opts || {};
  return new Promise((resolve) => {
    const modal = $('prompt-modal');
    $('prompt-title').textContent = title;
    $('prompt-msg').textContent = message;
    $('prompt-ok').textContent = okText;
    const inp = $('prompt-input');
    inp.value = defaultValue || '';
    modal.classList.remove('hidden');
    inp.focus();
    const done = (v) => { modal.classList.add('hidden'); resolve(v); };
    $('prompt-ok').onclick = () => done(inp.value);
    $('prompt-cancel').onclick = () => done(null);
    inp.onkeydown = (e) => { if (e.key === 'Enter') done(inp.value); };
    modal.onclick = (e) => { if (e.target === modal) done(null); };
  });
}


// ---------- 开发工具 ----------

const PerfHistory = { cpu: [], mem: [], temp: [], fps: [] };
const PERF_MAX = 60;

function bindDevTools() {
  $('dt-device').onchange = () => refreshDevTools();
  $('dt-file-refresh').onclick = loadDeviceFiles;
  $('dt-file-mkdir').onclick = async () => {
    const serial = $('dt-device').value;
    const name = await showPrompt({ title: '新建文件夹', message: $('dt-path').value, defaultValue: 'NewFolder' });
    if (name) {
      await API.post(`/api/devices/${encodeURIComponent(serial)}/files/mkdir`, { path: $('dt-path').value.replace(/\/$/, '') + '/' + name });
      loadDeviceFiles();
    }
  };
  $('dt-file-upload').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const serial = $('dt-device').value;
    const fd = new FormData();
    fd.append('path', $('dt-path').value);
    fd.append('file', f);
    await API.post(`/api/devices/${encodeURIComponent(serial)}/files/upload`, fd);
    toast('上传完成', 'ok');
    loadDeviceFiles();
    e.target.value = '';
  };
  $('dt-log-refresh').onclick = loadLogcat;
  $('dt-log-live').onchange = (e) => { State._logcatTimer && clearInterval(State._logcatTimer); if (e.target.checked) State._logcatTimer = setInterval(loadLogcat, 3000); };
  $('dt-startup-cold').onclick = () => measureStartup(true);
  $('dt-startup-warm').onclick = () => measureStartup(false);
  document.querySelectorAll('#tab-devtools [data-cmd]').forEach(btn => {
    btn.onclick = () => {
      const serial = $('dt-device').value;
      const templates = {
        shell: `adb -s ${serial} shell`,
        install: `adb -s ${serial} install xxx.apk`,
        logcat: `adb -s ${serial} logcat`,
        forward: `adb -s ${serial} forward tcp:8080 tcp:8080`,
      };
      navigator.clipboard && navigator.clipboard.writeText(templates[btn.dataset.cmd]);
      toast(`已复制: ${templates[btn.dataset.cmd]}`, 'ok');
    };
  });
  setInterval(pollMetrics, 2000);
}

async function refreshDevTools() {
  const serial = $('dt-device').value;
  if (!serial) return;
  $('dt-serial').textContent = serial;
  $('dt-serial-hint').textContent = 'PC 端任意 adb 工具可直接使用';
  loadDeviceFiles();
  loadLogcat();
}

async function measureStartup(cold) {
  const serial = $('dt-device').value;
  const pkg = $('dt-package').value.trim();
  if (!pkg) return toast('请输入包名', 'error');
  $('dt-startup-result').textContent = '测量中…';
  try {
    const r = await API.post(`/api/devices/${encodeURIComponent(serial)}/startup_time`, { package: pkg, cold });
    $('dt-startup-result').textContent =
      `${cold ? '冷' : '热'}启动: ${r.wait_ms >= 0 ? r.wait_ms + 'ms' : '-'}${r.total_ms > 0 ? ' (Total ' + r.total_ms + 'ms)' : ''} @ ${r.component}`;
  } catch (e) { toast(e.message, 'error'); }
}

async function loadLogcat() {
  const serial = $('dt-device').value;
  if (!serial) return;
  try {
    const r = await API.get(`/api/devices/${encodeURIComponent(serial)}/logcat?lines=300&level=${$('dt-log-level').value}&filter=${encodeURIComponent($('dt-log-filter').value || '')}`);
    $('dt-logcat').textContent = r.lines.join(String.fromCharCode(10)) || '(无日志)';
  } catch (e) {}
}

async function loadDeviceFiles() {
  const serial = $('dt-device').value;
  if (!serial) return;
  const path = $('dt-path').value || '/sdcard';
  try {
    const r = await API.get(`/api/devices/${encodeURIComponent(serial)}/files?path=${encodeURIComponent(path)}`);
    $('dt-path').value = r.path.replace(/\/$/, '') || '/';
    const tb = $('dt-files').querySelector('tbody');
    tb.innerHTML = '';
    for (const e of r.entries) {
      const tr = document.createElement('tr');
      const size = e.dir ? '—' : e.size > 1048576 ? (e.size / 1048576).toFixed(1) + 'MB' : (e.size / 1024).toFixed(0) + 'KB';
      tr.innerHTML = `<td class="${e.dir ? '' : 'dserial'}">${e.dir ? '📁 ' : ''}${esc(e.name)}</td><td>${size}</td><td class="hint">${esc(e.mtime)}</td>
        <td style="white-space:nowrap">${e.dir ? '' : `<button class="btn small" data-dl="${esc(e.name)}">下载</button>`}<button class="btn small danger" data-del="${esc(e.name)}">删</button></td>`;
      tb.appendChild(tr);
    }
    tb.querySelectorAll('[data-dl]').forEach(b => b.onclick = () => {
      window.open(`/api/devices/${encodeURIComponent(serial)}/files/download?path=${encodeURIComponent($('dt-path').value.replace(/\/$/, '') + '/' + b.dataset.dl)}`);
    });
    tb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const ok = await showConfirm({ title: '删除', message: `确认删除 ${b.dataset.del}？`, okText: '删除', danger: true });
      if (!ok) return;
      await API.del(`/api/devices/${encodeURIComponent(serial)}/files?path=${encodeURIComponent($('dt-path').value.replace(/\/$/, '') + '/' + b.dataset.del)}`);
      loadDeviceFiles();
    });
  } catch (e) { toast(e.message, 'error'); }
}

async function pollMetrics() {
  const serial = $('dt-device').value;
  if (!serial || !$('tab-devtools').classList.contains('active')) return;
  try {
    const m = await API.get(`/api/devices/${encodeURIComponent(serial)}/metrics`);
    const cpu = m.cpu_total_pct ?? 0;
    const memUsed = m.mem_total_kb ? Math.round((1 - (m.mem_free_kb || 0) / m.mem_total_kb) * 100) : 0;
    const temp = m.battery && m.battery.temperature ? (parseInt(m.battery.temperature) / 10).toFixed(1) : '—';
    const fps = m.fps ?? 0;
    pushPerf('cpu', cpu); pushPerf('mem', memUsed);
    pushPerf('temp', parseFloat(temp) || 0); pushPerf('fps', fps);
    $('dt-metrics').innerHTML = [
      `CPU <b>${cpu}%</b>`, `内存 <b>${memUsed}%</b>`, `电池 <b>${m.battery ? m.battery.level : '—'}%</b>`,
      `温度 <b>${temp}°C</b>`, `帧率 <b>${fps}</b>`, `充电 <b>${m.battery && m.battery.status === '2' ? '是' : '否'}</b>`,
    ].map(x => `<span>${x}</span>`).join('');
    renderPerf();
  } catch (e) {}
}

function pushPerf(k, v) {
  const arr = PerfHistory[k];
  arr.push(v);
  if (arr.length > PERF_MAX) arr.shift();
}

function sparkline(values, color, w = 120, h = 28) {
  if (values.length < 2) return '';
  const max = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - (v / max) * h}`).join(' ');
  return `<svg width="${w}" height="${h}" style="background:var(--panel2);border-radius:4px"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}

function renderPerf() {
  const box = $('dt-perf');
  box.innerHTML = [
    ['CPU %', PerfHistory.cpu, 'var(--acc)'], ['内存 %', PerfHistory.mem, 'var(--warn)'],
    ['温度 °C', PerfHistory.temp, 'var(--bad)'], ['帧率', PerfHistory.fps, 'var(--ok)'],
  ].map(([label, vals, color]) => `<div class="perf-card"><div class="perf-label">${label}</div>${sparkline(vals, color)}</div>`).join('');
}

init();
