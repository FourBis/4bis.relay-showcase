// Registro de trabajo externo: GitHub verifica identidad/SHA; el formulario declara evidencia.
import { api, apiRoot, escape } from "./api.js";
import { modalLoad, closeModal, isModalOpen, field, formSection, emptyState, toast } from "./ui.js";
import { externalReviewHtml } from "./chat-external-review.js";

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

export function externalEvidenceTargets(task) {
  const linked = Number(task?.issue_number);
  return ["pr", ...(Number.isSafeInteger(linked) && linked > 0
    && Number(task?.issue_link?.number) === linked ? ["issue"] : [])];
}

export function externalEvidencePayload(convId, headSha, summary, target = "pr") {
  if (!["pr","issue"].includes(target)) throw new Error("Elige PR o issue como destino de la evidencia.");
  const key = JSON.stringify([convId, headSha, summary, target]);
  if (!evidenceRequests.has(key)) evidenceRequests.set(key, globalThis.crypto?.randomUUID?.()
    || `evidence-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  return { action: "evidence", request_id: evidenceRequests.get(key), head_sha: headSha, summary, target };
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
  return `<div class="chat-task-head"><strong>PR del proyecto</strong>
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
      ${allowed.has("external_evidence") ? '<button type="button" class="btn btn-xs btn-primary" data-external-action="evidence">Declarar evidencia</button>' : ""}
      ${allowed.has("external_evidence_image") ? '<button type="button" class="btn btn-xs" data-external-action="images">Adjuntar imagen de evidencia</button>' : ""}</div>
    <details data-external-section="evidence"><summary>Evidencia declarada (${evidence.length})</summary>
      <p class="chat-task-detail">Texto aportado por una persona identificada. Relay no ejecutó estas pruebas.</p>
      ${evidence.length ? `<ul>${evidenceHtml}</ul>` : emptyState({title:"Sin evidencia declarada",sub:"Declara qué probaste para el head actual."})}</details>
    <details data-external-section="checks"><summary>Checks verificados en GitHub (${checks.length})</summary>
      ${checks.length ? `<ul class="chat-task-detail">${checkHtml}</ul>` : '<p class="chat-task-detail">GitHub no devolvió checks para este SHA.</p>'}</details>
    <details data-external-section="reviews"><summary>Revisiones GitHub (${reviews.length})</summary>
      ${reviews.length ? `<ul class="chat-task-detail">${reviewHtml}</ul>` : '<p class="chat-task-detail">Sin revisiones verificadas.</p>'}</details>
    <details data-external-section="history"><summary>Historial conservado (${history.length})</summary><ul class="chat-task-detail">${historyHtml}</ul></details>
    ${externalReviewHtml(task)}`;
}

function formError(message) {
  const output = document.getElementById(`${MODAL}-msg`);
  if (output) output.textContent = message;
}

// Registro rápido: el dev pega la URL o busca el PR; título, descripción y commits salen de GitHub.
export function prQueryKind(value) {
  const text = String(value || "").trim();
  if (!text) return {kind:"empty"};
  if (/^https?:\/\//i.test(text)) return {kind:"url", url:text.replace(/[?#].*$/,"").replace(/\/(files|commits|checks)(\/[^/]*)?\/?$/,"")};
  const number = text.match(/^#?([1-9]\d{0,9})$/);
  if (number) return {kind:"number", number:Number(number[1])};
  return {kind:"text", query:text};
}

function shortDate(value) {
  const date = new Date(value || "");
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("es-CL", {day:"2-digit", month:"short"});
}

export function externalPreviewHtml(pr) {
  const commits = Array.isArray(pr?.commits) ? pr.commits : [];
  const state = pr.draft ? ["warn","Borrador"] : ({OPEN:["ok","Abierto"],MERGED:["dim","Fusionado"],CLOSED:["dim","Cerrado"]})[pr.state] || ["dim",pr.state || "—"];
  const numbers = [Number.isFinite(pr.additions) ? `<span class="xpr-add">+${pr.additions}</span>` : "",
    Number.isFinite(pr.deletions) ? `<span class="xpr-del">−${pr.deletions}</span>` : "",
    Number.isFinite(pr.changed_files) ? `${pr.changed_files} archivo${pr.changed_files === 1 ? "" : "s"}` : ""].filter(Boolean).join(" ");
  const body = String(pr.body || "").trim();
  const missing = Number(pr.commits_total) > commits.length;
  return `<article class="xpr-card" aria-label="PR seleccionado">
    <header class="xpr-card-head">
      <div class="xpr-card-ref">${link(pr.url, `${pr.repo || ""} #${pr.number || ""}`)}
        <span class="badge ${state[0]}">${escape(state[1])}</span>${pr.registered ? '<span class="badge dim">Ya registrado</span>' : ""}</div>
      <h3 class="xpr-card-title">${escape(pr.title || "Sin título")}</h3>
      <p class="xpr-card-meta">${escape(pr.author || "autor sin verificar")} · <code>${escape(pr.head_ref || "?")}</code> → <code>${escape(pr.base_ref || "?")}</code>${numbers ? ` · ${numbers}` : ""}</p>
    </header>
    <section class="xpr-block"><h4 class="xpr-block-title">Descripción</h4>
      ${body ? `<div class="xpr-body">${escape(body)}</div>${pr.body_truncated ? '<p class="field-hint">Descripción recortada; el texto completo está en GitHub.</p>' : ""}`
        : '<p class="xpr-warn">El PR no tiene descripción. Agrégala en GitHub: es el contexto que usa la revisión.</p>'}</section>
    <section class="xpr-block"><h4 class="xpr-block-title">Commits <span class="xpr-count">${escape(String(pr.commits_total ?? commits.length))}</span></h4>
      ${commits.length ? `<ol class="xpr-commits">${commits.map(commit => `<li>
        <code class="xpr-sha">${escape(String(commit.sha || "").slice(0,7))}</code>
        <span class="xpr-commit-msg">${escape(commit.headline || "(sin mensaje)")}</span>
        <span class="xpr-commit-meta">${escape(commit.author || "")}${commit.date ? ` · ${escape(shortDate(commit.date))}` : ""}</span></li>`).join("")}</ol>`
        : '<p class="field-hint">GitHub no devolvió commits para este PR.</p>'}
      ${missing ? `<p class="field-hint">Se muestran los primeros ${commits.length}; el resto está en GitHub.</p>` : ""}</section>
    <p class="field-hint">Al registrar, el bot revisa el diff completo del último commit (<code>${escape(String(pr.head_sha || "").slice(0,7))}</code>) contra <code>${escape(pr.base_ref || "la base")}</code>.</p>
  </article>`;
}

export async function openExternalRegistration({ projectSlug = "", lockProject = false, onRegistered } = {}) {
  const session = ++modalSession;
  await modalLoad({ id: MODAL, title: "Registrar PR", loader: async () => {
    const catalog = await api("projects?capability=external_pr");
    return lockProject ? {...catalog,projects:(catalog.projects || []).filter(project => project.slug === projectSlug)} : catalog;
  },
    render: ({ projects = [] }) => {
      if (!projects.length) return emptyState({title:"No hay proyectos elegibles",sub:lockProject ? "Este proyecto no está habilitado para registrar PR con tu cuenta. Se requiere acceso de escritura y un repositorio GitHub configurado." : "Se requiere acceso de escritura y un repositorio GitHub configurado para registrar un PR."});
      const selected = projects.some(p => p.slug === projectSlug) ? projectSlug : projects[0].slug;
      const projectControl = projects.length > 1
        ? `<select id="external-project" name="project" class="select" aria-label="Proyecto">${projects.map(p => `<option value="${attrEscape(p.slug)}" ${p.slug === selected ? "selected" : ""}>${escape(p.slug)}</option>`).join("")}</select>`
        : `<input type="hidden" id="external-project" name="project" value="${attrEscape(selected)}"><span class="xpr-project">${escape(selected)}</span>`;
      return `<form id="external-register-form" class="xpr-form" autocomplete="off">
        <p class="xpr-lead">Pega la URL del PR o búscalo. El título, la descripción y los commits se leen de GitHub; solo se aceptan PR del repositorio del proyecto.</p>
        <div class="xpr-row">
          <div class="xpr-project-box"><span class="field-label">Proyecto</span>${projectControl}</div>
          <div class="xpr-combo">
            <label class="field-label" for="external-url">PR de GitHub</label>
            <input id="external-url" name="url" type="search" class="input" maxlength="500" spellcheck="false"
              placeholder="https://github.com/org/repo/pull/123 · #123 · palabras del título"
              role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="external-pr-options" aria-describedby="external-pr-status">
            <ul id="external-pr-options" class="xpr-options" role="listbox" hidden></ul>
          </div>
        </div>
        <p id="external-pr-status" class="field-hint" role="status" aria-live="polite"></p>
        <div id="external-pr-preview" class="xpr-preview"></div>
        <div class="xpr-actions"><button class="btn btn-primary" type="submit" disabled>Registrar PR</button></div>
      </form>`;
    },
    after: () => {
      const form = document.getElementById("external-register-form");
      if (!form) return;
      const project = form.querySelector('[name="project"]');
      const input = form.querySelector("#external-url");
      const list = form.querySelector("#external-pr-options");
      const status = form.querySelector("#external-pr-status");
      const previewBox = form.querySelector("#external-pr-preview");
      const button = form.querySelector('[type="submit"]');
      let repo = "", items = [], active = -1, current = null, version = 0, timer = null, controller = null;
      const alive = () => session === modalSession && isModalOpen(MODAL) && form.isConnected;
      const setStatus = (text, error = false) => { status.textContent = text; status.classList.toggle("field-error", error); };
      const closeList = () => { list.hidden = true; input.setAttribute("aria-expanded","false"); input.removeAttribute("aria-activedescendant"); active = -1; };
      const reset = () => { current = null; previewBox.innerHTML = ""; button.disabled = true; button.textContent = "Registrar PR"; formError(""); };
      const paint = () => {
        list.innerHTML = items.map((item, index) => `<li id="external-pr-option-${index}" role="option" class="xpr-option" aria-selected="${index === active}" data-index="${index}">
          <span class="xpr-option-num">#${item.number}</span><span class="xpr-option-title">${escape(item.title || "Sin título")}</span>${item.draft ? '<span class="badge warn">Borrador</span>' : ""}</li>`).join("");
        list.hidden = !items.length;
        input.setAttribute("aria-expanded", String(!list.hidden));
        if (active >= 0) input.setAttribute("aria-activedescendant", `external-pr-option-${active}`);
      };
      const request = () => { controller?.abort(); controller = new AbortController(); return {generation:++version, signal:controller.signal}; };
      const search = async (query = "") => {
        const {generation, signal} = request();
        setStatus(query ? "Buscando PR abiertos…" : "Cargando PR abiertos…");
        try {
          const data = await apiRoot(`/projects/${encodeURIComponent(project.value)}/pulls?${new URLSearchParams({page:"1",q:query})}`, {signal,cache:"no-store"});
          if (generation !== version || !alive()) return;
          repo = data.repo || repo; items = (data.pulls || []).slice(0, 10); active = -1;
          if (document.activeElement === input && !current) paint();
          setStatus(items.length ? `${repo}: elige un PR abierto o pega su URL (también cerrados o fusionados).`
            : `No hay PR abiertos${query ? " con esa búsqueda" : ""} en ${repo}. Pega la URL si está cerrado o fusionado.`);
        } catch (error) {
          if (generation !== version || signal.aborted || !alive()) return;
          items = []; closeList();
          setStatus(`No se pudo consultar la lista: ${error.message}. Puedes pegar la URL del PR.`, true);
        }
      };
      const preview = async url => {
        closeList(); reset();
        const {generation, signal} = request();
        setStatus("Leyendo el PR en GitHub…");
        previewBox.innerHTML = '<div class="xpr-card xpr-loading" aria-hidden="true"><div class="skeleton h-4 w-1/3"></div><div class="skeleton h-5 w-2/3"></div><div class="skeleton h-16 w-full"></div></div>';
        try {
          const data = await apiRoot(`/projects/${encodeURIComponent(project.value)}/external-pr/preview?${new URLSearchParams({url})}`, {signal,cache:"no-store"});
          if (generation !== version || !alive()) return;
          current = data; repo = data.repo || repo;
          if (input.value.trim() !== data.url) input.value = data.url;
          previewBox.innerHTML = externalPreviewHtml(data);
          button.disabled = false;
          button.textContent = data.registered ? "Abrir registro" : "Registrar PR";
          setStatus(data.registered ? "Este PR ya está registrado; se abrirá su ficha." : "");
        } catch (error) {
          if (generation !== version || signal.aborted || !alive()) return;
          previewBox.innerHTML = "";
          setStatus(error.message || "No se pudo leer el PR.", true);
        }
      };
      const choose = item => { if (item) { input.value = item.url; preview(item.url); } };
      const onInput = () => {
        // Invalida al instante cualquier lectura en vuelo: una respuesta vieja no debe pisar lo nuevo.
        clearTimeout(timer); controller?.abort(); ++version; reset();
        const query = prQueryKind(input.value);
        if (query.kind === "url") { timer = setTimeout(() => preview(query.url), 150); return; }
        if (query.kind === "number" && repo) { timer = setTimeout(() => preview(`https://github.com/${repo}/pull/${query.number}`), 350); return; }
        timer = setTimeout(() => search(query.kind === "text" ? query.query : query.kind === "number" ? `#${query.number}` : ""), 300);
      };
      input.addEventListener("input", onInput);
      input.addEventListener("focus", () => { if (!current && items.length) paint(); });
      input.addEventListener("blur", () => setTimeout(() => { if (alive() && !list.contains(document.activeElement)) closeList(); }, 150));
      input.addEventListener("keydown", event => {
        if (event.key === "Escape" && !list.hidden) { event.preventDefault(); event.stopPropagation(); closeList(); return; }
        if (list.hidden || !items.length) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          active = (active + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; paint();
        } else if (event.key === "Enter" && active >= 0) { event.preventDefault(); choose(items[active]); }
      });
      list.addEventListener("mousedown", event => {
        const option = event.target.closest("[data-index]");
        if (option) { event.preventDefault(); choose(items[Number(option.dataset.index)]); }
      });
      project.addEventListener("change", () => { repo = ""; items = []; input.value = ""; reset(); closeList(); search(""); });
      input.focus();
      search("");
      form.onsubmit = async event => {
        event.preventDefault();
        if (button.disabled || !current) return;
        if (current.registered) {
          // Ya existe: abrir su ficha sin re-registrar (puede ser de otra persona del equipo).
          const task = {id:current.registration_id, project_slug:project.value, mode:"external_pr"};
          closeModal(MODAL); await onRegistered?.(task); return;
        }
        const fields = [...form.elements].map(control => [control,control.disabled]);
        fields.forEach(([control]) => { control.disabled = true; });
        button.textContent = "Verificando en GitHub…"; formError("");
        try {
          const result = await apiRoot(`/projects/${encodeURIComponent(project.value)}/external-pr`, {method:"POST",body:{url:current.url}});
          if (!alive()) return;
          closeModal(MODAL); toast("PR registrado", "ok");
          await onRegistered?.(result);
        } catch (error) {
          if (alive()) formError(`No se pudo registrar: ${error.message}`);
        } finally {
          if (alive()) {
            fields.forEach(([control,disabled]) => { control.disabled = disabled; });
            button.textContent = "Registrar PR";
          }
        }
      };
    },
  });
}

