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
})();
