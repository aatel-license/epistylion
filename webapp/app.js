/* ═══════════════════════════════════════════════════════════════════
   Epistylion Console — app.js
   Vanilla JS, no dependencies.
═══════════════════════════════════════════════════════════════════ */

'use strict';

// ── Config ────────────────────────────────────────────────────────
const DEFAULT_CFG = {
  // Connection — Epistylion server
  url:          '',
  key:          '',
  // LLM Backend — sent to PATCH /v1/config on save
  llm_url:      '',
  llm_model:    '',
  llm_key:      '',
  max_steps:    20,
  rate_limit:   0,
  // Agent
  model:        '',       // per-request model override (empty = use llm_model)
  refresh:      10,
};

let cfg = { ...DEFAULT_CFG, ...JSON.parse(localStorage.getItem('epistylion_cfg') || '{}') };

function saveCfg() {
  localStorage.setItem('epistylion_cfg', JSON.stringify(cfg));
}

// ── Disabled tools (per-tool function-call toggle) ─────────────────
let disabledTools = new Set(JSON.parse(localStorage.getItem('epistylion_disabled_tools') || '[]'));

function saveDisabledTools() {
  localStorage.setItem('epistylion_disabled_tools', JSON.stringify([...disabledTools]));
}

function isToolEnabled(name) { return !disabledTools.has(name); }

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
  if (id === 'metrics')  loadMetrics();
  if (id === 'tools')    loadTools();
  // Settings: just re-populate fields from cfg (localStorage) — no server fetch.
  // Use the "Fetch from server" button to pull live values explicitly.
  if (id === 'settings') _populateSettingsFromCfg();
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

    // skills
    const skillsEl = el('llm-skills');
    if (status.skills && status.skills.length) {
      skillsEl.innerHTML = status.skills
        .map(s => `<span class="tool-tag" style="color:var(--amber)">${s}</span>`)
        .join(' ');
    } else {
      skillsEl.textContent = '—';
    }
    // refresh skill selector with latest list
    if (status.skills) {
      const sel = el('skill-select');
      const current = sel.value;
      sel.innerHTML = '<option value="">none</option>';
      for (const name of status.skills) {
        const opt = document.createElement('option');
        opt.value = name; opt.textContent = name;
        sel.appendChild(opt);
      }
      if (current && status.skills.includes(current)) sel.value = current;
    }

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

