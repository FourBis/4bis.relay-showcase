// GitHub publica la imagen mediante una acción humana; Relay conserva sólo su copia temporal.
import { apiRoot, escape } from "./api.js";
import { modalLoad, isModalOpen, field, formSection, emptyState, toast } from "./ui.js";
import { externalEvidenceTargets } from "./chat-external-pr.js";

const MODAL = "external-pr-modal", MAX_BYTES = 5 * 1024 * 1024;
let session = 0;
const attr = value => escape(String(value ?? ""));
const safeError = error => /[<>]/.test(error.message || "") ? "Respuesta no válida del servicio" : String(error.message || error).slice(0,500);
const targetKind = draft => typeof draft.target === "string" ? draft.target : draft.target?.kind;

export function validateEvidenceImage(file) {
  if (!file || !/\.png$/i.test(file.name || "") || !["","image/png"].includes(file.type || "")) throw new Error("Elige un archivo PNG.");
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > MAX_BYTES) throw new Error("El PNG debe tener contenido y pesar como máximo 5 MB.");
  return file;
}

export function imageGithubTarget(draft, task) {
  const kind = targetKind(draft);
  if (!externalEvidenceTargets(task).includes(kind)) return "";
  try {
    const url = new URL(draft.github_target_url);
    const expected = new URL(kind === "issue" ? task.issue_link.url : task.external_pr.url);
    return url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password
      && url.pathname.replace(/\/$/,"").toLowerCase() === expected.pathname.replace(/\/$/,"").toLowerCase() ? url.href : "";
  } catch { return ""; }
}

export function imageDownloadUrl(draft, convId) {
  try {
    const origin = globalThis.location?.origin || "http://relay.invalid";
    const url = new URL(draft.download_url,origin);
    const expected = `/conversations/${encodeURIComponent(convId)}/external-pr/images/${encodeURIComponent(draft.id)}`;
    return url.origin === origin && url.pathname === expected && !url.search && !url.hash ? url.pathname : "";
  } catch { return ""; }
}

export function evidenceImageDraftHtml(draft, task, convId, value = "") {
  const target = imageGithubTarget(draft,task), download = imageDownloadUrl(draft,convId);
  const confirmed = ["confirmed","cleaned"].includes(draft.state);
  const historical = draft.historical || (task.external_pr?.head_sha && draft.head_sha !== task.external_pr.head_sha);
  const cleanup = draft.state === "cleaned" ? "retirada" : typeof draft.cleanup === "string" ? draft.cleanup : draft.cleanup?.state || draft.cleanup_state;
  return `<section class="form-section" data-image-draft="${attr(draft.id)}">
    <strong>${escape(draft.filename || "Imagen PNG")}</strong> <span class="badge ${confirmed ? "dim" : "warn"}">${confirmed ? "Comentario verificado" : ({pending:"Borrador temporal",failed:"Borrador con error",prepared:"Borrador temporal"})[draft.state] || escape(draft.state || "Borrador temporal")}</span>
    <p class="chat-task-detail">Destino: ${targetKind(draft) === "issue" ? "Issue vinculada" : "PR"} · ${Number.isSafeInteger(draft.size) ? `${draft.size} bytes` : "Tamaño no informado"}<br>SHA: <code class="break-all">${escape(draft.head_sha || "sin verificar")}</code></p>
    ${historical ? '<p class="chat-task-detail">Esta imagen corresponde a otro SHA y se conserva como evidencia histórica.</p>' : ""}
    ${draft.error ? `<p class="chat-task-error" role="alert">${escape(draft.error)}</p>` : ""}
    ${confirmed ? `<p class="chat-task-detail">La imagen quedó vinculada a un comentario comprobado.${cleanup ? ` Copia temporal: ${escape(cleanup)}.` : ""} El archivo original de tu equipo se conserva.</p>` : `<p class="chat-task-detail">Descarga la copia temporal y adjúntala manualmente al comentario en GitHub. Después pega el enlace permanente de ese comentario para verificarlo.</p>
      <div class="field-actions">${download ? `<a class="btn btn-xs" href="${attr(download)}" download>Descargar PNG temporal</a>` : ""}${target ? `<a class="btn btn-xs" href="${attr(target)}" target="_blank" rel="noopener">Abrir destino en GitHub</a>` : ""}</div>
      ${draft.comment_body ? field({label:"Texto del comentario",control:`<textarea class="input" rows="3" readonly>${escape(draft.comment_body)}</textarea>`}) : ""}
      ${field({label:"Enlace permanente del comentario publicado",id:`image-comment-${draft.id}`,control:`<input id="image-comment-${attr(draft.id)}" class="input" type="url" data-image-comment="${attr(draft.id)}" value="${attr(value)}" placeholder="https://github.com/org/repo/pull/123#issuecomment-123">`})}
      <button type="button" class="btn btn-xs" data-image-confirm="${attr(draft.id)}">Verificar comentario publicado</button>`}
  </section>`;
}

