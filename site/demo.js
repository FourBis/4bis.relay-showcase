"use strict";
// ponytail: fixed, local scenarios. A real Relay connection belongs in
// the installed application, never in this public simulation.
let english = new URLSearchParams(location.search).get("lang") === "en";
const $ = selector => document.querySelector(selector);
const tr = (es, en) => english ? en : es;
const initialChats = () => ({
  docs: {name:null, messages:[], open:true, sharedMode:"none", sharingNote:"", actorDrafts:{owner:{draft:"",pending:null,receipt:null},recipient:{draft:"",pending:null,receipt:null}}},
  access: {name:null, messages:[], open:false, sharedMode:"none", sharingNote:"", actorDrafts:{owner:{draft:"",pending:null,receipt:null},recipient:{draft:"",pending:null,receipt:null}}},
});
let chats = initialChats(), active = "docs", turns = 1, feedback = false, split = false, continued = false, published = false, githubConnected = false, writeEnabled = false, alexProjects = new Set(), renaming;
const initialParityDemo = () => ({actor:"owner", scope:"mine", outcome:"accepted", memberArchived:false, memberEnabled:true,
  requestCounter:0, sharingNote:"", detailedEnabled:false,
  roles:{planner:"balanced", executor:"balanced", verifier:"balanced", documenter:"balanced", compactor:"balanced"},
  reviewer:"", reviewEnabled:false, notifyRecipient:false, notices:[], modelNotice:""});
let parityDemo = initialParityDemo();
// Pure state for the local external-PR scenario; no remote data or effects.
function initialExternalDemo() {
  return { registered:false, head:1, declaration:0, checks:0, approval:0,
    policy:false, complete:false, firstMerged:false, secondMerged:false, issueClosed:false, lastAction:"",
    reviewState:"idle", reviewHead:0, reviewModel:"", reviewAttempt:0 };
}
function externalDemoAssessment(state) {
  if (!state.registered) return "unregistered";
  if (state.firstMerged) return "merged";
  if (state.declaration !== state.head || state.checks !== state.head) return "missing_evidence";
  return state.approval === state.head ? "ready" : "review_pending";
}
function reduceExternalDemo(state, action, value) {
  const next = { ...state, lastAction:action };
  if (action === "register") next.registered = true;
  if (!next.registered) return state;
  if (action === "declare" && !next.firstMerged) next.declaration = next.head;
  if (action === "check" && !next.firstMerged) next.checks = next.head;
  if (action === "approve" && !next.firstMerged) next.approval = next.head;
  if (action === "new_head" && !next.firstMerged) next.head++;
  if (action === "review_queue" && !next.firstMerged) {
    next.reviewState = "queued"; next.reviewHead = next.head;
    next.reviewModel = value || ""; next.reviewAttempt++;
  }
  if (action === "review_complete" && next.reviewState === "queued" && next.reviewHead === next.head) next.reviewState = "findings";
  if (action === "review_fail" && next.reviewState === "queued" && next.reviewHead === next.head) next.reviewState = "failed";
  if (action === "policy") next.policy = value === true;
  if (action === "complete") next.complete = value === true;
  // A GitHub merge is independent from Relay's assessment, even in this example.
  if (action === "merge_first") next.firstMerged = true;
  if (action === "merge_second") next.secondMerged = true;
  if (next.policy && next.complete && next.firstMerged && next.secondMerged) next.issueClosed = true;
  // Disabling a policy or withdrawing a declaration never reopens a closed issue.
  return next;
}
// End of the pure external-PR scenario state.
let externalDemo = initialExternalDemo();
const chatLabel = id => chats[id].name || (id === "docs" ? tr("Documentación", "Documentation") : tr("Revisar accesos", "Access review"));
const windows = $("#chat-windows");
const dialog = $("#rename-dialog");
document.querySelectorAll("[data-en]").forEach(el => { el.dataset.es = el.textContent; });
const translatedAttributes = ["alt", "aria-label", "href"];
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

