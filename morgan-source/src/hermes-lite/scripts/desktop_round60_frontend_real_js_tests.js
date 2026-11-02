/**
 * Round 60: Real frontend chat.js verification suite.
 *
 * Runs the ACTUAL production `chat.js` in a Node VM to verify:
 *   F01: 补查成功事件流：message_reset 彻底清空气泡，二次生成无前缀拼接
 *   F02: 失败回退事件流：message_reset 后写入完整诚实提示与完整候选
 *   F03: 用户停止事件流：stop_ack 正确收敛状态机与提示
 *   F04: 请求身份栅栏：stale request_id / foreign session_id 事件被完全拒绝
 *
 * Run from src/hermes-lite with HERMES_AUDIT_ROUND set:
 *   node scripts\desktop_round59_frontend_real_js_tests.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CHAT_JS = process.env.CHAT_JS_OVERRIDE || path.resolve(__dirname, '../hermes_lite/chat_static/chat.js');
const AUDIT_ROUND = process.env.HERMES_AUDIT_ROUND;
if (!AUDIT_ROUND) {
  console.error('HERMES_AUDIT_ROUND must be explicit.');
  process.exit(2);
}
const AUDIT_DIR = path.resolve(__dirname, '../../../audit', AUDIT_ROUND);
const LOGS = path.join(AUDIT_DIR, 'logs');
fs.mkdirSync(LOGS, { recursive: true });
const OUT = path.join(LOGS, 'desktop_round60_frontend_real_js_tests.json');
if (fs.existsSync(OUT)) throw new Error('Refusing to overwrite existing evidence: ' + OUT);

function makeEl(id, tag) {
  const listeners = {};
  let _text = '';
  let _className = '';
  const kids = [];
  const el = {
    id,
    tagName: (tag || 'div').toUpperCase(),
    value: '', checked: false, hidden: false, disabled: false,
    style: {}, dataset: {}, children: kids,
    get className() { return _className; },
    set className(v) {
      _className = String(v || '');
      if (this.classList && this.classList._s) {
        this.classList._s = new Set(_className.split(/\s+/).filter(Boolean));
      }
    },
    get textContent() { return _text; },
    set textContent(v) { _text = String(v == null ? '' : v); },
    get innerHTML() { return _text; },
    set innerHTML(v) {
      _text = String(v == null ? '' : v);
      kids.length = 0;
    },
    classList: {
      _s: new Set(),
      add(c) {
        (_className || '').split(/\s+/).filter(Boolean).forEach((x) => this._s.add(x));
        this._s.add(c);
        _className = [...this._s].join(' ');
      },
      remove(c) {
        (_className || '').split(/\s+/).filter(Boolean).forEach((x) => this._s.add(x));
        this._s.delete(c);
        _className = [...this._s].join(' ');
      },
      contains(c) {
        return (_className || '').split(/\s+/).includes(c);
      },
      toggle(c) { this.contains(c) ? this.remove(c) : this.add(c); }
    },
    addEventListener(e, fn) { (listeners[e] = listeners[e] || []).push(fn); },
    removeEventListener(e, fn) {
      if (listeners[e]) {
        const idx = listeners[e].indexOf(fn);
        if (idx >= 0) listeners[e].splice(idx, 1);
      }
    },
    dispatchEvent(ev) {
      (listeners[ev.type] || []).forEach((f) => f(ev));
      if (typeof el['on' + ev.type] === 'function') el['on' + ev.type](ev);
    },
    appendChild(c) { kids.push(c); c.parentNode = el; return c; },
    removeChild(c) {
      const i = kids.indexOf(c);
      if (i >= 0) { kids.splice(i, 1); c.parentNode = null; }
      return c;
    },
    remove() {
      if (el.parentNode && el.parentNode.removeChild) el.parentNode.removeChild(el);
    },
    setAttribute(k, v) { el[k] = v; },
    getAttribute(k) { return el[k] || null; },
    removeAttribute(k) { delete el[k]; },
    querySelector(sel) {
      if (sel.startsWith('[data-mid="') && sel.endsWith('"]')) {
        const mid = sel.slice(11, -2);
        return findByMid(el, mid);
      }
      const cls = String(sel).replace(/^\./, '').trim();
      const hit = findByClass(el, cls);
      if (hit === el) {
        for (const c of (el.children || [])) {
          const chHit = findByClass(c, cls);
          if (chHit) return chHit;
        }
        return null;
      }
      return hit;
    },
    querySelectorAll(sel) {
      const cls = String(sel).replace(/^\./, '').trim();
      const out = [];
      (function walk(n) {
        if (!n) return;
        if ((n.className || '').split(/\s+/).includes(cls)) out.push(n);
        (n.children || []).forEach(walk);
      })(el);
      return out;
    },
    focus() {}, blur() {}, scrollIntoView() {}, scrollTo() {},
    insertAdjacentHTML() {}, setSelectionRange() {}, select() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
  };
  return el;
}

function findByMid(node, mid) {
  if (!node) return null;
  if (node.dataset && node.dataset.mid === mid) return node;
  if (node.getAttribute && node.getAttribute('data-mid') === mid) return node;
  for (const c of (node.children || [])) {
    const hit = findByMid(c, mid);
    if (hit) return hit;
  }
  return null;
}

function findByClass(node, cls) {
  if (!node) return null;
  if ((node.className || '').split(/\s+/).includes(cls)) return node;
  for (const c of (node.children || [])) {
    const hit = findByClass(c, cls);
    if (hit) return hit;
  }
  return null;
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
  }
  addEventListener(kind, fn) {
    (this.listeners[kind] = this.listeners[kind] || []).push(fn);
  }
  removeEventListener(kind, fn) {
    if (this.listeners[kind]) {
      const idx = this.listeners[kind].indexOf(fn);
      if (idx >= 0) this.listeners[kind].splice(idx, 1);
    }
  }
  dispatch(kind, data) {
    const ev = { type: kind, data: typeof data === 'string' ? data : JSON.stringify(data) };
    (this.listeners[kind] || []).forEach((fn) => fn(ev));
  }
  close() {}
}

function bootEnvironment() {
  const elements = {};
  const ids = [
    'input', 'messages', 'session-list', 'session-title', 'session-meta',
    'banner', 'notice', 'ctx-indicator', 'btn-send', 'btn-stop', 'toast',
    'modal-backdrop', 'settings-modal', 'memory-drawer', 'sidebar',
    'btn-sidebar-expand', 'btn-sidebar-collapse', 'more-menu', 'dropdown-more-wrap',
    'settings-close', 'gen-state', 'persona-body', 'persona-enabled', 'persona-count',
    'persona-dirty', 'persona-feedback', 'persona-path', 'btn-persona-save',
    'btn-persona-reload', 'quick-web-search', 'setting-web-search'
  ];
  for (const id of ids) elements[id] = makeEl(id, 'div');

  let esInstance = null;
  class MonitoredEventSource extends FakeEventSource {
    constructor(url) {
      super(url);
      esInstance = this;
    }
  }

  const context = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Date, Math, JSON, parseInt, parseFloat, Array, Object, String, Boolean, Number,
    Error, Promise, RegExp, isNaN, encodeURIComponent, decodeURIComponent,
    crypto: globalThis.crypto,
    __HERMES_TEST_NO_AUTO_BOOT__: true,
    addEventListener() {}, removeEventListener() {},
    innerWidth: 1240, innerHeight: 900, window: null,
    document: {
      getElementById(id) { if (!elements[id]) elements[id] = makeEl(id); return elements[id]; },
      createElement(tag) { return makeEl('anon', tag); },
      createTextNode(t) { const n = makeEl('text'); n.textContent = t; return n; },
      addEventListener() {}, removeEventListener() {},
      querySelectorAll() { return []; }, querySelector() { return null; },
      body: makeEl('body'), documentElement: makeEl('html')
    },
    navigator: { clipboard: { writeText: async () => {} } },
    localStorage: { _d: {}, getItem(k) { return this._d[k] || null; },
      setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    toast() {}, fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
    EventSource: MonitoredEventSource,
    location: { href: 'http://127.0.0.1:8771/' },
    requestAnimationFrame: (f) => setTimeout(f, 0),
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} })
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(CHAT_JS, 'utf8'), context);

  return {
    context,
    elements,
    getEs: () => esInstance,
    run: (code) => vm.runInContext(code, context)
  };
}

const results = [];
function check(id, title, ok, detail) {
  results.push({ id, title, pass: !!ok, detail: detail === undefined ? '' : detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${id}  ${title}`
    + (ok ? '' : `  [${JSON.stringify(detail)}]`));
}

function runFrontendTests() {
  console.log('=== Running Round 60 Frontend Tests (Production chat.js in Node VM) ===');

  // =========================================================================
  // F01: 补查成功事件流：message_reset 彻底清空气泡，二次生成无前缀拼接
  // =========================================================================
  {
    const env = bootEnvironment();
    const sid = 'sess-f01';
    const mid = 'msg-f01-asst';
    const reqId = 'req-f01';

    // Set state
    env.run(`
      state.sessionId = "${sid}";
      state.activeRequestBySession["${sid}"] = "${reqId}";
      connectStream();
    `);

    const es = env.getEs();
    if (!es) throw new Error('EventSource was not created by openStream');

    // Simulate candidate chunk streaming
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '我并不' });
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '了解影之刃零' });

    // Verify candidate was buffered/streamed in DOM bubble
    const bubbleInitial = env.elements.messages.querySelector(`[data-mid="${mid}"]`).querySelector('.bubble');
    const initialText = bubbleInitial.dataset.raw || bubbleInitial.textContent;
    const initialHasPrefix = initialText === '我并不了解影之刃零';

    // Backend detects gap, sends search_phase and message_reset
    es.dispatch('search_phase', {
      session_id: sid, request_id: reqId, phase: 'gap_fill',
      message: '这条回答显示缺少该对象资料，正在补查资料…', message_id: mid
    });
    es.dispatch('message_reset', { session_id: sid, request_id: reqId, message_id: mid });

    const bubbleReset = env.elements.messages.querySelector(`[data-mid="${mid}"]`).querySelector('.bubble');
    const resetClean = bubbleReset.dataset.raw === '' && bubbleReset.textContent === '';

    // Second generation arrives
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '据资料，' });
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '影之刃零由灵游坊开发。' });

    const finalText = bubbleReset.dataset.raw || bubbleReset.textContent;
    const noSplicing = finalText === '据资料，影之刃零由灵游坊开发。' && !finalText.includes('我并不了解');

    check('F01_1', '候选流式产生文本后，message_reset 干净重置气泡',
      initialHasPrefix && resetClean, { initialText, resetRaw: bubbleReset.dataset.raw, resetContent: bubbleReset.textContent });

    check('F01_2', '补查二次生成流式写入后，气泡绝无首轮候选前缀拼接',
      noSplicing, { finalText });
  }

  // =========================================================================
  // F02: 失败回退事件流：message_reset 后写入完整诚实提示与完整候选
  // =========================================================================
  {
    const env = bootEnvironment();
    const sid = 'sess-f02';
    const mid = 'msg-f02-asst';
    const reqId = 'req-f02';

    env.run(`
      state.sessionId = "${sid}";
      state.activeRequestBySession["${sid}"] = "${reqId}";
      connectStream();
    `);
    const es = env.getEs();

    // Candidate stream
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '我并不' });
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: '了解影之刃零' });

    // Reset on gap fill
    es.dispatch('message_reset', { session_id: sid, request_id: reqId, message_id: mid });

    // Fallback text emitted on search timeout
    const fallbackText = '（补查检索超时，未取得可用资料）\n\n我并不了解影之刃零';
    es.dispatch('delta', { session_id: sid, request_id: reqId, message_id: mid, text: fallbackText });

    const bubble = env.elements.messages.querySelector(`[data-mid="${mid}"]`).querySelector('.bubble');
    const rendered = bubble.dataset.raw || bubble.textContent;

    check('F02', '补查失败回退流式写入完整诚实说明与完整候选回答，非残缺前缀',
      rendered === fallbackText, { rendered });
  }

  // =========================================================================
  // F03: 用户停止事件流：stop_ack 正确收敛状态机与提示
  // =========================================================================
  {
    const env = bootEnvironment();
    const sid = 'sess-f03';

    env.run(`
      state.sessionId = "${sid}";
      drawBusy({ session_id: "${sid}" });
    `);

    const busyBefore = env.run('state.busy');
    env.run('connectStream();');
    const es = env.getEs();
    // Exercise only the listener installed by the unmodified production script.
    es.dispatch('stop_ack', { session_id: sid, request_id: 'req-f03' });

    const genState = env.elements['gen-state'].textContent;
    const noticeText = env.elements.notice.textContent;
    const busyAfter = env.run('state.busy');

    check('F03', '收到 stop_ack 事件后状态恢复为「空闲」，提示「已停止生成/搜索。」，busy 解除',
      busyBefore === true && busyAfter === false && genState === '空闲' && noticeText.includes('已停止生成/搜索。'),
      { busyBefore, busyAfter, genState, noticeText });
  }

  // =========================================================================
  // F04: 请求身份栅栏：stale request_id / foreign session_id 事件被完全拒绝
  // =========================================================================
  {
    const env = bootEnvironment();
    const sid = 'sess-f04';
    const mid = 'msg-f04';
    const currentReq = 'req-CURRENT';
    const staleReq = 'req-OLD';

    env.run(`
      state.sessionId = "${sid}";
      state.activeRequestBySession["${sid}"] = "${currentReq}";
      state.epoch = 10;
      connectStream();
    `);
    const es = env.getEs();

    // 1. Stale delta from foreign session
    es.dispatch('delta', { session_id: 'foreign-session', request_id: currentReq, message_id: 'foreign-mid', text: 'foreign' });
    const foreignMsg = env.elements.messages.querySelector('[data-mid="foreign-mid"]');

    // 2. Stale generation_end with old request_id
    let refreshCalled = false;
    env.context.refreshCurrent = async () => { refreshCalled = true; };

    es.dispatch('generation_end', {
      session_id: sid, request_id: staleReq, status: 'complete',
      error: null
    });

    const activeTracked = env.run(`state.activeRequestBySession["${sid}"]`);

    check('F04_1', '跨会话 stale delta 事件被静默拒绝，不写入 DOM',
      foreignMsg === null);

    check('F04_2', '旧 request_id 的迟到 generation_end 不清除当前活跃请求追踪',
      activeTracked === currentReq, { activeTracked });
  }

  // Summary
  const passed = results.filter((r) => r.pass).length;
  const failed = results.filter((r) => !r.pass).length;
  console.log(`\n=== Round 60 Frontend Tests: ${passed}/${results.length} passed, ${failed} failed ===`);

  const payload = {
    total: results.length,
    passed,
    failed,
    browser_verification_driver: "未执行；Node VM 使用模拟 DOM，只验证生产事件处理逻辑，不等同浏览器或视觉验证",
    results
  };

  fs.writeFileSync(OUT, JSON.stringify(payload, null, 2), 'utf8');
  console.log(`Results written to ${OUT}`);
  if (failed > 0) process.exit(1);
}

runFrontendTests();
