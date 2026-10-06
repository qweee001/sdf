/* v2 的「營運」頁（route: #ops）：把主控制台的日常操作搬進來，呼叫既有 API。
 *
 * 涵蓋：帳號啟動／停止、啟用／停用、功能開關（媒體／語音）、群組範圍（帳號 × 群組）。
 * 事件用 #main 上的事件委派，不受 app.js 重繪影響。
 * 人設編輯、私訊、媒體實測、新增帳號仍是主控制台的複雜對話框，這裡給明確入口。
 */
(function () {
  'use strict';

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let data = { status: null, directory: null };
  let busy = false;

  async function getJson(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    return res.json();
  }
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    return res.json().catch(() => ({}));
  }
  const personaOf = (acc) => { try { return JSON.parse(acc.persona || '{}') || {}; } catch (e) { return {}; } };

  async function refresh(force) {
    data.status = await getJson('/api/status');
    data.directory = await getJson('/api/groups/directory' + (force ? '?refresh=1' : ''));
  }

  function accountsTable() {
    const accounts = (data.status && data.status.accounts) || [];
    if (!accounts.length) return '<p class="muted">還沒有帳號。</p>';
    const rows = accounts.map((acc) => {
      const p = personaOf(acc);
      const state = acc.is_running ? '運行中' : (acc.setup_complete ? '已停止' : '待設定');
      return `<tr>
        <td><b>${esc(p.name || acc.name)}</b><div class="small muted">${esc((p.city || '未設定') + (p.district ? '・' + p.district : ''))}</div></td>
        <td><span class="badge ${acc.is_running ? 'teal' : ''}">${esc(state)}</span></td>
        <td>${acc.enabled ? '已啟用' : '已停用'}</td>
        <td>${(acc.groups || []).length} 群</td>
        <td class="small muted">回覆 ${acc.stats.replies_sent}｜主動 ${acc.stats.proactive_sent}｜錯誤 ${acc.stats.errors}</td>
        <td>
          <button class="button small" data-ops="start" data-id="${esc(acc.id)}"${acc.is_running ? ' disabled' : ''}>啟動</button>
          <button class="button small" data-ops="stop" data-id="${esc(acc.id)}"${acc.is_running ? '' : ' disabled'}>停止</button>
          <button class="button small" data-ops="toggle" data-id="${esc(acc.id)}">${acc.enabled ? '停用' : '啟用'}</button>
          <a class="button small" href="/" target="_blank" rel="noopener">人設／私訊</a>
        </td>
      </tr>`;
    }).join('');
    return `<div class="table-wrap"><table><thead><tr>
        <th>帳號</th><th>狀態</th><th>啟用</th><th>群組</th><th>統計</th><th>操作</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function featuresBlock() {
    const f = (data.status && data.status.features) || {};
    return `<div class="ops-grid">
      <label class="ops-item"><input type="checkbox" id="ops-media"${f.media_enabled ? ' checked' : ''}>
        <span><b>媒體功能</b><span class="small muted">圖片理解、圖片與影片生成</span></span></label>
      <label class="ops-item"><input type="checkbox" id="ops-voice"${f.voice_enabled ? ' checked' : ''}${f.voice_available ? '' : ' disabled'}>
        <span><b>語音功能</b><span class="small muted">${f.voice_available ? '本地克隆台灣腔' : '尚未就緒，已鎖定關閉'}</span></span></label>
    </div>`;
  }

  function membershipBlock() {
    const accounts = (data.status && data.status.accounts) || [];
    const groups = (data.directory && data.directory.groups) || [];
    if (!accounts.length || !groups.length) return '<p class="muted">沒有可勾選的群組。</p>';
    const head = groups.map((g) => `<th>${esc(g.display || g.title || g.id)}</th>`).join('');
    const rows = accounts.map((acc) => {
      const p = personaOf(acc);
      const mine = new Set((acc.groups || []).map(String));
      const cells = groups.map((g) => `<td class="ops-cell"><input type="checkbox" data-ops="membership"
        data-account="${esc(acc.id)}" data-group="${esc(g.id)}"${mine.has(String(g.id)) ? ' checked' : ''}></td>`).join('');
      return `<tr><td><b>${esc(p.name || acc.name)}</b></td>${cells}</tr>`;
    }).join('');
    return `<div class="table-wrap"><table><thead><tr><th>帳號</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small muted">勾選後立即呼叫 /api/groups/membership；一個群都沒勾的帳號會自動停用（與主控制台一致）。</p>`;
  }

  function view() {
    if (!data.status) {
      refresh(false).then(() => { if (window.SDFApp) window.SDFApp.refresh(); }).catch(() => {});
      return '<section class="card card-pad"><h2>營運操作</h2><p class="muted">載入中…（若一直停在這裡，請先登入主控制台）</p></section>';
    }
    return `
      <div class="view-head"><h1>營運操作</h1><div class="small muted">帳號啟停、功能開關、群組範圍；資料即時來自後端</div></div>
      <div class="ops-top">
        <button class="button small" data-ops="reload">重新整理</button>
        <a class="button small" href="/" target="_blank" rel="noopener">主控制台（人設／私訊／媒體實測／新增帳號）</a>
      </div>
      <section class="card card-pad"><h2>帳號（${(data.status.accounts || []).length}）</h2>${accountsTable()}</section>
      <section class="card card-pad"><h2>功能開關</h2>${featuresBlock()}</section>
      <section class="card card-pad"><h2>群組範圍</h2>${membershipBlock()}</section>`;
  }

  async function rerender() {
    await refresh(false);
    if (window.SDFApp) window.SDFApp.refresh();
  }

  window.SDFOps = { view, refresh };

  document.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-ops]');
    if (!btn || btn.tagName === 'INPUT' || busy) return;
    const act = btn.dataset.ops;
    if (act === 'reload') { btn.disabled = true; await rerender(); return; }
    if (act === 'start' || act === 'stop' || act === 'toggle') {
      busy = true; btn.disabled = true;
      try { await postJson(`/api/accounts/${btn.dataset.id}/${act}`, {}); await rerender(); }
      catch (err) { btn.textContent = '失敗'; }
      finally { busy = false; }
    }
  });

  document.addEventListener('change', async (event) => {
    const el = event.target;
    if (!el || !el.dataset || !el.dataset.ops) return;
    if (el.id === 'ops-media' || el.id === 'ops-voice') {
      busy = true;
      try {
        await postJson('/api/features', {
          media_enabled: document.getElementById('ops-media').checked,
          voice_enabled: document.getElementById('ops-voice').checked,
        });
        await rerender();
      } catch (err) { el.checked = !el.checked; }
      finally { busy = false; }
      return;
    }
    if (el.dataset.ops === 'membership') {
      const accountId = el.dataset.account;
      const boxes = [...document.querySelectorAll(`input[data-account="${accountId}"]`)];
      const groups = boxes.filter((b) => b.checked).map((b) => Number(b.dataset.group));
      busy = true; el.disabled = true;
      try { await postJson('/api/groups/membership', { account_id: accountId, groups: groups }); await refresh(true); }
      catch (err) { el.checked = !el.checked; el.disabled = false; }
      finally { busy = false; if (window.SDFApp) window.SDFApp.refresh(); }
    }
  });
})();
