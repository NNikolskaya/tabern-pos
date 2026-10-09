"use strict";

const station = location.pathname === "/kitchen" ? "KITCHEN" : "BAR";
const $ = (id) => document.getElementById(id);
const money = (cents) => `${(cents / 100).toFixed(2)} ₴`;
let orders = [];
let refreshing = false;
const pending = new Set();
const expandedOrders = new Set();

function node(tag, className, value) {
  const element = document.createElement(tag);
  element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
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
    throw new Error("Немає зв’язку з локальним сервером.");
  }
  let body;
  try { body = await response.json(); }
  catch (_) { throw new Error("Неочікувана відповідь сервера."); }
  if (!response.ok) {
    const error = new Error(body.error || `Помилка ${response.status}`);
    error.status = response.status;
    throw error;
  }
  eventContext.verify(path, requestEvent, body);
  setConnection(true);
  return body;
}

function updateOrder(order) {
  orders = [order, ...orders.filter((entry) => entry.id !== order.id)].sort((a, b) => b.id - a.id);
  render();
  window.dispatchEvent(new CustomEvent("orders-refreshed", {detail: orders}));
}

async function setIssued(orderId, lineId, issued, announce = true) {
  const key = `${orderId}:${lineId}`;
  if (pending.has(key) || pending.has(`bulk:${orderId}`)) return;
  pending.add(key);
  render();
  try {
    updateOrder(await api(`/api/orders/${orderId}/order-lines/${lineId}`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({issued})
    }));
    if (announce) showUndo(issued ? "Позицію видано" : "Позицію повернуто в не видано", () => setIssued(orderId, lineId, !issued, false));
  } catch (error) { await refresh(); throw error; }
  finally { pending.delete(key); render(); }
}

async function setIssuedBulk(orderId, lineIds, issued, announce = true) {
  const key = `bulk:${orderId}`;
  if (!lineIds.length || pending.has(key) || lineIds.some((lineId) => pending.has(`${orderId}:${lineId}`))) return;
  pending.add(key);
  render();
  try {
    updateOrder(await api(`/api/orders/${orderId}/station-lines`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({station, line_ids: lineIds, issued})
    }));
    if (announce) showUndo(issued ? "Усі позиції станції видано" : "Позиції повернуто в не видано", () => setIssuedBulk(orderId, lineIds, !issued, false));
  } catch (error) { await refresh(); throw error; }
  finally { pending.delete(key); render(); }
}

async function setPaid(orderId, paid, announce = true) {
  const key = `pay:${orderId}`;
  if (pending.has(key)) return;
  pending.add(key);
  render();
  try {
    updateOrder(await api(`/api/orders/${orderId}/status`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({status: paid ? "paid" : "unpaid"})
    }));
    if (announce) showUndo(paid ? "Замовлення оплачено" : "Оплату скасовано", () => setPaid(orderId, !paid, false));
  } catch (error) { await refresh(); throw error; }
  finally { pending.delete(key); render(); }
}

function orderCard(order, active) {
  const card = node("article", `station-card ${active ? "" : "issued-card"} ${order.paid ? "" : "unpaid-card"}`);
  const expanded = expandedOrders.has(order.id);
  card.classList.toggle("collapsed", !expanded);
  card.classList.add("interactive-card");
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  const open = () => {
    if (!expanded) expandedOrders.add(order.id);
    else if (station === "BAR" && !order.paid) window.dispatchEvent(new CustomEvent("bar-edit-order", {detail: order.id}));
    else expandedOrders.delete(order.id);
    render();
  };
  card.setAttribute("aria-label", expanded && station === "BAR" && !order.paid ? `Відкрити чек №${order.number} для редагування` : `${expanded ? "Згорнути" : "Розгорнути"} замовлення №${order.number}`);
  card.addEventListener("click", (event) => { if (!event.target.closest("button")) open(); });
  card.addEventListener("keydown", (event) => {
    if (event.target === card && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); }
  });
  const head = node("div", "station-card-head");
  const identity = node("div", "station-card-identity");
  const toggle = node("button", "collapse-button", expanded ? "▾" : "▸");
  toggle.type = "button";
  toggle.setAttribute("aria-label", `${expanded ? "Згорнути" : "Розгорнути"} замовлення №${order.number}`);
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    if (expandedOrders.has(order.id)) expandedOrders.delete(order.id);
    else expandedOrders.add(order.id);
    render();
  });
  identity.append(toggle);
  const summary = node("div", "station-card-description");
  summary.append(node("strong", "station-order-number", `№${order.number}`));
  const items = node("span", "station-card-summary");
  for (const [index, line] of order.lines.entries()) {
    if (index) items.append(document.createTextNode(", "));
    items.append(node("span", line.issued ? "summary-issued" : "summary-pending", `${line.item_name} х${line.quantity}`));
  }
  if (!order.lines.length) items.textContent = "Без позицій";
  summary.append(items);
  if (order.comment) summary.append(node("span", "station-order-comment", order.comment));
  identity.append(summary);
  head.append(identity);
  const actions = node("div", "station-card-actions");
  const unissuedHere = order.lines.filter((line) => line.station === station && !line.issued);
  if (unissuedHere.length) {
    const issueAll = node("button", "issue-button compact-action");
    issueAll.type = "button";
    issueAll.disabled = pending.has(`bulk:${order.id}`) || unissuedHere.some((line) => pending.has(`${order.id}:${line.id}`));
    issueAll.append(node("span", "action-symbol", "➜"), document.createTextNode(" Видати все"));
    issueAll.setAttribute("aria-label", `Видати всі невидані позиції ${station === "KITCHEN" ? "кухні" : "бару"} в замовленні №${order.number}`);
    issueAll.title = issueAll.getAttribute("aria-label");
    issueAll.addEventListener("click", (event) => { event.stopPropagation(); setIssuedBulk(order.id, unissuedHere.map((line) => line.id), true).catch(() => showNotice("Не вдалося видати позиції")); });
    actions.append(issueAll);
  }
  if (station === "BAR") {
    const pay = node("button", `${order.paid ? "reverse-button" : "pay-button"} compact-action`);
    pay.type = "button";
    pay.disabled = pending.has(`pay:${order.id}`) || (!order.paid && !order.lines.length);
    pay.append(node("span", "action-symbol", order.paid ? "↶" : "💵"), document.createTextNode(order.paid ? " Оплачено" : " Оплатити"));
    pay.setAttribute("aria-label", order.paid ? `Скасувати оплату замовлення №${order.number}` : `Оплатити замовлення №${order.number}`);
    pay.addEventListener("click", (event) => { event.stopPropagation(); setPaid(order.id, !order.paid).catch(() => showNotice("Не вдалося змінити оплату")); });
    actions.append(pay);
  }
  head.append(actions);
  card.append(head);
  for (const line of order.lines) {
    const ours = line.station === station;
    const row = node("div", `station-line ${ours ? "" : "other-station"} ${line.issued ? "line-issued" : ""}`);
    row.append(node("span", "station-line-name", `${line.quantity}× ${line.item_name}`));
    row.append(node("small", "line-station", line.station === "KITCHEN" ? "Кухня" : "Бар"));
    if (ours) {
      const button = node("button", line.issued ? "reverse-button" : "issue-button", line.issued ? "Повернути" : "Видати");
      button.type = "button";
      button.disabled = pending.has(`${order.id}:${line.id}`) || pending.has(`bulk:${order.id}`);
      button.setAttribute("aria-label", line.issued ? `Повернути ${line.item_name} в не видано` : `Видати ${line.item_name}`);
      button.addEventListener("click", (event) => { event.stopPropagation(); setIssued(order.id, line.id, !line.issued).catch(() => showNotice("Не вдалося змінити видачу")); });
      row.append(button);
    } else row.append(node("span", line.issued ? "issued-label" : "waiting-label", line.issued ? "✓ Видано" : "Очікує"));
    card.append(row);
  }
  if (station === "BAR") {
    const footer = node("div", "station-payment");
    footer.append(node("strong", "", money(order.total_cents)));
    card.append(footer);
  }
  return card;
}

