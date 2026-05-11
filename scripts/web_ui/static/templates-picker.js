(function () {
  const dataEl = document.getElementById('template-data');
  const picker = document.getElementById('template-picker');
  const textarea = document.getElementById('message-body');
  const saveBtn = document.getElementById('save-template-btn');
  const status = document.getElementById('template-status');

  if (!dataEl || !picker || !textarea || !saveBtn) {
    return;
  }

  let templates = [];
  try {
    templates = JSON.parse(dataEl.textContent || '[]');
  } catch (err) {
    templates = [];
  }

  function setStatus(text, kind) {
    status.textContent = text || '';
    status.dataset.kind = kind || '';
  }

  function findTemplate(id) {
    return templates.find((t) => t.id === id) || null;
  }

  picker.addEventListener('change', () => {
    const id = picker.value;
    if (!id) return;
    const tpl = findTemplate(id);
    if (!tpl) {
      setStatus('Template not found.', 'error');
      return;
    }
    if (textarea.value.trim() && textarea.value !== tpl.body) {
      const ok = confirm('Replace the current message body with template "' + tpl.name + '"?');
      if (!ok) {
        picker.value = '';
        return;
      }
    }
    textarea.value = tpl.body;
    setStatus('Loaded ' + tpl.name + '.', 'ok');
  });

  saveBtn.addEventListener('click', async () => {
    const body = textarea.value;
    if (!body.trim()) {
      setStatus('Type a message first.', 'error');
      return;
    }
    const name = prompt('Name for this template?');
    if (name === null) return;
    if (!name.trim()) {
      setStatus('Name is required.', 'error');
      return;
    }

    const form = new FormData();
    form.append('name', name);
    form.append('body', body);
    setStatus('Saving…', '');
    try {
      const res = await fetch('/api/templates', { method: 'POST', body: form });
      if (!res.ok) {
        let detail = 'Save failed.';
        try { detail = (await res.json()).detail || detail; } catch {}
        setStatus(detail, 'error');
        return;
      }
      const tpl = await res.json();
      templates.unshift(tpl);
      const opt = document.createElement('option');
      opt.value = tpl.id;
      opt.textContent = tpl.name;
      picker.insertBefore(opt, picker.children[1] || null);
      picker.value = tpl.id;
      setStatus('Saved as ' + tpl.name + '.', 'ok');
    } catch (err) {
      setStatus('Network error: ' + err, 'error');
    }
  });
})();
