// Configuración de propietario; habilitar permite al worker revisar los PR externos abiertos.
import { api, escape } from "./api.js";
import { modalLoad, isModalOpen, closeModal, field, formSection, emptyState, toast } from "./ui.js";

const MODAL = "external-pr-modal";
let session = 0;

export function externalReviewPolicyPayload(value) {
  const model = String(value.model || "").trim();
  if (value.enabled === true && !model) throw new Error("Selecciona un modelo existente antes de activar la revisión automática.");
  if (!["","personal","bot"].includes(value.publication_mode || "")) throw new Error("Elige un modo de publicación válido.");
  const recipients = [...new Set(String(value.recipients || "").split(/[,;\n]/).map(item => item.trim().toLowerCase()).filter(Boolean))];
  if (recipients.some(item => !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(item))) throw new Error("Ingresa correos válidos de usuarios activos de Relay.");
  return {enabled:value.enabled === true,model,publication_mode:value.publication_mode || "",
    publisher_actor:String(value.publisher_actor || "").trim(),recipients};
}

export async function openExternalReviewConfiguration({task,onSaved} = {}) {
  const current = ++session, slug = task?.project_slug;
  const policy = task?.review_configuration || {};
  await modalLoad({id:MODAL,title:"Configurar revisión automática",loader:async () => slug ? await api("models") : {models:[]},
    render:catalog => !slug ? emptyState({title:"Falta el proyecto del registro",sub:"Actualiza la tarea para consultar su configuración."})
      : `<form id="external-review-config-form">${formSection({title:"Revisión de PR externos",
        sub:"Al habilitar esta política, el proceso de fondo podrá iniciar revisiones de los PR externos abiertos de este proyecto, incluidos los ya registrados. La revisión no aprueba, fusiona ni despliega.",
        body:field({label:"Activación",control:`<label><input type="checkbox" name="enabled" ${policy.enabled === true ? "checked" : ""}> Revisar automáticamente los PR externos abiertos de este proyecto (incluidos los ya registrados)</label>`})
          + field({label:"Modelo existente",id:"external-review-model",control:`<select id="external-review-model" name="model" class="select"><option value="">Selecciona un modelo del catálogo</option>${(catalog.models || []).filter(model => model.spec && model.enabled !== false && model.enabled !== 0).map(model => `<option value="${escape(model.spec)}" ${model.spec === policy.model ? "selected" : ""}>${escape(model.label || model.spec)} · ${escape(model.spec)}</option>`).join("")}</select>`,hint:"No se elige un modelo por defecto al activar la revisión."})
          + field({label:"Publicación del informe",id:"external-review-publication",control:`<select id="external-review-publication" name="publication_mode" class="select"><option value="">Guardar sólo en Relay</option><option value="personal" ${policy.publication_mode === "personal" ? "selected" : ""}>Comentario con identidad personal</option><option value="bot" ${policy.publication_mode === "bot" ? "selected" : ""}>Comentario con bot configurado</option></select>`,hint:"El comentario del bot no aprueba ni fusiona el PR."})
          + field({label:"Identidad publicadora (opcional)",id:"external-review-publisher",control:`<input id="external-review-publisher" name="publisher_actor" class="input" value="${escape(policy.publisher_actor || "")}">`,hint:"Usa una identidad original existente; el servidor verifica su acceso al proyecto."})
          + field({label:"Destinatarios de avisos en Relay",id:"external-review-recipients",control:`<textarea id="external-review-recipients" name="recipients" class="input" rows="3">${escape((policy.recipients || []).join("\n"))}</textarea>`,hint:"Correos de usuarios activos de Relay, separados por coma o línea. No se crean cuentas ni se amplían permisos."}),
        actions:'<button type="submit" class="btn btn-primary">Guardar configuración</button>'})}</form>`,
    after:() => {
      const form = document.getElementById("external-review-config-form");
      if (!form) return;
      form.onsubmit = async event => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        if (button.disabled) return;
        let fields;
        try {
          const values = new FormData(form);
          const payload = externalReviewPolicyPayload({enabled:form.querySelector('[name="enabled"]').checked,
            model:values.get("model"),publication_mode:values.get("publication_mode"),
            publisher_actor:values.get("publisher_actor"),recipients:values.get("recipients")});
          fields = [...form.elements].map(control => [control,control.disabled]);
          fields.forEach(([control]) => { control.disabled = true; });
          const result = await api(`projects/${encodeURIComponent(slug)}`,{method:"PATCH",body:{external_pr_review_policy:payload}});
          if (current !== session || !isModalOpen(MODAL) || !form.isConnected) return;
          closeModal(MODAL); toast("Configuración de revisión guardada", "ok"); await onSaved?.(result);
        } catch (error) {
          if (current === session && isModalOpen(MODAL) && form.isConnected) document.getElementById(`${MODAL}-msg`).textContent = `No se pudo guardar la configuración: ${/[<>]/.test(error.message || "") ? "respuesta no válida" : error.message}. Conservamos los campos.`;
        } finally {
          if (current === session && isModalOpen(MODAL) && form.isConnected && fields) fields.forEach(([control,disabled]) => { control.disabled = disabled; });
        }
      };
    }});
}
