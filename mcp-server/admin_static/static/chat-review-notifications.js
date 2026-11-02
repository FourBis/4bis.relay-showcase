// Bandeja durable de Relay para la identidad conectada; no promete entrega por Discord.
import { api, escape } from "./api.js";
import { modalLoad, closeModal, isModalOpen, emptyState } from "./ui.js";
import { dataTable } from "./ui-table.js";
import { openConversationWindow } from "./workspace.js";

const MODAL = "external-pr-modal";
let session = 0;

export function reviewNotificationState(note) {
  return ({queued:"En cola",running:"En revisión",reviewed:"Informe disponible",blocked:"Bloqueada",
    failed:"Con error",superseded:"Histórica",created:"Sin leer",read:"Leído"})[note.state] || note.state || "Aviso";
}

export function reviewNotificationActions(note) {
  const id = escape(String(note.id || ""));
  return `<div class="cell-actions">${/^[A-Za-z0-9_-]{1,160}$/.test(note.conversation_id || "")
    ? `<button type="button" class="btn btn-xs" data-review-notification-open="${id}">Abrir conversación</button>` : ""}
    ${note.read_at || note.read ? '<span class="badge dim">Leído</span>' : note.can_mark_read === false
      ? '<span class="badge dim">Sin leer</span>' : `<button type="button" class="btn btn-xs" data-review-notification-read="${id}">Marcar leído</button>`}</div>`;
}

export async function openReviewNotifications({ projectSlug = "", onOpen } = {}) {
  const current = ++session;
  let notes = [];
  await modalLoad({id:MODAL,title:"Avisos de revisiones",loader:async () => {
    try { return await api("external-review/notifications"); }
    catch (error) { return {error:/[<>]/.test(error.message || "") ? "No se recibió una respuesta válida del servicio." : error.message}; }
  },render:data => {
    if (data.error) return `${emptyState({title:"No se pudieron cargar los avisos",sub:escape(data.error)})}<button type="button" class="btn" data-review-notifications-refresh>Reintentar</button>`;
    notes = (Array.isArray(data.notifications) ? data.notifications : [])
      .filter(note => !projectSlug || note.project_slug === projectSlug);
    return `<p class="chat-task-detail">Avisos guardados en Relay para tu cuenta${projectSlug ? ` en el proyecto ${escape(projectSlug)}` : ""}. La aprobación humana sigue siendo necesaria.</p>
      <button type="button" class="btn btn-xs" data-review-notifications-refresh>Actualizar avisos</button>
      ${data.has_more ? '<p class="chat-task-detail">Se muestran los avisos más recientes disponibles.</p>' : ""}<div data-review-notifications-table></div>`;
  },after:() => {
    const body = document.getElementById(`${MODAL}-body`);
    const active = () => current === session && isModalOpen(MODAL) && body.isConnected && mount?.isConnected;
    body.querySelector("[data-review-notifications-refresh]")?.addEventListener("click",() => openReviewNotifications({projectSlug,onOpen}));
    const mount = body.querySelector("[data-review-notifications-table]");
    if (!mount) return;
    const table = dataTable(mount,{rows:notes,pageSize:15,searchPlaceholder:"Buscar avisos de revisiones",
      sort:{key:0,dir:"desc"},empty:{title:"No hay avisos de revisiones",sub:"Los avisos aparecerán aquí cuando Relay guarde el resultado de una revisión para tu cuenta."},
      columns:[
        {key:"created_at",label:"Fecha"},
        {key:"project_slug",label:"Proyecto / SHA",render:note => `${escape(note.project_slug || "Sin proyecto")}<br><code>${escape((note.head_sha || "").slice(0,12))}</code>`},
        {key:"summary",label:"Aviso",render:note => `<span class="badge dim">${escape(reviewNotificationState(note))}</span><p>${escape((note.summary || "").slice(0,1500))}</p>`},
        {key:"id",label:"Acciones",sortable:false,render:note => onOpen
          ? reviewNotificationActions(note).replace("Abrir conversación","Ver registro") : reviewNotificationActions(note)},
      ]});
    mount.addEventListener("click",async event => {
      const button = event.target.closest?.("[data-review-notification-open],[data-review-notification-read]");
      if (!button || button.disabled || !active()) return;
      const id = button.dataset.reviewNotificationOpen || button.dataset.reviewNotificationRead;
      const note = notes.find(item => String(item.id) === id);
      if (!note) return;
      if (button.dataset.reviewNotificationOpen) {
        closeModal(MODAL);
        if (onOpen) onOpen(note);
        else openConversationWindow({id:note.conversation_id,project_slug:note.project_slug});
        return;
      }
      button.disabled = true;
      try {
        const response = await api(`external-review/notifications/${encodeURIComponent(id)}/read`,{method:"POST",body:{}});
        if (!active()) return;
        if (response.ok === false) throw new Error("No se pudo confirmar la lectura del aviso.");
        notes = notes.map(item => String(item.id) === id ? {...item,...response.notification,read:true} : item);
        table.setRows(notes);
        [...mount.querySelectorAll("[data-review-notification-open]")].find(item => item.dataset.reviewNotificationOpen === id)?.focus();
      } catch (error) {
        if (!active()) return;
        document.getElementById(`${MODAL}-msg`).textContent = `No se pudo marcar el aviso como leído: ${/[<>]/.test(error.message || "") ? "respuesta no válida" : error.message}`;
      } finally { if (active() && button.isConnected) button.disabled = false; }
    });
  }});
}

export function installReviewNotificationsButton() {
  const register = document.getElementById("chat-external-pr");
  if (!register) return null;
  let button = document.getElementById("chat-review-notifications");
  if (!button) {
    button = document.createElement("button"); button.id = "chat-review-notifications";
    button.type = "button"; button.className = "btn btn-xs mt-2"; button.textContent = "Avisos de revisiones";
    register.after(button);
  }
  button.onclick = openReviewNotifications;
  return button;
}
