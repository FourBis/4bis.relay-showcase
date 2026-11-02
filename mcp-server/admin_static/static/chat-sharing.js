import { apiRoot, escape } from "./api.js";
import { openModal, closeModal, formSection, field, toast } from "./ui.js";

const ID = "chat-sharing-modal";
let active = null;

export function sharingPermissionLabel(permission) {
  return permission === "collaborate" ? "Colaborar" : "Solo lectura";
}

export function sharingRecipients(recipients, query = "", selected = "") {
  const term = query.trim().toLocaleLowerCase();
  return recipients.filter(person => person.email === selected || !term
    || `${person.display_name || ""} ${person.email}`.toLocaleLowerCase().includes(term));
}

export function sharingPayload(person, permission) {
  if (!person?.email) throw new Error("Selecciona una persona de la lista de destinatarios elegibles.");
  if (!["read", "collaborate"].includes(permission)) throw new Error("Elige Solo lectura o Colaborar.");
  if (permission === "collaborate" && !person.can_collaborate)
    throw new Error("Esta persona puede recibir Solo lectura. Colaborar requiere permisos vigentes de escritura en el proyecto y la conversación.");
  return {permission};
}

function shell() {
  let modal = document.getElementById(ID);
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = ID; modal.className = "modal";
  modal.innerHTML = `<section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="${ID}-title">
    <header class="modal-header"><h3 id="${ID}-title">Compartir conversación</h3>
      <button type="button" class="icon-btn" data-sharing-close aria-label="Cerrar diálogo de acceso"><svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
    <div class="modal-body"><p id="${ID}-context" class="panel-sub"></p>
      <p class="panel-sub">Comparte con personas concretas que tengan acceso vigente al proyecto. Esto no concede permisos del proyecto ni acceso de administración. El acceso elegido no reduce los permisos que ya tenga un Admin.</p>
      <p id="${ID}-status" role="status" aria-live="polite"></p>
      <p id="${ID}-error" class="fail text-sm" role="alert" hidden></p>
      <form id="${ID}-form">${formSection({title:"Añadir o cambiar acceso",body:
        field({label:"Buscar persona",id:`${ID}-search`,control:`<input id="${ID}-search" class="input" type="search" autocomplete="off" placeholder="Nombre o correo">`})
        + field({label:"Persona",id:`${ID}-person`,control:`<select id="${ID}-person" class="select" required><option value="">Selecciona una persona</option></select>`})
        + field({label:"Acceso",id:`${ID}-permission`,control:`<select id="${ID}-permission" class="select"><option value="read">Solo lectura</option><option value="collaborate">Colaborar</option></select>`,hint:"Solo lectura permite consultar el hilo. Colaborar permite enviar mensajes con los permisos vigentes; no permite administrar ni volver a compartir."}),
        actions:`<button id="${ID}-save" class="btn btn-primary" type="submit" disabled>Guardar acceso</button>`})}</form>
      <section aria-labelledby="${ID}-people-title"><h4 id="${ID}-people-title">Personas con acceso compartido</h4>
        <p class="panel-sub">El acceso deja de estar disponible si se retira su permiso del proyecto.</p><div id="${ID}-people"></div></section>
    </div><footer class="modal-footer"><button class="btn" type="button" data-sharing-close>Cerrar</button><button id="${ID}-retry" class="btn" type="button" hidden>Reintentar consulta</button></footer>
  </section>`;
  document.body.append(modal);
  modal.querySelectorAll("[data-sharing-close]").forEach(button => button.onclick = closeSharing);
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !active || !modal.classList.contains("open")) return;
    event.preventDefault(); event.stopImmediatePropagation(); closeSharing();
  }, true);
  return modal;
}

function node(suffix) { return document.getElementById(`${ID}-${suffix}`); }
function current(session) { return active === session && session.modal.classList.contains("open"); }
function closeSharing() {
  const session = active;
  active = null;
  closeModal(ID);
  clearTimeout(session?.watchdog);
  if (session?.trigger?.isConnected) session.trigger.focus();
}

function error(session, message) {
  if (!current(session)) return;
  node("error").textContent = message;
  node("error").hidden = !message;
}

function paintChoices(session) {
  const select = node("person"), selected = select.value;
  select.innerHTML = '<option value="">Selecciona una persona</option>'
    + sharingRecipients(session.recipients, node("search").value, selected).map(person =>
      `<option value="${escape(person.email)}">${escape(person.display_name ? `${person.display_name} — ${person.email}` : person.email)}</option>`).join("");
  select.value = selected;
  const person = session.recipients.find(value => value.email === select.value);
  node("permission").querySelector('[value="collaborate"]').disabled = !person?.can_collaborate;
  if (!person?.can_collaborate) node("permission").value = "read";
  node("save").disabled = session.busy || !session.loaded || !person;
  node("save").textContent = session.busy ? (session.loaded ? "Guardando…" : "Consultando…")
    : session.shares.some(share => share.email === selected) ? "Actualizar acceso" : "Añadir acceso";
}

