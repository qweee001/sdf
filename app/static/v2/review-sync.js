/* v2 接上真審閱狀態：狀態從 /records 讀、切換寫回 DB（不再只存 localStorage）。
 *
 * 作法是包住 SDFCore.review：先照原本流程更新畫面，再 POST 到 /api/records/review。
 * record_key 由後端提供，跨帳號、跨裝置都一致。
 */
(function () {
  'use strict';

  const REVIEW_URL = '/api/records/review';
  let reviewPatched = false;

  function patchReview(core, groupId) {
    if (!core || reviewPatched || typeof core.review !== 'function') return;
    const original = core.review;
    core.review = function (record, status) {
      const result = original.apply(this, arguments);
      const key = record && record.record_key;
      if (!key) return result;
      fetch(REVIEW_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: Number(groupId), record_key: key, reviewed: status === 'reviewed' }),
      }).then((res) => {
        if (!res.ok) throw new Error('review ' + res.status);
        window.__sdfReviewSynced = (window.__sdfReviewSynced || 0) + 1;
      }).catch(() => {
        const toast = document.getElementById('toast');
        if (toast) {
          toast.textContent = '審閱狀態沒有寫回後端（只在本機生效）';
          toast.hidden = false;
        }
      });
      return result;
    };
    reviewPatched = true;
  }

  window.addEventListener('DOMContentLoaded', () => {
    const timer = setInterval(() => {
      const live = window.__sdfLive;
      if (!live || !live.ok) return;
      clearInterval(timer);
      patchReview(window.SDFCore, live.groupId);
    }, 300);
    setTimeout(() => clearInterval(timer), 8000);
  });
})();
