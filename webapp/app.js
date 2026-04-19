/* ═══════════════════════════════════════════════════════════════════
   Epistylion Console — app.js
   Vanilla JS, no dependencies.
═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Config ────────────────────────────────────────────────────────
const DEFAULT_CFG = {
  url:     '',
  key:     '',
  model:   'local-model',
  refresh: 10,
};

let cfg = { ...DEFAULT_CFG, ...JSON.parse(localStorage.getItem('epistylion_cfg') || '{}') };

function saveCfg() {
  localStorage.setItem('epistylion_cfg', JSON.stringify(cfg));
}

// ── API helpers ───────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (cfg.key) headers['X-Api-Key'] = cfg.key;
  const res = await fetch(cfg.url + path, { headers, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, type = 'info', ms = 3000) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ── Tabs ──────────────────────────────────────────────────────────
function switchTab(id) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === `tab-${id}`));
  if (id === 'metrics') loadMetrics();
  if (id === 'tools')   loadTools();
}

function initTabs() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

// ── Status strip ──────────────────────────────────────────────────
function setStatus(state, label) {
  const dot  = document.getElementById('status-dot');
  const lbl  = document.getElementById('status-label');
  const url  = document.getElementById('status-url');
  dot.className = { ok: 'dot-ok', err: 'dot-err', warn: 'dot-warn' }[state] ?? 'dot-unknown';
  lbl.textContent = label;
  url.textContent = cfg.url || '—';
}

// ── Helpers ───────────────────────────────────────────────────────
function fmt(n) {
  if (n == null || n === -1) return '—';
  if (n >= 3600) return `${Math.floor(n/3600)}h ${Math.floor((n%3600)/60)}m`;
  if (n >= 60)   return `${Math.floor(n/60)}m ${Math.floor(n%60)}s`;
  return `${Math.round(n)}s`;
}

function fmtMs(n) {
  if (n == null || n <= 0) return '—';
  return `${n.toFixed(1)} ms`;
}

function el(id) { return document.getElementById(id); }

// ══════════════════════════════════════════════════════════════════
// DASHBOARD
// ══════════════════════════════════════════════════════════════════

let dashRefreshTimer = null;

async function loadDashboard() {
  const btn = el('refresh-btn');
  btn.classList.add('spinning');

  try {
    const [health, status] = await Promise.all([
      apiFetch('/health'),
      apiFetch('/v1/status'),
    ]);

    const isOk = health.status === 'ok';
    setStatus(isOk ? 'ok' : 'warn', health.status.toUpperCase());

    el('kpi-status-val').textContent = health.status.toUpperCase();
    el('kpi-status-val').style.color = isOk ? 'var(--green)' : 'var(--amber)';
    el('kpi-uptime-val').textContent  = fmt(status.uptime_s);
    el('kpi-tools-val').textContent   = status.total_tools ?? '—';
    el('kpi-servers-val').textContent = `${health.servers_connected}/${health.servers_total}`;

    el('llm-url').textContent   = status.llm_backend   || '—';
    el('llm-model').textContent = status.llm_model     || '—';
    el('llm-steps').textContent = status.max_steps     ?? '—';
    el('llm-auth').textContent  = status.auth_enabled  ? '✓ enabled' : '✗ disabled';
    el('llm-rl').textContent    = status.rate_limit_rpm ? `${status.rate_limit_rpm} req/min` : 'disabled';

    const servers = status.servers || {};
    const listEl  = el('servers-list');
    listEl.innerHTML = '';

    if (!Object.keys(servers).length) {
      listEl.innerHTML = '<div class="empty-state">No servers connected</div>';
    } else {
      for (const [name, info] of Object.entries(servers)) {
        const row = document.createElement('div');
        row.className = 'server-row';
        const tags = (info.tools || []).slice(0, 8).map(t => `<span class="tool-tag">${t}</span>`).join('');
        const more = info.tool_count > 8 ? `<span class="tool-tag">+${info.tool_count - 8} more</span>` : '';
        row.innerHTML = `
          <div class="dot-${info.connected ? 'ok' : 'err'}" style="flex-shrink:0"></div>
          <div style="flex:1">
            <div class="server-name">${name}</div>
            <div class="server-tools-list">${tags}${more}</div>
          </div>
          <span class="server-tool-count">${info.tool_count} tools</span>
        `;
        listEl.appendChild(row);
      }
    }

    el('last-refresh').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    setStatus('err', 'Unreachable');
    toast(`${e.message}`, 'err');
  } finally {
    btn.classList.remove('spinning');
  }
}

function initDashboard() {
  el('refresh-btn').addEventListener('click', loadDashboard);

  el('auto-refresh-toggle').addEventListener('change', e => {
    clearInterval(dashRefreshTimer);
    if (e.target.checked) {
      dashRefreshTimer = setInterval(loadDashboard, cfg.refresh * 1000);
    }
  });

  if (cfg.url) loadDashboard();
}

// ══════════════════════════════════════════════════════════════════
// CHAT
// ══════════════════════════════════════════════════════════════════

let chatHistory = [];

function appendMsg(role, content, streaming = false) {
  const wrap = el('chat-messages');
  const div  = document.createElement('div');
  div.className = `msg ${role}`;
  const labels = { user: 'You', assistant: 'Assistant', tool: 'Tool', error: 'Error' };
  div.innerHTML = `
    <div class="msg-role">${labels[role] ?? role}</div>
    <div class="msg-body${streaming ? ' typing-cursor' : ''}">${escHtml(content)}</div>
  `;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  return div.querySelector('.msg-body');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

async function sendChat() {
  const input   = el('chat-input');
  const sendBtn = el('chat-send-btn');
  const text    = input.value.trim();
  if (!text) return;

  const useStream = el('stream-toggle').checked;
  const sysPrompt = el('system-prompt').value.trim();

  input.value = '';
  sendBtn.disabled = true;
  el('send-label').textContent = 'Sending…';

  appendMsg('user', text);
  chatHistory.push({ role: 'user', content: text });

  const messages = [];
  if (sysPrompt) messages.push({ role: 'system', content: sysPrompt });
  messages.push(...chatHistory);

  const body    = JSON.stringify({ model: cfg.model, messages, stream: useStream });
  const headers = { 'Content-Type': 'application/json' };
  if (cfg.key) headers['X-Api-Key'] = cfg.key;

  try {
    if (useStream) {
      const resp = await fetch(cfg.url + '/v1/chat/completions', { method: 'POST', headers, body });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);

      const bodyEl  = appendMsg('assistant', '', true);
      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let fullText = '', buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') break;
          try {
            const chunk = JSON.parse(data);
            const delta = chunk.choices?.[0]?.delta?.content || '';
            fullText   += delta;
            bodyEl.innerHTML = escHtml(fullText);
            bodyEl.classList.add('typing-cursor');
            el('chat-messages').scrollTop = 99999;
          } catch {}
        }
      }
      bodyEl.classList.remove('typing-cursor');
      chatHistory.push({ role: 'assistant', content: fullText });
    } else {
      const resp = await fetch(cfg.url + '/v1/chat/completions', { method: 'POST', headers, body });
      const data = await resp.json();
      if (data.error) throw new Error(data.error.message);
      const reply = data.choices?.[0]?.message?.content || '';
      appendMsg('assistant', reply);
      chatHistory.push({ role: 'assistant', content: reply });
      if (data._mcp_tool_calls?.length) {
        for (const tc of data._mcp_tool_calls) {
          appendMsg('tool', `${tc.tool}(${JSON.stringify(tc.args).slice(0,120)}) → ${String(tc.result).slice(0,200)}`);
        }
      }
    }
  } catch (e) {
    appendMsg('error', e.message);
  } finally {
    sendBtn.disabled = false;
    el('send-label').textContent = 'Send';
    input.focus();
  }
}

function initChat() {
  el('chat-send-btn').addEventListener('click', sendChat);
  el('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendChat();
  });
  el('clear-chat-btn').addEventListener('click', () => {
    chatHistory = [];
    el('chat-messages').innerHTML = '';
    toast('Conversation cleared', 'info', 1500);
  });
}

// ══════════════════════════════════════════════════════════════════
// TOOLS
// ══════════════════════════════════════════════════════════════════

let allTools = [], activeServer = 'all';

async function loadTools() {
  const qualified = el('qualified-toggle').checked;
  try {
    const data = await apiFetch(`/v1/tools${qualified ? '?qualified=true' : ''}`);
    allTools = data.tools || [];
    buildServerTabs(data.by_server || {});
    renderTools(allTools);
  } catch (e) {
    toast(`Tools error: ${e.message}`, 'err');
    el('tools-grid').innerHTML = '<div class="empty-state">Failed to load tools</div>';
  }
}

function buildServerTabs(byServer) {
  const container = el('tools-server-tabs');
  container.innerHTML = '';
  for (const s of ['all', ...Object.keys(byServer)]) {
    const btn = document.createElement('button');
    btn.className = `server-tab-btn${s === activeServer ? ' active' : ''}`;
    btn.textContent = s === 'all' ? `All (${allTools.length})` : `${s} (${(byServer[s]||[]).length})`;
    btn.addEventListener('click', () => {
      activeServer = s;
      document.querySelectorAll('.server-tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      filterTools();
    });
    container.appendChild(btn);
  }
}

function filterTools() {
  const query = el('tools-search').value.toLowerCase();
  const filtered = allTools.filter(t => {
    const name = t.function?.name || '', desc = t.function?.description || '';
    const matchQ   = !query || name.toLowerCase().includes(query) || desc.toLowerCase().includes(query);
    const matchSrv = activeServer === 'all' || name.startsWith(activeServer + '__') || name.startsWith(activeServer + '_');
    return matchQ && matchSrv;
  });
  renderTools(filtered);
}

function renderTools(tools) {
  const grid = el('tools-grid');
  grid.innerHTML = '';
  if (!tools.length) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">No tools found</div>';
    return;
  }
  for (const t of tools) {
    const fn    = t.function || {};
    const name  = fn.name || '?';
    const desc  = fn.description || '—';
    const props = fn.parameters?.properties || {};
    const req   = fn.parameters?.required || [];
    const serverMatch = name.match(/^(.+?)__/);
    const serverLabel = serverMatch ? serverMatch[1] : 'unknown';

    const propRows = Object.entries(props).map(([k, v]) =>
      `<tr>
        <td style="padding:3px 0;color:var(--amber-bright);font-size:11px">${k}${req.includes(k) ? ' <span style="color:var(--red)">*</span>' : ''}</td>
        <td style="padding:3px 0 3px 12px;color:var(--muted-2);font-size:10px">${v.type||''}${v.description?' — '+v.description:''}</td>
      </tr>`
    ).join('');

    const card = document.createElement('div');
    card.className = 'tool-card';
    card.innerHTML = `
      <div class="tool-card-header">
        <div>
          <div class="tool-card-name">${name}</div>
          <div class="tool-card-server">${serverLabel}</div>
        </div>
        <span class="tool-card-chevron">▶</span>
      </div>
      <div class="tool-card-body">
        <div class="tool-desc">${desc}</div>
        ${propRows
          ? `<div style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">Parameters</div>
             <table style="width:100%;border-collapse:collapse">${propRows}</table>`
          : '<div class="tool-desc" style="font-size:11px">No parameters</div>'
        }
        <div class="tool-card-actions">
          <button class="btn-ghost copy-json-btn" style="font-size:10px">Copy JSON</button>
        </div>
      </div>
    `;
    card.querySelector('.tool-card-header').addEventListener('click', () => card.classList.toggle('open'));
    card.querySelector('.copy-json-btn').addEventListener('click', e => {
      e.stopPropagation();
      navigator.clipboard.writeText(JSON.stringify(t, null, 2)).then(() => toast(`Copied ${name}`, 'ok', 1500));
    });
    grid.appendChild(card);
  }
}

function initTools() {
  el('tools-search').addEventListener('input', filterTools);
  el('qualified-toggle').addEventListener('change', loadTools);
  el('reload-tools-btn').addEventListener('click', loadTools);
}

// ══════════════════════════════════════════════════════════════════
// METRICS + CHARTS
// ══════════════════════════════════════════════════════════════════

let metricsTimer = null;

async function loadMetrics() {
  try {
    const m = await apiFetch('/metrics');

    el('m-req-total').textContent  = m.requests_total   ?? '—';
    el('m-req-ok').textContent     = m.requests_ok      ?? '—';
    el('m-req-err').textContent    = m.requests_error   ?? '—';
    el('m-tool-total').textContent = m.tool_calls_total ?? '—';

    drawLatencyChart(el('chart-latency'),      el('latency-legend'),      m.latency_ms      || {});
    drawLatencyChart(el('chart-tool-latency'), el('tool-latency-legend'), m.tool_latency_ms || {});
    drawBarChart(el('chart-by-path'), m.by_path || {}, 'var(--blue)');
    drawBarChart(el('chart-by-tool'), m.by_tool || {}, 'var(--amber)');

    const pillGrid = el('extra-metrics-grid');
    pillGrid.innerHTML = '';
    const extras = {
      'Stream requests': m.stream_requests,
      'Auth failures':   m.requests_auth_fail,
      'Rate limited':    m.requests_rate_limit,
      'Tool errors':     m.tool_calls_error,
      'Uptime':          fmt(m.uptime_s),
    };
    for (const [k, v] of Object.entries(extras)) {
      const pill = document.createElement('div');
      pill.className = 'metrics-pill';
      pill.innerHTML = `<span class="pill-key">${k}</span><span class="pill-val">${v ?? '—'}</span>`;
      pillGrid.appendChild(pill);
    }
  } catch (e) {
    toast(`Metrics error: ${e.message}`, 'err');
  }
}

function drawLatencyChart(canvas, legendEl, data) {
  const keys   = ['min', 'p50', 'p95', 'p99', 'max'];
  const colors = ['#4a566e', '#5b8dee', '#f0a000', '#ff4d4d', '#6a7a96'];
  const values = keys.map(k => data[k] || 0);
  const maxVal = Math.max(...values, 1);
  const W = canvas.offsetWidth || 400, H = parseInt(canvas.getAttribute('height')) || 180;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  const barW = Math.floor((W - 60) / keys.length) - 8, padL = 40, padB = 28, chartH = H - padB - 10;

  ctx.strokeStyle = '#1f2638'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = 10 + chartH - (chartH * i / 4);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillStyle = '#4a566e'; ctx.font = '9px IBM Plex Mono,monospace'; ctx.textAlign = 'right';
    ctx.fillText(fmtMs(maxVal * i / 4), padL - 4, y + 3);
  }
  for (let i = 0; i < keys.length; i++) {
    const x = padL + i * (barW + 8) + 8, h = Math.max(2, (values[i] / maxVal) * chartH), y = 10 + chartH - h;
    ctx.fillStyle = colors[i] + '33'; ctx.fillRect(x, y, barW, h);
    ctx.fillStyle = colors[i];        ctx.fillRect(x, y, barW, 3);
    ctx.fillStyle = '#6a7a96'; ctx.font = '9px IBM Plex Mono,monospace'; ctx.textAlign = 'center';
    ctx.fillText(keys[i], x + barW / 2, H - 8);
    if (values[i] > 0) {
      ctx.fillStyle = '#d8e0f0'; ctx.font = '8px IBM Plex Mono,monospace';
      ctx.fillText(values[i].toFixed(0), x + barW / 2, y - 4);
    }
  }
  if (legendEl) {
    legendEl.innerHTML = keys.map((k, i) =>
      `<div class="legend-item"><div class="legend-dot" style="background:${colors[i]}"></div><span>${k}: ${fmtMs(values[i])}</span></div>`
    ).join('');
  }
}

function drawBarChart(canvas, data, color = 'var(--amber)') {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]).slice(0, 12);
  const W = canvas.offsetWidth || 400, H = parseInt(canvas.getAttribute('height')) || 200;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  if (!entries.length) {
    ctx.fillStyle = '#4a566e'; ctx.font = '12px IBM Plex Mono,monospace'; ctx.textAlign = 'center';
    ctx.fillText('No data', W / 2, H / 2); return;
  }
  const maxVal = Math.max(...entries.map(e => e[1]), 1);
  const barH = Math.floor((H - 20) / entries.length) - 4;
  const padL = Math.min(160, W * 0.4), chartW = W - padL - 50;
  for (let i = 0; i < entries.length; i++) {
    const [key, val] = entries[i], y = 10 + i * (barH + 4), w = Math.max(2, (val / maxVal) * chartW);
    ctx.fillStyle = '#1a1f2e';    ctx.fillRect(padL, y, chartW, barH);
    ctx.fillStyle = color + '55'; ctx.fillRect(padL, y, w, barH);
    ctx.fillStyle = color;        ctx.fillRect(padL + w - 3, y, 3, barH);
    const shortKey = key.length > 22 ? '…' + key.slice(-20) : key;
    ctx.fillStyle = '#8897b5'; ctx.font = '10px IBM Plex Mono,monospace'; ctx.textAlign = 'right';
    ctx.fillText(shortKey, padL - 6, y + barH / 2 + 3);
    ctx.fillStyle = '#d8e0f0'; ctx.textAlign = 'left';
    ctx.fillText(val, padL + w + 6, y + barH / 2 + 3);
  }
}

function initMetrics() {
  el('reload-metrics-btn').addEventListener('click', loadMetrics);
  el('metrics-auto-toggle').addEventListener('change', e => {
    clearInterval(metricsTimer);
    if (e.target.checked) metricsTimer = setInterval(loadMetrics, 5000);
  });
}

// ══════════════════════════════════════════════════════════════════
// SETTINGS
// ══════════════════════════════════════════════════════════════════

function initSettings() {
  el('cfg-url').value     = cfg.url;
  el('cfg-key').value     = cfg.key;
  el('cfg-model').value   = cfg.model;
  el('cfg-refresh').value = cfg.refresh;

  el('save-settings-btn').addEventListener('click', () => {
    cfg.url     = el('cfg-url').value.trim().replace(/\/$/, '');
    cfg.key     = el('cfg-key').value.trim();
    cfg.model   = el('cfg-model').value.trim() || 'local-model';
    cfg.refresh = parseInt(el('cfg-refresh').value) || 10;
    saveCfg();
    el('status-url').textContent = cfg.url || '—';
    const msg = el('settings-saved-msg');
    msg.classList.remove('hidden');
    setTimeout(() => msg.classList.add('hidden'), 2000);
    toast('Saved — connecting…', 'ok', 2000);
    loadDashboard();
    switchTab('dashboard');
  });

  el('reset-settings-btn').addEventListener('click', () => {
    cfg = { ...DEFAULT_CFG };
    saveCfg();
    el('cfg-url').value     = cfg.url;
    el('cfg-key').value     = cfg.key;
    el('cfg-model').value   = cfg.model;
    el('cfg-refresh').value = cfg.refresh;
    toast('Reset to defaults', 'info', 2000);
  });

  el('toggle-key-vis').addEventListener('click', () => {
    const inp = el('cfg-key');
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    el('toggle-key-vis').textContent = show ? 'Hide' : 'Show';
  });
}

// ══════════════════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initSettings();
  initDashboard();
  initChat();
  initTools();
  initMetrics();

  el('status-url').textContent = cfg.url || '—';

  // Se non c'è URL configurata, vai dritto alle Settings
  if (!cfg.url) {
    switchTab('settings');
    el('cfg-url').focus();
  }
});

window.addEventListener('resize', () => {
  const tab = document.querySelector('.tab.active');
  if (tab?.id === 'tab-metrics') loadMetrics();
});