function recipientAssigned() { return !parityDemo.memberArchived && parityDemo.memberEnabled && alexProjects.has("web"); }
function canShareDemoMode(mode) {
  return mode === "none" || (recipientAssigned() && (mode !== "collaborate" || writeEnabled));
}
function canReadDemo(chat) {
  return parityDemo.actor === "owner" || (recipientAssigned() && chat.sharedMode !== "none");
}
function canReplyDemo(chat) {
  return parityDemo.actor === "owner" || (canReadDemo(chat) && chat.sharedMode === "collaborate" && writeEnabled);
}
function renderConversationControls() {
  $("#demo-actor").value = parityDemo.actor;
  $("#demo-scope").value = parityDemo.scope;
  $("#demo-send-outcome").value = parityDemo.outcome;
  const inScope = parityDemo.actor === "owner" ? parityDemo.scope === "mine" : parityDemo.scope === "shared";
  const available = Object.keys(chats).filter(id => inScope && canReadDemo(chats[id]));
  if (!available.includes(active) || !chats[active].open) active=available.find(id=>chats[id].open) || available[0] || active;
  const list = $("#demo-conversation-list"); list.replaceChildren();
  for (const id of available) {
    const button = node("button", chatLabel(id), `chip${active === id ? " selected" : ""}`);
    button.type = "button"; button.setAttribute("aria-pressed", String(active === id));
    button.addEventListener("click", () => { active = id; chats[id].open = true; renderChats(true); });
    list.append(button);
  }
  if (!available.length) list.append(node("p", tr("No hay conversaciones visibles en este alcance. Compartir requiere una persona activa, asignada a AuroraDemo y un acceso explícito.", "No conversations are visible in this scope. Sharing requires an active recipient assigned to AuroraDemo and an explicit grant."), "scene-note"));
  $("#demo-access-note").textContent = parityDemo.actor === "owner"
    ? tr("Vista de la persona propietaria · AuroraDemo. Tus conversaciones comienzan privadas.", "Conversation owner view · AuroraDemo. Conversations start private.")
    : tr("Vista de la persona destinataria ficticia. Sólo aparecen los hilos compartidos a los que aún tiene acceso.", "Fictional recipient view. Only conversations with current shared access are visible.");
  $("#demo-sharing").hidden = parityDemo.actor !== "owner" || !available.includes(active);
  $("#demo-share-mode").value = chats[active].sharedMode;
  $("#demo-save-sharing").disabled = !canShareDemoMode($("#demo-share-mode").value);
  $("#demo-sharing-result").textContent = ($("#demo-share-mode").value === "collaborate" && recipientAssigned() && !writeEnabled
    ? tr("Primero habilita el permiso de escritura ficticio en Equipo; un hilo de consulta sólo admite compartir lectura.", "First enable the fictional write permission in Team; a read-only conversation can only be shared for reading.") : "") || (chats[active].sharingNote === "removed" ? tr("Acceso retirado en esta memoria de página. No se notificó a nadie.", "Access removed in page memory. Nobody was notified.") : chats[active].sharingNote === "saved" ? tr("Acceso explícito ficticio guardado. Cambia a la vista de la persona destinataria para comprobar el alcance.", "Fictional explicit grant saved. Switch to the recipient view to inspect its scope.") : "") || (!recipientAssigned()
    ? tr("Primero asigna a Dev demo a AuroraDemo en Equipo. Una persona archivada o sin acceso activo no es elegible.", "First assign Dev demo to AuroraDemo in Team. Archived people or people without active access are not eligible.") : "");
  document.querySelectorAll("[data-open]").forEach(button => { button.disabled = !canReadDemo(chats[button.dataset.open]); });
  return new Set(available);
}
function acceptDemoRequest(id, request) {
  const chat = chats[id], local=chat.actorDrafts[request.actor];
  if (!chat.messages.some(item => item.requestId === request.id)) {
    chat.messages.push({text:request.text, author:"user", who:request.actor, requestId:request.id, order:request.order, part:0},
      {author:"relay", requestId:request.id, order:request.order, part:1, text:tr("Respuesta preparada del escenario. No se consultó un modelo ni se ejecutaron herramientas.", "Scripted scenario reply. No model or tool was called.")});
  }
  chat.messages.sort((a,b)=>a.order-b.order || a.part-b.part);
  if (local.draft === request.text) local.draft = "";
  local.receipt = {...request, state:"accepted"}; local.pending = null;
}
function sendDemoRequest(id) {
  const chat = chats[id], local=chat.actorDrafts[parityDemo.actor];
  if (!canReplyDemo(chat) || !modelEnabled(parityDemo.roles.executor) || local.pending || !local.draft.trim()) return;
  const request = {id:`demo-request-${String(++parityDemo.requestCounter).padStart(3,"0")}`,
    text:local.draft, actor:parityDemo.actor, model:parityDemo.roles.executor, order:parityDemo.requestCounter};
  if (parityDemo.outcome === "uncertain") {
    local.pending = request; local.receipt = {...request, state:"uncertain"};
  } else if (parityDemo.outcome === "rejected") {
    local.receipt = {...request, state:"rejected"};
  } else acceptDemoRequest(id, request);
  active = id; renderChats(true);
}
function renderChats(focus = false) {
  const available = renderConversationControls();
  if (!available.has(active) || !chats[active].open) active=[...available].find(id=>chats[id].open) || [...available][0] || active;
  windows.replaceChildren();
  for (const [id, chat] of Object.entries(chats)) {
    if (!chat.open || !available.has(id)) continue;
    const local=chat.actorDrafts[parityDemo.actor];
    const card = node("article", null, `chat-window${active === id ? " active" : ""}`);
    card.dataset.chat = id;
    const header = node("div", null, "window-heading"); header.append(node("h3", chatLabel(id)));
    const rename = node("button", tr("Nombre", "Name")); rename.type="button";
    rename.disabled = parityDemo.actor !== "owner";
    rename.setAttribute("aria-label", tr("Cambiar título del hilo ficticio", "Rename fictional conversation") + `: ${chatLabel(id)}`);
    rename.addEventListener("click", () => { renaming=id; $("#window-name").value=chatLabel(id); dialog.returnValue=""; dialog.showModal(); $("#window-name").focus(); $("#window-name").select(); });
    const close = node("button", "×"); close.type="button";
    close.setAttribute("aria-label", tr("Cerrar ventana", "Close window") + `: ${chatLabel(id)}`);
    close.addEventListener("click", () => { chat.open=false; active=Object.keys(chats).find(key => chats[key].open && available.has(key)) || id; renderChats(); $("#demo-conversation-list button")?.focus(); });
    header.append(rename,close);
    const context = node("p", `AuroraDemo · ${parityDemo.actor === "owner" ? tr("Propietaria", "Owner") : chat.sharedMode === "read" ? tr("Solo lectura", "Read only") : tr("Colaboración", "Collaboration")} · ${tr("Próximo modelo ficticio", "Next fictional model")}: ${modelLabel(parityDemo.roles.executor)}`, "demo-chat-context");
    const messages=node("div",null,"chat-messages"); messages.setAttribute("role","log"); messages.setAttribute("aria-label",tr("Mensajes ficticios", "Fictional messages")+`: ${chatLabel(id)}`);
    const welcome=tr("Escenario de AuroraDemo. Cada actor conserva su propio borrador e intentos de envío por hilo. No uses información real.", "AuroraDemo scenario. Drafts and send attempts are private to each actor and conversation. Use fictional information only.");
    for (const item of [{text:welcome,author:"relay"},...chat.messages]) {
      const bubble=node("p",null,`message${item.author === "user" ? " user" : ""}`);
      bubble.append(node("span",item.author === "user" ? (item.who === "recipient" ? tr("Destinataria ficticia", "Fictional recipient") : tr("Propietaria ficticia", "Fictional owner")) : tr("Relay · guion", "Relay · script"),"message-author"));
      bubble.append(document.createTextNode(item.text)); messages.append(bubble);
    }
    const form=node("form",null,"composer"); const input=node("textarea"); input.value=local.draft; input.maxLength=600; input.rows=2;
    input.placeholder=tr("Escribe un pedido ficticio.", "Write a fictional request."); input.setAttribute("aria-label",tr("Borrador ficticio", "Fictional draft")+`: ${chatLabel(id)}`);
    input.disabled=!canReplyDemo(chat) || Boolean(local.pending);
    input.addEventListener("input",()=>{local.draft=input.value;});
    input.addEventListener("focus",()=>{active=id; windows.querySelectorAll(".chat-window").forEach(el=>el.classList.toggle("active",el.dataset.chat===id));});
    const send=node("button",tr("Enviar en el escenario", "Send in this scenario")); send.type="submit"; send.disabled=input.disabled || !modelEnabled(parityDemo.roles.executor);
    form.append(input,send); form.addEventListener("submit",event=>{event.preventDefault(); sendDemoRequest(id);});
    const state=node("div",null,"demo-request-state"); state.setAttribute("role","status"); state.dataset.state=local.receipt?.state || "idle";
    if (!canReplyDemo(chat)) state.append(node("p",chat.sharedMode === "read" ? tr("Sólo puedes leer. El acceso compartido no habilita respuestas ni ejecución.", "You can read only. Shared read access does not allow replies or execution.") : tr("La colaboración necesita los permisos técnicos actuales del proyecto y la tarea. Habilita el permiso ficticio en Equipo.", "Collaboration needs current project and task technical permissions. Enable the fictional permission in Team.")));
    if (!modelEnabled(parityDemo.roles.executor)) state.append(node("p",tr("El modelo del próximo pedido está deshabilitado. Conserva el borrador y elige uno habilitado en Modelos y bandeja.", "The next request model is disabled. Keep the draft and choose an enabled model in Models and inbox.")));
    if (local.receipt) {
      const labels={accepted:tr("Confirmado · mensaje presente una sola vez", "Confirmed · message appears once"), rejected:tr("Rechazado antes de admitirse · corrige el borrador y envía explícitamente otro intento", "Rejected before admission · edit the draft and explicitly send another attempt"), uncertain:tr("Resultado desconocido · conserva el borrador y el mismo intento", "Unknown outcome · keep the draft and the same attempt")};
      state.append(node("p",labels[local.receipt.state]),node("code",local.receipt.id),node("p",`${tr("Modelo fijado en ese intento", "Model bound to that attempt")}: ${modelLabel(local.receipt.model)}`));
    }
    if (local.pending) {
      const lookup=node("button",tr("Comprobar el mismo envío", "Look up the same send"),"text-button"); lookup.type="button";
      lookup.disabled=!canReplyDemo(chat) || parityDemo.actor!==local.pending.actor;
      lookup.addEventListener("click",()=>{if (!local.pending || parityDemo.actor!==local.pending.actor || !canReplyDemo(chat)) return; acceptDemoRequest(id,local.pending); renderChats(true);});
      const wait=node("p",tr("El pedido queda bloqueado hasta conocer su resultado. Comprobar localiza el envío ya aceptado del guion; no vuelve a enviarlo. Sólo el actor original puede reconciliarlo.", "The request stays frozen until its outcome is known. Lookup finds the scripted accepted send; it never sends again. Only its original actor can reconcile it."),"scene-note");
      state.append(lookup,wait);
    }
    card.append(header,context,messages,form,state); windows.append(card); messages.scrollTop=messages.scrollHeight;
  }
  if (!windows.childElementCount) windows.append(node("p",tr("No hay ventanas abiertas con acceso en este alcance. Elige una conversación visible o vuelve a la vista de la persona propietaria.", "No accessible windows are open in this scope. Choose a visible conversation or return to the owner view."),"scene-note"));
  if (focus) (windows.querySelector(`[data-chat="${active}"] textarea:not(:disabled)`) || windows.querySelector(`[data-chat="${active}"] .demo-request-state button:not(:disabled)`))?.focus();
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
  const projectNames = {web: "AuroraDemo", shared: "InventoryDemo", docs: tr("Documentación", "Documentation")};
  $("#alex-projects").textContent = alexProjects.size ? [...alexProjects].map(id => projectNames[id]).join(", ") : tr("Ninguno", "None");
  $("#allow-write").disabled = !recipientAssigned() || !githubConnected || writeEnabled;
  $("#connect-github").disabled = githubConnected || !parityDemo.memberEnabled;
  $("#continue-task").disabled = !recipientAssigned() || !writeEnabled || continued || !modelEnabled(parityDemo.roles.executor);
  $("#assignment-state").textContent = alexProjects.has("web") ? tr("Proyecto web asignado", "Web project assigned") : tr("Solo lectura", "Read-only");
  $("#repo-state").textContent = githubConnected ? tr("Cuenta personal conectada (simulación)", "Personal account linked (simulation)") : tr("No conectada", "Not connected");
  $("#write-state").textContent = writeEnabled ? tr("Habilitado explícitamente", "Explicitly enabled") : tr("Deshabilitado", "Disabled");
  $("#execution-state").textContent = continued ? tr("Continuada explícitamente", "Explicitly continued") : tr("En pausa", "Paused");
  $("#team-result").textContent = continued ? tr("Dev demo continuó la tarea explícitamente. No hubo ejecución automática.", "Dev demo explicitly continued the task. No autorun took place.") : "";
  $("#assignment-result").textContent = alexProjects.size ? tr("Cambios guardados para Dev demo.", "Changes saved for Dev demo.") : "";
  $("#github-result").textContent = githubConnected ? tr("Cuenta personal de Dev demo conectada en esta simulación.", "Dev demo's personal account is linked in this simulation.") : "";
  $("#publish-task").disabled = !continued || published;
  renderParitySurfaces();
  $("#publish-result").textContent = published ? tr("Admin Admin demo publicó la PR ficticia. Sigue sin merge ni integración.", "Admin Admin demo published the fictional PR. It remains unmerged and not integrated.") : "";
}

