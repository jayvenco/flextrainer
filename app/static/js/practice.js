(function () {
  'use strict';
  const { $, api, toast, h } = ft;
  const root = $('#practice'); const sid = root.dataset.session; const examId = root.dataset.exam;
  const stage = $('#stage'); const L = 'ABCDEFGH';
  let state = null, cur = null, selected = new Set(), phase = 'answer', busy = false, gpt = false;

  const setProgress = (done, total) => {
    const pct = total ? Math.round(100 * done / total) : 0;
    $('#pbar i').style.width = pct + '%'; $('#pbar').setAttribute('aria-valuenow', pct);
    $('#pcount').textContent = `${done} / ${total} answered`;
  };

  async function load() {
    state = await api('GET', '/ui/sessions/' + sid); gpt = state.gpt;
    setProgress(state.answered, state.total);
    const q = await api('GET', `/ui/sessions/${sid}/question`);
    if (q.done) return showSummary();
    cur = q.question; selected = new Set(); phase = 'answer';
    render(q.index, q.total);
  }

  function render(index, total) {
    stage.replaceChildren();
    stage.append(h('p', { class: 'small muted' }, `Question ${index} of ${total}`),
      h('p', { class: 'qtext', id: 'qtext' }, cur.text));
    if (cur.qtype === 'mc') {
      if (cur.multi) stage.append(h('p', { class: 'hint' }, 'Select all that apply.'));
      const list = h('ul', { class: 'optlist', role: 'group', 'aria-labelledby': 'qtext' });
      cur.options.forEach((o, i) => {
        list.append(h('li', {}, h('button', { class: 'opt', type: 'button', 'aria-pressed': 'false', 'data-i': i, onclick: () => toggle(i) },
          h('span', { class: 'k' }, L[i]), h('span', {}, o), h('span', { class: 'mark' }))));
      });
      stage.append(list);
      stage.append(h('div', { class: 'row', id: 'actions' },
        h('button', { class: 'btn', id: 'submit', type: 'button', disabled: true, onclick: submit }, 'Submit'),
        h('button', { class: 'btn ghost', type: 'button', onclick: skip }, 'Skip')));
    } else {
      stage.append(h('div', { class: 'row', id: 'actions' },
        h('button', { class: 'btn', id: 'reveal', type: 'button', onclick: reveal }, 'Reveal answer (R)'),
        h('button', { class: 'btn ghost', type: 'button', onclick: skip }, 'Skip')));
    }
    stage.append(h('div', { id: 'feedback', class: 'feedback', 'aria-live': 'polite' }));
    const first = stage.querySelector('.opt, #reveal'); if (first) first.focus({ preventScroll: true });
  }

  function toggle(i) {
    if (phase !== 'answer') return;
    if (cur.multi) { selected.has(i) ? selected.delete(i) : selected.add(i); }
    else { selected = new Set([i]); }
    document.querySelectorAll('.opt').forEach((b) => b.setAttribute('aria-pressed', selected.has(Number(b.dataset.i)) ? 'true' : 'false'));
    $('#submit').disabled = selected.size === 0;
  }

  const wrap = async (fn) => { if (busy) return; busy = true; try { await fn(); } catch (e) { toast(e.message, true); } finally { busy = false; } };

  function submit() {
    if (!selected.size) return;
    wrap(async () => {
      const btn = $('#submit'); btn.disabled = true; if (!cur.has_key && gpt) btn.textContent = 'Asking ChatGPT…';
      const r = await api('POST', `/ui/sessions/${sid}/answer`, { question_id: cur.id, chosen: [...selected].sort() });
      phase = 'feedback'; showFeedback(r); setProgress(r.progress.answered, r.progress.total);
      if (r.needs_check && gpt) check();
    });
  }

  function verdict(r) {
    if (r.correct === true) return h('div', { class: 'verdict ok', role: 'status' }, '✔ Correct');
    if (r.correct === false) return h('div', { class: 'verdict bad', role: 'status' }, '✘ Wrong');
    return h('div', { class: 'verdict neutral', role: 'status' }, 'No answer key for this question' + (gpt ? ' (ChatGPT could not provide one).' : '. Add a ChatGPT key in Settings to get one.'));
  }

  function showFeedback(r) {
    document.querySelectorAll('.opt').forEach((b) => {
      const i = Number(b.dataset.i); b.disabled = true;
      const right = r.correct_indexes.includes(i), picked = r.chosen.includes(i);
      b.classList.toggle('is-right', right); b.classList.toggle('is-wrong', r.correct !== null && picked && !right);
      b.querySelector('.mark').textContent = right ? '✔' : (picked && r.correct !== null ? '✘' : (picked ? '•' : ''));
    });
    const fb = $('#feedback'); fb.replaceChildren(verdict(r));
    if (r.key_source === 'chatgpt') fb.append(h('p', { class: 'src' }, 'The answer key comes from ChatGPT (your source had none).'));
    if (r.explanation) fb.append(h('p', {}, h('b', {}, 'Explanation: '), r.explanation));
    fb.append(h('div', { id: 'ai' }));
    renderAi(r.ai, r.disputed);
    const act = $('#actions'); act.replaceChildren(h('button', { class: 'btn', id: 'next', type: 'button', onclick: next }, 'Next (Enter)'));
    $('#next').focus();
  }

  function renderAi(ai, disputed) {
    const box = $('#ai'); if (!box) return; box.replaceChildren();
    if (ai.status === 'unchecked' || ai.status === 'error') return;
    if (disputed) box.append(h('p', { class: 'verdict neutral' }, '⚠ ChatGPT disagrees with your answer key' + (ai.correct.length ? ': it answers ' + ai.correct.map((i) => L[i]).join(', ') + '.' : '.') + ' Review this question on the exam page.'));
    else if (ai.status === 'confirmed') box.append(h('p', { class: 'src' }, '✔ Confirmed by ChatGPT'));
    if (ai.note) box.append(h('p', {}, h('b', {}, 'ChatGPT: '), ai.note));
    if (ai.link) box.append(h('p', { class: 'src' }, 'Suggested reference (verify before relying on it): ', h('a', { href: ai.link, target: '_blank', rel: 'noopener noreferrer nofollow' }, ai.link)));
  }

  async function check() {
    const qid = cur.id; const box = $('#ai'); if (box) box.textContent = 'ChatGPT is checking this question…';
    try {
      const q = await api('POST', `/ui/questions/${qid}/verify`);
      if (cur && cur.id === qid) {
        const ai = { status: q.verify_status, note: q.verify_note, link: q.verify_link, correct: q.ai_correct };
        renderAi(ai, q.verify_status === 'disputed');
      }
    } catch (e) { const b = $('#ai'); if (b) b.textContent = 'ChatGPT check failed: ' + e.message; }
  }

  function skip() {
    wrap(async () => { await api('POST', `/ui/sessions/${sid}/answer`, { question_id: cur.id, skip: true }); await load(); });
  }
  function next() { wrap(load); }

  function reveal() {
    wrap(async () => {
      const btn = $('#reveal'); btn.disabled = true; btn.textContent = 'Loading…';
      const r = await api('POST', `/ui/questions/${cur.id}/reveal`);
      phase = 'self';
      const fb = $('#feedback'); fb.replaceChildren(
        h('div', { class: 'open-answer' }, r.answer || 'No reference answer available.'));
      if (r.explanation) fb.append(h('p', {}, h('b', {}, 'Explanation: '), r.explanation));
      fb.append(h('div', { id: 'ai' })); renderAi(r.ai, r.disputed);
      fb.append(h('p', {}, 'Was your answer right?'));
      $('#actions').replaceChildren(
        h('button', { class: 'btn', type: 'button', onclick: () => self(true) }, 'I was right (Y)'),
        h('button', { class: 'btn ghost', type: 'button', onclick: () => self(false) }, 'I was wrong (N)'));
    });
  }
  function self(ok) {
    if (phase !== 'self') return;
    wrap(async () => {
      const r = await api('POST', `/ui/sessions/${sid}/answer`, { question_id: cur.id, self_correct: ok });
      phase = 'feedback'; setProgress(r.progress.answered, r.progress.total);
      $('#actions').replaceChildren(h('button', { class: 'btn', id: 'next', type: 'button', onclick: next }, 'Next (Enter)'));
      $('#next').focus();
    });
  }

  async function showSummary() {
    const s = await api('GET', `/ui/sessions/${sid}/summary`);
    setProgress(s.total, s.total); stage.classList.add('hidden');
    const box = $('#summary'); box.classList.remove('hidden'); box.replaceChildren(
      h('h1', {}, 'Session complete'),
      h('p', { class: 'score' }, s.accuracy === null ? '–' : s.accuracy + '%'),
      h('p', { class: 'muted' }, `${s.correct} correct · ${s.wrong} wrong · ${s.skipped} skipped`));
    if (s.wrong_items.length) {
      box.append(h('h2', {}, 'To review'), h('ul', { class: 'plain' }, s.wrong_items.map((w) => h('li', {}, w.text))));
    }
    box.append(h('div', { class: 'row' },
      h('a', { class: 'btn', href: ft.BASE + '/exams/' + examId }, 'Back to exam'),
      s.wrong_items.length ? h('button', { class: 'btn ghost', type: 'button', onclick: again }, 'Practise wrong answers') : null,
      h('a', { class: 'btn ghost', href: ft.BASE + '/' }, 'All exams')));
  }
  async function again() {
    try { const r = await api('POST', `/ui/exams/${examId}/sessions`, { mode: 'wrong', count: null, shuffle: true }); location.href = ft.BASE + '/practice/' + r.id; }
    catch (e) { toast(e.message, true); }
  }

  // keyboard shortcuts
  const keys = $('#keys-dialog');
  $('#show-keys').addEventListener('click', () => keys.showModal());
  $('#keys-close').addEventListener('click', () => keys.close());
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || ft.typing(e) || keys.open || !cur) return;
    const k = e.key.toLowerCase();
    if (k === '?') { keys.showModal(); return; }
    if (phase === 'answer' && cur.qtype === 'mc') {
      const n = /^[1-8]$/.test(k) ? Number(k) - 1 : L.toLowerCase().indexOf(k);
      if (k.length === 1 && n >= 0 && n < cur.options.length) { toggle(n); e.preventDefault(); return; }
      if (e.key === 'Enter' && selected.size) { e.preventDefault(); submit(); return; }
    }
    if (phase === 'feedback' && e.key === 'Enter') { e.preventDefault(); next(); return; }
    if (phase === 'answer' && cur.qtype === 'open' && k === 'r') { reveal(); return; }
    if (phase === 'self' && (k === 'y' || k === 'n')) { self(k === 'y'); return; }
    if (phase !== 'feedback' && k === 's') skip();
  });

  load().catch((e) => { stage.replaceChildren(h('p', { class: 'alert bad' }, e.message)); });
})();
