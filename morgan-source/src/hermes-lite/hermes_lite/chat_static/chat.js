/* Local chat page (Round 29). No framework, no external asset, no telemetry. */
'use strict';

const $ = (id) => document.getElementById(id);

const DEFAULT_SESSION_SETTINGS = {
  style: 'standard',
  history_mode: 'auto',
  custom_budget: 2048,
  max_tokens: 512,
  use_memory: true,
  learn_memory: true,
  web_search: false,
};

const DEFAULT_APPEARANCE = {
  theme: 'system',
  fontSize: 'standard',
  density: 'standard',
  sendShortcut: 'enter',
};

const state = {
  sessionId: null,
  sessions: [],
  messages: [],
  memories: [],
  // Round 50: persona editor. `personaSaved` is the last body confirmed ON DISK;
  // `personaDirty` means the textarea differs from it. A refresh must never
  // overwrite unsaved edits, so the dirty flag gates every repaint.
  //
  // Round 51: `personaEditSeq` is bumped on EVERY keystroke, and `personaReadSeq`
  // on every GET that is issued. A save or a read only paints the textarea when
  // the response it carries is still the newest thing that happened. That is what
  // stops "save A, keep typing B, response arrives, B is replaced by A".
  //
  // Round 52: `personaWriteRev` orders READS against SAVES and SWITCHES, and a
  // write against a later write. The Round-51 counters only ever ordered an
  // operation against a *later operation of the same kind*, which left the
  // cross-kind case open: a GET issued before "enable" was clicked could land
  // afterwards and paint the switch back to the value it happened to read.
  personaSaved: null,
  personaDirty: false,
  personaEnabled: false,
  personaLoaded: false,
  personaSaving: false,
  personaSwitchPending: false,
  personaEditSeq: 0,
  personaReadSeq: 0,
  personaWriteRev: 0,
  personaEnabledKnown: false,
  personaUnconfirmed: false,
  sessionSettings: Object.assign({}, DEFAULT_SESSION_SETTINGS),
  sessionSettingsCache: {}, // sid -> latest settings
  sessionVersion: {},       // sid -> authoritative server version (_seq)
  confirmedSessionSettings: {}, // sid -> last confirmed settings from server
  settingsSaveError: {},    // sid -> { error, message, ... } or null
  settingsSeq: {},          // sid -> monotonically increasing request sequence
  settingsOpSeq: {},        // sid -> operation counter for updates
  settingsSettledSeq: {},   // sid -> operation counter settled
  pendingSettingsPromise: {}, // sid -> active save Promise chain
  sending: false,           // guard against duplicate send calls
  navToken: 0,              // token to prevent out-of-order session navigation
  lastFocusElement: null,   // element that triggered modal/drawer to restore focus
  boundaryMsgId: null,
  boundarySeq: 0,
  drafts: {},
  lastSentSnapshot: null,
  sessionPendingPrompt: {},
  // Round 39: per-session identity of the attempt the page is tracking. Input
  // recovery must key on session_id AND request_id; a single global "last sent"
  // cannot tell a late event from another attempt apart from the live one.
  activeRequestBySession: {},   // sid -> request_id currently tracked
  failedSearchBySession: {},    // sid -> {text, userMessageId, requestId, error}
  esSid: null,                  // session the open EventSource belongs to
  // Round 39: bumped on every operation that changes what the composer/busy
  // state should be. An async continuation that started before a bump must not
  // write busy/composer state afterwards -- it is answering an older question.
  epoch: 0,
  searchQuery: '',
  busy: false,
  streamingEl: null,
  streamingText: '',
  banner: '',
  notice: '',
  // Round 40: which attempt owns the current *progress* notice (if any).
  // {sid, requestId, text, at} or null. Terminal notices are not tracked here.
  progressNotice: null,
  es: null,
  clientMsgId: null,
  appearance: Object.assign({}, DEFAULT_APPEARANCE),
  modelStatus: null,
  // Round 41: start UNKNOWN, not 4096. Assuming a window before the service
  // reports one is how "success" gets displayed for a value nobody measured.
  modelActualWindow: null,
  modelTargetWindow: null,
  modelInstanceSeq: 0,
  modelReloading: false,
  // Round 41: the backend's capability snapshot. Single source for window,
  // reply reserve and budget limits, so the page never re-derives its own copy.
  capabilities: null,
  capabilitiesRequestSeq: 0,
  // Round 43: model configuration registry as the page sees it.
  modelProfiles: [],
  modelProfileMeta: null,
  currentSummary: null,
  summaryGenerating: false,
};
window.state = state;

/**
 * Safely parse and validate a version sequence number (_seq, base_seq, current_seq).
 * Returns:
 *   number >= 0: valid non-negative integer version (including 0)
 *   null: field is missing (undefined, null, or empty string)
 *   NaN: explicitly invalid value (negative, non-integer, non-numeric string)
 */
function parseSeq(val) {
  if (val === undefined || val === null || val === '') {
    return null;
  }
  const n = Number(val);
  if (Number.isInteger(n) && n >= 0) {
    return n;
  }
  return NaN;
}

/* ------------------------------------------------------------------ API */
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts || {}));
  if (!r.ok) {
    let body = '';
    try { body = await r.text(); } catch (e) { /* ignore */ }
    throw new Error(`${r.status} ${body.slice(0, 200)}`);
  }
  return r.json();
}

const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
const postJson = post;

function toast(text) {
  const el = $('toast');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2200);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* --------------------------------------------------------------------------
   In-App Smooth Floating Dialogs (Confirm & Prompt)
   -------------------------------------------------------------------------- */
function showInAppConfirm({ title = '确认操作', message = '', danger = false, confirmText = '确定', cancelText = '取消', type = 'warning' } = {}) {
  return new Promise((resolve) => {
    const modal = $('confirm-modal');
    if (!modal) {
      resolve(window.confirm(message));
      return;
    }
    const titleEl = $('confirm-title');
    const msgEl = $('confirm-message');
    const inputWrap = $('confirm-input-wrap');
    const iconWrap = $('confirm-icon-wrap');
    const btnOk = $('btn-confirm-ok');
    const btnCancel = $('btn-confirm-cancel');

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    if (inputWrap) inputWrap.hidden = true;
    if (iconWrap) {
      iconWrap.className = 'confirm-icon-wrap' + (danger ? ' danger' : (type === 'info' ? ' info' : ''));
      iconWrap.innerHTML = danger
        ? '<svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
        : '<svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
    }
    if (btnOk) {
      btnOk.textContent = confirmText;
      btnOk.className = danger ? 'btn-primary btn-danger' : 'btn-primary';
    }
    if (btnCancel) btnCancel.textContent = cancelText;

    modal.hidden = false;
    updateBackdropVisibility();

    const cleanup = () => {
      modal.hidden = true;
      updateBackdropVisibility();
      window.removeEventListener('keydown', onKeyDown);
      if (btnOk) btnOk.onclick = null;
      if (btnCancel) btnCancel.onclick = null;
    };

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        cleanup();
        resolve(false);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        cleanup();
        resolve(true);
      }
    };

    window.addEventListener('keydown', onKeyDown);
    if (btnCancel) btnCancel.onclick = () => { cleanup(); resolve(false); };
    if (btnOk) btnOk.onclick = () => { cleanup(); resolve(true); };
    if (btnOk) btnOk.focus();
  });
}

function showInAppPrompt({ title = '输入信息', message = '', defaultValue = '', placeholder = '', confirmText = '确定', cancelText = '取消' } = {}) {
  return new Promise((resolve) => {
    const modal = $('confirm-modal');
    if (!modal) {
      resolve(window.prompt(message, defaultValue));
      return;
    }
    const titleEl = $('confirm-title');
    const msgEl = $('confirm-message');
    const inputWrap = $('confirm-input-wrap');
    const inputEl = $('confirm-input');
    const iconWrap = $('confirm-icon-wrap');
    const btnOk = $('btn-confirm-ok');
    const btnCancel = $('btn-confirm-cancel');

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    if (inputWrap) inputWrap.hidden = false;
    if (inputEl) {
      inputEl.value = defaultValue || '';
      inputEl.placeholder = placeholder || '';
    }
    if (iconWrap) {
      iconWrap.className = 'confirm-icon-wrap info';
      iconWrap.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
    }
    if (btnOk) {
      btnOk.textContent = confirmText;
      btnOk.className = 'btn-primary';
    }
    if (btnCancel) btnCancel.textContent = cancelText;

    modal.hidden = false;
    updateBackdropVisibility();

    const cleanup = () => {
      modal.hidden = true;
      if (inputWrap) inputWrap.hidden = true;
      updateBackdropVisibility();
      window.removeEventListener('keydown', onKeyDown);
      if (btnOk) btnOk.onclick = null;
      if (btnCancel) btnCancel.onclick = null;
    };

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        cleanup();
        resolve(null);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const val = inputEl ? inputEl.value : '';
        cleanup();
        resolve(val);
      }
    };

    window.addEventListener('keydown', onKeyDown);
    if (btnCancel) btnCancel.onclick = () => { cleanup(); resolve(null); };
    if (btnOk) btnOk.onclick = () => {
      const val = inputEl ? inputEl.value : '';
      cleanup();
      resolve(val);
    };
    if (inputEl) {
      inputEl.focus();
      inputEl.select();
    }
  });
}

function formatCleanDate(raw) {
  if (!raw) return '';
  try {
    const s = String(raw).trim();
    if (s.includes('T')) {
      const d = new Date(s);
      if (!isNaN(d.getTime())) {
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      }
    }
    return s.replace(/T/, ' ').replace(/\+08:00$/, '');
  } catch (e) {
    return raw;
  }
}


/* Minimal Markdown renderer */
function md(src) {
  const raw = String(src == null ? '' : src);
  const blocks = [];
  const BT = '\u0000B';
  let text = raw.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push({lang: lang || '', code: code.replace(/\n$/, '')});
    return BT + (blocks.length - 1) + '\u0000';
  });

  text = esc(text);

  const lines = text.split('\n');
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of lines) {
    if (!line.trim()) { closeList(); out.push(''); continue; }
    const mOl = line.match(/^\s*\d+\.\s+(.*)$/);
    const mUl = line.match(/^\s*[-*]\s+(.*)$/);
    const mH = line.match(/^(#{1,4})\s+(.*)$/);
    if (mOl) {
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inline(mOl[1])}</li>`);
    } else if (mUl) {
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inline(mUl[1])}</li>`);
    } else if (mH) {
      closeList();
      out.push(`<p><strong>${inline(mH[2])}</strong></p>`);
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  let html = out.join('\n');

  html = html.replace(/\u0000B(\d+)\u0000/g, (m, i) => {
    const b = blocks[Number(i)];
    const label = b.lang ? `<span class="code-lang">${esc(b.lang)}</span>` : '';
    return `<pre>${label}<code>${esc(b.code)}</code></pre>`;
  });
  return html;
}

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
}

function timeLabel(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------------------------------------------------------------- Appearance */
function loadAppearance() {
  try {
    const saved = localStorage.getItem('hermes_lite_appearance');
    if (saved) {
      Object.assign(state.appearance, JSON.parse(saved));
    }
  } catch (e) { /* ignore */ }
  applyAppearance();
}

function saveAppearance() {
  try {
    localStorage.setItem('hermes_lite_appearance', JSON.stringify(state.appearance));
  } catch (e) { /* ignore */ }
  applyAppearance();
}

function applyAppearance() {
  const root = document.documentElement;
  root.setAttribute('data-theme', state.appearance.theme);
  root.setAttribute('data-font-size', state.appearance.fontSize);
  root.setAttribute('data-density', state.appearance.density);

  if ($('setting-theme')) $('setting-theme').value = state.appearance.theme;
  if ($('setting-font-size')) $('setting-font-size').value = state.appearance.fontSize;
  if ($('setting-density')) $('setting-density').value = state.appearance.density;
  if ($('setting-send-shortcut')) $('setting-send-shortcut').value = state.appearance.sendShortcut;

  const isCtrl = state.appearance.sendShortcut === 'ctrl_enter';
  const hint = isCtrl ? 'Ctrl+Enter 发送' : 'Enter 发送';
  const ph = isCtrl ? '输入消息，Ctrl+Enter 发送，Enter 换行' : '输入消息，Enter 发送，Shift+Enter 换行';
  if ($('shortcut-hint')) $('shortcut-hint').textContent = hint;
  if ($('input')) $('input').placeholder = ph;
}

/* --------------------------------------------------------------- Sessions */
async function loadSessions() {
  const out = await api('/api/chat/sessions');
  state.sessions = out.sessions || [];
  renderSessions();
}

function getFilteredSessions() {
  const q = (state.searchQuery || '').trim().toLowerCase();
  if (!q) return state.sessions;
  return state.sessions.filter((s) => {
    const t = (s.title || '').toLowerCase();
    const l = (s.last_message || '').toLowerCase();
    return t.includes(q) || l.includes(q);
  });
}

function renderSessions() {
  const box = $('session-list');
  if (!box) return;
  box.innerHTML = '';
  const filtered = getFilteredSessions();

  if ($('session-count-label')) {
    if (state.searchQuery.trim()) {
      $('session-count-label').textContent = `搜索结果 (${filtered.length}/${state.sessions.length})`;
    } else {
      $('session-count-label').textContent = `对话 (${state.sessions.length})`;
    }
  }

  if (!filtered.length) {
    const emptyMsg = state.searchQuery.trim()
      ? '未找到匹配对话<br><span style="font-size:11px;opacity:0.8;">（仅搜索标题与最后预览）</span>'
      : '还没有对话';
    box.innerHTML = `<div class="mem-empty">${emptyMsg}</div>`;
    return;
  }

  for (const s of filtered) {
    const b = document.createElement('button');
    b.className = 'session-item' + (s.id === state.sessionId ? ' active' : '');
    b.dataset.sid = s.id;
    const sub = s.last_message ? String(s.last_message).replace(/\s+/g, ' ').slice(0, 32) : '（空）';
    b.innerHTML = `<span class="s-title">${esc(s.title)}</span>` +
                  `<span class="s-sub">${s.message_count} 条 · ${esc(sub)}</span>`;
    b.onclick = () => openSession(s.id);
    box.appendChild(b);
  }
}

async function newSession() {
  const out = await post('/api/chat/sessions', {});
  if (!out.ok) { toast('新建失败'); return; }
  await loadSessions();
  await openSession(out.session.id);
  $('input').focus();
}

async function openSession(sid) {
  if (!sid) return;

  // 1. Save draft for previous session before switching
  if (state.sessionId && $('input')) {
    state.drafts[state.sessionId] = $('input').value;
  }

  // 2. If in narrow screen overlay mode, close sidebar on selection
  if (window.innerWidth <= 800 && $('sidebar')) {
    $('sidebar').classList.remove('open');
    updateBackdropVisibility();
  }

  // 3. Stale navigation token. Also bump the UI epoch: an in-flight
  // generation_end handler that is waiting on a network call must be able to
  // tell that the user has moved to another session (round 40).
  const currentNavToken = ++state.navToken;
  state.epoch++;
  closeStream();

  const out = await api(`/api/chat/session?id=${encodeURIComponent(sid)}`);
  // Drop response if user navigated away while request was in flight
  if (currentNavToken !== state.navToken) {
    return;
  }
  if (!out.ok) { toast('会话不存在'); return; }

  state.sessionId = sid;
  state.capabilities = null;
  state.messages = out.messages || [];
  const backendSettings = out.session.settings || {};
  const parsedServerSeq = parseSeq(backendSettings._seq);
  let serverSeq;
  if (parsedServerSeq === null) {
    // Missing version field in legacy session: align with backend default of 0
    serverSeq = 0;
  } else if (Number.isNaN(parsedServerSeq)) {
    // Explicitly invalid version: record error and do NOT guess 1!
    console.error(`[Hermes] Invalid _seq received from server for session ${sid}:`, backendSettings._seq);
    serverSeq = NaN;
  } else {
    serverSeq = parsedServerSeq;
  }

  // Adopt authoritative server state on initial load or if server is newer
  if (Number.isNaN(serverSeq)) {
    state.sessionVersion[sid] = NaN;
    state.settingsSaveError[sid] = {
      error: 'invalid_version',
      message: '服务端会话版本格式非法，请刷新重试'
    };
  } else {
    const hasKnown = typeof state.sessionVersion[sid] === 'number' && !Number.isNaN(state.sessionVersion[sid]);
    if (!hasKnown || serverSeq >= state.sessionVersion[sid]) {
      state.sessionVersion[sid] = serverSeq;
      state.confirmedSessionSettings[sid] = Object.assign({}, backendSettings);
      state.sessionSettings = Object.assign({}, DEFAULT_SESSION_SETTINGS, backendSettings);
      state.sessionSettingsCache[sid] = Object.assign({}, state.sessionSettings);
      state.settingsSaveError[sid] = null;
    } else {
      state.sessionSettings = Object.assign({}, DEFAULT_SESSION_SETTINGS, backendSettings, state.sessionSettingsCache[sid] || {});
    }
  }
  state.boundaryMsgId = out.session.boundary_msg_id || null;
  state.boundarySeq = out.session.boundary_seq || 0;
  state.currentSummary = out.summary || null;
  state.lastPlan = null;
  state.streamingEl = null;
  state.streamingText = '';
  state.banner = '';

  $('session-title').textContent = out.session.title;
  $('session-meta').textContent =
    `${out.session.message_count} 条消息 · 创建于 ${timeLabel(out.session.created_at)}`;
  $('banner').hidden = true;
  setNotice('');

  // 4. Restore draft strictly for new session
  const restoredDraft = state.drafts[sid] || '';
  $('input').value = restoredDraft;
  adjustInputHeight();

  // 5. Round 39: if this session's last turn is a user message with no reply,
  // its search failed while the user was elsewhere (switching sessions closes
  // that session's event stream, so the failure event never reached this page).
  // Make the question recoverable here, without touching any other session.
  const unanswered = findUnansweredUserMessage(state.messages);
  if (unanswered && !restoredDraft.trim() && !state.failedSearchBySession[sid]) {
    state.failedSearchBySession[sid] = {
      sessionId: sid,
      text: unanswered.content || '',
      userMessageId: unanswered.id,
      requestId: unanswered.request_id || null,
      error: '该问题没有收到回答',
      errorKind: 'unanswered_turn',
      reasonZh: '这个问题没有收到回答',
    };
    state.drafts[sid] = unanswered.content || '';
    $('input').value = state.drafts[sid];
    adjustInputHeight();
    setNoticeWithActions(
      `🌐 上一次提问没有收到回答（可能是联网搜索未成功）。已把原问题放回输入框，可重试或修改。`,
      true,
      [{ label: '重试原问题', onClick: () => retryFailedWebSearch(sid, unanswered.content || '', unanswered.id) }]
    );
  } else if (restoredDraft.trim()) {
    $('input').value = restoredDraft;
    adjustInputHeight();
  }

  syncQuickBar();
  syncSettingsModal();
  if ($('compress-modal') && !$('compress-modal').hidden) {
    loadCompressModalData();
  }
  updateContextIndicator();
  renderMessages();
  renderSessions();
  drawBusy(out.active, out.model_slot);
  $('input').focus();
  await loadCapabilities();
}

async function renameSession() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  closeMoreMenu();
  const title = await showInAppPrompt({
    title: '重命名对话',
    message: '请输入新的对话名称：',
    defaultValue: $('session-title').textContent || ''
  });
  if (title == null) return;
  const out = await post('/api/chat/sessions/rename',
                         {session_id: targetSid, title});
  if (!out.ok) { toast(out.message || '重命名失败'); return; }
  if (state.sessionId === targetSid) {
    $('session-title').textContent = out.session.title;
  }
  await loadSessions();
  toast('已重命名');
}