function appendMsg(role, content, streaming = false, skillUsed = '') {
  const wrap = el('chat-messages');
  const div  = document.createElement('div');
  div.className = `msg ${role}`;
  const labels = { user: 'You', assistant: 'Assistant', tool: 'Tool', error: 'Error' };
  const skillBadge = (role === 'assistant' && skillUsed)
    ? `<span class="skill-badge">⬡ ${skillUsed}</span>`
    : '';
  div.innerHTML = `
    <div class="msg-role">${labels[role] ?? role}${skillBadge}</div>
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
  const activeSkill = el('skill-select').value;

  input.value = '';
  sendBtn.disabled = true;
  el('send-label').textContent = 'Sending…';

  appendMsg('user', text);
  chatHistory.push({ role: 'user', content: text });

  const messages = [];
  if (sysPrompt) messages.push({ role: 'system', content: sysPrompt });
  messages.push(...chatHistory);

  const payload = { model: cfg.model || cfg.llm_model || 'local-model', messages, stream: useStream };
  if (activeSkill) payload.skill = activeSkill;
  if (disabledTools.size) payload.disabled_tools = [...disabledTools];

  const body    = JSON.stringify(payload);
  const headers = { 'Content-Type': 'application/json' };
  if (cfg.key) headers['X-Api-Key'] = cfg.key;

  try {
    if (useStream) {
      const resp = await fetch(cfg.url + '/v1/chat/completions', { method: 'POST', headers, body });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);

      const bodyEl  = appendMsg('assistant', '', true, activeSkill);
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
      appendMsg('assistant', reply, false, activeSkill);
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

async function loadSkills() {
  try {
    const data = await apiFetch('/v1/skills');
    const skills = data.skills || [];
    const sel = el('skill-select');
    // preserve current selection
    const current = sel.value;
    // rebuild options
    sel.innerHTML = '<option value="">none</option>';
    for (const name of skills) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    if (current && skills.includes(current)) sel.value = current;
  } catch {
    // /v1/skills not available yet — leave selector with only "none"
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
  loadSkills();

  el('skill-select').addEventListener('change', () => {
    el('skill-select').classList.toggle('has-skill', !!el('skill-select').value);
  });
}

// ══════════════════════════════════════════════════════════════════
// TOOLS
// ══════════════════════════════════════════════════════════════════

let allTools = [], activeServer = 'all', byServerMap = {};

async function loadTools() {
  const qualified = el('qualified-toggle').checked;
  try {
    const data = await apiFetch(`/v1/tools${qualified ? '?qualified=true' : ''}`);
    allTools    = data.tools || [];
    byServerMap = data.by_server || {};
    buildServerTabs(byServerMap);
    renderTools(allTools);
  } catch (e) {
    toast(`Tools error: ${e.message}`, 'err');
    el('tools-grid').innerHTML = '<div class="empty-state">Failed to load tools</div>';
  }
}

// ── helpers for server-level bulk disable ─────────────────────────

/** All qualified tool names that belong to a given server entry in byServer. */
function _serverToolNames(serverName) {
  return (byServerMap[serverName] || []);
}

/** True if every tool of a server is in disabledTools. */
function _isServerDisabled(serverName) {
  const tools = _serverToolNames(serverName);
  return tools.length > 0 && tools.every(n => disabledTools.has(n));
}

/** Enable or disable all tools of a server at once. */
function _setServerEnabled(serverName, enabled) {
  for (const name of _serverToolNames(serverName)) {
    if (enabled) disabledTools.delete(name);
    else         disabledTools.add(name);
  }
  saveDisabledTools();
}

function buildServerTabs(byServer) {
  const container = el('tools-server-tabs');
  container.innerHTML = '';

  for (const s of ['all', ...Object.keys(byServer)]) {
    const isAll = s === 'all';
    const count = isAll ? allTools.length : (byServer[s] || []).length;
    const serverDisabled = !isAll && _isServerDisabled(s);

    const wrap = document.createElement('div');
    wrap.className = 'server-tab-wrap';
    wrap.dataset.server = s;

    // ── server label / filter button ────────────────────────────
    const btn = document.createElement('button');
    btn.className = `server-tab-btn${s === activeServer ? ' active' : ''}${(!isAll && serverDisabled) ? ' server-all-disabled' : ''}`;
    btn.dataset.server = s;
    btn.textContent = isAll ? `All (${count})` : `${s} (${count})`;
    btn.addEventListener('click', () => {
      activeServer = s;
      document.querySelectorAll('.server-tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      filterTools();
    });
    wrap.appendChild(btn);

    // ── per-server enable/disable toggle (not on "All" tab) ─────
    if (!isAll) {
      const tog = document.createElement('label');
      tog.className = 'server-toggle';
      tog.title = serverDisabled
        ? `Enable all ${s} tools`
        : `Disable all ${s} tools`;

      const inp = document.createElement('input');
      inp.type    = 'checkbox';
      inp.checked = !serverDisabled;
      inp.dataset.server = s;

      const track = document.createElement('span');
      track.className = 'server-toggle-track';

      tog.appendChild(inp);
      tog.appendChild(track);

      tog.addEventListener('click', e => e.stopPropagation());
      inp.addEventListener('change', e => {
        const enabled = e.target.checked;
        _setServerEnabled(s, enabled);
        _updateDisabledBadge();

        // update pill appearance
        btn.classList.toggle('server-all-disabled', !enabled);
        tog.title = enabled ? `Disable all ${s} tools` : `Enable all ${s} tools`;

        // re-render so individual card toggles reflect the new state
        filterTools();
        toast(
          enabled ? `${s}: all tools enabled` : `${s}: all tools disabled`,
          'info', 1800,
        );
      });
      wrap.appendChild(tog);
    }

    container.appendChild(wrap);
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
    const enabled = isToolEnabled(name);

    const propRows = Object.entries(props).map(([k, v]) =>
      `<tr>
        <td style="padding:3px 0;color:var(--amber-bright);font-size:11px">${k}${req.includes(k) ? ' <span style="color:var(--red)">*</span>' : ''}</td>
        <td style="padding:3px 0 3px 12px;color:var(--muted-2);font-size:10px">${v.type||''}${v.description?' — '+v.description:''}</td>
      </tr>`
    ).join('');

    const card = document.createElement('div');
    card.className = `tool-card${enabled ? '' : ' tool-disabled'}`;
    card.innerHTML = `
      <div class="tool-card-header">
        <div style="flex:1;min-width:0">
          <div class="tool-card-name">${name}</div>
          <div class="tool-card-server">${serverLabel}</div>
        </div>
        <label class="tool-enable-toggle" title="${enabled ? 'Enabled — click to exclude from function calls' : 'Disabled — click to include in function calls'}">
          <input type="checkbox" ${enabled ? 'checked' : ''} data-tool="${name}">
          <span class="tool-toggle-track"></span>
        </label>
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

    // toggle: stop label click from propagating to card-header (which would expand card)
    const toggleLabel = card.querySelector('.tool-enable-toggle');
    const toggleInput = card.querySelector('.tool-enable-toggle input');
    toggleLabel.addEventListener('click', e => e.stopPropagation());
    toggleInput.addEventListener('change', e => {
      const on = e.target.checked;
      if (on) disabledTools.delete(name); else disabledTools.add(name);
      saveDisabledTools();
      card.classList.toggle('tool-disabled', !on);
      toggleLabel.title = on
        ? 'Enabled — click to exclude from function calls'
        : 'Disabled — click to include in function calls';
      // update disabled count badge
      _updateDisabledBadge();
      toast(on ? `${name} enabled` : `${name} disabled`, 'info', 1200);
    });

    card.querySelector('.tool-card-header').addEventListener('click', () => card.classList.toggle('open'));
    card.querySelector('.copy-json-btn').addEventListener('click', e => {
      e.stopPropagation();
      navigator.clipboard.writeText(JSON.stringify(t, null, 2)).then(() => toast(`Copied ${name}`, 'ok', 1500));
    });
    grid.appendChild(card);
  }
  _updateDisabledBadge();
}

