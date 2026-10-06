/* v2 的「調度台」分頁：把經典控制台的核心搬進 v2 的外殼，讓 `/` 成為唯一入口。
 *
 * 內容：全局 KPI、三條帳號車道、可切換審閱的群組訊息流、攔截原因長條。
 * 資料：/api/status、/api/groups/directory、/api/groups/{id}/records、/api/records/review（全部既有 API）。
 */
(function () {
  'use strict';

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const REASON_LABEL = {
    tone: '語氣不符場合', time_mismatch: '時段穿幫', near_duplicate: '近似重複', place_typo: '地名別字',
    simplified_chars: '簡體字混入', refusal: '拒答', group_meta: '群務話題', blocked_video: '影片阻擋',
    fabricate: '編造經歷', offtopic: '離題', ok: '成功', human: '真人訊息', managed: '水軍訊息',
    rate_limited: '限流', error: '錯誤', typo_held: '用字未過（暫緩）', gate_held: '審核未過（暫緩）',
    too_long: '過長', stale_context: '上下文已過時',
  };

  let board = { status: null, directory: null, records: null, groupId: null };
  const personaOf = (acc) => { try { return JSON.parse(acc.persona || '{}') || {}; } catch (e) { return {}; } };

  async function getJson(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    return res.json();
  }

  function groups() {
    return (board.directory && board.directory.groups) || [];
  }
  function accounts() {
    return (board.status && board.status.accounts) || [];
  }
  function pickGroup() {
    if (board.groupId && groups().some((g) => String(g.id) === String(board.groupId))) return String(board.groupId);
    try {
      const saved = localStorage.getItem('sdf_monitor_group');
      if (saved && groups().some((g) => String(g.id) === String(saved))) return String(saved);
    } catch (e) { /* 沒存過就算了 */ }
    return groups().length ? String(groups()[0].id) : '';
  }

  async function loadAll() {
    board.status = await getJson('/api/status');
    board.directory = await getJson('/api/groups/directory');
    board.groupId = pickGroup();
    if (board.groupId) {
      board.records = await getJson(`/api/groups/${board.groupId}/records?limit=200`).catch(() => null);
    }
    return board;
  }

  function kpiRow() {
    const status = board.status || {};
    const audit = status.reply_audit || {};
    const policy = audit.policy || {};
    const blocked = Object.values(policy).reduce((a, b) => a + (Number(b) || 0), 0);
    const sent = Number((audit.sent || {}).ok || 0);
    const calls = accounts().reduce((a, x) => a + (Number((x.stats || {}).decision_calls) || 0), 0);
    const held = accounts().reduce((a, x) => a + (Number((x.stats || {}).gate_held) || 0), 0);
    const pending = (board.records && board.records.counts && board.records.counts.pending) || 0;
    const top = Object.entries(policy).sort((a, b) => b[1] - a[1])[0];
    return `<div class="board-kpi">
      <span>運行 <b>${status.running || 0}/${status.total || 0}</b></span>
      <span class="${pending ? 'warn' : ''}">待檢視 <b>${pending}</b></span>
      <span>24h 送出 <b>${sent}</b></span>
      <span class="warn">24h 攔截 <b>${blocked}</b></span>
      <span>決策呼叫 <b>${calls}</b></span>
      <span class="muted">${top ? '最多是「' + esc(REASON_LABEL[top[0]] || top[0]) + '」' + top[1] + ' 件' : '24h 無攔截'}</span>
    </div>`;
  }

  function lanes() {
    const rows = accounts().map((acc) => {
      const p = personaOf(acc);
      const state = acc.is_running ? '運行中' : (acc.setup_complete ? '已停止' : '待設定');
      const mine = (acc.groups || []).map(String);
      const blocks = groups().filter((g) => mine.includes(String(g.id))).map((g) => {
        const active = String(g.id) === String(board.groupId);
        return `<div class="lane-block${active ? ' on' : ''}" data-action="board-group" data-id="${esc(g.id)}">
            <b>${esc(g.display || g.title || g.id)}</b>
            <span class="small muted">${g.members || 0} 人｜訊息 ${g.msg_count || 0}｜真人 ${g.human_senders || 0}</span>
          </div>`;
      }).join('');
      const idle = groups().filter((g) => !mine.includes(String(g.id))).length;
      return `<div class="lane">
        <div class="lane-head">
          <b>${esc(p.name || acc.name)}</b>
          <span class="badge ${acc.is_running ? 'teal' : ''}">${esc(state)}</span>
        </div>
        <div class="small muted">${esc((p.city || '未設定') + (p.district ? '・' + p.district : ''))}｜回覆 ${acc.stats.replies_sent}｜主動 ${acc.stats.proactive_sent}｜錯誤 ${acc.stats.errors}</div>
        <div class="lane-body">${blocks || '<span class="small muted">尚未勾選群組</span>'}
          ${idle ? `<span class="small muted">另有 ${idle} 個群未選</span>` : ''}</div>
      </div>`;
    }).join('');
    return `<div class="board-lanes">${rows || '<p class="muted">還沒有帳號。</p>'}</div>`;
  }

  function bars() {
    const audit = ((board.status || {}).reply_audit) || {};
    const stages = Object.keys(audit);
    if (!stages.length) return '<p class="small muted">近 24h 沒有攔截紀錄。</p>';
    return stages.map((stage) => {
      const reasons = audit[stage] || {};
      const max = Math.max(1, ...Object.values(reasons).map((v) => Number(v) || 0));
      const rows = Object.keys(reasons)
        .sort((a, b) => (Number(reasons[b]) || 0) - (Number(reasons[a]) || 0))
        .map((r) => {
          const val = Number(reasons[r]) || 0;
          return `<div class="bar-row"><span class="bl">${esc(REASON_LABEL[r] || r)}</span>
            <progress class="bt${stage === 'sent' ? ' ok' : ''}" max="${max}" value="${val}" aria-label="${esc(REASON_LABEL[r] || r)}"></progress>
            <span class="bn">${val}</span></div>`;
        }).join('');
      return `<div class="bar-stage"><div class="small muted">${esc(stage)}</div>${rows}</div>`;
    }).join('');
  }

  function feed() {
    if (!board.groupId) return '<p class="small muted">沒有可選的群組。</p>';
    const payload = board.records;
    if (!payload) return '<p class="small muted">載入中…</p>';
    const rows = (payload.records || []).slice(-40).reverse().map((r) => {
      const bot = r.role === 'assistant';
      const time = r.timestamp ? new Date(r.timestamp * 1000).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' }) : '';
      const review = bot
        ? `<button class="button small" data-action="board-review" data-key="${esc(r.record_key || '')}" data-review="${r.reviewed ? '0' : '1'}">${r.reviewed ? '✓ 已檢視' : '○ 待檢視'}</button>`
        : '';
      return `<div class="feed-row">
        <span class="badge ${bot ? '' : 'teal'}">${bot ? '水軍' : '真人'}</span>
        <span class="small muted">${esc(r.sender_name || '')} ${time}</span>
        <span class="feed-text">${esc(r.content || '')}</span>${review}
      </div>`;
    }).join('');
    const counts = (payload.counts) || {};
    return `<div class="small muted feed-counts">待檢視 ${counts.pending || 0}｜已檢視 ${counts.reviewed || 0}（狀態寫在後端，跨裝置一致）</div>${rows}`;
  }

  function view() {
    if (!board.status) {
      loadAll().then(() => { if (window.SDFApp) window.SDFApp.refresh(); }).catch(() => {});
      return '<section class="card card-pad"><h2>調度台</h2><p class="muted">載入中…（若一直停在這裡請先登入）</p></section>';
    }
    const options = groups().map((g) => `<option value="${esc(g.id)}"${String(g.id) === String(board.groupId) ? ' selected' : ''}>${esc(g.display || g.title || g.id)}</option>`).join('');
    return `
      <div class="view-head"><h1>調度台</h1><div class="small muted">三條帳號車道、待檢視佇列、攔截原因</div></div>
      ${kpiRow()}
      <section class="card card-pad"><h2>帳號車道</h2>${lanes()}</section>
      <div class="board-split">
        <section class="card card-pad"><h2>群組訊息流
            <select class="board-select" data-action="board-pick">${options}</select></h2>
          ${feed()}</section>
        <section class="card card-pad"><h2>攔截原因（近 24h）</h2>${bars()}</section>
      </div>`;
  }

  async function act(action, el) {
    if (action === 'board-pick') {
      board.groupId = el.value;
      try { localStorage.setItem('sdf_monitor_group', String(board.groupId)); } catch (e) {}
      board.records = await getJson(`/api/groups/${board.groupId}/records?limit=200`).catch(() => null);
      if (window.SDFApp) window.SDFApp.refresh();
      return;
    }
    if (action === 'board-group') {
      board.groupId = el.dataset.id;
      try { localStorage.setItem('sdf_monitor_group', String(board.groupId)); } catch (e) {}
      board.records = await getJson(`/api/groups/${board.groupId}/records?limit=200`).catch(() => null);
      if (window.SDFApp) window.SDFApp.refresh();
      return;
    }
    if (action === 'board-review') {
      const key = el.dataset.key;
      if (!key) return;
      el.disabled = true;
      await fetch('/api/records/review', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: Number(board.groupId), record_key: key, reviewed: el.dataset.review === '1' }),
      }).catch(() => {});
      board.records = await getJson(`/api/groups/${board.groupId}/records?limit=200`).catch(() => null);
      if (window.SDFApp) window.SDFApp.refresh();
    }
  }

  window.SDFBoard = { view, refresh: loadAll, act };
})();
