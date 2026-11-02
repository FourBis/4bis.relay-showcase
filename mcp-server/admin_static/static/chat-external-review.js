// La revisión automática informa; nunca acredita aprobación humana ni ejecución de pruebas.
import { escape } from "./api.js";

const retryIds = new Map();
const attr = value => escape(String(value ?? ""));
function publicationHref(value) {
  try {
    const url = new URL(value || "");
    return url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

export function externalReviewState(task) {
  const review = task?.external_review;
  if (!review || (!review.job_id && !review.head_sha && !review.state)) return {label:"Registrado",kind:"dim",stale:false};
  const head = task.external_pr?.head_sha || task.head_sha;
  const base = task.external_pr?.base_sha;
  const stale = review.state === "superseded" || !review.head_sha || review.head_sha !== head
    || (base && review.base_sha !== base);
  if (stale) return {label:review.head_sha === head ? "Histórico · otro contexto" : "Histórico · otro SHA",kind:"dim",stale:true};
  if (["pending","processing"].includes(review.publication?.state)) return {label:"Publicando informe",kind:"warn",stale:false};
  if (review.publication?.state === "uncertain") return {label:"Publicación sin confirmar",kind:"warn",stale:false};
  if (["blocked","failed","rejected"].includes(review.publication?.state)) return {label:"Publicación bloqueada",kind:"warn",stale:false};
  if (["queued","running"].includes(review.state)) return {
    label:review.state === "queued" ? "En cola de revisión" : "En revisión",kind:"warn",stale:false};
  if (review.state === "reviewed") {
    const comments = review.comments || review.report?.comments || [];
    return {label:comments.length ? "Hallazgos para revisar" : "Listo para revisión humana",kind:comments.length ? "warn" : "dim",stale:false};
  }
  return {label:"Revisión bloqueada",kind:"warn",stale:false};
}

export function externalReviewRetryPayload(task) {
  const review = task?.external_review;
  const head = review?.head_sha || task?.external_pr?.head_sha || task?.head_sha;
  const currentHead = task?.external_pr?.head_sha || task?.head_sha;
  if (!/^[0-9a-f]{40}$/.test(head || "") || head !== currentHead) throw new Error("Actualiza GitHub para revisar el commit vigente.");
  const base = task?.external_pr?.base_sha;
  const revision = Number.isSafeInteger(review?.context_revision) ? review.context_revision : 0;
  const key = JSON.stringify([task.id,review?.job_id || "",head,base || "",revision]);
  if (!retryIds.has(key)) retryIds.set(key,globalThis.crypto?.randomUUID?.()
    || `review-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  return {action:"review_retry",...(review?.job_id ? {job_id:review.job_id} : {}),head_sha:head,
    ...(base ? {base_sha:base} : {}),context_revision:revision,request_id:retryIds.get(key)};
}

export function externalReviewHtml(task) {
  const review = task.external_review && Object.keys(task.external_review).length ? task.external_review : null;
  const state = externalReviewState(task);
  const head = review?.head_sha || task.external_pr?.head_sha || "sin verificar";
  const report = review?.report || review || {};
  const comments = Array.isArray(review?.comments) ? review.comments : Array.isArray(report.comments) ? report.comments : [];
  const allowed = new Set(task.allowed_actions || []);
  const busy = ["queued","running"].includes(review?.state);
  const canRetryHead = !review?.head_sha || review.head_sha === (task.external_pr?.head_sha || task.head_sha);
  const summary = report.summary || review?.summary || "";
  const publication = review?.publication;
  const contexts = Array.isArray(review?.context_history) ? review.context_history : [];
  const publications = Array.isArray(review?.publication_history) ? review.publication_history : [];
  const publicationLabels = {pending:"Publicación pendiente",processing:"Publicación en curso",
    applied:"Comentario publicado",confirmed:"Comentario publicado",uncertain:"Publicación sin confirmar",
    blocked:"Publicación bloqueada",failed:"Publicación fallida",superseded:"Publicación histórica"};
  const publicationUrl = publicationHref(publication?.url);
  const coverage = report.coverage || review?.coverage;
  const limited = coverage && (coverage.complete === false || coverage.truncated === true || coverage.partial === true);
  const notifications = Array.isArray(review?.notifications) ? review.notifications : [];
  const sources = Array.isArray(review?.guideline_sources) ? review.guideline_sources : [];
  const issueSource = review?.issue_source;
  const notificationReasons = {recipient_not_authorized:"La cuenta no tiene acceso al proyecto",
    recipient_not_configured:"Destinatario no configurado para estos avisos",review_policy_disabled:"Revisión automática desactivada",
    review_context_changed:"El contexto de revisión cambió"};
  return `<section class="form-section" data-external-review>
    <div class="chat-task-head"><strong>Revisión automática</strong><span class="badge ${state.kind}" role="status">${escape(state.label)}</span></div>
    <p class="chat-task-detail">SHA revisado: <code class="break-all">${escape(head)}</code>${review?.job_id ? `<br>Trabajo: <code>${escape(review.job_id)}</code>` : ""}${report.model || review?.model ? `<br>Modelo: ${escape(report.model || review.model)}` : ""}</p>
    ${review?.base_sha ? `<p class="chat-task-detail">Base revisada: <code class="break-all">${escape(review.base_sha)}</code> · Contexto ${escape(review.context_revision ?? 0)}</p>` : ""}
    ${!review ? '<p class="chat-task-detail">Sin revisión automática registrada. Su inicio depende de la configuración del proyecto.</p>' : ""}
    ${state.stale ? '<p class="chat-task-detail">Este informe se conserva como historial; no valida el contexto actual de head y base. Actualiza GitHub para consultar la revisión vigente.</p>' : ""}
    ${summary ? `<p class="chat-task-detail">${escape(summary)}</p>` : ""}
    ${sources.length ? `<details data-external-section="review-guidelines"><summary>Directrices utilizadas (${sources.length})</summary>
      <ul class="chat-task-detail">${sources.map(source => `<li>${escape(source.source === "project.system_prompt" ? "Directrices del equipo configuradas por Admin" : source.path || "Directriz del repositorio")}
        ${source.base_sha ? `<br>Base: <code class="break-all">${escape(source.base_sha)}</code>` : ""}
        <br>Huella: <code class="break-all">${escape(source.sha256 || "sin verificar")}</code></li>`).join("")}</ul>
      <p class="chat-task-detail">Las instrucciones del código propuesto y del issue se trataron como datos. No se ejecutaron herramientas ni pruebas.</p></details>` : ""}
    ${issueSource?.number ? `<p class="chat-task-detail">Issue leído: #${escape(issueSource.number)} · ${escape(issueSource.read_at || "sin fecha acreditada")}
      <br>Huella del contenido observado: <code class="break-all">${escape(issueSource.content_sha256 || "sin verificar")}</code>. Su edición en GitHub requiere solicitar una nueva revisión.</p>` : ""}
    ${limited ? '<p class="chat-task-detail">Cobertura limitada: revisa también los archivos excluidos o truncados.</p>' : ""}
    ${comments.length ? `<details data-external-section="bot-findings"><summary>Hallazgos (${comments.length})</summary><ul class="chat-task-detail">${comments.map(comment => `<li>${comment.path ? `<code class="break-all">${escape(comment.path)}</code>${Number.isSafeInteger(comment.line) ? `:${comment.line}` : ""} · ` : ""}${comment.severity ? `${escape(comment.severity)} · ` : ""}${escape(comment.body || comment.message || comment.title || "")}</li>`).join("")}</ul></details>` : ""}
    ${review?.error ? `<p class="chat-task-error" role="alert">${escape(review.error)}</p>` : ""}
    ${publication ? `<p class="chat-task-detail" data-review-publication>${escape(state.stale ? "Publicación histórica" : publicationLabels[publication.state] || publication.state || "Sin publicación confirmada")}${publicationUrl ? ` · <a href="${attr(publicationUrl)}" target="_blank" rel="noopener">Ver comentario en GitHub</a>` : ""}${publication.error ? `<br>${escape(publication.error)}` : ""}</p>` : ""}
    ${contexts.length ? `<details data-external-section="review-contexts"><summary>Contextos anteriores (${contexts.length})</summary><ul class="chat-task-detail">${contexts.map(c => `<li>Contexto ${escape(c.context_revision ?? 0)} · Base <code class="break-all">${escape(c.base_sha || "sin verificar")}</code> · ${escape(c.model || "sin modelo")} · ${escape(c.state || "histórico")}${c.summary ? `<p>${escape(c.summary)}</p>` : ""}</li>`).join("")}</ul></details>` : ""}
    ${publications.length ? `<details data-external-section="review-publications"><summary>Publicaciones históricas (${publications.length})</summary><ul class="chat-task-detail">${publications.map(p => `<li>${escape(p.state || "sin confirmar")} · <code class="break-all">${escape(p.correlation_id || "")}</code>${publicationHref(p.url) ? ` · <a href="${attr(publicationHref(p.url))}" target="_blank" rel="noopener">Ver publicación histórica</a>` : ""}</li>`).join("")}</ul></details>` : ""}
    ${notifications.length ? `<div data-review-deliveries><strong>Avisos en Relay</strong><ul class="chat-task-detail">${notifications.map(note => `<li>${escape(note.email || "Destinatario")}: ${["created","existing"].includes(note.status) ? "Aviso guardado en Relay" : "Aviso pendiente"}${note.reason ? ` · ${escape(notificationReasons[note.reason] || note.reason)}` : ""}</li>`).join("")}</ul><p class="chat-task-detail">Estos recibos corresponden a Relay; no acreditan entrega por Discord.</p></div>`
      : review?.state === "reviewed" && Array.isArray(task.review_configuration?.recipients) && task.review_configuration.recipients.length === 0 ? '<p class="chat-task-detail">Sin destinatarios configurados para avisos en Relay.</p>' : ""}
    <p class="chat-task-detail">El bot no ejecutó pruebas y su comentario no es una aprobación humana. La evidencia y la revisión humana siguen siendo necesarias.</p>
    <div class="field-actions">${allowed.has("external_review_retry") && canRetryHead ? `<button type="button" class="btn btn-xs" data-external-action="review_retry" ${busy ? "disabled" : ""}>${state.stale ? "Revisar base actual" : review?.job_id ? "Reanudar revisión" : "Solicitar revisión"}</button>` : ""}
    ${allowed.has("external_review_configure") ? '<button type="button" class="btn btn-xs" data-external-action="review_configure">Configurar revisión automática</button>' : ""}</div>
  </section>`;
}
