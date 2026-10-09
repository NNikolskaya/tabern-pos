"use strict";

const $ = (id) => document.getElementById(id);
const money = (cents) => `${(cents / 100).toFixed(2)} ₴`;
const jsonOptions = (method, body) => ({method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
let menu = [];
let categories = [];
let events = [];
let editingItemId = null;
let editingCategoryId = null;
let editingEventId = null;
const expandedCategories = new Set();
let refreshing = false;

function node(tag, className = "", value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
}

function message(id, value, kind = "") {
  $(id).textContent = value;
  $(id).className = `message ${kind}`;
}

function setConnection(online) {
  $("connection").textContent = online ? "Підключено" : "Немає з’єднання · повторюємо";
  $("connection").className = `connection ${online ? "online" : "offline"}`;
}

async function api(path, options = {}) {
  let response;
  let requestEvent;
  try {
    const request = eventContext.request(path, options);
    requestEvent = request.eventId;
    response = await fetch(path, {cache: "no-store", ...request.options});
  }
  catch (_) { setConnection(false); throw new Error("Немає зв’язку з локальним сервером. Перевірте Wi-Fi."); }
  let body;
  try { body = await response.json(); }
  catch (_) { throw new Error("Неочікувана відповідь сервера."); }
  if (!response.ok) throw new Error(body.error || `Помилка сервера ${response.status}`);
  eventContext.verify(path, requestEvent, body);
  setConnection(true);
  return body;
}

function resetEventForm() {
  editingEventId = null;
  $("event-form").reset();
  $("save-event").textContent = "Створити подію";
  $("cancel-event").classList.add("hidden");
}

function editEvent(event) {
  editingEventId = event.id;
  $("event-name").value = event.name;
  $("event-date").value = event.date || "";
  $("event-enabled").checked = event.enabled;
  $("save-event").textContent = "Зберегти подію";
  $("cancel-event").classList.remove("hidden");
  $("event-name").focus();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function renderEvents() {
  $("selected-event-name").textContent = eventContext.event?.name || "Подія недоступна";
  const root = $("event-list");
  root.replaceChildren();
  for (const event of events) {
    const row = node("div", `event-row ${event.selected ? "selected" : ""}`);
    const main = node("div", "event-row-main");
    main.append(node("strong", "", event.name), node("small", "", `${event.date || "Без дати"} · ${event.selected ? "Вибрана зараз" : event.enabled ? "Доступна" : "Вимкнена"}`));
    const actions = node("div", "event-row-actions");
    if (!event.selected) {
      const select = node("button", "primary", "Обрати");
      select.type = "button";
      select.disabled = !event.enabled;
      select.addEventListener("click", async () => {
        select.disabled = true;
        try {
          await api(`/api/events/${event.id}/select`, {method: "PUT"});
          await eventContext.refresh();
          message("event-message", `Поточна подія: ${event.name}`, "success");
          await refreshAdmin();
        } catch (error) { message("event-message", error.message, "error"); }
        finally { select.disabled = false; }
      });
      actions.append(select);
    }
    const edit = node("button", "secondary", "Змінити");
    edit.type = "button";
    edit.addEventListener("click", () => editEvent(event));
    actions.append(edit);
    row.append(main, actions);
    root.append(row);
  }
}

function categoryOptions(select, selected, excludedId = null) {
  const excluded = new Set();
  if (excludedId !== null) {
    const collect = (id) => {
      excluded.add(id);
      for (const child of categories.filter((entry) => entry.parent_id === id)) collect(child.id);
    };
    collect(excludedId);
  }
  select.replaceChildren(node("option", "", "Корінь меню"));
  select.firstChild.value = "";
  const append = (parentId, depth) => {
    for (const category of categories.filter((entry) => entry.parent_id === parentId)) {
      if (excluded.has(category.id)) continue;
      const option = node("option", "", `${"　".repeat(depth)}${category.name}`);
      option.value = String(category.id);
      select.append(option);
      append(category.id, depth + 1);
    }
  };
  append(null, 0);
  select.value = selected === null ? "" : String(selected);
}

function resetItemForm() {
  editingItemId = null;
  $("item-form").reset();
  $("form-heading").textContent = "Додати позицію";
  $("save-item").textContent = "Додати позицію";
  $("cancel-edit").classList.add("hidden");
  $("remove-image").classList.add("hidden");
}

function resetCategoryForm() {
  editingCategoryId = null;
  $("category-form").reset();
  $("category-form-heading").textContent = "Додати розділ";
  $("save-category").textContent = "Додати розділ";
  $("cancel-category").classList.add("hidden");
  $("remove-category-image").classList.add("hidden");
  categoryOptions($("category-parent"), null);
}

function editItem(item) {
  editingItemId = item.id;
  $("item-name").value = item.name;
  $("item-price").value = (item.price_cents / 100).toFixed(2);
  $("item-category").value = item.category_id === null ? "" : String(item.category_id);
  $("item-station").value = item.station;
  $("remove-image").classList.toggle("hidden", !item.has_image);
  $("item-enabled").checked = item.enabled;
  $("form-heading").textContent = "Редагувати позицію";
  $("save-item").textContent = "Зберегти позицію";
  $("cancel-edit").classList.remove("hidden");
  $("item-name").focus({preventScroll: true});
  $("form-heading").scrollIntoView({behavior: "smooth", block: "start"});
}

function editCategory(category) {
  editingCategoryId = category.id;
  $("category-name").value = category.name;
  categoryOptions($("category-parent"), category.parent_id, category.id);
  $("category-form-heading").textContent = "Редагувати розділ";
  $("remove-category-image").classList.toggle("hidden", !category.has_image);
  $("save-category").textContent = "Зберегти розділ";
  $("cancel-category").classList.remove("hidden");
  $("category-name").focus({preventScroll: true});
  $("category-form-heading").scrollIntoView({behavior: "smooth", block: "start"});
}

function renderItem(root, item, depth) {
  const row = node("div", `admin-row ${item.enabled ? "" : "disabled"}`);
  row.style.paddingLeft = `${depth * 18}px`;
  const main = node("div", "admin-row-main admin-item-main");
  const picture = node("img", "admin-item-image");
  picture.src = item.has_image ? `/api/menu/${item.id}/image` : "/default-dish.svg";
  picture.alt = "";
  const details = node("div", "admin-item-text");
  details.append(node("strong", "", item.name), node("small", "", `${money(item.price_cents)} · ${item.station === "KITCHEN" ? "Кухня" : "Бар"} · ${item.enabled ? "Доступна" : "Вимкнена"}`));
  main.append(picture, details);
  const actions = node("div", "admin-row-actions");
  const edit = node("button", "", "Змінити");
  edit.type = "button";
  edit.addEventListener("click", () => editItem(item));
  const toggle = node("button", "", item.enabled ? "Вимкнути" : "Увімкнути");
  toggle.type = "button";
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      await api(`/api/menu/${item.id}`, jsonOptions("PUT", {name: item.name, price: (item.price_cents / 100).toFixed(2), category_id: item.category_id, station: item.station, enabled: !item.enabled}));
      await refreshAdmin();
    } catch (error) { message("admin-message", error.message, "error"); }
    finally { toggle.disabled = false; }
  });
  actions.append(edit, toggle);
  row.append(main, actions);
  root.append(row);
}

