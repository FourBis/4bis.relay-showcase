// Cuentas personales: Gmail se consulta desde esta vista; los borradores
// preparados se conservan temporalmente en memoria para revisarlos y enviarlos.
import { $, api, escape } from "./api.js";
import { confirmModal, emptyState, field, formSection, skeletonRows, toast } from "./ui.js";

const PROVIDERS = new Set(["github", "google"]);
const PROVIDER_NAMES = { github: "GitHub", google: "Google / Gmail" };
const PROVIDER_PATHS = {
  github: ["/login/oauth/authorize"],
  google: ["/o/oauth2/v2/auth", "/o/oauth2/auth"],
};
let actor = null;
let snapshot = null;
let draftsSnapshot = [];
let currentQuery = "";
let generation = 0;
const SEND_REQUESTS_KEY = "relay.account.mail-send-ids.v1";
const SEND_REQUESTS_LIMIT = 50;
const attr = value => escape(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");

export function parseRecipients(value, required = false) {
  const recipients = String(value || "").split(/[;,]/).map(part => part.trim()).filter(Boolean);
  if (required && !recipients.length) return { ok: false, recipients: [], error: "Ingresa al menos un destinatario." };
  if (recipients.some(address => !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(address))) {
    return { ok: false, recipients: [], error: "Revisa las direcciones; sepáralas con coma o punto y coma." };
  }
  return { ok: true, recipients };
}

async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function readSendRequests(storage) {
  const raw = storage.getItem(SEND_REQUESTS_KEY);
  if (!raw) return [];
  const rows = JSON.parse(raw);
  if (!Array.isArray(rows) || rows.some(row => !Array.isArray(row) || row.length !== 2
    || !/^[a-f0-9]{64}$/.test(row[0])
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(row[1]))) {
    throw new Error("El registro seguro de reintentos no se pudo leer.");
  }
  return rows;
}

export async function requestIdForDraft(draft, email, storage,
  makeId = () => crypto.randomUUID(), hash = sha256) {
  try {
    if (storage === undefined) storage = globalThis.localStorage;
    if (!storage || !email) throw new Error("No se pudo identificar tu cuenta para proteger el reintento.");
    const payload = JSON.stringify([email.trim().toLowerCase(), draft.to, draft.cc,
      draft.bcc, draft.subject, draft.body]);
    const fingerprint = await hash(payload);
    const rows = readSendRequests(storage);
    const previous = rows.find(row => row[0] === fingerprint);
    if (previous) return { id: previous[1], fingerprint };
    if (rows.length >= SEND_REQUESTS_LIMIT) {
      throw new Error("Hay demasiados envíos sin confirmación guardados. Revisa Gmail antes de enviar otro correo.");
    }
    const id = makeId();
    storage.setItem(SEND_REQUESTS_KEY, JSON.stringify([...rows, [fingerprint, id]]));
    return { id, fingerprint };
  } catch (error) {
    return { error: error?.message || "No se pudo guardar el identificador seguro de reintento." };
  }
}

export function forgetConfirmedSend(fingerprint, storage) {
  try {
    if (storage === undefined) storage = globalThis.localStorage;
    if (!storage) return false;
    const rows = readSendRequests(storage).filter(row => row[0] !== fingerprint);
    storage.setItem(SEND_REQUESTS_KEY, JSON.stringify(rows));
    return true;
  } catch {
    return false;
  }
}

export function isAuthorizationUrl(provider, value) {
  if (!PROVIDERS.has(provider)) return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      && url.hostname === (provider === "github" ? "github.com" : "accounts.google.com")
      && PROVIDER_PATHS[provider].includes(url.pathname);
  } catch {
    return false;
  }
}

function shellMarkup() {
  return `<div id="account-identity" class="muted mb-4">Cargando tus conexiones…</div>
    <div id="account-connections" class="grid gap-3 md:grid-cols-2" aria-live="polite"></div>
    <section id="account-gmail" class="mt-5"></section>`;
}

