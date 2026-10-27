/* narciFarm 主控制器：设备/分组/总览/广播/批量/场景/调度/控制台/截图/日志 */

const State = {
  devices: [],
  groups: [],
  scenarios: [],
  schedules: [],
  apps: [],
  selected: new Set(),
  settings: {},
  currentScenario: null,
  mirror: null,
  lastShotAt: {},
};

const $ = (id) => document.getElementById(id);

window.appSelectedDevices = () => State.devices.filter(d => State.selected.has(d.serial)).map(d => d.serial);

// ---------- 初始化 ----------

async function init() {
  try { State.settings = await API.get('/api/settings'); } catch (e) {}
  $('st-adb').classList.toggle('on', !!State.settings.adb_ok);
  $('st-jar').classList.toggle('on', !!State.settings.server_jar_ok);
  if (!State.settings.adb_ok) toast('未找到 adb，请设置 NARCI_ADB 环境变量', 'error');
  if (!State.settings.server_jar_ok) toast('缺少 scrcpy-server.jar，运行 scripts/fetch_scrcpy_server.py', 'error', 8000);

  bindTabs();
  bindSidebar();
  bindOverview();
  bindBroadcast();
  bindBatch();
  bindScenario();
  bindSchedule();
  bindConsole();
  bindApps();
  bindCaptures();
  bindModal();
  connectEventStream();

  await Promise.all([refreshDevices(), refreshGroups(), refreshScenarios(), refreshSchedules()]);
  setInterval(() => refreshDevices(true), 3000);
  setInterval(refreshRuns, 5000);
  setInterval(snapshotTicker, 1000);
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
      <span class="dserial" title="${esc(d.serial)}">${esc(d.serial)}</span>
      <span class="dmodel">${esc(d.model || d.android_version || '')}</span>`;
    li.querySelector('input').onchange = (e) => {
      if (e.target.checked) State.selected.add(d.serial); else State.selected.delete(d.serial);
    };
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
    if (!confirm('确定关闭 narciFarm 服务？投屏会话与后台任务将一并停止。')) return;
    try {
      await API.post('/api/shutdown');
      document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;' +
        'color:#7d8b9d;font-size:16px;flex-direction:column;gap:10px">' +
        '<div>服务已关闭，此页面不再可用。</div><div style="font-size:13px">如需再次使用，请重新运行 narciFarm.exe。</div></div>';
    } catch (e) {
      toast(e.message.replace(/^\d+ /, ''), 'error', 6000);
    }
  };
}

function fillDeviceSelects() {
  const online = State.devices.filter(d => d.state === 'device');
  for (const [selId] of [['bc-master'], ['cs-device'], ['am-device']]) {
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

  State.mirror = new Mirror(serial, canvas, {
    getTargets,
    recording,
    onGesture: (step) => {
      recorded.push(step);
      recInd.textContent = `已录制 ${recorded.length} 步`;
      ScenarioEditor.steps.push(step);
      ScenarioEditor._syncRaw();
      ScenarioEditor.render();
    },
    onFallback: (reason) => { recInd.textContent = `快照模式(${reason.slice(0, 20)})`; },
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

  const statsTimer = setInterval(() => {
    if (State.mirror && !State.mirror.fallback) {
      State.mirror.requestStats();
      $('mm-fps').textContent = `${State.mirror.fps} fps · ${State.mirror.width}x${State.mirror.height}`;
    } else if (State.mirror && State.mirror.fallback) {
      $('mm-fps').textContent = '快照轮询模式';
    }
  }, 2000);

  $('mm-close').onclick = closeMirror;
  $('mirror-modal').onclick = (e) => { if (e.target === $('mirror-modal')) closeMirror(); };
  function closeMirror() {
    clearInterval(statsTimer);
    if (videoRecorder && videoRecorder.state !== 'inactive') videoRecorder.stop();
    if (State.mirror) { State.mirror.close(); State.mirror = null; }
    $('mirror-modal').classList.add('hidden');
    recInd.textContent = '';
  }
  // 快捷键按钮
  document.querySelectorAll('.mirror-subbar [data-key]').forEach(btn => {
    btn.onclick = () => State.mirror && State.mirror.key(btn.dataset.key);
  });
  $('mm-send-text').onclick = async () => {
    const t = $('mm-text').value;
    if (!t || !State.mirror) return;
    if (/[^\x00-\x7F]/.test(t)) {
      // 含非 ASCII（中文等）: input text 不支持, 自动走剪贴板粘贴路径
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
    if (!text) {
      try { text = await navigator.clipboard.readText(); } catch (e) {}
    }
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

init();