function renderExternalDemo() {
  const state = externalDemo;
  const head = state.head.toString(16).padStart(40, "0");
  $("#external-head").textContent = head;
  const labels = {
    unregistered:tr("Sin registrar", "Not registered"),
    missing_evidence:tr("Falta evidencia actual", "Current evidence missing"),
    review_pending:tr("Revisión pendiente", "Review pending"),
    ready:tr("Apto en esta simulación", "Ready in this simulation"),
    merged:tr("Fusionado (simulación)", "Merged (simulation)"),
  };
  $("#external-assessment").textContent = labels[externalDemoAssessment(state)];
  const evidenceState = value => !value ? tr("Sin evidencia", "No evidence")
    : value === state.head ? tr("SHA actual · simulada", "Current SHA · simulated")
      : tr("Histórica · otro SHA", "Historical · different SHA");
  $("#external-declaration").textContent = evidenceState(state.declaration);
  $("#external-checks").textContent = evidenceState(state.checks);
  $("#external-review").textContent = evidenceState(state.approval);
  $("#external-register").disabled = state.registered;
  for (const [id, value] of [["declare", state.declaration], ["check", state.checks], ["approve", state.approval]]) {
    $("#external-" + id).disabled = !state.registered || state.firstMerged || value === state.head;
  }
  $("#external-new-head").disabled = !state.registered || state.firstMerged;
  $("#external-policy").disabled = !state.registered;
  $("#external-complete").disabled = !state.registered;
  $("#external-policy").checked = state.policy;
  $("#external-complete").checked = state.complete;
  $("#external-policy-state").textContent = state.policy
    ? tr("Habilitada (simulación)", "Enabled (simulation)") : tr("Deshabilitada", "Disabled");
  $("#external-project-state").textContent = !state.policy ? tr("Sin cambios", "Unchanged")
    : !state.registered ? tr("Pendiente", "Pending")
      : state.firstMerged ? tr("Integrado · opción ficticia", "Merged · fictional option")
        : tr("En revisión · opción ficticia", "In review · fictional option");
  const merged = tr("fusionado", "merged"), open = tr("abierto", "open");
  $("#external-merge-state").textContent = `#34: ${state.firstMerged ? merged : open} · #35: ${state.secondMerged ? merged : open}`;
  $("#external-issue-state").textContent = state.issueClosed ? tr("Cerrada (simulación)", "Closed (simulation)") : tr("Abierta", "Open");
  $("#external-merge-first").disabled = !state.registered || state.firstMerged;
  $("#external-merge-second").disabled = !state.registered || state.secondMerged;
  $("#external-closure-reason").textContent = state.issueClosed
    ? tr("Se verificaron ambas fusiones y la declaración humana con la política habilitada. No se reabre automáticamente.", "Both merges and the human declaration were verified with the policy enabled. It is never reopened automatically.")
    : !state.policy ? tr("Política deshabilitada: vincular o fusionar no activa la sincronización.", "Policy disabled: linking or merging does not activate synchronization.")
      : !state.complete ? tr("Avance parcial: falta declarar la resolución completa.", "Partial work: a complete-resolution declaration is missing.")
        : tr("La issue permanece abierta hasta verificar el merge de todos los PR vinculados.", "The issue stays open until every linked PR has a verified merge.");
  renderReviewDemo();
  $("#external-result").textContent = state.lastAction
    ? tr("Escenario ficticio actualizado. No se realizó ninguna operación remota. Reiniciar demo restaura todos los pasos.", "Fictional scenario updated. No remote operation took place. Reset demo restores every step.") : "";
}

