(function () {
  'use strict';

  const editor = document.getElementById('editor');
  if (!editor) return;
  const testId = editor.dataset.testId;

  function api(url, options) {
    return fetch(url, Object.assign({
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }, options || {})).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok && d.ok, status: r.status, data: d }; });
    });
  }

  function jsonApi(url, method, body) {
    return api(url, {
      method: method,
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify(body || {})
    });
  }

  function uploadImage(url, file) {
    const fd = new FormData();
    fd.append('image', file);
    return api(url, { method: 'POST', body: fd });
  }

  function reload() { window.location.reload(); }

  // Добавить вопрос
  const addBtn = document.getElementById('add-question-btn');
  addBtn.addEventListener('click', function () {
    const text = document.getElementById('new-question-text').value.trim();
    if (!text) { alert('Введите текст вопроса'); return; }
    const type = document.getElementById('new-question-type').value;
    jsonApi('/admin/api/tests/' + testId + '/questions/', 'POST', { text: text, question_type: type })
      .then(function (r) { r.ok ? reload() : alert(r.data.error || 'Ошибка'); });
  });

  editor.addEventListener('click', function (e) {
    const t = e.target;

    // Сохранить вопрос
    if (t.classList.contains('qe-save')) {
      const qEl = t.closest('.question-editor');
      const qid = qEl.dataset.questionId;
      const text = qEl.querySelector('.qe-text').value.trim();
      const type = qEl.querySelector('.qe-type-select').value;
      jsonApi('/admin/api/questions/' + qid + '/', 'PUT', { text: text, question_type: type })
        .then(function (r) { if (!r.ok) alert(r.data.error || 'Ошибка'); });
    }

    // Удалить вопрос
    if (t.classList.contains('qe-delete')) {
      if (!confirm('Удалить вопрос?')) return;
      const qid = t.closest('.question-editor').dataset.questionId;
      api('/admin/api/questions/' + qid + '/', { method: 'DELETE' }).then(function () { reload(); });
    }

    // Добавить вариант
    if (t.classList.contains('opt-add')) {
      const qid = t.closest('.question-editor').dataset.questionId;
      jsonApi('/admin/api/questions/' + qid + '/options/', 'POST', { text: 'Новый вариант', is_correct: false })
        .then(function (r) { r.ok ? reload() : alert(r.data.error || 'Ошибка'); });
    }

    // Удалить вариант
    if (t.classList.contains('opt-delete')) {
      if (!confirm('Удалить вариант?')) return;
      const oid = t.closest('.option-row').dataset.optionId;
      api('/admin/api/options/' + oid + '/', { method: 'DELETE' }).then(function () { reload(); });
    }
  });

  editor.addEventListener('change', function (e) {
    const t = e.target;

    // Сохранение текста варианта по blur/change
    if (t.classList.contains('opt-text')) {
      const row = t.closest('.option-row');
      const oid = row.dataset.optionId;
      jsonApi('/admin/api/options/' + oid + '/', 'PUT', { text: t.value.trim() })
        .then(function (r) { if (!r.ok) alert(r.data.error || 'Ошибка'); });
    }

    // Правильность варианта
    if (t.classList.contains('opt-correct')) {
      const row = t.closest('.option-row');
      const oid = row.dataset.optionId;
      jsonApi('/admin/api/options/' + oid + '/', 'PUT', { is_correct: t.checked })
        .then(function (r) { if (!r.ok) alert(r.data.error || 'Ошибка'); });
    }

    // Изображение вопроса
    if (t.classList.contains('qe-image-input') && t.files.length) {
      const qid = t.closest('.question-editor').dataset.questionId;
      uploadImage('/admin/api/questions/' + qid + '/image/', t.files[0])
        .then(function (r) { r.ok ? reload() : alert(r.data.error || 'Ошибка'); });
    }

    // Изображение варианта
    if (t.classList.contains('opt-image-input') && t.files.length) {
      const oid = t.closest('.option-row').dataset.optionId;
      uploadImage('/admin/api/options/' + oid + '/image/', t.files[0])
        .then(function (r) { r.ok ? reload() : alert(r.data.error || 'Ошибка'); });
    }
  });
})();
