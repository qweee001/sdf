/* 把真後端資料餵給 Codex 那套前端（版面與互動不動）。
 *
 * 資料來源（同一站、需登入 session）：
 *   GET /api/status                 帳號與統計
 *   GET /api/groups/directory       群組清單與活動量
 *   GET /api/groups/<id>/messages   該群真實訊息（當成「紀錄」來源）
 *
 * 沒登入（401）時不改資料，畫面顯示原本的示範內容，並提示回 / 登入。
 * 審閱標記目前只存在瀏覽器（後端還沒有審閱狀態），畫面上會說明。
 */
(function () {
  'use strict';

  const pad = (n) => String(n).padStart(2, '0');

  function toLocalTime(ts) {
    const d = new Date(Number(ts) * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function toIso(ts) {
    return new Date(Number(ts) * 1000).toISOString();
  }

  async function getJson(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    return res.json();
  }

  function mapAccounts(status) {
    return (status.accounts || []).map((acc) => {
      let persona = {};
      try { persona = JSON.parse(acc.persona || '{}') || {}; } catch (e) { persona = {}; }
      const running = !!acc.is_running;
      return {
        id: String(acc.id),
        name: persona.name || acc.name || acc.id,
        type: running ? '運行中' : '已停止',
        enabled: !!acc.enabled,
        detail: (acc.stats || {}),
      };
    });
  }

  function mapGroups(directory, selectedIds) {
    const list = (directory.groups || [])
      .filter((g) => (g.msg_count || 0) > 0 || selectedIds.has(String(g.id)))
      .map((g) => ({
        id: String(g.id),
        name: g.display || g.title || String(g.id),
        members: g.members || 0,
        msgCount: g.msg_count || 0,
        humans: g.human_senders || 0,
        selected: selectedIds.has(String(g.id)),
        lastTs: g.last_ts || 0,
      }));
    return list.sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
  }

  function mapRecords(messages, groups, accounts) {
    const knownGroup = groups[0] ? groups[0].id : 'live';
    return (messages || []).map((row, index) => {
      const isBot = String(row.role) === 'assistant';
      const content = String(row.content || '');
      return {
        id: 'live-' + (row.id != null ? row.id : index),
        // record_key 是後端給的跨帳號穩定鍵；審閱狀態切換靠它寫回 DB
        record_key: row.record_key || '',
        timestamp: toIso(row.timestamp),
        time: toLocalTime(row.timestamp),
        groupId: String(row.group_id != null ? row.group_id : knownGroup),
        accountId: isBot ? String(row.sender_id || '') : '',
        event: isBot ? '水軍送出訊息' : '收到一則訊息',
        content: content,
        source: (row.sender_name || '') + (isBot ? '（水軍帳號）' : '（真人）'),
        // 後端說已檢視就是已檢視（跨裝置一致），不再是前端自己記
        status: isBot ? (row.reviewed ? 'reviewed' : 'pending') : 'reviewed',
      };
    });
  }

  async function bootstrap() {
    const status = await getJson('/api/status');
    const directory = await getJson('/api/groups/directory');

    const accounts = mapAccounts(status);
    const selectedIds = new Set();
    (status.accounts || []).forEach((acc) => {
      (acc.groups || []).forEach((gid) => selectedIds.add(String(gid)));
    });
    const groups = mapGroups(directory, selectedIds);

    // 取最活躍的群當預設，撈它的真實紀錄（含後端審閱狀態）
    const primary = groups[0];
    let messages = [];
    let counts = { pending: 0, reviewed: 0 };
    if (primary && /^-?\d+$/.test(primary.id)) {
      try {
        const payload = await getJson('/api/groups/' + primary.id + '/records?limit=200');
        messages = payload.records || [];
        counts = payload.counts || counts;
        window.__sdfGroupId = primary.id;
      } catch (err) {
        messages = [];
      }
    }
    const records = mapRecords(messages, groups, accounts);

    if (window.SDFCore && typeof window.SDFCore.setData === 'function') {
      const sizes = window.SDFCore.setData({
        groups: groups.map((g) => ({ id: g.id, name: g.name })),
        accounts: accounts.map((a) => ({ id: a.id, name: a.name, type: a.type, enabled: a.enabled })),
        records: records,
        defaultFilters: {
          accountId: 'all',
          groupId: groups.length ? groups[0].id : 'all',
          status: 'all',
          query: '',
          range: '7d',
        },
      });
      window.__sdfLive = { ok: true, sizes: sizes, groups: groups, accounts: accounts, groupId: primary ? primary.id : '', counts: counts };
    }
    if (window.SDFApp) {
      window.SDFApp.reseed();
      window.SDFApp.refresh();
    }
    markLive(groups, records);
  }

  function markLive(groups, records) {
    const badge = Array.from(document.querySelectorAll('.badge')).find(
      (el) => (el.textContent || '').indexOf('即時資料') >= 0
    );
    if (badge) {
      badge.textContent = '即時資料 · ' + groups.length + ' 群 / ' + records.length + ' 則';
      badge.title = '資料來自本站在線後端；審閱標記目前只存在這台瀏覽器';
    }
    const note = document.querySelector('.sidebar-foot, .env-note');
    if (note) note.textContent = '即時資料 · 審閱標記存本機';
  }

  function showAuthHint() {
    const badge = Array.from(document.querySelectorAll('.badge')).find(
      (el) => (el.textContent || '').indexOf('即時資料') >= 0
    );
    if (badge) {
      badge.textContent = '未登入 · 顯示示範資料';
      badge.title = '請先到 / 登入，再回來這裡看即時資料';
    }
  }

  function enterApp() {
    // 有 session 就直接進站，不必再按一次前端那個假登入
    const btn = document.getElementById('demo-enter');
    const loginVisible = document.getElementById('login-screen');
    if (btn && loginVisible && !loginVisible.hidden && getComputedStyle(loginVisible).display !== 'none') {
      btn.click();
    }
  }

  function pointToRealLogin() {
    const note = document.querySelector('#login-screen .muted, #login-screen p.muted');
    if (note) {
      note.innerHTML = '尚未登入。<a href="/classic">到經典控制台登入</a>後重整這一頁，就會顯示即時資料。';
    }
    const badge = Array.from(document.querySelectorAll('.badge')).find(
      (el) => (el.textContent || '').indexOf('即時資料') >= 0
    );
    if (badge) {
      badge.textContent = '未登入 · 顯示示範資料';
      badge.title = '請先到 /classic 登入';
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    bootstrap().then(enterApp).catch(() => pointToRealLogin());
  });
})();
