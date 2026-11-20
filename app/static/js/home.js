(function () {
  'use strict';
  const { $, api, toast } = ft;
  const dlg = $('#exam-dialog');
  const open = () => { dlg.showModal(); $('#exam-title').focus(); };
  ['#new-exam', '#new-exam-2'].forEach((s) => { const b = $(s); if (b) b.addEventListener('click', open); });
  $('#exam-cancel').addEventListener('click', () => dlg.close());
  $('#exam-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const r = await api('POST', '/ui/exams', { title: $('#exam-title').value, description: $('#exam-desc').value });
      location.href = ft.BASE + '/exams/' + r.id;
    } catch (err) { toast(err.message, true); }
  });
  document.querySelectorAll('[data-quick-start]').forEach((b) => b.addEventListener('click', async () => {
    try {
      const r = await api('POST', '/ui/exams/' + b.dataset.quickStart + '/sessions', { mode: 'smart', count: 20, shuffle: true });
      location.href = ft.BASE + '/practice/' + r.id;
    } catch (err) { toast(err.message, true); }
  }));
  const act = (attr, fn) => document.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener('click', async () => {
    try { await fn(b); } catch (err) { toast(err.message, true); }
  }));
  act('data-home-dup', async (b) => { await api('POST', `/ui/exams/${b.dataset.homeDup}/duplicate`); location.reload(); });
  act('data-home-reset', async (b) => {
    if (!confirm('Reset all progress for this exam? Questions are kept.')) return;
    await api('POST', `/ui/exams/${b.dataset.homeReset}/reset`); location.reload();
  });
  act('data-home-del', async (b) => {
    if (!confirm(`Delete "${b.dataset.title}", all its questions and your progress? This cannot be undone.`)) return;
    await api('DELETE', '/ui/exams/' + b.dataset.homeDel); location.reload();
  });
})();
