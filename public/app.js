'use strict';
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const state = { token: '', stages: {}, platforms: {}, view: 'applications', platform: '', stage: '', q: '', sort: 'recent', date: '', page: 1, pages: 1, detailKey: null, refreshing: false, request: 0, dirty: false, stats: null };
const labels = { inbox: 'Входящие', applications: 'Все отклики', replies: 'Ответы компаний', tasks: 'Следующие шаги', queue: 'Очередь вакансий', documents: 'Мои резюме' };
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function safeURL(value) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? escapeHTML(url.href) : '#'; } catch { return '#'; } }
function dateLabel(value, withTime = false) { if (!value) return 'Неизвестно'; const date = new Date(value); return Number.isNaN(+date) ? 'Неизвестно' : new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}) }).format(date); }
function dayLabel(value) { return dateLabel(value + 'T12:00:00+03:00'); }
function toast(message) { const el = $('#toast'); el.textContent = message; el.classList.remove('hidden'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.add('hidden'), 4000); }
function showError(message) { $('#error-banner').textContent = message; $('#error-banner').classList.remove('hidden'); }
async function api(path, data) { const response = await fetch(path, data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Career-Token': state.token }, body: JSON.stringify(data) }); const value = await response.json(); if (!response.ok) throw new Error(value?.error || 'Не удалось выполнить запрос'); return value; }
function params() { return new URLSearchParams({ view: state.view, platform: state.platform, stage: state.stage, q: state.q, sort: state.sort, date: state.date, page: String(state.page) }); }
function badge(stage, label) { return `<span class="badge ${escapeHTML(stage)}">${escapeHTML(label || state.stages[stage] || stage)}</span>`; }
function initial(company) { return (company || '?').replace(/^(ООО|АО|ИП)\s+/i, '').split(/[\s/]+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase(); }
function color(str) { return [...str].reduce((v, c) => v + c.charCodeAt(0), 0) % 5; }
function renderStats(stats) {
  state.stats = stats;
  $('#stat-total').textContent = stats.confirmed;
  $('#stat-total-foot').textContent = `${stats.new} новых · ${stats.previous} ранее отправленных`;
  $('#stat-today').textContent = stats.today;
  $('#stat-today-foot').textContent = stats.dailyTarget ? `Цель ${stats.dailyTarget} · осталось ${stats.remainingToday}` : 'С полуночи по Москве';
  $('#stat-invited').textContent = stats.invitations;
  $('#stat-interviews').textContent = `${stats.interviews} назначенных встреч · ${stats.offers} офферов`;
  $('#stat-tasks').textContent = stats.needsAction;
  $('#stat-new').textContent = stats.new;
  $('#nav-total').textContent = stats.confirmed;
  $('#nav-replies').textContent = ['reply', 'invited', 'interview', 'offer', 'rejected'].reduce((v, k) => v + stats.counts[k], 0);
  $('#nav-tasks').textContent = stats.needsAction;
  $('#today-label').textContent = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: 'numeric', month: 'long' }).format(new Date());
  $('#activity-note').textContent = `${stats.rejected} отказов отмечено. Приглашение — ещё не назначенная встреча.`;
  const max = Math.max(1, ...stats.daily.map(d => d.count));
  $('#activity-chart').innerHTML = stats.daily.length ? stats.daily.map(d => `<div class="bar-group" title="${escapeHTML(d.date)}: ${d.count}"><span class="bar-value">${d.count}</span><div class="bar" data-height="${Math.max(3, d.count / max * 103)}"></div><span class="bar-label">${escapeHTML(dayLabel(d.date))}</span></div>`).join('') : '<p class="muted">Датированных отправок пока нет</p>';
  $$('.bar').forEach(el => el.style.height = el.dataset.height + 'px');
  const hh = stats.hhSync;
  const hhText = hh.state === 'running' ? 'Читаем статусы и чаты…' : hh.state === 'success' ? `${hh.count} откликов · ${hh.chats} чатов${hh.warnings?.length ? ` · пропущено ${hh.warnings.length}` : ''} · ${dateLabel(hh.at, true)}` : hh.state === 'error' ? hh.error : 'Нажми ↻ для первого обновления';
  $('#hh-sync-state').textContent = hhText;
  $('#hh-sync-state').classList.toggle('error-text', hh.state === 'error');
  $('#hh-sync-button').disabled = hh.state === 'running';
  $('#hh-sync-button').classList.toggle('spinning', hh.state === 'running');
  $('#auto-hh').checked = stats.autoHH;
  $('#last-import').textContent = stats.lastImport?.at ? `Обновлено ${dateLabel(stats.lastImport.at, true)}` : 'Ожидаем импорт';
  const warnings = stats.lastImport?.warnings || [];
  $('#data-quality').textContent = warnings.length ? `Проверь импорт: ${warnings.length} предупреждений` : 'Только подтверждённые отправки · без повторов';
  if (warnings.length) { $('#data-quality').title = warnings.join('\n'); }
  const active = document.activeElement;
  if (!active?.classList.contains('platform-filter')) {
    $('#platform-filters').innerHTML = `<button class="platform-filter ${!state.platform ? 'active' : ''}" data-platform="">Все площадки</button>` + stats.platforms.filter(p => p.confirmed || p.queued).map(p => `<button class="platform-filter ${state.platform === p.id ? 'active' : ''}" data-platform="${p.id}">${escapeHTML(p.id === 'company' ? 'Компании' : p.name)} <span>${p.confirmed}</span></button>`).join('');
  }
}
async function loadJobs() {
  if (state.view === 'documents') return loadDocuments();
  if (state.view === 'inbox') return loadInbox();
  const request = ++state.request;
  const result = await api('/api/jobs?' + params());
  if (request !== state.request) return;
  state.pages = result.pages;
  if (state.page > result.pages) { state.page = result.pages; return loadJobs(); }
  $('#result-count').textContent = result.total;
  $('#jobs-body').innerHTML = result.items.map(row => `<tr tabindex="0" data-key="${escapeHTML(row.key)}" aria-label="${escapeHTML(row.company + ', ' + row.title + ', ' + row.stage_label)}"><td><div class="job-main"><span class="company-avatar color-${color(row.company)}">${escapeHTML(initial(row.company))}</span><div class="job-meta"><span class="job-company">${escapeHTML(row.company || 'Компания не указана')}</span><span class="job-title" title="${escapeHTML(row.title)}">${escapeHTML(row.title || 'Название не указано')}</span></div></div></td><td><span class="platform-name ${escapeHTML(row.platform)}">${escapeHTML(row.platform === 'company' ? 'Компания' : row.platform_label)}</span></td><td>${badge(row.effective_stage, row.stage_label)}</td><td>${row.sent_at ? escapeHTML(dateLabel(row.sent_at)) : '<span class="muted">—</span>'}</td><td><span class="next-action">${escapeHTML(row.next_action || (row.effective_stage === 'blocked' ? 'Уточнить данные' : row.effective_stage === 'invited' ? 'Проверить приглашение' : '—'))}</span>${row.follow_up ? `<span class="next-date">${escapeHTML(dayLabel(row.follow_up))}</span>` : ''}</td><td><span class="row-arrow">↗</span></td></tr>`).join('');
  $('#empty-state').classList.toggle('hidden', !!result.items.length);
  $('#pagination-label').textContent = result.total ? `${(state.page - 1) * result.limit + 1}–${Math.min(state.page * result.limit, result.total)} из ${result.total} записей` : '0 записей';
  $('#page-label').textContent = `${state.page} / ${result.pages}`;
  $('#prev-page').disabled = state.page <= 1;
  $('#next-page').disabled = state.page >= result.pages;
  $('#clear-filters').classList.toggle('hidden', !state.platform && !state.stage && !state.q && !state.date);
}
async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try { const stats = await api('/api/stats'); renderStats(stats); await loadJobs(); $('#error-banner').classList.add('hidden'); }
  catch (error) { showError(error.message); }
  finally { state.refreshing = false; }
}
async function setView(view) {
  state.view = view;
  state.page = 1;
  $$('.nav-item[data-view]').forEach(el => { const active = el.dataset.view === view; el.classList.toggle('active', active); el.setAttribute('aria-current', active ? 'page' : 'false'); });
  $('#breadcrumb-page').textContent = labels[view];
  $('#page-title').innerHTML = ({ inbox: 'Входящие', applications: 'Каждый отклик — на виду<span>.</span>', replies: 'Разговор начинается здесь<span>.</span>', tasks: 'Следующий шаг — твой<span>.</span>', queue: 'Следующие возможности<span>.</span>', documents: 'Твой опыт, в нужной версии<span>.</span>' })[view];
  $('#table-heading').firstChild.textContent = labels[view] + ' ';
  $('#table-description').textContent = ({ applications: 'От первой отправки до следующего разговора.', replies: 'Приглашения, ответы и отказы. Встречи отмечаются отдельно.', tasks: 'Данные, которые нужно уточнить, и запланированные действия.', queue: 'Найденные вакансии, включая отложенные и неподходящие.' })[view] || '';
  const hasJobs = view !== 'documents' && view !== 'inbox';
  $('#jobs-panel').classList.toggle('hidden', !hasJobs);
  $('#documents-panel').classList.toggle('hidden', view !== 'documents');
  $('#inbox-panel').classList.toggle('hidden', view !== 'inbox');
  $('#overview-grid').classList.toggle('hidden', !hasJobs);
  $('#export-button').disabled = !hasJobs;
  await loadJobs();
}
async function loadInbox() {
  const request = ++state.request;
  const pending = $('#inbox-pending').checked ? 1 : 0;
  const result = await api(`/api/inbox?page=${state.page}&pending=${pending}`);
  if (request !== state.request || state.view !== 'inbox') return;
  if (state.page > result.pages) { state.page = result.pages; return loadInbox(); }
  const gmail = result.gmail;
  $('#gmail-state').textContent = gmail.state === 'running' ? 'Проверяем Gmail…' :
    gmail.state === 'success' ? `Gmail · ${dateLabel(gmail.at, true)} · новых: ${gmail.count}` :
    gmail.error || 'Gmail ещё не проверен';
  $('#gmail-sync-button').disabled = gmail.state === 'running';
  $('#telegram-state').textContent = result.telegram.error || result.telegramChats.error || '';
  $('#inbox-page').textContent = `${state.page} / ${result.pages}`;
  $('#inbox-prev').disabled = state.page === 1;
  $('#inbox-next').disabled = state.page >= result.pages;
  const signature = JSON.stringify(result.items);
  if ($('#inbox-list').dataset.signature === signature) return;
  $('#inbox-list').dataset.signature = signature;
  $('#inbox-list').innerHTML = result.items.map(row => `<article class="inbox-row">
    <div><strong>${escapeHTML(row.subject || row.sender || 'Сообщение без темы')}</strong>
      <small>${({ hh: 'HH', gmail: 'Gmail', telegram: 'Telegram' })[row.source]} · ${escapeHTML(dateLabel(row.at, true))} · ${escapeHTML(state.inboxKinds[row.kind])}${row.urgent ? ' · Срочно' : ''}${row.done ? ' · Закрыто' : ''}${row.draft ? ' · Есть черновик' : ''}</small>
      ${row.next_action || row.due ? `<small>${escapeHTML(row.next_action)} ${row.due ? ' · ' + escapeHTML(row.due.replace('T', ' ')) + ' МСК' : ''}</small>` : ''}
    </div><button class="button secondary" data-inbox-id="${escapeHTML(row.id)}">Открыть</button>
  </article>`).join('') || '<p class="empty-state">Нет сообщений по выбранному фильтру.</p>';
}