function modelEnabled(id) { return id === "balanced" || id === "detailed" && parityDemo.detailedEnabled; }
function modelLabel(id) { return id === "detailed" ? tr("Detallado · ficticio", "Detailed · fictional") : tr("Equilibrado · ficticio", "Balanced · fictional"); }
function reviewerReady() { return parityDemo.reviewEnabled && modelEnabled(parityDemo.reviewer); }
function appendModelOptions(select, current, empty = false) {
  select.replaceChildren();
  if (empty) { const option=node("option",tr("Sin configurar", "Not configured")); option.value=""; select.append(option); }
  for (const id of ["balanced","detailed"]) { const option=node("option",modelLabel(id)+(modelEnabled(id)?"":tr(" · deshabilitado", " · disabled"))); option.value=id; option.disabled=!modelEnabled(id); select.append(option); }
  select.value=current;
}
function renderModelDemo() {
  $("#demo-detailed-model").checked=parityDemo.detailedEnabled;
  const focusedRole=document.activeElement?.dataset.demoRole;
  const fields=$("#demo-role-selectors"); fields.replaceChildren();
  for (const [role,es,en] of [["planner","Planificar","Plan"],["executor","Ejecutar","Execute"],["verifier","Verificar","Verify"],["documenter","Documentar","Document"],["compactor","Compactar contexto","Compact context"]]) {
    const label=node("label",tr(es,en)); const select=node("select"); select.dataset.demoRole=role; select.setAttribute("aria-label",tr(es,en)); appendModelOptions(select,parityDemo.roles[role]);
    select.addEventListener("change",()=>{parityDemo.roles[role]=select.value; parityDemo.modelNotice="role_updated"; renderChats(); renderTask();});
    label.append(select); fields.append(label);
  }
  if(focusedRole) fields.querySelector(`[data-demo-role="${focusedRole}"]`)?.focus();
  appendModelOptions($("#demo-review-model"),parityDemo.reviewer,true);
  $("#demo-enable-review").checked=parityDemo.reviewEnabled;
  $("#demo-enable-review").disabled=!modelEnabled(parityDemo.reviewer) && !parityDemo.reviewEnabled;
  $("#demo-review-config-state").textContent=reviewerReady() ? tr("Revisión asesora activada para este escenario. No habilita publicación de comentarios ni aprobación humana.", "Advisory review is enabled in this scenario. It grants neither comment publication nor human approval.") : !parityDemo.reviewer ? tr("Elige un modelo habilitado antes de activar la revisión. Comienza desactivada.", "Choose an enabled model before opting in. Review starts off.") : !modelEnabled(parityDemo.reviewer) ? tr("El modelo elegido está deshabilitado; no se puede solicitar otra revisión.", "The selected model is disabled; another review cannot be requested.") : tr("Modelo disponible, revisión desactivada. Activarla es una decisión separada.", "Model available, review off. Enabling review is a separate decision.");
  $("#demo-model-result").textContent=Object.values(parityDemo.roles).some(id=>!modelEnabled(id)) ? tr("Hay un rol seleccionado cuyo modelo está deshabilitado. Configurar no prueba disponibilidad; el próximo envío necesita un ejecutor habilitado.", "A selected role uses a disabled model. Configuration does not test availability; the next send needs an enabled executor.") : parityDemo.modelNotice ? tr("Rol actualizado sólo en memoria. No se probó ni llamó a ningún modelo.", "Role updated in page memory only. No model was tested or called.") : "";
}
function renderNotificationDemo() {
  $("#demo-notify-recipient").checked=parityDemo.notifyRecipient;
  $("#demo-notify-recipient").disabled=!recipientAssigned() && !parityDemo.notifyRecipient;
  $("#demo-notification-config").textContent=!recipientAssigned() ? tr("Dev demo necesita estar activo y asignado a AuroraDemo. Revisa Equipo; restaurar conserva las asignaciones, pero no reactiva el acceso.", "Dev demo must be active and assigned to AuroraDemo. Check Team; restoring preserves assignments but does not reactivate access.") : parityDemo.notifyRecipient ? tr("Destinatario local ficticio configurado. Un resultado asesor puede añadir un aviso a esta bandeja.", "Fictional local recipient configured. An advisory result can add a notice to this inbox.") : tr("Sin destinatarios: no se añadirá ningún aviso.", "No recipients: no notice will be added.");
  const list=$("#demo-notification-list"); list.replaceChildren();
  const visible=recipientAssigned() ? parityDemo.notices : [];
  if (!visible.length) list.append(node("p",tr("Bandeja vacía en este escenario.", "Empty inbox in this scenario."),"demo-empty"));
  for (const notice of visible) {
    const item=node("article",null,"demo-notice"); item.append(node("strong",tr("Revisión ficticia de AuroraDemo", "Fictional AuroraDemo review")),node("p",`${notice.sha === externalDemo.head ? tr("PR #34 · SHA actual", "PR #34 · current SHA") : tr("PR #34 · SHA histórico", "PR #34 · historical SHA")} ${notice.sha.toString(16).padStart(40,"0")}`),node("span",notice.read?tr("Leído", "Read"):tr("Sin leer", "Unread")));
    const button=node("button",tr("Marcar leído", "Mark read"),"text-button"); button.type="button"; button.disabled=notice.read; button.addEventListener("click",()=>{notice.read=true; renderNotificationDemo();}); item.append(button); list.append(item);
  }
}
function renderReviewDemo() {
  const state=externalDemo; const current=state.reviewHead===state.head; const queued=state.reviewState==="queued" && current;
  const runReady=parityDemo.reviewEnabled && modelEnabled(state.reviewModel);
  $("#demo-review-readiness").textContent=queued && !runReady ? tr("El modelo fijado en este intento ya no está habilitado, o se desactivó la revisión. No se completa el intento con otro modelo; simula su fallo y reintenta explícitamente.", "The model bound to this attempt is disabled, or review was turned off. This attempt is not completed with another model; simulate failure and explicitly retry.") : reviewerReady()?tr("Modelo ficticio habilitado y revisión activada para AuroraDemo.", "Fictional model enabled and review opted in for AuroraDemo."):tr("Primero configura un modelo y activa la revisión asesora en Modelos y bandeja.", "First configure a model and opt in under Models and inbox.");
  const labels={idle:tr("Sin solicitar", "Not requested"),queued:tr("En cola · simulada", "Queued · simulated"),findings:tr("Hallazgos asesores · simulados", "Advisory findings · simulated"),failed:tr("Falló · simulación", "Failed · simulation")};
  $("#demo-review-state").textContent=state.reviewHead && !current ? tr("Histórica · el head cambió", "Historical · head changed") : labels[state.reviewState];
  $("#demo-review-sha").textContent=state.reviewHead ? state.reviewHead.toString(16).padStart(40,"0") : "—";
  $("#demo-request-review").textContent=state.reviewState==="failed" && current?tr("Reintentar revisión explícitamente", "Explicitly retry review"):tr("Solicitar revisión ficticia", "Request fictional review");
  $("#demo-request-review").disabled=!state.registered || state.firstMerged || !reviewerReady() || queued || state.reviewState==="findings" && current;
  $("#demo-review-complete").disabled=!queued || !runReady; $("#demo-review-fail").disabled=!queued;
  $("#demo-review-result").textContent=state.reviewState==="findings"?tr("Guion: falta una referencia al criterio de aceptación en el ejemplo. Revisión del diff/blob ficticio, sin herramientas. Es asesoría; no acredita pruebas, aprobación ni el head nuevo.", "Script: the sample is missing an acceptance-criterion reference. Review of a fictional diff/blob, without tools. Advisory only: it proves no checks, approval, or new head."):state.reviewState==="failed"?tr("Fallo ficticio del revisor. El registro y la evidencia permanecen; reintentar requiere otra acción explícita para el SHA actual.", "Fictional reviewer failure. Registration and evidence remain; retry requires another explicit action for the current SHA."):queued?tr("Solicitud ficticia en cola. Elige un resultado del escenario; no se lanzó ningún modelo.", "Fictional request queued. Choose a scenario result; no model was started."):"";
}
function renderParitySurfaces() {
  $("#archive-person").textContent=parityDemo.memberArchived?tr("Restaurar persona ficticia", "Restore fictional person"):tr("Archivar persona ficticia", "Archive fictional person");
  $("#edit-alex").disabled=parityDemo.memberArchived;
  $("#activate-person").hidden=parityDemo.memberArchived || parityDemo.memberEnabled;
  $("#archive-result").textContent=parityDemo.memberArchived
    ? tr("Persona archivada: conserva sus asignaciones, sin acceso. Restaurar vuelve a mostrarla, pero no reactiva su acceso.", "Archived person: assignments preserved, without access. Restoring makes the person visible but does not reactivate access.")
    : !parityDemo.memberEnabled
      ? tr("Persona restaurada, todavía sin acceso. Las asignaciones se conservaron. Activar acceso ficticio es una acción separada y no ejecuta tareas.", "Person restored, still without access. Assignments were preserved. Enable fictional access is a separate action and does not run tasks.")
      : tr("Escenario de equipo: archivar conserva datos y asignaciones, y retira el acceso. Restaurar no lo reactiva.", "Team scenario: archiving preserves data and assignments, and removes access. Restoring does not reactivate it.");
  filterDemoProjects(); renderModelDemo(); renderNotificationDemo(); renderReviewDemo();
}
function filterDemoProjects() {
  const query=$("#project-filter").value.trim().toLocaleLowerCase(); let count=0;
  for (const option of $("#project-picker").options) { option.hidden=!option.textContent.toLocaleLowerCase().includes(query); if (!option.hidden) count++; }
  $("#project-filter-empty").textContent=count?"":tr("Sin proyectos que coincidan. Borra el filtro para recuperar la lista; no se cambia la selección.", "No matching projects. Clear the filter to recover the list; selection is unchanged.");
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
  document.title = tr("FourBis Relay — Tu equipo, tus repositorios y la IA", "FourBis Relay — Your team, repositories, and AI");
  renderChats(); renderTask(); renderExternalDemo();
}