function paintShares(session) {
  node("people").innerHTML = session.shares.length ? session.shares.map(share =>
    `<div class="toolbar"><span><strong>${escape(share.display_name || share.email)}</strong>${share.display_name ? `<br><span class="muted">${escape(share.email)}</span>` : ""} · ${sharingPermissionLabel(share.permission)}</span>
      <button type="button" class="btn btn-xs danger" data-sharing-revoke="${escape(share.email)}" ${session.busy || !session.loaded ? "disabled" : ""}>Retirar acceso</button></div>`).join("")
    : '<p class="muted">Todavía no compartiste esta conversación con otra persona.</p>';
  node("people").querySelectorAll("[data-sharing-revoke]").forEach(button => {
    button.onclick = () => mutate(session, button.dataset.sharingRevoke, null, button);
  });
}

function applySnapshot(session, snapshot) {
  if (!snapshot || !Array.isArray(snapshot.shares) || !Array.isArray(snapshot.recipients)
      || snapshot.access?.can_share !== true)
    throw new Error("No se pudo confirmar el estado actual de los accesos. Cierra y vuelve a abrir el diálogo para consultarlo.");
  session.shares = snapshot.shares; session.recipients = snapshot.recipients; session.loaded = true;
  paintChoices(session); paintShares(session);
}

async function load(session) {
  if (!current(session) || session.busy) return;
  session.busy = true; node("retry").hidden = true;
  node("status").textContent = "Consultando accesos y personas elegibles…";
  error(session, ""); paintChoices(session);
  session.watchdog = setTimeout(() => {
    if (current(session)) node("status").textContent = "La consulta tarda más de lo habitual. Puedes cerrar el diálogo y volver a abrirlo.";
  }, 8000);
  try {
    const snapshot = await apiRoot(`/conversations/${encodeURIComponent(session.id)}/shares`);
    if (!current(session)) return;
    session.busy = false; applySnapshot(session, snapshot);
    node("status").textContent = session.recipients.length ? "" : "No hay personas elegibles. Revisa sus permisos del proyecto en Equipo.";
    if (document.activeElement === session.modal.querySelector("[data-sharing-close]")) node("search").focus();
  } catch (cause) {
    if (!current(session)) return;
    error(session, cause.message); node("status").textContent = ""; node("retry").hidden = false;
  } finally {
    clearTimeout(session.watchdog);
    if (current(session)) { session.busy = false; paintChoices(session); }
  }
}

async function mutate(session, email, permission, trigger) {
  if (!current(session) || session.busy || !session.loaded) return;
  let body;
  try {
    if (permission !== null) body = sharingPayload(session.recipients.find(person => person.email === email), permission);
  } catch (cause) { error(session, cause.message); return; }
  session.busy = true; error(session, "");
  const actionHadFocus = document.activeElement === trigger;
  node("status").textContent = permission === null ? "Retirando acceso…" : "Guardando acceso…";
  paintChoices(session);
  node("people").querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const snapshot = await apiRoot(`/conversations/${encodeURIComponent(session.id)}/shares/${encodeURIComponent(email)}`,
      permission === null ? {method:"DELETE"} : {method:"PUT",body:JSON.stringify(body)});
    if (!current(session)) return;
    session.busy = false; applySnapshot(session, snapshot);
    node("status").textContent = permission === null ? "Acceso retirado." : "Acceso guardado.";
    toast(node("status").textContent, "ok");
    if (actionHadFocus && (document.activeElement === document.body || document.activeElement === trigger))
      (trigger.isConnected ? trigger : node("person")).focus();
    session.onChanged?.();
  } catch (cause) {
    if (current(session)) {
      session.loaded = false;
      error(session, cause.message);
      node("status").textContent = "No se confirmó el cambio. Consulta los accesos antes de repetirlo.";
      node("retry").hidden = false;
    }
  } finally {
    if (current(session)) {
      session.busy = false; paintChoices(session);
      node("people").querySelectorAll("button").forEach(button => { button.disabled = !session.loaded; });
      if (actionHadFocus && document.activeElement === document.body)
        (!session.loaded ? node("retry") : trigger?.isConnected ? trigger : node("person")).focus();
    }
  }
}

export function openConversationSharing({conversationId, title = "", trigger = document.activeElement, onChanged} = {}) {
  if (!conversationId) return;
  if (active) closeSharing();
  const modal = shell();
  const session = active = {id:conversationId,modal,trigger,onChanged,shares:[],recipients:[],loaded:false,busy:false};
  node("context").textContent = title || "Esta conversación";
  node("search").value = ""; node("person").value = ""; node("permission").value = "read";
  node("people").replaceChildren();
  node("search").oninput = () => paintChoices(session);
  node("person").onchange = () => {
    const existing = session.shares.find(share => share.email === node("person").value);
    node("permission").value = existing?.permission || "read";
    paintChoices(session);
  };
  node("retry").onclick = () => load(session);
  node("form").onsubmit = event => {
    event.preventDefault();
    return mutate(session, node("person").value, node("permission").value, node("save"));
  };
  openModal(ID); modal.querySelector("[data-sharing-close]").focus();
  return load(session);
}
