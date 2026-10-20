// Vínculo común de issue para trabajo de Relay y PR externos.
import { apiRoot, escape } from "./api.js";
import { field, formSection, modalLoad, closeModal, isModalOpen, toast, confirmModal } from "./ui.js";

const MODAL = "issue-link-modal";
let modalSession = 0;
const attr = value => escape(String(value ?? "")).replaceAll('"', "&quot;").replaceAll("'", "&#39;");

export function parseIssueInput(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^#?\d+$/.test(text)) {
    const number = Number(text.replace(/^#/, ""));
    if (Number.isSafeInteger(number) && number > 0 && number <= 2147483647) return number;
  }
  try {
    const url = new URL(text);
    if (url.protocol === "https:" && url.hostname === "github.com" && !url.port
        && !url.username && !url.password && !url.search && !url.hash
        && /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/[1-9]\d*\/?$/.test(url.pathname)) {
      const number = Number(url.pathname.replace(/\/$/, "").split("/").pop());
      if (Number.isSafeInteger(number) && number <= 2147483647) return `https://github.com${url.pathname.replace(/\/$/, "")}`;
    }
  } catch (_) { /* El mensaje de abajo también cubre URL inválida. */ }
  throw new Error("Ingresa un número de issue positivo o su URL canónica de GitHub. El servidor verificará el repositorio y el acceso.");
}

export function optionalIssuePayload(value) {
  const issue = parseIssueInput(value);
  return issue === null ? {} : { issue };
}

export function issueLinkPayload(value, resolutionComplete = false) {
  const issue = parseIssueInput(value);
  if (issue === null) throw new Error("Selecciona la issue que corresponde a este trabajo.");
  return { issue, resolution_complete: resolutionComplete === true };
}

export function issuePickerHtml({ id, value = "", required = false } = {}) {
  return field({ label:required ? "Issue de GitHub" : "Issue de GitHub (opcional)", id, required,
    control:`<input id="${attr(id)}" name="issue" class="input" list="${attr(id)}-options" value="${attr(value)}" ${required ? "required" : ""} autocomplete="off" placeholder="Número o https://github.com/org/repo/issues/123">
      <datalist id="${attr(id)}-options"></datalist>
      <p class="field-hint" data-issue-picker-status role="status">Cargando issues del proyecto…</p>
      <button type="button" class="btn btn-xs" data-issue-picker-retry>Actualizar lista</button>`,
    hint:"Elige de la lista o pega un número o URL del mismo repositorio. Se verifica en GitHub al guardar; el título de la tarea sigue siendo una descripción libre." });
}

export function mountIssuePicker(root, { id, projectSlug = "", onChange } = {}) {
  const input = root?.querySelector(`#${id}`);
  const list = root?.querySelector(`#${id}-options`);
  const status = input?.parentElement?.querySelector("[data-issue-picker-status]");
  const retry = input?.parentElement?.querySelector("[data-issue-picker-retry]");
  if (!input || !list) return { setProject() {}, destroy() {} };
  let project = projectSlug, version = 0, disposed = false;
  const changed = () => onChange?.(input.value);
  const refresh = async () => {
    const current = ++version;
    list.innerHTML = "";
    if (!project) { if (status) status.textContent = "Elige un proyecto para consultar sus issues."; return; }
    if (status) status.textContent = "Cargando issues del proyecto…";
    if (retry) retry.disabled = true;
    try {
      const result = await apiRoot(`/projects/${encodeURIComponent(project)}/issues`);
      if (disposed || current !== version) return;
      const issues = Array.isArray(result.issues) ? result.issues : [];
      list.innerHTML = issues.map(issue => `<option value="${attr(issue.url || issue.number)}">${escape(`#${issue.number} · ${issue.title || "Sin título"} · ${issue.state || ""}`)}</option>`).join("");
      if (status) status.textContent = issues.length
        ? `${issues.length} issue(s) disponibles. También puedes pegar un número o URL.`
        : "No hay issues en esta lista. Puedes ingresar una URL o número para verificarlo al guardar.";
    } catch (error) {
      if (!disposed && current === version && status) status.textContent = `No se pudo cargar la lista: ${error.message}. Puedes reintentar o ingresar una URL o número; el dato escrito se conserva.`;
    } finally { if (!disposed && current === version && retry) retry.disabled = false; }
  };
  input.addEventListener("input", changed);
  retry?.addEventListener("click", refresh);
  refresh();
  return {
    setProject(next) {
      if (next === project) return;
      project = next; input.value = ""; changed(); refresh();
    },
    destroy() { disposed = true; ++version; input.removeEventListener("input", changed); retry?.removeEventListener("click", refresh); },
  };
}

export function issueLinkHtml(issue) {
  if (!issue) return "Sin issue vinculada";
  const label = `${issue.repo || "GitHub"} #${issue.number || ""}${issue.title ? ` · ${issue.title}` : ""}`;
  try {
    const url = parseIssueInput(issue.url);
    if (typeof url === "string") {
      const parts = new URL(url).pathname.split("/");
      if ((!issue.repo || issue.repo.toLowerCase() === `${parts[1]}/${parts[2]}`.toLowerCase())
          && Number(parts[4]) === Number(issue.number)) {
        return `<a href="${attr(url)}" target="_blank" rel="noopener">${escape(label)}</a>`;
      }
    }
  } catch (_) { /* Una referencia antigua incompleta se muestra como texto. */ }
  return escape(label);
}

export function issueSyncHtml(snapshot, { canRetry = false } = {}) {
  if (!snapshot) return '<p class="chat-task-detail" role="status">Cargando sincronización de la issue…</p>';
  const labels = { disabled:"No configurada", not_configured:"No configurada", pending:"Pendiente",
    error:"Error", failed:"Error", synced:"Sincronizada", applied:"Sincronizada", unchanged:"Al día",
    partial:"Parcial", not_linked:"Sin issue vinculada", unlinked:"Sin issue vinculada",
    uncertain:"Resultado incierto", processing:"En curso", unobserved:"Sin lectura verificada" };
  const reported = snapshot.status || "pending";
  // Apagar la política no borra errores ni incertidumbres de efectos anteriores.
  const status = snapshot.enabled === false && !["error", "failed", "uncertain", "processing"].includes(reported)
    ? "disabled" : reported;
  const observed = snapshot.observed || {};
  const issueState = observed.issue_state || observed.issue?.state || snapshot.issue?.state;
  const projects = Array.isArray(observed.projects) ? observed.projects : [];
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  return `<p class="chat-task-detail"><span class="badge ${["error", "failed"].includes(status) ? "err" : ["synced", "applied", "unchanged"].includes(status) ? "ok" : "warn"}">${escape(labels[status] || status)}</span>
      ${snapshot.enabled === false ? "Política desactivada. Solo se reconcilian resultados inciertos anteriores; no se aplican nuevos movimientos." : "Estado de la última sincronización registrada."}
      ${issueState ? `<br>Estado observado en GitHub: ${escape(issueState)}` : ""}
      ${observed.at || observed.observed_at ? `<br>Lectura: ${escape(observed.at || observed.observed_at)}` : ""}</p>
    ${projects.length ? `<ul class="chat-task-detail">${projects.map(project => `<li>Project ${escape(project.title || project.project_id || "")}: ${escape(project.status || project.option_name || project.value || project.option_id || "sin estado observado")}</li>`).join("")}</ul>` : ""}
    ${snapshot.error ? `<p class="chat-task-error" role="alert">${escape(snapshot.error)}</p>` : ""}
    ${canRetry && ["error", "failed"].includes(status) && !events.some(event => ["uncertain", "processing"].includes(event.state))
      ? '<button type="button" class="btn btn-xs" data-issue-action="retry">Revisar y reintentar</button>' : ""}
    ${events.length ? `<details data-issue-section="history"><summary>Historial de sincronización (${events.length})</summary><ul class="chat-task-detail">${events.map(event => `<li>${escape(event.at || event.created_at || "")} · ${escape(event.transition || event.kind || event.event || "Evento")} · ${escape(event.status || event.state || "")}${event.error ? ` · ${escape(event.error)}` : ""}</li>`).join("")}</ul></details>` : ""}`;
}

export function issuePanelHtml(task) {
  const allowed = new Set(task.allowed_actions || []);
  const issue = task.issue_link;
  return `<section class="form-section" data-issue-panel>
    <strong>Issue del trabajo</strong><p class="chat-task-detail">${issueLinkHtml(issue)}</p>
    ${issue ? `<p class="chat-task-detail">${task.resolution_complete ? "Declarada como resolución completa por una persona." : "Trabajo parcial: no se declara resuelta toda la issue."} Si hay varios PR, el cierre requiere comprobar que todos los vinculados estén fusionados. Cerrar un PR sin merge no cierra la issue.</p><div data-issue-sync>${issueSyncHtml(task.issue_sync, {canRetry:allowed.has("issue_sync")})}</div>` : '<p class="chat-task-detail">Puedes asociar una issue verificada al trabajo antes o después de implementar.</p>'}
    <div class="chat-task-actions">${allowed.has("link_issue") ? `<button type="button" class="btn btn-xs" data-issue-action="link">${issue ? "Editar vínculo / resolución" : "Vincular issue"}</button>` : ""}
    ${issue && allowed.has("issue_sync") ? '<button type="button" class="btn btn-xs" data-issue-action="refresh">Actualizar sincronización</button>' : ""}</div>
  </section>`;
}

export async function openIssueLink({ convId, projectSlug, task, onSaved } = {}) {
  const session = ++modalSession;
  let picker;
  await modalLoad({ id:MODAL, title:"Vincular issue del trabajo", loader:async () => task,
    render:() => `<form id="issue-link-form">${formSection({ title:"Issue y alcance de la resolución",
      sub:"La misma issue puede tener varios trabajos y PR. Vincularla no la cierra ni activa la política de sincronización.",
      body:issuePickerHtml({ id:"issue-link-input", value:task.issue_link?.url || task.issue_link?.number || "", required:true })
        + field({ label:"Alcance declarado por ti", id:"issue-resolution-complete",
          control:`<label class="chat-task-track"><input id="issue-resolution-complete" name="resolution_complete" type="checkbox" ${task.resolution_complete ? "checked" : ""}> Este trabajo resuelve la issue completa</label>`,
          hint:"Déjalo desmarcado para avances parciales. Con política activa, el cierre solo procede tras verificar el merge de todos los PR vinculados; un PR cerrado sin merge conserva la issue abierta." }),
      actions:'<button type="submit" class="btn btn-primary">Guardar vínculo</button>',
    })}</form>`,
    after:() => {
      const form = document.getElementById("issue-link-form");
      picker = mountIssuePicker(form, {id:"issue-link-input",projectSlug});
      const input = form.querySelector('[name="issue"]');
      const resolution = form.querySelector('[name="resolution_complete"]');
      // Una declaración anterior no se transfiere a otra referencia por accidente.
      input.addEventListener("input", () => { resolution.checked = false; });
      input.focus();
      form.onsubmit = async event => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        if (button.disabled) return;
        const output = document.getElementById(`${MODAL}-msg`);
        try {
          const payload = issueLinkPayload(input.value, resolution.checked);
          button.disabled = true; input.disabled = true; resolution.disabled = true;
          if (output) output.textContent = "Verificando issue en GitHub…";
          const next = await apiRoot(`/conversations/${encodeURIComponent(convId)}/issue`, {method:"POST",body:payload});
          if (session !== modalSession || !isModalOpen(MODAL)) return;
          picker.destroy(); closeModal(MODAL); toast("Vínculo de issue guardado", "ok"); await onSaved?.(next);
        } catch (error) {
          if (session === modalSession && isModalOpen(MODAL) && output) output.textContent = `No se pudo guardar: ${error.message}. Conservamos los datos para reintentar.`;
        } finally { button.disabled = false; input.disabled = false; resolution.disabled = false; }
      };
    },
  });
}

export function mountIssuePanel(root, { convId, projectSlug, task, onSaved, onBusyChange } = {}) {
  const region = root.querySelector("[data-issue-sync]");
  const link = root.querySelector('[data-issue-action="link"]');
  const refresh = root.querySelector('[data-issue-action="refresh"]');
  const canRetry = new Set(task.allowed_actions || []).has("issue_sync");
  let disposed = false, version = 0, busy = false;
  const load = async (action = "") => {
    if (busy || !region) return;
    busy = true; const current = ++version;
    if (action) onBusyChange?.(true);
    if (refresh) refresh.disabled = true;
    const retry = region.querySelector?.('[data-issue-action="retry"]');
    if (retry) retry.disabled = true;
    try {
      if (action === "retry" && (!await confirmModal({
        title:"Revisar y reintentar sincronización",
        body:"Revisa primero el estado observado de la issue y Projects. Si la política está habilitada, el reintento vuelve a verificar GitHub y puede aplicar el mapeo configurado al estado actual, incluso después de un conflicto manual. Los efectos inciertos deben reconciliarse y se conservan los límites de escritura.",
        confirmText:"Revisé el estado y reintentar",
      }) || disposed)) return;
      const snapshot = await apiRoot(`/conversations/${encodeURIComponent(convId)}/issue-sync`,
        action ? {method:"POST",body:{action}} : undefined);
      if (!disposed && current === version) {
        const open = [...(region.querySelectorAll?.("details[open][data-issue-section]") || [])]
          .map(section => section.dataset.issueSection);
        region.innerHTML = issueSyncHtml(snapshot, {canRetry});
        region.querySelectorAll?.("[data-issue-section]").forEach(section => {
          section.open = open.includes(section.dataset.issueSection);
        });
      }
    } catch (error) {
      if (!disposed && current === version) region.innerHTML = `<p class="chat-task-error" role="alert">No se pudo consultar la sincronización: ${escape(error.message)}. Puedes reintentar con Actualizar sincronización.</p>`;
    } finally {
      busy = false;
      if (action) onBusyChange?.(false);
      if (!disposed && refresh) refresh.disabled = false;
      if (!disposed && retry) retry.disabled = false;
    }
  };
  const edit = () => openIssueLink({convId,projectSlug,task,onSaved});
  const apply = () => load("refresh");
  const retry = event => {
    const button = event.target?.closest?.('[data-issue-action="retry"]');
    if (button && !button.disabled && canRetry) load("retry");
  };
  link?.addEventListener("click", edit);
  refresh?.addEventListener("click", apply);
  region?.addEventListener("click", retry);
  if (task.issue_link) load();
  return {destroy() { disposed = true; ++version; link?.removeEventListener("click", edit); refresh?.removeEventListener("click", apply); region?.removeEventListener("click", retry); }};
}
