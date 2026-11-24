// Admin-only forms; every endpoint independently enforces the admin role.
const catalogOptions = (items, label) => `<option value="">Выберите запись</option>` +
  items.map(item => `<option value="${item.id}">${safe(label(item))}</option>`).join('');

const catalogListButton = (key, title, count) =>
  `<button type="button" class="btn small catalog-list-button" data-catalog-list="${key}">
    ${title} <span class="catalog-count">${count}</span>
  </button>`;

function bindCatalogLists(data) {
  const formats = {
    clients: ['Список клиентов', c => `№ ${c.id} · ${safe(c.name)} · ${safe(c.phone || '—')}`],
    tariffs: ['Список тарифов', t => `№ ${t.id} · ${safe(t.name)} · ${t.speed_mbps} Мбит/с`],
    addresses: ['Список адресов', a => `№ ${a.id} · ${safe(a.city)}, ${safe(a.street)}, д. ${safe(a.building)}${a.apartment ? ', кв. ' + safe(a.apartment) : ''}`],
    services: ['Список договоров', s => `Договор ${safe(s.account_number)} · ${safe(s.client_name)}<br>${safe(s.city)}, ${safe(s.street)}, д. ${safe(s.building)} · ${safe(s.tariff_name)}`],
    router_models: ['Список моделей роутеров', m => `№ ${m.id} · ${safe(m.manufacturer)} ${safe(m.model)}`],
    routers: ['Список роутеров', r => `№ ${r.id} · ${safe(r.serial_number)} · ${safe(r.manufacturer)} ${safe(r.model)}`],
    rentals: ['Список передач в аренду', r => `№ ${r.id} · договор ${safe(r.account_number)} · роутер ${safe(r.serial_number)}
      · ${date(r.rented_at)} · ${r.returned_at ? 'Возвращён ' + date(r.returned_at) :
        `<button class="btn small" data-return-rental="${r.id}">Отметить возврат</button>`}`],
  };

  $('#content').insertAdjacentHTML('beforeend', `<dialog id="catalog-dialog" class="catalog-dialog" aria-labelledby="catalog-dialog-title">
    <div class="catalog-dialog-header">
      <h2 id="catalog-dialog-title"></h2>
      <button type="button" class="btn small" id="catalog-close" aria-label="Закрыть список">Закрыть</button>
    </div>
    <input type="search" id="catalog-filter" aria-label="Поиск по списку" placeholder="Поиск по списку">
    <ul id="catalog-dialog-list" class="catalog-dialog-list"></ul>
    <p id="catalog-no-results" class="muted" hidden>Ничего не найдено.</p>
  </dialog>`);

  const dialog = $('#catalog-dialog');
  $('#catalog-close').onclick = () => dialog.close();
  dialog.onclick = event => { if (event.target === dialog) dialog.close(); };
  $('#catalog-filter').oninput = event => {
    const term = event.target.value.trim().toLocaleLowerCase('ru-RU');
    let visible = 0;
    document.querySelectorAll('#catalog-dialog-list .catalog-entry').forEach(row => {
      row.hidden = !row.textContent.toLocaleLowerCase('ru-RU').includes(term);
      if (!row.hidden) visible++;
    });
    $('#catalog-no-results').hidden = visible !== 0;
  };

  document.querySelectorAll('[data-catalog-list]').forEach(button => button.onclick = () => {
    const key = button.dataset.catalogList;
    const [title, format] = formats[key];
    $('#catalog-dialog-title').textContent = title;
    $('#catalog-filter').value = '';
    $('#catalog-dialog-list').innerHTML = data[key].map(item =>
      `<li class="catalog-entry">${format(item)}</li>`).join('');
    $('#catalog-no-results').hidden = data[key].length > 0;
    dialog.showModal();
    $('#catalog-filter').focus();

    document.querySelectorAll('[data-return-rental]').forEach(returnButton => returnButton.onclick = () => run(async () => {
      await api('/api/admin/rentals/' + Number(returnButton.dataset.returnRental) + '/return', {method:'POST'});
      await showAdminRentals();
      document.querySelector('[data-catalog-list="rentals"]').click();
    }));
  });
}

function bindCatalogForms(refresh) {
  document.querySelectorAll('[data-catalog-endpoint]').forEach(form => form.onsubmit = event => {
    event.preventDefault();
    run(async () => {
      const payload = Object.fromEntries(new FormData(form).entries());
      for (const name of ['client_id', 'address_id', 'tariff_id', 'router_model_id', 'service_id', 'router_id', 'speed_mbps']) {
        if (name in payload) payload[name] = Number(payload[name]);
      }
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await api('/api/admin/' + form.dataset.catalogEndpoint, {method:'POST', body:JSON.stringify(payload)});
        await refresh();
      } finally { button.disabled = false; }
    });
  });
}

