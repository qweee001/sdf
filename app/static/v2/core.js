(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SDFCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  let groups = [
    { id: 'feedback', name: '使用者回饋' },
    { id: 'product', name: '產品交流' },
    { id: 'service', name: '服務通知' }
  ];
  let accounts = Array.from({length: 6}, (_, i) => ({
    id: 'account-' + (i + 1), name: '服務帳號 ' + String(i + 1).padStart(2, '0'),
    type: '自動化帳號', enabled: i < 4
  }));
  const times = ['17:21','17:18','17:12','17:08','16:54','16:42','16:36','16:22','16:10','15:54','15:40','15:21','15:08','14:50','14:36','14:20','14:05','13:48'];
  let records = times.map((time, index) => ({
    id: 'record-' + String(index + 1).padStart(2, '0'),
    timestamp: '2026-10-06T' + time + ':00+08:00', time,
    groupId: 'feedback', accountId: 'account-3',
    event: index % 2 === 0 ? '收到一則新訊息' : '新增回覆紀錄',
    content: index < 2 ? '回覆內容待檢視' : '已收到您的回饋，感謝提供使用體驗。',
    source: '此處呈現合成的來源訊息，用於預覽內容閱讀與審閱流程。',
    status: index < 2 ? 'pending' : 'reviewed'
  }));
  const defaultFilters = { accountId: 'account-3', groupId: 'feedback', status: 'all', query: '', range: '24h' };
  const routes = ['overview', 'board', 'accounts', 'monitor', 'audit', 'media', 'groups', 'ops', 'preferences'];
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }
  function sanitizeFilters(input) {
    const data = input && typeof input === 'object' ? input : {};
    return {
      accountId: data.accountId === 'all' || accounts.some(a => a.id === data.accountId) ? data.accountId : defaultFilters.accountId,
      groupId: data.groupId === 'all' || groups.some(g => g.id === data.groupId) ? data.groupId : defaultFilters.groupId,
      status: ['all','pending','reviewed'].includes(data.status) ? data.status : 'all',
      query: typeof data.query === 'string' ? data.query.slice(0, 100) : '',
      range: ['24h','7d','all'].includes(data.range) ? data.range : '24h'
    };
  }
  function filterRecords(items, rawFilters) {
    const filters = sanitizeFilters(rawFilters);
    const term = filters.query.trim().toLocaleLowerCase();
    const anchor = Date.now();  // 接上真後端後改成相對於現在（原本是示範固定日期）
    const maxAge = filters.range === '24h' ? 86400000 : filters.range === '7d' ? 604800000 : Infinity;
    return items.filter(item => {
      const account = accounts.find(a => a.id === item.accountId);
      const group = groups.find(g => g.id === item.groupId);
      const haystack = [item.time,item.event,item.content,account?.name,group?.name].join(' ').toLocaleLowerCase();
      const age = anchor - Date.parse(item.timestamp);
      return (filters.accountId === 'all' || item.accountId === filters.accountId)
        && (filters.groupId === 'all' || item.groupId === filters.groupId)
        && (filters.status === 'all' || item.status === filters.status)
        && age >= 0 && age <= maxAge
        && (!term || haystack.includes(term));
    }).sort((a,b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
  }
  function counts(items) {
    return { total: items.length, pending: items.filter(r => r.status === 'pending').length, reviewed: items.filter(r => r.status === 'reviewed').length };
  }
  function paginate(items, requestedPage, size = 5) {
    const pages = Math.max(1, Math.ceil(items.length / size));
    const numeric = Number.isFinite(Number(requestedPage)) ? Math.floor(Number(requestedPage)) : 1;
    const page = Math.max(1, Math.min(pages, numeric));
    const offset = (page - 1) * size;
    return { page, pages, items: items.slice(offset, offset + size), from: items.length ? offset + 1 : 0, to: Math.min(offset + size, items.length), total: items.length };
  }
  function review(items, id) {
    return items.map(item => item.id === id ? {...item, status: 'reviewed'} : {...item});
  }
  function validateLogin(username, password) {
    return { username: String(username || '').trim() ? '' : '請輸入帳號', password: String(password || '') ? '' : '請輸入密碼' };
  }
  function search(query, items) {
    const term = query.trim().toLocaleLowerCase().slice(0, 100);
    if (!term) return [];
    return [
      ...accounts.map(a => ({type:'account',id:a.id,label:a.name,description:a.type})),
      ...groups.map(g => ({type:'group',id:g.id,label:g.name,description:'群組'})),
      ...items.map(r => ({type:'record',id:r.id,label:r.time + ' · ' + r.event,description:r.content}))
    ].filter(x => (x.label + ' ' + x.description).toLocaleLowerCase().includes(term)).slice(0, 24);
  }
  function sanitizeSavedViews(value) {
    if (!Array.isArray(value)) return [];
    return value.filter(v => v && typeof v.name === 'string').slice(0, 12).map((v,i) => ({
      id: 'view-' + i, name:v.name.trim().slice(0,40) || '未命名檢視', filters:sanitizeFilters(v.filters)
    }));
  }
  // 由 live.js 注入真後端資料；沒注入時仍可獨立當示範跑。
  // 匯出的 groups/accounts/records 是陣列參照，重新綁定變數不會更新它們，
  // 所以一律原地替換（splice 清空後 push），閉包與外部看到的都是同一份。
  function replaceInPlace(target, source) {
    if (!Array.isArray(source)) return;
    target.splice(0, target.length);
    source.forEach(item => target.push(item));
  }
  function setData(next) {
    const data = next && typeof next === 'object' ? next : {};
    if (data.groups && data.groups.length) replaceInPlace(groups, data.groups);
    if (data.accounts && data.accounts.length) replaceInPlace(accounts, data.accounts);
    if (Array.isArray(data.records)) replaceInPlace(records, data.records);
    if (data.defaultFilters && typeof data.defaultFilters === 'object') {
      Object.assign(defaultFilters, data.defaultFilters);
    }
    return {groups: groups.length, accounts: accounts.length, records: records.length};
  }
  return {groups,accounts,records,defaultFilters,routes,escapeHtml,sanitizeFilters,filterRecords,counts,paginate,review,validateLogin,search,sanitizeSavedViews,setData};
});

