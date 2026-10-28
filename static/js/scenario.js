/* 场景编辑器：步骤表格 + 参数表单 + JSON 导入导出 */

const STEP_FIELDS = {
  tap: [['x_pct', 'x (0~1)'], ['y_pct', 'y (0~1)']],
  swipe: [['x_pct', '起点x'], ['y_pct', '起点y'], ['x2_pct', '终点x'], ['y2_pct', '终点y'], ['duration_ms', '时长ms']],
  text: [['text', '文本']],
  key: [['keycode', '按键(back/home/menu/power/volume_up/volume_down/recents/enter/数字)']],
  back: [], home: [],
  launch: [['package', '包名']],
  stop_app: [['package', '包名']],
  wait: [['ms', '毫秒']],
  shell: [['cmd', '命令'], ['timeout', '超时s']],
  screenshot: [['name', '文件名(可选)']],
  tap_text: [['text', '要点击的文本']],
  wait_text: [['text', '等待出现的文本'], ['timeout_ms', '超时ms'], ['interval_ms', '轮询间隔ms']],
  assert_text: [['text', '文本'], ['expect', 'exists(须存在) / absent(须不存在)'],
                 ['match', '匹配模式 exact/contains/regex/startswith/endswith']],
  reboot: [['wait', '重启后等待上线 1/0']],
  locate: [],
  airplane: [['on', '开飞行模式 1/0']],
  lock: [], unlock: [],
  assert_value: [['actual', '实际值(支持 {{变量}})'], ['expected', '期望值'],
                 ['op', '比较符 eq/ne/gt/gte/lt/lte/contains/regex']],
  rand_wait: [['min_ms', '最小ms'], ['max_ms', '最大ms']],
  start_activity: [['component', 'com.pkg/.MainActivity']],
  tap_image: [['image', '模板文件名(如 btn.png)'], ['threshold', '阈值0~1']],
  wait_image: [['image', '模板文件名'], ['timeout_ms', '超时ms'], ['interval_ms', '轮询ms'], ['threshold', '阈值0~1']],
  assert_image: [['image', '模板文件名'], ['expect', 'exists / absent'], ['threshold', '阈值0~1']],
  ocr_tap: [['text', '要找的文字'], ['region', '限定区域 x1,y1,x2,y2(可选)']],
  ocr_get: [['text', '要找的文字(留空取全部)']],
  ocr_find: [['text', '过滤文字(留空返回全部)'], ['region', '限定区域 x1,y1,x2,y2(可选)']],
  find_image: [['image', '模板文件名'], ['threshold', '阈值0~1'], ['max_results', '最多结果数'],
               ['region', '限定区域 x1,y1,x2,y2(可选)'], ['scales', '缩放列表 如 1,0.8,0.6(可选)']],
  tap_el: [['kind', '定位维度 text/id/desc/class/package/xpath'], ['value', '匹配值'],
           ['contains', '模糊匹配 1/0'], ['index', '第几个(0起)']],
  wait_el: [['kind', '定位维度'], ['value', '匹配值'], ['contains', '模糊匹配 1/0'],
            ['index', '第几个(0起)'], ['timeout_ms', '超时ms'], ['interval_ms', '轮询ms']],
  assert_el: [['kind', '定位维度'], ['value', '匹配值'], ['contains', '模糊匹配 1/0'],
              ['index', '第几个(0起)'], ['expect', 'exists / absent'],
              ['match', '匹配模式 exact/contains/regex/startswith/endswith']],
  swipe_el: [['kind', '定位维度'], ['value', '匹配值'], ['contains', '模糊匹配 1/0'],
             ['index', '第几个(0起)'], ['x2', '终点x'], ['y2', '终点y'], ['duration_ms', '时长ms']],
  loop: [['count', '次数']],
};

const SELECTOR_KINDS = ['text', 'id', 'desc', 'class', 'package', 'xpath'];
const MATCH_MODES = ['exact', 'contains', 'regex', 'startswith', 'endswith'];
const COMPARE_OPS = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'regex'];
const SELECTOR_STEPS = ['tap_el', 'wait_el', 'assert_el', 'swipe_el'];