function renderCategory(root, category, depth) {
  const row = node("div", "admin-row admin-category-row");
  row.style.paddingLeft = `${depth * 18}px`;
  const main = node("div", "admin-row-main");
  const expanded = expandedCategories.has(category.id);
  const toggle = node("button", "admin-category-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-label", `${expanded ? "Згорнути" : "Розгорнути"} розділ ${category.name}`);
  toggle.append(node("span", "admin-category-arrow", expanded ? "▾" : "▸"));
  if (category.has_image) {
    const picture = node("img", "admin-item-image");
    picture.src = `/api/categories/${category.id}/image`;
    picture.alt = "";
    toggle.append(picture);
  }
  toggle.append(node("strong", "", `${category.has_image ? "" : "📁 "}${category.name}`));
  toggle.addEventListener("click", () => {
    if (expanded) expandedCategories.delete(category.id);
    else expandedCategories.add(category.id);
    renderAdmin();
  });
  main.append(toggle);
  row.append(main);
  const actions = node("div", "admin-row-actions");
  const add = node("button", "", "+ Позиція");
  add.type = "button";
  add.addEventListener("click", () => { resetItemForm(); $("item-category").value = String(category.id); $("item-name").focus({preventScroll: true}); $("form-heading").scrollIntoView({behavior: "smooth", block: "start"}); });
  const edit = node("button", "", "Змінити");
  edit.type = "button";
  edit.addEventListener("click", () => editCategory(category));
  const remove = node("button", "", "Видалити");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    if (!window.confirm(`Видалити порожній розділ «${category.name}»?`)) return;
    remove.disabled = true;
    try { await api(`/api/categories/${category.id}`, {method: "DELETE"}); await refreshAdmin(); }
    catch (error) { message("category-message", error.message, "error"); }
    finally { remove.disabled = false; }
  });
  actions.append(add, edit, remove);
  row.append(actions);
  root.append(row);
}

function renderAdmin() {
  renderEvents();
  const itemSelected = $("item-category").value;
  const parentSelected = $("category-parent").value;
  categoryOptions($("item-category"), itemSelected ? Number(itemSelected) : null);
  categoryOptions($("category-parent"), parentSelected ? Number(parentSelected) : null, editingCategoryId);
  const root = $("admin-items");
  root.replaceChildren();
  $("admin-count").textContent = `${categories.length} розділів · ${menu.length} позицій`;
  const renderLevel = (parentId, depth) => {
    for (const category of categories.filter((entry) => entry.parent_id === parentId)) {
      renderCategory(root, category, depth);
      if (expandedCategories.has(category.id)) renderLevel(category.id, depth + 1);
    }
    for (const item of menu.filter((entry) => entry.category_id === parentId)) renderItem(root, item, depth);
  };
  renderLevel(null, 0);
  if (!menu.length && !categories.length) root.append(node("p", "empty-state", "Меню порожнє. Додайте розділ або позицію."));
}