async function deleteSession() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  closeMoreMenu();
  const ok = await showInAppConfirm({
    title: '删除对话',
    message: '确定删除该对话？其中的消息会一起删除。\n由它独有的记忆也会删除（仍被其他对话支持的记忆会保留）。',
    danger: true,
    confirmText: '删除'
  });
  if (!ok) return;
  const out = await post('/api/chat/sessions/delete', {session_id: targetSid});
  if (!out.ok) { toast('删除失败'); return; }
  const removed = (out.memories_removed || []).length;
  const kept = (out.memories_kept || []).length;
  delete state.drafts[targetSid];
  delete state.sessionSettingsCache[targetSid];
  delete state.sessionVersion[targetSid];
  delete state.confirmedSessionSettings[targetSid];
  delete state.settingsSaveError[targetSid];
  delete state.pendingSettingsPromise[targetSid];
  delete state.settingsSeq[targetSid];
  delete state.settingsOpSeq[targetSid];
  delete state.settingsSettledSeq[targetSid];
  if (state.sessionId === targetSid) {
    state.sessionId = null;
    state.messages = [];
  }
  await loadSessions();
  if (state.sessions.length) await openSession(state.sessions[0].id);
  else { resetMain(); }
  await loadMemories();
  toast(`已删除对话${removed ? `，并移除 ${removed} 条独有记忆` : ''}${kept ? `，保留 ${kept} 条共用记忆` : ''}`);
}

function resetMain() {
  $('session-title').textContent = '没有对话';
  $('session-meta').textContent = '';
  state.messages = [];
  renderMessages();
}

/* ---------------------------------------------------- Session Settings API */

/**
 * Round 41: is this reply cap one of the presets, or a custom value?
 *
 * The presets are shortcuts, not the allowed set, so the UI has to be able to
 * show "768" as a custom entry rather than silently snapping to a preset.
 */
function maxTokensIsPreset(v) {
  return [128, 512, 1024].includes(Number(v));
}

/** Mirror a numeric reply cap into both the quick bar and the modal. */
function syncMaxTokensControls(v) {
  const n = Number(v);
  const preset = maxTokensIsPreset(n);
  if ($('quick-max-tokens')) {
    $('quick-max-tokens').value = preset ? String(n) : 'custom';
  }
  if ($('quick-max-tokens-custom')) {
    $('quick-max-tokens-custom').hidden = preset;
    if (!preset) $('quick-max-tokens-custom').value = String(n);
  }
  if ($('setting-max-tokens')) {
    $('setting-max-tokens').value = preset ? String(n) : 'custom';
  }
  if ($('setting-max-tokens-custom')) {
    $('setting-max-tokens-custom').hidden = preset;
    if (!preset) $('setting-max-tokens-custom').value = String(n);
  }
}

function syncQuickBar() {
  const cfg = state.sessionSettings;
  if ($('quick-style')) $('quick-style').value = cfg.style || 'standard';
  if ($('quick-history')) $('quick-history').value = cfg.history_mode || 'auto';
  syncMaxTokensControls(cfg.max_tokens || 512);
  if ($('quick-web-search')) $('quick-web-search').checked = Boolean(cfg.web_search);
}

function syncSettingsModal() {
  const cfg = state.sessionSettings;
  if ($('setting-style')) $('setting-style').value = cfg.style || 'standard';
  if ($('setting-history')) {
    $('setting-history').value = cfg.history_mode || 'auto';
    toggleCustomBudgetVisibility(cfg.history_mode === 'custom');
  }
  if ($('setting-custom-budget')) $('setting-custom-budget').value = cfg.custom_budget || 2048;
  syncMaxTokensControls(cfg.max_tokens || 512);
  if ($('setting-use-memory')) $('setting-use-memory').checked = !!cfg.use_memory;
  if ($('setting-learn-memory')) $('setting-learn-memory').checked = !!cfg.learn_memory;
  if ($('setting-web-search')) $('setting-web-search').checked = Boolean(cfg.web_search);
  syncCustomBudgetLimits();
}

/**
 * Round 41: keep the custom-budget input's band in sync with the LIVE window.
 *
 * The old page hardcoded max=3584. The real ceiling is
 * `actual_window - reply_reserve`, which changes when either of those changes,
 * so the input attributes are recomputed instead of being declared in HTML.
 */
function syncCustomBudgetLimits() {
  const limits = (state.capabilities
                  && state.capabilities.input
                  && state.capabilities.input.custom_limits) || null;
  const input = $('setting-custom-budget');
  const hint = $('hint-custom-budget');
  if (!input) return;
  if (!limits || !limits.known) {
    input.removeAttribute('max');
    input.min = String((limits && limits.min) || 256);
    if (hint) {
      hint.textContent = (limits && limits.reason)
        ? `当前无法确定可用上限：${limits.reason}。`
        : '尚未获得实际上下文窗口，暂时无法确定可用上限。';
    }
    return;
  }
  input.min = String(limits.min);
  input.max = String(limits.max);
  input.step = String(limits.step || 64);
  if (hint) {
    hint.textContent =
      `这是本次输入的上下文预算上限（当前可用 ${limits.min}~${limits.max}），`
      + '并非模型的物理上下文窗口。公式：实际窗口 − 回复预留。';
  }
}

function toggleCustomBudgetVisibility(show) {
  const grp = $('group-custom-budget');
  if (grp) grp.hidden = !show;
}

/* ----------------------------------------------- Round 41 param validation */
/**
 * Client-side mirrors of `chatparams.parse_strict_int` / `validate_max_output`.
 *
 * These exist ONLY to give immediate feedback. The backend re-validates every
 * write with the same rules, and the tests assert the two agree, so the page can
 * never let through a value the server would silently reject (or vice versa).
 *
 * Deliberately NOT used: parseInt(), Number(), `| 0`, Math.trunc. Each of those
 * silently turns "768.7" into 768 or "" into 0, which is exactly the class of
 * bug this round removes.
 */
function parseStrictIntClient(raw) {
  if (raw === null || raw === undefined) {
    return { ok: false, code: 'empty', message: '不能为空。' };
  }
  if (typeof raw === 'boolean') {
    return { ok: false, code: 'not_integer', message: '必须是整数，不能是布尔值。' };
  }
  const s = String(raw).trim();
  if (!s) return { ok: false, code: 'empty', message: '不能为空。' };
  // Plain integer literal only: optional sign then digits, nothing else.
  if (!/^[+-]?\d+$/.test(s)) {
    return { ok: false, code: 'not_integer',
             message: `必须是整数，收到「${s}」（不会自动截断小数）。` };
  }
  const n = Number(s);
  if (!Number.isSafeInteger(n)) {
    return { ok: false, code: 'not_integer', message: `数值过大：${s}。` };
  }
  return { ok: true, value: n };
}

function replyLimits() {
  const r = (state.capabilities && state.capabilities.reply) || {};
  return { min: r.min != null ? r.min : 32,
           max: r.max != null ? r.max : 32768 };
}

function actualWindowOrNull() {
  const w = state.capabilities && state.capabilities.window;
  const v = w ? w.actual : state.modelActualWindow;
  return (typeof v === 'number' && v > 0) ? v : null;
}

function validateMaxTokensClient(raw) {
  const p = parseStrictIntClient(raw);
  if (!p.ok) return p;
  const n = p.value;
  const lim = replyLimits();
  if (n <= 0) {
    return { ok: false, code: 'not_positive', message: `回复上限必须大于 0，收到 ${n}。` };
  }
  if (n < lim.min) {
    return { ok: false, code: 'too_small',
             message: `回复上限至少 ${lim.min} tokens，收到 ${n}。` };
  }
  if (n > lim.max) {
    return { ok: false, code: 'too_large',
             message: `回复上限最大 ${lim.max} tokens，收到 ${n}。` };
  }
  const win = actualWindowOrNull();
  if (win) {
    const room = win - n;
    const minRoom = (state.capabilities && state.capabilities.input
                     && state.capabilities.input.min_room) || 256;
    if (room < minRoom) {
      return { ok: false, code: 'window_conflict',
               message: `回复上限 ${n} 与当前实际窗口 ${win} 冲突：`
                        + `留给输入的空间只剩 ${room} tokens，至少要保留 ${minRoom}。`
                        + '请调小回复上限，或先把模型窗口调大。' };
    }
  }
  return { ok: true, value: n };
}

function validateContextWindowClient(raw) {
  const p = parseStrictIntClient(raw);
  if (!p.ok) return p;
  const n = p.value;
  const w = (state.capabilities && state.capabilities.window) || {};
  const min = w.min != null ? w.min : 512;
  const max = w.max != null ? w.max : 131072;
  const step = w.step != null ? w.step : 256;
  if (n <= 0) return { ok: false, code: 'not_positive', message: `窗口必须大于 0，收到 ${n}。` };
  if (n < min) return { ok: false, code: 'too_small', message: `窗口至少 ${min} tokens，收到 ${n}。` };
  if (n > max) return { ok: false, code: 'too_large', message: `窗口最大 ${max} tokens，收到 ${n}。` };
  if (n % step) {
    const lo = Math.floor(n / step) * step;
    return { ok: false, code: 'not_aligned',
             message: `窗口需要是 ${step} 的整数倍，收到 ${n}（相邻可取值 ${lo} 或 ${lo + step}）。` };
  }
  return { ok: true, value: n };
}

function validateCustomBudgetClient(raw, maxOutput = null) {
  const p = parseStrictIntClient(raw);
  if (!p.ok) return p;
  const n = p.value;
  const lim = (state.capabilities && state.capabilities.input
               && state.capabilities.input.custom_limits) || null;
  const min = (lim && lim.min) || 256;
  if (n < min) {
    return { ok: false, code: 'too_small',
             message: `自定义输入预算至少 ${min} tokens，收到 ${n}。` };
  }
  const win = actualWindowOrNull();
  const upper = (win && maxOutput != null) ? win - maxOutput : (lim && lim.known ? lim.max : null);
  if (upper != null && n > upper) {
    return { ok: false, code: 'exceeds_room',
             message: `自定义输入预算 ${n} 超出当前可用空间（上限 ${upper}）。`
                      + '请调小该值，或调整窗口/回复上限。' };
  }
  return { ok: true, value: n };
}

/** Show/hide an inline validation message inside the settings modal. */
function setParamError(text) {
  const el = $('param-error');
  if (!el) { if (text) toast(text); return; }
  if (!text) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = text;
}

async function dispatchSessionSettingsUpdate(targetSid, newSettings, { isReset = false } = {}) {
  if (!targetSid) return { ok: false, error: 'no_session', message: '未指定会话' };

  // 1. Operation counter tracking
  state.settingsOpSeq[targetSid] = (state.settingsOpSeq[targetSid] || 0) + 1;
  const opId = state.settingsOpSeq[targetSid];

  // 2. Optimistic UI update for current session
  if (state.sessionId === targetSid) {
    if (isReset) {
      state.sessionSettings = Object.assign({}, DEFAULT_SESSION_SETTINGS);
    } else {
      Object.assign(state.sessionSettings, newSettings);
    }
    state.sessionSettingsCache[targetSid] = Object.assign({}, state.sessionSettings);
    syncQuickBar();
    syncSettingsModal();
    updateContextIndicator();
  }

  // 3. Chained promise queue
  const prevPromise = state.pendingSettingsPromise[targetSid];
  const savePromise = (async () => {
    if (prevPromise) {
      try {
        const prevRes = await prevPromise;
        if (prevRes && prevRes.ok === false) {
          // If preceding queued update failed, stop cascade
          return prevRes;
        }
      } catch (e) {
        return { ok: false, error: 'previous_failed', message: e.message || '前序设置保存失败' };
      }
    }

    const knownVersion = state.sessionVersion[targetSid];
    const parsedKnown = parseSeq(knownVersion);
    let baseSeq;
    if (parsedKnown === null) {
      // Missing: compatible with backend unversioned default of 0
      baseSeq = 0;
    } else if (Number.isNaN(parsedKnown)) {
      // Explicitly invalid: reject without guessing 1
      return {
        ok: false,
        error: 'invalid_base_seq',
        message: '会话版本非法，无法执行安全保存。'
      };
    } else {
      baseSeq = parsedKnown;
    }

    const url = isReset ? '/api/chat/sessions/settings/reset' : '/api/chat/sessions/settings';
    const payload = isReset
      ? { session_id: targetSid, base_seq: baseSeq }
      : { session_id: targetSid, settings: newSettings, base_seq: baseSeq };

    let res;
    try {
      res = await post(url, payload);
    } catch (netErr) {
      res = { ok: false, error: 'network_error', message: netErr.message || '网络异常，设置保存未成功' };
    }
    return res;
  })();

  state.pendingSettingsPromise[targetSid] = savePromise;

  try {
    const res = await savePromise;
    state.settingsSettledSeq[targetSid] = opId;
    if (state.pendingSettingsPromise[targetSid] === savePromise) {
      delete state.pendingSettingsPromise[targetSid];
    }

    if (res && res.ok) {
      state.settingsSaveError[targetSid] = null;
      const respSeq = parseSeq(res.current_seq !== undefined ? res.current_seq : (res.settings && res.settings._seq));
      if (respSeq !== null && !Number.isNaN(respSeq)) {
        state.sessionVersion[targetSid] = respSeq;
      }
      if (res.settings) {
        state.confirmedSessionSettings[targetSid] = Object.assign({}, res.settings);
        state.sessionSettingsCache[targetSid] = Object.assign({}, res.settings);
      }

      if (state.sessionId === targetSid) {
        if (res.settings) {
          state.sessionSettings = Object.assign({}, res.settings);
        }
        syncQuickBar();
        syncSettingsModal();
        updateContextIndicator();
        if (state.notice && state.notice.includes('设置')) {
          setNotice('');
        }
        state.capabilities = null;
        renderCapabilities();
        syncCustomBudgetLimits();
        await loadCapabilities();
        if (state.sessionId === targetSid) toast(isReset ? '已恢复本会话默认设置' : '设置已更新');
      }
      return { ok: true, settings: res.settings, current_seq: state.sessionVersion[targetSid] };
    } else {
      const errInfo = {
        error: (res && res.error) || 'save_failed',
        message: (res && res.message) || '设置保存失败',
        serverSettings: (res && res.settings) || null,
        currentSeq: res && res.current_seq !== undefined ? res.current_seq : null
      };
      state.settingsSaveError[targetSid] = errInfo;

      if (res && (res.error === 'version_conflict' || res.error === 'future_version')) {
        const syncSeq = parseSeq(res.current_seq !== undefined ? res.current_seq : (res.settings && res.settings._seq));
        if (syncSeq !== null && !Number.isNaN(syncSeq)) {
          state.sessionVersion[targetSid] = syncSeq;
        }
        if (res.settings) {
          state.confirmedSessionSettings[targetSid] = Object.assign({}, res.settings);
        }
      }

      if (state.sessionId === targetSid) {
        setNotice(`设置保存失败（${errInfo.message}）。已阻止发送。可点击“恢复已保存设置”或重新修改。`, true);
        toast(`设置保存失败：${errInfo.message}`);
      }
      return { ok: false, error: errInfo.error, message: errInfo.message };
    }
  } catch (err) {
    state.settingsSettledSeq[targetSid] = opId;
    if (state.pendingSettingsPromise[targetSid] === savePromise) {
      delete state.pendingSettingsPromise[targetSid];
    }
    state.settingsSaveError[targetSid] = {
      error: 'exception',
      message: err.message || '设置保存异常'
    };
    if (state.sessionId === targetSid) {
      setNotice(`设置保存失败：${err.message || '网络异常'}`, true);
      toast('设置保存失败');
    }
    return { ok: false, error: 'exception', message: err.message };
  }
}

function revertSessionSettings(sid) {
  const targetSid = sid || state.sessionId;
  if (!targetSid) return;
  const confirmed = state.confirmedSessionSettings[targetSid] || DEFAULT_SESSION_SETTINGS;
  state.sessionSettings = Object.assign({}, DEFAULT_SESSION_SETTINGS, confirmed);
  state.sessionSettingsCache[targetSid] = Object.assign({}, state.sessionSettings);
  state.settingsSaveError[targetSid] = null;
  const confSeq = parseSeq(confirmed._seq);
  if (confSeq !== null && !Number.isNaN(confSeq)) {
    state.sessionVersion[targetSid] = confSeq;
  }
  if (state.sessionId === targetSid) {
    syncQuickBar();
    syncSettingsModal();
    updateContextIndicator();
    setNotice('');
    toast('已恢复已保存的设置');
  }
}

async function saveSessionSettingsFromModal() {
  const targetSid = state.sessionId;
  if (!targetSid) return;

  // Round 41: validate BEFORE building the patch. No silent clamping: if the
  // user typed something invalid or conflicting, say so and stop.
  const mode = $('setting-history') ? $('setting-history').value : 'auto';
  const rawMax = ($('setting-max-tokens').value === 'custom')
    ? ($('setting-max-tokens-custom') ? $('setting-max-tokens-custom').value : '')
    : $('setting-max-tokens').value;

  const maxRes = validateMaxTokensClient(rawMax);
  if (!maxRes.ok) {
    setParamError(`回复上限无效：${maxRes.message}`);
    toast('回复上限无效，未保存');
    return;
  }

  let budgetValue = null;
  if (mode === 'custom') {
    const rawBudget = $('setting-custom-budget')
      ? $('setting-custom-budget').value : '';
    const budRes = validateCustomBudgetClient(rawBudget, maxRes.value);
    if (!budRes.ok) {
      setParamError(`自定义输入预算无效：${budRes.message}`);
      toast('输入预算无效，未保存');
      return;
    }
    budgetValue = budRes.value;
  }

  setParamError('');
  const settings = {
    style: $('setting-style').value,
    history_mode: mode,
    max_tokens: maxRes.value,
    use_memory: $('setting-use-memory').checked,
    learn_memory: $('setting-learn-memory').checked,
    web_search: $('setting-web-search') ? $('setting-web-search').checked : false,
  };
  if (budgetValue !== null) settings.custom_budget = budgetValue;

  const res = await dispatchSessionSettingsUpdate(targetSid, settings);
  if (res && res.ok) {
    closeSettingsModal();
  } else if (res && res.message) {
    setParamError(res.message);
  }
}

async function updateQuickSetting(key, val) {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  const patch = {};
  if (key === 'max_tokens') {
    // Round 41: same rules as the modal; a preset is just a shortcut.
    const v = validateMaxTokensClient(val);
    if (!v.ok) {
      toast(`回复上限无效：${v.message}`);
      syncQuickBar();  // snap the control back to the stored value
      return { ok: false, error: v.code, message: v.message };
    }
    patch[key] = v.value;
  } else {
    patch[key] = val;
  }
  return await dispatchSessionSettingsUpdate(targetSid, patch);
}

async function resetSessionSettings() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  return await dispatchSessionSettingsUpdate(targetSid, {}, { isReset: true });
}

/* -------------------------------------------------------- Restart Boundary */
async function setRestartBoundary() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  closeMoreMenu();
  if (state.busy) {
    toast('生成进行中，请先停止生成再设置分界线');
    return;
  }
  const ok = await showInAppConfirm({
    title: '从此处重新开始',
    message: '确定从当前位置之后作为全新上下文开始？\n此前所有消息仍会完整保存在本地并展示，但后续回答将不再带入分界线之前的对话记录。',
    confirmText: '确定开始'
  });
  if (!ok) return;

  const res = await post('/api/chat/sessions/restart_boundary', {
    session_id: targetSid,
  });
  if (res.ok) {
    if (state.sessionId === targetSid) {
      state.boundaryMsgId = res.boundary_msg_id;
      state.boundarySeq = res.boundary_seq;
      renderMessages();
      updateContextIndicator();
    }
    toast('已在此处设置分界线，后续对话将从此处重新开始');
  } else {
    toast(res.message || '设置分界线失败');
  }
}