async function openInbox(id) {
  const path = '/api/inbox/' + encodeURIComponent(id);
  const row = await api(path);
  const dialog = $('#inbox-dialog');
  $('#inbox-detail').innerHTML = `<div class="detail-top"><span>${({ hh: 'HH', gmail: 'Gmail', telegram: 'Telegram' })[row.source]}</span><button class="close-button" id="close-inbox" aria-label="Закрыть карточку">×</button></div>
    <h2 id="inbox-title">${escapeHTML(row.subject || 'Сообщение')}</h2>
    <p>${escapeHTML(row.sender)} ${escapeHTML(dateLabel(row.at, true))}</p>
    <div class="detail-actions">${row.url ? `<a class="button primary" href="${safeURL(row.url)}" target="_blank" rel="noopener noreferrer">Оригинальный диалог ↗</a>` : ''}
    ${row.job_key ? '<button class="button secondary" id="inbox-job">Отклик</button>' : ''}</div>
    <section class="detail-section"><div class="long-text">${escapeHTML(row.text)}</div></section>
    <form id="inbox-form"><div class="form-grid">
      <label class="form-field">Тип<select name="kind">${Object.entries(state.inboxKinds).map(([value, label]) => `<option value="${value}" ${row.kind === value ? 'selected' : ''}>${escapeHTML(label)}</option>`).join('')}</select></label>
      <label class="form-field">Дедлайн или интервью (МСК)<input type="datetime-local" name="due" value="${escapeHTML(row.due)}"></label>
      <label class="form-field wide">Следующее действие<input name="next_action" value="${escapeHTML(row.next_action)}"></label>
      <label class="form-field wide">Черновик${row.recipient ? ' для ' + escapeHTML(row.recipient) : ''}<textarea name="draft" rows="5">${escapeHTML(row.draft)}</textarea></label>
      <label><input type="checkbox" name="urgent" ${row.urgent ? 'checked' : ''}> Срочно</label>
      <label><input type="checkbox" name="done" ${row.done ? 'checked' : ''}> Обработано</label>
    </div><div class="save-bar"><span>Сохранение черновика локально</span><button type="submit" class="button primary">Сохранить</button></div></form>`;
  const form = $('#inbox-form');
  form.addEventListener('input', () => { state.dirty = true; });
  $('#close-inbox').addEventListener('click', closeInbox);
  $('#inbox-job')?.addEventListener('click', action(async () => { if (closeInbox()) await openDetail(row.job_key); }));
  form.addEventListener('submit', action(async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    values.urgent = form.elements.urgent.checked;
    values.done = form.elements.done.checked;
    await api(path, values);
    state.dirty = false;
    toast('Сохранено');
    await loadInbox();
  }));
  dialog.showModal();
}