function connectionMarkup(connection) {
  const status = connection.needs_reconnect ? "Necesita reconexión"
    : connection.connected ? `Conectada${connection.email || connection.login
      ? ` · ${connection.email || connection.login}` : ""}`
      : connection.configured ? "Sin vincular" : "No disponible";
  const badge = connection.needs_reconnect ? "warn" : connection.connected ? "ok" : "dim";
  let actions = "";
  if (!snapshot.local_identity && connection.can_connect) {
    actions += `<button class="btn btn-primary" type="button" data-account-action="connect" data-provider="${attr(connection.provider)}">${connection.connected ? "Volver a conectar" : "Conectar"}</button>`;
  }
  if (!snapshot.local_identity && connection.connected) {
    actions += ` <button class="btn danger" type="button" data-account-action="disconnect" data-provider="${attr(connection.provider)}">Desconectar</button>`;
  }
  const explanation = connection.message || (connection.provider === "github"
    ? "Usa tu identidad de GitHub para las operaciones que admitan credenciales personales."
    : "Conecta tu Gmail para buscar, leer y enviar correo desde Mi cuenta.");
  return `<article class="form-section">
    <header class="form-section-head"><h3 class="form-section-title">${escape(connection.label || PROVIDER_NAMES[connection.provider] || connection.provider)}</h3>
      <p class="form-section-sub"><span class="badge ${badge}">${escape(status)}</span></p></header>
    <p class="muted mb-3">${escape(explanation)}</p>
    <div class="field-actions">${actions || `<span class="muted">${snapshot.local_identity ? "Entra por Cloudflare Access para vincular cuentas personales." : escape(status)}</span>`}</div>
  </article>`;
}

function googleConnection() {
  return snapshot?.connections?.find(connection => connection.provider === "google") || null;
}

function showGmail() {
  const google = googleConnection();
  return !snapshot?.local_identity && Boolean(google?.connected && !google.needs_reconnect);
}

function renderGmailShell() {
  const root = $("#account-gmail");
  if (!showGmail()) {
    root.innerHTML = formSection({ title: "Gmail personal",
      sub: "Tu correo personal solo se muestra en esta vista.", grid: false,
      body: emptyState({ title: "Conecta tu cuenta de Google",
        sub: snapshot?.local_identity
          ? "Entra a Relay con tu correo de Cloudflare Access para usar una cuenta personal."
          : "Cuando Google esté conectado, podrás buscar, leer y redactar correo aquí." }) });
    return;
  }
  const searchField = field({ label: "Buscar en Gmail", id: "account-mail-query",
    control: '<input id="account-mail-query" class="input" type="search" autocomplete="off" placeholder="Remitente, asunto o texto">' });
  root.innerHTML = `<section class="form-section">
    <header class="form-section-head"><div><h3 class="form-section-title">Gmail personal</h3>
      <p class="form-section-sub">Conectada como ${escape(googleConnection()?.email || snapshot.email)} · Los mensajes de Gmail no se almacenan. Los borradores preparados desde chats quedan disponibles hasta una hora.</p></div>
      <button class="btn btn-primary" type="button" data-account-action="compose">Redactar</button></header>
    <div id="account-drafts" class="mt-3"></div>
    <form id="account-mail-search" class="toolbar">${searchField}<button class="btn" type="submit">Buscar correo</button></form>
    <div id="account-compose" class="mt-4" hidden></div>
    <div id="account-mail-detail" class="mt-4" hidden></div>
    <p id="account-mail-status" class="muted mt-3" role="status" aria-live="polite"></p>
    <div id="account-mail-results" class="table-wrap mt-3"><table>
      <thead><tr><th class="th">Remitente</th><th class="th">Asunto</th><th class="th">Fecha</th><th class="th"></th></tr></thead>
      <tbody>${skeletonRows(4, 3)}</tbody>
    </table></div>
  </section>`;
}

function recipientsText(value) {
  return Array.isArray(value) ? value.join(", ") : String(value || "");
}