async function clearRestartBoundary() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  closeMoreMenu();
  if (state.busy) {
    toast('生成进行中，请先停止生成再清除分界线');
    return;
  }
  const res = await post('/api/chat/sessions/clear_boundary', {
    session_id: targetSid,
  });
  if (res.ok) {
    if (state.sessionId === targetSid) {
      state.boundaryMsgId = null;
      state.boundarySeq = 0;
      renderMessages();
      updateContextIndicator();
    }
    toast('已清除分界线，后续对话将恢复完整历史');
  } else {
    toast(res.message || '清除分界线失败');
  }
}

/* --------------------------------------------------------------- Messages */
function renderMessages() {
  const box = $('messages');
  if (!box) return;
  box.innerHTML = '';
  if (!state.messages.length) {
    box.innerHTML = `<div class="empty" id="empty-state">
      <p class="empty-title">开始一段对话</p>
      <p class="empty-sub">输入消息即可交流。会话记录自动保存在本机，离线安全可用。</p></div>`;
    return;
  }

  for (const m of state.messages) {
    box.appendChild(messageNode(m));
    if (state.boundaryMsgId && m.id === state.boundaryMsgId) {
      box.appendChild(boundaryNode());
    }
  }
  scrollBottom();
}

function boundaryNode() {
  const el = document.createElement('div');
  el.className = 'chat-boundary';
  el.innerHTML = `
    <span class="chat-boundary-line"></span>
    <span class="chat-boundary-badge">⚡ 已从此处重新开始（以上记录不带入后续上下文）</span>
    <span class="chat-boundary-line"></span>`;
  return el;
}

function messageNode(m) {
  const row = document.createElement('div');
  row.className = 'msg ' + m.role;
  row.dataset.mid = m.id;

  const wrap = document.createElement('div');
  wrap.className = 'bubble-wrap';

  // If assistant message and has web_search metadata with results, show sources card
  if (m.role === 'assistant' && m.web_search && m.web_search.results && m.web_search.results.length) {
    const sourcesCard = renderSourcesCard(m.web_search);
    wrap.appendChild(sourcesCard);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (m.role === 'assistant') bubble.innerHTML = md(m.content || '');
  else bubble.textContent = m.content || '';
  wrap.appendChild(bubble);

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  const parts = [`<span>${timeLabel(m.created_at)}</span>`];
  // Round 54: "联网回答" must mean "material from a search really went into the
  // prompt that produced this reply". Requiring status === 'ready' is not enough
  // on its own -- `discarded` (results found but all irrelevant) and `empty`
  // must never carry the tag -- so the results array must also be non-empty.
  // History is rendered from the stored record as-is: an old message is never
  // retro-labelled because the switch happens to be on now.
  if (m.role === 'assistant' && m.web_search
      && m.web_search.status === 'ready'
      && Array.isArray(m.web_search.results)
      && m.web_search.results.length > 0) {
    parts.push('<span class="state-tag web-search-tag" title="本次回答参考了联网搜索摘要，回答由本地模型生成"><svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg><span>联网回答</span></span>');
  }
  if (m.status && m.status !== 'complete') {
    const label = {
      streaming: '生成中',
      interrupted: '已中断（以下只是已收到的部分）',
      error: m.content ? '出错（以下只是已收到的部分）' : (m.error ? `失败：${m.error}` : '生成失败'),
    }[m.status] || m.status;
    parts.push(`<span class="state-tag ${m.status}">${esc(label)}</span>`);
  } else if (m.status === 'complete' && m.error && (m.error.includes('截断') || m.error.includes('回复上限'))) {
    parts.push(`<span class="state-tag length" title="${esc(m.error)}">已达回复上限 (截断)</span>`);
  }
  if (m.role === 'assistant') {
    parts.push('<button class="copy-btn" data-copy><svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><span>复制</span></button>');
  }
  meta.innerHTML = parts.join('');
  const copy = meta.querySelector('[data-copy]');
  if (copy) {
    copy.onclick = async () => {
      const txt = bubble.textContent || '';
      try { await navigator.clipboard.writeText(txt); toast('已复制'); }
      catch (e) { toast('复制失败，请手动选择'); }
    };
  }
  wrap.appendChild(meta);
  row.appendChild(wrap);
  return row;
}

function renderSourcesCard(webSearch) {
  const card = document.createElement('div');
  card.className = 'sources-card';
  const results = webSearch.results || [];
  const count = results.length;
  const trimmed = webSearch.trimmed_count || 0;
  const trimNote = trimmed > 0 ? ` · 因上下文预算精选前 ${count} 条` : '';

  let itemsHtml = '';
  for (const r of results) {
    const pubHtml = r.published_date ? `<span class="source-date">发布于: ${esc(formatCleanDate(r.published_date))}</span>` : '';
    const timeHtml = r.retrieved_at ? `<span class="source-time">检索于: ${esc(formatCleanDate(r.retrieved_at))}</span>` : '';
    let href = esc(r.url || '#');
    if (!href.startsWith('http://') && !href.startsWith('https://')) {
      href = '#';
    }
    const engineName = r.backend || webSearch.backend || '';
    const backendBadge = engineName ? `<span class="source-backend">${esc(engineName)}</span>` : '';
    itemsHtml += `
      <div class="source-item">
        <div class="source-head">
          <span class="source-idx">[${r.index}]</span>
          <a class="source-title" href="${href}" target="_blank" rel="noopener noreferrer">${esc(r.title || '网页链接')}</a>
          ${backendBadge}
          <span class="source-domain">${esc(r.domain || '')}</span>
          <span class="source-badge">搜索摘要</span>
        </div>
        <div class="source-snippet">${esc(r.snippet || '')}</div>
        <div class="source-meta-row">${pubHtml} ${timeHtml}</div>
      </div>
    `;
  }

  card.innerHTML = `
    <div class="sources-header" role="button" tabindex="0" aria-expanded="false">
      <svg class="sources-icon-svg" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
      <span class="sources-title">参考搜索资料 (${count}条${trimNote})</span>
      <span class="sources-toggle-hint">
        <span class="sources-toggle-text">展开</span>
        <svg class="sources-toggle-chevron" viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
      </span>
    </div>
    <div class="sources-body" hidden>
      ${itemsHtml}
    </div>
  `;

  const header = card.querySelector('.sources-header');
  const body = card.querySelector('.sources-body');
  const toggleHint = card.querySelector('.sources-toggle-hint');

  const toggle = () => {
    const isHidden = body.hidden;
    body.hidden = !isHidden;
    header.setAttribute('aria-expanded', String(!isHidden));
    const toggleText = card.querySelector('.sources-toggle-text');
    if (toggleText) toggleText.textContent = isHidden ? '收起' : '展开';
    card.classList.toggle('expanded', isHidden);
  };

  header.onclick = toggle;
  header.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggle();
    }
  };

  return card;
}

function scrollBottom() {
  const box = $('messages');
  if (box) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------- Context Info */
function updateContextIndicator(exactPlan) {
  const el = $('ctx-indicator');
  if (!el) return;
  const cfg = state.sessionSettings;
  const maxTokens = cfg.max_tokens || 512;
  // Round 41: the live capability snapshot wins; the legacy fields are only a
  // fallback for the brief window before the first /api/chat/capabilities load.
  const capWin = (state.capabilities && state.capabilities.window
                  && state.capabilities.window.actual) || null;
  const modelWin = capWin || state.modelActualWindow || null;
  const maxAvail = modelWin ? Math.max(0, modelWin - maxTokens) : null;

  if (exactPlan) {
    state.lastPlan = exactPlan;
  }
  const plan = exactPlan || state.lastPlan;

  if (plan && plan.turns_included != null) {
    const turns = plan.turns_included != null ? plan.turns_included : '—';
    const tokens = plan.prompt_tokens != null ? ` (${plan.prompt_tokens} tokens)` : '';
    const budgetNote = (plan.is_custom_budget && plan.budget != null) ? ` · 有效预算 ${plan.budget} tokens` : '';
    let sumNote = '';
    if (plan.summary_used) {
      sumNote = ` [较早摘要: 生效 (省 ${plan.tokens_saved || 0} tokens)]`;
    } else if (plan.summary_active) {
      sumNote = ` [较早摘要: 已启用但未生效 (${plan.fallback_reason || '无空间节省'})]`;
    }
    el.textContent = `本次带入最近 ${turns} 轮${tokens}${budgetNote}${sumNote} · 回复上限 ${maxTokens} tokens`;
    return;
  }

  // Pre-send estimation. When the actual window is unknown we say so rather
  // than subtracting from an assumed 4096.
  const winNote = modelWin ? '' : ' · 实际窗口未知';
  if (cfg.history_mode === 'current_only') {
    el.textContent = `仅当前消息 · 回复上限 ${maxTokens} tokens${winNote}`;
  } else if (cfg.history_mode === 'custom') {
    const customB = cfg.custom_budget || 2048;
    if (maxAvail != null && customB > maxAvail) {
      el.textContent = `自定义预算 ${customB} (受限于模型窗口生效 ${maxAvail}) tokens · 回复上限 ${maxTokens} tokens`;
    } else if (maxAvail == null) {
      el.textContent = `自定义预算 ${customB} tokens · 回复上限 ${maxTokens} tokens${winNote}`;
    } else {
      el.textContent = `自定义预算 ${customB} tokens · 回复上限 ${maxTokens} tokens`;
    }
  } else {
    // Auto
    const effMsgs = state.boundarySeq > 0
      ? state.messages.filter((m) => (m.seq || 0) > state.boundarySeq)
      : state.messages;
    const turns = Math.floor(effMsgs.filter((m) => m.role === 'user').length);
    const sumActive = state.currentSummary && state.currentSummary.is_active && state.currentSummary.status === 'ready';
    const sumNote = sumActive ? ' · 较早摘要已启用' : '';
    el.textContent = `预估带入对话 ${turns} 轮${sumNote} · 回复上限 ${maxTokens} tokens${winNote}`;
  }
}

/* ------------------------------------------------------------------ Send */
function drawBusy(active, slot) {
  state.busy = !!active;
  if ($('btn-send')) $('btn-send').disabled = state.busy;
  if ($('btn-stop')) $('btn-stop').hidden = !state.busy;
  const waiting = (slot && slot.chat_waiting) || (active && active.waiting_for_slot);
  const extracting = slot && slot.state === 'extracting';
  const cancelling = slot && slot.state === 'cancelling';
  const genStateEl = $('gen-state');
  if (!genStateEl) return;

  if (!state.busy) {
    genStateEl.textContent = extracting ? '后台记忆整理中…' : '空闲';
  } else if (waiting) {
    genStateEl.textContent = '等待后台记忆释放模型…';
  } else if (cancelling) {
    genStateEl.textContent = '正在停止后台请求…';
  } else if (active && active.stop_requested) {
    genStateEl.textContent = '正在停止…';
  } else {
    genStateEl.textContent = '正在生成…';
  }
}

function setNotice(text, isError) {
  setNoticeWithActions(text, isError, []);
}

/**
 * Set a *progress* notice (搜索中 / 已找到资料 / 正在生成).
 *
 * Round 40: progress notices are owned by the attempt that produced them, so a
 * later terminal event can clear the one it owns and nothing else. Without this
 * ownership the page kept saying "正在生成回答…" after the answer had arrived.
 */
function setProgressNotice(sid, requestId, text) {
  state.notice = text || '';
  const n = $('notice');
  if (n) {
    if (!text) { n.hidden = true; n.innerHTML = ''; }
    else {
      n.hidden = false;
      n.className = 'notice';
      n.innerHTML = `<span class="notice-text">${esc(text)}</span>`;
    }
  }
  state.progressNotice = { sid: sid || null, requestId: requestId || null,
                           text: text || '', at: Date.now() };
}

/**
 * Clear the progress notice if (and only if) this attempt still owns it.
 *
 * Terminal notices (失败 / 已停止 / 容量拒绝) are NOT progress notices, so they
 * are never cleared here. A stale attempt cannot clear a newer attempt's
 * progress notice because the owner's request_id will not match.
 */
function clearProgressNoticeIfOwned(sid, requestId) {
  const p = state.progressNotice;
  if (!p) return false;
  if (sid && p.sid && p.sid !== sid) return false;
  if (requestId && p.requestId && p.requestId !== requestId) return false;
  state.progressNotice = null;
  setNotice('');
  return true;
}

function setNoticeWithActions(text, isError, actions) {
  // Any notice written through this choke point supersedes progress tracking:
  // only setProgressNotice re-establishes ownership afterwards.
  state.progressNotice = null;
  state.notice = text || '';
  const n = $('notice');
  if (!n) return;
  if (!text) { n.hidden = true; n.innerHTML = ''; return; }
  n.hidden = false;
  n.className = 'notice' + (isError ? ' error' : '');

  let actionsHtml = '';
  if (actions && actions.length) {
    actionsHtml = `<div class="notice-actions">` + actions.map((a, i) =>
      `<button class="notice-action-btn ${a.className || ''}" data-action-idx="${i}">${esc(a.label || a.text || '重试')}</button>`
    ).join('') + `</div>`;
  }

  n.innerHTML = `<span class="notice-text">${esc(text)}</span>${actionsHtml}`;

  if (actions && actions.length) {
    n.querySelectorAll('[data-action-idx]').forEach(btn => {
      const idx = parseInt(btn.dataset.actionIdx, 10);
      btn.onclick = () => {
        if (actions[idx] && actions[idx].onClick) {
          actions[idx].onClick();
        }
      };
    });
  }
}

async function retryFailedWebSearch(targetSid, text, userMsgId) {
  if (!targetSid || !text) return;
  if (state.busy) { toast('还在生成中，请先停止'); return; }

  setNotice('🌐 正在重新检索并组织回答…', false);
  drawBusy({ session_id: targetSid });

  try {
    const out = await post('/api/chat/send', {
      session_id: targetSid,
      text: text,
      web_search: true,
      retry_user_msg_id: userMsgId || null,
    });

    if (!out.ok) {
      drawBusy(null);
      setNotice(out.message || '重试失败', true);
      return;
    }

    // Update pending prompt tracking
    state.sessionPendingPrompt[targetSid] = {
      sessionId: targetSid,
      text: text,
      userMessageId: out.user_message_id,
      requestId: out.request_id,
      webSearch: true,
      time: Date.now()
    };
    state.activeRequestBySession[targetSid] = out.request_id;
    // The old attempt is superseded; its failure note must not fire again.
    delete state.failedSearchBySession[targetSid];

    if (state.sessionId === targetSid) {
      if ($('input') && $('input').value === text) {
        $('input').value = '';
        delete state.drafts[targetSid];
        adjustInputHeight();
      }
      await refreshCurrent();
      connectStream();
    }
  } catch (err) {
    drawBusy(null);
    setNotice(`重试请求异常: ${err.message || err}`, true);
  }
}

/* -------------------------------------------------- Search failure recovery */
/**
 * Human-readable explanation per failure kind.
 *
 * Round 39: the acceptance list covers more than a network failure, so the
 * notice has to say WHY. A budget refusal is not a network fault and must not
 * tell the user to "check the connection".
 */
function searchFailureReasonZh(errorKind, fallbackText) {
  switch (errorKind) {
    case 'no_search_results':
      return '检索服务可用，但没有返回可用的搜索资料';
    case 'search_budget_exceeded':
      return '已获得搜索资料，但当前上下文窗口装不下这些参考资料';
    case 'search_queue_full':
      return '同时进行的联网搜索太多，本次没有发起检索';
    case 'search_shutting_down':
      return '应用正在退出，本次没有发起检索';
    case 'search_failed':
      return fallbackText || '检索请求未成功';
    case 'search_needs_subject':
      return '这条消息需要联网，但没能确定要检索的具体对象';
    case 'web_search_disabled':
      return '当前未联网（联网开关是关闭的），这条消息没有检索网络';
    case 'query_too_long':
      return fallbackText || '要检索的查询过长，本次没有发起检索';
    default:
      return fallbackText || errorKind || '搜索未成功';
  }
}

/**
 * The last turn of the session is a user message with no assistant reply.
 *
 * That is what a failed search leaves behind: the user row is stored (so the
 * question is not lost) but the assistant placeholder was dropped. Returning it
 * lets a later visit to the session recover the question, which is what the
 * "switch away during search" case requires.
 */
function findUnansweredUserMessage(messages) {
  if (!Array.isArray(messages) || !messages.length) return null;
  const last = messages[messages.length - 1];
  if (!last || last.role !== 'user') return null;
  return last;
}

/**
 * Record a failed search for a session without touching the UI.
 *
 * Used when the failure arrives for a session that is not on screen (or for an
 * attempt this page is no longer tracking). The question stays recoverable, but
 * nothing is written into the composer of whatever session IS on screen.
 *
 * Round 40: the record uses THIS event's own query / user_message_id. The
 * session's pending snapshot is only consulted when it demonstrably belongs to
 * the same attempt; otherwise a stale failure would be recorded with the text of
 * a newer question the user has since sent, which would then be "restored" as if
 * the user had asked it.
 */
function rememberFailedSearch(sid, d) {
  if (!sid) return;
  const pending = state.sessionPendingPrompt[sid];
  const pendingIsThisAttempt = Boolean(
    pending && d.request_id && pending.requestId === d.request_id);
  const text = (pendingIsThisAttempt && pending.text) || d.query || '';
  if (!text) return;
  state.failedSearchBySession[sid] = {
    sessionId: sid,
    text: text,
    userMessageId: (pendingIsThisAttempt && pending.userMessageId)
      || d.user_message_id || null,
    requestId: d.request_id || null,
    error: d.error || d.error_kind || '搜索未成功',
    errorKind: d.error_kind || 'search_failed',
    reasonZh: searchFailureReasonZh(d.error_kind, d.error),
  };
  // Keep the question reachable through the per-session draft, but only if the
  // user has no newer unsent text of their own for that session.
  if (!state.drafts[sid] || !state.drafts[sid].trim()) {
    state.drafts[sid] = text;
  }
}

/**
 * Search failed on the session the user is looking at: restore, or offer retry.
 *
 * Three cases, exactly as the requirement lists them:
 *   A composer empty, no newer draft  -> restore the question, offer retry
 *   B composer has new text           -> keep it, offer retry / fill-in
 *   C session switched away           -> never write into the new session
 */
function handleWebSearchFailure(sid, d) {
  // Round 54: "the switch was off" is NOT a search failure -- nothing was
  // attempted, so there is no failed search to restore or retry. Show the plain
  // explanation and leave the composer alone.
  if (d.error_kind === 'web_search_disabled') {
    setNotice(searchFailureReasonZh('web_search_disabled', d.error), false);
    return;
  }
  // Round 54: "needs a subject" is also not a transport failure. The question was
  // preserved by the server; just explain what was missing.
  if (d.error_kind === 'search_needs_subject') {
    rememberFailedSearch(sid, d);
    setNotice(searchFailureReasonZh('search_needs_subject', d.error), true);
    return;
  }
  rememberFailedSearch(sid, d);
  const rec = state.failedSearchBySession[sid] || {};
  const failedQuery = rec.text || d.query || '';
  const failedUserMsgId = rec.userMessageId || d.user_message_id || null;
  const reason = searchFailureReasonZh(d.error_kind, d.error);

  if (!failedQuery) {
    setNotice(`🌐 搜索未成功（${reason}）。`, true);
    return;
  }

  // Case C: the failure belongs to another session. It was already stored for
  // that session above; the composer here must stay untouched.
  if (state.sessionId !== sid) return;

  const inputEl = $('input');
  const curInput = inputEl ? inputEl.value : '';
  const curDraft = state.drafts[sid] || '';
  const hasNewContent = Boolean(curInput.trim()) || Boolean(curDraft.trim() && curDraft !== failedQuery);

  if (!hasNewContent) {
    // Case A
    if (inputEl) {
      inputEl.value = failedQuery;
      state.drafts[sid] = failedQuery;
      adjustInputHeight();
      inputEl.focus();
    }
    setNoticeWithActions(
      `🌐 搜索未成功（${reason}）。已恢复原问题到输入框，可修改后重试或直接重新发送。`,
      true,
      [{ label: '重试搜索', onClick: () => retryFailedWebSearch(sid, failedQuery, failedUserMsgId) }]
    );
    return;
  }

  // Case B: never overwrite what the user typed.
  setNoticeWithActions(
    `🌐 搜索未成功（${reason}）。输入框已有新内容，原问题未自动覆盖。`,
    true,
    [
      { label: '重试原问题', onClick: () => retryFailedWebSearch(sid, failedQuery, failedUserMsgId) },
      {
        label: '填回输入框', className: 'secondary', onClick: () => {
          const el = $('input');
          if (el) {
            el.value = failedQuery;
            state.drafts[sid] = failedQuery;
            adjustInputHeight();
            el.focus();
          }
        }
      }
    ]
  );
}

function newClientId() {
  const rnd = (crypto.randomUUID ? crypto.randomUUID()
                                 : Math.random().toString(36).slice(2) + Date.now());
  return 'u-' + rnd.replace(/-/g, '').slice(0, 24);
}

function adjustInputHeight() {
  const input = $('input');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(Math.max(input.scrollHeight, 42), 180) + 'px';
}

async function send() {
  const input = $('input');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  if (!state.sessionId) { await newSession(); if (!state.sessionId) return; }
  if (state.busy) { toast('还在生成中，请先停止'); return; }

  // Prevent duplicate send dispatch while waiting for settings or network
  if (state.sending) return;
  state.sending = true;

  const targetSid = state.sessionId;

  try {
    // 1. Wait for complete settings save queue for targetSid
    // Continue waiting if newer settings saves were queued during wait
    while (state.pendingSettingsPromise[targetSid] ||
           ((state.settingsOpSeq[targetSid] || 0) !== (state.settingsSettledSeq[targetSid] || 0))) {
      const curPromise = state.pendingSettingsPromise[targetSid];
      if (!curPromise) {
        // Yield to microtask queue in case an op was registered and promise is attaching
        await new Promise(r => setTimeout(r, 0));
        if (!state.pendingSettingsPromise[targetSid]) break;
        continue;
      }

      setNotice('正在等待设置保存完成…');
      let saveRes;
      try {
        saveRes = await curPromise;
      } catch (e) {
        saveRes = { ok: false, error: 'exception', message: e.message || '等待设置保存时发生异常' };
      }

      if (!saveRes || !saveRes.ok || state.settingsSaveError[targetSid]) {
        const err = state.settingsSaveError[targetSid] || saveRes;
        const reason = (err && err.message) || '设置保存未成功';
        if (state.sessionId === targetSid) {
          setNotice(`设置保存失败（${reason}）。已阻止发送以保证配置一致。请重试或恢复已保存设置。`, true);
          toast('设置未保存，发送已阻止');
        }
        return; // Halt: 0 chat requests sent, input & draft preserved!
      }

      // Check if user navigated away during wait
      if (state.sessionId !== targetSid) {
        return;
      }
    }

    // 2. Comprehensive pre-dispatch verification
    if (state.sessionId !== targetSid) return;
    if (state.pendingSettingsPromise[targetSid] ||
        ((state.settingsOpSeq[targetSid] || 0) !== (state.settingsSettledSeq[targetSid] || 0))) {
      return;
    }
    if (state.settingsSaveError[targetSid]) {
      const err = state.settingsSaveError[targetSid];
      if (state.sessionId === targetSid) {
        setNotice(`当前设置保存未成功（${err.message || '未保存'}）。已阻止发送以保证配置一致。请重试或恢复已保存设置。`, true);
        toast('设置未保存，发送已阻止');
      }
      return;
    }
    if (state.busy) {
      toast('还在生成中，请先停止');
      return;
    }

    setNotice('');
    const cid = newClientId();
    const webSearchSnapshot = Boolean(state.sessionSettings.web_search);
    // A new send is a new UI operation: any generation_end handler still waiting
    // on a network call belongs to an earlier question and must stand down.
    state.epoch++;

    // Save prompt snapshot for input recovery if search fails or generation refuses.
    // Keyed by session so a failure in session A can never touch session B.
    state.lastSentSnapshot = {
      sessionId: targetSid,
      text: text,
      clientMsgId: cid,
      webSearch: webSearchSnapshot,
      time: Date.now()
    };
    state.sessionPendingPrompt[targetSid] = Object.assign({}, state.lastSentSnapshot);

    const out = await post('/api/chat/send', {
      session_id: targetSid, text, client_msg_id: cid, web_search: webSearchSnapshot,
    });

    if (!out.ok) {
      if (out.error === 'model_busy') {
        setNotice(out.message || '模型正忙，请稍候再发。', true);
        toast('模型正忙，请稍候再发');
        return;
      }
      setNotice(out.message || '发送失败', true);
      if (out.kept_in_composer) { drawBusy(null); return; }
      if (state.sessionId === targetSid) {
        await refreshCurrent();
      }
      drawBusy(null);
      return;
    }

    // Attach server assigned IDs to pending prompt
    state.lastSentSnapshot.userMessageId = out.user_message_id;
    state.lastSentSnapshot.requestId = out.request_id;
    if (state.sessionPendingPrompt[targetSid]) {
      state.sessionPendingPrompt[targetSid].userMessageId = out.user_message_id;
      state.sessionPendingPrompt[targetSid].requestId = out.request_id;
    }
    state.activeRequestBySession[targetSid] = out.request_id;

    // 4. Success: clear draft for targetSid
    delete state.drafts[targetSid];

    // Only clear input and connect stream if user is STILL in targetSid!
    if (state.sessionId === targetSid) {
      input.value = '';
      adjustInputHeight();
      state.messages = state.messages.filter((m) => m.id !== out.user_message_id);
      await refreshCurrent();
      drawBusy({session_id: targetSid});
      if (out.duplicate) { toast('这条消息已经回答过了'); return; }
      connectStream();
    } else {
      // User already switched to session B! Do not touch input of B, do not refresh B as A!
      drawBusy({session_id: targetSid});
    }
  } finally {
    state.sending = false;
  }
}

async function refreshCurrent() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  // Round 39: capture the epoch AND the tracked request before awaiting. A
  // refresh that was issued for an older attempt must not paint busy state over
  // a newer attempt that has started (or over a session the user has left).
  const startedEpoch = state.epoch;
  const startedReq = state.activeRequestBySession[targetSid] || null;
  const out = await api(`/api/chat/session?id=${encodeURIComponent(targetSid)}`);
  // If user switched away while request was in-flight, drop!
  if (!out.ok || state.sessionId !== targetSid) return;
  // If a newer attempt for this session began while we were waiting, the busy
  // state in this response describes the OLD attempt. Keep the message list
  // (it is the same session, so it is still authoritative) but do not let it
  // clear or re-open the busy state of the attempt that is now live.
  const superseded = (state.epoch !== startedEpoch) ||
    (startedReq && state.activeRequestBySession[targetSid] &&
     state.activeRequestBySession[targetSid] !== startedReq);
  state.messages = out.messages || [];
  state.boundaryMsgId = out.session.boundary_msg_id || null;
  state.boundarySeq = out.session.boundary_seq || 0;
  $('session-title').textContent = out.session.title;
  $('session-meta').textContent =
    `${out.session.message_count} 条消息 · 创建于 ${timeLabel(out.session.created_at)}`;
  renderMessages();
  if (!superseded) {
    drawBusy(out.active, out.model_slot);
  }
  if (out.session.note) showBanner(out.session.note);
}

