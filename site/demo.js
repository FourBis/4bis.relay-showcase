"use strict";
// ponytail: three fixed, local scenarios. A real Relay connection belongs in
// the installed application, never in this public simulation.
let english = new URLSearchParams(location.search).get("lang") === "en";
const $ = selector => document.querySelector(selector);
const tr = (es, en) => english ? en : es;
const initialChats = () => ({
  docs: {name: null, draft: "", messages: [], open: true},
  access: {name: null, draft: "", messages: [], open: false},
});
let chats = initialChats(), active = "docs", turns = 1, feedback = false, renaming;
const chatLabel = id => chats[id].name || (id === "docs" ? tr("Documentación", "Documentation") : tr("Revisar accesos", "Access review"));
const windows = $("#chat-windows");
const dialog = $("#rename-dialog");
document.querySelectorAll("[data-en]").forEach(el => { el.dataset.es = el.textContent; });
const translatedAttributes = ["alt", "aria-label"];
for (const attribute of translatedAttributes) {
  document.querySelectorAll(`[data-en-${attribute}]`).forEach(el => {
    el.setAttribute(`data-es-${attribute}`, el.getAttribute(attribute));
  });
}

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function renderChats(focus = false) {
  windows.replaceChildren();
  for (const [id, chat] of Object.entries(chats)) {
    if (!chat.open) continue;
    const card = node("article", null, `chat-window${active === id ? " active" : ""}`);
    card.dataset.chat = id;
    const header = node("div", null, "window-heading");
    header.append(node("h3", chatLabel(id)));
    const rename = node("button", "✎");
    rename.setAttribute("aria-label", tr("Renombrar ventana", "Rename window") + `: ${chatLabel(id)}`);
    rename.addEventListener("click", () => {
      renaming = id;
      $("#window-name").value = chatLabel(id);
      dialog.returnValue = "";
      dialog.showModal();
      $("#window-name").focus();
      $("#window-name").select();
    });
    const close = node("button", "×");
    close.setAttribute("aria-label", tr("Cerrar ventana", "Close window") + `: ${chatLabel(id)}`);
    close.addEventListener("click", () => {
      chat.open = false;
      active = Object.keys(chats).find(key => chats[key].open) || id;
      renderChats();
      document.querySelector(`[data-open="${id}"]`).focus();
    });
    header.append(rename, close);
    const messages = node("div", null, "chat-messages");
    messages.setAttribute("role", "log");
    messages.setAttribute("aria-label", tr("Mensajes", "Messages") + `: ${chatLabel(id)}`);
    const welcome = id === "docs"
      ? tr("La guía ya tiene un plan. Puedes continuar esta conversación mientras revisas otra tarea.", "The guide already has a plan. You can continue this conversation while reviewing another task.")
      : tr("Este chat conserva su propio borrador. Cambiar de ventana no mezcla las conversaciones.", "This chat keeps its own draft. Switching windows does not mix conversations.");
    for (const item of [{text: welcome, author: "relay"}, ...chat.messages]) {
      const bubble = node("p", null, `message${item.author === "user" ? " user" : ""}`);
      bubble.append(node("span", item.author === "user" ? tr("Tú", "You") : tr("Relay · ejemplo", "Relay · example"), "message-author"));
      bubble.append(document.createTextNode(item.text));
      messages.append(bubble);
    }
    const form = node("form", null, "composer");
    const input = node("textarea");
    input.value = chat.draft;
    input.maxLength = 600;
    input.rows = 2;
    input.placeholder = tr("Deja un borrador aquí…", "Keep a draft here…");
    input.setAttribute("aria-label", tr("Borrador", "Draft") + `: ${chatLabel(id)}`);
    input.addEventListener("input", () => { chat.draft = input.value; });
    input.addEventListener("focus", () => {
      active = id;
      windows.querySelectorAll(".chat-window").forEach(el => el.classList.toggle("active", el.dataset.chat === id));
    });
    const send = node("button", tr("Probar", "Try"));
    send.type = "submit";
    form.append(input, send);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!chat.draft.trim()) { input.focus(); return; }
      chat.messages.push({text: chat.draft.trim(), author: "user"}, {author: "relay", text: tr(
        "Respuesta de ejemplo: el mensaje quedó en esta ventana. En Relay instalado, aquí respondería el proveedor configurado.",
        "Example reply: the message stays in this window. In an installed Relay, your configured provider would respond here.")});
      chat.draft = "";
      active = id;
      renderChats(true);
    });
    card.append(header, messages, form);
    windows.append(card);
    messages.scrollTop = messages.scrollHeight;
  }
  if (!windows.childElementCount) windows.append(node("p", tr("Elige una conversación para recuperar su ventana y borrador.", "Choose a conversation to recover its window and draft."), "scene-note"));
  if (focus) windows.querySelector(`[data-chat="${active}"] textarea`)?.focus();
}

