(function () {
  'use strict';
  const { $, api, toast, h } = ft;
  const root = $('#exam-root'); const id = root.dataset.exam;
  const reload = () => location.reload();
  const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };

  // practice
  on('#start-form', 'submit', async (e) => {
    e.preventDefault();
    try {
      const count = $('#count').value;
      const r = await api('POST', `/ui/exams/${id}/sessions`, { mode: $('#mode').value, count: count ? Number(count) : null, shuffle: true });
      location.href = ft.BASE + '/practice/' + r.id;
    } catch (err) { toast(err.message, true); }
  });

  // exam actions
  const dlg = $('#exam-dialog');
  on('#rename-exam', 'click', () => { $('#exam-title').value = root.dataset.title; $('#exam-desc').value = root.dataset.desc; dlg.showModal(); });
  on('#exam-cancel', 'click', () => dlg.close());
  on('#exam-form', 'submit', async (e) => {
    e.preventDefault();
    try { await api('PATCH', '/ui/exams/' + id, { title: $('#exam-title').value, description: $('#exam-desc').value }); reload(); }
    catch (err) { toast(err.message, true); }
  });
  on('#reset-exam', 'click', async () => {
    if (!confirm('Reset all progress for this exam? Questions are kept.')) return;
    try { await api('POST', `/ui/exams/${id}/reset`); reload(); } catch (err) { toast(err.message, true); }
  });
  on('#delete-exam', 'click', async () => {
    if (!confirm('Delete this exam, all its questions and your progress? This cannot be undone.')) return;
    try { await api('DELETE', '/ui/exams/' + id); location.href = ft.BASE + '/'; } catch (err) { toast(err.message, true); }
  });

  // import
  function showResults(results) {
    const box = $('#import-result'); box.replaceChildren();
    results.forEach((r) => {
      const msg = r.added ? `${r.added} question(s) added` + (r.duplicates ? `, ${r.duplicates} duplicate(s) skipped` : '') : (r.error || 'Nothing added');
      box.append(h('div', { class: 'res' }, h('b', {}, r.name), ': ', msg, r.added && r.error ? ' (' + r.error + ')' : ''));
    });
    if (results.some((r) => r.added)) box.append(h('p', {}, h('button', { class: 'btn', type: 'button', onclick: reload }, 'Show questions')));
  }
  on('#upload-form', 'submit', async (e) => {
    e.preventDefault();
    const files = $('#files').files; if (!files.length) return toast('Choose at least one file.', true);
    const fd = new FormData(); for (const f of files) fd.append('files', f);
    fd.append('ai', $('#ai-upload').checked ? 'true' : 'false');
    fd.append('generate', $('#gen-upload').checked ? 'true' : 'false');
    const btn = e.submitter; btn.disabled = true; btn.textContent = 'Importing…';
    try { showResults((await api('POST', `/ui/exams/${id}/upload`, fd)).results); }
    catch (err) { toast(err.message, true); } finally { btn.disabled = false; btn.textContent = 'Import'; }
  });
  on('#scrape-form', 'submit', async (e) => {
    e.preventDefault();
    const urls = $('#urls').value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!urls.length) return toast('Enter at least one URL.', true);
    const btn = e.submitter; btn.disabled = true; btn.textContent = 'Scraping…';
    try { showResults((await api('POST', `/ui/exams/${id}/scrape`, { urls, ai: $('#ai-scrape').checked, generate: $('#gen-scrape').checked })).results); }
    catch (err) { toast(err.message, true); } finally { btn.disabled = false; btn.textContent = 'Scrape'; }
  });
  document.querySelectorAll('[data-del-source]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Remove this source and all questions imported from it?')) return;
    try { await api('DELETE', '/ui/sources/' + b.dataset.delSource); reload(); } catch (err) { toast(err.message, true); }
  }));

  // question list
  on('#qfilter', 'change', (e) => {
    const v = e.target.value;
    document.querySelectorAll('details.q').forEach((d) => {
      d.hidden = v === 'nokey' ? d.dataset.nokey !== '1' : (v && d.dataset.status !== v);
    });
  });
  document.querySelectorAll('[data-del-q]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Delete this question?')) return;
    try { await api('DELETE', '/ui/questions/' + b.dataset.delQ); reload(); } catch (err) { toast(err.message, true); }
  }));
  document.querySelectorAll('[data-accept]').forEach((b) => b.addEventListener('click', async () => {
    try { await api('POST', `/ui/questions/${b.dataset.accept}/accept-ai`); reload(); } catch (err) { toast(err.message, true); }
  }));
  document.querySelectorAll('[data-verify]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true; b.textContent = 'Checking…';
    try { await api('POST', `/ui/questions/${b.dataset.verify}/verify`); reload(); }
    catch (err) { toast(err.message, true); b.disabled = false; b.textContent = 'Check with ChatGPT'; }
  }));

  // edit dialog
  const qd = $('#q-dialog'); let editing = null;
  on('#q-cancel', 'click', () => qd.close());
  document.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', async () => {
    try {
      const q = await api('GET', '/ui/questions/' + b.dataset.edit); editing = q.id;
      $('#q-text').value = q.text; $('#q-options').value = q.options.join('\n');
      $('#q-correct').value = q.correct.map((i) => 'ABCDEFGH'[i]).join(',');
      $('#q-answer').value = q.answer_text; $('#q-expl').value = q.explanation; qd.showModal();
    } catch (err) { toast(err.message, true); }
  }));
  on('#q-form', 'submit', async (e) => {
    e.preventDefault();
    const options = $('#q-options').value.split('\n').map((s) => s.trim()).filter(Boolean);
    const correct = ($('#q-correct').value.toUpperCase().match(/[A-H]/g) || []).map((c) => 'ABCDEFGH'.indexOf(c));
    try {
      await api('PATCH', '/ui/questions/' + editing, { text: $('#q-text').value, options, correct, answer_text: $('#q-answer').value, explanation: $('#q-expl').value });
      reload();
    } catch (err) { toast(err.message, true); }
  });

  // bulk ChatGPT check
  let timer = null;
  async function poll() {
    try {
      const s = await api('GET', `/ui/exams/${id}/verify/status`);
      const box = $('#verify-progress'); box.classList.toggle('hidden', !s.total);
      const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
      $('#verify-bar').style.width = pct + '%';
      $('#verify-text').textContent = `${s.done} of ${s.total} checked` + (s.errors ? ` · ${s.errors} failed` : '') + (s.message ? ' · ' + s.message : '');
      if (s.running) { timer = setTimeout(poll, 1500); }
      else if (s.total && s.done) { setTimeout(reload, 1200); }
    } catch (err) { /* transient */ }
  }
  async function startVerify(redo) {
    try { await api('POST', `/ui/exams/${id}/verify` + (redo ? '?redo=true' : '')); poll(); }
    catch (err) { toast(err.message, true); }
  }
  on('#verify-all', 'click', () => startVerify(false));
  on('#verify-redo', 'click', () => { if (confirm('Check every question again?')) startVerify(true); });
  if (root.dataset.running === '1') poll();
  window.addEventListener('beforeunload', () => clearTimeout(timer));
})();