function closeInbox() {
  if (state.dirty && !window.confirm('Закрыть без сохранения изменений?')) return false;
  $('#inbox-dialog').close();
  state.dirty = false;
  return true;
}

async function loadDocuments() {
  const result = await api('/api/documents');
  $('#documents-list').innerHTML = result.map(doc => `<div class="document-item"><span class="file-icon">PDF</span><div><strong>${escapeHTML(doc.name)}</strong><small>${Math.round(doc.size / 1024)} КБ · локальный файл</small></div><a class="button secondary" target="_blank" rel="noopener noreferrer" href="/api/documents/${encodeURIComponent(doc.id)}">Открыть ↗</a></div>`).join('') || '<p class="empty-state">PDF-резюме пока не найдены.</p>';
}
function eventName(event) { return ({ application: 'Действие по отклику', delivery_failure: 'Письмо не доставлено', chat_archive: 'Сообщение из архива чата', platform_status: 'Статус на площадке', manual_response: 'Добавлен ответ' })[event.kind] || 'Событие'; }
async function openDetail(key) {
  if (state.dirty && !window.confirm('В карточке есть несохранённые изменения. Открыть другой отклик?')) return;
  const row = await api('/api/jobs/' + encodeURIComponent(key));
  state.detailKey = key;
  state.dirty = false;
  const payload = row.payload;
  const cover = payload.coverLetter || payload.letter;
  $('#detail-content').innerHTML = `<div class="detail-top"><span class="eyebrow">${escapeHTML(row.platform_label)} / ОТКЛИК</span><button class="close-button" id="close-detail" aria-label="Закрыть карточку">×</button></div><div class="detail-company">${escapeHTML(row.company || 'Компания не указана')}</div><h2 id="detail-title">${escapeHTML(row.title || 'Вакансия')}</h2><div class="detail-badges">${badge(row.effective_stage)}${row.platform_status ? `<span class="subtle-pill">HH: ${escapeHTML(row.platform_status)}</span>` : ''}${row.confirmed ? '<span class="subtle-pill">ОТКЛИК ПОДТВЕРЖДЁН</span>' : ''}</div><div class="detail-actions">${row.url ? `<a class="button primary" target="_blank" rel="noopener noreferrer" href="${safeURL(row.url)}">Вакансия ↗</a>` : ''}${row.chat_url ? `<a class="button secondary" target="_blank" rel="noopener noreferrer" href="${safeURL(row.chat_url)}">Открыть чат ↗</a>` : ''}</div><dl class="detail-meta"><div><dt>Отправлен</dt><dd>${row.sent_at ? escapeHTML(dateLabel(row.sent_at, true)) : row.confirmed ? 'Ранее; точная дата неизвестна' : 'Отправка не подтверждена'}</dd></div><div><dt>Резюме при отправке</dt><dd>${escapeHTML(payload.resume || payload.resumeVerification && 'Версия требует проверки' || 'Версия в журнале не указана')}</dd></div></dl><section class="detail-section"><h3>Мои действия</h3><form id="detail-form"><div class="form-grid"><label class="form-field">Статус<select name="manual_stage"><option value="">Автоматически: ${escapeHTML(state.stages[row.response_stage || row.stage])}</option>${Object.entries(state.stages).map(([id, name]) => `<option value="${id}" ${row.manual_stage === id ? 'selected' : ''}>${escapeHTML(name)}</option>`).join('')}</select></label><label class="form-field">Напомнить<input type="date" name="follow_up" value="${escapeHTML(row.follow_up)}"></label><label class="form-field wide">Следующий шаг<input name="next_action" value="${escapeHTML(row.next_action)}" placeholder="Например, ответить рекрутеру или уточнить формат"></label><label class="form-field wide">Заметка<textarea name="note" rows="3" placeholder="Что важно помнить о компании и общении">${escapeHTML(row.note)}</textarea></label><label class="form-field wide">Последние сообщения / ответ<textarea name="response" rows="4" placeholder="Добавить ответ из почты, Telegram или другой площадки">${escapeHTML(row.response)}</textarea></label></div><div class="save-bar"><span id="save-hint">Заметки сохраняются на этом компьютере</span><button class="button primary" type="submit">Сохранить</button></div></form></section>${row.response ? `<section class="detail-section"><h3>Последние сообщения</h3><div class="long-text">${escapeHTML(row.response)}</div></section>` : ''}${cover ? `<section class="detail-section"><h3>Сопроводительное письмо</h3><div class="long-text">${escapeHTML(cover)}</div></section>` : ''}${!cover && payload.coverLetterSent ? '<p class="sync-hint">Отправка сопроводительного отмечена в журнале; его текст не сохранён.</p>' : ''}<section class="detail-section"><h3>История · ${row.events.length}</h3>${row.events.length ? row.events.map(event => `<article class="event"><time>${escapeHTML(dateLabel(event.at, true))}${event.kind === 'chat_archive' && event.payload.timeIsImport ? ' · время импорта' : ''}</time><strong>${escapeHTML(eventName(event))}</strong>${event.text.length > 350 ? `<details><summary>Показать сообщение</summary><p>${escapeHTML(event.text)}</p></details>` : `<p>${escapeHTML(event.text)}</p>`}</article>`).join('') : '<p class="sync-hint">Вакансия найдена, действий пока нет.</p>'}</section>`;
  const dialog = $('#job-dialog');
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  $('#close-detail').addEventListener('click', closeDetail);
  $('#detail-form').addEventListener('input', () => { state.dirty = true; $('#save-hint').textContent = 'Есть несохранённые изменения'; });
  $('#detail-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('button[type=submit]', event.currentTarget);
    button.disabled = true;
    try {
      const values = Object.fromEntries(new FormData(event.currentTarget));
      values.manual_stage = values.manual_stage || null;
      await api('/api/jobs/' + encodeURIComponent(key), values);
      state.dirty = false;
      $('#save-hint').textContent = 'Сохранено';
      toast('Изменения сохранены');
      await refresh();
    } catch (error) { $('#save-hint').textContent = error.message; }
    finally { button.disabled = false; }
  });
}
function closeDetail() { if (state.dirty && !window.confirm('Закрыть карточку без сохранения изменений?')) return; $('#job-dialog').close(); state.detailKey = null; state.dirty = false; }
function action(fn) { return async (...args) => { try { await fn(...args); } catch (error) { showError(error.message); } }; }
async function init() {
  const bootstrap = await api('/api/bootstrap');
  Object.assign(state, bootstrap);
  $('#stage-filter').innerHTML += Object.entries(state.stages).map(([id, name]) => `<option value="${id}">${escapeHTML(name)}</option>`).join('');
  $$('.nav-item[data-view]').forEach(el => el.addEventListener('click', action(() => setView(el.dataset.view))));
  $('#jobs-body').addEventListener('click', action(event => { const row = event.target.closest('tr[data-key]'); if (row) return openDetail(row.dataset.key); }));
  $('#jobs-body').addEventListener('keydown', action(event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); const row = event.target.closest('tr[data-key]'); if (row) return openDetail(row.dataset.key); } }));
  $('#platform-filters').addEventListener('click', action(async event => { const button = event.target.closest('[data-platform]'); if (!button) return; state.platform = button.dataset.platform; state.page = 1; $$('.platform-filter').forEach(el => el.classList.toggle('active', el === button)); await loadJobs(); }));
  let debounce;
  $('#search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(action(async () => { state.q = $('#search').value; state.page = 1; await loadJobs(); }), 200); });
  for (const [id, key] of [['stage-filter', 'stage'], ['sort-filter', 'sort'], ['date-filter', 'date']]) $( '#' + id).addEventListener('change', action(async event => { state[key] = event.target.value; state.page = 1; await loadJobs(); }));
  $('#clear-filters').addEventListener('click', action(async () => { state.platform = state.stage = state.q = state.date = ''; state.page = 1; $('#search').value = $('#stage-filter').value = $('#date-filter').value = ''; renderStats(state.stats); await loadJobs(); }));
  $('#prev-page').addEventListener('click', action(async () => { state.page = Math.max(1, state.page - 1); await loadJobs(); }));
  $('#next-page').addEventListener('click', action(async () => { state.page = Math.min(state.pages, state.page + 1); await loadJobs(); }));
  $('#sync-button').addEventListener('click', action(async () => { const button = $('#sync-button'); button.disabled = true; try { const result = await api('/api/sync', {}); await refresh(); toast(result.warnings.length ? 'Данные обновлены с предупреждениями' : 'Данные обновлены'); } finally { button.disabled = false; } }));
  $('#hh-sync-button').addEventListener('click', action(async () => { await api('/api/sync/hh', {}); await refresh(); toast('Синхронизация HH запущена'); }));
  $('#auto-hh').addEventListener('change', action(async event => { try { await api('/api/settings', { autoHH: event.target.checked }); toast(event.target.checked ? 'HH будет проверяться каждые 2 часа' : 'Проверка HH по кнопке'); } catch (error) { event.target.checked = !event.target.checked; throw error; } }));
  $('#gmail-sync-button').addEventListener('click', action(async () => { await api('/api/sync/gmail', {}); await loadInbox(); }));
  $('#inbox-pending').addEventListener('change', action(async () => { state.page = 1; await loadInbox(); }));
  $('#inbox-prev').addEventListener('click', action(async () => { state.page--; await loadInbox(); }));
  $('#inbox-next').addEventListener('click', action(async () => { state.page++; await loadInbox(); }));
  $('#inbox-list').addEventListener('click', action(event => {
    const button = event.target.closest('[data-inbox-id]');
    if (button) return openInbox(button.dataset.inboxId);
  }));
  $('#inbox-dialog').addEventListener('cancel', event => { event.preventDefault(); closeInbox(); });
  $('#export-button').addEventListener('click', () => { window.location.href = '/api/export.csv?' + params(); });
  $('#job-dialog').addEventListener('cancel', event => { event.preventDefault(); closeDetail(); });
  $('#job-dialog').addEventListener('click', event => { if (event.target === $('#job-dialog')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) closeDetail(); } });
  window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
  await refresh();
  setInterval(refresh, 5000);
}
init().catch(error => showError(error.message));
