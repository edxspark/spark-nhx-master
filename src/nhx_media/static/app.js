'use strict';

// ── 工具 ────────────────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const fmtNum = (n) => Number(n || 0).toLocaleString('zh-CN');
const fmtGB = (n) => (n == null ? '—' : Number(n).toFixed(1) + ' GB');
const fmtCNY = (n) => '¥' + Number(n || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 });

function fmtDur(sec) {
  if (!sec) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show ' + (kind || '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast ' + (kind || ''); }, 3200);
}

// ── 认证 ────────────────────────────────────────────────────────────────────
// token 存 sessionStorage（关标签页即失效），比 localStorage 更适合管理后台。

const TOKEN_KEY = 'nhx_token';

const AUTH = {
  mode: 'open',
  user: null,
  get token() { return sessionStorage.getItem(TOKEN_KEY) || ''; },
  set token(v) { v ? sessionStorage.setItem(TOKEN_KEY, v) : sessionStorage.removeItem(TOKEN_KEY); },
};

async function rawFetch(path, opts) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (opts || {}).headers || {});
  if (AUTH.token) headers['Authorization'] = 'Bearer ' + AUTH.token;
  const res = await fetch('/api' + path, Object.assign({}, opts || {}, { headers }));
  let body;
  try { body = await res.json(); }
  catch { throw new Error(`HTTP ${res.status}：响应不是 JSON`); }
  return { res, body };
}

async function api(path, opts) {
  let { res, body } = await rawFetch(path, opts);

  // 401 → 弹登录框，登录成功后自动重放原请求
  if (res.status === 401) {
    const ok = await promptLogin(body.error || '需要登录');
    if (!ok) throw new Error('未登录，操作已取消');
    ({ res, body } = await rawFetch(path, opts));
  }

  if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body.data;
}

let _loginResolver = null;

function promptLogin(hint) {
  $('#loginHint').textContent = hint || '请输入用户名与口令。';
  $('#loginError').hidden = true;
  $('#loginPass').value = '';
  $('#loginModal').classList.add('open');
  setTimeout(() => $('#loginUser').focus(), 50);
  return new Promise((resolve) => { _loginResolver = resolve; });
}

function closeLogin(result) {
  $('#loginModal').classList.remove('open');
  if (_loginResolver) { _loginResolver(result); _loginResolver = null; }
}

async function doLogin() {
  const username = $('#loginUser').value.trim();
  const password = $('#loginPass').value;
  if (!username || !password) {
    $('#loginError').textContent = '请填写用户名与口令';
    $('#loginError').hidden = false;
    return;
  }
  try {
    const { res, body } = await rawFetch('/auth/login', {
      method: 'POST', body: JSON.stringify({ username, password }),
    });
    if (!res.ok || !body.ok) {
      $('#loginError').textContent = body.error || '登录失败';
      $('#loginError').hidden = false;
      return;
    }
    AUTH.token = body.data.token;
    AUTH.user = { username: body.data.username, role: body.data.role };
    toast(`已登录：${body.data.username}`, 'ok');
    closeLogin(true);
    await refreshAuth();   // 登录后刷新徽标与按钮状态
  } catch (e) {
    $('#loginError').textContent = e.message;
    $('#loginError').hidden = false;
  }
}

async function doLogout() {
  try { await rawFetch('/auth/logout', { method: 'POST' }); } catch { /* 忽略 */ }
  AUTH.token = '';
  AUTH.user = null;
  toast('已退出登录');
  closeLogin(false);
  await refreshAuth();
  const active = $('.tab.active').dataset.tab;
  if (active === 'items') loadItems(); else if (active === 'validate') loadValidate();
  else if (active === 'compliance') loadCompliance(); else loadOverview();
}