/**
 * Round 41: load the backend's capability snapshot and repaint every dependent
 * control. Called on boot, after a settings save and after a model reload, so
 * the page never keeps computing with a stale window.
 */
async function loadCapabilities() {
  const targetSid = state.sessionId;
  const nav = state.navToken;
  const settingsRevision = state.sessionSettings && state.sessionSettings._seq;
  const instanceSeq = state.modelInstanceSeq;
  const requestSeq = ++state.capabilitiesRequestSeq;
  const stillCurrent = () => requestSeq === state.capabilitiesRequestSeq
    && targetSid === state.sessionId && nav === state.navToken
    && settingsRevision === (state.sessionSettings && state.sessionSettings._seq)
    && instanceSeq === state.modelInstanceSeq;
  let cap;
  try {
    const sid = targetSid ? `?session_id=${encodeURIComponent(targetSid)}` : '';
    cap = await api(`/api/chat/capabilities${sid}`);
  } catch (e) {
    if (stillCurrent()) {
      state.capabilities = null;
      state.modelActualWindow = null;
      renderCapabilities();
      syncCustomBudgetLimits();
      updateContextIndicator();
    }
    return null;
  }
  if (!stillCurrent()) return null;
  if (!cap || cap.ok === false) {
    state.capabilities = null;
    state.modelActualWindow = null;
    renderCapabilities();
    syncCustomBudgetLimits();
    updateContextIndicator();
    return null;
  }
  state.capabilities = cap;
  state.modelActualWindow = cap.window && typeof cap.window.actual === 'number'
    ? cap.window.actual : null;
  renderCapabilities();
  syncCustomBudgetLimits();
  updateContextIndicator();
  return cap;
}

/** Paint the capability facts, each with an honest source label. */
function renderCapabilities() {
  const cap = state.capabilities;
  if (!cap) {
    for (const id of ['disp-actual-window', 'model-actual-window', 'disp-reply-reserve', 'disp-input-budget', 'disp-declared-window']) {
      if ($(id)) $(id).textContent = '未知（未取得最新能力信息）';
    }
    return;
  }
  const w = cap.window || {};

  // Actual (measured) window — never a fallback number.
  const actualTxt = (typeof w.actual === 'number') ? `${w.actual} tokens` : '未知';
  if ($('disp-actual-window')) $('disp-actual-window').textContent = actualTxt;
  if ($('model-actual-window')) $('model-actual-window').textContent = actualTxt;
  if ($('model-alias')) {
    $('model-alias').textContent = (cap.model && cap.model.display_name)
      || (cap.model && cap.model.alias) || '未知模型';
  }

  // Declared (model metadata) window — a claim about the model, not this box.
  if ($('disp-declared-window')) {
    const d = w.declared;
    const src = w.declared_source || 'unknown';
    if (typeof d === 'number') {
      const suffix = src === 'deployment-config' ? '（配置声明）' : '（模型元数据）';
      $('disp-declared-window').textContent = `${d} tokens${suffix}`;
    } else {
      $('disp-declared-window').textContent = '未知（未取得）';
    }
  }
  if ($('disp-reply-reserve')) {
    const r = cap.reply || {};
    $('disp-reply-reserve').textContent =
      (typeof r.max_output === 'number') ? `${r.max_output} tokens` : '—';
  }
  if ($('disp-input-budget')) {
    const i = cap.input || {};
    $('disp-input-budget').textContent =
      (typeof i.auto_budget === 'number') ? `${i.auto_budget} tokens` : '未知';
  }
  if ($('disp-target-window')) {
    $('disp-target-window').textContent =
      (typeof state.modelTargetWindow === 'number') ? `${state.modelTargetWindow} tokens` : '未选择';
  }

  // Window limit hint derives from the shared band, not a literal in the page.
  if ($('hint-window-limits')) {
    const step = w.step || 256;
    $('hint-window-limits').textContent =
      `目标值需重新加载模型才生效；可填写 ${w.min || 512}~${w.max || 131072} 之间 ${step} 的整数倍`
      + '（例如 3072）。是否真正加载取决于本机显存，本应用不会自动探测显存极限。';
  }
  if ($('input-custom-window')) {
    $('input-custom-window').min = String(w.min || 512);
    $('input-custom-window').max = String(w.max || 131072);
    $('input-custom-window').step = String(w.step || 256);
  }
}

function showBanner(text) {
  state.banner = text || '';
  const b = $('banner');
  if (!b) return;
  if (!text) { b.hidden = true; return; }
  b.hidden = false;
  b.textContent = text;
}

async function stopGen() {
  const targetSid = state.sessionId;
  if (!targetSid) return;
  // Bump first: any refresh/send continuation still waiting for the network is
  // now answering a pre-stop question and must not repaint the busy state.
  state.epoch++;
  const out = await post('/api/chat/stop', {session_id: targetSid});
  if (!out.ok) {
    // Round 40: the backend saying "nothing to stop" means the attempt already
    // ended on its own. The page may still be showing itself as busy because the
    // finishing `generation_end` raced this click. Leaving the page stuck in
    // "正在生成…" would be the visible bug, so settle the UI and re-sync.
    toast(out.message || '没有正在生成的回复');
    drawBusy(null);
    if (state.sessionId === targetSid) {
      await refreshCurrent();
    }
    return;
  }
  toast(out.message);
  if ($('gen-state')) $('gen-state').textContent = '已停止';
  setNotice('已停止生成/搜索。', false);
  drawBusy(null);
}

/* ------------------------------------------------------------------- SSE */
function closeStream() {
  if (state.es) { try { state.es.close(); } catch (e) { /* ignore */ } state.es = null; }
  state.esSid = null;
}