$("#language").addEventListener("click", () => {
  english = !english;
  const url = new URL(location.href);
  english ? url.searchParams.set("lang", "en") : url.searchParams.delete("lang");
  history.replaceState(null, "", url);
  translate();
});
document.querySelectorAll("[data-open]").forEach(button => button.addEventListener("click", () => {
  const id = button.dataset.open;
  if (!canReadDemo(chats[id])) return;
  parityDemo.scope = parityDemo.actor === "owner" ? "mine" : "shared";
  active = id;
  chats[active].open = true;
  selectScene("chats");
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
$("#save-projects").addEventListener("click", () => { alexProjects = new Set([...$("#project-picker").selectedOptions].map(option => option.value)); if (!alexProjects.has("web")) { writeEnabled = false; continued = false; } $("#edit-project-panel").hidden = true; renderTask(); renderChats(); });
$("#connect-github").addEventListener("click", () => { githubConnected = true; renderTask(); });
$("#allow-write").addEventListener("click", () => { if (recipientAssigned() && githubConnected) writeEnabled = true; renderTask(); renderChats(); });
$("#continue-task").addEventListener("click", () => { if (recipientAssigned() && writeEnabled && modelEnabled(parityDemo.roles.executor)) continued = true; renderTask(); });
$("#publish-task").addEventListener("click", () => { published = true; renderTask(); });
$("#feedback").addEventListener("click", () => { feedback = true; renderTask(); });
for (const [id, action] of [["register", "register"], ["declare", "declare"], ["check", "check"], ["approve", "approve"], ["new-head", "new_head"], ["merge-first", "merge_first"], ["merge-second", "merge_second"]]) {
  $("#external-" + id).addEventListener("click", () => {
    externalDemo = reduceExternalDemo(externalDemo, action);
    renderExternalDemo();
  });
}
for (const action of ["policy", "complete"]) {
  $("#external-" + action).addEventListener("change", event => {
    externalDemo = reduceExternalDemo(externalDemo, action, event.target.checked);
    renderExternalDemo();
  });
}
$("#reset").addEventListener("click", () => {
  chats = initialChats(); active = "docs"; turns = 1; feedback = false; split = false; continued = false; published = false; githubConnected = false; writeEnabled = false; alexProjects = new Set();
  $("#edit-project-panel").hidden = true; for (const option of $("#project-picker").options) option.selected = false;
  externalDemo = initialExternalDemo(); parityDemo=initialParityDemo(); $("#project-filter").value="";
  renderChats(); renderTask(); renderExternalDemo(); selectScene("chats");
});

$("#demo-actor").addEventListener("change",event=>{parityDemo.actor=event.target.value; parityDemo.scope=parityDemo.actor==="owner"?"mine":"shared"; renderChats();});
$("#demo-scope").addEventListener("change",event=>{parityDemo.scope=event.target.value; renderChats();});
$("#demo-send-outcome").addEventListener("change",event=>{parityDemo.outcome=event.target.value;});
$("#demo-share-mode").addEventListener("change",()=>{
  const mode=$("#demo-share-mode").value;
  $("#demo-save-sharing").disabled=!canShareDemoMode(mode);
  $("#demo-sharing-result").textContent = mode === "collaborate" && recipientAssigned() && !writeEnabled
    ? tr("Primero habilita el permiso de escritura ficticio en Equipo; un hilo de consulta sólo admite compartir lectura.", "First enable the fictional write permission in Team; a read-only conversation can only be shared for reading.") : !recipientAssigned() && mode !== "none"
      ? tr("Primero asigna a Dev demo a AuroraDemo en Equipo. Una persona archivada o sin acceso activo no es elegible.", "First assign Dev demo to AuroraDemo in Team. Archived people or people without active access are not eligible.") : "";
});
$("#demo-save-sharing").addEventListener("click",()=>{if (parityDemo.actor!=="owner") return; const mode=$("#demo-share-mode").value; if (!canShareDemoMode(mode)) return; chats[active].sharedMode=mode; chats[active].sharingNote=mode==="none"?"removed":"saved"; renderChats();});
$("#demo-go-team").addEventListener("click",()=>{selectScene("team",true); $("#edit-alex").focus();});
$("#project-filter").addEventListener("input",filterDemoProjects);
$("#archive-person").addEventListener("click",()=>{parityDemo.memberArchived=!parityDemo.memberArchived; if(parityDemo.memberArchived){parityDemo.memberEnabled=false; continued=false; $("#edit-project-panel").hidden=true;} renderChats(); renderTask();});
$("#activate-person").addEventListener("click",()=>{if (parityDemo.memberArchived) return; parityDemo.memberEnabled=true; renderChats(); renderTask();});
$("#demo-detailed-model").addEventListener("change",event=>{parityDemo.detailedEnabled=event.target.checked; renderChats(); renderTask();});
$("#demo-review-model").addEventListener("change",event=>{parityDemo.reviewer=event.target.value; renderModelDemo(); renderReviewDemo();});
$("#demo-enable-review").addEventListener("change",event=>{if(event.target.checked && !modelEnabled(parityDemo.reviewer)) return; parityDemo.reviewEnabled=event.target.checked; renderModelDemo(); renderReviewDemo();});
$("#demo-notify-recipient").addEventListener("change",event=>{if(event.target.checked && !recipientAssigned()) return; parityDemo.notifyRecipient=event.target.checked; renderNotificationDemo();});
$("#demo-request-review").addEventListener("click",()=>{if(!reviewerReady() || !externalDemo.registered || externalDemo.firstMerged || externalDemo.reviewState==="queued" && externalDemo.reviewHead===externalDemo.head) return; externalDemo=reduceExternalDemo(externalDemo,"review_queue",parityDemo.reviewer); renderExternalDemo();});
$("#demo-review-complete").addEventListener("click",()=>{if(!parityDemo.reviewEnabled || !modelEnabled(externalDemo.reviewModel) || externalDemo.reviewState!=="queued" || externalDemo.reviewHead!==externalDemo.head) return; externalDemo=reduceExternalDemo(externalDemo,"review_complete"); if(parityDemo.notifyRecipient && recipientAssigned()) parityDemo.notices.push({sha:externalDemo.head, read:false}); renderExternalDemo(); renderNotificationDemo();});
$("#demo-review-fail").addEventListener("click",()=>{if(externalDemo.reviewState!=="queued" || externalDemo.reviewHead!==externalDemo.head) return; externalDemo=reduceExternalDemo(externalDemo,"review_fail"); renderExternalDemo();});
document.querySelectorAll("[data-demo-scene]").forEach(button=>button.addEventListener("click",()=>selectScene(button.dataset.demoScene,true)));

translate();