function render() {
  const activeRoot = $("active-orders");
  const issuedRoot = $("issued-orders");
  activeRoot.replaceChildren();
  issuedRoot.replaceChildren();
  let activeCount = 0;
  let issuedCount = 0;
  const activeOrders = [];
  const completedOrders = [];
  for (const order of orders) {
    const stationLines = order.lines.filter((line) => line.station === station);
    if (!stationLines.length && station !== "BAR") continue;
    const active = stationLines.some((line) => !line.issued);
    (active ? activeOrders : completedOrders).push(order);
  }
  activeOrders.sort((a, b) => a.id - b.id);
  completedOrders.sort((a, b) => station === "BAR" ? Number(a.paid) - Number(b.paid) || a.id - b.id : a.id - b.id);
  for (const order of activeOrders) activeRoot.append(orderCard(order, true));
  for (const order of completedOrders) issuedRoot.append(orderCard(order, false));
  activeCount = activeOrders.length;
  issuedCount = completedOrders.length;
  $("active-count").textContent = String(activeCount);
  $("issued-count").textContent = String(issuedCount);
  if (!activeRoot.children.length) activeRoot.append(node("p", "empty-state", station === "KITCHEN" ? "Усі кухонні позиції виконано." : "Усі барні позиції видано."));
  if (!issuedRoot.children.length) issuedRoot.append(node("p", "empty-state", station === "KITCHEN" ? "Поки немає виконаних замовлень." : "Поки немає виданих замовлень."));
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    await eventContext.refresh();
    orders = (await api("/api/orders")).orders;
    render();
    window.dispatchEvent(new CustomEvent("orders-refreshed", {detail: orders}));
  } catch (_) { /* The next poll retries the current Event. */ }
  finally { refreshing = false; }
}

window.addEventListener("event-changed", (event) => {
  if (!event.detail.previous) return;
  orders = [];
  expandedOrders.clear();
  render();
  window.dispatchEvent(new CustomEvent("orders-refreshed", {detail: orders}));
  refresh();
});

$("station-title").textContent = station === "KITCHEN" ? "Кухня" : "Бар";
document.title = `${station === "KITCHEN" ? "Кухня" : "Бар"} · Pop-up orders`;
if (station === "KITCHEN") document.querySelector("#fulfillment-view h2").textContent = "Виконано";
if (station === "BAR") {
  $("bar-tabs").hidden = false;
  const switchBarTab = (view) => {
    const entry = view === "entry";
    $("bar-entry").hidden = !entry;
    $("fulfillment-view").hidden = entry;
    $("bar-tab-entry").classList.toggle("active", entry);
    $("bar-tab-fulfillment").classList.toggle("active", !entry);
    $("bar-tab-entry").setAttribute("aria-selected", String(entry));
    $("bar-tab-fulfillment").setAttribute("aria-selected", String(!entry));
  };
  $("bar-tab-entry").addEventListener("click", () => window.dispatchEvent(new Event("bar-new-order")));
  $("bar-tab-fulfillment").addEventListener("click", () => switchBarTab("fulfillment"));
  window.addEventListener("bar-show-entry", () => switchBarTab("entry"));
  switchBarTab("fulfillment");
}
eventContext.ready.then(refresh);
setInterval(refresh, 2500);
window.addEventListener("focus", refresh);
window.addEventListener("online", refresh);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