async function loadDrafts() {
  const root = $("#account-drafts");
  if (!root) return;
  root.innerHTML = '<p class="muted">Cargando borradores preparados…</p>';
  try {
    const data = await api("account/gmail/drafts");
    draftsSnapshot = Array.isArray(data.drafts) ? data.drafts : [];
    root.innerHTML = draftsSnapshot.length ? `<section class="form-section"><header class="form-section-head"><div><h4 class="form-section-title">Borradores preparados desde chats</h4><p class="form-section-sub">Revísalos y cárgalos para confirmar el envío.</p></div></header><ul class="divide-y divide-zinc-800">${draftsSnapshot.map(draft => `<li class="flex items-center justify-between gap-3 py-2"><span class="min-w-0"><strong class="block truncate text-sm">${escape(draft.subject || "(sin asunto)")}</strong><span class="muted block truncate">Para: ${escape(recipientsText(draft.to))}</span></span><button class="btn btn-xs shrink-0" type="button" data-account-action="load-draft" data-draft-id="${attr(draft.draft_id)}">Revisar borrador</button></li>`).join("")}</ul></section>` : "";
  } catch (error) {
    draftsSnapshot = [];
    root.innerHTML = `<p class="fail" role="status">No se pudieron cargar los borradores: ${escape(error.message)}</p>`;
  }
}

function loadDraft(draftId) {
  const draft = draftsSnapshot.find(item => item.draft_id === draftId);
  if (!draft) return;
  const compose = $("#account-compose");
  compose.innerHTML = composeMarkup(); compose.hidden = false;
  $("#account-mail-to").value = recipientsText(draft.to);
  $("#account-mail-cc").value = recipientsText(draft.cc);
  $("#account-mail-bcc").value = recipientsText(draft.bcc);
  $("#account-mail-subject").value = draft.subject || "";
  $("#account-mail-body").value = draft.body || "";
  $("#account-mail-to").focus();
}

