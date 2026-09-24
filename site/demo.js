"use strict";
// ponytail: fixed, local scenarios. A real Relay connection belongs in
// the installed application, never in this public simulation.
let english = new URLSearchParams(location.search).get("lang") === "en";
const $ = selector => document.querySelector(selector);
const tr = (es, en) => english ? en : es;
const initialChats = () => ({
  docs: {name: null, draft: "", messages: [], open: true},
  access: {name: null, draft: "", messages: [], open: false},
});
let chats = initialChats(), active = "docs", turns = 1, feedback = false, split = false, continued = false, published = false, githubConnected = false, writeEnabled = false, alexProjects = new Set(), renaming;
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
  $("#resume-result").textContent = turns > 1 ? tr("Misma rama y workspace ficticios; el plan sigue visible.", "Same fictional branch and workspace; the plan stays visible.") : "";
  $("#review-step").classList.toggle("reviewed", feedback);
  $("#review-copy").textContent = published ? tr("PR de ejemplo publicada; no se hizo merge ni integración.", "Example PR published; no merge or integration took place.") : tr("Sin publicar. No se simula merge ni integración.", "Not published. No merge or integration is simulated.");
  $("#feedback-result").textContent = feedback ? tr("Feedback ficticio recibido; no se creó ni modificó ninguna PR real.", "Fictional feedback received; no real PR was created or changed.") : "";
  $("#feedback").disabled = !published || feedback;
  $("#split-task").disabled = split;
  $("#split-result").textContent = split ? tr("Presupuesto agotado: el padre se sustituyó una vez por cuatro subtareas; la prueba dependiente espera a las cuatro.", "Budget exhausted: the parent was replaced once by four subtasks; the dependent check waits for all four.") : "";
  $("#subtask-count").textContent = split ? "4" : "0";
  $("#graph-before").hidden = split;
  $("#graph-after").hidden = !split;
  const children = $("#task-children");
  children.replaceChildren();
  if (split) {
    for (const [name, status] of [[tr("Migrar base de navegación", "Migrate navigation shell"), tr("Completada", "Completed")], [tr("Integrar rutas nuevas", "Integrate new routes"), tr("En curso", "In progress")], [tr("Ajustar componentes", "Update components"), tr("Pendiente", "Pending")], [tr("Actualizar pruebas visuales", "Update visual checks"), tr("Pendiente", "Pending")]]) {
      const item = node("li"); item.append(node("strong", name), node("span", status)); children.append(item);
    }
  }
  const projectNames = {web: tr("Migración del cliente web", "Web client migration"), shared: tr("Componentes compartidos", "Shared components"), docs: tr("Documentación", "Documentation")};
  $("#alex-projects").textContent = alexProjects.size ? [...alexProjects].map(id => projectNames[id]).join(", ") : tr("Ninguno", "None");
  $("#allow-write").disabled = !alexProjects.has("web") || !githubConnected || writeEnabled;
  $("#connect-github").disabled = githubConnected;
  $("#continue-task").disabled = !writeEnabled || continued;
  $("#assignment-state").textContent = alexProjects.has("web") ? tr("Proyecto web asignado", "Web project assigned") : tr("Solo lectura", "Read-only");
  $("#repo-state").textContent = githubConnected ? tr("Cuenta personal conectada (simulación)", "Personal account linked (simulation)") : tr("No conectada", "Not connected");
  $("#write-state").textContent = writeEnabled ? tr("Habilitado explícitamente", "Explicitly enabled") : tr("Deshabilitado", "Disabled");
  $("#execution-state").textContent = continued ? tr("Continuada explícitamente", "Explicitly continued") : tr("En pausa", "Paused");
  $("#team-result").textContent = continued ? tr("Alex continuó la tarea explícitamente. No hubo ejecución automática.", "Alex explicitly continued the task. No autorun took place.") : "";
  $("#assignment-result").textContent = alexProjects.size ? tr("Cambios guardados para Alex.", "Changes saved for Alex.") : "";
  $("#github-result").textContent = githubConnected ? tr("Cuenta personal de Alex conectada en esta simulación.", "Alex's personal account is linked in this simulation.") : "";
  $("#publish-task").disabled = !continued || published;
  $("#publish-result").textContent = published ? tr("Admin Ana publicó la PR ficticia. Sigue sin merge ni integración.", "Admin Ana published the fictional PR. It remains unmerged and not integrated.") : "";
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
  document.title = tr("FourBis Relay — Tareas largas que se adaptan", "FourBis Relay — Long tasks that adapt");
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
$("#split-task").addEventListener("click", () => { split = true; renderTask(); });
$("#edit-alex").addEventListener("click", () => { $("#edit-project-panel").hidden = false; for (const option of $("#project-picker").options) option.selected = alexProjects.has(option.value); $("#project-picker").focus(); });
$("#save-projects").addEventListener("click", () => { alexProjects = new Set([...$("#project-picker").selectedOptions].map(option => option.value)); if (!alexProjects.has("web")) { writeEnabled = false; continued = false; } $("#edit-project-panel").hidden = true; renderTask(); });
$("#connect-github").addEventListener("click", () => { githubConnected = true; renderTask(); });
$("#allow-write").addEventListener("click", () => { if (alexProjects.has("web") && githubConnected) writeEnabled = true; renderTask(); });
$("#continue-task").addEventListener("click", () => { continued = true; renderTask(); });
$("#publish-task").addEventListener("click", () => { published = true; renderTask(); });
$("#feedback").addEventListener("click", () => { feedback = true; renderTask(); });
$("#reset").addEventListener("click", () => {
  chats = initialChats(); active = "docs"; turns = 1; feedback = false; split = false; continued = false; published = false; githubConnected = false; writeEnabled = false; alexProjects = new Set();
  $("#edit-project-panel").hidden = true; for (const option of $("#project-picker").options) option.selected = false;
  renderChats(); renderTask(); selectScene("chats");
});
translate();
