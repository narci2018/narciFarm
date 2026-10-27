/* 使用分析 + 会话回放（PostHog）：
 * - 以 ES module 形式 import posthog-js；index.html 的 importmap 把裸导入名 "posthog-js"
 *   指到本地 static/vendor/posthog-js/module.full.no-external.js（自带 session replay）
 * - 默认不采集：用户勾选「使用分析」后才会初始化 SDK 并录制
 * - 隐私默认值：所有输入框内容遮罩、日志/控制台/结果面板文本遮罩、投屏画布与截图整块屏蔽
 * - 尊重浏览器 DNT；选择存 localStorage（拒绝后不再产生任何上报请求）
 * - 项目 key 由服务端 /api/settings 下发（NARCI_POSTHOG_KEY），未配置时完全不初始化
 *
 * 关键旅程事件（打开投屏 / 连接耗时 / 降级 / 群控 / 场景运行）统一走 capture()，
 * 回放时间轴上即可看到这些事件，方便定位“点开投屏几分钟才出画面”这类问题。
 */
import posthog from 'posthog-js';

const Analytics = {
  CONSENT_KEY: 'narciFarm.analytics.consent', // 'granted' | 'denied'
  key: '',
  host: 'https://us.i.posthog.com',
  client: null,
  enabled: false,
  toggle: null,

  consent() {
    try { return localStorage.getItem(this.CONSENT_KEY); } catch (e) { return null; }
  },

  _store(value) {
    try { localStorage.setItem(this.CONSENT_KEY, value); } catch (e) { /* 隐私模式下不可写，按会话内存态处理 */ }
  },

  _dnt() {
    return navigator.doNotTrack === '1' || window.doNotTrack === '1' || navigator.msDoNotTrack === '1';
  },

  /* app.js 拿到 /api/settings 后调用 */
  init(settings) {
    const s = settings || {};
    this.key = String(s.posthog_key || '').trim();
    if (s.posthog_host) this.host = String(s.posthog_host);
    this.toggle = document.getElementById('an-consent');
    const wrap = document.getElementById('an-consent-wrap');

    if (!this.key || this._dnt()) {
      // 未配置项目 key 或用户声明了 DNT：不初始化 SDK，也不显示开关
      if (wrap) wrap.classList.add('hidden');
      return;
    }
    if (wrap) wrap.classList.remove('hidden');

    const granted = this.consent() === 'granted';
    if (this.toggle) {
      this.toggle.checked = granted;
      this.toggle.onchange = () => this.setConsent(this.toggle.checked);
    }
    if (granted) this._start();
  },

  setConsent(granted) {
    if (this.toggle) this.toggle.checked = !!granted;
    if (!granted) {
      this._store('denied');
      this.enabled = false;
      if (this.client) {
        // 撤销同意：停止录制并彻底退出采集
        try { this.client.stopSessionRecording(); } catch (e) { /* ignore */ }
        try { this.client.opt_out_capturing(); } catch (e) { /* ignore */ }
      }
      return;
    }
    this._store('granted');
    this._start();
    this.capture('analytics_consent_granted');
  },

  _start() {
    if (!this.key) return;
    if (this.client) {
      // 之前拒绝过：清除 opt-out 后恢复录制
      try { this.client.opt_in_capturing(); } catch (e) { /* ignore */ }
      try { this.client.startSessionRecording(); } catch (e) { /* ignore */ }
      this.enabled = true;
      return;
    }
    this.client = posthog.init(this.key, {
      api_host: this.host,
      person_profiles: 'identified_only',
      respect_dnt: true,
      capture_pageview: true,
      autocapture: true,
      session_recording: {
        maskAllInputs: true,
        maskTextSelector: '.console-out, #log-view, .result-box, .dserial, .topstats, .device-grid .name',
        blockSelector: '.mirror-stage, #capture-grid',
        recordCanvas: false,
      },
    });
    this.enabled = true;
  },

  /* 事件上报：未同意 / 未初始化时是空操作，绝不干扰业务 */
  capture(event, props) {
    if (!this.enabled || !this.client) return;
    try { this.client.capture(event, props || {}); } catch (e) { /* ignore */ }
  },
};

// 其余脚本（api/mirror/scenario/app.js）是经典脚本，统一经全局访问
globalThis.Analytics = Analytics;
document.dispatchEvent(new Event('narci:analytics-ready'));