async function refreshAdmin() {
  if (refreshing) return;
  refreshing = true;
  try {
    await eventContext.refresh();
    const results = await Promise.all([api("/api/events"), api("/api/menu?all=1"), api("/api/categories")]);
    events = results[0].events;
    menu = results[1].items;
    categories = results[2].categories;
    renderAdmin();
  } catch (_) { setConnection(false); }
  finally { refreshing = false; }
}

async function showAddresses() {
  try {
    const info = await api("/api/info");
    const hint = $("network-hint");
    hint.replaceChildren();
    if (!info.addresses.length) return;
    hint.append(node("span", "", "Адреса для інших пристроїв: "));
    const first = node("a", "", info.addresses[0]);
    first.href = info.addresses[0];
    hint.append(first);
  } catch (_) { /* Status appears in the connection indicator. */ }
}

function init() {
  window.addEventListener("event-changed", (event) => {
    if (!event.detail.previous) return;
    menu = [];
    categories = [];
    expandedCategories.clear();
    resetItemForm();
    resetCategoryForm();
    renderAdmin();
    refreshAdmin();
  });
  $("cancel-event").addEventListener("click", resetEventForm);
  $("event-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {name: $("event-name").value, date: $("event-date").value || null, enabled: $("event-enabled").checked};
    const button = $("save-event");
    button.disabled = true;
    try {
      const saved = editingEventId === null
        ? await api("/api/events", jsonOptions("POST", payload))
        : await api(`/api/events/${editingEventId}`, jsonOptions("PUT", payload));
      resetEventForm();
      message("event-message", saved.selected ? "Поточну подію оновлено." : "Подію збережено. Натисніть «Обрати», щоб працювати з нею.", "success");
      await eventContext.refresh();
      await refreshAdmin();
    } catch (error) { message("event-message", error.message, "error"); }
    finally { button.disabled = false; }
  });
  $("cancel-edit").addEventListener("click", resetItemForm);
  $("remove-image").addEventListener("click", async () => {
    if (editingItemId === null) return;
    try {
      await api(`/api/menu/${editingItemId}/image`, {method: "DELETE"});
      $("remove-image").classList.add("hidden");
      await refreshAdmin();
      message("admin-message", "Фото прибрано. Використовується стандартне зображення.", "success");
    } catch (error) { message("admin-message", error.message, "error"); }
  });
  $("cancel-category").addEventListener("click", resetCategoryForm);
  $("remove-category-image").addEventListener("click", async () => {
    if (editingCategoryId === null) return;
    try {
      await api(`/api/categories/${editingCategoryId}/image`, {method: "DELETE"});
      $("remove-category-image").classList.add("hidden");
      await refreshAdmin();
      message("category-message", "Картинку розділу прибрано.", "success");
    } catch (error) { message("category-message", error.message, "error"); }
  });
  $("item-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = $("item-category").value;
    const payload = {name: $("item-name").value, price: $("item-price").value, category_id: selected ? Number(selected) : null, station: $("item-station").value, enabled: $("item-enabled").checked};
    const file = $("item-image").files[0];
    if (file && (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 3 * 1024 * 1024)) {
      message("admin-message", "Оберіть JPEG, PNG або WebP до 3 МБ.", "error");
      return;
    }
    const button = $("save-item");
    button.disabled = true;
    try {
      const saved = editingItemId === null
        ? await api("/api/menu", jsonOptions("POST", payload))
        : await api(`/api/menu/${editingItemId}`, jsonOptions("PUT", payload));
      editingItemId = saved.id;
      if (file) await api(`/api/menu/${saved.id}/image`, {method: "PUT", headers: {"Content-Type": file.type}, body: file});
      resetItemForm();
      message("admin-message", "Позицію збережено.", "success");
      await refreshAdmin();
    } catch (error) { message("admin-message", error.message, "error"); await refreshAdmin(); }
    finally { button.disabled = false; }
  });
  $("category-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = $("category-parent").value;
    const payload = {name: $("category-name").value, parent_id: selected ? Number(selected) : null};
    const file = $("category-image").files[0];
    if (file && (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 3 * 1024 * 1024)) {
      message("category-message", "Оберіть JPEG, PNG або WebP до 3 МБ.", "error");
      return;
    }
    const button = $("save-category");
    button.disabled = true;
    try {
      const saved = editingCategoryId === null
        ? await api("/api/categories", jsonOptions("POST", payload))
        : await api(`/api/categories/${editingCategoryId}`, jsonOptions("PUT", payload));
      editingCategoryId = saved.id;
      if (file) await api(`/api/categories/${saved.id}/image`, {method: "PUT", headers: {"Content-Type": file.type}, body: file});
      resetCategoryForm();
      message("category-message", "Розділ збережено.", "success");
      await refreshAdmin();
    } catch (error) { message("category-message", error.message, "error"); await refreshAdmin(); }
    finally { button.disabled = false; }
  });
  eventContext.ready.then(refreshAdmin);
  showAddresses();
  setInterval(refreshAdmin, 2500);
  window.addEventListener("focus", refreshAdmin);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAdmin(); });
}

init();
