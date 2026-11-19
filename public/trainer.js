'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let token, current = null, state = null, timer, saving = Promise.resolve(), locked = false;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Career-Token':token}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить действие');
  return result;
}
function error(message) {
  const el = $('#trainer-error');
  el.textContent = message;
  el.classList.toggle('hidden', !message);
}
function busy(value) {
  locked = value;
  $('#category').disabled = value;
  $('#mode').disabled = value;
  $('#question-card').setAttribute('aria-busy', String(value));
  document.querySelectorAll('#question-card button').forEach(button => {button.disabled = value;});
  if ($('#answer')) $('#answer').readOnly = value;
}
function saveDraft() {
  clearTimeout(timer);
  const field = $('#answer');
  if (!current || !field) return saving;
  const payload = {id:current.id, revision:current.revision, answer:field.value};
  $('#save-status').textContent = 'Сохраняем ответ…';
  saving = saving.catch(() => {}).then(() => api('/api/trainer/answer', payload)).then(() => {
    $('#save-status').textContent = 'Ответ сохранён';
  });
  return saving;
}
function render(result) {
  state = result;
  current = result.question;
  $('#reviewed').textContent = `${result.stats.reviewed} / ${result.stats.total}`;
  $('#confident').textContent = result.stats.confident;
  $('#due').textContent = result.stats.due;
  $('#category').innerHTML = '<option value="">Все темы</option>' + result.categories.map(category => `<option value="${escapeHTML(category)}">${escapeHTML(category)}</option>`).join('');
  $('#category').value = result.category;
  $('#mode').value = result.mode;
  $('#save-status').textContent = '';
  const card = $('#question-card');
  if (!current) {
    const next = result.nextReview ? new Date(result.nextReview).toLocaleString('ru-RU', {day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}) : null;
    card.innerHTML = `<div class="trainer-empty"><h2 tabindex="-1">${result.mode === 'review' ? 'Сейчас повторять нечего.' : 'Все вопросы этого блока разобраны.'}</h2><p>${next ? 'Следующее повторение: ' + escapeHTML(next) + '.' : 'Выбери другую тему или начни изучать новые вопросы.'}</p><button class="button" id="continue-learning">${result.mode === 'review' ? 'Перейти к изучению' : 'Проверить очередь'}</button></div>`;
    $('#continue-learning').addEventListener('click', () => { $('#mode').value = 'learn'; loadState().catch(showFailure); });
    card.querySelector('h2').focus();
    return;
  }
  card.innerHTML = `<div class="question-meta"><span>${escapeHTML(current.category)}</span><span>${escapeHTML(current.id)}</span><span>${current.priority === 'P0' ? 'Главное' : 'Следующий уровень'}</span>${current.isReview ? '<span class="repeat-label">Повторение</span>' : ''}</div>
    <h2 id="question-title" tabindex="-1">${escapeHTML(current.question)}</h2>
    <label for="answer" class="answer-label">Твой ответ</label>
    <textarea id="answer" maxlength="15000" placeholder="Объясни своими словами. Добавь пример из своего кода и ограничения решения.">${escapeHTML(current.answer)}</textarea>
    <div class="question-actions"><button class="button" id="reveal">Показать разбор</button><span class="question-hint">Можно сначала ответить вслух.<br>На один ответ — примерно 1–2 минуты.</span></div>
    <section id="answer-review" class="answer-review hidden" aria-label="Разбор ответа">
      <h3>Что должно прозвучать</h3><p>${escapeHTML(current.answer_outline)}</p>
      <h3>Что могут спросить дальше</h3><p>${escapeHTML(current.follow_up)}</p>
      <div class="source-links">${current.sources.map(source => {
        const url = new URL(source.url);
        return ['https:','http:'].includes(url.protocol) ? `<a href="${escapeHTML(url.href)}" target="_blank" rel="noreferrer">${escapeHTML(source.title)}</a>` : '';
      }).join('')}</div>
      <h3 id="grade-title" tabindex="-1">Насколько уверенно ответил?</h3><p class="grade-help">Оценка сохранится, затем появится следующий вопрос.</p>
      <div class="grade-buttons"><button class="grade-button" data-score="0"><strong>Не смог объяснить</strong><small>Повтор через 10 минут</small></button><button class="grade-button" data-score="1"><strong>С подсказкой</strong><small>Повтор завтра</small></button><button class="grade-button" data-score="2"><strong>Сам объяснил и привёл пример</strong><small>Повтор через 3 дня</small></button><button class="grade-button" data-score="3"><strong>Разобрал и дополнительные вопросы</strong><small>Повтор через неделю</small></button></div>
    </section>`;
  $('#answer').addEventListener('input', () => {
    clearTimeout(timer);
    $('#save-status').textContent = 'Есть несохранённые изменения';
    timer = setTimeout(() => { saveDraft().catch(showFailure); }, 600);
  });
  $('#answer').addEventListener('blur', () => {if (!locked) saveDraft().catch(showFailure);});
  $('#reveal').addEventListener('click', async () => {
    if (locked) return;
    busy(true); error('');
    try {
      await saveDraft();
      $('#answer-review').classList.remove('hidden');
      $('#reveal').classList.add('hidden');
      $('#grade-title').focus();
    } catch (exc) { showFailure(exc); }
    finally { busy(false); }
  });
  document.querySelectorAll('[data-score]').forEach(button => button.addEventListener('click', () => grade(Number(button.dataset.score))));
  $('#question-title').focus();
}
function showFailure(exc) {
  error(exc.message || 'Не удалось связаться с локальным сервером. Повтори действие.');
  $('#save-status').textContent = 'Не сохранено — проверь сообщение об ошибке';
}
async function loadState() {
  busy(true);
  error('');
  try {
    const params = new URLSearchParams({category:$('#category').value,mode:$('#mode').value});
    render(await api('/api/trainer/state?' + params));
  } finally { busy(false); }
}
async function grade(score) {
  if (locked || !current) return;
  busy(true); error('');
  try {
    await saveDraft();
    await api('/api/trainer/grade', {id:current.id, revision:current.revision, answer:$('#answer').value,score});
    current = null;
    $('#question-card').innerHTML = '<p>Ответ сохранён. Загружаем следующий вопрос…</p><button class="button" id="retry-load">Продолжить тренировку</button>';
    $('#retry-load').addEventListener('click', () => loadState().catch(showFailure));
    await loadState();
  } catch (exc) { showFailure(exc); }
  finally { busy(false); }
}
async function changeFilter() {
  busy(true);error('');
  try { await saveDraft(); await loadState(); }
  catch (exc) {
    if (state) {$('#category').value=state.category;$('#mode').value=state.mode;}
    showFailure(exc);
  } finally {busy(false);}
}
$('#category').addEventListener('change', changeFilter);
$('#mode').addEventListener('change', changeFilter);
(async () => {
  try {token=(await api('/api/bootstrap')).token;await loadState();}
  catch (exc) {
    showFailure(exc);
    $('#question-card').innerHTML='<p>Тренажёр пока не загрузился.</p><button class="button" id="retry-init">Повторить загрузку</button>';
    $('#retry-init').addEventListener('click', () => location.reload());busy(false);
  }
})();