function _updateDisabledBadge() {
  const count = disabledTools.size;
  let badge = el('tools-disabled-badge');
  if (!badge) {
    badge = document.createElement('span');
    badge.id = 'tools-disabled-badge';
    badge.className = 'disabled-badge';
    const hdr = document.querySelector('#tab-tools .header-actions');
    if (hdr) hdr.prepend(badge);
  }
  if (count > 0) {
    badge.textContent = `${count} disabled`;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }

  // Keep server-tab toggles in sync when individual tool cards are toggled
  document.querySelectorAll('.server-toggle input[data-server]').forEach(inp => {
    const s = inp.dataset.server;
    const allDisabled = _isServerDisabled(s);
    inp.checked = !allDisabled;
    const btn = document.querySelector(`.server-tab-btn[data-server="${s}"]`);
    if (btn) btn.classList.toggle('server-all-disabled', allDisabled);
  });
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

// ── Settings helpers ──────────────────────────────────────────────

/** Populate the LLM Backend fields from a /v1/status snapshot.
 *  Only overwrites fields that are currently empty in the form
 *  so manual edits are never clobbered. Skills list is always refreshed. */
function _applyStatusToSettings(status) {
  const urlField   = el('cfg-llm-url');
  const modelField = el('cfg-llm-model');
  const stepsField = el('cfg-max-steps');
  const rlField    = el('cfg-rate-limit');

  if (!urlField.value   && status.llm_backend && status.llm_backend !== '—')
    urlField.value   = status.llm_backend;
  if (!modelField.value && status.llm_model   && status.llm_model   !== '—')
    modelField.value = status.llm_model;
  if (!stepsField.value && status.max_steps != null)
    stepsField.value = status.max_steps;
  if (!rlField.value    && status.rate_limit_rpm != null)
    rlField.value    = status.rate_limit_rpm;

  // Skills — always refresh (read-only display)
  const skillBox = el('cfg-skills-display');
  if (status.skills && status.skills.length) {
    skillBox.innerHTML = status.skills
      .map(s => `<span class="tool-tag" style="color:var(--amber)">${s}</span>`)
      .join(' ');
  } else {
    skillBox.innerHTML = '<span class="muted" style="font-size:11px">No skills loaded</span>';
  }
}

/** Populate every Settings input from the current in-memory cfg object. */
function _populateSettingsFromCfg() {
  el('cfg-url').value        = cfg.url;
  el('cfg-key').value        = cfg.key;
  el('cfg-llm-url').value    = cfg.llm_url;
  el('cfg-llm-model').value  = cfg.llm_model;
  el('cfg-llm-key').value    = cfg.llm_key;
  el('cfg-max-steps').value  = cfg.max_steps;
  el('cfg-rate-limit').value = cfg.rate_limit;
  el('cfg-model').value      = cfg.model;
  el('cfg-refresh').value    = cfg.refresh;
}

async function _fetchAndPopulateBackend(force = false) {
  if (!cfg.url) return;
  try {
    const status = await apiFetch('/v1/status');
    if (force) {
      // Explicit button press — overwrite all LLM backend fields unconditionally
      if (status.llm_backend && status.llm_backend !== '—') el('cfg-llm-url').value   = status.llm_backend;
      if (status.llm_model   && status.llm_model   !== '—') el('cfg-llm-model').value = status.llm_model;
      if (status.max_steps   != null) el('cfg-max-steps').value  = status.max_steps;
      if (status.rate_limit_rpm != null) el('cfg-rate-limit').value = status.rate_limit_rpm;
      const skillBox = el('cfg-skills-display');
      if (status.skills && status.skills.length) {
        skillBox.innerHTML = status.skills.map(s => `<span class="tool-tag" style="color:var(--amber)">${s}</span>`).join(' ');
      } else {
        skillBox.innerHTML = '<span class="muted" style="font-size:11px">No skills loaded</span>';
      }
    } else {
      _applyStatusToSettings(status);   // only fills empty fields
    }
    toast('Fetched from server', 'ok', 1500);
  } catch (e) {
    toast(`Could not fetch: ${e.message}`, 'err');
  }
}

// ══════════════════════════════════════════════════════════════════
// SETTINGS
// ══════════════════════════════════════════════════════════════════

function initSettings() {
  // ── Populate fields from localStorage ──────────────────────────
  _populateSettingsFromCfg();

  // ── Save & Apply ───────────────────────────────────────────────
  el('save-settings-btn').addEventListener('click', async () => {
    // Connection
    cfg.url     = el('cfg-url').value.trim().replace(/\/$/, '');
    cfg.key     = el('cfg-key').value.trim();
    // LLM Backend
    cfg.llm_url    = el('cfg-llm-url').value.trim().replace(/\/$/, '');
    cfg.llm_model  = el('cfg-llm-model').value.trim();
    cfg.llm_key    = el('cfg-llm-key').value.trim();
    cfg.max_steps  = parseInt(el('cfg-max-steps').value)  || 20;
    cfg.rate_limit = parseInt(el('cfg-rate-limit').value) || 0;
    // Agent
    cfg.model   = el('cfg-model').value.trim();
    cfg.refresh = parseInt(el('cfg-refresh').value) || 10;
    saveCfg();

    el('status-url').textContent = cfg.url || '—';

    // Push LLM backend config to server (best-effort)
    if (cfg.url) {
      const patch = {};
      if (cfg.llm_url)   patch.base_url   = cfg.llm_url;
      if (cfg.llm_model) patch.model      = cfg.llm_model;
      if (cfg.llm_key)   patch.llm_api_key = cfg.llm_key;
      if (cfg.max_steps) patch.max_steps  = cfg.max_steps;
      patch.rate_limit = cfg.rate_limit;
      // Per-request model override
      if (cfg.model)     patch.agent_model = cfg.model;
      try {
        await apiFetch('/v1/config', {
          method: 'PATCH',
          body:   JSON.stringify(patch),
        });
      } catch (_) { /* server may not be up yet — silently ignore */ }
    }

    const msg = el('settings-saved-msg');
    msg.classList.remove('hidden');
    setTimeout(() => msg.classList.add('hidden'), 2000);
    toast('Saved & applied', 'ok', 2000);
    loadDashboard();
    switchTab('dashboard');
  });

  // ── Reset ──────────────────────────────────────────────────────
  el('reset-settings-btn').addEventListener('click', () => {
    cfg = { ...DEFAULT_CFG };
    saveCfg();
    _populateSettingsFromCfg();
    toast('Reset to defaults', 'info', 2000);
  });

  // ── Console key visibility toggle ──────────────────────────────
  el('toggle-key-vis').addEventListener('click', () => {
    const inp  = el('cfg-key');
    const show = inp.type === 'password';
    inp.type   = show ? 'text' : 'password';
    el('toggle-key-vis').textContent = show ? 'Hide' : 'Show';
  });

  // ── LLM key visibility toggle ───────────────────────────────────
  el('toggle-llm-key-vis').addEventListener('click', () => {
    const inp  = el('cfg-llm-key');
    const show = inp.type === 'password';
    inp.type   = show ? 'text' : 'password';
    el('toggle-llm-key-vis').textContent = show ? 'Hide' : 'Show';
  });

  // ── Fetch from server button ────────────────────────────────────
  el('fetch-backend-btn').addEventListener('click', () => _fetchAndPopulateBackend(true));

  // Auto-populate LLM backend fields when switching to Settings
  // (only if they are empty, so we don't overwrite user edits)
  if (cfg.url && !cfg.llm_url && !cfg.llm_model) {
    _fetchAndPopulateBackend();
  }
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