function connectStream() {
  closeStream();
  const streamSid = state.sessionId;
  if (!streamSid) return;
  state.esSid = streamSid;

  const es = new EventSource(`/api/chat/stream?session_id=${encodeURIComponent(streamSid)}`);
  state.es = es;

  es.addEventListener('hello', (ev) => {
    const d = JSON.parse(ev.data);
    if (!d.active || d.active.session_id !== state.sessionId) {
      closeStream();
      refreshCurrent();
    }
  });

  es.addEventListener('delta', (ev) => {
    const d = JSON.parse(ev.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    appendDelta(d.message_id, d.text);
  });

  es.addEventListener('message_reset', (ev) => {
    const d = JSON.parse(ev.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    const box = $('messages');
    if (!box) return;
    const row = d.message_id ? box.querySelector(`[data-mid="${d.message_id}"]`) : null;
    if (row) {
      const bubble = row.querySelector('.bubble');
      if (bubble) {
        bubble.dataset.raw = '';
        bubble.dataset.started = '1';
        bubble.textContent = '';
      }
    }
  });

  es.addEventListener('stop_ack', () => {
    if ($('gen-state')) $('gen-state').textContent = '已停止';
    setNotice('已停止生成/搜索。', false);
    drawBusy(null);
  });

  es.addEventListener('search_phase', (ev) => {
    const d = JSON.parse(ev.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    // Round 40: a phase event belongs to ONE attempt. If this page is already
    // tracking a different request for the session, the phase is stale and must
    // not overwrite the newer attempt's progress line.
    const evSid = d.session_id || streamSid;
    const evReq = d.request_id || null;
    const tracked = evSid ? state.activeRequestBySession[evSid] : null;
    const stale = Boolean(evReq && tracked && evReq !== tracked);
    if ($('gen-state') && !stale) {
      $('gen-state').textContent = d.message || d.phase;
    }
    if (d.phase === 'searching') {
      if (!stale) setProgressNotice(evSid, evReq, '🌐 正在搜索互联网资料…');
    } else if (d.phase === 'found') {
      if (!stale) {
        setProgressNotice(evSid, evReq,
          `🌐 已找到 ${d.sources ? d.sources.length : ''} 条可用资料，正在组织回答…`);
      }
    } else if (d.phase === 'discarded') {
      // Round 54: results came back but none can support the question. Say so --
      // this must never look like a successful web answer.
      if (!stale) setNotice(d.message || '检索到的资料与问题无关，已舍弃；将直接回答', false);
    } else if (d.phase === 'not_allowed') {
      // Round 54: the user asked to search while the switch is off.
      if (!stale) setNotice(d.message || '当前未联网，这条消息不会检索网络', false);
    } else if (d.phase === 'answering') {
      if (!stale) {
        setProgressNotice(evSid, evReq, '🌐 已加载搜索摘要，本地模型正在生成回答…');
      }
    } else if (d.phase === 'failed') {
      // A failure notice is terminal, not progress: it must survive.
      if (!stale) setNotice(d.message || '未获得可用搜索资料', true);
    } else if (d.phase === 'gap_fill') {
      if (!stale) setProgressNotice(evSid, evReq, d.message || '🌐 正在补查资料…');
      const box = $('messages');
      if (box) {
        const row = d.message_id ? box.querySelector(`[data-mid="${d.message_id}"]`) : null;
        if (row) {
          const bubble = row.querySelector('.bubble');
          if (bubble) {
            bubble.dataset.raw = '';
            bubble.dataset.started = '1';
            bubble.textContent = '';
          }
        }
      }
    } else if (d.phase === 'gap_fill_failed') {
      if (!stale) setNotice(d.message || '补查未取得可用资料', false);
    }
  });

  es.addEventListener('generation_end', async (ev) => {
    const d = JSON.parse(ev.data);

    // ---- Identity gate (round 39, strengthened in round 40) --------------
    // Events carry a flat `session_id` / `request_id` (see ChatService._emit_end).
    // An event must only be allowed to touch state that still belongs to it.
    const evSid = d.session_id || streamSid;
    const evReq = d.request_id || null;
    // Capture the UI generation THIS handler observed on entry. Any await may
    // let another operation (a new send, a stop, a session switch) run; after
    // returning we compare against this value to detect that.
    const entryEpoch = state.epoch;
    const entrySid = state.sessionId;

    const stillMine = () => {
      // Re-evaluated after EVERY await. Three independent facts matter:
      //  1. the page still shows the session this event belongs to;
      //  2. no newer UI operation happened (`epoch`);
      //  3. the per-session tracked request is still this event's request.
      if (evSid && state.sessionId !== evSid) return false;
      if (state.epoch !== entryEpoch) return false;
      const tracked = evSid ? state.activeRequestBySession[evSid] : null;
      if (evReq && tracked && evReq !== tracked) return false;
      return true;
    };

    const trackedReq = evSid ? state.activeRequestBySession[evSid] : null;
    const isStaleAttempt = Boolean(evReq && trackedReq && evReq !== trackedReq);
    const isForeignSession = Boolean(evSid && state.sessionId && evSid !== state.sessionId);

    if (isStaleAttempt || isForeignSession) {
      // Record the failure for its OWN session so the question is recoverable,
      // but do not touch the composer, the notice, the stream or busy state.
      if (d.web_search_failed) {
        rememberFailedSearch(evSid, d);
      }
      return;
    }

    // Closing the stream is safe here: the stream belongs to `streamSid`, and we
    // have just established the event belongs to the session on screen. A stale
    // event never reaches this line.
    closeStream();

    // ---- First await -----------------------------------------------------
    await refreshCurrent();
    if (!stillMine()) {
      // The world moved while we were fetching. Do not touch busy, the notice,
      // the context indicator, or the tracked request of the live attempt.
      if (d.web_search_failed) rememberFailedSearch(evSid, d);
      return;
    }

    drawBusy(null);

    // The attempt just finished: its own progress line is now false ("正在生成
    // 回答…"), so clear it. Terminal notices written below replace it anyway.
    clearProgressNoticeIfOwned(evSid, evReq);

    if (d.web_search_failed) {
      const sid = d.session_id || state.sessionId;
      handleWebSearchFailure(sid, d);
    } else if (d.status === 'refused') {
      setNotice(d.error || '这条消息没有发送。', true);
    } else if (d.status === 'interrupted') {
      setNotice(d.error || '已停止生成。已收到的部分已保留。', false);
    } else if (d.finish_reason === 'length') {
      setNotice(`已达到回复上限（${d.max_output || 512} tokens），回答已截断。`, false);
    } else if (d.error) {
      setNotice(d.error, true);
    }
    if (d.plan) {
      showBanner(d.plan);
    }
    updateContextIndicator(d);

    // Only the attempt that still owns the slot may retire its tracking entry.
    if (evSid && (!evReq || state.activeRequestBySession[evSid] === evReq)) {
      delete state.activeRequestBySession[evSid];
    }

    // ---- More awaits -----------------------------------------------------
    await loadSessions();
    await loadMemories();
    // These two only refresh lists, but they must not repaint the header/notice
    // of a session the user has since switched to, so we re-check before any
    // further UI write. (No further writes here today; the guard documents the
    // requirement and protects future edits in this chain.)
    if (!stillMine()) return;
  });

  es.addEventListener('summary_generated', async (ev) => {
    const d = JSON.parse(ev.data);
    if (!d.session_id || d.session_id === state.sessionId) {
      toast(`已生成对话摘要（节省约 ${d.tokens_saved || 0} tokens）`);
      if ($('compress-modal') && !$('compress-modal').hidden) {
        await loadCompressModalData();
      }
    }
  });

  es.addEventListener('summary_error', async (ev) => {
    const d = JSON.parse(ev.data);
    if (!d.session_id || d.session_id === state.sessionId) {
      toast(`对话摘要未成功：${d.error || '未知错误'}`);
      if ($('compress-modal') && !$('compress-modal').hidden) {
        await loadCompressModalData();
      }
    }
  });

  es.onerror = () => {
    closeStream();
    if (state.sessionId === streamSid) {
      refreshCurrent();
    }
  };
}

function appendDelta(mid, piece) {
  const box = $('messages');
  if (!box) return;
  let row = box.querySelector(`[data-mid="${mid}"]`);
  if (!row) {
    const empty = $('empty-state');
    if (empty) empty.remove();
    row = messageNode({id: mid, role: 'assistant', content: '', status: 'streaming',
                       created_at: new Date().toISOString()});
    box.appendChild(row);
  }
  const bubble = row.querySelector('.bubble');
  if (!bubble.dataset.started) {
    bubble.dataset.started = '1';
    bubble.dataset.raw = '';
  }
  bubble.dataset.raw += piece;
  bubble.classList.add('caret');
  bubble.textContent = bubble.dataset.raw;
  const tag = row.querySelector('.state-tag');
  if (!tag) {
    const meta = row.querySelector('.msg-meta');
    meta.innerHTML = `<span>${timeLabel(new Date().toISOString())}</span>` +
      '<span class="state-tag streaming">生成中</span>';
  }
  scrollBottom();
}

/* -------------------------------------------------------------- Memories */
async function loadMemories() {
  const out = await api('/api/chat/memories');
  state.memories = out.memories || [];
  const effective = state.memories.filter(
    (m) => m.enabled && m.deleted_at == null && m.strength === 'explicit');
  const total = state.memories.filter((m) => m.deleted_at == null).length;
  const badge = $('memory-count');
  if (badge) {
    badge.textContent = String(effective.length);
    badge.title = `生效 ${effective.length} 条 / 共 ${total} 条（暂定与已停用的不会注入聊天）`;
  }
  renderMemories();
}

const KIND_LABEL = {preference: '偏好', address: '称呼', background: '背景'};

function renderMemories() {
  const box = $('memory-list');
  if (!box) return;
  box.innerHTML = '';
  if (!state.memories.length) {
    box.innerHTML = '<div class="mem-empty">还没有记住任何信息。<br>' +
      '试着说「以后回答简短一点」或「请叫我小王」。</div>';
    return;
  }
  for (const m of state.memories) {
    const el = document.createElement('div');
    const cls = ['mem'];
    if (!m.enabled) cls.push('off');
    if (m.strength === 'tentative') cls.push('tentative');
    el.className = cls.join(' ');
    const kindCls = m.kind === 'background' ? 'background'
                  : (m.kind === 'address' ? 'address' : '');
    const strengthTag = m.strength === 'tentative'
      ? '<span class="mem-tag tentative">暂定 · 不注入</span>'
      : '<span class="mem-tag">明确 · 生效</span>';
    const offTag = m.enabled ? '' : '<span class="mem-tag">已停用</span>';
    const origin = m.origin === 'model' ? '模型提取' : (m.origin === 'user' ? '手动修改' : '规则提取');
    el.innerHTML = `
      <div class="mem-top">
        <span class="mem-kind ${kindCls}">${KIND_LABEL[m.kind] || esc(m.kind)}</span>
        ${strengthTag}${offTag}
      </div>
      <div class="mem-content">${esc(m.content)}</div>
      <div class="mem-quote">原文：${esc(m.evidence_quote || '')}</div>
      <details class="mem-details">
        <summary>技术来源与记录信息</summary>
        <div class="mem-details-body">
          来源类型：${esc(origin)}<br>
          建立时间：${timeLabel(m.created_at)}<br>
          更新时间：${timeLabel(m.updated_at)}
          ${m.source_session_title ? `<br>来源会话：${esc(m.source_session_title)}` : ''}
        </div>
      </details>
      <div class="mem-actions">
        ${m.strength === 'tentative' ? '<button data-act="confirm" class="btn-confirm">确认并启用</button>' : ''}
        <button data-act="edit">修改</button>
        <button data-act="toggle">${m.enabled ? '停用' : '启用'}</button>
        <button data-act="del" class="del">删除</button>
      </div>`;
    const confirmBtn = el.querySelector('[data-act="confirm"]');
    if (confirmBtn) confirmBtn.onclick = () => confirmMemory(m);
    el.querySelector('[data-act="edit"]').onclick = () => editMemory(m);
    el.querySelector('[data-act="toggle"]').onclick = () => toggleMemory(m);
    el.querySelector('[data-act="del"]').onclick = () => delMemory(m);
    box.appendChild(el);
  }
}

async function confirmMemory(m) {
  const out = await post('/api/chat/memories/confirm', {id: m.id});
  if (!out.ok) { toast('操作失败'); return; }
  await loadMemories();
  toast('已确认并启用，后续聊天将按此偏好生效');
}

async function editMemory(m) {
  const v = prompt('修改这条记忆的内容：\n（改完后，聊天的偏好会立刻按新内容生效）', m.content);
  if (v == null) return;
  const out = await post('/api/chat/memories/update', {id: m.id, content: v});
  if (!out.ok) { toast(out.message || '修改失败'); return; }
  await loadMemories();
  toast('已更新');
}

async function toggleMemory(m) {
  const out = await post('/api/chat/memories/update', {id: m.id, enabled: !m.enabled});
  if (!out.ok) { toast('操作失败'); return; }
  await loadMemories();
  toast(m.enabled ? '已停用，不再注入聊天' : '已启用');
}

async function delMemory(m) {
  if (!await showInAppConfirm({ title: '删除记忆', message: '确定删除这条记忆？删除后不会从旧记录里自动恢复。', danger: true, confirmText: '删除' })) return;
  const out = await post('/api/chat/memories/delete', {id: m.id});
  if (!out.ok) { toast('删除失败'); return; }
  await loadMemories();
  toast('已删除');
}

/* ----------------------------------------------------------- Diagnostics */
async function loadDiagnostics() {
  const box = $('diag-content');
  if (!box) return;
  let out;
  try {
    out = await api('/api/chat/status');
  } catch (e) {
    box.innerHTML = `<span style="color:var(--danger)">状态读取失败：${esc(String(e))}</span>`;
    return;
  }
  const rc = out.request_counts || {};
  const rows = [
    ['模型连接', out.reachable ? '<span style="color:var(--ok)">已连接 (OK)</span>' : '<span style="color:var(--danger)">不可达</span>'],
    ['模型端点', `<span class="mono">${esc(out.base_url || '—')}</span>`],
    ['自报模型', `${esc(out.server_model || '—')}（别名 ${esc(out.alias || '—')}）`],
    ['实际物理上下文', `<span class="mono">${out.window || '—'} tokens (当前模型实际窗口，只读)</span>`],
    ['会话与消息数', `会话 ${(out.store || {}).sessions || 0} 个 · 消息 ${(out.store || {}).messages || 0} 条`],
    ['记忆统计', `生效 ${(out.store || {}).memories_enabled || 0} / 共 ${(out.store || {}).memories_live || 0} 条`],
    ['请求次数统计', `聊天 ${rc.chat || 0} · 提取 ${rc.extraction || 0} · 辅助 ${rc.aux || 0}`],
    ['含 tools 请求', `${rc.with_tools || 0}（纯聊天规范应为 0）`],
  ];
  if (out.error) {
    rows.push(['错误提示', `<span style="color:var(--danger)">${esc(out.error)}</span>`]);
  }
  const slot = out.model_slot || {};
  const slotLabel = {
    idle: '空闲', chat: '聊天生成中', extracting: '后台记忆整理中',
    cancelling: '正在停止后台请求',
  }[slot.state] || slot.state || '—';
  rows.push(['当前模型占用', slotLabel]);

  box.innerHTML = rows.map(([k, v]) => `
    <div class="diag-row">
      <span class="diag-k">${k}</span>
      <span class="diag-v">${v}</span>
    </div>`).join('');
}

/* ------------------------------------------------------------- Model Runtime (Round 32) */
async function loadModelStatus() {
  let data;
  try {
    data = await api('/api/chat/model/status');
  } catch (e) {
    console.error('Failed to load model status:', e);
    return;
  }
  if (!data) return;

  // Prevent stale delayed responses from overwriting newer state (Req VIII.6)
  const seq = data.instance_seq || 0;
  if (seq < state.modelInstanceSeq) {
    return;
  }
  state.modelInstanceSeq = seq;
  state.modelStatus = data;

  // Round 41: no `|| 4096` fallback. If the service has not told us its window,
  // the page must say "unknown" instead of assuming 4096 and showing success.
  const actualWin = (typeof data.actual_window === 'number' && data.actual_window > 0)
    ? data.actual_window : null;
  state.modelActualWindow = actualWin;
  updateContextIndicator();

  // If not in the middle of reloading, default target window to current actual window
  if (actualWin && !data.reloading
      && !['stopping', 'loading', 'restoring'].includes(data.status)) {
    if (!state.modelTargetWindow || state.modelTargetWindow === state.modelActualWindow) {
      state.modelTargetWindow = actualWin;
    }
  }

  // Render status fields. Round 43: nothing here falls back to a Qwen literal.
  // When nothing is loaded, the name is empty and the page says 未加载.
  const profileName = data.profile_display_name || '';
  const ready = data.status === 'ready';
  if ($('model-alias')) $('model-alias').textContent = data.alias || '未知模型';
  if ($('model-profile-name')) {
    $('model-profile-name').textContent = profileName || (ready ? '未记录配置' : '未加载');
  }
  if ($('model-profile-file')) $('model-profile-file').textContent = data.profile_file_name || '—';
  if ($('brand-model')) {
    $('brand-model').textContent = profileName
      ? `${profileName} · 本机离线`
      : (ready ? '本机离线' : '未加载模型');
  }

  if ($('model-ownership')) {
    if (data.owned) {
      $('model-ownership').innerHTML = '<span class="badge-tag owned">本应用启动</span>';
    } else {
      $('model-ownership').innerHTML = '<span class="badge-tag external">外部服务</span>';
    }
  }

  if ($('model-actual-window')) {
    $('model-actual-window').textContent = actualWin ? `${actualWin} tokens` : '未知';
  }
  if ($('disp-actual-window')) {
    $('disp-actual-window').textContent = actualWin ? `${actualWin} tokens` : '未知';
  }
  if ($('disp-target-window')) {
    $('disp-target-window').textContent = (typeof state.modelTargetWindow === 'number')
      ? `${state.modelTargetWindow} tokens` : '未选择';
  }

  if ($('model-status-badge')) {
    const statusMap = {
      ready: { label: '就绪', cls: 'status-ready' },
      stopping: { label: '正在停止', cls: 'status-stopping' },
      loading: { label: '正在加载', cls: 'status-loading' },
      restoring: { label: '正在恢复', cls: 'status-restoring' },
      unavailable: { label: '不可用', cls: 'status-unavailable' },
    };
    const st = statusMap[data.status] || { label: data.status || '未知', cls: '' };
    $('model-status-badge').innerHTML = `<span class="badge-tag ${st.cls}">${st.label}</span>`;
  }

  if ($('model-pid')) {
    $('model-pid').textContent = data.pid ? String(data.pid) : '—';
  }

  // Update window option cards (Round 41: presets + a custom entry)
  const targetWin = state.modelTargetWindow
    || actualWin || null;
  const isPreset = [2048, 4096].includes(targetWin);
  const r2048 = $('radio-window-2048');
  const r4096 = $('radio-window-4096');
  const rCustom = $('radio-window-custom');
  if (r2048) r2048.checked = (targetWin === 2048);
  if (r4096) r4096.checked = (targetWin === 4096);
  if (rCustom) rCustom.checked = Boolean(targetWin && !isPreset);
  if ($('opt-window-2048')) $('opt-window-2048').classList.toggle('selected', targetWin === 2048);
  if ($('opt-window-4096')) $('opt-window-4096').classList.toggle('selected', targetWin === 4096);
  if ($('opt-window-custom')) $('opt-window-custom').classList.toggle('selected', Boolean(targetWin && !isPreset));
  if ($('group-custom-window')) $('group-custom-window').hidden = isPreset;
  if ($('input-custom-window') && targetWin && !isPreset) {
    $('input-custom-window').value = String(targetWin);
  }

  // Banners
  const errBanner = $('model-error-banner');
  if (errBanner) {
    if (data.error) {
      errBanner.textContent = data.error;
      errBanner.hidden = false;
    } else {
      errBanner.hidden = true;
    }
  }

  const busyBanner = $('model-busy-banner');
  if (busyBanner) {
    if (data.busy) {
      busyBanner.textContent = `当前正在执行任务（${data.busy_reason}），请先等待完成或停止后再重新加载模型。`;
      busyBanner.hidden = false;
    } else {
      busyBanner.hidden = true;
    }
  }

  const extBanner = $('model-external-banner');
  if (extBanner) {
    if (!data.owned) {
      const aw = actualWin ? `当前实际窗口 ${actualWin} tokens。` : '当前实际窗口未知。';
      extBanner.textContent = `${aw}当前使用外部模型服务，本应用不会擅自重启它；`
        + '如需修改上下文长度，请在该服务的启动参数中设置 n_ctx。';
      extBanner.hidden = false;
    } else {
      extBanner.hidden = true;
    }
  }

  // Reload button and option inputs state
  const btnReload = $('btn-reload-model');
  const isReloading = data.reloading || ['stopping', 'loading', 'restoring'].includes(data.status);
  state.modelReloading = isReloading;

  if (r2048) r2048.disabled = !data.owned || isReloading;
  if (r4096) r4096.disabled = !data.owned || isReloading;
  if (rCustom) rCustom.disabled = !data.owned || isReloading;
  if ($('input-custom-window')) $('input-custom-window').disabled = !data.owned || isReloading;

  if (btnReload) {
    if (!data.owned) {
      btnReload.disabled = true;
      btnReload.textContent = '应用并重新加载模型';
      btnReload.title = '外部服务只读，需在其启动配置中调整';
    } else if (data.busy) {
      btnReload.disabled = true;
      btnReload.textContent = '应用并重新加载模型';
      btnReload.title = data.busy_reason || '任务进行中';
    } else if (isReloading) {
      btnReload.disabled = true;
      btnReload.textContent = '正在重新加载模型…';
      btnReload.title = '正在重新加载中，请稍候';
    } else if (data.status === 'unavailable') {
      btnReload.disabled = false;
      btnReload.textContent = '重试重新加载模型';
      btnReload.title = '模型不可用，点击重新加载并恢复运行';
    } else {
      btnReload.disabled = false;
      btnReload.textContent = '应用并重新加载模型';
      btnReload.title = '点击重新加载模型';
    }
  }

  // Poll if reloading until settled
  if (isReloading) {
    setTimeout(loadModelStatus, 800);
  }
}

/**
 * Round 41: after a reload completes, the capability cache MUST be refreshed.
 *
 * `loadModelStatus()` only paints the model tab; the window/budget facts live in
 * `state.capabilities`. Without this poll the page would keep computing budgets
 * from the OLD window after a successful reload, which is exactly the stale-cache
 * bug the requirement calls out.
 */
function watchReloadCompletion(maxMs = 180000) {
  const began = Date.now();
  const tick = async () => {
    if (Date.now() - began > maxMs) {
      state.modelReloading = false;
      return;
    }
    let st = null;
    try {
      st = await api('/api/chat/model/status');
    } catch (e) {
      setTimeout(tick, 1500);
      return;
    }
    const busyState = st && (st.reloading
      || ['stopping', 'loading', 'restoring'].includes(st.status));
    if (!busyState) {
      // Reload finished (successfully or not): re-read the authoritative facts.
      state.modelReloading = false;
      await loadModelStatus().catch(() => {});
      await loadCapabilities().catch(() => {});
      // Round 43: profile identity (current / last success / target) is only
      // trustworthy AFTER the switch settles, so refresh it here too.
      await loadModelProfiles().catch(() => {});
      // A rolled-back switch is a real outcome the user must see, not a silent
      // no-op. `error` is set by the backend in that case.
      const err = st && st.error;
      if (err) {
        toast(`模型切换未成功：${err}`, true);
        setProfileError(err, true);
      }
      return;
    }
    setTimeout(tick, 1000);
  };
  setTimeout(tick, 800);
}

async function reloadModel() {
  // Round 41: validate the TARGET with the shared rules instead of a 2-value
  // whitelist, and say exactly what is wrong when it fails.
  const res0 = validateContextWindowClient(state.modelTargetWindow);
  if (!res0.ok) {
    toast(`目标窗口无效：${res0.message}`);
    return;
  }
  const targetWindow = res0.value;
  if (targetWindow === state.modelActualWindow
      && state.modelStatus && state.modelStatus.status === 'ready') {
    toast(`当前生效窗口已是 ${targetWindow} tokens，无需重复加载`);
    return;
  }
  const btn = $('btn-reload-model');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '正在重新加载模型…';
  }
  try {
    const res = await api('/api/chat/model/reload', {
      method: 'POST',
      body: JSON.stringify({ target_window: targetWindow }),
    });
    if (res.ok) {
      toast(`已开始重新加载模型（目标窗口: ${targetWindow} tokens）`);
      state.modelReloading = true;
      loadModelStatus();
      // Round 41: keep polling until the reload settles, then refresh the
      // capability cache so budgets use the NEW window.
      watchReloadCompletion();
    } else {
      // Round 41: a reload failure must NOT be reported as success, and the
      // page must not keep showing the target as the applied value.
      toast(`重新加载失败：${res.message || res.error}`, true);
      loadModelStatus();
      loadCapabilities();
    }
  } catch (e) {
    toast(`重新加载请求异常：${e}`, true);
    loadModelStatus();
    loadCapabilities();
  }
}

/* --------------------------------------------------------- Modals & UI */
function isSidebarOverlayOpen() {
  return window.innerWidth <= 800 && $('sidebar') && $('sidebar').classList.contains('open');
}

function updateBackdropVisibility() {
  const isNarrow = window.innerWidth <= 800;
  const drawerOpen = $('memory-drawer') && !$('memory-drawer').hidden;
  const modalOpen = $('settings-modal') && !$('settings-modal').hidden;
  const compressOpen = $('compress-modal') && !$('compress-modal').hidden;
  const confirmOpen = $('confirm-modal') && !$('confirm-modal').hidden;
  const sidebarOpen = isSidebarOverlayOpen();
  // On desktop (> 800px), memory drawer does not block the whole screen with a dark veil
  const needsBackdrop = modalOpen || compressOpen || confirmOpen || (sidebarOpen && isNarrow) || (drawerOpen && isNarrow);
  if ($('modal-backdrop')) {
    $('modal-backdrop').hidden = !needsBackdrop;
  }
}

function openMemoryDrawer() {
  closeSettingsModal();
  closeCompressModal();
  closeMoreMenu();
  state.lastFocusElement = document.activeElement;
  $('memory-drawer').hidden = false;
  $('btn-memory').setAttribute('aria-expanded', 'true');
  updateBackdropVisibility();
  loadMemories();
  if ($('memory-close')) $('memory-close').focus();
}

function closeMemoryDrawer() {
  $('memory-drawer').hidden = true;
  $('btn-memory').setAttribute('aria-expanded', 'false');
  updateBackdropVisibility();
  if (state.lastFocusElement && typeof state.lastFocusElement.focus === 'function') {
    state.lastFocusElement.focus();
  } else if ($('btn-memory')) {
    $('btn-memory').focus();
  }
}

function openSettingsModal(defaultTab) {
  closeMemoryDrawer();
  closeCompressModal();
  closeMoreMenu();
  state.lastFocusElement = document.activeElement;
  $('settings-modal').hidden = false;
  updateBackdropVisibility();
  syncSettingsModal();
  if (defaultTab) {
    switchSettingsTab(defaultTab);
  }
  loadDiagnostics();
  if ($('settings-close')) $('settings-close').focus();
}

function closeSettingsModal() {
  $('settings-modal').hidden = true;
  updateBackdropVisibility();
  if (state.lastFocusElement && typeof state.lastFocusElement.focus === 'function') {
    state.lastFocusElement.focus();
  }
}

function openCompressModal() {
  closeSettingsModal();
  closeMemoryDrawer();
  closeMoreMenu();
  state.lastFocusElement = document.activeElement;
  $('compress-modal').hidden = false;
  updateBackdropVisibility();
  loadCompressModalData();
  if ($('compress-close')) $('compress-close').focus();
}

function closeCompressModal() {
  $('compress-modal').hidden = true;
  updateBackdropVisibility();
  if (state.lastFocusElement && typeof state.lastFocusElement.focus === 'function') {
    state.lastFocusElement.focus();
  }
}

async function loadCompressModalData() {
  if (!state.sessionId) return;
  const alertEl = $('compress-alert');
  const btnGen = $('btn-generate-compress');
  const btnStop = $('btn-stop-compress');
  const btnDel = $('btn-delete-summary');
  const scopeRange = $('compress-coverage-range');
  const candTurns = $('compress-candidate-turns');

  try {
    const res = await api(`/api/chat/session/summary?session_id=${encodeURIComponent(state.sessionId)}`);
    if (!res.ok) {
      if (alertEl) {
        alertEl.hidden = false;
        alertEl.className = 'model-alert error';
        alertEl.textContent = res.message || '读取对话压缩状态失败';
      }
      return;
    }

    state.currentSummary = res.summary || null;
    const isGen = !!res.is_generating;
    state.summaryGenerating = isGen;

    // Scope & candidate stats
    if (candTurns) {
      candTurns.textContent = res.candidate_turns_count > 0
        ? `${res.candidate_turns_count} 轮完整历史（符合压缩条件）`
        : '0 轮（无可压缩的旧轮次）';
    }

    if (res.summary && res.summary.status === 'ready') {
      if (scopeRange) {
        scopeRange.textContent = `消息 #${res.summary.start_seq} 至 #${res.summary.end_seq}（共 ${res.summary.source_turn_count} 轮）`;
      }
      renderCompressPreview(res.summary);
      if (btnDel) btnDel.hidden = false;
      if (btnGen) btnGen.textContent = '重新生成摘要';
    } else {
      if (scopeRange) {
        scopeRange.textContent = res.candidate_turns_count > 0 ? `拟处理前 ${res.candidate_turns_count} 轮完整对话` : '暂无';
      }
      renderCompressPreview(null);
      if (btnDel) btnDel.hidden = true;
      if (btnGen) btnGen.textContent = '开始生成摘要';
    }

    // Button states & alert
    if (isGen) {
      if (btnGen) btnGen.disabled = true;
      if (btnStop) btnStop.hidden = false;
      if (alertEl) {
        alertEl.hidden = false;
        alertEl.className = 'model-alert info';
        alertEl.textContent = '正在由本地模型生成结构化对话摘要，请稍候…';
      }
    } else {
      if (btnStop) btnStop.hidden = true;
      if (res.last_error) {
        if (btnGen) btnGen.disabled = false;
        if (alertEl) {
          alertEl.hidden = false;
          alertEl.className = 'model-alert error';
          alertEl.textContent = `压缩未成功：${res.last_error}`;
        }
      } else if (res.busy) {
        if (btnGen) btnGen.disabled = true;
        if (alertEl) {
          alertEl.hidden = false;
          alertEl.className = 'model-alert warning';
          alertEl.textContent = `当前模型忙碌（${res.busy_reason || '其他任务进行中'}），无法开始压缩。`;
        }
      } else if (!res.can_compress && (!res.summary || res.summary.status !== 'ready')) {
        if (btnGen) btnGen.disabled = true;
        if (alertEl) {
          alertEl.hidden = false;
          alertEl.className = 'model-alert info';
          alertEl.textContent = res.reason || '当前对话轮数较少（需超出保留的最近 3 轮完整对话），暂无需压缩。';
        }
      } else {
        if (btnGen) btnGen.disabled = false;
        if (alertEl) alertEl.hidden = true;
      }
    }

    updateContextIndicator();
  } catch (e) {
    if (alertEl) {
      alertEl.hidden = false;
      alertEl.className = 'model-alert error';
      alertEl.textContent = `状态读取异常：${e.message || e}`;
    }
  }
}

function renderCompressPreview(summary) {
  const box = $('compress-result-box');
  if (!summary || summary.status !== 'ready') {
    if (box) box.hidden = true;
    return;
  }
  if (box) box.hidden = false;
  const json = summary.summary_json || {};

  function renderList(items) {
    if (!items || !items.length) return '<span style="color:var(--muted);font-style:italic;">（无记录）</span>';
    return '<ul style="margin:4px 0 0;padding-left:18px;">' + items.map(it => {
      const src = it.source_msg_id ? ` <span style="color:var(--muted);font-size:11px;">[来源 ${esc(it.source_msg_id)}]</span>` : '';
      const text = esc(it.text || it.content || (typeof it === 'string' ? it : JSON.stringify(it)));
      let extra = '';
      if (it.superseded_by) {
        extra = ` <span style="color:var(--warn-text);font-size:11px;">(已被替代: ${esc(it.superseded_by)})</span>`;
      }
      return `<li style="margin-bottom:3px;">${text}${extra}${src}</li>`;
    }).join('') + '</ul>';
  }

  if ($('section-user-facts')) $('section-user-facts').innerHTML = renderList(json.user_facts_and_constraints);
  if ($('section-goals-decisions')) $('section-goals-decisions').innerHTML = renderList(json.goals_and_decisions);
  if ($('section-todos')) $('section-todos').innerHTML = renderList(json.todos_and_open_issues);
  if ($('section-superseded')) $('section-superseded').innerHTML = renderList(json.superseded_or_revoked);

  const badge = $('compress-status-badge');
  if (badge) {
    badge.textContent = summary.is_active ? '已启用' : '已就绪 (未启用)';
    badge.className = 'result-badge ' + (summary.is_active ? 'ready' : 'disabled');
  }

  const savingsBadge = $('compress-savings-badge');
  if (savingsBadge) {
    const saved = summary.tokens_saved != null ? summary.tokens_saved : 0;
    if (saved > 0) {
      savingsBadge.textContent = `节省约 ${saved} tokens`;
      savingsBadge.style.color = '#137333';
      savingsBadge.style.background = '#e6f4ea';
    } else {
      savingsBadge.textContent = `无空间节省 (净增加 ${-saved} tokens，聊天将回退原文)`;
      savingsBadge.style.color = '#b06000';
      savingsBadge.style.background = '#fef7e0';
    }
  }

  if ($('compress-toggle-active')) {
    $('compress-toggle-active').checked = !!summary.is_active;
  }

  const metaEl = $('compress-meta-info');
  if (metaEl) {
    metaEl.innerHTML = `压缩统计：已压缩 ${summary.source_turn_count || 0} 轮旧对话，覆盖消息 #${summary.start_seq || '—'} 至 #${summary.end_seq || '—'}；输入 ${summary.input_tokens || 0} tokens，生成摘要 ${summary.output_tokens || 0} tokens。`;
  }
}

async function generateSummary() {
  if (!state.sessionId) return;
  const btnGen = $('btn-generate-compress');
  if (btnGen) btnGen.disabled = true;
  const alertEl = $('compress-alert');
  if (alertEl) {
    alertEl.hidden = false;
    alertEl.className = 'model-alert info';
    alertEl.textContent = '已提交压缩请求，正在由本地模型生成结构化对话摘要…';
  }
  const btnStop = $('btn-stop-compress');
  if (btnStop) btnStop.hidden = false;

  try {
    const res = await postJson('/api/chat/session/summary/generate', {
      session_id: state.sessionId,
      recent_turns_kept: 3,
    });
    if (!res.ok) {
      if (alertEl) {
        alertEl.hidden = false;
        alertEl.className = 'model-alert error';
        alertEl.textContent = res.message || '启动压缩失败';
      }
      if (btnGen) btnGen.disabled = false;
      if (btnStop) btnStop.hidden = true;
      return;
    }
    state.summaryGenerating = true;
    pollSummaryStatus();
  } catch (e) {
    if (alertEl) {
      alertEl.hidden = false;
      alertEl.className = 'model-alert error';
      alertEl.textContent = `提交异常：${e.message || e}`;
    }
    if (btnGen) btnGen.disabled = false;
    if (btnStop) btnStop.hidden = true;
  }
}

async function stopGenerateSummary() {
  if (!state.sessionId) return;
  try {
    await postJson('/api/chat/session/summary/stop', { session_id: state.sessionId });
    toast('已请求停止压缩');
    await loadCompressModalData();
  } catch (e) {
    toast(`停止请求异常：${e}`);
  }
}

async function toggleSummaryActive(isActive) {
  if (!state.sessionId) return;
  try {
    const res = await postJson('/api/chat/session/summary/active', {
      session_id: state.sessionId,
      active: !!isActive,
    });
    if (!res.ok) {
      toast(`切换状态失败：${res.message || res.error}`, true);
      return;
    }
    if (state.currentSummary) {
      state.currentSummary.is_active = !!isActive;
    }
    toast(isActive ? '已启用对话摘要' : '已停用对话摘要');
    await loadCompressModalData();
  } catch (e) {
    toast(`切换状态异常：${e}`, true);
  }
}

async function deleteSummary() {
  if (!state.sessionId) return;
  if (!await showInAppConfirm({
    title: '删除对话摘要',
    message: '确定要删除本会话的对话摘要吗？\n删除后原有历史消息完整保留，将恢复原有裁剪方式。',
    danger: true,
    confirmText: '删除'
  })) {
    return;
  }
  try {
    const res = await postJson('/api/chat/session/summary/delete', { session_id: state.sessionId });
    if (!res.ok) {
      toast(`删除失败：${res.message || res.error}`, true);
      return;
    }
    state.currentSummary = null;
    toast('已删除会话摘要');
    await loadCompressModalData();
  } catch (e) {
    toast(`删除异常：${e}`, true);
  }
}

function pollSummaryStatus() {
  let count = 0;
  const timer = setInterval(async () => {
    count++;
    if (!state.sessionId || count > 60 || ($('compress-modal') && $('compress-modal').hidden && !state.summaryGenerating)) {
      clearInterval(timer);
      return;
    }
    try {
      const res = await api(`/api/chat/session/summary?session_id=${encodeURIComponent(state.sessionId)}`);
      if (res.ok && !res.is_generating) {
        clearInterval(timer);
        state.summaryGenerating = false;
        if (state.sessionId === res.session_id) {
          state.currentSummary = res.summary || null;
          loadCompressModalData();
        }
      }
    } catch (_) {}
  }, 1200);
}

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-pane').forEach((p) => {
    p.classList.toggle('active', p.id === tabId);
  });
  if (tabId === 'tab-diagnostics') {
    loadDiagnostics();
  }
  if (tabId === 'tab-model') {
    loadModelStatus();
  }
  if (tabId === 'tab-persona') {
    // Refreshing here is safe: loadPersona() refuses to overwrite unsaved text.
    loadPersona();
  }
}