async function refreshAuth() {
  let st;
  try {
    const { body } = await rawFetch('/auth/status', { method: 'GET' });
    st = body.data;
  } catch { return; }
  AUTH.mode = st.mode;
  AUTH.user = st.user;

  const badge = $('#authBadge');
  const label = { open: '免登录', protected: '写需登录', strict: '需登录' }[st.mode] || st.mode;
  badge.className = 'badge ' + st.mode;
  badge.textContent = st.authenticated ? `${st.user.username} · ${label}` : label;
  badge.title = st.mode === 'open'
    ? '免登录模式：开箱即用。启用登录：python3 src/nhx_media/auth.py mode protected'
    : (st.has_users ? '已启用登录保护' : '已启用登录保护，但尚无用户');

  $('#loginBtn').hidden = st.authenticated;
  $('#logoutBtn').hidden = !st.authenticated;
  // 免登录模式下没账号可登，隐藏按钮
  if (!st.authenticated && st.mode === 'open') $('#loginBtn').hidden = true;
}

async function initAuth() {
  await refreshAuth();
  $('#loginBtn').addEventListener('click', () => promptLogin());
  $('#logoutBtn').addEventListener('click', doLogout);
  $('#loginSubmit').addEventListener('click', doLogin);
  $('#loginCancel').addEventListener('click', () => closeLogin(false));
  $('#loginPass').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('#loginUser').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#loginPass').focus(); });
  $('#loginModal').addEventListener('click', (e) => {
    if (e.target === $('#loginModal')) closeLogin(false);
  });
}

const STATUS_LABEL = { draft: '草稿', indexed: '已登记', published: '已上线', offline: '已下架' };

// ── 全局状态 ────────────────────────────────────────────────────────────────

const state = {
  meta: null,
  series: [],
  items: [], total: 0, offset: 0, limit: 100,
  filters: { kind: '', series: '', status: '', q: '' },
  bitrate: '720p',
  editing: null,
};

// ── 标签页 ──────────────────────────────────────────────────────────────────

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + tab.dataset.tab));
    const name = tab.dataset.tab;
    if (name === 'validate') loadValidate();
    if (name === 'compliance') loadCompliance();
    if (name === 'overview') loadOverview();
  });
});

// ── 总览 ────────────────────────────────────────────────────────────────────

