"use strict";

(() => {
  let current = null;
  let refreshing = null;
  const banner = document.getElementById("current-event");

  function show(event) {
    if (!banner) return;
    banner.textContent = event ? `Подія: ${event.name}${event.date ? ` · ${event.date}` : ""}` : "Подія недоступна";
    banner.title = event ? `Поточна подія: ${event.name}` : "Немає вибраної події";
  }

  async function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const response = await fetch("/api/context", {cache: "no-store"});
      if (!response.ok) throw new Error("Не вдалося завантажити поточну подію");
      const next = (await response.json()).event;
      if (!next || !Number.isInteger(next.id)) throw new Error("Поточна подія недоступна");
      const previous = current;
      current = next;
      show(next);
      if (previous?.id !== next.id) window.dispatchEvent(new CustomEvent("event-changed", {detail: {previous, current: next}}));
      return next;
    })();
    try { return await refreshing; }
    catch (error) { if (!current) show(null); throw error; }
    finally { refreshing = null; }
  }

  const scoped = (path) => /^\/api\/(menu|categories|orders)(\/|\?|$)/.test(path);
  function request(path, options = {}) {
    if (!scoped(path)) return {options, eventId: null};
    if (!current) throw new Error("Дочекайтеся завантаження поточної події.");
    const eventId = current.id;
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") headers.set("X-Event-Id", String(eventId));
    return {options: {...options, headers}, eventId};
  }

  function verify(path, eventId, body) {
    if (eventId === null) return;
    if (current?.id !== eventId || (body && "event_id" in body && body.event_id !== eventId)) {
      refresh().catch(() => {});
      throw new Error("Поточну подію змінено. Дані оновлюються.");
    }
  }

  const ready = refresh().catch(() => null);
  window.eventContext = {get id() { return current?.id ?? null; }, get event() { return current; }, ready, refresh, request, verify};
  setInterval(() => refresh().catch(() => {}), 2500);
  window.addEventListener("focus", () => refresh().catch(() => {}));
  window.addEventListener("online", () => refresh().catch(() => {}));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh().catch(() => {}); });
})();