const ScenarioEditor = {
  scenarioId: null,
  steps: [],

  loadList() { return API.get('/api/scenarios'); },

  open(sc) {
    this.scenarioId = sc ? sc.id : null;
    document.getElementById('sc-editor-title').textContent = sc ? `编辑场景 #${sc.id}` : '新建场景';
    document.getElementById('sc-name').value = sc ? sc.name : '';
    document.getElementById('sc-repeat').value = sc ? (sc.repeat_count || 1) : 1;
    this.steps = sc ? JSON.parse(sc.steps || '[]') : [];
    this._syncRaw();
    this.render();
  },

  _syncRaw() {
    document.getElementById('sc-editor-raw').value = JSON.stringify(this.steps, null, 2);
  },

  render() {
    const tbody = document.getElementById('sc-steps');
    tbody.innerHTML = '';
    this.steps.forEach((step, idx) => {
      const tr = document.createElement('tr');
      const summary = this._summarize(step);
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="step-badge">${esc(step.type)}</span></td>
        <td class="hint">${esc(summary)}</td>
        <td style="white-space:nowrap">
          <button class="btn small" data-act="up" data-i="${idx}">↑</button>
          <button class="btn small" data-act="down" data-i="${idx}">↓</button>
          <button class="btn small" data-act="edit" data-i="${idx}">编辑</button>
          <button class="btn small danger" data-act="del" data-i="${idx}">删</button>
        </td>`;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll('button[data-act]').forEach(btn => {
      btn.onclick = () => this._rowAction(btn.dataset.act, +btn.dataset.i);
    });
  },

  _summarize(step) {
    if (step.type === 'loop') return `循环 ${step.count} 次, ${step.steps.length} 个子步骤`;
    if (step.type === 'assert_text') return `${step.expect === 'absent' ? '须不存在' : '须存在'}: ${step.text}`;
    if (SELECTOR_STEPS.includes(step.type)) {
      const fuzzy = step.contains ? '~' : '=';
      const idx = step.index ? `[${step.index}]` : '';
      return `${step.kind || 'text'}${fuzzy}${step.value || ''}${idx}`;
    }
    if (step.type === 'ocr_tap') return `点击 OCR 文字: ${step.text}`;
    if (step.type === 'find_image') return `图像匹配 ${step.image} (阈值 ${step.threshold}, 最多 ${step.max_results || 1})`;
    if (step.type === 'assert_image') return `${step.expect === 'absent' ? '须不存在' : '须存在'}: ${step.image}`;
    return Object.entries(step).filter(([k]) => k !== 'type')
      .map(([k, v]) => `${k}=${v}`).join(', ');
  },

  _rowAction(act, idx) {
    if (act === 'del') { this.steps.splice(idx, 1); }
    else if (act === 'up' && idx > 0) { [this.steps[idx - 1], this.steps[idx]] = [this.steps[idx], this.steps[idx - 1]]; }
    else if (act === 'down' && idx < this.steps.length - 1) { [this.steps[idx + 1], this.steps[idx]] = [this.steps[idx], this.steps[idx + 1]]; }
    else if (act === 'edit') { this._editStep(idx); return; }
    this._syncRaw();
    this.render();
  },

  _editStep(idx) {
    const step = this.steps[idx];
    const fields = STEP_FIELDS[step.type] || [];
    // 所有步骤都支持失败重试策略
    const retryFields = [
      ['retry', '失败重试次数'],
      ['retry_interval_ms', '重试间隔ms'],
      ['on_fail', '最终失败时 abort / continue'],
    ];
    const allFields = (fields || []).concat(retryFields);
    const form = allFields.map(([key, label]) => {
      const val = step[key] ?? '';
      const isBool = key === 'contains';
      const select = (name, options) =>
        `<select data-k="${name}">${options.map(k =>
          `<option value="${k}"${k === val ? ' selected' : ''}>${k}</option>`).join('')}</select>`;
      let input;
      if (key === 'kind' && SELECTOR_KINDS.includes(val)) input = select('kind', SELECTOR_KINDS);
      else if (key === 'match') input = select('match', MATCH_MODES);
      else if (key === 'op') input = select('op', COMPARE_OPS);
      else input = `<input data-k="${esc(key)}" value="${esc(isBool ? (val ? 1 : 0) : val)}">`;
      return `<label style="min-width:auto">${esc(label)}</label>${input}`;
    }).join('');
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="4"><div class="form-row">${form}
      <button class="btn small primary" data-ok>确定</button>
      <button class="btn small" data-cancel>取消</button></div></td>`;
    const ref = document.querySelectorAll('#sc-steps tr')[idx];
    ref.after(tr);
    tr.querySelector('[data-ok]').onclick = () => {
      tr.querySelectorAll('[data-k]').forEach(inp => {
        const k = inp.dataset.k;
        let v = inp.value;
        if (['x_pct', 'y_pct', 'x2_pct', 'y2_pct', 'threshold'].includes(k)) v = parseFloat(v) || 0;
        else if (['duration_ms', 'ms', 'timeout', 'count', 'timeout_ms', 'interval_ms',
                   'max_results', 'index', 'x2', 'y2', 'retry', 'retry_interval_ms'].includes(k)) v = parseInt(v) || 0;
        else if (k === 'contains') v = (v === '1' || v === 'true' || v === true);
        else if (k === 'region' || k === 'scales') {
          v = v.split(',').map(x => parseFloat(x.trim())).filter(x => !isNaN(x));
          if (k === 'region' && v.length !== 4) v = [];
          if (!v.length) v = undefined;
        }
        step[k] = v;
      });
      tr.remove();
      this._syncRaw(); this.render();
    };
    tr.querySelector('[data-cancel]').onclick = () => tr.remove();
  },

  addStep(type) {
    const step = { type };
    if (type === 'loop') step.count = 2, step.steps = [{ type: 'wait', ms: 1000 }];
    if (type === 'tap') step.x_pct = 0.5, step.y_pct = 0.5;
    if (type === 'wait') step.ms = 1000;
    if (type === 'swipe') { step.x_pct = 0.5; step.y_pct = 0.7; step.x2_pct = 0.5; step.y2_pct = 0.3; step.duration_ms = 300; }
    if (SELECTOR_STEPS.includes(type)) {
      step.kind = 'text'; step.value = ''; step.contains = false; step.index = 0;
      if (type === 'wait_el') { step.timeout_ms = 10000; step.interval_ms = 1000; }
      if (type === 'assert_el') step.expect = 'exists';
      if (type === 'swipe_el') step.duration_ms = 300;
    }
    if (type === 'assert_value') { step.actual = ''; step.expected = ''; step.op = 'eq'; }
    if (type === 'airplane') step.on = true;
    if (type === 'reboot') step.wait = true;
    if (type === 'ocr_tap') step.text = '';
    if (type === 'ocr_find') step.text = '';
    if (type === 'find_image') { step.image = ''; step.threshold = 0.8; step.max_results = 1; }
    this.steps.push(step);
    this._syncRaw(); this.render();
    this._editStep(this.steps.length - 1);
  },

  applyRaw() {
    try {
      const arr = JSON.parse(document.getElementById('sc-editor-raw').value);
      if (!Array.isArray(arr)) throw new Error('必须是数组');
      this.steps = arr;
      this.render();
      toast('已应用 JSON', 'ok');
    } catch (e) { toast(`JSON 解析失败: ${e.message}`, 'error'); }
  },

  async save() {
    const name = document.getElementById('sc-name').value.trim();
    if (!name) { toast('请填写场景名', 'error'); return; }
    const repeat = Math.max(1, parseInt(document.getElementById('sc-repeat').value) || 1);
    if (this.scenarioId) {
      await API.put(`/api/scenarios/${this.scenarioId}`, { name, steps: this.steps, repeat_count: repeat });
      toast('场景已更新', 'ok');
    } else {
      const sc = await API.post('/api/scenarios', { name, steps: this.steps, repeat_count: repeat });
      this.scenarioId = sc.id;
      toast('场景已创建', 'ok');
    }
    if (window.appRefreshScenarios) window.appRefreshScenarios();
  },

  async run() {
    if (window.appSelectedDevices().length === 0) { toast('请先在左侧勾选目标设备', 'error'); return; }
    if (!this.scenarioId) { toast('请先保存场景', 'error'); return; }
    try {
      const collect = document.getElementById('sc-collect-metrics');
      const interval = document.getElementById('sc-metrics-interval');
      const r = await API.post(`/api/scenarios/${this.scenarioId}/run`, {
        targets: window.appSelectedDevices(),
        collect_metrics: !!(collect && collect.checked),
        metrics_interval: interval ? parseFloat(interval.value) || 2 : 2,
      });
      if (window.Analytics) {
        window.Analytics.capture('scenario_run_started', { targets: r.targets.length });
      }
      toast(`场景已启动 run=${r.run_id.slice(0, 8)} 设备=${r.targets.length}`, 'ok');
    } catch (e) { toast(e.message, 'error'); }
  },
};
