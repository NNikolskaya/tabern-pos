"use strict";

// Bar uses the same order API as Reception. This panel owns only its local draft.
(() => {
  if (location.pathname !== "/bar") return;
  const $ = (id) => document.getElementById(id);
  const money = (cents) => `${(cents / 100).toFixed(2)} ₴`;
  const cart = new Map();
  let menu = [];
  let categories = [];
  let categoryId = null;
  let order = null;
  let comment = "";
  let dirty = false;
  let busy = false;
  let conflict = false;
  let blocked = false;
  let pendingId = null;
  let version = 0;
  let timer = null;
  let navigationVersion = 0;

  function node(tag, className, value) {
    const element = document.createElement(tag);
    element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  }

  const newKey = (itemId) => `new:${itemId}`;
  const activeLine = (itemId) => [...cart.values()].reverse().find((line) => line.menu_item_id === itemId && !line.issued);
  function lines() { return Array.from(cart.values(), (line) => ({menu_item_id: line.menu_item_id, quantity: line.quantity, ...(line.id ? {line_id: line.id} : {})})); }
  function sameLines(left, right) {
    const plain = (entries) => entries.map((line) => ({menu_item_id: line.menu_item_id, quantity: line.quantity}));
    return JSON.stringify(plain(left)) === JSON.stringify(plain(right));
  }
  function store() {
    sessionStorage.setItem("bar-draft", JSON.stringify({event_id: eventContext.id, orderId: order?.id ?? null, revision: order?.revision ?? null, pendingId, comment, cart: Array.from(cart)}));
  }
  function setFromOrder(saved) {
    order = saved;
    comment = saved.comment || "";
    cart.clear();
    for (const line of saved.lines) cart.set(`line:${line.id}`, {
      id: line.id, menu_item_id: line.menu_item_id, name: line.item_name, price_cents: line.price_cents, station: line.station, quantity: line.quantity, issued: line.issued, existing: true
    });
    dirty = false;
    conflict = false;
    blocked = false;
    pendingId = null;
    sessionStorage.removeItem("bar-draft");
    render();
  }
  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; flush(); }, delay);
  }
  function change() {
    version += 1;
    dirty = true;
    blocked = false;
    store();
    render();
    schedule(150);
  }
  function add(item) {
    if (conflict || order?.paid) return;
    const current = activeLine(item.id);
    if (current?.quantity >= 999) return;
    if (current) current.quantity += 1;
    else cart.set(newKey(item.id), {menu_item_id: item.id, name: item.name, price_cents: item.price_cents, station: item.station, quantity: 1, issued: false, existing: false});
    change();
  }
  function adjust(id, delta) {
    const line = cart.get(id);
    if (!line || line.issued || conflict || order?.paid) return;
    const quantity = line.quantity + delta;
    if (quantity < 1) cart.delete(id);
    else if (quantity <= 999) line.quantity = quantity;
    else return;
    change();
  }

  function renderMenu() {
    const root = $("bar-menu");
    root.replaceChildren();
    const currentCategory = categories.find((entry) => entry.id === categoryId);
    if (categoryId !== null && !currentCategory) categoryId = null;
    if (currentCategory) {
      const back = node("button", "menu-folder menu-back");
      back.type = "button";
      back.setAttribute("aria-label", "Назад до попереднього розділу");
      back.append(node("span", "menu-folder-icon", "←"), node("span", "menu-card-content", "Назад"));
      back.addEventListener("click", () => { categoryId = currentCategory.parent_id; renderMenu(); });
      root.append(back);
    }
    for (const category of categories.filter((entry) => entry.parent_id === categoryId)) {
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
      button.addEventListener("click", () => { categoryId = category.id; renderMenu(); });
      root.append(button);
    }
    for (const item of menu.filter((entry) => entry.category_id === categoryId)) {
      const button = node("button", "bar-menu-item");
      button.type = "button";
      button.disabled = conflict || !!order?.paid || (activeLine(item.id)?.quantity ?? 0) >= 999;
      button.title = `${item.name} · ${money(item.price_cents)}`;
      const picture = node("img", "menu-card-image");
      picture.src = item.has_image ? `/api/menu/${item.id}/image` : "/default-dish.svg";
      picture.alt = "";
      picture.loading = "lazy";
      picture.addEventListener("error", () => { if (!picture.src.endsWith("/default-dish.svg")) picture.src = "/default-dish.svg"; });
      const label = node("span", "menu-card-content");
      label.append(node("strong", "", item.name), node("small", "", money(item.price_cents)));
      button.append(picture, label);
      button.addEventListener("click", () => add(item));
      root.append(button);
    }
    if (!root.children.length) root.append(node("p", "empty-state", "У цьому розділі немає позицій."));
  }

  function render() {
    $("bar-check-title").textContent = order?.id ? `Чек №${order.id}${order.paid ? " · Оплачено" : ""}` : "Нове замовлення";
    const root = $("bar-cart-lines");
    root.replaceChildren();
    let total = 0;
    for (const [id, line] of cart) {
      total += line.quantity * line.price_cents;
      const row = node("div", "bar-cart-line");
      const label = node("span", "");
      label.append(node("strong", "", line.name), node("small", "", `${line.station === "KITCHEN" ? "Кухня" : "Бар"} · ${line.issued ? "Видано" : `${money(line.price_cents)} / шт.`}`));
      row.append(label);
      const actions = node("span", "bar-cart-actions");
      if (line.issued) actions.append(node("span", "issued-label", `${line.quantity} ×`));
      else {
        const controls = node("span", "bar-qty");
        const minus = node("button", "", "−");
        const plus = node("button", "", "+");
        minus.type = plus.type = "button";
        minus.disabled = plus.disabled = conflict || !!order?.paid;
        plus.disabled = plus.disabled || line.quantity >= 999;
        minus.addEventListener("click", () => adjust(id, -1));
        plus.addEventListener("click", () => adjust(id, 1));
        controls.append(minus, node("span", "", String(line.quantity)), plus);
        actions.append(controls);
      }
      if (line.id && line.station === "BAR") {
        const issue = node("button", line.issued ? "reverse-button" : "issue-button", line.issued ? "Повернути" : "Видати");
        issue.type = "button";
        issue.disabled = dirty || busy || conflict || blocked;
        issue.addEventListener("click", () => setIssued(order.id, line.id, !line.issued).catch(() => showNotice("Не вдалося змінити видачу")));
        actions.append(issue);
      }
      row.append(actions);
      root.append(row);
    }
    if (!cart.size) root.append(node("p", "empty-state", "Оберіть позицію з меню."));
    if ($("bar-comment").value !== comment) $("bar-comment").value = comment;
    $("bar-comment").disabled = conflict || !!order?.paid;
    $("bar-total").textContent = money(total);
    $("bar-save-state").textContent = conflict ? "Чек змінено на іншому пристрої." : blocked ? "Перевірте позиції в чеку." : dirty || busy ? "Синхронізація…" : order?.paid ? "Оплачений чек доступний лише для перегляду." : order ? "Усі зміни збережено" : "Перший вибір створить замовлення.";
    $("bar-save-state").classList.toggle("unsaved", dirty || conflict || blocked);
    $("bar-reload").classList.toggle("hidden", !conflict);
    $("bar-pay").classList.toggle("hidden", !order || !cart.size);
    $("bar-pay").textContent = order?.paid ? "Скасувати оплату" : "Оплатити";
    $("bar-pay").classList.toggle("reverse-button", !!order?.paid);
    $("bar-pay").classList.toggle("pay-button", !order?.paid);
    $("bar-pay").disabled = dirty || busy || conflict || blocked;
    renderMenu();
  }

  async function flush() {
    if (!dirty || busy || conflict || blocked) return;
    if (!order && !cart.size) { dirty = false; sessionStorage.removeItem("bar-draft"); render(); return; }
    busy = true;
    const sentVersion = version;
    const sentLines = lines();
    const sentComment = comment;
    render();
    try {
      let saved;
      if (!order) {
        pendingId ||= globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
        store();
        saved = await api("/api/orders", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({request_id: pendingId, lines: sentLines, comment: sentComment})});
      } else {
        saved = await api(`/api/orders/${order.id}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({expected_revision: order.revision, line_mode: "identified", require_unpaid: true, lines: sentLines, comment: sentComment})});
      }
      order = saved;
      pendingId = null;
      updateOrder(saved);
      if (sentVersion === version && sameLines(lines(), saved.lines) && comment === (saved.comment || "")) setFromOrder(saved);
      else {
        for (const line of saved.lines) {
          const local = cart.get(newKey(line.menu_item_id));
          if (local && !local.id && !line.issued) { local.id = line.id; local.existing = true; }
        }
        store();
      }
    } catch (error) {
      if (error.status === 409 && order) {
        try {
          const current = await api(`/api/orders/${order.id}`);
          order = current;
          if (sameLines(sentLines, current.lines) && sentComment === (current.comment || "") && !current.paid) {
            if (sentVersion === version && sameLines(lines(), current.lines) && comment === (current.comment || "")) setFromOrder(current);
            else store();
          } else conflict = true;
          updateOrder(current);
        } catch (_) { schedule(2500); }
      } else if (error.status === 400) blocked = true;
      else schedule(2500);
    } finally {
      busy = false;
      render();
      if (dirty && !conflict && !blocked && !timer) schedule(150);
    }
  }

  async function refreshMenu() {
    try {
      const [newMenu, newCategories] = await Promise.all([api("/api/menu"), api("/api/categories")]);
      if (JSON.stringify([menu, categories]) !== JSON.stringify([newMenu.items, newCategories.categories])) {
        menu = newMenu.items;
        categories = newCategories.categories;
        render();
      }
    } catch (_) { /* Connection indicator is shared with fulfillment. */ }
  }

  window.addEventListener("orders-refreshed", (event) => {
    const fresh = event.detail;
    const saved = pendingId && fresh.find((entry) => entry.request_id === pendingId);
    if (saved && !busy) {
      order = saved;
      pendingId = null;
      if (sameLines(lines(), saved.lines) && comment === (saved.comment || "")) setFromOrder(saved);
      else store();
    }
    const current = order && fresh.find((entry) => entry.id === order.id);
    if (current && current.revision !== order.revision && !busy) {
      if (!dirty) setFromOrder(current);
      else if (sameLines(lines(), current.lines) && comment === (current.comment || "") && !current.paid) {
        order = current;
        for (const line of current.lines) {
          const local = [...cart.values()].find((entry) => entry.id === line.id);
          if (local) local.issued = line.issued;
        }
        store();
      } else conflict = true;
      render();
    }
  });

  async function waitForSave() {
    for (let count = 0; count < 100; count += 1) {
      if (conflict || blocked) return false;
      if (!dirty && !busy) return true;
      if (dirty && !busy && !timer) await flush();
      else await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return false;
  }

  window.addEventListener("bar-new-order", async () => {
    const navigation = ++navigationVersion;
    if (!await waitForSave()) {
      if (navigation !== navigationVersion) return;
      showNotice("Дочекайтеся збереження поточного чека");
      window.dispatchEvent(new Event("bar-show-entry"));
      return;
    }
    if (navigation !== navigationVersion) return;
    order = null;
    comment = "";
    cart.clear();
    conflict = false;
    blocked = false;
    pendingId = null;
    sessionStorage.removeItem("bar-draft");
    render();
    window.dispatchEvent(new Event("bar-show-entry"));
  });

  window.addEventListener("bar-edit-order", async (event) => {
    const navigation = ++navigationVersion;
    if (!await waitForSave()) {
      if (navigation !== navigationVersion) return;
      showNotice("Дочекайтеся збереження поточного чека");
      window.dispatchEvent(new Event("bar-show-entry"));
      return;
    }
    if (navigation !== navigationVersion) return;
    try {
      const selected = await api(`/api/orders/${event.detail}`);
      if (navigation !== navigationVersion) return;
      if (selected.paid) { showNotice("Чек уже оплачено"); return; }
      setFromOrder(selected);
      window.dispatchEvent(new Event("bar-show-entry"));
    } catch (_) { /* The connection indicator remains visible. */ }
  });

  window.addEventListener("event-changed", (event) => {
    if (!event.detail.previous) return;
    navigationVersion += 1;
    clearTimeout(timer);
    timer = null;
    order = null;
    comment = "";
    cart.clear();
    menu = [];
    categories = [];
    categoryId = null;
    dirty = busy = conflict = blocked = false;
    pendingId = null;
    sessionStorage.removeItem("bar-draft");
    render();
    refreshMenu();
  });

  async function initialize() {
    await eventContext.ready;
    try {
      const saved = JSON.parse(sessionStorage.getItem("bar-draft") || "null");
      if (saved && (!saved.event_id || saved.event_id === eventContext.id)) {
        order = saved.orderId === null ? null : {id: saved.orderId, revision: saved.revision};
        comment = saved.comment || "";
        pendingId = saved.pendingId;
        for (const [id, line] of saved.cart) {
          const itemId = line.menu_item_id ?? Number(id);
          line.menu_item_id = itemId;
          cart.set(line.id ? `line:${line.id}` : newKey(itemId), line);
        }
        dirty = !!cart.size || !!order;
        version = 1;
      } else if (saved) sessionStorage.removeItem("bar-draft");
    } catch (_) { sessionStorage.removeItem("bar-draft"); }
    render();
    refreshMenu();
  }
  $("bar-pay").addEventListener("click", () => {
    if (!order || dirty || busy || conflict || blocked) return;
    setPaid(order.id, !order.paid).catch(() => showNotice("Не вдалося змінити оплату"));
  });
  $("bar-comment").addEventListener("input", (event) => {
    if (conflict || order?.paid) return;
    comment = event.target.value;
    if (!order && !cart.size) { store(); return; }
    change();
  });
  $("bar-reload").addEventListener("click", async () => {
    if (!order) return;
    try { setFromOrder(await api(`/api/orders/${order.id}`)); }
    catch (_) { /* Connection indicator remains visible. */ }
  });
  initialize();
  setInterval(refreshMenu, 2500);
  window.addEventListener("focus", refreshMenu);
  window.addEventListener("online", refreshMenu);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshMenu(); });
})();