function toggleMoreMenu() {
  const m = $('more-menu');
  const btn = $('btn-more');
  const willShow = m.hidden;
  m.hidden = !willShow;
  btn.setAttribute('aria-expanded', String(willShow));
}

function closeMoreMenu() {
  const m = $('more-menu');
  if (m) m.hidden = true;
  const btn = $('btn-more');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

function exportSession(fmt) {
  closeMoreMenu();
  if (!state.sessionId) { toast('先选择一个对话'); return; }
  const a = document.createElement('a');
  a.href = `/api/chat/export?session_id=${encodeURIComponent(state.sessionId)}&format=${fmt}`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(`已导出 .${fmt}`);
}

/* ==================================================================
   Round 43: model profile management
   ==================================================================
   Four DIFFERENT things the UI must never merge:
     default   what the next cold start loads (a registry setting)
     target    what the user asked to load (in flight)
     current   what is ACTUALLY serving (verified)
     lastOK    the last profile that loaded and verified
   Editing a profile changes none of them except its own stored fields. */

/**
 * Show a profile-area message.
 *
 * `sticky` means "this is an outcome the user must read, not a transient
 * registry status": a refused switch or an edit-is-not-applied notice. Without
 * this flag the refresh that follows EVERY operation silently erased the reason
 * the operation failed -- the page looked like nothing had happened.
 *
 * `kind` is 'error' (a refusal) or 'info' (a completed operation that the user
 * still needs to understand, e.g. "added, but the running model did not change").
 * Rendering a success in the error colour is its own kind of dishonesty.
 */
function setProfileError(text, sticky, kind) {
  const el = $('profile-error');
  state.modelProfileError = text
    ? {text: String(text), sticky: Boolean(sticky), kind: kind || 'error'}
    : null;
  if (!el) return;
  if (!text) {
    el.hidden = true;
    el.textContent = '';
    el.classList.remove('info');
    return;
  }
  el.hidden = false;
  el.textContent = String(text);
  el.classList.toggle('info', kind === 'info');
}

function repaintProfileError() {
  const el = $('profile-error');
  if (!el) return;
  const cur = state.modelProfileError;
  if (!cur || !cur.text) {
    el.hidden = true;
    el.textContent = '';
    el.classList.remove('info');
    return;
  }
  el.hidden = false;
  el.textContent = cur.text;
  el.classList.toggle('info', cur.kind === 'info');
}

function setDraftError(text) {
  const el = $('pf-error');
  if (!el) return;
  if (!text) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = text;
}

async function loadModelProfiles() {
  let data;
  try {
    data = await api('/api/chat/model/profiles');
  } catch (e) {
    setProfileError(`读取模型配置失败：${e}`, true);
    renderModelProfiles();
    return null;
  }
  if (!data || data.ok === false) {
    // A corrupt registry is REPORTED, never shown as "no profiles".
    const msg = (data && (data.message || data.error)) || '读取模型配置失败';
    state.modelProfileRegistryError = String(msg);
    state.modelProfiles = [];
    state.modelProfileMeta = data || {};
    setProfileError(msg, true);
    renderModelProfiles();
    return data;
  }
  state.modelProfiles = data.profiles || [];
  state.modelProfileMeta = data;
  state.modelProfileRegistryError = data.registry_error
    ? String(data.registry_error.message || '注册表异常') : '';
  // A sticky outcome (refused switch / edit-not-applied) survives the refresh;
  // only a plain registry status is replaced by the newly-read one.
  const cur = state.modelProfileError;
  if (cur && cur.sticky) {
    repaintProfileError();
  } else {
    setProfileError(state.modelProfileRegistryError);
  }
  renderModelProfiles();
  return data;
}

function profileById(pid) {
  return (state.modelProfiles || []).find((p) => p.profile_id === pid) || null;
}

function renderModelProfiles() {
  const box = $('profile-list');
  if (!box) return;
  const meta = state.modelProfileMeta || {};
  const list = state.modelProfiles || [];
  const switching = Boolean(meta.reloading);
  const owned = state.modelStatus ? state.modelStatus.owned : null;

  // Relationship row: default / current / target / last success.
  const def = profileById(meta.default_profile_id);
  const cur = profileById(meta.current_profile_id);
  const tgt = profileById(meta.target_profile_id);
  const lastOK = profileById(meta.last_success_profile_id);
  if ($('profile-relations')) $('profile-relations').hidden = list.length === 0;
  if ($('disp-default-profile')) $('disp-default-profile').textContent = def ? def.display_name : '未设置';
  if ($('disp-current-profile')) {
    $('disp-current-profile').textContent = cur ? cur.display_name
      : (state.modelStatus && state.modelStatus.status === 'ready' ? '未记录（外部服务）' : '未加载');
  }
  if ($('disp-target-profile')) $('disp-target-profile').textContent = tgt ? tgt.display_name : '未选择';
  if ($('disp-last-success-profile')) $('disp-last-success-profile').textContent = lastOK ? lastOK.display_name : '无记录';

  if (!list.length) {
    // Distinguish "there are genuinely no profiles" from "the registry could not
    // be read": the second is a fault, not an empty state.
    box.innerHTML = state.modelProfileRegistryError
      ? '<div class="profile-empty">模型配置无法读取，请查看上方错误提示。'
        + '注册表不会被自动覆盖，也不会被清空。</div>'
      : '<div class="profile-empty">还没有任何模型配置。点击「＋ 添加配置」创建，'
        + '或让应用从 deployment.json 迁移现有模型。</div>';
    return;
  }

  const rows = list.map((p) => {
    const tags = [];
    if (p.is_current) tags.push('<span class="profile-tag current">正在运行</span>');
    if (p.is_default) tags.push('<span class="profile-tag default">默认启动</span>');
    if (p.is_last_success && !p.is_current) tags.push('<span class="profile-tag last-success">最近成功</span>');
    const isTarget = tgt && tgt.profile_id === p.profile_id;
    if (isTarget) tags.push('<span class="profile-tag last-success">正在切换</span>');

    // Buttons: the running profile cannot be deleted; an external service
    // cannot be switched at all (the app will not stop somebody else's server).
    const canLoad = !switching && (owned !== false);
    const loadTitle = switching ? '模型正在切换中'
      : (owned === false ? '当前是外部服务，本应用不会停止或替换它' : '');
    const delTitle = p.is_current ? '正在运行的配置不能直接删除'
      : (isTarget ? '正在切换的目标配置不能删除'
      : (list.length <= 1 ? '这是最后一个配置，不能删除' : '只删除配置，不删除模型文件'));
    const defaultBtn = p.is_default
      ? '<button disabled title="已经是默认启动配置">已是默认</button>'
      : `<button data-action="default" data-id="${esc(p.profile_id)}"${switching ? ' disabled' : ''}>设为默认</button>`;
    return `<div class="profile-item${p.is_current ? ' is-current' : ''}">
      <div class="profile-item-head">
        <span class="profile-item-name">${esc(p.display_name)}</span>
        ${tags.join('')}
      </div>
      <div class="profile-item-meta">
        <span>文件：<code>${esc(p.file_name || '—')}</code></span>
        <span>窗口：${typeof p.target_context_window === 'number' ? p.target_context_window + ' tokens' : '—'}</span>
        <span>GPU 层：${typeof p.gpu_layers === 'number' ? p.gpu_layers : '—'}</span>
        <span>默认回复：${typeof p.default_max_output_tokens === 'number' ? p.default_max_output_tokens + ' tokens' : '—'}</span>
        <span>别名：<code>${esc(p.serving_alias || '—')}</code></span>
      </div>
      <div class="profile-item-actions">
        <button data-action="load" data-id="${esc(p.profile_id)}"
                ${canLoad ? '' : 'disabled'} title="${esc(loadTitle || '加载此配置（会重启受管模型服务）')}">加载此配置</button>
        <button data-action="edit" data-id="${esc(p.profile_id)}"${switching ? ' disabled' : ''}>编辑</button>
        ${defaultBtn}
        <button data-action="delete" data-id="${esc(p.profile_id)}" class="danger"
                ${(p.is_current || isTarget || list.length <= 1 || switching) ? 'disabled' : ''}
                title="${esc(delTitle)}">删除</button>
      </div>
    </div>`;
  }).join('');
  box.innerHTML = rows;

  box.querySelectorAll('button[data-action]').forEach((btn) => {
    btn.onclick = () => onProfileAction(btn.getAttribute('data-action'), btn.getAttribute('data-id'));
  });
}

async function onProfileAction(action, profileId) {
  const prof = profileById(profileId);
  if (!prof) return;
  if (action === 'edit') { openProfileForm(prof); return; }
  if (action === 'load') { await loadModelProfile(profileId); return; }
  if (action === 'default') {
    const res = await api('/api/chat/model/profile/default', {
      method: 'POST', body: JSON.stringify({profile_id: profileId}),
    });
    if (res.ok) {
      toast(`默认启动配置已设为「${res.display_name}」（不会改变当前正在运行的模型）`);
    } else {
      setProfileError(res.message || res.error);
    }
    await loadModelProfiles();
    return;
  }
  if (action === 'delete') {
    const others = (state.modelProfiles || []).filter((p) => p.profile_id !== profileId);
    if (prof.is_default) {
      // The default must be replaced explicitly. No silent auto-pick.
      const names = others.map((p) => `${p.display_name}`).join(' / ');
      const pick = window.prompt(
        `「${prof.display_name}」是默认启动配置。\n删除前请指定新的默认配置名称：\n${names}`, '');
      if (pick === null) return;
      const chosen = others.find((p) => p.display_name === String(pick).trim());
      if (!chosen) { setProfileError('没有找到名为该名称的配置，未执行删除。'); return; }
      const res = await api('/api/chat/model/profile/delete', {
        method: 'POST',
        body: JSON.stringify({profile_id: profileId, new_default_profile_id: chosen.profile_id}),
      });
      if (!res.ok) setProfileError(res.message || res.error);
      else toast(`已删除配置「${res.deleted_display_name}」，模型文件保留。`);
      await loadModelProfiles();
      return;
    }
    if (!await showInAppConfirm({
      title: '删除模型配置',
      message: `确定删除配置「${prof.display_name}」？\n只会删除配置，不会删除模型文件。`,
      danger: true,
      confirmText: '删除配置'
    })) return;
    const res = await api('/api/chat/model/profile/delete', {
      method: 'POST', body: JSON.stringify({profile_id: profileId}),
    });
    if (res.ok) toast(`已删除配置「${res.deleted_display_name}」，模型文件保留。`);
    else setProfileError(res.message || res.error);
    await loadModelProfiles();
  }
}

async function loadModelProfile(profileId) {
  const prof = profileById(profileId);
  if (!prof) return;
  setProfileError('');
  const res = await api('/api/chat/model/profile/switch', {
    method: 'POST', body: JSON.stringify({profile_id: profileId}),
  });
  if (!res.ok) {
    // A refusal must be visible, and must NOT look like a switch happened.
    // Sticky: the refresh below would otherwise erase it.
    setProfileError(`切换未开始：${res.message || res.error}`, true);
    toast(`切换未开始：${res.message || res.error}`, true);
    await loadModelProfiles();
    return;
  }
  toast(`已开始切换到「${prof.display_name}」，完成后会核验实际加载结果`);
  state.modelReloading = true;
  await loadModelProfiles();
  await loadModelStatus();
  watchReloadCompletion();
}

function openProfileForm(prof) {
  const editing = Boolean(prof);
  if ($('profile-form-title')) {
    $('profile-form-title').textContent = editing ? '编辑模型配置' : '添加模型配置';
  }
  if ($('profile-form-id')) $('profile-form-id').value = editing ? prof.profile_id : '';
  if ($('pf-display-name')) $('pf-display-name').value = editing ? (prof.display_name || '') : '';
  if ($('pf-model-path')) $('pf-model-path').value = editing ? (prof.model_path || '') : '';
  if ($('pf-alias')) $('pf-alias').value = editing ? (prof.serving_alias || '') : '';
  if ($('pf-window')) $('pf-window').value = String(editing ? (prof.target_context_window || 4096) : 4096);
  if ($('pf-gpu-layers')) $('pf-gpu-layers').value = String(editing ? (prof.gpu_layers ?? 99) : 99);
  if ($('pf-max-output')) $('pf-max-output').value = String(editing ? (prof.default_max_output_tokens || 512) : 512);
  if ($('pf-file-check')) $('pf-file-check').textContent = '';
  if ($('pf-project-root')) {
    const meta = state.modelProfileMeta || {};
    if (meta.project_root) $('pf-project-root').textContent = meta.project_root;
  }
  setDraftError('');
  if ($('profile-form')) $('profile-form').hidden = false;
  if ($('pf-display-name')) $('pf-display-name').focus();
}

function closeProfileForm() {
  if ($('profile-form')) $('profile-form').hidden = true;
  setDraftError('');
}

function profileDraftFromForm() {
  return {
    display_name: ($('pf-display-name') || {}).value || '',
    model_path: ($('pf-model-path') || {}).value || '',
    serving_alias: ($('pf-alias') || {}).value || '',
    target_context_window: ($('pf-window') || {}).value,
    gpu_layers: ($('pf-gpu-layers') || {}).value,
    default_max_output_tokens: ($('pf-max-output') || {}).value,
  };
}

async function validateProfileDraft() {
  const draft = profileDraftFromForm();
  const res = await api('/api/chat/model/profile/validate', {
    method: 'POST', body: JSON.stringify({profile: draft}),
  });
  if (!res.ok) {
    setDraftError(res.message || res.error);
    if ($('pf-file-check')) $('pf-file-check').textContent = '';
    return null;
  }
  setDraftError((res.warnings || []).join(' '));
  if ($('pf-file-check')) {
    const f = res.file || {};
    $('pf-file-check').textContent = f.is_file
      ? `已找到文件：${f.bytes} 字节${f.looks_like_gguf ? '，GGUF 格式正常' : '，但头部不是 GGUF'}`
      : '该路径下没有找到文件。';
  }
  return res;
}

async function saveProfileDraft() {
  const pid = ($('profile-form-id') || {}).value || '';
  const draft = profileDraftFromForm();
  const path = pid ? '/api/chat/model/profile/update' : '/api/chat/model/profile/create';
  const body = pid ? {profile_id: pid, profile: draft} : {profile: draft};
  const res = await api(path, {method: 'POST', body: JSON.stringify(body)});
  if (!res.ok) {
    setDraftError(res.message || res.error);
    return;
  }
  closeProfileForm();
  toast(res.message || (pid ? '配置已更新' : '配置已添加'));
  await loadModelProfiles();

  // If the EDIT target is the running profile, say clearly that nothing about
  // the live model changed. Sticky, because nothing else will say it again.
  const status = state.modelStatus || {};
  if (pid && status.profile_id === pid) {
    setProfileError('该配置正在运行：本次修改只影响下次加载，当前模型仍按原参数运行。', true, 'info');
  } else if (pid && status.target_profile_id === pid) {
    setProfileError('该配置正在加载切换中：本次修改只影响后续重新加载，当前加载仍按原参数执行。', true, 'info');
  } else if (res.message) {
    // A completed save is INFORMATION, not a failure: same box, different tone.
    setProfileError(res.message, true, 'info');
  }
}

/** Ask the DESKTOP (not the browser) for a real local path. */
/* ------------------------------------------------------------- persona -- */
/* Round 50: a single global persona file. The page shows the file's REAL
   content; saving writes that same file.

   Round 51: every in-flight operation carries a version, and a response is only
   allowed to paint the textarea if it is still the newest thing that happened.
   Without that, "save A -> keep typing B -> response arrives" replaced B with A
   and cleared the unsaved marker, losing what the user had typed.

   Round 52: the ordering rule now spans reads, saves AND switches.

   Counters, and what each one orders:
     personaEditSeq    bumped on every keystroke (the user's own editing)
     personaReadSeq    bumped when a GET is ISSUED -> orders GET against GET
     personaWriteRev   bumped when a SAVE or SWITCH is ISSUED -> orders every
                       GET against any later write, and a write against a later
                       write
     personaWritePending  number of writes in flight right now

   A GET may paint only when BOTH hold:
     * no newer GET was issued afterwards        (personaReadSeq unchanged)
     * no write was issued afterwards            (personaWriteRev unchanged)
     * no write was already in flight when the GET was issued
       (personaWritePending was 0 at issue)

   The third condition is not redundant. The page cannot know in which order the
   server will land two requests it has already sent: a GET issued while a save
   is in flight may perfectly well read the file BEFORE that save lands. Response
   arrival order proves nothing about disk order, so "the last response to arrive
   wins" is exactly the rule that must NOT be used. A read taken during a write is
   therefore trusted only for the file path, which never races.

   A SAVE or SWITCH response is *superseded* when a later write was issued after
   it; a superseded response reports nothing (no switch paint, no feedback). The
   one exception is a superseded SAVE's body: the switch writes
   `persona_state.json` and never touches `persona.md`, so "the body bytes are on
   disk" remains a fact and the saved baseline may still advance.

   Because two concurrent writes would race on persona_state.json (both are
   atomic whole-file replaces, and arrival order says nothing about land order),
   the page serialises them: a switch toggle is refused while a save is in
   flight, and a save is refused while a switch write is in flight.

   A textarea write is refused when a newer edit exists than the one the response
   was issued under. Marking the buffer clean is refused under the same rule. */

function setPersonaFeedback(msg, kind) {
  const el = $('persona-feedback');
  if (!el) return;
  if (!msg) { el.hidden = true; el.textContent = ''; el.className = 'param-error'; return; }
  el.hidden = false;
  el.textContent = msg;
  el.className = kind === 'ok' ? 'param-error ok'
    : (kind === 'info' ? 'param-error info' : 'param-error');
}

function updatePersonaCount() {
  const ta = $('persona-body');
  const el = $('persona-count');
  if (!ta || !el) return;
  const raw = ta.value || '';
  el.textContent = `${raw.length} 字符`;
  // Round 51: `personaUnconfirmed` marks "the box content was never proven to be
  // on disk" (a write whose read-back failed). Text equality alone cannot express
  // that, because the bytes may well be identical to what was sent.
  const dirty = state.personaUnconfirmed
    || (state.personaSaved !== null && raw !== state.personaSaved);
  state.personaDirty = dirty;
  if ($('persona-dirty')) $('persona-dirty').hidden = !dirty;
}

/* The single place the switch is painted. It also records that the real state is
   now known, so a later failed write can put the box back to a KNOWN value
   instead of flipping whatever happens to be displayed.

   `known=false` paints a value we do NOT have evidence for (the revert of a click
   whose write never reached the server). The distinction matters: a later failure
   must not treat such a value as "the last confirmed state". */
function applyPersonaEnabled(enabled, known) {
  state.personaEnabled = Boolean(enabled);
  state.personaEnabledKnown = (known === undefined) ? true : Boolean(known);
  if ($('persona-enabled')) $('persona-enabled').checked = Boolean(enabled);
}

/* Paint a GET response. `allowText` is false when the box holds unsaved edits
   (Round 50 rule) -- the switch and the path may still be refreshed. */
function paintPersona(data, allowText) {
  if (!data) return;
  if (data.enabled !== undefined && data.enabled !== null) {
    applyPersonaEnabled(data.enabled);
  }
  if ($('persona-path')) $('persona-path').textContent = data.path || '—';
  const ta = $('persona-body');
  if (ta && allowText) {
    ta.value = data.body || '';
    state.personaSaved = data.body || '';
    state.personaUnconfirmed = false;
  }
  updatePersonaCount();
}

/* Round 52: does the answer to this GET still describe the file as it will be?
   Never by arrival order -- see the header comment. */
function personaReadIsStale(readSeq, writeRevAtIssue, writesPendingAtIssue) {
  if (state.personaReadSeq !== readSeq) return 'newer_read';
  if (state.personaWriteRev !== writeRevAtIssue) return 'newer_write';
  // A GET issued while a write was in flight may read the file before or after
  // that write lands; its content cannot be trusted for the switch or the body.
  if (writesPendingAtIssue > 0) return 'raced_write';
  return null;
}

async function loadPersona(opts) {
  const force = !!(opts && opts.force);
  // Unsaved edits keep the textarea. `force` only comes from the explicit
  // discard button, which clears the dirty flag before calling us -- so by the
  // time a forced read runs, replacing the text is what the user asked for.
  const allowText = !(state.personaDirty && !force);
  const readSeq = ++state.personaReadSeq;
  const writeRevAtIssue = state.personaWriteRev;
  const writesPendingAtIssue = (state.personaSaving ? 1 : 0)
    + (state.personaSwitchPending ? 1 : 0);
  // Round 51: remember the EDIT generation too. Even a forced read was issued
  // before the user typed whatever is in the box now, so its content describes an
  // older state and must not replace newer input.
  const editSeqAtIssue = state.personaEditSeq;

  let data = null;
  let failure = null;
  try {
    data = await api('/api/chat/persona');
  } catch (e) {
    failure = `读取人格文件失败：${e && e.message ? e.message : e}`;
  }

  // Round 52: a stale read reports NOTHING -- not the switch, not the body, not
  // the saved baseline, not the unsaved marker, and above all not this read's
  // error. A later operation already owns the truth on screen.
  const stale = personaReadIsStale(readSeq, writeRevAtIssue, writesPendingAtIssue);
  if (stale) return null;

  if (failure) {
    setPersonaFeedback(failure, 'error');
    return null;
  }
  if (!data || data.ok === false) {
    setPersonaFeedback((data && (data.message || data.error)) || '读取人格文件失败', 'error');
    // Even on failure show where the file should be, so the user can fix it.
    if (data && data.path && $('persona-path')) $('persona-path').textContent = data.path;
    return null;
  }
  state.personaLoaded = true;
  if (state.personaEditSeq !== editSeqAtIssue) {
    // The user typed while the read was in flight: keep their text, and only
    // report the switch/path. Explicitly NOT clearing the unsaved marker --
    // what is in the box has never been written.
    state.personaSaved = data.body || '';
    if (data.enabled !== undefined && data.enabled !== null) {
      applyPersonaEnabled(data.enabled);
    }
    if (data.path && $('persona-path')) $('persona-path').textContent = data.path;
    updatePersonaCount();
    setPersonaFeedback('读取期间你又编辑了内容，编辑框里保留你的输入（尚未保存）。', 'info');
    return data;
  }
  if (!allowText) {
    // There were unsaved edits when the read was issued; only the non-text
    // fields may be refreshed, and the unsaved marker stays up.
    paintPersona(data, false);
    return data;
  }
  paintPersona(data, true);
  setPersonaFeedback('');
  return data;
}

async function savePersona() {
  if (state.personaSaving) return;
  const ta = $('persona-body');
  if (!ta) return;
  // Round 52: writes are serialised. A save and a switch write both replace
  // persona_state.json wholesale, and the page cannot order their landings; the
  // simplest honest constraint is "one write at a time".
  if (state.personaSwitchPending) {
    setPersonaFeedback('启用开关正在保存，请等它完成后再保存正文。', 'info');
    return;
  }

  // Snapshot exactly what is being submitted, and which edit generation it is.
  const submittedBody = ta.value;
  const submittedEditSeq = state.personaEditSeq;
  const submittedEnabled = $('persona-enabled')
    ? Boolean($('persona-enabled').checked) : undefined;

  state.personaSaving = true;
  const writeRevAtIssue = ++state.personaWriteRev;
  setPersonaFeedback('正在保存…', 'info');
  let res = null;
  let thrown = null;
  try {
    res = await api('/api/chat/persona/save', {
      method: 'POST',
      body: JSON.stringify({ body: submittedBody, enabled: submittedEnabled }),
    });
  } catch (e) {
    thrown = e;
  }
  state.personaSaving = false;

  const typedSince = state.personaEditSeq !== submittedEditSeq;
  const currentText = ta.value;
  // Round 52: a write issued after this one owns the switch and the feedback.
  // (Unreachable while writes are serialised; kept so that lifting that
  // constraint cannot silently reintroduce the overwrite.)
  const superseded = state.personaWriteRev !== writeRevAtIssue;

  if (thrown) {
    if (superseded) return;
    setPersonaFeedback(
      `保存失败：${thrown.message || thrown}（输入内容已保留，未写入文件）`, 'error');
    updatePersonaCount();
    return;
  }

  const bodySaved = !!(res && (res.body_saved === true || res.ok === true));

  if (!res || res.ok === false) {
    if (bodySaved) {
      // Partial success: the bytes ARE on disk, the switch is not (or the
      // read-back could not confirm them). Never say "not written" here -- that
      // would send the user into a needless retype over existing content.
      const confirmed = (res.verified !== false);
      if (confirmed && !typedSince) {
        // What was written is what is in the box: it is now the saved baseline.
        state.personaSaved = submittedBody;
      } else if (!confirmed) {
        // Written but unconfirmed: the box content has NOT been proven to match
        // the file, so the honest marker is "still unsaved". The input is kept.
        state.personaSaved = submittedBody;
        state.personaUnconfirmed = true;
      } else {
        // Confirmed, but the user typed since: the baseline is still what landed.
        state.personaSaved = submittedBody;
      }
      // The switch's truth comes from the server when it says. When it does not,
      // put the box back to the last state we actually knew instead of leaving
      // whatever the user clicked looking like it took effect.
      let switchNote = '';
      if (!superseded) {
        if (res.enabled !== undefined && res.enabled !== null) {
          applyPersonaEnabled(res.enabled);
        } else if (state.personaEnabledKnown) {
          applyPersonaEnabled(state.personaEnabled);
          switchNote = ' 启用开关已回到上一次确认的状态。';
        } else {
          // Do not present the guess as persisted fact.
          applyPersonaEnabled(submittedEnabled === true, false);
          switchNote = ' 启用开关的实际保存状态未能确认，请点「放弃修改并重新读取」核对。';
        }
        setPersonaFeedback(
          `${(res && res.message) || '人格正文已保存，但启用开关未保存。'}`
          + (typedSince ? ' 编辑框里还有未保存的新修改。' : '')
          + switchNote, 'error');
      }
      updatePersonaCount();
      return;
    }
    setPersonaFeedback(
      `${(res && (res.message || res.error)) || '保存失败'}（输入内容已保留，未写入文件）`,
      'error');
    updatePersonaCount();
    return;
  }

  // Full success on the server side. Now decide what the page may claim.
  if (typedSince) {
    // The user kept typing while the save was in flight. Their new text is
    // strictly newer than what was saved, so:
    //   * the textarea is NOT touched (their B stays),
    //   * the clean marker is NOT set (B is still unsaved),
    //   * the saved baseline still advances to what was actually written, so the
    //     unsaved marker correctly compares B against the on-disk A.
    state.personaSaved = submittedBody;
    state.personaUnconfirmed = false;
    updatePersonaCount();
    if (!superseded) {
      if (res.enabled !== undefined && res.enabled !== null) {
        applyPersonaEnabled(res.enabled);
      }
      setPersonaFeedback(
        `已保存到文件（${res.chars != null ? res.chars : submittedBody.length} 字符）。`
        + '保存期间你又继续编辑了，编辑框里的新内容还没有保存。', 'info');
    }
    return;
  }

  // Nothing changed during the save: refresh non-text fields from the file and
  // mark the buffer clean. The textarea is already showing exactly what was
  // written, so a re-read is not needed to "restore" it.
  state.personaSaved = submittedBody;
  state.personaUnconfirmed = false;
  if ($('persona-dirty')) $('persona-dirty').hidden = true;
  state.personaDirty = false;
  if (superseded) {
    // A newer write owns the switch and the feedback line; only the body facts
    // above are ours to keep.
    updatePersonaCount();
    return;
  }
  if (res.enabled !== undefined && res.enabled !== null) {
    applyPersonaEnabled(res.enabled);
  }
  if ($('persona-path') && res.path) $('persona-path').textContent = res.path;
  updatePersonaCount();
  const on = (res.enabled === undefined || res.enabled === null) ? null : res.enabled;
  setPersonaFeedback(
    `已保存到文件（${res.chars != null ? res.chars : submittedBody.length} 字符）`
    + (on === null ? '。' : (on ? '，人格已启用。' : '，人格当前关闭，不会参与请求。')),
    'ok');
  toast('人格已保存');
  if (currentText !== submittedBody) {
    // Defensive: if the box somehow differs, keep it and re-mark dirty.
    updatePersonaCount();
  }
}

async function onPersonaEnabledToggle() {
  const box = $('persona-enabled');
  if (!box) return;
  const want = Boolean(box.checked);
  // Round 52: one write at a time (see savePersona). Refusing here is better than
  // letting two whole-file replaces race on persona_state.json.
  if (state.personaSaving) {
    applyPersonaEnabled(state.personaEnabledKnown ? state.personaEnabled : !want,
      state.personaEnabledKnown);
    setPersonaFeedback('正文正在保存，请等它完成后再切换启用开关。', 'info');
    return;
  }
  if (state.personaSwitchPending) {
    // A toggle is already in flight and the box was clicked again mid-flight.
    // Ignore this click: its write would race the one in flight.
    applyPersonaEnabled(state.personaEnabledKnown ? state.personaEnabled : !want,
      state.personaEnabledKnown);
    return;
  }
  const previousKnown = state.personaEnabledKnown;
  const previous = previousKnown ? state.personaEnabled : !want;
  const writeRevAtIssue = ++state.personaWriteRev;
  state.personaSwitchPending = true;
  let res = null;
  let thrown = null;
  try {
    res = await api('/api/chat/persona/enabled', {
      method: 'POST', body: JSON.stringify({ enabled: want }),
    });
  } catch (e) {
    thrown = e;
  }
  state.personaSwitchPending = false;
  // A newer write (another toggle, or a save) owns the switch and the feedback.
  if (state.personaWriteRev !== writeRevAtIssue) return;
  if (thrown || !res || res.ok === false) {
    // Revert to a value there is EVIDENCE for. When the previous state was never
    // confirmed, say so instead of presenting the revert as persisted fact.
    applyPersonaEnabled(previous, previousKnown);
    setPersonaFeedback(
      ((thrown && (thrown.message || String(thrown)))
       || (res && (res.message || res.error)) || '保存启用状态失败')
      + (previousKnown ? ' 已回到上一次确认的状态。'
        : ' 启用开关的实际状态未能确认，请点「放弃修改并重新读取」核对。'),
      'error');
    return;
  }
  applyPersonaEnabled(res.enabled);
  setPersonaFeedback(state.personaEnabled
    ? '人格已启用，下一条请求起生效。'
    : '人格已关闭，之后的请求不再包含人格。', 'ok');
}

/* Explicit user action: drop unsaved edits and show the file as it is.
   Round 52: refused while a write is in flight -- a read issued during a write
   cannot tell whether it saw the file before or after that write landed, so it
   would show a state that may already be wrong. */
function discardPersonaEdits() {
  if (state.personaSaving || state.personaSwitchPending) {
    setPersonaFeedback('正在保存，请等保存完成后再重新读取，否则读到的可能不是最终内容。',
      'info');
    return Promise.resolve(null);
  }
  state.personaDirty = false;
  state.personaSaved = null;
  state.personaUnconfirmed = false;
  setPersonaFeedback('');
  return loadPersona({ force: true });
}

async function browseModelFile() {
  if (!window.pywebview || !window.pywebview.api
      || typeof window.pywebview.api.choose_model_file !== 'function') {
    setDraftError('当前环境不支持系统文件选择器，请直接填写本地路径（支持绝对路径）。');
    return;
  }
  let picked;
  try {
    picked = await window.pywebview.api.choose_model_file();
  } catch (e) {
    setDraftError(`打开文件选择器失败：${e}`);
    return;
  }
  if (!picked || !picked.ok) {
    if (picked && picked.message) setDraftError(picked.message);
    return;
  }
  if ($('pf-model-path')) $('pf-model-path').value = picked.path;
  setDraftError('');
  await validateProfileDraft();
}


function bindEvents() {
  // Sidebar actions
  $('btn-new').onclick = () => newSession();
  $('btn-sidebar-collapse').onclick = () => {
    if (window.innerWidth <= 800) {
      $('sidebar').classList.remove('open');
      updateBackdropVisibility();
    } else {
      $('sidebar').classList.add('collapsed');
      $('btn-sidebar-expand').hidden = false;
    }
  };
  $('btn-sidebar-expand').onclick = () => {
    if (window.innerWidth <= 800) {
      $('sidebar').classList.add('open');
      updateBackdropVisibility();
    } else {
      $('sidebar').classList.remove('collapsed');
      $('btn-sidebar-expand').hidden = true;
    }
  };

  // Responsive window resize
  window.addEventListener('resize', () => {
    if (window.innerWidth > 800) {
      $('sidebar').classList.remove('open');
      const isCollapsed = $('sidebar').classList.contains('collapsed');
      $('btn-sidebar-expand').hidden = !isCollapsed;
    } else {
      $('btn-sidebar-expand').hidden = false;
    }
    updateBackdropVisibility();
  });

  // Search
  const searchInput = $('session-search');
  searchInput.addEventListener('input', () => {
    state.searchQuery = searchInput.value;
    $('btn-search-clear').hidden = !searchInput.value;
    renderSessions();
  });
  $('btn-search-clear').onclick = () => {
    searchInput.value = '';
    state.searchQuery = '';
    $('btn-search-clear').hidden = true;
    renderSessions();
    searchInput.focus();
  };

  // Chat action buttons
  $('btn-send').onclick = () => send();
  $('btn-stop').onclick = () => stopGen();
  $('btn-session-settings').onclick = () => openSettingsModal('tab-session');
  $('btn-more').onclick = (e) => {
    e.stopPropagation();
    toggleMoreMenu();
  };
  $('btn-rename').onclick = () => renameSession();
  $('btn-restart-boundary').onclick = () => setRestartBoundary();
  $('btn-compress-history').onclick = () => { closeMoreMenu(); openCompressModal(); };
  $('btn-export-md').onclick = () => exportSession('md');
  $('btn-export-json').onclick = () => exportSession('json');
  $('btn-delete').onclick = () => deleteSession();

  // Quick Bar
  $('quick-style').addEventListener('change', (e) => updateQuickSetting('style', e.target.value));
  $('quick-history').addEventListener('change', (e) => {
    if (e.target.value === 'custom') {
      openSettingsModal('tab-session');
    } else {
      updateQuickSetting('history_mode', e.target.value);
    }
  });
  $('quick-max-tokens').addEventListener('change', (e) => {
    if (e.target.value === 'custom') {
      // Reveal the custom box; the value is committed on Enter/blur so a
      // half-typed number is never sent.
      const box = $('quick-max-tokens-custom');
      if (box) {
        box.hidden = false;
        box.value = String(state.sessionSettings.max_tokens || 512);
        box.focus();
        box.select && box.select();
      }
      return;
    }
    updateQuickSetting('max_tokens', e.target.value);
  });
  if ($('quick-max-tokens-custom')) {
    const commitQuickMax = () => {
      const box = $('quick-max-tokens-custom');
      if (!box || box.hidden) return;
      updateQuickSetting('max_tokens', box.value);
    };
    $('quick-max-tokens-custom').addEventListener('blur', commitQuickMax);
    $('quick-max-tokens-custom').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commitQuickMax(); }
    });
  }
  // Modal: same pattern for the reply cap select + custom box.
  if ($('setting-max-tokens')) {
    $('setting-max-tokens').addEventListener('change', (e) => {
      const box = $('setting-max-tokens-custom');
      if (!box) return;
      if (e.target.value === 'custom') {
        box.hidden = false;
        box.value = String(state.sessionSettings.max_tokens || 512);
        box.focus();
      } else {
        box.hidden = true;
      }
    });
  }
  if ($('quick-web-search')) {
    $('quick-web-search').addEventListener('change', (e) => updateQuickSetting('web_search', e.target.checked));
  }
  $('btn-quick-to-settings').onclick = () => openSettingsModal('tab-session');

  // Input area
  const input = $('input');
  input.addEventListener('input', () => {
    adjustInputHeight();
    if (state.sessionId) {
      state.drafts[state.sessionId] = input.value;
    }
  });
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    const isCtrl = state.appearance.sendShortcut === 'ctrl_enter';
    const shouldSend = isCtrl
      ? (e.key === 'Enter' && (e.ctrlKey || e.metaKey))
      : (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey);
    if (shouldSend) {
      e.preventDefault();
      send();
    }
  });

  // Drawers & Modals
  $('btn-memory').onclick = () => openMemoryDrawer();
  $('memory-close').onclick = () => closeMemoryDrawer();
  $('btn-open-settings').onclick = () => openSettingsModal('tab-appearance');
  $('settings-close').onclick = () => closeSettingsModal();
  $('compress-close').onclick = () => closeCompressModal();
  $('btn-cancel-compress').onclick = () => closeCompressModal();
  $('btn-generate-compress').onclick = () => generateSummary();
  $('btn-stop-compress').onclick = () => stopGenerateSummary();
  $('btn-delete-summary').onclick = () => deleteSummary();
  $('compress-toggle-active').addEventListener('change', (e) => toggleSummaryActive(e.target.checked));
  $('modal-backdrop').onclick = () => {
    if (window.innerWidth <= 800 && $('sidebar').classList.contains('open')) {
      $('sidebar').classList.remove('open');
    }
    closeMemoryDrawer();
    closeSettingsModal();
    closeCompressModal();
    closeMoreMenu();
    updateBackdropVisibility();
  };

  // Close dropdown menu on outside click
  document.addEventListener('click', (e) => {
    const moreWrap = $('dropdown-more-wrap');
    if (moreWrap && !moreWrap.contains(e.target)) {
      closeMoreMenu();
    }
  });

  // ESC key handler
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('compress-modal').hidden) {
        closeCompressModal();
      } else if (!$('settings-modal').hidden) {
        closeSettingsModal();
      } else if (!$('memory-drawer').hidden) {
        closeMemoryDrawer();
      } else if (window.innerWidth <= 800 && $('sidebar').classList.contains('open')) {
        $('sidebar').classList.remove('open');
        updateBackdropVisibility();
      } else {
        closeMoreMenu();
      }
    }
  });

  // Settings tabs
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    tab.onclick = () => switchSettingsTab(tab.dataset.tab);
  });

  // Session settings in modal
  $('setting-history').addEventListener('change', (e) => {
    toggleCustomBudgetVisibility(e.target.value === 'custom');
  });
  $('btn-save-session-settings').onclick = () => saveSessionSettingsFromModal();
  $('btn-reset-session-settings').onclick = () => resetSessionSettings();

  // Persona (Round 50, versioning added in Round 51)
  if ($('btn-persona-save')) $('btn-persona-save').onclick = () => savePersona();
  if ($('btn-persona-reload')) {
    // Round 51: discarding unsaved edits is an EXPLICIT user action and the only
    // path allowed to replace typed text with the file's content.
    $('btn-persona-reload').onclick = () => { discardPersonaEdits(); };
  }
  if ($('persona-enabled')) {
    $('persona-enabled').addEventListener('change', () => onPersonaEnabledToggle());
  }
  if ($('persona-body')) {
    // Typing bumps the edit generation immediately, so neither a late save
    // response nor a late GET response can overwrite it or clear the marker.
    $('persona-body').addEventListener('input', () => {
      if (state.personaSaved === null) state.personaSaved = '';
      state.personaEditSeq++;
      updatePersonaCount();
    });
  }

  // Appearance settings
  $('setting-theme').addEventListener('change', (e) => {
    state.appearance.theme = e.target.value;
    saveAppearance();
  });
  $('setting-font-size').addEventListener('change', (e) => {
    state.appearance.fontSize = e.target.value;
    saveAppearance();
  });
  $('setting-density').addEventListener('change', (e) => {
    state.appearance.density = e.target.value;
    saveAppearance();
  });
  $('setting-send-shortcut').addEventListener('change', (e) => {
    state.appearance.sendShortcut = e.target.value;
    saveAppearance();
  });

  // Model window: presets + custom (Round 41 generalisation)
  const pickWindow = (value) => {
    state.modelTargetWindow = value;
    const num = (typeof value === 'number') ? value : null;
    if ($('disp-target-window')) {
      $('disp-target-window').textContent = num ? `${num} tokens` : '未选择';
    }
    const isPreset = [2048, 4096].includes(num);
    if ($('opt-window-2048')) $('opt-window-2048').classList.toggle('selected', num === 2048);
    if ($('opt-window-4096')) $('opt-window-4096').classList.toggle('selected', num === 4096);
    if ($('opt-window-custom')) $('opt-window-custom').classList.toggle('selected', Boolean(num && !isPreset));
    if ($('group-custom-window')) $('group-custom-window').hidden = isPreset;
    if ($('input-custom-window') && num && !isPreset) {
      $('input-custom-window').value = String(num);
    }
  };
  const r2048 = $('radio-window-2048');
  if (r2048) r2048.onchange = () => pickWindow(2048);
  const r4096 = $('radio-window-4096');
  if (r4096) r4096.onchange = () => pickWindow(4096);
  const rCustom = $('radio-window-custom');
  if (rCustom) {
    rCustom.onchange = () => {
      if ($('group-custom-window')) $('group-custom-window').hidden = false;
      const v = $('input-custom-window') ? $('input-custom-window').value : '';
      const check = validateContextWindowClient(v);
      if (check.ok) {
        pickWindow(check.value);
      } else {
        state.modelTargetWindow = null;
        if ($('disp-target-window')) $('disp-target-window').textContent = '未选择';
        if ($('opt-window-2048')) $('opt-window-2048').classList.remove('selected');
        if ($('opt-window-4096')) $('opt-window-4096').classList.remove('selected');
        if ($('opt-window-custom')) $('opt-window-custom').classList.add('selected');
        if ($('input-custom-window')) $('input-custom-window').focus();
      }
    };
  }
  if ($('input-custom-window')) {
    $('input-custom-window').addEventListener('input', (e) => {
      const check = validateContextWindowClient(e.target.value);
      if (check.ok) {
        state.modelTargetWindow = check.value;
        if ($('disp-target-window')) $('disp-target-window').textContent = `${check.value} tokens`;
        setParamError('');
      } else {
        state.modelTargetWindow = null;
        if ($('disp-target-window')) $('disp-target-window').textContent = '未选择';
        if (e.target.value !== '') setParamError(`目标窗口无效：${check.message}`);
      }
    });
  }

  // Reload model button
  const btnReload = $('btn-reload-model');
  if (btnReload) {
    btnReload.onclick = () => {
      reloadModel();
    };
  }

  // ---- Round 43: model profile management ---------------------------- //
  if ($('btn-profile-add')) {
    $('btn-profile-add').onclick = () => openProfileForm(null);
  }
  if ($('btn-pf-cancel')) {
    $('btn-pf-cancel').onclick = () => closeProfileForm();
  }
  if ($('btn-pf-browse')) {
    $('btn-pf-browse').onclick = () => browseModelFile();
  }
  if ($('btn-pf-save')) {
    $('btn-pf-save').onclick = () => saveProfileDraft();
  }
  // Live file check: the user should see "not found" BEFORE saving.
  if ($('pf-model-path')) {
    let pathTimer = null;
    $('pf-model-path').addEventListener('input', () => {
      if (pathTimer) clearTimeout(pathTimer);
      pathTimer = setTimeout(() => {
        const v = $('pf-model-path').value.trim();
        if (!v) { setDraftError(''); if ($('pf-file-check')) $('pf-file-check').textContent = ''; return; }
        validateProfileDraft();
      }, 450);
    });
  }
}

