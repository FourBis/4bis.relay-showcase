// Registro de trabajo externo: GitHub verifica identidad/SHA; el formulario declara evidencia.
import { api, apiRoot, escape } from "./api.js";
import { modalLoad, closeModal, isModalOpen, field, formSection, emptyState, toast } from "./ui.js";
import { issuePickerHtml, mountIssuePicker, optionalIssuePayload } from "./chat-issue.js";

const MODAL = "external-pr-modal";
let modalSession = 0;
const evidenceRequests = new Map();

export function externalStateLabel(state) {
  return ({ registered: "Registrado", missing_evidence: "Falta evidencia",
    review_pending: "Revisión pendiente", ready: "Apto en Relay" })[state] || "Registrado";
}

function githubUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com"
      && !url.username && !url.password && !url.port ? url.href : "";
  } catch (_) { return ""; }
}

function link(value, label) {
  const url = githubUrl(value);
  return url ? `<a href="${attrEscape(url)}" target="_blank" rel="noopener">${escape(label)}</a>` : escape(label);
}

function attrEscape(value) { return escape(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
function sha(value) { return `<code class="break-all">${escape(value || "sin verificar")}</code>`; }
function human(actor) {
  return escape(actor?.github_login || actor?.relay_actor || actor?.login || "sin identidad");
}

export function externalEvidencePayload(convId, headSha, summary) {
  const key = JSON.stringify([convId, headSha, summary]);
  if (!evidenceRequests.has(key)) evidenceRequests.set(key, globalThis.crypto?.randomUUID?.()
    || `evidence-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  return { action: "evidence", request_id: evidenceRequests.get(key), head_sha: headSha, summary };
}

export function externalPanelHtml(task) {
  const pr = task.external_pr || {};
  const assessment = task.assessment || {};
  const evidence = task.evidence || [];
  const allowed = new Set(task.allowed_actions || []);
  const policy = assessment.policy;
  const checks = pr.checks || [];
  const reviews = pr.reviews || [];
  const history = task.history || [];
  const stale = (entry) => entry.head_sha !== pr.head_sha;
  const evidenceHtml = evidence.map((entry) => `<li class="chat-task-detail">
    <span class="badge ${stale(entry) ? "warn" : "dim"}">${stale(entry) ? "Histórica · otro SHA" : "Declarada · SHA actual"}</span>
    <p>${escape(entry.summary || "")}</p>${sha(entry.head_sha)} · ${human(entry.actor)} · ${escape(entry.at || "")}</li>`).join("");
  const checkHtml = checks.map((check) => `<li>${link(check.html_url, check.name || "Check")}:
    ${escape(check.conclusion || check.status || "pendiente")}
    ${check.head_sha !== pr.head_sha ? '<span class="badge warn">otro SHA</span>' : ""}</li>`).join("");
  const reviewHtml = reviews.map((review) => `<li>${link(review.html_url, review.user?.login || "Revisor")}:
    ${escape(review.state || "sin estado")} · ${sha(review.commit_id)}
    ${review.commit_id !== pr.head_sha ? '<span class="badge warn">Histórica</span>' : ""}
    ${review.user?.type !== "User" ? '<span class="badge dim">Bot / identidad no humana</span>' : ""}
    ${review.user?.id === pr.author?.id ? '<span class="badge warn">Autor del PR</span>' : ""}</li>`).join("");
  const historyHtml = history.map(entry => {
    const snapshot = entry.snapshot;
    return `<li>${escape(entry.at || "")} · ${escape(({github_snapshot:"Lectura GitHub",declared_evidence:"Declaración",verification_failed:"Falló la verificación"})[entry.kind] || entry.kind || "Evento")}
      · ${human(entry.actor)} ${sha(entry.head_sha || snapshot?.head_sha || entry.evidence?.head_sha)}
      ${entry.evidence?.summary ? `<p>${escape(entry.evidence.summary)}</p>` : ""}
      ${snapshot ? `<details><summary>Datos conservados de esta lectura</summary><p>Base: ${sha(snapshot.base_sha)} · ${escape(snapshot.state || "")}</p>
        <ul>${(snapshot.reviews || []).map(review => `<li>Revisión ${escape(review.user?.login || "")} · ${escape(review.state || "")} · ${sha(review.commit_id)}</li>`).join("")}
        ${(snapshot.checks || []).map(check => `<li>Check ${escape(check.name || "")} · ${escape(check.conclusion || check.status || "")} · ${sha(check.head_sha)}</li>`).join("")}</ul></details>` : ""}</li>`;
  }).join("");
  return `<div class="chat-task-head"><strong>PR externo</strong>
    <span class="badge ${assessment.state === "ready" ? "ok" : "warn"}">${externalStateLabel(assessment.state)}</span></div>
    <div class="chat-task-meta">${link(pr.url, `${pr.repo || "GitHub"} #${pr.number || ""}`)}
      <span>${escape(pr.state || "sin verificar")}${pr.draft ? " · Draft" : ""}</span>
      <span>Riesgo: ${escape(({low:"bajo",medium:"medio",high:"alto"})[task.risk] || task.risk || "sin definir")}</span></div>
    ${task.title ? `<p class="chat-task-detail"><strong>Tarea: ${escape(task.title)}</strong></p>` : ""}
    ${pr.title ? `<p class="chat-task-detail">${escape(pr.title)}</p>` : ""}
    <p class="chat-task-detail">Responsable: ${human(task.responsible)} · Actor Relay: ${escape(task.responsible?.relay_actor || "sin identidad")}
      <br>Autor GitHub verificado: ${human(pr.author)} (${escape(pr.author?.type || "sin tipo")})
      <br>Herramienta / bot declarado: ${escape(task.actor_label || "sin declarar")}${task.issue_number ? `<br>Issue: #${Number(task.issue_number)}` : ""}</p>
    <p class="chat-task-detail">Head actual: ${sha(pr.head_sha)}<br>Base: ${sha(pr.base_sha)}
      <br>Última lectura GitHub: ${escape(pr.observed_at || "sin verificar")}</p>
    ${task.verification_error ? `<p class="chat-task-error" role="alert">${escape(task.verification_error)}</p>` : ""}
    ${(assessment.reasons || []).length ? `<ul class="chat-task-detail">${assessment.reasons.map(reason => `<li>${escape(reason)}</li>`).join("")}</ul>` : ""}
    <p class="chat-task-detail">${policy ? `Política: checks requeridos ${escape((policy.required_checks || []).join(", ") || "ninguno")}; declaración ${policy.require_declaration ? "requerida" : "opcional"}.` : "No hay política de evidencia configurada para este riesgo."}
      Se exige aprobación humana independiente del autor y del responsable para el SHA actual. Un check verde acredita el resultado publicado por GitHub; no equivale a aprobación humana. El estado en Relay no impide fusionar en GitHub.</p>
    <div class="chat-task-actions">${allowed.has("external_refresh") ? '<button type="button" class="btn btn-xs" data-external-action="refresh">Actualizar desde GitHub</button>' : ""}
      ${allowed.has("external_evidence") ? '<button type="button" class="btn btn-xs btn-primary" data-external-action="evidence">Declarar evidencia</button>' : ""}</div>
    <details data-external-section="evidence"><summary>Evidencia declarada (${evidence.length})</summary>
      <p class="chat-task-detail">Texto aportado por una persona identificada. Relay no ejecutó estas pruebas.</p>
      ${evidence.length ? `<ul>${evidenceHtml}</ul>` : emptyState({title:"Sin evidencia declarada",sub:"Declara qué probaste para el head actual."})}</details>
    <details data-external-section="checks"><summary>Checks verificados en GitHub (${checks.length})</summary>
      ${checks.length ? `<ul class="chat-task-detail">${checkHtml}</ul>` : '<p class="chat-task-detail">GitHub no devolvió checks para este SHA.</p>'}</details>
    <details data-external-section="reviews"><summary>Revisiones GitHub (${reviews.length})</summary>
      ${reviews.length ? `<ul class="chat-task-detail">${reviewHtml}</ul>` : '<p class="chat-task-detail">Sin revisiones verificadas.</p>'}</details>
    <details data-external-section="history"><summary>Historial conservado (${history.length})</summary><ul class="chat-task-detail">${historyHtml}</ul></details>`;
}

function formError(message) {
  const output = document.getElementById(`${MODAL}-msg`);
  if (output) output.textContent = message;
}

export async function openExternalRegistration({ projectSlug = "", onRegistered } = {}) {
  const session = ++modalSession;
  let issuePicker;
  await modalLoad({ id: MODAL, title: "Registrar PR externo", loader: async () => {
    return await api("projects?capability=external_pr");
  },
    render: ({ projects = [] }) => projects.length ? `<form id="external-register-form">${formSection({
      title: "Trabajo hecho fuera de Relay",
      sub: "Registra la tarea y su PR. GitHub verificará el repositorio, el autor y el commit. Tú quedas como responsable con tu identidad GitHub conectada.",
      body: field({label:"Proyecto autorizado",id:"external-project",required:true,control:`<select id="external-project" name="project" class="select" required>${projects.map(p => `<option value="${attrEscape(p.slug)}" ${p.slug === projectSlug ? "selected" : ""}>${escape(p.slug)}</option>`).join("")}</select>`})
        + field({label:"URL del PR GitHub",id:"external-url",required:true,control:'<input id="external-url" name="url" type="url" class="input" required placeholder="https://github.com/organización/repositorio/pull/123">'})
        + field({label:"Tarea / objetivo",id:"external-title",required:true,control:'<input id="external-title" name="title" class="input" maxlength="200" required>'})
        + field({label:"Riesgo",id:"external-risk",required:true,control:'<select id="external-risk" name="risk" class="select" required><option value="">Elige el riesgo</option><option value="low">Bajo</option><option value="medium">Medio</option><option value="high">Alto</option></select>',hint:"Se aplica la política configurada del proyecto para ese riesgo."})
        + issuePickerHtml({id:"external-issue"})
        + field({label:"Herramienta / bot utilizado (opcional)",id:"external-actor",control:'<input id="external-actor" name="actor_label" class="input" maxlength="200" placeholder="Ej. Codex, Claude, trabajo manual">',hint:"Es una declaración; el autor del PR se lee de GitHub."}),
      actions:'<button class="btn btn-primary" type="submit">Registrar PR</button>',
    })}</form>` : emptyState({title:"No hay proyectos elegibles",sub:"Se requiere acceso de escritura y un repositorio GitHub configurado para registrar un PR."}),
    after: () => {
      const form = document.getElementById("external-register-form");
      if (!form) return;
      const project = form.querySelector('[name="project"]');
      issuePicker = mountIssuePicker(form, {id:"external-issue",projectSlug:project.value});
      project.addEventListener("change", () => issuePicker.setProject(project.value));
      form.querySelector('[name="url"]').focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        if (button.disabled) return;
        const values = new FormData(form);
        const body = {url:String(values.get("url")).trim(),title:String(values.get("title")).trim(),risk:values.get("risk")};
        if (values.get("actor_label")) body.actor_label = String(values.get("actor_label")).trim();
        try { Object.assign(body, optionalIssuePayload(values.get("issue"))); }
        catch (error) { formError(error.message); return; }
        button.disabled = true; button.textContent = "Verificando en GitHub…"; formError("");
        try {
          const result = await apiRoot(`/projects/${encodeURIComponent(values.get("project"))}/external-pr`, {method:"POST",body});
          if (session !== modalSession || !isModalOpen(MODAL)) return;
          issuePicker.destroy(); closeModal(MODAL); toast("PR externo registrado", "ok");
          await onRegistered?.(result);
        } catch (error) {
          if (session === modalSession && isModalOpen(MODAL)) formError(`No se pudo registrar: ${error.message}. Conservamos los datos para reintentar.`);
        } finally { button.disabled = false; button.textContent = "Registrar PR"; }
      };
    },
  });
}

export async function openExternalEvidence({ convId, task, onSaved } = {}) {
  const session = ++modalSession;
  const headSha = task.external_pr?.head_sha;
  await modalLoad({ id: MODAL, title: "Declarar evidencia", loader: async () => task,
    render: () => `<form id="external-evidence-form">${formSection({title:"Declaración para el commit actual",
      sub:"Describe las pruebas realizadas y sus resultados. La identidad se registra en el servidor. Esto no crea una aprobación ni acredita ejecución por Relay.",
      body:field({label:"Head SHA",control:sha(headSha)}) + field({label:"Qué se probó y resultado",id:"external-summary",required:true,control:'<textarea id="external-summary" name="summary" class="input" rows="6" maxlength="12000" required></textarea>'}),
      actions:'<button class="btn btn-primary" type="submit">Guardar declaración</button>',
    })}</form>`,
    after: () => {
      const form = document.getElementById("external-evidence-form");
      form.querySelector("textarea").focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        if (button.disabled) return;
        const summary = form.querySelector("textarea").value.trim();
        if (!summary) { formError("Describe las pruebas y sus resultados."); return; }
        button.disabled = true; button.textContent = "Guardando…"; formError("");
        try {
          const result = await apiRoot(`/conversations/${encodeURIComponent(convId)}/external-pr`, {
            method:"POST",body:externalEvidencePayload(convId,headSha,summary),
          });
          if (session !== modalSession || !isModalOpen(MODAL)) return;
          closeModal(MODAL); toast("Evidencia declarada guardada", "ok"); await onSaved?.(result);
        } catch (error) {
          if (session === modalSession && isModalOpen(MODAL)) formError(`No se pudo guardar: ${error.message}. El texto se conserva para reintentar.`);
        } finally { button.disabled = false; button.textContent = "Guardar declaración"; }
      };
    },
  });
}
