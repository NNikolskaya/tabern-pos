"use strict";

const $ = (id) => document.getElementById(id);
const money = (cents) => `${(cents / 100).toFixed(2)} ₴`;
const cart = new Map();
const originalLines = new Map();
const newKey = (itemId) => `new:${itemId}`;
const activeEntry = (itemId) => [...cart].reverse().find(([, line]) => line.menu_item_id === itemId && !line.issued);
let menu = [];
let categories = [];
let menuCategoryId = null;
let orders = [];
let selectedOrderId = null;
let baseRevision = null;
let comment = "";
let dirty = false;
let conflict = false;
let pending = null;
let busy = false;
let refreshing = false;
let editVersion = 0;
let retryTimer = null;
let syncBlocked = false;

function node(tag, className = "", value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
}

function setMessage(value, kind = "") {
  $("order-message").textContent = value;
  $("order-message").className = `message ${kind}`;
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
  } catch (_) {
    setConnection(false);
    throw new Error("Немає зв’язку з локальним сервером. Перевірте Wi-Fi.");
  }
  let body;
  try { body = await response.json(); }
  catch (_) { throw new Error("Неочікувана відповідь сервера."); }
  if (!response.ok) {
    const error = new Error(body.error || `Помилка сервера ${response.status}`);
    error.status = response.status;
    throw error;
  }
  eventContext.verify(path, requestEvent, body);
  setConnection(true);
  return body;
}

