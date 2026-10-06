/* v2 營運頁加強版：把主控制台的四塊功能搬進來（人設／私訊／媒體實測／新增帳號）。
 * 全部呼叫既有 API，不改後端；彈窗自帶樣式（見 styles.css 的 .sdf-* 區塊）。
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
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || (url + ' -> ' + res.status));
    return payload;
  }
  const personaOf = (acc) => { try { return JSON.parse(acc.persona || '{}') || {}; } catch (e) { return {}; } };

  async function refresh(force) {
    data.status = await getJson('/api/status');
    data.directory = await getJson('/api/groups/directory' + (force ? '?refresh=1' : ''));
  }

  // ---------- 彈窗 ----------------------------------------------------------
  function modal(title, bodyHtml, footerHtml) {
    const wrap = document.createElement('div');
    wrap.className = 'sdf-modal';
    wrap.innerHTML = `<div class="sdf-modal-box">
        <div class="sdf-modal-head"><b>${esc(title)}</b><button class="sdf-x" data-sdf="close">✕</button></div>
        <div class="sdf-modal-body">${bodyHtml}</div>
        <div class="sdf-modal-foot">${footerHtml || ''}</div>
      </div>`;
    document.body.appendChild(wrap);
    return wrap;
  }
  const closeModal = (el) => { if (el && el.parentNode) el.parentNode.removeChild(el); };

  // ---------- 人設 ----------------------------------------------------------
  const PERSONA_FIELDS = [
    ['name', '名字', 'text'], ['gender', '性別', 'select'],
    ['age', '年齡', 'number'], ['city', '城市', 'text'], ['district', '地區', 'text'],
    ['industry', '行業', 'text'], ['university', '學歷', 'text'],
    ['schedule', '作息', 'select2'], ['personality', '性格', 'text'],
    ['hobbies', '興趣愛好（用、分隔）', 'text'], ['looking_for', '想找什麼', 'text'],
    ['dating_count', '約炮成約次數', 'text'], ['chat_style', '聊天風格', 'select3'],
  ];

  async function openPersona(accountId) {
    const box = modal('人設設定', '<p class="small muted">載入中…</p>', '');
    try {
      const { persona } = await getJson(`/api/accounts/${accountId}/persona`);
      const rows = PERSONA_FIELDS.map(([key, label, type]) => {
        const value = persona[key] == null ? '' : String(persona[key]);
        if (type === 'select') {
          return `<label class="sdf-field"><span>${label}</span><select data-pf="${key}">
            <option${value !== '男' ? ' selected' : ''}>女</option><option${value === '男' ? ' selected' : ''}>男</option></select></label>`;
        }
        if (type === 'select2') {
          return `<label class="sdf-field"><span>${label}</span><select data-pf="${key}">
            ${['正常', '夜貓', '早起'].map((o) => `<option${value === o ? ' selected' : ''}>${o}</option>`).join('')}</select></label>`;
        }
        if (type === 'select3') {
          const opts = ['俏皮少男表情', '溫柔成熟', '直球撩人', '安靜慢熱'];
          return `<label class="sdf-field"><span>${label}</span><select data-pf="${key}">
            ${opts.map((o) => `<option${value === o ? ' selected' : ''}>${o}</option>`).join('')}</select></label>`;
        }
        return `<label class="sdf-field"><span>${label}</span><input data-pf="${key}" type="${type}" value="${esc(value)}"></label>`;
      }).join('');
      box.querySelector('.sdf-modal-body').innerHTML = `<div class="sdf-grid">${rows}</div>`;
      box.querySelector('.sdf-modal-foot').innerHTML = `
        <button class="button small" data-sdf="persona-regen" data-id="${esc(accountId)}">重新生成（換一個）</button>
        <button class="button small primary" data-sdf="persona-save" data-id="${esc(accountId)}">儲存人設</button>
        <button class="button small" data-sdf="close">關閉</button>`;
      box.dataset.account = accountId;
    } catch (err) {
      box.querySelector('.sdf-modal-body').innerHTML = `<p class="small muted">載入失敗：${esc(err.message)}</p>`;
    }
  }

  async function savePersona(box) {
    const accountId = box.dataset.account;
    const payload = {};
    box.querySelectorAll('[data-pf]').forEach((el) => {
      const key = el.dataset.pf;
      payload[key] = el.type === 'number' ? Number(el.value || 0) : el.value;
    });
    try {
      await postJson(`/api/accounts/${accountId}/persona`, payload);
      closeModal(box);
      await rerender();
      toast('人設已儲存');
    } catch (err) { toast('儲存失敗：' + err.message); }
  }

  async function regenPersona(btn, box) {
    btn.disabled = true;
    try {
      const { persona } = await postJson(`/api/accounts/${btn.dataset.id}/persona/regenerate`, {});
      Object.entries(persona || {}).forEach(([key, value]) => {
        const el = box.querySelector(`[data-pf="${key}"]`);
        if (el) el.value = value == null ? '' : String(value);
      });
      toast('已重新生成，確認後按儲存');
    } catch (err) { toast('重新生成失敗：' + err.message); }
    finally { btn.disabled = false; }
  }

  // ---------- 私訊 ----------------------------------------------------------
  async function openPrivates(accountId) {
    const box = modal('私訊檢視', '<p class="small muted">載入中…</p>', '<button class="button small" data-sdf="close">關閉</button>');
    try {
      const { messages } = await getJson(`/api/accounts/${accountId}/privates`);
      if (!messages || !messages.length) {
        box.querySelector('.sdf-modal-body').innerHTML = '<p class="small muted">沒有私訊紀錄。</p>';
        return;
      }
      const rows = messages.map((m) => {
        const time = m.timestamp ? new Date(m.timestamp * 1000).toLocaleString('zh-TW') : '';
        const incoming = String(m.role) !== 'assistant';
        return `<div class="sdf-private ${incoming ? 'in' : 'out'}">
          <div class="small muted">${esc(m.sender_name || (incoming ? '對方' : '水軍'))} · ${esc(time)}${m.is_read ? ' · 已讀' : ''}</div>
          <div>${esc(m.content || '')}</div>
          ${incoming && !m.is_read ? `<button class="button small" data-sdf="private-read" data-id="${esc(accountId)}" data-msg="${esc(m.id)}">標記已讀</button>` : ''}
        </div>`;
      }).join('');
      box.querySelector('.sdf-modal-body').innerHTML = rows;
    } catch (err) {
      box.querySelector('.sdf-modal-body').innerHTML = `<p class="small muted">載入失敗：${esc(err.message)}</p>`;
    }
  }

  // ---------- 媒體實測 ------------------------------------------------------
  async function openLiveTest() {
    const box = modal('媒體實測', '<p class="small muted">載入中…</p>', '');
    const paint = async () => {
      const st = await getJson('/api/live-test/status').catch(() => ({}));
      const running = !!st.running;
      const log = (st.log || []).slice(-8).map((l) => `<div class="small muted">${esc(String(l))}</div>`).join('');
      box.querySelector('.sdf-modal-body').innerHTML = `
        <p class="small muted">狀態：${running ? '進行中（' + esc(st.step || '') + '）' : '無進行中實測'}</p>
        ${st.result ? `<pre class="sdf-pre">${esc(JSON.stringify(st.result, null, 2))}</pre>` : ''}
        ${log}`;
      box.querySelector('.sdf-modal-foot').innerHTML = running
        ? '<button class="button small" data-sdf="lt-stop">停止</button><button class="button small" data-sdf="close">關閉</button>'
        : '<button class="button small primary" data-sdf="lt-start">啟動實測</button><button class="button small" data-sdf="close">關閉</button>';
    };
    await paint();
    box.dataset.kind = 'livetest';
    box._repaint = paint;
  }

  // ---------- 新增帳號（Telegram 登入）-------------------------------------
  function openAddAccount() {
    const box = modal('新增水軍帳號', `
      <label class="sdf-field"><span>手機號碼（含國碼）</span><input id="sdf-tg-phone" placeholder="+886912345678"></label>
      <div id="sdf-tg-step2" hidden><label class="sdf-field"><span>驗證碼</span><input id="sdf-tg-code"></label></div>
      <div id="sdf-tg-step3" hidden><label class="sdf-field"><span>兩步驗證密碼</span><input id="sdf-tg-pass" type="password"></label></div>
      <div id="sdf-tg-step4" hidden><label class="sdf-field"><span>帳號名稱（可留空）</span><input id="sdf-tg-name"></label></div>
      <p class="small muted" id="sdf-tg-hint">建立後帳號保持停止，請先設定人設與群組再啟動。</p>`,
      `<button class="button small primary" data-sdf="tg-next" data-step="1">傳送驗證碼</button>
       <button class="button small" data-sdf="close">關閉</button>`);
    box.dataset.kind = 'add';
  }

  async function tgNext(btn, box, step) {
    const hint = box.querySelector('#sdf-tg-hint');
    btn.disabled = true;
    try {
      if (step === '1') {
        await postJson('/api/tglogin/start', { phone: box.querySelector('#sdf-tg-phone').value.trim() });
        box.querySelector('#sdf-tg-step2').hidden = false;
        btn.dataset.step = '2';
        btn.textContent = '確認驗證碼';
        hint.textContent = '驗證碼已送出。';
      } else if (step === '2') {
        const res = await postJson('/api/tglogin/code', { code: box.querySelector('#sdf-tg-code').value.trim() });
        if (res && res.need_password) {
          box.querySelector('#sdf-tg-step3').hidden = false;
          btn.dataset.step = '3';
          btn.textContent = '確認兩步驗證';
        } else {
          box.querySelector('#sdf-tg-step4').hidden = false;
          btn.dataset.step = '4';
          btn.textContent = '建立帳號';
        }
      } else if (step === '3') {
        await postJson('/api/tglogin/password', { password: box.querySelector('#sdf-tg-pass').value });
        box.querySelector('#sdf-tg-step4').hidden = false;
        btn.dataset.step = '4';
        btn.textContent = '建立帳號';
      } else {
        const res = await postJson('/api/tglogin/password', {}).catch(() => null); // 已登入時後端允許直接建立
        const name = box.querySelector('#sdf-tg-name').value.trim();
        void res;
        await postJson('/api/accounts/add', { auth_id: window.__sdfAuthId || '', name });
        closeModal(box);
        await rerender();
        toast('帳號已建立（保持停止）');
      }
    } catch (err) {
      hint.textContent = '失敗：' + err.message;
    } finally { btn.disabled = false; }
  }

  // ---------- 畫面 ----------------------------------------------------------
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
        <td class="ops-actions">
          <button class="button small" data-ops="start" data-id="${esc(acc.id)}"${acc.is_running ? ' disabled' : ''}>啟動</button>
          <button class="button small" data-ops="stop" data-id="${esc(acc.id)}"${acc.is_running ? '' : ' disabled'}>停止</button>
          <button class="button small" data-ops="toggle" data-id="${esc(acc.id)}">${acc.enabled ? '停用' : '啟用'}</button>
          <button class="button small" data-sdf="persona" data-id="${esc(acc.id)}">人設</button>
          <button class="button small" data-sdf="privates" data-id="${esc(acc.id)}">私訊</button>
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
      <p class="small muted">勾選後立即呼叫 /api/groups/membership；一個群都沒勾的帳號會自動停用。</p>`;
  }

  function view() {
    if (!data.status) {
      refresh(false).then(() => { if (window.SDFApp) window.SDFApp.refresh(); }).catch(() => {});
      return '<section class="card card-pad"><h2>營運操作</h2><p class="muted">載入中…（若一直停在這裡請先登入）</p></section>';
    }
    return `
      <div class="view-head"><h1>營運操作</h1><div class="small muted">帳號啟停、人設、私訊、功能開關、群組範圍、媒體實測</div></div>
      <div class="ops-top">
        <button class="button small" data-ops="reload">重新整理</button>
        <button class="button small" data-sdf="add-account">＋ 新增水軍帳號</button>
        <button class="button small" data-sdf="livetest">媒體實測</button>
        <a class="button small" href="/classic" target="_blank" rel="noopener">經典調度台</a>
      </div>
      <section class="card card-pad"><h2>帳號（${(data.status.accounts || []).length}）</h2>${accountsTable()}</section>
      <section class="card card-pad"><h2>功能開關</h2>${featuresBlock()}</section>
      <section class="card card-pad"><h2>群組範圍</h2>${membershipBlock()}</section>`;
  }

  async function rerender() {
    await refresh(false);
    if (window.SDFApp) window.SDFApp.refresh();
  }

  function toast(message) {
    let el = document.getElementById('sdf-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'sdf-toast';
      el.className = 'sdf-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add('on');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove('on'), 2600);
  }

  window.SDFOps = { view, refresh, openPersona, openPrivates, openLiveTest, openAddAccount };

  document.addEventListener('click', async (event) => {
    const sdfBtn = event.target.closest('[data-sdf]');
    if (sdfBtn) {
      const act = sdfBtn.dataset.sdf;
      const box = sdfBtn.closest('.sdf-modal');
      if (act === 'close') { closeModal(box); return; }
      if (act === 'persona') { openPersona(sdfBtn.dataset.id); return; }
      if (act === 'privates') { openPrivates(sdfBtn.dataset.id); return; }
      if (act === 'livetest') { openLiveTest(); return; }
      if (act === 'add-account') { openAddAccount(); return; }
      if (act === 'persona-save') { await savePersona(box); return; }
      if (act === 'persona-regen') { await regenPersona(sdfBtn, box); return; }
      if (act === 'tg-next') { await tgNext(sdfBtn, box, sdfBtn.dataset.step || '1'); return; }
      if (act === 'lt-start') { await postJson('/api/live-test/start', {}).catch((e) => toast('啟動失敗：' + e.message)); await box._repaint(); return; }
      if (act === 'lt-stop') { await postJson('/api/live-test/stop', {}).catch(() => {}); await box._repaint(); return; }
      if (act === 'private-read') {
        await postJson(`/api/accounts/${sdfBtn.dataset.id}/privates/${sdfBtn.dataset.msg}/read`, {}).catch(() => {});
        openPrivates(sdfBtn.dataset.id);
        return;
      }
      return;
    }

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
