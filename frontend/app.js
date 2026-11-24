const state = { ticketId: null, sessionId: null, token: null, user: null };
const $ = selector => document.querySelector(selector);
const safe = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {new:'Новое',in_progress:'В работе',resolved:'Решено',escalated:'Передано'};
const date = value => value ? new Date(value).toLocaleString('ru-RU') : '—';
const content = html => { $('#content').innerHTML = html; };
function flash(message) { const el = $('#flash'); el.textContent = message; el.hidden = !message; }
async function api(path, options = {}) {
  const headers = {'Content-Type': options.body instanceof URLSearchParams ? 'application/x-www-form-urlencoded' : 'application/json'};
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  const response = await fetch(path, {...options, headers});
  const result = await response.json();
  if (response.status === 401 && state.token) { logout(); throw new Error('Сеанс истёк. Войдите снова.'); }
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось выполнить запрос');
  return result;
}
async function run(task) { try { flash(''); await task(); } catch (error) { flash(error.message || 'Ошибка соединения'); } }
async function loadTickets() {
  const query = $('#search').value.trim();
  const tickets = await api('/api/tickets?search=' + encodeURIComponent(query));
  $('#ticket-list').innerHTML = tickets.length ? tickets.map(t =>
    `<button class="ticket-item ${state.ticketId === t.id ? 'active' : ''}" data-ticket="${t.id}">
      <span class="row"><strong>№ ${t.id} · ${safe(t.subscriber_id)}</strong><span class="badge ${t.status}">${labels[t.status]}</span></span>
      <p>${safe(t.problem_description)}</p></button>`).join('') : '<p class="empty">Обращений пока нет</p>';
  document.querySelectorAll('[data-ticket]').forEach(button => button.addEventListener('click',
    () => run(() => openTicket(Number(button.dataset.ticket)))));
}
function welcome() {
  state.ticketId = null; state.sessionId = null;
  content(`<div class="welcome"><div class="icon-circle">✳</div><span class="kicker">NETEXPERT · ПЕРВАЯ ВЕРСИЯ</span>
    <h1>Диагностика, у которой есть история</h1>
    <p class="muted">Создайте обращение, проведите короткий опрос и получите рекомендацию по правилам базы знаний. Ответы и решения сохраняются в PostgreSQL.</p>
    <button id="start-new" class="btn primary">Создать обращение</button></div>`);
  $('#start-new').onclick = newTicket;
}
function logout() {
  state.token = null; state.user = null; state.ticketId = null; state.sessionId = null;
  $('#current-user').textContent = ''; $('#logout').hidden = true;
  $('#ticket-list').innerHTML = '';
  loginScreen();
}
function loginScreen() {
  content(`<div class="panel welcome"><span class="kicker">ДОСТУП ДЛЯ СОТРУДНИКОВ</span><h1>Вход в NetExpert</h1>
    <p class="muted">Используйте учётную запись, созданную администратором.</p>
    <form id="login-form"><div class="fields">
      <label class="field">Логин<input name="username" required autocomplete="username"></label>
      <label class="field">Пароль<input name="password" type="password" required autocomplete="current-password"></label>
    </div><button type="submit" class="btn primary">Войти</button></form></div>`);
  $('#login-form').onsubmit = event => { event.preventDefault(); run(async () => {
    const form = new FormData(event.target);
    const result = await api('/api/auth/token', {method:'POST', body:new URLSearchParams({
      username:String(form.get('username')), password:String(form.get('password'))})});
    state.token = result.access_token;
    state.user = await api('/api/auth/me');
    if (!['operator','admin'].includes(state.user.role)) { logout(); throw new Error('Рабочее место доступно операторам'); }
    $('#current-user').textContent = state.user.username;
    $('#logout').hidden = false;
    welcome(); await loadTickets();
  }); };
}
function newTicket() {
  state.ticketId = null; state.sessionId = null;
  content(`<div class="panel"><span class="kicker">НОВОЕ ОБРАЩЕНИЕ</span><h1>Регистрация проблемы</h1>
    <p class="muted">Идентификатор абонента может быть номером договора или лицевого счёта.</p>
    <form id="ticket-form"><div class="fields">
      <label class="field">Идентификатор абонента<input name="subscriber" required maxlength="100" placeholder="Например, 100254"></label>
      <label class="field">Описание проблемы<textarea name="description" required minlength="5" maxlength="2000" placeholder="Что именно не работает и когда началась проблема?"></textarea></label>
    </div><div class="actions"><button class="btn primary" type="submit">Сохранить обращение</button><button type="button" id="cancel" class="btn">Отмена</button></div></form></div>`);
  $('#cancel').onclick = welcome;
  $('#ticket-form').onsubmit = event => { event.preventDefault(); run(async () => {
    const form = new FormData(event.target);
    const ticket = await api('/api/tickets', {method:'POST',body:JSON.stringify({
      subscriber_id:String(form.get('subscriber')).trim(),
      problem_description:String(form.get('description')).trim()})});
    await loadTickets(); await openTicket(ticket.id);
  }); };
}
async function openTicket(id) {
  state.ticketId = id; state.sessionId = null;
  const t = await api('/api/tickets/' + id);
  content(`<div class="panel"><div class="heading"><div><span class="kicker">КАРТОЧКА ОБРАЩЕНИЯ</span><h1>Обращение № ${t.id}</h1></div>
    <span class="badge ${t.status}">${labels[t.status]}</span></div>
    <p>${safe(t.problem_description)}</p>
    <div class="meta"><div><small>Абонент</small><b>${safe(t.subscriber_id)}</b></div>
      <div><small>Оператор</small><b>${safe(t.created_by)}</b></div>
      <div><small>Создано</small><b>${date(t.created_at)}</b></div></div>
    ${['new','in_progress'].includes(t.status) && !t.sessions.some(s => s.status === 'active') ? '<div class="divider"></div><button id="start-session" class="btn primary">Начать диагностику</button>' : ''}</div>
    <div class="panel"><h2>Диагностические сессии</h2>${t.sessions.length ? t.sessions.map(s =>
      `<button class="btn small session-link" data-session="${s.id}">${date(s.started_at)} · ${s.status === 'active' ? 'В процессе' : safe(s.suspected_cause)}</button>`).join(' ') : '<p class="muted smalltext">Диагностика ещё не проводилась.</p>'}</div>
    <div class="panel"><h2>История статусов</h2><ul class="history">${t.history.map(h =>
      `<li><b>${labels[h.new_status] || safe(h.new_status)}</b> · ${safe(h.changed_by)} · ${date(h.changed_at)}${h.comment ? '<br>'+safe(h.comment) : ''}</li>`).join('')}</ul></div>`);
  $('#start-session')?.addEventListener('click', () => run(async () => {
    const result = await api(`/api/tickets/${id}/sessions`, {method:'POST'});
    await loadTickets(); showSession(result);
  }));
  document.querySelectorAll('.session-link').forEach(button => button.onclick = () => run(async () => showSession(await api('/api/sessions/'+button.dataset.session))));
  await loadTickets();
}
function showSession(data) {
  state.sessionId = data.session.id;
  const s = data.session, q = data.question;
  let html = `<div class="panel"><span class="kicker">ДИАГНОСТИКА · ОБРАЩЕНИЕ № ${s.ticket_id}</span>
    <h1>${s.status === 'active' ? 'Пошаговая диагностика' : 'Результат диагностики'}</h1>
    <p class="muted">Ответы записываются в историю сессии. База знаний: версия № ${s.knowledge_version_id}.</p>`;
  if (q) {
    html += `<div class="progress"><span style="width:${Math.min(100,data.answers.length*20)}%"></span></div>
      <span class="kicker">ВОПРОС ${data.answers.length+1}</span><h2>${safe(q.prompt)}</h2>
      <div class="option-list">${q.options.map(o => `<button class="option" data-answer="${safe(o.code)}"><span class="option-dot"></span>${safe(o.label)}</button>`).join('')}</div>`;
  }
  if (s.status === 'completed') {
    html += `<div class="panel result ${s.escalation_required ? 'escalation' : ''}">
      <span class="kicker">${s.escalation_required ? 'ТРЕБУЕТСЯ ЭСКАЛАЦИЯ' : 'НАЙДЕНО РЕШЕНИЕ'}</span>
      <h2>${safe(s.suspected_cause)}</h2><p><strong>Рекомендация:</strong> ${safe(s.recommendation)}</p>
      <p><strong>Основание:</strong> ${safe(s.explanation)}</p></div>
      ${data.ticket_status === 'in_progress' ? `<div class="actions"><button class="btn primary" data-status="${s.escalation_required ? 'escalated' : 'resolved'}">${s.escalation_required ? 'Передать на вторую линию' : 'Проблема устранена'}</button>
      ${s.escalation_required ? '' : '<button class="btn" data-status="escalated">Не помогло — передать на вторую линию</button>'}</div>` : ''}`;
  }
  html += `<div class="divider"></div><button id="back" class="btn">К обращению</button></div>`;
  if (data.answers.length) html += `<div class="panel"><h2>Записанные ответы</h2><ul class="answer-list">${data.answers.map(a =>
    `<li>${safe(a.prompt)} <span>${({yes:'Да',no:'Нет',unknown:'Неизвестно'})[a.answer_code] || safe(a.answer_code)}</span></li>`).join('')}</ul></div>`;
  content(html);
  $('#back').onclick = () => run(() => openTicket(s.ticket_id));
  document.querySelectorAll('[data-answer]').forEach(button => button.onclick = () => run(async () => {
    document.querySelectorAll('[data-answer]').forEach(b => b.disabled = true);
    const next = await api(`/api/sessions/${s.id}/answers`, {method:'POST',body:JSON.stringify({question_id:q.id,answer_code:button.dataset.answer})});
    showSession(next);
  }));
  document.querySelectorAll('[data-status]').forEach(button => button.onclick = () => run(async () => {
    await api(`/api/tickets/${s.ticket_id}/status`, {method:'PATCH',body:JSON.stringify({status:button.dataset.status})});
    await openTicket(s.ticket_id);
  }));
}
$('#new-ticket').onclick = () => state.user ? newTicket() : loginScreen();
$('#logout').onclick = logout;
let searchTimer;
$('#search').oninput = () => { if (!state.user) return; clearTimeout(searchTimer); searchTimer = setTimeout(() => run(loadTickets), 250); };
loginScreen();