export async function openExternalImages({convId,task,onSaved} = {}) {
  const current = ++session;
  const base = `/conversations/${encodeURIComponent(convId)}/external-pr/images`;
  const targets = externalEvidenceTargets(task);
  let drafts = [];
  let uploadFile = null, uploadTarget = "", uploadId = "";
  await modalLoad({id:MODAL,title:"Imágenes de evidencia",loader:async () => {
    try { return await apiRoot(base); }
    catch (error) { return {images:[],error:safeError(error)}; }
  },render:data => {
    drafts = Array.isArray(data.images) ? data.images : [];
    return `${data.error ? `<p class="chat-task-error" role="alert">No se pudieron consultar los borradores: ${escape(data.error)}</p>` : ""}
      <p class="chat-task-detail">Relay prepara una copia temporal. Publica la imagen manualmente en GitHub y revisa su visibilidad según el repositorio. El original local no se modifica. Cerrar esta ventana conserva los borradores pendientes.</p>
      <form id="external-image-form">${formSection({title:"Preparar PNG",body:
        field({label:"Archivo PNG (máximo 5 MB)",id:"external-image-file",required:true,control:'<input id="external-image-file" class="input" type="file" accept="image/png,.png" required>'})
        + field({label:"Destino de la imagen",id:"external-image-target",control:`<select id="external-image-target" class="select"><option value="pr">PR</option>${targets.includes("issue") ? '<option value="issue">Issue vinculada</option>' : ""}</select>`})
        + field({label:"Consentimiento para la copia temporal",control:'<label><input type="checkbox" id="external-image-consent" required> Entiendo que la imagen se copiará temporalmente a Relay y que debo revisar su visibilidad antes de publicarla en GitHub.</label>'}),
        actions:'<button type="submit" class="btn btn-primary">Preparar copia temporal</button>'})}</form>
      <button type="button" class="btn btn-xs" data-image-refresh>Actualizar borradores</button><div data-image-drafts></div>`;
  },after:() => {
    const form = document.getElementById("external-image-form");
    const body = form.parentElement, mount = body.querySelector("[data-image-drafts]");
    const active = () => current === session && isModalOpen(MODAL) && form.isConnected;
    const message = value => { if (active()) document.getElementById(`${MODAL}-msg`).textContent = value; };
    const paint = () => {
      const values = new Map([...mount.querySelectorAll("[data-image-comment]")].map(input => [input.dataset.imageComment,input.value]));
      mount.innerHTML = drafts.length ? drafts.map(draft => evidenceImageDraftHtml(draft,task,convId,values.get(String(draft.id)) || "")).join("")
        : emptyState({title:"Sin borradores de imágenes",sub:"Elige un PNG y prepara su copia temporal para adjuntarlo manualmente al PR o a la issue vinculada."});
    };
    paint();
    form.onsubmit = async event => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      if (button.disabled) return;
      const file = form.querySelector("#external-image-file");
      const consent = form.querySelector("#external-image-consent");
      const target = form.querySelector("#external-image-target").value;
      let fields;
      try {
        validateEvidenceImage(file.files[0]);
        if (!consent.checked) throw new Error("Confirma el consentimiento antes de copiar el PNG.");
        if (!targets.includes(target)) throw new Error("El destino no está vinculado a esta tarea.");
        if (uploadFile !== file.files[0] || uploadTarget !== target) {
          uploadFile = file.files[0]; uploadTarget = target;
          uploadId = globalThis.crypto?.randomUUID?.() || `image-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        }
        const payload = new FormData();
        payload.append("image",file.files[0]); payload.append("head_sha",task.external_pr.head_sha);
        payload.append("target",target); payload.append("consent","true");
        payload.append("request_id",uploadId);
        fields = [...form.elements].map(control => [control,control.disabled]);
        fields.forEach(([control]) => { control.disabled = true; }); message("");
        const response = await apiRoot(base,{method:"POST",body:payload});
        if (!active()) return;
        const draft = response.draft || response;
        drafts = [...drafts.filter(item => item.id !== draft.id),draft]; paint();
        file.value = ""; consent.checked = false;
        message(""); toast("Copia temporal preparada; publícala manualmente en GitHub.","ok");
        await onSaved?.(draft);
      } catch (error) { message(`No se pudo confirmar la preparación: ${safeError(error)}. Conservamos el archivo original; actualiza los borradores antes de volver a preparar la copia.`); }
      finally { if (active() && fields) fields.forEach(([control,disabled]) => { control.disabled = disabled; }); }
    };
    body.querySelector("[data-image-refresh]").addEventListener("click",async event => {
      const button = event.currentTarget; button.disabled = true;
      try { const data = await apiRoot(base); if (active()) { drafts = data.images || []; paint(); message(""); } }
      catch (error) { message(`No se pudieron actualizar los borradores: ${safeError(error)}`); }
      finally { if (active()) button.disabled = false; }
    });
    mount.addEventListener("click",async event => {
      const button = event.target.closest?.("[data-image-confirm]");
      if (!button || button.disabled || !active()) return;
      const id = button.dataset.imageConfirm;
      const input = [...mount.querySelectorAll("[data-image-comment]")].find(item => item.dataset.imageComment === id);
      const commentUrl = input?.value.trim();
      if (!commentUrl) { message("Pega el enlace permanente del comentario publicado."); input?.focus(); return; }
      button.disabled = true; input.disabled = true; message("");
      try {
        const response = await apiRoot(`${base}/${encodeURIComponent(id)}/confirm`,{method:"POST",body:{comment_url:commentUrl}});
        if (!active()) return;
        const draft = response.draft || response;
        drafts = drafts.map(item => String(item.id) === id ? draft : item); paint();
        message(""); toast("Comentario de imagen verificado.","ok");
        await onSaved?.(draft);
      } catch (error) { message(`No se pudo verificar el comentario: ${safeError(error)}. El borrador pendiente se conserva.`); }
      finally { if (active() && button.isConnected) { button.disabled = false; input.disabled = false; } }
    });
  }});
}