const jsonOptions = (method, body) => ({method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const readableTime = (value) => new Date(value).toLocaleString("uk-UA", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
const currentOrder = () => orders.find((order) => order.id === selectedOrderId);
const editable = () => selectedOrderId === null || !!currentOrder() && !currentOrder().paid;

function switchTab(tab) {
  const check = tab === "check";
  $("check-view").hidden = !check;
  $("orders-view").hidden = check;
  $("tab-check").classList.toggle("active", check);
  $("tab-orders").classList.toggle("active", !check);
  $("tab-check").setAttribute("aria-selected", String(check));
  $("tab-orders").setAttribute("aria-selected", String(!check));
}

function setCartFromOrder(order) {
  selectedOrderId = order.id;
  baseRevision = order.revision;
  comment = order.comment || "";
  dirty = false;
  conflict = false;
  syncBlocked = false;
  cart.clear();
  originalLines.clear();
  for (const line of order.lines) {
    const snapshot = {name: line.item_name, price_cents: line.price_cents, station: line.station, issued: line.issued, existing: true};
    if (!line.issued) originalLines.set(line.menu_item_id, snapshot);
    cart.set(`line:${line.id}`, {...snapshot, id: line.id, menu_item_id: line.menu_item_id, quantity: line.quantity});
  }
  sessionStorage.removeItem("order-draft");
}

function startNewCheck() {
  if (pending || dirty || busy) { setMessage("Дочекайтеся синхронізації поточного чека.", "error"); return; }
  selectedOrderId = null;
  baseRevision = null;
  comment = "";
  dirty = false;
  conflict = false;
  cart.clear();
  originalLines.clear();
  sessionStorage.removeItem("order-draft");
  setMessage("");
  switchTab("check");
  renderAll();
}

function openOrder(id) {
  if (pending || dirty || busy) { setMessage("Дочекайтеся синхронізації поточного чека.", "error"); switchTab("check"); return; }
  const order = orders.find((entry) => entry.id === id);
  if (!order) return;
  setCartFromOrder(order);
  setMessage("");
  switchTab("check");
  renderAll();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function modifyQuantity(id, delta) {
  if (!editable() || conflict) return;
  const line = cart.get(id);
  if (!line || line.issued) return;
  const quantity = line.quantity + delta;
  if (quantity < 1) cart.delete(id);
  else if (quantity <= 999) line.quantity = quantity;
  queueSave(id);
}

function addMenuItem(item) {
  if (!editable() || conflict) return;
  const existing = activeEntry(item.id);
  const key = existing?.[0] ?? newKey(item.id);
  const line = existing?.[1];
  if (line) {
    if (line.quantity >= 999) return;
    line.quantity += 1;
  } else {
    const snapshot = originalLines.get(item.id);
    cart.set(key, snapshot ? {...snapshot, menu_item_id: item.id, quantity: 1} : {menu_item_id: item.id, name: item.name, price_cents: item.price_cents, station: item.station, issued: false, quantity: 1, existing: false});
  }
  queueSave(key);
}

function renderMenu() {
  const root = $("menu");
  root.replaceChildren();
  const currentCategory = categories.find((category) => category.id === menuCategoryId);
  if (menuCategoryId !== null && !currentCategory) menuCategoryId = null;
  if (currentCategory) {
    const back = node("button", "menu-folder menu-back");
    back.type = "button";
    back.setAttribute("aria-label", "Назад до попереднього розділу");
    back.append(node("span", "menu-folder-icon", "←"), node("span", "menu-card-content", "Назад"));
    back.addEventListener("click", () => { menuCategoryId = currentCategory.parent_id; renderMenu(); });
    root.append(back);
  }
  if (!menu.length && !categories.length) {
    root.append(node("p", "muted", "Меню порожнє. Додайте позиції в адмінці."));
    return;
  }
  const children = categories.filter((category) => category.parent_id === menuCategoryId);
  const items = menu.filter((item) => item.category_id === menuCategoryId);
  for (const category of children) {
    const button = node("button", "menu-folder");
    button.type = "button";
    if (category.has_image) {
      const picture = node("img", "menu-card-image");
      picture.src = `/api/categories/${category.id}/image`;
      picture.alt = "";
      picture.loading = "lazy";
      picture.addEventListener("error", () => { picture.remove(); button.prepend(node("span", "menu-folder-icon", "⌂")); });
      button.append(picture);
    } else button.append(node("span", "menu-folder-icon", "⌂"));
    button.append(node("span", "menu-card-content", category.name));
    button.addEventListener("click", () => { menuCategoryId = category.id; renderMenu(); });
    root.append(button);
  }
  for (const item of items) {
    const button = node("button", "menu-item");
    button.type = "button";
    button.disabled = !editable() || conflict || (activeEntry(item.id)?.[1].quantity ?? 0) >= 999;
    button.title = `${item.name} · ${money(item.price_cents)}`;
    const picture = node("img", "menu-card-image");
    picture.src = item.has_image ? `/api/menu/${item.id}/image` : "/default-dish.svg";
    picture.alt = "";
    picture.loading = "lazy";
    picture.addEventListener("error", () => { if (!picture.src.endsWith("/default-dish.svg")) picture.src = "/default-dish.svg"; });
    const content = node("span", "menu-card-content");
    content.append(node("strong", "", item.name), node("small", "", money(item.price_cents)));
    button.append(picture, content);
    button.addEventListener("click", () => addMenuItem(item));
    root.append(button);
  }
  if (!children.length && !items.length) root.append(node("p", "empty-state", "У цьому розділі немає позицій."));
}

function renderCheck() {
  const order = currentOrder();
  const paid = order && order.paid;
  $("check-title").textContent = order ? `Чек №${order.number}` : "Новий чек";
  $("check-time").textContent = order ? readableTime(order.created_at) : "";
  $("check-state").textContent = order && order.completed ? "Завершено" : paid ? "Оплачено" : order ? "Не оплачено" : "Новий";
  $("check-state").className = `badge ${paid ? "paid" : ""}`;
  $("menu-panel").hidden = false;
  const root = $("cart");
  root.replaceChildren();
  let total = 0;
  let unavailable = false;
  for (const [id, line] of cart) {
    if (!line.existing && !menu.some((item) => item.id === line.menu_item_id)) unavailable = true;
    total += line.price_cents * line.quantity;
    const row = node("div", "receipt-line");
    row.dataset.itemId = String(id);
    const item = node("div", "receipt-item");
    item.append(node("strong", "", line.name), node("small", "", `${money(line.price_cents)} / шт.`));
    const quantity = node("div", "receipt-quantity");
    item.append(node("small", line.issued ? "issued-label" : "waiting-label", `${line.station === "KITCHEN" ? "Кухня" : "Бар"} · ${line.issued ? "Видано" : "Очікує видачі"}`));
    if (line.issued) {
      quantity.append(node("span", "", String(line.quantity)));
    } else {
      const minus = node("button", "quantity-button", "−");
      minus.type = "button";
      minus.disabled = conflict || !editable();
      minus.setAttribute("aria-label", `Зменшити кількість ${line.name}`);
      minus.addEventListener("click", () => modifyQuantity(id, -1));
      const plus = node("button", "quantity-button", "+");
      plus.type = "button";
      plus.disabled = line.quantity >= 999 || conflict || !editable();
      plus.setAttribute("aria-label", `Збільшити кількість ${line.name}`);
      plus.addEventListener("click", () => modifyQuantity(id, 1));
      quantity.append(minus, node("span", "quantity-value", String(line.quantity)), plus);
    }
    row.append(item, quantity, node("strong", "receipt-line-total", money(line.price_cents * line.quantity)));
    root.append(row);
  }
  if (!cart.size) root.append(node("p", "empty-state", "Оберіть позиції з меню, щоб створити чек."));
  $("total").textContent = money(total);
  if ($("order-comment").value !== comment) $("order-comment").value = comment;
  $("order-comment").disabled = !editable() || conflict;
  $("jump-receipt").textContent = `До чека · ${money(total)}`;
  renderSaveState();
  $("paid-order").classList.toggle("hidden", !order);
  $("paid-order").textContent = paid ? "Скасувати оплату" : "Оплатити";
  $("paid-order").classList.toggle("reverse-payment", !!paid);
  $("paid-order").disabled = busy || dirty || conflict || !!pending || (!paid && !cart.size);
  $("discard-changes").classList.toggle("hidden", !order || !conflict);
  $("new-check").disabled = !!pending || busy || dirty;
  $("new-check-mobile").disabled = $("new-check").disabled;
  if (unavailable && !pending) setMessage("Позиція більше недоступна. Видаліть її з чека.", "error");
}

function renderSaveState() {
  const order = currentOrder();
  $("save-state").textContent = conflict ? "Конфлікт змін · завантажте поточну версію" : dirty || busy ? "Синхронізація…" : order?.paid ? "Оплачений чек. Скасуйте оплату, щоб редагувати." : order ? "Усі зміни збережено" : "Чек створиться після вибору першої позиції.";
  $("save-state").classList.toggle("unsaved", !!conflict || !!dirty);
}

function updateCartDisplay(id) {
  const row = $("cart").querySelector(`[data-item-id="${id}"]`);
  const line = cart.get(id);
  if (row && line) {
    row.querySelector(".quantity-value").textContent = String(line.quantity);
    row.querySelector(".receipt-line-total").textContent = money(line.price_cents * line.quantity);
    row.querySelectorAll(".quantity-button")[1].disabled = line.quantity >= 999;
  } else {
    renderCheck();
  }
  const total = Array.from(cart.values()).reduce((sum, entry) => sum + entry.price_cents * entry.quantity, 0);
  $("total").textContent = money(total);
  $("jump-receipt").textContent = `До чека · ${money(total)}`;
  $("paid-order").disabled = true;
  $("new-check").disabled = true;
  $("new-check-mobile").disabled = true;
  renderSaveState();
}

function renderOrders() {
  const root = $("orders");
  root.replaceChildren();
  $("orders-count").textContent = `${orders.length} замовлень`;
  $("tab-orders-count").textContent = String(orders.length);
  if (!orders.length) {
    root.append(node("p", "empty-state", "Поки що немає замовлень."));
    return;
  }
  for (const order of orders) {
    const card = node("button", "order-card");
    card.type = "button";
    const top = node("div", "order-card-top");
    top.append(node("strong", "", `Чек №${order.number}`), node("span", `badge ${order.paid ? "paid" : ""}`, order.completed ? "Завершено" : order.paid ? "Оплачено" : "Не оплачено"));
    card.append(top, node("span", "muted", readableTime(order.created_at)),
      ...(order.comment ? [node("span", "order-comment-visible", order.comment)] : []),
      node("span", "order-summary", order.lines.map((line) => `${line.quantity}× ${line.item_name}`).join(", ")),
      node("span", "order-summary", `${order.lines.filter((line) => line.issued).length}/${order.lines.length} видано`),
      node("strong", "order-total", money(order.total_cents)));
    card.addEventListener("click", () => openOrder(order.id));
    root.append(card);
  }
}

function renderAll() {
  renderCheck();
  renderMenu();
  renderOrders();
}

function newRequestId() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (globalThis.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index++) bytes[index] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function upsertOrder(order) {
  orders = [order, ...orders.filter((entry) => entry.id !== order.id)].sort((a, b) => b.id - a.id);
}

function cartLines() {
  return Array.from(cart.values(), (line) => ({menu_item_id: line.menu_item_id, quantity: line.quantity, ...(line.id ? {line_id: line.id} : {})}));
}

function sameLines(left, right) {
  const plain = (entries) => entries.map((line) => ({menu_item_id: line.menu_item_id, quantity: line.quantity}));
  return JSON.stringify(plain(left)) === JSON.stringify(plain(right));
}

function storeDraft() {
  sessionStorage.setItem("order-draft", JSON.stringify({event_id: eventContext.id, selectedOrderId, baseRevision, comment, cart: Array.from(cart)}));
}

function queueSave(id) {
  editVersion += 1;
  dirty = true;
  syncBlocked = false;
  storeDraft();
  setMessage("");
  updateCartDisplay(id);
  scheduleSave(150);
}

function scheduleSave(delay) {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(() => { retryTimer = null; flushChanges(); }, delay);
}

async function refreshData() {
  if (refreshing) return;
  refreshing = true;
  try {
    try { await eventContext.refresh(); }
    catch (_) { setConnection(false); return; }
    const before = JSON.stringify([menu, categories, orders, selectedOrderId, baseRevision, conflict, !!pending]);
    const results = await Promise.allSettled([api("/api/menu"), api("/api/categories"), api("/api/orders")]);
    if (results[0].status === "fulfilled") {
      menu = results[0].value.items;
      for (const line of cart.values()) {
        if (line.existing) continue;
        const item = menu.find((entry) => entry.id === line.menu_item_id);
        if (item) { line.name = item.name; line.price_cents = item.price_cents; }
      }
    }
    if (results[1].status === "fulfilled") categories = results[1].value.categories;
    if (results[2].status === "fulfilled") {
      const fresh = results[2].value.orders;
      if (pending && !busy) {
        const saved = fresh.find((entry) => entry.request_id === pending.request_id);
        if (saved) {
          pending = null;
          sessionStorage.removeItem("pending-order");
          if (sameLines(cartLines(), saved.lines) && comment === (saved.comment || "")) {
            setCartFromOrder(saved);
          } else {
            selectedOrderId = saved.id;
            baseRevision = saved.revision;
            storeDraft();
          }
        }
      }
      const selected = fresh.find((entry) => entry.id === selectedOrderId);
      if (selected && selected.revision !== baseRevision && !busy) {
        if (!dirty) {
          setCartFromOrder(selected);
        } else if (sameLines(cartLines(), selected.lines) && comment === (selected.comment || "") && !selected.paid) {
          baseRevision = selected.revision;
          for (const line of selected.lines) {
            const current = [...cart.values()].find((entry) => entry.id === line.id);
            if (current) current.issued = line.issued;
          }
        } else {
          conflict = true;
          setMessage("Чек змінено на іншому пристрої. Завантажте поточну версію.", "error");
        }
      }
      orders = fresh;
    }
    if (results.some((result) => result.status === "rejected")) setConnection(false);
    if (before !== JSON.stringify([menu, categories, orders, selectedOrderId, baseRevision, conflict, !!pending])) renderAll();
    if (dirty && !busy && !conflict && !syncBlocked && !retryTimer && results[2].status === "fulfilled") flushChanges();
  } finally { refreshing = false; }
}

async function flushChanges() {
  if (busy || conflict || syncBlocked || !dirty) return;
  if (selectedOrderId === null && !cart.size) {
    dirty = false;
    sessionStorage.removeItem("order-draft");
    renderAll();
    return;
  }
  busy = true;
  const version = editVersion;
  const lines = cartLines();
  const sentComment = comment;
  renderAll();
  try {
    let saved;
    if (selectedOrderId === null) {
      if (!pending) {
        pending = {event_id: eventContext.id, request_id: newRequestId(), lines, comment: sentComment};
        sessionStorage.setItem("pending-order", JSON.stringify(pending));
      }
      saved = await api("/api/orders", jsonOptions("POST", pending));
      pending = null;
      sessionStorage.removeItem("pending-order");
    } else {
      saved = await api(`/api/orders/${selectedOrderId}`, jsonOptions("PUT", {expected_revision: baseRevision, line_mode: "identified", lines, comment: sentComment}));
    }
    upsertOrder(saved);
    selectedOrderId = saved.id;
    baseRevision = saved.revision;
    if (version === editVersion && sameLines(cartLines(), saved.lines) && comment === (saved.comment || "")) {
      setCartFromOrder(saved);
      setMessage("");
    } else {
      for (const line of saved.lines) {
        const local = cart.get(newKey(line.menu_item_id));
        if (local && !local.id && !line.issued) { local.id = line.id; local.existing = true; }
        if (!line.issued) originalLines.set(line.menu_item_id, {name: line.item_name, price_cents: line.price_cents, station: line.station, issued: false, existing: true});
      }
      storeDraft();
    }
  } catch (error) {
    if (error.status === 400 && pending) {
      pending = null;
      sessionStorage.removeItem("pending-order");
    }
    if (error.status === 400) syncBlocked = true;
    if (error.status === 409 && selectedOrderId !== null) {
      try {
        const current = await api(`/api/orders/${selectedOrderId}`);
        upsertOrder(current);
        if (sameLines(lines, current.lines) && sentComment === (current.comment || "") && !current.paid) {
          baseRevision = current.revision;
          if (version === editVersion && sameLines(cartLines(), current.lines) && comment === (current.comment || "")) setCartFromOrder(current);
          else storeDraft();
          setMessage("");
        } else {
          conflict = true;
          setMessage("Чек змінено на іншому пристрої. Завантажте поточну версію.", "error");
        }
      } catch (_) { scheduleSave(2500); }
    } else {
      setMessage(error.message + (!error.status ? " Повторимо автоматично після відновлення зв’язку." : ""), "error");
      if (!error.status) scheduleSave(2500);
    }
  } finally {
    busy = false;
    renderAll();
    if (dirty && !conflict && !syncBlocked && !retryTimer) scheduleSave(150);
  }
}

async function changePayment(orderId, paid, announce = true) {
  const order = currentOrder();
  if (busy || (dirty && order?.id === orderId)) throw new Error("Чек ще синхронізується");
  busy = true;
  renderAll();
  try {
    const saved = await api(`/api/orders/${orderId}/status`, jsonOptions("PUT", {status: paid ? "paid" : "unpaid"}));
    upsertOrder(saved);
    if (selectedOrderId === orderId) setCartFromOrder(saved);
    setMessage(`Чек №${saved.number} ${paid ? "оплачено" : "знову не оплачено"}.`, "success");
    if (announce) showUndo(paid ? "Чек оплачено" : "Оплату скасовано", () => changePayment(orderId, !paid, false));
  } catch (error) {
    if (error.status === 409) await refreshData();
    setMessage(error.message, "error");
    throw error;
  } finally { busy = false; renderAll(); }
}

function markPaid() {
  const order = currentOrder();
  if (!order || dirty || conflict || busy || (!order.paid && !cart.size)) return;
  changePayment(order.id, !order.paid).catch(() => {});
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
    if (info.addresses.length > 1) {
      const more = node("details", "other-addresses");
      more.append(node("summary", "", "Інші адреси"));
      for (const address of info.addresses.slice(1)) {
        const link = node("a", "", address);
        link.href = address;
        more.append(link);
      }
      hint.append(more);
    }
  } catch (_) { /* The connection indicator covers this. */ }
}

function resetForEvent() {
  selectedOrderId = null;
  baseRevision = null;
  comment = "";
  dirty = false;
  conflict = false;
  pending = null;
  busy = false;
  syncBlocked = false;
  clearTimeout(retryTimer);
  retryTimer = null;
  cart.clear();
  originalLines.clear();
  menu = [];
  categories = [];
  orders = [];
  menuCategoryId = null;
  sessionStorage.removeItem("order-draft");
  sessionStorage.removeItem("pending-order");
  setMessage("Поточну подію змінено. Завантажуємо її дані.");
  renderAll();
  refreshData();
}

async function init() {
  window.addEventListener("event-changed", (event) => { if (event.detail.previous) resetForEvent(); });
  await eventContext.ready;
  try {
    const stored = sessionStorage.getItem("pending-order");
    if (stored) {
      const saved = JSON.parse(stored);
      if (eventContext.id && (!saved.event_id || saved.event_id === eventContext.id)) pending = saved;
      else sessionStorage.removeItem("pending-order");
    }
    const draft = sessionStorage.getItem("order-draft");
    if (draft) {
      const state = JSON.parse(draft);
      if (!eventContext.id || (state.event_id && state.event_id !== eventContext.id)) throw new Error("Draft belongs to another Event");
      selectedOrderId = state.selectedOrderId;
      baseRevision = state.baseRevision;
      comment = state.comment || "";
      for (const [id, line] of state.cart) {
        const itemId = line.menu_item_id ?? Number(id);
        line.menu_item_id = itemId;
        cart.set(line.id ? `line:${line.id}` : newKey(itemId), line);
        if (line.existing && !line.issued) originalLines.set(itemId, {name: line.name, price_cents: line.price_cents, station: line.station, issued: false, existing: true});
      }
      dirty = !!cart.size || selectedOrderId !== null;
      editVersion = 1;
      if (dirty) setMessage("Відновлюємо незавершену синхронізацію…");
    } else if (pending) {
      comment = pending.comment || "";
      for (const line of pending.lines) cart.set(newKey(line.menu_item_id), {menu_item_id: line.menu_item_id, name: `Позиція №${line.menu_item_id}`, price_cents: 0, quantity: line.quantity, existing: false});
      dirty = true;
      editVersion = 1;
    }
  } catch (_) { pending = null; sessionStorage.removeItem("pending-order"); sessionStorage.removeItem("order-draft"); }
  $("tab-check").addEventListener("click", () => switchTab("check"));
  $("tab-orders").addEventListener("click", () => switchTab("orders"));
  $("new-check").addEventListener("click", startNewCheck);
  $("new-check-mobile").addEventListener("click", startNewCheck);
  $("jump-receipt").addEventListener("click", () => document.querySelector(".receipt-panel").scrollIntoView({behavior: "smooth"}));
  $("back-to-top").addEventListener("click", () => window.scrollTo({top: 0, behavior: "smooth"}));
  $("paid-order").addEventListener("click", markPaid);
  $("order-comment").addEventListener("input", (event) => {
    if (!editable() || conflict) return;
    comment = event.target.value;
    if (selectedOrderId === null && !cart.size) { storeDraft(); return; }
    queueSave(null);
  });
  $("discard-changes").addEventListener("click", () => {
    const order = currentOrder();
    if (order) { setCartFromOrder(order); setMessage("Завантажено поточну версію чека."); renderAll(); }
  });
  renderAll();
  refreshData();
  showAddresses();
  setInterval(refreshData, 2500);
  window.addEventListener("focus", refreshData);
  window.addEventListener("online", refreshData);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshData(); });
}

init();
