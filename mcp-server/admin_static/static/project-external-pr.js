// Trabajo del equipo en el repositorio del proyecto; usa el registro y las acciones existentes.
import { apiRoot, escape } from "./api.js";
import { openSidePanel, setSidePanelBody, isSidePanelCurrent, emptyState } from "./ui.js";
import { openExternalRegistration, externalStateLabel } from "./chat-external-pr.js";
import { mountTaskPanel } from "./chat-task.js";
import { openReviewNotifications } from "./chat-review-notifications.js";

let detailPanel = null;
const attr = value => escape(String(value ?? "")).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const safeError = error => /[<>]/.test(error?.message || "") ? "El servicio no devolvió una respuesta válida." : String(error?.message || error || "Error de lectura").slice(0,500);

function stopDetail() { detailPanel?.destroy(); detailPanel = null; }

export function projectPRListHtml({ tasks = [], canRegister = false, nextCursor = "", loading = false, error = "" } = {}) {
  const records = tasks.map(task => {
    const pr = task.external_pr || {};
    return `<li class="py-2"><button type="button" class="btn btn-xs" data-project-pr-record="${attr(task.id)}">Ver PR #${escape(String(pr.number || ""))}</button>
      <strong>${escape(task.title || pr.title || "PR registrado")}</strong>
      <p class="chat-task-detail">${escape(pr.repo || "")} · ${escape(pr.state || "Sin verificar")}
        · ${externalStateLabel(task.assessment?.state)}<br>SHA observado: <code class="break-all">${escape(pr.head_sha || "Sin verificar")}</code></p></li>`;
  }).join("");
  return `<p class="chat-task-detail">Registra el trabajo del equipo realizado con sus propias herramientas sobre el mismo repositorio vinculado a este proyecto. No necesitas iniciar un chat. No se integran forks ni otros repositorios.</p>
    <div class="toolbar"><button type="button" class="btn btn-primary" data-project-pr-register ${canRegister && !loading ? "" : "disabled"}>Registrar PR</button>
      <button type="button" class="btn btn-xs" data-project-pr-refresh ${loading ? "disabled" : ""}>Actualizar registros</button>
      <button type="button" class="btn btn-xs" data-project-pr-notices>Avisos de revisiones</button></div>
    ${!canRegister && !loading ? '<p class="chat-task-detail">El registro requiere acceso de escritura y un repositorio GitHub vinculado al proyecto.</p>' : ""}
    ${error ? `<p class="chat-task-error" role="alert">No se pudieron leer los registros: ${escape(error)}</p>` : ""}
    ${loading ? '<p class="chat-task-detail" role="status">Leyendo registros…</p>' : ""}
    ${records ? `<ul aria-label="PR registrados del proyecto">${records}</ul>` : !loading && !error ? emptyState({title:"Sin PR registrados",sub:"Registra un PR del repositorio de este proyecto para seguir su revisión y evidencia."}) : ""}
    ${nextCursor ? `<button type="button" class="btn btn-xs" data-project-pr-more ${loading ? "disabled" : ""}>Más registros</button>` : ""}
    <p class="chat-task-detail">Esta lista muestra registros de Relay; no consulta GitHub automáticamente. La revisión automática requiere la política y un modelo existentes configurados. Los comentarios y avisos conservan su estado verificable. El merge y el despliegue requieren decisión humana.</p>`;
}

export async function openProjectPRs(projectSlug) {
  stopDetail();
  const generation = openSidePanel(`PR del proyecto · ${projectSlug}`);
  setSidePanelBody('<section class="p-4" data-project-pr-list></section>');
  const body = document.querySelector("[data-project-pr-list]");
  if (!body) return;
  let tasks = [], cursor = "", canRegister = false, loading = false, error = "";
  const active = () => isSidePanelCurrent(generation) && body.isConnected;
  const paint = () => { if (active()) body.innerHTML = projectPRListHtml({tasks,canRegister,nextCursor:cursor,loading,error}); };
  const load = async (reset = false) => {
    if (!active() || loading) return;
    if (reset) { tasks = []; cursor = ""; }
    loading = true; error = ""; paint();
    try {
      const result = await apiRoot(`/projects/${encodeURIComponent(projectSlug)}/external-pr${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (!active()) return;
      const rows = (Array.isArray(result.tasks) ? result.tasks : []).filter(task => task.project_slug === projectSlug && task.mode === "external_pr");
      tasks = [...new Map([...tasks,...rows].map(task => [task.id,task])).values()];
      canRegister = result.can_register === true;
      cursor = typeof result.next_cursor === "string" ? result.next_cursor : "";
    } catch (failure) { if (active()) error = safeError(failure); }
    finally { loading = false; paint(); }
  };
  body.addEventListener("click",event => {
    if (!active()) return;
    const target = event.target.closest?.("button");
    if (!target || target.disabled) return;
    if (target.matches("[data-project-pr-register]") && canRegister) {
      openExternalRegistration({projectSlug,lockProject:true,onRegistered:task => {
        if (active() && task?.project_slug === projectSlug) openProjectPRRecord(projectSlug,task.id);
      }});
    } else if (target.matches("[data-project-pr-refresh]")) load(true);
    else if (target.matches("[data-project-pr-more]")) load();
    else if (target.matches("[data-project-pr-notices]")) openReviewNotifications({projectSlug,onOpen:note => {
      if (active() && note.project_slug === projectSlug) openProjectPRRecord(projectSlug,note.conversation_id);
    }});
    else if (target.dataset.projectPrRecord && tasks.some(task => task.id === target.dataset.projectPrRecord)) {
      openProjectPRRecord(projectSlug,target.dataset.projectPrRecord);
    }
  });
  await load();
}

export function openProjectPRRecord(projectSlug, recordId) {
  stopDetail();
  const generation = openSidePanel(`PR del proyecto · ${projectSlug}`);
  setSidePanelBody(`<section class="p-4"><button type="button" class="btn btn-xs" data-project-pr-back>Volver a los PR del proyecto</button>
    <p class="chat-task-detail">Registro de trabajo y revisión del repositorio del proyecto. No es necesario usar el chat.</p>
    <div class="chat-task-panel chat-external-panel" data-project-pr-detail></div></section>`);
  const panel = document.querySelector("[data-project-pr-detail]");
  document.querySelector("[data-project-pr-back]")?.addEventListener("click",() => openProjectPRs(projectSlug));
  detailPanel = mountTaskPanel({convId:recordId,projectSlug,panelElement:panel,pollTabId:"projects",
    isCurrent:() => isSidePanelCurrent(generation) && !!panel?.isConnected});
}