async function loadOverview() {
  const st = await api('/stats?bitrate=' + encodeURIComponent(state.bitrate));
  const e = st.estimate;

  $('#statCards').innerHTML = [
    card('条目总数', fmtNum(st.total_items), `视频 ${fmtNum(st.by_series['人纪'] ? st.by_series['人纪'].count + (st.by_series['天纪'] ? st.by_series['天纪'].count : 0) : 0)} ／ 音频 ${fmtNum(st.by_series['喜马拉雅音频专辑'] ? st.by_series['喜马拉雅音频专辑'].count : 0)}`),
    card('总时长', st.total_duration_hours.toFixed(1) + ' h', `${fmtNum(st.total_duration_sec)} 秒`),
    card('估算体积', fmtGB(st.estimated_gb), `码率档位 ${e.bitrate_preset}`, 'hi'),
    card('已上线', fmtNum(st.by_status.published || 0), `草稿 ${fmtNum(st.by_status.draft || 0)}`, ''),
    card('已配 OSS', fmtNum(st.with_oss), `占比 ${pct(st.with_oss, st.total_items)}`, st.with_oss ? '' : 'warn'),
    card('转录覆盖', (st.transcript_coverage * 100).toFixed(1) + '%', `${fmtNum(st.with_transcript)} 条有转录`, st.transcript_coverage < 0.8 ? 'warn' : ''),
  ].join('');

  $('#estimateCaveat').innerHTML =
    `<strong>估算口径（置信度：${esc(e.confidence)}）</strong><p>${esc(e.caveat)}</p>`;

  const c = st.cost_estimate_cny;
  $('#costBlock').innerHTML = `<div class="cards">
    ${card('标准存储 / 月', fmtCNY(c.storage_standard_month), '约 ¥0.12/GB·月')}
    ${card('低频存储 / 月', fmtCNY(c.storage_infrequent_month), '约 ¥0.08/GB·月')}
    ${card('CDN 全片播 1000 次', fmtCNY(c.cdn_full_play_once_1000), '约 ¥0.20/GB 流量', 'warn')}
  </div>
  <div class="callout warn" style="margin-top:14px">
    <strong>成本结构提示</strong>
    <p>CDN 流量通常是存储费用的数百倍。按当前估算，存储约 ${fmtCNY(c.storage_standard_month)}/月，
    而 1000 次全片播放的流量费约 ${fmtCNY(c.cdn_full_play_once_1000)}。
    建议：分片 HLS + 多码率自适应 + 签名 URL 防盗链，避免整段 MP4 直下。</p>
  </div>`;

  const bt = Object.entries(st.storage_gb_by_bitrate);
  const maxGB = Math.max(...bt.map(([, v]) => v), 1);
  $('#bitrateBlock').innerHTML = `<div class="tablewrap"><table>
    <thead><tr><th>档位</th><th class="num">估算体积</th><th>占比</th><th class="num">标准存储 / 月</th></tr></thead>
    <tbody>${bt.map(([name, gb]) => `<tr>
      <td>${esc(name)}${name === state.bitrate ? ' <span class="badge indexed">当前</span>' : ''}</td>
      <td class="num">${fmtGB(gb)}</td>
      <td><div style="background:#2a2f3a;border-radius:4px;height:7px;width:100%;max-width:240px">
        <div style="background:var(--accent);height:7px;border-radius:4px;width:${(gb / maxGB * 100).toFixed(1)}%"></div>
      </div></td>
      <td class="num">${fmtCNY(gb * 0.12)}</td>
    </tr>`).join('')}</tbody></table></div>`;

  $('#seriesBlock').innerHTML = `<div class="tablewrap"><table>
    <thead><tr><th>系列</th><th>类型</th><th class="num">条目</th><th class="num">时长</th>
    <th class="num">估算体积</th><th class="num">已上线</th><th>索引文件</th></tr></thead>
    <tbody>${state.series.map((s) => {
      const b = st.by_series[s.name] || { bytes: 0 };
      return `<tr>
        <td>${esc(s.name)}</td>
        <td>${s.kind === 'video' ? '视频' : '音频'}</td>
        <td class="num">${fmtNum(s.item_count)}</td>
        <td class="num">${fmtDur(s.total_duration_sec)}</td>
        <td class="num">${fmtGB(b.bytes / 1024 ** 3)}</td>
        <td class="num">${fmtNum(s.published)}</td>
        <td><code>${esc(s.path)}</code></td>
      </tr>`;
    }).join('')}</tbody></table></div>`;
}

function card(k, v, note, cls) {
  return `<div class="card ${cls || ''}"><div class="k">${esc(k)}</div>
    <div class="v">${v}</div>${note ? `<div class="note">${esc(note)}</div>` : ''}</div>`;
}
function pct(a, b) { return b ? (a / b * 100).toFixed(1) + '%' : '0%'; }

// ── 资源清单 ────────────────────────────────────────────────────────────────

async function loadItems() {
  const p = new URLSearchParams();
  if (state.filters.kind) p.set('kind', state.filters.kind);
  if (state.filters.series) p.set('series', state.filters.series);
  if (state.filters.status) p.set('status', state.filters.status);
  if (state.filters.q) p.set('q', state.filters.q);
  p.set('limit', state.limit);
  p.set('offset', state.offset);

  const res = await api('/items?' + p.toString());
  state.items = res.items;
  state.total = res.total;

  $('#itemsMeta').textContent =
    `共 ${fmtNum(res.total)} 条，显示第 ${res.total ? state.offset + 1 : 0}–${Math.min(state.offset + state.limit, res.total)} 条`;

  $('#itemsTable').querySelector('tbody').innerHTML = res.items.map((it) => `
    <tr data-id="${esc(it.id)}">
      <td><code>${esc(it.id)}</code></td>
      <td class="title" title="${esc(it.title)}">${esc(it.title)}</td>
      <td>${esc(it.subject || '—')}</td>
      <td><span class="badge ${esc(it.status)}">${esc(STATUS_LABEL[it.status] || it.status)}</span></td>
      <td class="${it.oss_key ? 'yes' : 'no'}">${it.oss_key ? '✓' : '—'}</td>
      <td class="num">${fmtDur(it.duration_sec)}</td>
      <td class="num">${(it.segments || []).length || '—'}</td>
      <td class="${it.transcript_key ? 'yes' : 'no'}">${it.transcript_key ? '✓' : '—'}</td>
      <td><button class="btn" data-edit="${esc(it.id)}">编辑</button></td>
    </tr>`).join('');

  $$('[data-edit]').forEach((b) => b.addEventListener('click', () => openDrawer(b.dataset.edit)));

  $('#prev').disabled = state.offset === 0;
  $('#next').disabled = state.offset + state.limit >= state.total;
  $('#pageInfo').textContent = `第 ${Math.floor(state.offset / state.limit) + 1} / ${Math.max(1, Math.ceil(state.total / state.limit))} 页`;
}

