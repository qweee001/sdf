(function () {
  'use strict';
  const C = window.SDFCore;
  const $ = selector => document.querySelector(selector);
  const esc = C.escapeHtml;
  const storageKey = 'sdf-ui-demo-v1';
  const icons = {
    overview:'<path d="m3 10 9-7 9 7v10H14v-6h-4v6H3Z"/>',
    accounts:'<rect x="5" y="7" width="14" height="13" rx="3"/><path d="M12 3v4M9 12v2M15 12v2M9 17h6M2 11v5M22 11v5"/>',
    monitor:'<path d="M8 19H3v-2a5 5 0 0 1 6-5M16 19h5v-2a5 5 0 0 0-6-5"/><circle cx="12" cy="7" r="4"/><path d="M6 22v-3a6 6 0 0 1 12 0v3"/>',
    audit:'<path d="M6 3h9l4 4v14H6Z"/><path d="M14 3v5h5M9 12h7M9 16h5"/>',
    media:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 18 5-5 4 3 4-7 5 9"/>',
    groups:'<path d="M4 4h16v12H9l-5 5Z"/><path d="M8 9h8M8 12h5"/>',
    preferences:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/>',
    file:'<path d="M6 3h8l5 5v13H6Z"/><path d="M13 3v6h6"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
    check:'<path d="m5 12 4 4L19 6"/>'
  };
  const icon = name => '<svg viewBox="0 0 24 24" aria-hidden="true">' + (icons[name] || icons.file) + '</svg>';
  const labels = {overview:'工作總覽',accounts:'帳號管理',monitor:'群組監控',audit:'回覆審計',media:'媒體測試',groups:'群組總管',board:'調度台',ops:'營運操作',preferences:'顯示偏好'};
  const state = {
    route:'overview', filters:{...C.defaultFilters}, records:C.records.map(r => ({...r})),
    selectedId:'record-01', page:1, favorites:['feedback'], savedViews:[], density:'comfortable',
    accountQuery:'', accountStatus:'all', media:null, mediaUrl:null, scroll:{}
  };
  let active = false, toastTimer, undoAction = null;
  const mobileQuery = matchMedia('(max-width:850px)');
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || '{}');
    state.filters = C.sanitizeFilters(stored.filters);
    state.favorites = Array.isArray(stored.favorites) ? [...new Set(stored.favorites.filter(id => C.groups.some(g => g.id === id)))] : ['feedback'];
    state.savedViews = C.sanitizeSavedViews(stored.savedViews);
    state.density = stored.density === 'dense' ? 'dense' : 'comfortable';
    const reviewedIds = Array.isArray(stored.reviewedIds) ? stored.reviewedIds : [];
    state.records = state.records.map(r => reviewedIds.includes(r.id) ? {...r,status:'reviewed'} : r);
  } catch (_) { /* Storage is optional; all interactions also work in memory. */ }

  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        filters:state.filters,favorites:state.favorites,savedViews:state.savedViews,density:state.density,
        reviewedIds:state.records.filter(r => r.status === 'reviewed').map(r => r.id)
      }));
    } catch (_) { toast('瀏覽器未允許儲存；變更在本次開啟期間有效。'); }
  }
  function toast(message, undo) {
    clearTimeout(toastTimer); $('#toast-text').textContent = message;
    $('#toast').hidden = false; undoAction = undo || null; $('#toast-undo').hidden = !undo;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; undoAction = null; }, undo ? 12000 : 6500);
  }
  const accountName = id => C.accounts.find(a => a.id === id)?.name || '全部帳號';
  const groupName = id => C.groups.find(g => g.id === id)?.name || '全部群組';
  const statusBadge = status => '<span class="badge ' + (status === 'pending' ? 'pending' : 'reviewed') + '">' + (status === 'pending' ? '待檢視' : '已檢視') + '</span>';
  const accountBadge = enabled => '<span class="badge ' + (enabled ? 'enabled' : 'paused') + '">' + (enabled ? '已啟用' : '已暫停') + '</span>';
  function scoped(status) { return C.filterRecords(state.records, {...state.filters, ...(status ? {status} : {})}); }
  function empty(title='沒有符合的紀錄', description='調整篩選或清除搜尋，再試一次。') {
    return '<div class="empty">' + icon('search') + '<h3>' + esc(title) + '</h3><p>' + esc(description) + '</p><button class="text-button" data-action="reset-filters">重設篩選</button></div>';
  }
  function selectOptions(items, selected, allLabel) {
    return '<option value="all"' + (selected === 'all' ? ' selected' : '') + '>' + allLabel + '</option>' + items.map(item => '<option value="' + item.id + '"' + (item.id === selected ? ' selected' : '') + '>' + esc(item.name) + '</option>').join('');
  }
  function filters() {
    return '<div class="filter-bar card"><label>帳號<select data-filter="accountId" aria-label="篩選帳號">' + selectOptions(C.accounts,state.filters.accountId,'全部帳號') + '</select></label><span class="badge teal">水軍帳號</span><label>群組<select data-filter="groupId" aria-label="篩選群組">' + selectOptions(C.groups,state.filters.groupId,'全部群組') + '</select></label><label>時間<select data-filter="range" aria-label="篩選時間">' + [['24h','近 24 小時'],['7d','近 7 天'],['all','全部時間']].map(([v,l]) => '<option value="' + v + '"' + (state.filters.range === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select></label><small class="muted">示範基準：2026-10-06 18:00</small></div>';
  }
  function heading(title, subtitle, actions='') {
    return '<div class="page-heading"><h1>' + title + '</h1><p class="subtitle">' + subtitle + '</p><div class="heading-actions">' + actions + '</div></div>';
  }
  function recordTable(items, compact=false) {
    if (!items.length) return empty();
    return '<div class="table-wrap"><table' + (compact ? ' class="compact"' : '') + '><caption class="sr-only">事件紀錄，點擊時間查看詳情</caption><thead><tr><th scope="col">時間</th><th scope="col">事件</th><th scope="col">帳號</th><th scope="col">狀態</th></tr></thead><tbody>' + items.map(r => '<tr' + (r.id === state.selectedId ? ' class="selected"' : '') + '><td><button class="row-link" data-action="select-record" data-id="' + r.id + '" aria-label="檢視 ' + r.time + ' ' + esc(r.event) + '">' + r.time + '</button></td><td>' + esc(r.event) + '</td><td>' + esc(accountName(r.accountId)) + '</td><td>' + statusBadge(r.status) + '</td></tr>').join('') + '</tbody></table></div>';
  }
  function accountTable(items, compact=false) {
    if (!items.length) return '<div class="empty"><h3>沒有符合的帳號</h3><p>調整搜尋或狀態條件。</p></div>';
    return '<div class="table-wrap"><table' + (compact ? ' class="compact"' : '') + '><caption class="sr-only">水軍帳號列表</caption><thead><tr><th scope="col">帳號</th><th scope="col">類型</th><th scope="col">狀態</th></tr></thead><tbody>' + items.map(a => '<tr' + (state.filters.accountId === a.id ? ' class="selected"' : '') + '><td><button class="row-link account-label" data-action="account-records" data-id="' + a.id + '">' + icon('accounts') + esc(a.name) + '</button></td><td>' + a.type + '</td><td>' + accountBadge(a.enabled) + '</td></tr>').join('') + '</tbody></table></div>';
  }
  function metadata(r) {
    return '<dl class="metadata"><dt>時間</dt><dd>2026-10-06 ' + r.time + '</dd><dt>來源群組</dt><dd>' + esc(groupName(r.groupId)) + '</dd><dt>帳號</dt><dd>' + esc(accountName(r.accountId)) + ' <span class="badge teal">水軍帳號</span></dd></dl>';
  }
  function overview() {
    const all = scoped('all'), pending = all.filter(r => r.status === 'pending'), selected = all.find(r => r.id === state.selectedId) || all[0];
    const tally = C.counts(all);
    return heading('工作總覽','日常監控與待檢視紀錄','<select data-filter="accountId" aria-label="檢視帳號">' + selectOptions(C.accounts,state.filters.accountId,'全部帳號') + '</select><span class="badge teal">水軍帳號</span>')
      + '<div class="notice' + (pending.length ? '' : ' neutral') + '"><span aria-hidden="true">' + (pending.length ? '●' : '✓') + '</span><strong>' + (pending.length ? pending.length + ' 筆回覆待檢視' : '目前篩選下的紀錄皆已檢視') + '</strong><button class="text-button" data-action="open-pending">前往檢視 ' + icon('arrow') + '</button></div>'
      + '<div class="overview-grid"><section class="card card-pad monitor-card"><div class="card-header"><div class="card-title">' + icon('monitor') + '<h2>群組監控</h2></div><button class="text-button" data-action="navigate" data-route="monitor">開啟工作頁 ↗</button></div><div class="toolbar"><div class="tabs">' + C.groups.map(g => '<button class="tab' + (state.filters.groupId === g.id ? ' active' : '') + '" data-action="choose-group" data-id="' + g.id + '" aria-pressed="' + (state.filters.groupId === g.id) + '">' + esc(g.name) + '</button>').join('') + '</div></div>' + recordTable(all.slice(0,5),true) + '</section>'
      + '<section class="card card-pad audit-card"><div class="card-header"><div class="card-title">' + icon('audit') + '<h2>回覆審計</h2></div></div><div class="tabs"><button class="tab active" data-action="open-pending">待檢視 ' + tally.pending + '</button><button class="tab" data-action="open-reviewed">已檢視 ' + tally.reviewed + '</button></div><div class="pending-list">' + (pending.length ? pending.slice(0,2).map(r => '<button class="pending-item" data-action="open-record" data-id="' + r.id + '"><time>' + r.time + '</time><span><strong>' + esc(r.content) + '</strong><small>' + esc(groupName(r.groupId)) + ' · ' + esc(accountName(r.accountId)) + '</small></span>' + icon('arrow') + '</button>').join('') : '<p class="muted">沒有待處理項目。</p>') + '</div>'
      + '<div class="detail-mini"><h3>選取紀錄</h3>' + (selected ? metadata(selected) + '<div class="content-box">' + esc(selected.content) + '</div><button class="button primary full" data-action="open-record" data-id="' + selected.id + '">開啟紀錄工作頁 ' + icon('arrow') + '</button>' : empty()) + '</div></section>'
      + '<div class="lower-grid"><section class="card card-pad"><div class="card-header"><div class="card-title">' + icon('accounts') + '<h2>帳號管理</h2></div><button class="text-button" data-action="navigate" data-route="accounts">查看全部</button></div>' + accountTable(C.accounts,true) + '<div class="subtle-footer">線上資料 · 沒有啟停帳號的後端操作</div></section><section class="card card-pad"><div class="card-title">' + icon('media') + '<h2>媒體測試</h2></div><div class="media-empty">' + icon('file') + '<h3>未啟動</h3><p>尚未選擇媒體</p><button class="button secondary" data-action="navigate" data-route="media">選擇媒體</button></div></section></div></div>';
  }
  function statusTabs(tally) {
    return '<div class="tabs">' + [['all','全部',tally.total],['pending','待檢視',tally.pending],['reviewed','已檢視',tally.reviewed]].map(([status,label,n]) => '<button class="tab' + (state.filters.status === status ? ' active' : '') + '" data-action="status" data-status="' + status + '" aria-pressed="' + (state.filters.status === status) + '">' + label + ' ' + n + '</button>').join('') + '</div>';
  }
  function groupPane() {
    return '<section class="card group-pane"><div class="card-header"><h2>群組</h2>' + icon('groups') + '</div><div class="group-list">' + C.groups.map(g => '<div class="group-choice' + (state.filters.groupId === g.id ? ' active' : '') + '"><button data-action="choose-group" data-id="' + g.id + '" aria-pressed="' + (state.filters.groupId === g.id) + '">' + icon('groups') + esc(g.name) + '</button><button class="icon-button" data-action="favorite" data-id="' + g.id + '" aria-label="' + (state.favorites.includes(g.id) ? '取消收藏' : '收藏') + esc(g.name) + '" aria-pressed="' + state.favorites.includes(g.id) + '">' + (state.favorites.includes(g.id) ? '★' : '☆') + '</button></div>').join('') + '</div><p class="group-note">已收藏的群組顯示於側欄。</p></section>';
  }
  function detailPane(filtered) {
    const r = filtered.find(x => x.id === state.selectedId);
    if (!r) return '<section class="card detail-pane"><h2>紀錄詳情</h2>' + empty('尚未選取紀錄','清单有結果後，點選時間開啟詳情。') + '</section>';
    const index = filtered.findIndex(x => x.id === r.id);
    return '<section class="card detail-pane" id="record-detail" tabindex="-1" aria-label="紀錄詳情"><div class="card-header"><h2>紀錄詳情</h2><div class="detail-arrows"><button class="icon-button" data-action="move-record" data-delta="-1" aria-label="上一筆紀錄"' + (index === 0 ? ' disabled' : '') + '>‹</button><span>' + (index+1) + ' / ' + filtered.length + '</span><button class="icon-button" data-action="move-record" data-delta="1" aria-label="下一筆紀錄"' + (index === filtered.length-1 ? ' disabled' : '') + '>›</button></div></div>'
      + metadata(r) + '<dl class="metadata"><dt>事件</dt><dd>' + esc(r.event) + '</dd><dt>狀態</dt><dd>' + statusBadge(r.status) + '</dd></dl><div class="detail-section"><h3>回覆內容</h3><div class="content-box">' + esc(r.content) + '</div><div class="detail-actions"><button class="button primary" data-action="review" data-id="' + r.id + '"' + (r.status === 'reviewed' ? ' disabled' : '') + '>' + (r.status === 'reviewed' ? '已完成檢視' : '標記已檢視') + '</button><button class="button secondary" data-action="source">查看來源</button></div><p class="keyboard-hint">↑ ↓ 切換紀錄 · 審閱變更在示範資料中生效</p><details class="record-source" id="source-content"><summary>來源訊息 · 合成資料</summary><p>' + esc(r.source) + '</p></details></div></section>';
  }
  function workspace() {
    const filtered = scoped(), base = scoped('all');
    const page = C.paginate(filtered,state.page);
    state.page = page.page;
    if (!filtered.some(r => r.id === state.selectedId)) state.selectedId = page.items[0]?.id || null;
    const pages = Array.from({length:page.pages}, (_, i) => '<button data-action="page" data-page="' + (i+1) + '" class="' + (i+1 === page.page ? 'current' : '') + '" aria-label="第 ' + (i+1) + ' 頁"' + (i+1 === page.page ? ' aria-current="page"' : '') + '>' + (i+1) + '</button>').join('');
    const savedSelect = state.savedViews.length ? '<select id="saved-view-select" class="saved-view-select" aria-label="套用已儲存檢視"><option value="">已儲存的檢視</option>' + state.savedViews.map(v => '<option value="' + esc(v.id) + '">' + esc(v.name) + '</option>').join('') + '</select>' : '';
    return '<div class="breadcrumb"><a href="#overview">工作總覽</a><span>/</span><span>' + labels[state.route] + '</span></div>' + heading('紀錄工作頁','保留篩選，逐筆檢視',savedSelect + '<button class="button secondary" data-action="save-view">' + icon('star') + '儲存檢視</button>')
      + filters() + '<div class="workspace">' + groupPane() + '<section class="card records-pane"><div class="card-header"><h2>事件紀錄</h2><input id="record-query" class="record-search" type="search" value="' + esc(state.filters.query) + '" placeholder="搜尋事件內容" aria-label="搜尋事件內容"></div>' + statusTabs(C.counts(base)) + recordTable(page.items)
      + '<div class="pagination"><span>顯示 ' + page.from + '–' + page.to + '，共 ' + page.total + ' 筆</span><div class="page-buttons"><button data-action="page" data-page="' + (page.page-1) + '" aria-label="上一頁"' + (page.page===1?' disabled':'') + '>‹</button>' + pages + '<button data-action="page" data-page="' + (page.page+1) + '" aria-label="下一頁"' + (page.page===page.pages?' disabled':'') + '>›</button></div></div></section>' + detailPane(filtered) + '</div>';
  }
  function accountsPage() {
    const filtered = C.accounts.filter(a => a.name.includes(state.accountQuery.trim()) && (state.accountStatus === 'all' || a.enabled === (state.accountStatus === 'enabled')));
    return heading('帳號管理','清楚呈現每個水軍帳號的狀態') + '<section class="card card-pad"><div class="toolbar"><input id="account-query" type="search" placeholder="搜尋帳號名稱" value="' + esc(state.accountQuery) + '" aria-label="搜尋帳號名稱"><select id="account-status" aria-label="帳號狀態">' + [['all','全部狀態'],['enabled','已啟用'],['paused','已暫停']].map(([v,l]) => '<option value="' + v + '"' + (v===state.accountStatus?' selected':'') + '>' + l + '</option>').join('') + '</select><span class="muted">' + filtered.length + ' 個合成帳號</span></div>' + accountTable(filtered) + '<div class="subtle-footer">點擊帳號查看紀錄。本版未連接帳號啟停、登入或刪除操作。</div></section>';
  }
  function groupsPage() {
    return heading('群組總管','收藏常用群組，快速回到工作脈絡') + '<section class="card card-pad"><div class="table-wrap"><table><thead><tr><th>群組</th><th>示範紀錄</th><th>待檢視</th><th>收藏</th><th>操作</th></tr></thead><tbody>' + C.groups.map(g => { const tally=C.counts(state.records.filter(r => r.groupId===g.id)); return '<tr><td>' + esc(g.name) + '</td><td>' + tally.total + '</td><td>' + tally.pending + '</td><td><button class="icon-button" data-action="favorite" data-id="' + g.id + '" aria-label="切換收藏 ' + esc(g.name) + '" aria-pressed="' + state.favorites.includes(g.id) + '">' + (state.favorites.includes(g.id)?'★':'☆') + '</button></td><td><button class="text-button" data-action="group-records" data-id="' + g.id + '">檢視紀錄 →</button></td></tr>'; }).join('') + '</tbody></table></div></section>';
  }
  function mediaPage() {
    const file = state.media;
    return heading('媒體測試','本機檔案預覽，不上傳、不發送') + '<div class="media-layout"><section class="card card-pad"><div class="card-title">' + icon('media') + '<h2>預覽媒體</h2></div><div class="media-empty">' + (file ? '<img class="file-preview" src="' + esc(state.mediaUrl) + '" alt="本機選取圖片的預覽"><h3>' + esc(file.name) + '</h3><p>' + (file.size/1024).toFixed(1) + ' KB · ' + esc(file.type) + '</p>' : icon('file') + '<h3>尚未選擇媒體</h3><p>選擇 PNG、JPEG 或 WebP，最大 10 MB。</p>') + '<input id="media-input" type="file" accept="image/png,image/jpeg,image/webp" hidden><button class="button secondary" data-action="choose-media">選擇圖片</button>' + (file?'<button class="text-button" data-action="clear-media">清除預覽</button>':'') + '</div></section><section class="card card-pad"><h2>處理狀態</h2><dl class="metadata"><dt>本機預覽</dt><dd>' + (file?'已選擇檔案':'未啟動') + '</dd><dt>網路傳輸</dt><dd>未連接</dd><dt>後端測試</dt><dd>尚未實作</dd></dl><div class="notice neutral">此頁展示檔案選擇與空狀態；不會呼叫圖片生成或訊息發送服務。</div></section></div>';
  }
  function preferencesPage() {
    return heading('顯示偏好','個人化本機介面，不變更服務設定') + '<div class="preferences-grid"><section class="card card-pad"><h2>閱讀與密度</h2><div class="preference-row"><div><h3>表格密度</h3><p>調整行距，保留資訊與可讀性。</p></div><select id="density-select" aria-label="表格密度"><option value="comfortable"' + (state.density==='comfortable'?' selected':'') + '>舒適</option><option value="dense"' + (state.density==='dense'?' selected':'') + '>緊湊</option></select></div><div class="preference-row"><div><h3>重設示範</h3><p>清除本機收藏、檢視及合成資料的審閱狀態。</p></div><button class="button secondary" data-action="reset-demo">重設</button></div></section><section class="card card-pad"><h2>已儲存的檢視</h2>' + (state.savedViews.length ? state.savedViews.map(v => '<div class="saved-item"><div><strong>' + esc(v.name) + '</strong><p>' + esc(groupName(v.filters.groupId)) + ' · ' + esc(accountName(v.filters.accountId)) + '</p></div><button class="text-button" data-action="apply-view" data-id="' + esc(v.id) + '">套用</button><button class="icon-button" data-action="delete-view" data-id="' + esc(v.id) + '" aria-label="刪除檢視 ' + esc(v.name) + '">×</button></div>').join('') : '<div class="empty"><p>在紀錄工作頁選擇「儲存檢視」。</p></div>') + '</section></div>';
  }
  function renderNav() {
    $('#primary-nav').innerHTML = C.routes.map(route => '<a class="nav-link' + (state.route===route?' active':'') + '" href="#' + route + '"' + (state.route===route?' aria-current="page"':'') + '>' + icon(route) + '<span>' + labels[route] + '</span>' + (route==='audit'?'<span class="count">' + C.counts(state.records).pending + '</span>':'') + '</a>').join('');
    $('#sidebar-favorites').innerHTML = state.favorites.length ? state.favorites.map(id => '<button class="nav-link" data-action="group-records" data-id="' + id + '">' + icon('groups') + '<span>' + esc(groupName(id)) + '</span></button>').join('') : '<p class="small muted">尚未收藏群組</p>';
  }
  function render(preserveFocus=false) {
    const focusId = preserveFocus ? document.activeElement?.id : null;
    const selection = preserveFocus ? document.activeElement?.selectionStart : null;
    const scroll = window.scrollY;
    document.body.classList.toggle('dense',state.density==='dense');
    renderNav();
    $('#main').innerHTML = state.route === 'board' && window.SDFBoard ? window.SDFBoard.view()
      : state.route === 'ops' && window.SDFOps ? window.SDFOps.view()
      : state.route === 'overview' ? overview() : ['monitor','audit'].includes(state.route) ? workspace() : state.route === 'accounts' ? accountsPage() : state.route === 'groups' ? groupsPage() : state.route === 'media' ? mediaPage() : preferencesPage();
    document.title = labels[state.route] + ' · SDF 控制台';
    if (focusId) {
      const input=document.getElementById(focusId);
      if (input) { input.focus({preventScroll:true}); if (selection!=null && input.setSelectionRange && input.type==='search') input.setSelectionRange(selection,selection); }
    }
    window.scrollTo(0,scroll);
  }
  function closeSidebar() {
    $('#sidebar').classList.remove('open'); $('#sidebar-scrim').hidden = true;
    $('#mobile-menu').setAttribute('aria-expanded','false');
    $('#sidebar').inert = mobileQuery.matches;
  }
  function navigate(route) {
    if (!C.routes.includes(route)) route='overview';
    state.scroll[state.route]=window.scrollY;
    state.route=route; closeSidebar();
    if (location.hash!=='#'+route) history.replaceState(null,'','#'+route);
    render(); window.scrollTo(0,state.scroll[route]||0); $('#main').focus({preventScroll:true});
  }
  function setFilter(key,value) {
    state.filters=C.sanitizeFilters({...state.filters,[key]:value}); state.page=1;
    state.selectedId=scoped()[0]?.id || null; persist(); render();
  }
  function openRecord(id) {
    const r=state.records.find(x=>x.id===id); if(!r)return;
    state.filters=C.sanitizeFilters({...state.filters,accountId:r.accountId,groupId:r.groupId,status:'all',query:''});
    state.selectedId=id; state.page=Math.floor(scoped().findIndex(x=>x.id===id)/5)+1; persist(); navigate('audit');
  }
  function selectRecord(id, moveFocus=true) {
    if (!scoped().some(r=>r.id===id)) return;
    state.selectedId=id; state.page=Math.floor(scoped().findIndex(r=>r.id===id)/5)+1;
    render(); if(moveFocus && matchMedia('(max-width:1250px)').matches) $('#record-detail')?.focus();
  }
  function moveRecord(delta) {
    const list=scoped(), index=list.findIndex(r=>r.id===state.selectedId), next=list[index+delta];
    if(next)selectRecord(next.id,false);
  }
  function applyView(id) {
    const view=state.savedViews.find(v=>v.id===id); if(!view)return;
    state.filters=C.sanitizeFilters(view.filters); state.page=1; state.selectedId=null; persist(); navigate('audit'); toast('已套用「'+view.name+'」');
  }
  const actions = {
    'board-pick':b=>window.SDFBoard&&window.SDFBoard.act('board-pick',b),
    'board-group':b=>window.SDFBoard&&window.SDFBoard.act('board-group',b),
    'board-review':b=>window.SDFBoard&&window.SDFBoard.act('board-review',b),
    navigate:b=>navigate(b.dataset.route),
    'choose-group':b=>setFilter('groupId',b.dataset.id),
    'reset-filters':()=>{state.filters={...C.defaultFilters};state.page=1;state.selectedId='record-01';persist();render();},
    'account-records':b=>{state.filters=C.sanitizeFilters({...C.defaultFilters,accountId:b.dataset.id,groupId:'all'});state.page=1;state.selectedId=null;persist();navigate('audit');},
    'group-records':b=>{state.filters=C.sanitizeFilters({...C.defaultFilters,accountId:'all',groupId:b.dataset.id});state.page=1;state.selectedId=null;persist();navigate('monitor');},
    'open-pending':()=>{state.filters.status='pending';state.page=1;state.selectedId=null;persist();navigate('audit');},
    'open-reviewed':()=>{state.filters.status='reviewed';state.page=1;state.selectedId=null;persist();navigate('audit');},
    status:b=>setFilter('status',b.dataset.status),
    'select-record':b=>{if(state.route==='overview')openRecord(b.dataset.id);else selectRecord(b.dataset.id);},
    'open-record':b=>openRecord(b.dataset.id),
    page:b=>{const page=C.paginate(scoped(),Number(b.dataset.page));state.page=page.page;state.selectedId=page.items[0]?.id||null;render();},
    'move-record':b=>moveRecord(Number(b.dataset.delta)),
    favorite:b=>{const id=b.dataset.id;if(!C.groups.some(g=>g.id===id))return;state.favorites=state.favorites.includes(id)?state.favorites.filter(x=>x!==id):[...state.favorites,id];persist();render();},
    review:b=>{
      const record=state.records.find(r=>r.id===b.dataset.id);if(!record||record.status==='reviewed')return;
      state.records=C.review(state.records,record.id);persist();render();
      toast('已標記為已檢視（標記存在本機）',()=>{state.records=state.records.map(r=>r.id===record.id?{...r,status:'pending'}:r);persist();render();toast('已復原審閱狀態');});
    },
    source:()=>{const details=$('#source-content');if(details){details.open=!details.open;if(details.open)details.scrollIntoView({block:'nearest'});}},
    'save-view':()=>{$('#view-name').value='';$('#save-dialog').showModal();$('#view-name').focus();},
    'close-save':()=>$('#save-dialog').close(),
    'close-search':()=>$('#search-dialog').close(),
    'apply-view':b=>applyView(b.dataset.id),
    'delete-view':b=>{state.savedViews=state.savedViews.filter(v=>v.id!==b.dataset.id);persist();render();toast('已刪除本機檢視');},
    'choose-media':()=>$('#media-input').click(),
    'clear-media':()=>{if(state.mediaUrl)URL.revokeObjectURL(state.mediaUrl);state.media=null;state.mediaUrl=null;render();},
    'reset-demo':()=>{
      if(!window.confirm('重設這個瀏覽器中的線上資料、收藏與儲存檢視？'))return;
      state.records=C.records.map(r=>({...r}));state.filters={...C.defaultFilters};state.savedViews=[];state.favorites=['feedback'];state.page=1;state.selectedId='record-01';state.density='comfortable';persist();render();toast('示範資料已重設');
    }
  };
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href^="#"]');
    if(active && link && C.routes.includes(link.getAttribute('href').slice(1))){event.preventDefault();navigate(link.getAttribute('href').slice(1));return;}
    const button=event.target.closest('[data-action]');if(button && !button.disabled && actions[button.dataset.action])actions[button.dataset.action](button);
  });
  document.addEventListener('change',event=>{
    const el=event.target;
    if(el.dataset.filter)setFilter(el.dataset.filter,el.value);
    if(el.id==='account-status'){state.accountStatus=['all','enabled','paused'].includes(el.value)?el.value:'all';render();}
    if(el.id==='density-select'){state.density=el.value==='dense'?'dense':'comfortable';persist();render();}
    if(el.id==='saved-view-select')applyView(el.value);
    if(el.id==='media-input'){
      const file=el.files[0];if(!file)return;
      if(!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size>10*1024*1024){toast('請選擇 10 MB 以下的 PNG、JPEG 或 WebP 圖片。');el.value='';return;}
      if(state.mediaUrl)URL.revokeObjectURL(state.mediaUrl);
      state.media=file;state.mediaUrl=URL.createObjectURL(file);render();toast('圖片在本機預覽，沒有上傳。');
    }
  });
  document.addEventListener('input',event=>{
    if(event.target.id==='record-query'){state.filters.query=event.target.value.slice(0,100);state.page=1;persist();render(true);}
    if(event.target.id==='account-query'){state.accountQuery=event.target.value;render(true);}
  });
  function showSearchResults() {
    const query=$('#command-query').value, results=C.search(query,state.records);
    $('#command-results').innerHTML = !query.trim() ? '<p class="empty">搜尋「服務帳號」、「使用者回饋」或「17:21」。</p>' : !results.length ? '<p class="empty">沒有符合的結果。</p>' : results.map(r=>'<button class="command-result" data-result-type="'+r.type+'" data-result-id="'+r.id+'">'+icon(r.type==='account'?'accounts':r.type==='group'?'groups':'audit')+'<span><strong>'+esc(r.label)+'</strong><small>'+esc(r.description)+'</small></span></button>').join('');
  }
  function openSearch(){if(!active)return;$('#command-query').value='';showSearchResults();$('#search-dialog').showModal();$('#command-query').focus();}
  $('#search-open').addEventListener('click',openSearch);
  $('#command-query').addEventListener('input',showSearchResults);
  $('#command-results').addEventListener('click',event=>{
    const b=event.target.closest('[data-result-id]');if(!b)return;
    $('#search-dialog').close();
    if(b.dataset.resultType==='record')openRecord(b.dataset.resultId);
    else if(b.dataset.resultType==='account')actions['account-records']({dataset:{id:b.dataset.resultId}});
    else actions['group-records']({dataset:{id:b.dataset.resultId}});
  });
  $('#save-form').addEventListener('submit',event=>{
    event.preventDefault();const name=$('#view-name').value.trim();if(!name)return;
    const existing=state.savedViews.find(v=>v.name===name);
    if(existing)existing.filters={...state.filters};
    else if(state.savedViews.length>=12){toast('最多儲存 12 個檢視，請先刪除不需要的項目。');return;}
    else state.savedViews.push({id:'view-'+Date.now(),name:name.slice(0,40),filters:{...state.filters}});
    persist();$('#save-dialog').close();render();toast(existing?'已更新同名檢視':'檢視已儲存在此瀏覽器');
  });
  $('#mobile-menu').addEventListener('click',()=>{
    const open=$('#sidebar').classList.toggle('open');$('#sidebar-scrim').hidden=!open;$('#mobile-menu').setAttribute('aria-expanded',String(open));
    $('#sidebar').inert=!open && mobileQuery.matches;
    if(open)$('#primary-nav a')?.focus();
  });
  mobileQuery.addEventListener('change',closeSidebar);
  $('#sidebar-scrim').addEventListener('click',closeSidebar);
  $('#toast-close').addEventListener('click',()=>{$('#toast').hidden=true;undoAction=null;clearTimeout(toastTimer);});
  $('#toast-undo').addEventListener('click',()=>{const fn=undoAction;undoAction=null;if(fn)fn();});
  document.addEventListener('keydown',event=>{
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&active){event.preventDefault();if(!document.querySelector('dialog[open]'))openSearch();return;}
    if(event.key==='Escape' && $('#sidebar').classList.contains('open')){closeSidebar();$('#mobile-menu').focus();}
    if(!active||!['audit','monitor'].includes(state.route)||document.querySelector('dialog[open]')||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;
    if(event.target.closest('input,textarea,select,button,a,[contenteditable="true"],summary'))return;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();moveRecord(event.key==='ArrowDown'?1:-1);}
  });
  window.addEventListener('hashchange',()=>{if(active)navigate(location.hash.slice(1));});
  $('#password-toggle').addEventListener('click',()=>{
    const input=$('#password'), show=input.type==='password';input.type=show?'text':'password';
    $('#password-toggle').textContent=show?'隱藏':'顯示';$('#password-toggle').setAttribute('aria-label',show?'隱藏密碼':'顯示密碼');$('#password-toggle').setAttribute('aria-pressed',String(show));
  });
  $('#login-form').addEventListener('submit',event=>{
    event.preventDefault();
    const errors=C.validateLogin($('#username').value,$('#password').value);
    for(const name of ['username','password']){$('#'+name+'-error').textContent=errors[name];$('#'+name).setAttribute('aria-invalid',String(Boolean(errors[name])));}
    $('#login-error').hidden=true;
    if(errors.username||errors.password){$('#'+(errors.username?'username':'password')).focus();return;}
    // No authentication backend is connected. Never send or persist these values.
    $('#password').value='';
    $('#login-error').textContent='尚未連接身分驗證。請使用「進入示範」；這不是帳密驗證結果。';
    $('#login-error').hidden=false;
  });
  $('#demo-enter').addEventListener('click',()=>{
    $('#login-form').reset();$('#password').type='password';$('#password-toggle').textContent='顯示';$('#password-toggle').setAttribute('aria-pressed','false');$('#password-toggle').setAttribute('aria-label','顯示密碼');
    $('#login-error').hidden=true;for(const n of ['username','password']){$('#'+n+'-error').textContent='';$('#'+n).removeAttribute('aria-invalid');}
    active=true;$('#login-screen').hidden=true;$('#app-shell').hidden=false;navigate(C.routes.includes(location.hash.slice(1))?location.hash.slice(1):'overview');
  });
  $('#demo-exit').addEventListener('click',()=>{
    active=false;closeSidebar();$('#app-shell').hidden=true;$('#login-screen').hidden=false;$('#toast').hidden=true;clearTimeout(toastTimer);undoAction=null;document.title='SDF 控制台 · 調度台 v2';window.scrollTo(0,0);$('#username').focus();
  });
  window.addEventListener('beforeunload',()=>{if(state.mediaUrl)URL.revokeObjectURL(state.mediaUrl);});
  document.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);});
  // 給 live.js 用：外部注入真資料後重讀一次（state 只在初始化時抓一次資料）
  window.SDFApp = {
    isActive: () => active,
    reseed: () => {
      state.filters = {...C.defaultFilters};
      state.records = C.records.map(r => ({...r}));
      state.favorites = C.groups.length ? [C.groups[0].id] : [];
      state.page = 1;
      state.selectedId = state.records.length ? state.records[0].id : null;
      state.accountQuery = '';
      state.accountStatus = 'all';
    },
    refresh: () => {
      if (active) navigate(location.hash.slice(1) || 'overview');
    },
  };
})();

