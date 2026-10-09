"use strict";

(() => {
  let timer;
  let generation = 0;
  const bar = document.createElement("div");
  bar.className = "undo-snackbar hidden";
  bar.setAttribute("role", "status");
  bar.setAttribute("aria-live", "polite");
  const message = document.createElement("span");
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Скасувати";
  bar.append(message, button);
  document.body.append(bar);

  window.showUndo = (label, undo) => {
    const current = ++generation;
    clearTimeout(timer);
    message.textContent = label;
    button.disabled = false;
    button.classList.remove("hidden");
    bar.classList.remove("hidden");
    button.onclick = async () => {
      button.disabled = true;
      try {
        await undo();
        if (current === generation) bar.classList.add("hidden");
      } catch (_) {
        if (current === generation) {
          message.textContent = "Не вдалося скасувати. Спробуйте ще раз.";
          button.disabled = false;
        }
      }
    };
    timer = setTimeout(() => { if (current === generation) bar.classList.add("hidden"); }, 7000);
  };

  window.showNotice = (label) => {
    const current = ++generation;
    clearTimeout(timer);
    message.textContent = label;
    button.classList.add("hidden");
    bar.classList.remove("hidden");
    timer = setTimeout(() => { if (current === generation) bar.classList.add("hidden"); }, 5000);
  };
})();