// ── 编辑抽屉 ────────────────────────────────────────────────────────────────

async function openDrawer(id) {
  const found = await api('/items/' + encodeURIComponent(id));
  const it = found.item;
  state.editing = { kind: found.kind, series_id: found.series_id, item: it };

  $('#drawerTitle').textContent = it.title || it.id;
  const segs = it.segments && it.segments.length ? it.segments : [{ start: 0, end: 0, topic: '' }];

  $('#drawerBody').innerHTML = `
    <div class="field"><label>ID</label>
      <input value="${esc(it.id)}" disabled>
      <div class="hint">ID 不可修改（kebab-case）</div></div>

    <div class="field"><label>标题</label>
      <input id="dTitle" value="${esc(it.title || '')}"></div>

    <div class="row2">
      <div class="field"><label>科目</label>
        <input id="dSubject" value="${esc(it.subject || '')}"></div>
      <div class="field"><label>状态</label>
        <select id="dStatus">${state.meta.statuses.map((s) =>
          `<option value="${s}" ${s === it.status ? 'selected' : ''}>${esc(state.meta.status_labels[s])}</option>`).join('')}
        </select></div>
    </div>

    <div class="field"><label>OSS Key</label>
      <input id="dOss" value="${esc(it.oss_key || '')}" placeholder="nhx/renji/zhenjiu/001.mp4">
      <div class="hint">媒体本体存 OSS，索引只存 Key。规范：<code>nhx/&lt;系列&gt;/&lt;科目&gt;/&lt;三位序号&gt;.&lt;ext&gt;</code></div></div>

    <div class="row2">
      <div class="field"><label>时长（秒）</label>
        <input id="dDur" type="number" min="0" value="${it.duration_sec || ''}"></div>
      <div class="field"><label>转录 Key</label>
        <input id="dTranscript" value="${esc(it.transcript_key || '')}" placeholder="nhx/renji/zhenjiu/001.transcript.json"></div>
    </div>

    <div class="row2">
      <div class="field"><label>来源类型</label>
        <select id="dSourceType">${state.meta.source_types.map((s) =>
          `<option value="${s}" ${s === it.source_type ? 'selected' : ''}>${s}</option>`).join('')}
        </select></div>
      <div class="field"><label>授权状态</label>
        <select id="dLicense">${state.meta.license_statuses.map((s) =>
          `<option value="${s}" ${s === it.license_status ? 'selected' : ''}>${s}</option>`).join('')}
        </select></div>
    </div>

    <div class="field"><label>来源 URL</label>
      <input id="dSourceUrl" value="${esc(it.source_url || '')}">
      <div class="hint">用于下架定位，务必填写</div></div>

    <div class="field"><label>章节标注（segments）</label>
      <div class="seg-row seg-head"><span>开始(s)</span><span>结束(s)</span><span>主题</span><span></span></div>
      <div id="segList"></div>
      <button class="btn" id="addSeg" style="margin-top:6px">+ 添加章节</button>
      <div class="hint">已上线条目至少需要 1 个章节；不得重叠，不得超过总时长。</div></div>

    <div class="actions">
      <button class="btn primary" id="save">保存</button>
      <button class="btn" id="publish">标记上线</button>
      <button class="btn danger" id="del">删除</button>
    </div>`;

  const list = $('#segList');
  const addRow = (s) => {
    const row = document.createElement('div');
    row.className = 'seg-row';
    row.innerHTML = `<input type="number" min="0" class="sStart" value="${s.start ?? 0}">
      <input type="number" min="0" class="sEnd" value="${s.end ?? 0}">
      <input type="text" class="sTopic" value="${esc(s.topic || '')}" placeholder="主题">
      <button class="btn sDel">×</button>`;
    row.querySelector('.sDel').addEventListener('click', () => row.remove());
    list.appendChild(row);
  };
  segs.forEach(addRow);
  $('#addSeg').addEventListener('click', () => addRow({ start: 0, end: 0, topic: '' }));

  $('#save').addEventListener('click', saveItem);
  $('#publish').addEventListener('click', async () => {
    try { await saveItem(); await api(`/items/${encodeURIComponent(it.id)}/status`,
      { method: 'POST', body: JSON.stringify({ status: 'published' }) });
      toast('已上线', 'ok'); closeDrawer(); loadItems();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#del').addEventListener('click', async () => {
    if (!confirm(`确认删除 ${it.id}？`)) return;
    try { await api('/items/' + encodeURIComponent(it.id), { method: 'DELETE' });
      toast('已删除', 'ok'); closeDrawer(); loadItems(); }
    catch (e) { toast(e.message, 'err'); }
  });

  $('#drawer').classList.add('open');
  $('#scrim').classList.add('open');
}

function collectSegments() {
  return $$('#segList .seg-row').map((r) => ({
    start: Number(r.querySelector('.sStart').value || 0),
    end: Number(r.querySelector('.sEnd').value || 0),
    topic: r.querySelector('.sTopic').value.trim(),
  })).filter((s) => s.end > s.start || s.topic);
}

async function saveItem() {
  const ed = state.editing;
  if (!ed) return;
  const item = Object.assign({}, ed.item, {
    title: $('#dTitle').value.trim(),
    subject: $('#dSubject').value.trim(),
    status: $('#dStatus').value,
    oss_key: $('#dOss').value.trim() || null,
    duration_sec: Number($('#dDur').value || 0) || null,
    transcript_key: $('#dTranscript').value.trim() || null,
    source_type: $('#dSourceType').value,
    license_status: $('#dLicense').value,
    source_url: $('#dSourceUrl').value.trim() || null,
    segments: collectSegments(),
  });
  await api('/items', { method: 'POST', body: JSON.stringify({
    kind: ed.kind, series_id: ed.series_id, item,
  })});
  toast('已保存', 'ok');
  closeDrawer();
  loadItems();
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#scrim').classList.remove('open');
  state.editing = null;
}
$('#drawerClose').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

// ── 校验 ────────────────────────────────────────────────────────────────────

async function loadValidate() {
  const v = await api('/validate');
  $('#validateSummary').innerHTML = `<div class="cards">
    ${card('已检查', fmtNum(v.items_checked), '条条目')}
    ${card('错误', fmtNum(v.errors.length), '阻塞上线', v.errors.length ? 'warn' : '')}
    ${card('警告', fmtNum(v.warnings.length), '建议修复')}
    ${card('结果', v.passed ? '通过' : '未通过', v.passed ? '无阻塞项' : '存在错误', v.passed ? '' : 'warn')}
  </div>`;

  if (!v.errors.length && !v.warnings.length) {
    $('#validateBody').innerHTML = '<div class="callout" style="margin-top:14px">✅ 全部通过</div>';
    return;
  }
  const section = (title, arr, cls) => arr.length ? `
    <h2>${title}（${arr.length}）</h2>
    ${arr.slice(0, 100).map((f) => `<div class="finding ${cls}">
      <span class="who">${esc(f.item_id)}</span> — ${esc(f.message)}</div>`).join('')}
    ${arr.length > 100 ? `<p class="muted">仅显示前 100 条，共 ${arr.length} 条。</p>` : ''}` : '';

  $('#validateBody').innerHTML =
    section('错误', v.errors, 'error') + section('警告', v.warnings, 'warning');
}

// ── 合规 ────────────────────────────────────────────────────────────────────

async function loadCompliance() {
  const st = await api('/stats?bitrate=' + encodeURIComponent(state.bitrate));
  $('#licenseBlock').innerHTML = `<div class="tablewrap"><table>
    <thead><tr><th>授权状态</th><th class="num">条目数</th><th>操作</th></tr></thead>
    <tbody>${Object.entries(st.by_license).map(([k, n]) => `<tr>
      <td>${esc(k)}</td><td class="num">${fmtNum(n)}</td>
      <td><button class="btn" data-quick="${esc(k)}">按此状态批量下架</button></td>
    </tr>`).join('')}</tbody></table></div>`;
  $$('[data-quick]').forEach((b) => b.addEventListener('click', () => {
    $('#offLicense').value = b.dataset.quick;
    $('#offSource').value = '';
    toast('已填入授权状态，确认后点「执行下架」');
  }));
}

$('#offApply').addEventListener('click', async () => {
  const license_status = $('#offLicense').value || null;
  const source_type = $('#offSource').value || null;
  if (!license_status && !source_type) { toast('必须至少指定一项', 'err'); return; }
  if (!confirm('确认执行批量下架？此操作会把匹配条目置为「已下架」。')) return;
  try {
    const r = await api('/offline', { method: 'POST', body: JSON.stringify({ license_status, source_type }) });
    $('#offResult').innerHTML = `<div class="callout warn">
      <strong>已下架 ${r.count} 条</strong>
      <p>${r.offlined.slice(0, 30).map(esc).join('、')}${r.count > 30 ? ' …' : ''}</p></div>`;
    toast(`已下架 ${r.count} 条`, 'ok');
    loadCompliance();
  } catch (e) { toast(e.message, 'err'); }
});

// ── 初始化 ──────────────────────────────────────────────────────────────────

async function init() {
  await initAuth();
  state.meta = await api('/meta');
  state.series = await api('/series');

  const bSel = $('#bitrate');
  bSel.innerHTML = state.meta.bitrate_presets.map((b) =>
    `<option value="${b}" ${b === state.bitrate ? 'selected' : ''}>${b}</option>`).join('');
  bSel.addEventListener('change', () => { state.bitrate = bSel.value; loadOverview(); });

  $('#fKind').innerHTML = '<option value="">全部</option>' +
    state.meta.kinds.map((k) => `<option value="${k}">${k === 'video' ? '视频' : '音频'}</option>`).join('');
  $('#fSeries').innerHTML = '<option value="">全部</option>' +
    state.series.map((s) => `<option value="${s.series_id}">${esc(s.name)}</option>`).join('');
  $('#fStatus').innerHTML = '<option value="">全部</option>' +
    state.meta.statuses.map((s) => `<option value="${s}">${esc(state.meta.status_labels[s])}</option>`).join('');

  $('#offLicense').innerHTML = '<option value="">（不指定）</option>' +
    state.meta.license_statuses.map((s) => `<option value="${s}">${s}</option>`).join('');
  $('#offSource').innerHTML = '<option value="">（不指定）</option>' +
    state.meta.source_types.map((s) => `<option value="${s}">${s}</option>`).join('');

  $('#fApply').addEventListener('click', () => {
    state.filters = {
      kind: $('#fKind').value, series: $('#fSeries').value,
      status: $('#fStatus').value, q: $('#fQuery').value.trim(),
    };
    state.offset = 0;
    loadItems();
  });
  $('#fReset').addEventListener('click', () => {
    $('#fKind').value = ''; $('#fSeries').value = '';
    $('#fStatus').value = ''; $('#fQuery').value = '';
    state.filters = { kind: '', series: '', status: '', q: '' };
    state.offset = 0;
    loadItems();
  });
  $('#fQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#fApply').click(); });
  $('#prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); loadItems(); });
  $('#next').addEventListener('click', () => { state.offset += state.limit; loadItems(); });
  $('#refresh').addEventListener('click', () => {
    const active = $('.tab.active').dataset.tab;
    if (active === 'validate') loadValidate();
    else if (active === 'compliance') loadCompliance();
    else if (active === 'items') loadItems();
    else loadOverview();
    toast('已刷新');
  });

  await loadOverview();
  await loadItems();
}

init().catch((e) => toast('初始化失败：' + e.message, 'err'));