export async function openExternalEvidence({ convId, task, onSaved } = {}) {
  const session = ++modalSession;
  const headSha = task.external_pr?.head_sha;
  const targets = externalEvidenceTargets(task);
  await modalLoad({ id: MODAL, title: "Declarar evidencia", loader: async () => task,
    render: () => `<form id="external-evidence-form">${formSection({title:"Declaración para el commit actual",
      sub:"Describe las pruebas realizadas y sus resultados. Guarda la declaración en Relay y solicita un comentario en el PR o la issue seleccionada. El envío requiere publicación configurada y autorización vigente. La identidad se registra en el servidor; la declaración no crea una aprobación ni acredita ejecución de pruebas por Relay.",
      body:field({label:"Head SHA",control:sha(headSha)})
        + field({label:"Destino de la evidencia",id:"external-evidence-target",control:`<select id="external-evidence-target" name="target" class="select"><option value="pr">PR</option>${targets.includes("issue") ? '<option value="issue">Issue vinculada</option>' : ''}</select>`,hint:"El destino se verifica al guardar. La solicitud se envía sólo si la publicación está configurada y tu autorización sigue vigente; guardar no confirma que el comentario ya esté publicado."})
        + field({label:"Qué se probó y resultado",id:"external-summary",required:true,control:'<textarea id="external-summary" name="summary" class="input" rows="6" maxlength="12000" required></textarea>'}),
      actions:'<button class="btn btn-primary" type="submit">Guardar y solicitar publicación</button>',
    })}</form>`,
    after: () => {
      const form = document.getElementById("external-evidence-form");
      form.querySelector("textarea").focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        if (button.disabled) return;
        const summary = form.querySelector("textarea").value.trim();
        const target = form.querySelector('[name="target"]').value;
        if (!summary) { formError("Describe las pruebas y sus resultados."); return; }
        if (!targets.includes(target)) { formError("El destino no está vinculado a esta tarea."); return; }
        const fields = [...form.elements].map(control => [control,control.disabled]);
        fields.forEach(([control]) => { control.disabled = true; });
        button.textContent = "Guardando..."; formError("");
        try {
          const result = await apiRoot(`/conversations/${encodeURIComponent(convId)}/external-pr`, {
            method:"POST",body:externalEvidencePayload(convId,headSha,summary,target),
          });
          if (session !== modalSession || !isModalOpen(MODAL) || !form.isConnected) return;
          closeModal(MODAL); toast("Declaración guardada; solicitud de publicación registrada", "ok"); await onSaved?.(result);
        } catch (error) {
          if (session === modalSession && isModalOpen(MODAL) && form.isConnected) formError(`No se pudo confirmar el resultado: ${error.message}. El texto se conserva y el destino también.`);
        } finally {
          if (session === modalSession && isModalOpen(MODAL) && form.isConnected) {
            fields.forEach(([control,disabled]) => { control.disabled = disabled; });
            button.textContent = "Guardar y solicitar publicación";
          }
        }
      };
    },
  });
}
