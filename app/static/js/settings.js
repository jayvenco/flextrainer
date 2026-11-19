(function () {
  'use strict';
  const { $, api, toast } = ft;
  const reload = () => location.reload();

  $('#gpt-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const key = $('#gpt-key').value.trim();
    try { await api('POST', '/ui/settings/openai', { key: key || null, model: $('#gpt-model').value.trim() }); toast('Saved'); setTimeout(reload, 600); }
    catch (err) { toast(err.message, true); }
  });
  $('#gpt-clear').addEventListener('click', async () => {
    if (!confirm('Remove the stored ChatGPT key?')) return;
    try { await api('POST', '/ui/settings/openai', { key: '', model: $('#gpt-model').value.trim() }); reload(); } catch (err) { toast(err.message, true); }
  });
  $('#gpt-test').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { const r = await api('POST', '/ui/settings/openai/test'); toast('Connection works (model ' + r.model + ')'); }
    catch (err) { toast(err.message, true); } finally { e.target.disabled = false; }
  });

  $('#backup-cfg').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api('POST', '/ui/settings/backup', { enabled: $('#b-enabled').checked, keep: Number($('#b-keep').value) }); toast('Saved'); }
    catch (err) { toast(err.message, true); }
  });
  $('#b-now').addEventListener('click', async () => {
    try { await api('POST', '/ui/backups'); reload(); } catch (err) { toast(err.message, true); }
  });
  document.querySelectorAll('[data-del-backup]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Delete this backup?')) return;
    try { await api('DELETE', '/ui/backups/' + encodeURIComponent(b.dataset.delBackup)); reload(); } catch (err) { toast(err.message, true); }
  }));
  $('#b-import').addEventListener('change', async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (!confirm('Restore from "' + f.name + '"? All current data will be replaced (a safety copy is made first).')) { e.target.value = ''; return; }
    const fd = new FormData(); fd.append('file', f);
    try { await api('POST', '/ui/backups/import', fd); toast('Restored'); setTimeout(() => { location.href = ft.BASE + '/'; }, 800); }
    catch (err) { toast(err.message, true); e.target.value = ''; }
  });
})();