function renderTask() {
  $("#turn-count").textContent = turns;
  $("#resume-result").textContent = turns > 1 ? tr("Misma rama, mismo workspace. El siguiente turno continúa el historial de ejemplo.", "Same branch, same workspace. The next turn continues the example history.") : "";
  $("#review-step").classList.toggle("reviewed", feedback);
  $("#review-copy").textContent = feedback ? tr("Feedback aplicado en la misma PR #17. Nueva validación pendiente.", "Feedback applied to the same PR #17. New validation pending.") : tr("La PR #17 conserva su identidad.", "PR #17 keeps its identity.");
  $("#feedback-result").textContent = feedback ? tr("Simulación completada. No se creó ni modificó ninguna PR real.", "Simulation complete. No real PR was created or changed.") : "";
  $("#feedback").disabled = feedback;
}

function translate() {
  document.documentElement.lang = english ? "en" : "es";
  document.querySelectorAll("[data-en]").forEach(el => { el.textContent = el.dataset[english ? "en" : "es"]; });
  for (const attribute of translatedAttributes) {
    document.querySelectorAll(`[data-en-${attribute}]`).forEach(el => {
      el.setAttribute(attribute, el.getAttribute(`data-${english ? "en" : "es"}-${attribute}`));
    });
  }
  $("#language").textContent = english ? "ES" : "EN";
  $("#language").setAttribute("aria-label", english ? "Cambiar a español" : "Switch to English");
  document.title = tr("FourBis Relay — Conversaciones que conservan el trabajo", "FourBis Relay — Conversations that keep the work");
  renderChats(); renderTask();
}

$("#language").addEventListener("click", () => {
  english = !english;
  const url = new URL(location.href);
  english ? url.searchParams.set("lang", "en") : url.searchParams.delete("lang");
  history.replaceState(null, "", url);
  translate();
});
document.querySelectorAll("[data-open]").forEach(button => button.addEventListener("click", () => {
  active = button.dataset.open;
  chats[active].open = true;
  renderChats(true);
}));
dialog.addEventListener("close", () => {
  if (dialog.returnValue === "save" && $("#window-name").value.trim()) {
    chats[renaming].name = $("#window-name").value.trim();
    renderChats();
    windows.querySelector(`[data-chat="${renaming}"] .window-heading button`)?.focus();
  }
});
function selectScene(name, focus = false) {
  document.querySelectorAll("[data-scene]").forEach(button => {
    const selected = button.dataset.scene === name;
    button.setAttribute("aria-selected", selected);
    button.tabIndex = selected ? 0 : -1;
    document.getElementById(button.getAttribute("aria-controls")).hidden = !selected;
    if (selected && focus) button.focus();
  });
}
const tabs = [...document.querySelectorAll("[data-scene]")];
tabs.forEach((button, index) => {
  button.addEventListener("click", () => selectScene(button.dataset.scene));
  button.addEventListener("keydown", event => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    selectScene(tabs[next].dataset.scene, true);
  });
});
$("#resume").addEventListener("click", () => { turns++; renderTask(); });
$("#feedback").addEventListener("click", () => { feedback = true; renderTask(); });
$("#reset").addEventListener("click", () => {
  chats = initialChats(); active = "docs"; turns = 1; feedback = false;
  renderChats(); renderTask(); selectScene("chats");
});
translate();