async function showAdminServices() {
  const data = await api('/api/admin/catalog');
  adminShell(`<div class="panel"><span class="kicker">СПРАВОЧНИКИ И УСЛУГИ</span><h1>Подключения</h1>
    <p class="muted">Сначала добавьте клиента, тариф и адрес, затем создайте договор на подключение.</p></div>
    <div class="catalog-grid">
      <section class="panel"><h2>Клиенты</h2>
        <form data-catalog-endpoint="clients" class="fields">
          <label class="field">ФИО<input name="name" required maxlength="200"></label>
          <label class="field">Телефон<input name="phone" type="tel" required maxlength="40"></label>
          <button type="submit" class="btn primary">Добавить клиента</button></form>
        ${catalogListButton('clients', 'Список клиентов', data.clients.length)}</section>
      <section class="panel"><h2>Тарифы</h2>
        <form data-catalog-endpoint="tariffs" class="fields">
          <label class="field">Название<input name="name" required maxlength="100"></label>
          <label class="field">Скорость, Мбит/с<input name="speed_mbps" type="number" min="1" required></label>
          <button type="submit" class="btn primary">Добавить тариф</button></form>
        ${catalogListButton('tariffs', 'Список тарифов', data.tariffs.length)}</section>
      <section class="panel"><h2>Адреса</h2>
        <form data-catalog-endpoint="addresses" class="fields">
          <label class="field">Город<input name="city" required maxlength="100"></label>
          <label class="field">Улица<input name="street" required maxlength="100"></label>
          <label class="field">Дом<input name="building" required maxlength="30"></label>
          <label class="field">Квартира<input name="apartment" maxlength="30"></label>
          <button type="submit" class="btn primary">Добавить адрес</button></form>
        ${catalogListButton('addresses', 'Список адресов', data.addresses.length)}</section>
      <section class="panel"><h2>Договоры и подключения</h2>
        <form data-catalog-endpoint="services" class="fields">
          <label class="field">Клиент<select name="client_id" required>${catalogOptions(data.clients, c => c.name + ' · ' + (c.phone || '—'))}</select></label>
          <label class="field">Адрес подключения<select name="address_id" required>${catalogOptions(data.addresses, a => `${a.city}, ${a.street}, д. ${a.building}${a.apartment ? ', кв. ' + a.apartment : ''}`)}</select></label>
          <label class="field">Тариф<select name="tariff_id" required>${catalogOptions(data.tariffs, t => t.name)}</select></label>
          <label class="field">Номер договора<input name="account_number" required maxlength="100"></label>
          <button type="submit" class="btn primary">Добавить подключение</button></form>
        ${catalogListButton('services', 'Список договоров', data.services.length)}</section>
    </div>`);
  bindCatalogForms(showAdminServices);
  bindCatalogLists(data);
}

async function showAdminRentals() {
  const data = await api('/api/admin/catalog');
  adminShell(`<div class="panel"><span class="kicker">ОБОРУДОВАНИЕ</span><h1>Аренда роутеров</h1>
    <p class="muted">Добавьте модель и роутер, затем укажите подключение, которому передан роутер.</p></div>
    <div class="catalog-grid">
      <section class="panel"><h2>Модели роутеров</h2>
        <form data-catalog-endpoint="router-models" class="fields">
          <label class="field">Производитель<input name="manufacturer" required maxlength="100"></label>
          <label class="field">Модель<input name="model" required maxlength="100"></label>
          <button type="submit" class="btn primary">Добавить модель</button></form>
        ${catalogListButton('router_models', 'Список моделей роутеров', data.router_models.length)}</section>
      <section class="panel"><h2>Роутеры</h2>
        <form data-catalog-endpoint="routers" class="fields">
          <label class="field">Модель<select name="router_model_id" required>${catalogOptions(data.router_models, m => m.manufacturer + ' ' + m.model)}</select></label>
          <label class="field">Серийный номер<input name="serial_number" required maxlength="100"></label>
          <button type="submit" class="btn primary">Добавить роутер</button></form>
        ${catalogListButton('routers', 'Список роутеров', data.routers.length)}</section>
      <section class="panel catalog-wide"><h2>Передача в аренду</h2>
        <form data-catalog-endpoint="rentals" class="fields admin-fields">
          <label class="field">Договор и адрес<select name="service_id" required>${catalogOptions(data.services.filter(s => s.active), s => `${s.account_number} · ${s.client_name} · ${s.city}, ${s.street}, д. ${s.building}`)}</select></label>
          <label class="field">Роутер<select name="router_id" required>${catalogOptions(data.routers, r => r.serial_number + ' · ' + r.manufacturer + ' ' + r.model)}</select></label>
          <button type="submit" class="btn primary">Выдать роутер</button></form>
        ${catalogListButton('rentals', 'Список передач в аренду', data.rentals.length)}</section>
    </div>`);
  bindCatalogForms(showAdminRentals);
  bindCatalogLists(data);
}
