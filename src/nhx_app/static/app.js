'use strict';

// ── 工具 ────────────────────────────────────────────────────────────────────

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3200);
}

// ── 迷你 Markdown 渲染（零依赖，先转义再解析）────────────────────────────
//
// 安全前提：**先整体转义**，再用白名单标签拼装。
// 因此模型输出里的 <script> 不可能变成真标签。

function md(src) {
  let t = esc(src || '');

  // 代码块
  const blocks = [];
  t = t.replace(/```([\s\S]*?)```/g, (_, code) => {
    blocks.push('<pre><code>' + code.replace(/^\w*\n/, '') + '</code></pre>');
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });

  // 行内代码
  const inlines = [];
  t = t.replace(/`([^`\n]+)`/g, (_, c) => {
    inlines.push('<code>' + c + '</code>');
    return '\u0000I' + (inlines.length - 1) + '\u0000';
  });

  const lines = t.split('\n');
  const out = [];
  let listType = null, inQuote = false, tableBuf = [];

  const closeList = () => { if (listType) { out.push('</' + listType + '>'); listType = null; } };
  const closeQuote = () => { if (inQuote) { out.push('</blockquote>'); inQuote = false; } };
  const flushTable = () => {
    if (!tableBuf.length) return;
    const rows = tableBuf.map(r => r.split('|').map(c => c.trim()).filter((c, i, a) => i > 0 && i < a.length - 0));
    // 去掉分隔行 |---|---|
    const body = rows.filter(r => !r.every(c => /^:?-{2,}:?$/.test(c)));
    if (body.length) {
      out.push('<table><thead><tr>' + body[0].map(c => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>');
      for (const r of body.slice(1)) {
        out.push('<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>');
      }
      out.push('</tbody></table>');
    }
    tableBuf = [];
  };

  for (const raw of lines) {
    const line = raw;

    if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); closeQuote(); tableBuf.push(line.trim()); continue; }
    flushTable();

    if (!line.trim()) { closeList(); closeQuote(); out.push(''); continue; }

    if (/^---+$/.test(line.trim())) { closeList(); closeQuote(); out.push('<hr>'); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); closeQuote(); const lv = h[1].length; out.push('<h' + lv + '>' + h[2] + '</h' + lv + '>'); continue; }

    const q = line.match(/^&gt;\s?(.*)$/);
    if (q) { closeList(); if (!inQuote) { out.push('<blockquote>'); inQuote = true; } out.push('<p>' + q[1] + '</p>'); continue; }
    closeQuote();

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) { if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; } out.push('<li>' + ul[1] + '</li>'); continue; }

    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) { if (listType !== 'ol') { closeList(); out.push('<ol>'); listType = 'ol'; } out.push('<li>' + ol[1] + '</li>'); continue; }
    closeList();

    out.push('<p>' + line + '</p>');
  }
  closeList(); closeQuote(); flushTable();

  let html = out.join('\n');

  // 行内样式
  html = html
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // 还原占位
  html = html.replace(/\u0000I(\d+)\u0000/g, (_, i) => inlines[+i]);
  html = html.replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);
  return html;
}

// ── 认证 ────────────────────────────────────────────────────────────────────

const TOKEN_KEY = 'nhx_app_token';
const AUTH = {
  get token() { return sessionStorage.getItem(TOKEN_KEY) || ''; },
  set token(v) { v ? sessionStorage.setItem(TOKEN_KEY, v) : sessionStorage.removeItem(TOKEN_KEY); },
  user: null, mode: 'open',
};

async function api(path, opts) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (opts || {}).headers || {});
  if (AUTH.token) headers['Authorization'] = 'Bearer ' + AUTH.token;
  const res = await fetch('/api' + path, Object.assign({}, opts || {}, { headers }));
  let body;
  try { body = await res.json(); } catch { throw new Error('HTTP ' + res.status); }
  if (!res.ok || !body.ok) {
    const e = new Error(body.error || ('HTTP ' + res.status));
    e.status = res.status;
    throw e;
  }
  return body.data;
}

let _loginResolve = null;
function promptLogin(hint) {
  $('#loginHint').textContent = hint || '请输入用户名与口令。';
  $('#loginError').hidden = true;
  $('#loginPass').value = '';
  $('#loginModal').classList.add('open');
  setTimeout(() => $('#loginUser').focus(), 50);
  return new Promise((r) => { _loginResolve = r; });
}
function closeLogin(v) {
  $('#loginModal').classList.remove('open');
  if (_loginResolve) { _loginResolve(v); _loginResolve = null; }
}
async function doLogin() {
  const username = $('#loginUser').value.trim(), password = $('#loginPass').value;
  if (!username || !password) { showLoginErr('请填写用户名与口令'); return; }
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) { showLoginErr(body.error || '登录失败'); return; }
    AUTH.token = body.data.token;
    toast('已登录：' + body.data.username, 'ok');
    closeLogin(true);
    await refreshAuth();
  } catch (e) { showLoginErr(e.message); }
}
function showLoginErr(m) { $('#loginError').textContent = m; $('#loginError').hidden = false; }

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
  AUTH.token = '';
  toast('已退出登录');
  await refreshAuth();
}

async function refreshAuth() {
  try {
    const st = await api('/auth/status');
    AUTH.user = st.user; AUTH.mode = st.mode;
    $('#loginBtn').hidden = st.authenticated || st.mode === 'open';
    $('#logoutBtn').hidden = !st.authenticated;
  } catch { /* ignore */ }
}

// ── 状态 ────────────────────────────────────────────────────────────────────

const S = { sessionId: null, busy: false, controller: null, config: null };

// ── 渲染 ────────────────────────────────────────────────────────────────────

function ensureChatStarted() {
  const w = $('#welcome');
  if (w) w.remove();
}

function addUserMessage(text) {
  ensureChatStarted();
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = '<div class="avatar">我</div><div class="body"></div>';
  el.querySelector('.body').textContent = text;
  $('#chat').appendChild(el);
  scrollDown();
}

function addAssistantShell() {
  ensureChatStarted();
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML =
    '<div class="avatar">倪</div>' +
    '<div class="body">' +
    '  <div class="msg-head"></div>' +
    '  <div class="md cursor"></div>' +
    '</div>';
  $('#chat').appendChild(el);
  scrollDown();
  return el;
}

function setLayer(el, layer) {
  const head = el.querySelector('.msg-head');
  if (!layer) return;
  const map = { L1: 'L1 学习', L2: 'L2 参考', L3: 'L3 文化', EMERGENCY: '⚠ 急症分诊' };
  head.innerHTML = '<span class="layer-tag ' + esc(layer) + '">' + esc(map[layer] || layer) + '</span>';
  if (layer === 'EMERGENCY') {
    el.querySelector('.md').parentElement.classList.add('emergency');
    el.querySelector('.md').insertAdjacentHTML('beforebegin',
      '<p class="em-title">⚠ 这需要立即就医</p>');
  }
}

function setGuard(el, guard) {
  if (!guard || !guard.findings || !guard.findings.length) return;
  const head = el.querySelector('.msg-head');
  const cls = guard.verdict === 'blocked' ? 'block' : (guard.verdict === 'repaired' ? 'warn' : '');
  const label = guard.verdict === 'blocked' ? '守门：已拦截改写'
    : guard.verdict === 'repaired' ? '守门：已改写' : '守门：已记录';
  const chip = document.createElement('span');
  chip.className = 'guard-chip ' + cls;
  chip.textContent = label;
  chip.title = guard.findings.map(f => '[' + f.rule + '] ' + f.reason).join('\n');
  head.appendChild(chip);
}

function setSources(el, sources) {
  if (!sources || !sources.length) return;
  const details = document.createElement('details');
  details.className = 'sources';
  details.innerHTML = '<summary>知识库命中 ' + sources.length + ' 条</summary>' +
    sources.map(s =>
      '<div class="src-item">' +
      '<div class="src-title">' + esc(s.title) + '</div>' +
      '<div class="src-path">' + esc(s.path) + ' · ' + esc(s.kind) + '</div>' +
      '<div class="src-snip">' + esc(s.snippet) + '</div>' +
      '</div>').join('');
  el.querySelector('.body').appendChild(details);
}

function scrollDown() {
  const c = $('#chat');
  c.scrollTop = c.scrollHeight;
}

// ── 发送（SSE 流式）────────────────────────────────────────────────────────

async function send(text) {
  if (S.busy || !text.trim()) return;
  S.busy = true;
  $('#send').hidden = true;
  $('#stop').hidden = false;
  $('#input').value = '';
  autoGrow();

  addUserMessage(text);
  const el = addAssistantShell();
  const mdEl = el.querySelector('.md');

  let buffer = '', layer = '', raw = '';
  S.controller = new AbortController();

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (AUTH.token) headers['Authorization'] = 'Bearer ' + AUTH.token;

    const res = await fetch('/api/chat', {
      method: 'POST', headers, signal: S.controller.signal,
      body: JSON.stringify({ text, session_id: S.sessionId }),
    });

    if (res.status === 401) {
      mdEl.innerHTML = '';
      const ok = await promptLogin('需要登录后才能对话');
      if (ok) { S.busy = false; $('#send').hidden = false; $('#stop').hidden = true; return send(text); }
      mdEl.innerHTML = '<p class="muted">未登录，已取消。</p>';
      return;
    }
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error(b.error || ('HTTP ' + res.status));
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let sseBuf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      sseBuf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = sseBuf.indexOf('\n\n')) >= 0) {
        const frame = sseBuf.slice(0, idx);
        sseBuf = sseBuf.slice(idx + 2);

        let ev = 'message', dataStr = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
        }
        if (!dataStr) continue;

        let data;
        try { data = JSON.parse(dataStr); } catch { continue; }

        if (ev === 'session') { S.sessionId = data.session_id; }
        else if (ev === 'triage' && data.hit) { setLayer(el, 'EMERGENCY'); }
        else if (ev === 'route') { layer = data.layer; setLayer(el, layer); }
        else if (ev === 'sources') { setSources(el, data.items); }
        else if (ev === 'delta') { buffer += data.text; mdEl.innerHTML = md(buffer); scrollDown(); }
        else if (ev === 'guard') { setGuard(el, data); }
        else if (ev === 'error') { throw new Error(data.message); }
        else if (ev === 'done') { raw = data; }
      }
    }

    mdEl.classList.remove('cursor');
    if (raw && raw.emergency) setLayer(el, 'EMERGENCY');
    mdEl.innerHTML = md(buffer) || '<p class="muted">（无内容）</p>';
    scrollDown();

  } catch (e) {
    mdEl.classList.remove('cursor');
    if (e.name === 'AbortError') {
      mdEl.innerHTML = md(buffer) + '<p class="muted">（已停止）</p>';
    } else {
      mdEl.innerHTML = '<p class="muted">出错了：' + esc(e.message) + '</p>';
      toast(e.message, 'err');
    }
  } finally {
    S.busy = false;
    S.controller = null;
    $('#send').hidden = false;
    $('#stop').hidden = true;
    $('#input').focus();
  }
}

// ── 输入框 ──────────────────────────────────────────────────────────────────

function autoGrow() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 190) + 'px';
}

// ── 初始化 ──────────────────────────────────────────────────────────────────

async function init() {
  try {
    S.config = await api('/config');
  } catch { /* ignore */ }

  const pill = $('#statusPill');
  if (S.config && S.config.llm_ready) {
    pill.textContent = S.config.model;
    pill.className = 'pill ok';
    pill.title = '模型已就绪';
  } else {
    pill.textContent = '未配置模型';
    pill.className = 'pill bad';
    pill.title = '服务端未配置 NHX_LLM_API_KEY';
  }

  await refreshAuth();

  $('#send').addEventListener('click', () => send($('#input').value));
  $('#stop').addEventListener('click', () => { if (S.controller) S.controller.abort(); });
  $('#input').addEventListener('input', autoGrow);
  $('#input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send($('#input').value);
    }
  });

  $('#newChat').addEventListener('click', async () => {
    if (S.busy && S.controller) S.controller.abort();
    S.sessionId = null;
    $('#chat').innerHTML = '';
    location.reload();
  });

  $('#loginBtn').addEventListener('click', () => promptLogin());
  $('#logoutBtn').addEventListener('click', doLogout);
  $('#loginSubmit').addEventListener('click', doLogin);
  $('#loginCancel').addEventListener('click', () => closeLogin(false));
  $('#loginPass').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('#loginModal').addEventListener('click', (e) => {
    if (e.target === $('#loginModal')) closeLogin(false);
  });

  document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
    $('#input').value = c.dataset.q;
    autoGrow();
    send(c.dataset.q);
  }));

  $('#input').focus();
  autoGrow();
}

init();