function renderMessages(messages) {
  const tbody = $("#account-mail-results tbody");
  if (!tbody) return;
  if (!messages.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="!p-0">${emptyState({ title: "No hay mensajes", sub: currentQuery ? "Prueba otra búsqueda." : "Tu bandeja no tiene mensajes disponibles." })}</td></tr>`;
    return;
  }
  tbody.innerHTML = messages.map(message => `<tr>
    <td class="px-3 py-2 text-sm">${escape(message.from || "—")}</td>
    <td class="px-3 py-2"><div class="text-sm text-zinc-100">${escape(message.subject || "(sin asunto)")}</div><div class="muted truncate max-w-xl">${escape(message.snippet || "")}</div></td>
    <td class="px-3 py-2 text-sm text-zinc-400">${escape(message.date || "—")}</td>
    <td class="px-3 py-2 text-right"><button class="btn btn-xs" type="button" data-account-action="open-message" data-message-id="${attr(message.id)}">Abrir</button></td>
  </tr>`).join("");
}

async function loadMessages(query = currentQuery) {
  currentQuery = String(query || "").trim();
  const results = $("#account-mail-results");
  const status = $("#account-mail-status");
  if (!results || !status) return;
  const request = ++generation;
  const detail = $("#account-mail-detail");
  if (detail) { detail.replaceChildren(); detail.hidden = true; }
  const compose = $("#account-compose");
  if (compose) { compose.replaceChildren(); compose.hidden = true; }
  status.textContent = "Cargando mensajes…";
  results.querySelector("tbody").innerHTML = skeletonRows(4, 3);
  try {
    const params = new URLSearchParams({ q: currentQuery, limit: "10" });
    const data = await api(`account/gmail/messages?${params}`);
    if (request !== generation) return;
    renderMessages(Array.isArray(data.messages) ? data.messages : []);
    status.textContent = data.email ? `Buzón: ${data.email}` : "";
  } catch (error) {
    if (request !== generation) return;
    draftsSnapshot = [];
    const drafts = $("#account-drafts");
    if (drafts) drafts.replaceChildren();
    results.querySelector("tbody").innerHTML = `<tr><td colspan="4" class="!p-0">${emptyState({ title: "No se pudo cargar Gmail", sub: error.message, action: '<button class="btn" type="button" data-account-action="retry-mail">Reintentar</button>' })}</td></tr>`;
    status.textContent = "";
  }
}

async function openMessage(id) {
  const detail = $("#account-mail-detail");
  if (!detail) return;
  detail.hidden = false;
  detail.innerHTML = `<section class="form-section"><p class="muted">Cargando mensaje…</p></section>`;
  try {
    const data = await api(`account/gmail/messages/${encodeURIComponent(id)}`);
    detail.innerHTML = `<section class="form-section">
      <header class="form-section-head"><div><h3 class="form-section-title">${escape(data.subject || "(sin asunto)")}</h3><p class="form-section-sub">De ${escape(data.from || "—")} · Para ${escape(data.to || "—")} · ${escape(data.date || "—")}</p></div>
        <button class="btn" type="button" data-account-action="close-message">Cerrar</button></header>
      <pre class="whitespace-pre-wrap break-words text-sm text-zinc-200">${escape(data.body || "")}</pre>
    </section>`;
  } catch (error) {
    detail.innerHTML = `<section class="form-section"><p class="fail" role="alert">${escape(error.message)}</p><button class="btn" type="button" data-account-action="close-message">Cerrar</button></section>`;
  }
}

function composeMarkup() {
  const from = googleConnection()?.email || snapshot?.email || "";
  const body = `<form id="account-mail-compose" class="form-grid" novalidate>
    ${field({ label: "De", id: "account-mail-from", control: `<input id="account-mail-from" class="input" value="${escape(from)}" readonly>` })}
    ${field({ label: "Para", id: "account-mail-to", required: true, hint: "Puedes separar varias direcciones con coma o punto y coma.", control: '<input id="account-mail-to" class="input" type="text" inputmode="email" autocomplete="off">' })}
    ${field({ label: "CC", id: "account-mail-cc", control: '<input id="account-mail-cc" class="input" type="text" inputmode="email" autocomplete="off">' })}
    ${field({ label: "CCO", id: "account-mail-bcc", control: '<input id="account-mail-bcc" class="input" type="text" inputmode="email" autocomplete="off">' })}
    ${field({ label: "Asunto", id: "account-mail-subject", required: true, control: '<input id="account-mail-subject" class="input" type="text" maxlength="998" autocomplete="off">' })}
    ${field({ label: "Mensaje", id: "account-mail-body", required: true, control: '<textarea id="account-mail-body" class="input min-h-48" rows="8"></textarea>' })}
    <p id="account-mail-compose-status" class="field-error" role="status" aria-live="polite"></p>
  </form>`;
  return formSection({ title: "Redactar correo", sub: "Revisa el contenido y pulsa Enviar correo. Relay no enviará sin esa acción.", body,
    grid: false, actions: '<button id="account-mail-send" class="btn btn-primary" type="submit" form="account-mail-compose">Enviar correo</button> <button class="btn" type="button" data-account-action="cancel-compose">Cancelar</button>' });
}

async function sendMail(event) {
  event.preventDefault();
  if (!showGmail()) return;
  const to = parseRecipients($("#account-mail-to").value, true);
  const cc = parseRecipients($("#account-mail-cc").value);
  const bcc = parseRecipients($("#account-mail-bcc").value);
  const subject = $("#account-mail-subject").value.trim();
  const body = $("#account-mail-body").value;
  const status = $("#account-mail-compose-status");
  if (!to.ok || !cc.ok || !bcc.ok || !subject || !body.trim()) {
    status.textContent = !to.ok ? to.error : !cc.ok ? cc.error : !bcc.ok ? bcc.error
      : !subject ? "Escribe un asunto." : "Escribe el mensaje.";
    return;
  }
  const draft = { from: googleConnection()?.email || snapshot.email, to: to.recipients,
    cc: cc.recipients, bcc: bcc.recipients, subject, body };
  const request = await requestIdForDraft(draft, snapshot?.email);
  if (request.error) {
    status.textContent = `${request.error} No se envió el correo.`;
    return;
  }
  const button = $("#account-mail-send");
  button.disabled = true;
  status.textContent = "Enviando…";
  try {
    const result = await api("account/gmail/send", { method: "POST", body: {
      request_id: request.id, to: draft.to, cc: draft.cc, bcc: draft.bcc,
      subject: draft.subject, body: draft.body,
    } });
    if (result?.state !== "sent" || !result.message_id) {
      throw new Error(result?.error || "Relay no confirmó que Gmail aceptó el envío.");
    }
    toast(`Correo enviado por ${googleConnection()?.email || draft.from}.`, "ok");
    forgetConfirmedSend(request.fingerprint);
    $("#account-mail-compose").reset();
    $("#account-compose").hidden = true;
    $("#account-compose").replaceChildren();
    await loadMessages(currentQuery);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function connect(provider) {
  if (!PROVIDERS.has(provider) || snapshot.local_identity) return;
  try {
    const data = await api(`account/${provider}/connect`, { method: "POST", body: {} });
    if (!isAuthorizationUrl(provider, data.authorization_url)) {
      throw new Error("El servidor devolvió una dirección de autorización no permitida.");
    }
    window.location.assign(data.authorization_url);
  } catch (error) {
    toast(`No se pudo iniciar la conexión: ${error.message}`, "err");
  }
}

async function disconnect(provider) {
  if (!PROVIDERS.has(provider) || snapshot.local_identity) return;
  if (!await confirmModal({ title: `¿Desconectar ${PROVIDER_NAMES[provider]}?`,
    body: "Relay retirará las credenciales guardadas para tu cuenta. Puedes volver a conectarla después.",
    confirmText: "Desconectar", danger: true })) return;
  try {
    await api(`account/${provider}`, { method: "DELETE" });
    toast(`${PROVIDER_NAMES[provider]} desconectado.`, "ok");
    await loadAccount();
  } catch (error) {
    toast(`No se pudo desconectar: ${error.message}`, "err");
  }
}

function wireRoot(root) {
  root.addEventListener("click", event => {
    const button = event.target.closest("[data-account-action]");
    if (!button) return;
    const action = button.dataset.accountAction;
    if (action === "connect") connect(button.dataset.provider);
    if (action === "disconnect") disconnect(button.dataset.provider);
    if (action === "compose") {
      const compose = $("#account-compose");
      compose.innerHTML = composeMarkup(); compose.hidden = false;
      $("#account-mail-to").focus();
    }
    if (action === "load-draft") loadDraft(button.dataset.draftId);
    if (action === "cancel-compose") {
      confirmModal({ title: "¿Cerrar este borrador?",
        body: "Si un envío quedó sin confirmar, Relay conserva su identificador para que el reintento no genere un envío duplicado. Revisa Gmail antes de volver a redactar el mismo correo con cambios.",
        confirmText: "Descartar borrador", danger: true }).then(ok => {
        if (!ok) return;
        $("#account-compose").replaceChildren(); $("#account-compose").hidden = true;
      });
    }
    if (action === "open-message") openMessage(button.dataset.messageId);
    if (action === "close-message") { const detail = $("#account-mail-detail"); detail.replaceChildren(); detail.hidden = true; }
    if (action === "retry-mail") loadMessages(currentQuery);
    if (action === "retry-account") loadAccount();
  });
  root.addEventListener("submit", event => {
    if (event.target.id === "account-mail-search") {
      event.preventDefault(); loadMessages($("#account-mail-query").value);
    }
    if (event.target.id === "account-mail-compose") sendMail(event);
  });
}

export function initAccount(currentActor) {
  actor = currentActor;
  const root = $("#account-root");
  if (!root || root.dataset.wired) return;
  root.dataset.wired = "true";
  root.innerHTML = shellMarkup();
  wireRoot(root);
}

export async function loadAccount() {
  const root = $("#account-root");
  if (!root) return;
  root.innerHTML = shellMarkup();
  snapshot = null;
  draftsSnapshot = [];
  const request = ++generation;
  $("#account-identity").textContent = "Actualizando tus conexiones…";
  $("#account-connections").innerHTML = '<div class="card p-4"><span class="skeleton block h-5 w-1/2"></span><span class="skeleton mt-3 block h-4 w-3/4"></span></div><div class="card p-4"><span class="skeleton block h-5 w-1/2"></span><span class="skeleton mt-3 block h-4 w-3/4"></span></div>';
  try {
    snapshot = await api("account/connections");
    if (request !== generation) return;
    const label = snapshot.local_identity ? "Acceso local de Relay" : snapshot.email;
    $("#account-identity").textContent = `Identidad Relay: ${label || actor?.email || "sin identificar"}`;
    $("#account-connections").innerHTML = (snapshot.connections || []).map(connectionMarkup).join("");
    renderGmailShell();
    if (showGmail()) {
      await Promise.all([loadMessages(currentQuery), loadDrafts()]);
    }
  } catch (error) {
    if (request !== generation) return;
    $("#account-identity").textContent = "No se pudieron consultar tus conexiones.";
    $("#account-connections").innerHTML = emptyState({ title: "No se pudo cargar Mi cuenta", sub: error.message,
      action: '<button class="btn" type="button" data-account-action="retry-account">Reintentar</button>' });
  }
}