/* ------------------------------------------------------------------- Boot */
async function boot() {
  loadAppearance();
  bindEvents();
  await loadSessions();
  await loadMemories();
  loadModelStatus().catch(() => {});
  if (state.sessions.length) {
    await openSession(state.sessions[0].id);
  } else {
    await newSession();
  }
  // Round 41: pull the capability snapshot AFTER a session exists, so the
  // budget limits reflect that session's reply reserve.
  await loadCapabilities().catch(() => {});
  // Round 43: the profile list is what the model tab renders, and it also
  // carries current/default/last-success identity, so load it at boot too.
  await loadModelProfiles().catch(() => {});
}

// Expose key production functions for UI handlers and automated verification
window.parseSeq = parseSeq;
window.dispatchSessionSettingsUpdate = dispatchSessionSettingsUpdate;
window.revertSessionSettings = revertSessionSettings;
window.updateQuickSetting = updateQuickSetting;
window.saveSessionSettingsFromModal = saveSessionSettingsFromModal;
window.resetSessionSettings = resetSessionSettings;
window.openSession = openSession;
window.send = send;
window.api = api;
window.post = post;
window.postJson = postJson;
window.setNotice = setNotice;
window.setNoticeWithActions = setNoticeWithActions;
window.retryFailedWebSearch = retryFailedWebSearch;
window.handleWebSearchFailure = handleWebSearchFailure;
window.rememberFailedSearch = rememberFailedSearch;
window.searchFailureReasonZh = searchFailureReasonZh;
// Exported so a cross-layer test can open the REAL stream subscription and then
// deliver REAL backend events into it, instead of re-implementing the handler.
window.connectStream = connectStream;
window.closeStream = closeStream;
window.refreshCurrent = refreshCurrent;
window.stopGen = stopGen;
window.drawBusy = drawBusy;
window.setProgressNotice = setProgressNotice;
window.clearProgressNoticeIfOwned = clearProgressNoticeIfOwned;
window.syncQuickBar = syncQuickBar;
window.syncSettingsModal = syncSettingsModal;
window.loadModelStatus = loadModelStatus;
window.loadCapabilities = loadCapabilities;
window.renderCapabilities = renderCapabilities;
window.syncMaxTokensControls = syncMaxTokensControls;
window.syncCustomBudgetLimits = syncCustomBudgetLimits;
window.validateMaxTokensClient = validateMaxTokensClient;
window.validateContextWindowClient = validateContextWindowClient;
window.validateCustomBudgetClient = validateCustomBudgetClient;
window.parseStrictIntClient = parseStrictIntClient;
window.reloadModel = reloadModel;
window.watchReloadCompletion = watchReloadCompletion;
// Round 43: model profile management (exported for automated verification).
window.loadModelProfiles = loadModelProfiles;
window.renderModelProfiles = renderModelProfiles;
window.loadModelProfile = loadModelProfile;
window.onProfileAction = onProfileAction;
window.openProfileForm = openProfileForm;
window.closeProfileForm = closeProfileForm;
window.profileDraftFromForm = profileDraftFromForm;
window.validateProfileDraft = validateProfileDraft;
window.saveProfileDraft = saveProfileDraft;
window.browseModelFile = browseModelFile;
window.setProfileError = setProfileError;
window.openSettingsModal = openSettingsModal;
window.closeSettingsModal = closeSettingsModal;
window.openCompressModal = openCompressModal;
window.closeCompressModal = closeCompressModal;
window.loadCompressModalData = loadCompressModalData;
window.generateSummary = generateSummary;
window.stopGenerateSummary = stopGenerateSummary;
window.toggleSummaryActive = toggleSummaryActive;
window.deleteSummary = deleteSummary;
window.updateContextIndicator = updateContextIndicator;
// Round 50: persona editor (exported for automated verification).
window.loadPersona = loadPersona;
window.savePersona = savePersona;
window.onPersonaEnabledToggle = onPersonaEnabledToggle;
window.updatePersonaCount = updatePersonaCount;
window.discardPersonaEdits = discardPersonaEdits;
window.applyPersonaEnabled = applyPersonaEnabled;
window.boot = boot;

if (!window.__HERMES_TEST_NO_AUTO_BOOT__) {
  boot().catch((e) => {
    document.body.insertAdjacentHTML('beforeend',
      `<div class="banner">页面初始化失败：${esc(String(e))}</div>`);
  });
}
