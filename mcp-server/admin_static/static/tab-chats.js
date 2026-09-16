// Tab Chat (2026-07-20e): workspace unificado tipo VS Code.
//
// Reemplaza los tabs Chats + Conversaciones. UNA sola forma de ver una
// conversación en toda la UI:
//   - Sidebar: lista de conversaciones (abiertas/cerradas, búsqueda).
//   - Panel: el hilo con burbujas user/experto + tool calls inline;
//     streaming en vivo mientras corre; input abajo. Cerrada = read-only
//     con el summary arriba y sin input.
//   - Memoria: facts + FTS5 del proyecto activo (toggle en el sidebar).
//
// "En curso" (tab-running) queda aparte como vista de operaciones.
//
// Exports usados por otros módulos:
//   - initChats / loadChats  → main.js (wiring + loader del tab).
//   - viewChat               → tab-running.js (abre el .md raw).

import { $, $$, on, api, apiRoot, escape, _dbg, openProjectInVSCode, gitWebUrl, fmtDuration } from "./api.js";
import { toast, openModal, closeModal, confirmModal, alertModal, modalLoad, emptyState } from "./ui.js";
import { chime, isMuted, toggleMute, pedirPermisoNotificaciones } from "./chime.js";
// En UNA línea a propósito: chat-strip-junk.test.mjs carga este módulo
// neutralizando los imports con un regex `^import .*$` por línea, y un
// import multilínea le deja huérfano el resto del bloque.
import { refreshQuestions, matchCommand, helpText, wireCommandPalette, paletteOpen, addCopyButtons } from "./chat-tools.js";
import { attachGrafo, detachGrafo, pokeGrafo, wireGrafoPanel, planEnCurso } from "./chat-grafo.js";
import { aplicarAnchosGuardados } from "./panel-resize.js";
import { openConvDiff } from "./chat-diff.js";
import { openChatObject } from "./chat-objects.js";

// --- estado ----------------------------------------------------------
// Ponytail: una conversación activa por vez (la UI es single-pane).
let activeChat = null;   // {convId, projectSlug, currentChatId, busy, readOnly}
let convCache = [];      // última lista de /conversations (para filtrar client-side)
// Catálogo de /admin/api/models. `chosenModel` vacío = "el del proyecto",
// que es el comportamiento de siempre: solo se manda `model` en el body
// cuando el humano eligió uno a mano, si no pisaríamos el
// defaults_json.model del proyecto sin que nadie lo haya pedido.
let modelCatalog = [];
// `globalThis.localStorage?.` y no `localStorage.` pelado: esto corre al
// IMPORTAR el módulo, y chat-strip-junk.test.mjs lo importa desde Node,
// donde no hay localStorage. Sin el guard el test entero muere con
// ReferenceError antes de llegar a stripJunk. Los otros usos viven
// dentro de handlers, que en Node no se ejecutan nunca.
let chosenModel = globalThis.localStorage?.getItem("chat.model") || "";
let chatObjectBackWired = false;
let chatSelectionGeneration = 0;

function selectionIsCurrent(generation, convId = null) {
  return generation === chatSelectionGeneration
    && (!convId || activeChat?.convId === convId);
}

// =====================================================================
// SIDEBAR: lista de conversaciones
// =====================================================================

export async function loadChats() {
  const list = $("#chat-conv-list");
  // Iter 10.2: loading placeholder visible. /conversations puede
  // tardar >5s si la DB está lockeada (modo nocturno escribiendo
  // facts, compactor, etc.) y la sidebar quedaba muda. Tres
  // "—" animados en lugar de texto plano: el motion lo distingue
  // del estado vacío "sin conversaciones todavía" que es estático.
  if (list) {
    list.innerHTML = `<p class="chat-conv-loading p-3 text-xs text-zinc-400">
      <span class="chat-typing"><span></span><span></span><span></span></span>
      cargando conversaciones…
    </p>`;
  }
  try {
    // Poblar el filtro de proyectos una sola vez. Su propio try: el
    // filtro es un extra, la lista de conversaciones es el tab. Bug
    // 2026-07-22: /admin/api/projects spawnea cbm y puede tardar >15s
    // (store lockeado por el watcher) — el throw se llevaba puesta la
    // sidebar entera y quedaba "el server no respondió" con las
    // conversaciones sin pedir.
    // El filtro es un `input` + `datalist` (52 proyectos no se buscan en
    // un select nativo). Se llena una sola vez; su propio try porque
    // /admin/api/projects spawnea cbm y puede tardar >15s — ver abajo.
    const sel = $("#chat-project-filter");
    const opts = $("#chat-project-opts");
    if (opts && !opts.options.length) {
      try {
        const { projects } = await api("projects");
        opts.innerHTML = projects.map((p) =>
          `<option value="${escape(p.slug)}"></option>`).join("");
      } catch (e) {
        _dbg("loadChats: filtro de proyectos no cargó", e.message);
      }
    }
    // Escrito a medias ("box") no es un slug: filtrar por eso devolvía
    // cero conversaciones mientras el humano seguía tipeando. Solo
    // filtra cuando coincide exacto con un proyecto, o cuando está vacío.
    const escrito = sel ? sel.value.trim() : "";
    const conocido = !escrito || !!opts?.querySelector(
      `option[value="${CSS.escape(escrito)}"]`);
    const project = conocido ? escrito : "";
    const q = "?limit=100" + (project ? `&project=${encodeURIComponent(project)}` : "");
    const r = await apiRoot("/conversations" + q);
    convCache = r.conversations || [];
    renderConvList();
  } catch (e) {
    _dbg("loadChats ERROR", e.message);
    if (list) list.innerHTML =
      `<p class="p-3 text-xs text-red-400">error: ${escape(e.message)}</p>`;
  }
}

/** El número del rail: cuántas abiertas hay, para verlo colapsado. */
function pintarRail() {
  const el = $("#chat-rail-count");
  if (el) el.textContent = String(convCache.filter((c) => c.status === "open").length);
}

function renderConvList() {
  pintarRail();
  const list = $("#chat-conv-list");
  if (!list) return;
  const term = ($("#chat-search")?.value || "").trim().toLowerCase();
  const filtered = convCache.filter((c) => {
    if (!term) return true;
    return (c.project_slug || "").toLowerCase().includes(term)
      || (c.summary || "").toLowerCase().includes(term)
      || (c.id || "").toLowerCase().includes(term);
  });
  if (!filtered.length) {
    list.innerHTML =
      `<p class="p-3 text-xs text-zinc-400">${
        term ? "sin conversaciones que matcheen." :
        "sin conversaciones todavía. Crea una con ➕ Nuevo."}</p>`;
    return;
  }
  const open = filtered.filter((c) => c.status === "open");
  const closed = filtered.filter((c) => c.status !== "open");
  let html = "";
  if (open.length) html += group("Abiertas", open);
  if (closed.length) html += group("Cerradas", closed);
  list.innerHTML = html;
  $$(".chat-conv-item").forEach((el) =>
    el.addEventListener("click", () => selectConversation(el.dataset.id)));
}

function group(title, convs) {
  return `<div class="chat-conv-group">${title} · ${convs.length}</div>` +
    convs.map((c) => {
      const active = activeChat && activeChat.convId === c.id ? " active" : "";
      const dot = c.status === "open" ? "open" : "closed";
      const when = shortWhen(c.last_activity_at);
      const preview = c.summary
        ? escape(c.summary.slice(0, 80))
        // Singular con 1: la sidebar lo muestra en cada fila, asi que
        // "1 mensajes" se ve todo el tiempo.
        : `<span class="text-zinc-400">${c.messages_len ?? 0} ${(c.messages_len ?? 0) === 1 ? "mensaje" : "mensajes"}</span>`;
      // Iter 10.3: chip de rama en la sidebar para que sepas de un
      // vistazo qué ramas quedaron locales sin borrar. Solo muestro
      // el nombre — el "merged/ahead" lo da el panel al seleccionar.
      // Las abiertas NO muestran chip: la rama es la activa, no hay
      // sorpresa; el panel ya la señala.
      const branchChip = c.branch
        ? `<span class="cci-branch" title="rama local: ${escape(c.branch)}">🌿 ${escape(c.branch)}</span>`
        : "";
      return `<div class="chat-conv-item${active}" data-id="${escape(c.id)}"
          title="${escape(c.id)}">
          <div class="cci-top">
            <span class="cci-dot ${dot}"></span>
            <span class="cci-proj">${escape(c.project_slug || "?")}</span>
            <span class="cci-when">${escape(when)}</span>
          </div>
          <div class="cci-preview">${preview}</div>
          ${branchChip}
        </div>`;
    }).join("");
}

function shortWhen(ts) {
  if (!ts) return "";
  // ts viene como "YYYY-MM-DD HH:MM:SS" (UTC). Mostramos HH:MM o la fecha.
  const t = Date.parse(ts.replace(" ", "T") + "Z");
  if (Number.isNaN(t)) return ts.slice(5, 10);
  const diff = Date.now() - t;
  if (diff < 86_400_000) return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
}

// Collapse de la lista de conversaciones (mismo patrón que el rail:
// clase + localStorage). Colapsada quedan solo el chevron y el ➕.
function wireSidebarCollapse() {
  const aside = document.querySelector(".chat-sidebar");
  const btn = $("#chat-sidebar-toggle");
  if (!aside || !btn) return;
  const KEY = "4bis.ui.chatSidebar.collapsed";
  const apply = (on) => {
    aside.classList.toggle("collapsed", on);
    btn.title = on ? "Expandir lista de conversaciones"
                   : "Colapsar lista de conversaciones";
  };
  if (document.body.classList.contains("workspace-mode")) {
    btn.title = "Cerrar lista de conversaciones";
    btn.onclick = () => setChatDrawer(false);
    return;
  }
  apply(localStorage.getItem(KEY) === "1");
  // `wirePanelResize()` corre en el boot de main.js, ANTES de que este
  // init ponga la clase `collapsed`: en ese momento el ancho guardado se
  // aplica igual y queda una columna colapsada de 420px. Re-aplicar acá,
  // ya con la clase puesta, es lo que la deja en los 3rem del CSS.
  aplicarAnchosGuardados();
  const alternar = (on) => {
    apply(on);
    localStorage.setItem(KEY, on ? "1" : "0");
    // Colapsar tira el ancho guardado (manda el CSS) y expandir lo
    // devuelve. Sin esto, expandir después de haber arrastrado dejaba la
    // columna en el ancho del CSS y el arrastre se perdía.
    aplicarAnchosGuardados();
  };
  btn.onclick = () => alternar(!aside.classList.contains("collapsed"));
  // Colapsada, la columna entera vuelve a abrir. El chevron seguía ahí,
  // pero es un blanco de 24px en una barra de 48 que por lo demás está
  // vacía: colapsar dejaba 98 conversaciones en el DOM y ninguna
  // alcanzable, y se leía como "se perdieron". El click del propio
  // chevron no se duplica —él ya alterna— así que se filtra.
  aside.addEventListener("click", (ev) => {
    if (!aside.classList.contains("collapsed")) return;
    if (ev.target.closest("#chat-sidebar-toggle, #chat-new")) return;
    alternar(false);
  });
}

// Drawer de conversaciones (<md, 2026-07-31). En desktop la lista es una
// columna fija; en el teléfono se llevaba 288px de 375 y dejaba el hilo
// en 87px. Mismo gesto que el rail global: botón ☰, backdrop para cerrar,
// y elegir una conversación cierra (si no, tapa lo que acabás de abrir).
function setChatDrawer(open) {
  const aside = document.querySelector(".chat-sidebar");
  if (!aside) return;
  if (open && document.body.classList.contains("workspace-mode")) {
    aside.classList.remove("collapsed");
  }
  aside.classList.toggle("mobile-open", open);
  $$(".chat-drawer-btn").forEach((b) =>
    b.setAttribute("aria-expanded", String(open)));
}

function wireChatDrawer() {
  const aside = document.querySelector(".chat-sidebar");
  if (!aside) return;
  $$(".chat-drawer-btn").forEach((b) => b.addEventListener("click", () =>
    setChatDrawer(!aside.classList.contains("mobile-open"))));
  $("#chat-drawer-backdrop")?.addEventListener(
    "click", () => setChatDrawer(false));
}

// =====================================================================
// PANEL: seleccionar y renderizar una conversación
// =====================================================================

export async function selectConversation(convId) {
  const generation = ++chatSelectionGeneration;
  setChatDrawer(false);
  try {
    const meta = await apiRoot(`/conversations/${encodeURIComponent(convId)}`);
    if (generation !== chatSelectionGeneration) return;
    if (!meta || meta.error) {
      toast("No se pudo cargar la conversación: " + (meta?.error ?? "404"), "err");
      return;
    }
    await openConversation(meta, generation);
  } catch (e) {
    if (generation !== chatSelectionGeneration) return;
    toast("No se pudo abrir la conversación: " + e.message, "err");
  }
}

async function openConversation(meta, generation) {
  if (generation !== chatSelectionGeneration) return;
  const readOnly = meta.status !== "open";
  activeChat = {
    convId: meta.id,
    projectSlug: meta.project_slug,
    // La rama la usa `/diff` para titular el visor sin re-pedir la conv.
    branch: meta.branch || null,
    currentChatId: null,
    busy: false,
    readOnly,
  };
  showMainView("convo");
  renderConvList();  // refrescar highlight del sidebar
  $("#chat-draft-project").hidden = true;  // por si venías de un borrador
  renderHeader(meta, generation);

  // Summary arriba (solo cerradas / si existe).
  const summaryEl = $("#chat-summary");
  if (readOnly && meta.summary) {
    summaryEl.textContent = meta.summary;
    summaryEl.hidden = false;
  } else {
    summaryEl.hidden = true;
    summaryEl.textContent = "";
  }

  // Input vs read-only.
  $("#chat-input-bar").hidden = readOnly;
  $("#chat-readonly-note").hidden = !readOnly;

  // Open in VS Code: siempre visible (si falla, el toast avisa).
  const vscodeBtn = $("#chat-panel-vscode");
  vscodeBtn.hidden = false;
  const ghLink = $("#chat-panel-gh-link");
  ghLink.hidden = true;
  try {
    const r = await api(`projects/${encodeURIComponent(meta.project_slug)}`);
    if (!selectionIsCurrent(generation, meta.id)) return;
    const proj = r?.project || r || null;
    if (proj && proj.git_remote_url) {
      const url = gitWebUrl(proj.git_remote_url);
      if (url) { ghLink.href = url; ghLink.hidden = false; }
    }
  } catch (_) { /* gh link queda oculto */ }
  if (!selectionIsCurrent(generation, meta.id)) return;

  renderSuggestions([]);   // las del hilo anterior no valen para éste
  // El panel del plan se engancha ACÁ y no al final: más abajo hay
  // `return`s tempranos (el de `resumeRunningChat`, sobre todo) y con
  // el enganche al final una conversación con un run en curso se abría
  // mostrando el plan de la conversación ANTERIOR. Justo el caso que
  // más se mira.
  attachGrafo(meta.id);
  await loadMessages(meta.id);
  if (!selectionIsCurrent(generation, meta.id)) return;
  setBusy(false);
  if (!readOnly) $("#chat-panel-input").focus();

  // ¿Hay un run EN CURSO para esta conversación? Puede haber arrancado
  // antes de este page load (recargaste en medio), desde Discord, o desde
  // otra pestaña. Retomamos el polling para ver el live; sin esto el panel
  // se quedaba mudo aunque el relay estuviera trabajando.
  if (!readOnly) {
    const resumed = await resumeRunningChat(meta.id, generation);
    if (!selectionIsCurrent(generation, meta.id)) return;
    if (resumed) return;
  }

  // Banner "continuar" si el último run cortó (error / budget / cancel).
  if (!selectionIsCurrent(generation, meta.id)) return;
  if (!readOnly) await maybeShowContinueBanner(meta, generation);
  if (!selectionIsCurrent(generation, meta.id)) return;
  // Una pregunta abierta puede llevar días esperando: se pinta al abrir
  // el hilo, no solo al terminar un run.
  if (!readOnly) refreshChatQuestions(meta.id, generation);
}

// Busca un chat `running` de esta conversación y engancha el polling.
// Devuelve true si retomó uno.
async function resumeRunningChat(convId, generation) {
  try {
    const r = await apiRoot("/chats?status=running&limit=50");
    if (!selectionIsCurrent(generation, convId)) return false;
    // `source !== "grafo"`: los nodos de un plan corren como chats del
    // MISMO hilo. Sin el filtro, abrir una conversación con el plan
    // trabajando enganchaba el polling a un NODO — el composer se ponía
    // en "ocupado" por un run que no era del humano, y al cerrar el nodo
    // sonaba el chime de "terminó" con el plan a mitad de camino.
    const run = (r.chats || []).find(
      (c) => c.conversation_id === convId && (c.source || "") !== "grafo");
    if (!run) return false;
    activeChat.currentChatId = run.id;
    setBusy(true);
    // sin await: el polling corre en background y la UI sigue interactiva
    pollChat(run.id, convId, generation);
    pokeGrafo().catch(() => {});
    return true;
  } catch (e) {
    _dbg("resumeRunningChat falló", e.message);
    return false;
  }
}

function showMainView(which) {
  $("#chat-empty").hidden = which !== "empty";
  $("#chat-convo").hidden = which !== "convo";
  $("#chat-memoria").hidden = which !== "memoria";
}

function renderHeader(meta, generation = chatSelectionGeneration) {
  const slug = meta.project_slug || activeChat?.projectSlug || "?";
  $("#chat-panel-title").textContent = `${slug} · ${meta.id.slice(0, 8)}`;

  const stEl = $("#chat-convo-status");
  stEl.textContent = meta.status;
  stEl.className = "badge " + (meta.status === "open" ? "warn"
    : meta.status === "closed" ? "ok" : "dim");
  // Iter 10.3: rama local acumulada + botón borrarla. Solo tiene sentido
  // cuando la conv está cerrada (en open la rama está checked-out y es
  // trabajo en curso). El detalle (merged/ahead/behind) lo trae el
  // endpoint /branch, que también es lo que decide el copy del confirm.
  loadBranchBadge(meta, generation);

  // Tres estados, no dos: "vinculado" sin hilo DM significa que las
  // respuestas NO salen de esta pantalla. Antes se pintaba igual que un
  // vínculo sano y por eso el botón "no hacía lo que esperabas".
  const linked = !!meta.discord_user_id, hasThread = !!meta.discord_thread_id;
  const who = meta.discord_author || (meta.discord_user_id || "").slice(0, 8);
  $("#chat-panel-meta").textContent =
    !linked ? "🖥 solo aquí"
      : hasThread ? `📱 también al DM de @${who}`
        : `⚠️ @${who} sin hilo DM — las respuestas no salen de acá`;

  // Botones Discord + Cerrar solo para conversaciones abiertas.
  const ro = meta.status !== "open";
  // 📱 mientras no haya hilo DM: cubre las dos formas de no tenerlo —
  // sin vincular, o vinculado con el hilo roto (hereda el usuario).
  $("#chat-panel-follow-mobile").hidden = ro || hasThread;
  $("#chat-panel-link-discord").hidden = ro || linked;
  $("#chat-panel-unlink-discord").hidden = ro || !linked;
  renderChimeBtn(meta.id);
  $("#chat-panel-close-conv").hidden = ro;
  // Diff de la rama: mientras la conv tenga rama. En las cerradas la rama
  // ya no existe (se borra al abrir el PR) y el modal lo dice con el link.
  const diffBtn = $("#chat-panel-diff");
  diffBtn.hidden = !meta.branch;
  diffBtn.onclick = () => openConvDiff(meta);
  $("#chat-panel-compact").hidden = ro || !meta.context;
  $("#chat-panel-extract-facts").hidden = ro || !meta.context;
  renderContextBadge(meta.context);
  renderUsageBadge(meta);
}

// El chip ⏱: cuánto tiempo de TRABAJO lleva la conversación — la suma de
// lo que duraron sus runs, que es lo que el relay guarda por run en
// `chats.duration_ms`. El reloj de pared va en el `title` y no al revés
// porque no dice nada útil: una conversación queda abierta entre pedido
// y pedido, y los dos números difieren por un orden de magnitud (medido
// el 2026-08-31 en `50379bac`: 16h16 de pared, 2h49 de trabajo). Es la
// misma cuenta que /cerrar mete en el body del PR.
function renderUsageBadge(meta) {
  const el = $("#chat-panel-usage");
  const u = meta.usage_time;
  if (!el) return;
  if (!u || !u.runs) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = `⏱ ${fmtDuration(u.ms)}`;
  const pared = paredMs(meta);
  el.title = `Tiempo de uso: la suma de los ${u.runs} run`
    + `${u.runs === 1 ? "" : "s"} de esta conversación.`
    + (pared ? `\nAbierta hace ${fmtDuration(pared)} (reloj de pared).` : "")
    + "\n\nEs el número que va a las horas del PR al cerrarla.";
}

/** Reloj de pared de la conversación en ms, o 0 si las fechas no parsean. */
function paredMs(meta) {
  const t0 = Date.parse(meta.started_at || "");
  const t1 = Date.parse(meta.closed_at || meta.last_activity_at || "");
  return Number.isFinite(t0) && Number.isFinite(t1) && t1 > t0 ? t1 - t0 : 0;
}

// Medidor de contexto del hilo: tokens REALES del último run (el relay
// los lee del usage del provider).
//
// 2026-08-31: el badge muestra el PICO, no la base. La base es lo que
// pesaba el hilo cuando ARRANCÓ el último run — o sea, un número viejo:
// medido sobre las conversaciones de dos semanas, el pico es 1,19x la
// base en la mediana y 4,67x en el peor caso (un hilo mostraba 8%
// cuando el run había llegado al 38%). El pico es además lo que decide
// el color y el banner, así que el número grande y el color contaban
// cosas distintas. La base queda en el tooltip: sigue siendo la que
// dice cuánto bajarías compactando.
function renderContextBadge(ctx) {
  const el = $("#chat-panel-context");
  lastCtx = ctx || null;
  if (!ctx) { el.hidden = true; renderCompactBanner(null); return; }
  const k = (n) => `${Math.round(n / 1000)}k`;
  el.hidden = false;
  // El "~" avisa que el denominador NO es la ventana del modelo que
  // corrió, sino el default global: sin eso el porcentaje se lee como
  // un dato del proveedor cuando es una estimación nuestra.
  const aprox = ctx.limit_medido ? "" : "~";
  el.textContent = `🧠 ${aprox}${ctx.peak_pct}% · ${k(ctx.peak_tokens)}/${k(ctx.limit)}`;
  el.className = "badge " + (ctx.hot ? "err"
    : ctx.peak_pct >= ctx.warn_pct - 20 ? "warn" : "ok");
  el.title = `Pico del último run: ${ctx.peak_tokens} tokens de `
    + `${ctx.limit} (${ctx.peak_pct}%).\n`
    + `El hilo arranca en ${ctx.base_tokens} (${ctx.pct}%) antes de `
    + `trabajar: eso es lo que bajaría compactando.\n`
    + `Aviso a partir de ${ctx.warn_tokens} tokens.\n`
    + (ctx.limit_medido
      ? `Ventana de ${ctx.model_name || "el modelo"}, del catálogo.`
      : "Ventana estimada (el modelo no tiene ventana cargada en "
        + "Modelos): el % es orientativo.")
    + (ctx.hot
      ? "\n\nCompacta el hilo (🗜) o abre uno nuevo si cambiaste de tema."
      : "");
  renderCompactBanner(ctx);
}

// Último contexto medido. renderBubbles pisa el innerHTML del hilo (y con
// él, el banner), y corre DESPUÉS de renderHeader al abrir la conversación:
// sin este cache el aviso solo aparecía al terminar un run.
let lastCtx = null;

// Aviso accionable cuando el hilo se puso pesado (2026-08-13).
// El medidor 🧠 ya se pintaba en rojo, pero es un badge de 10px en el
// header: nadie que no lo haya construido sabe qué significa ni que
// existe `compactar`. Y pasado ese punto el experto empieza a contestar
// sin ejecutar tools — el modo de falla que dejó 14 turnos muertos en el
// hilo a292ca08. El banner va en el hilo, con el botón adentro.
function renderCompactBanner(ctx) {
  const box = $("#chat-panel-messages");
  const old = document.getElementById("chat-panel-compact-banner");
  if (!box || !ctx?.hot || activeChat?.readOnly) { old?.remove(); return; }
  if (old) return;                       // ya está: no re-pintar ni saltar
  const k = (n) => `${Math.round(n / 1000)}k`;
  const banner = document.createElement("div");
  banner.id = "chat-panel-compact-banner";
  banner.className = "chat-banner";
  banner.innerHTML =
    `<div class="chat-banner-title">🧠 El hilo se puso pesado`
    + ` (${k(ctx.peak_tokens)} de ${k(ctx.limit)} tokens)</div>
     <div class="chat-banner-hint">Desde acá el experto empieza a
       responder sin tocar el repo. <strong>Compactar</strong> reemplaza
       los turnos viejos por un resumen: mismo hilo, misma rama, mismo
       Discord — solo se libera el contexto.
       <button id="chat-compact-now" class="btn btn-xs"
         style="margin-left:.35rem">🗜 Compactar ahora</button></div>`;
  box.appendChild(banner);
  banner.querySelector("#chat-compact-now")
    ?.addEventListener("click", () => compactActiveConversation());
  box.scrollTop = box.scrollHeight;
}

// Re-pinta el medidor 🧠 (y el botón 🗜 Compactar, que depende de él) con
// el historial ya persistido. Se llama al terminar cada run: `renderHeader`
// solo corre al ABRIR la conversación, así que en un hilo nuevo el medidor
// nacía vacío (sin historial no hay usage que medir) y se quedaba así toda
// la sesión — recién aparecía al reabrir/recargar. Reporte 2026-07-27.
async function refreshContextBadge(convId) {
  try {
    const meta = await refreshConvMeta(convId);
    if (!meta || activeChat?.convId !== convId) return;
    renderContextBadge(meta.context);
    renderUsageBadge(meta);
    $("#chat-panel-compact").hidden = !meta.context || !!activeChat.readOnly;
    $("#chat-panel-extract-facts").hidden =
      !meta.context || !!activeChat.readOnly;
  } catch (_) { /* el medidor es informativo: si falla, ni se nota */ }
}

// Compactar sin cerrar: el historial se reemplaza por su resumen. Misma
// conversación, misma rama, mismo Discord — solo baja el contexto. El
// compactador es un LLM: tarda, así que timeout largo y aviso previo.
async function compactActiveConversation() {
  if (!activeChat || activeChat.readOnly || activeChat.busy) return;
  const convId = activeChat.convId, short = convId.slice(0, 8);
  if (!await confirmModal({
    title: `Compactar el hilo ${short}…`,
    body: "El historial se reemplaza por un resumen: el hilo, la rama y el "
      + "vínculo con Discord siguen igual, pero el próximo mensaje arranca "
      + "con el contexto liberado.\n\n"
      + "El detalle de los turnos viejos se pierde (queda el resumen y los "
      + "hechos destilados). Tarda hasta un par de minutos.",
    confirmText: "Compactar",
  })) return;
  setBusy(true);
  toast(`Compactando ${short}… (el compactador es un LLM, espera un rato)`, "info");
  try {
    const r = await apiRoot(
      `/conversations/${encodeURIComponent(convId)}/compact`,
      { method: "POST" }, 300_000);
    const antes = r.context_before
      ? `${Math.round(r.context_before.base_tokens / 1000)}k tokens`
      : `${r.chars_before} chars`;
    toast(`Hilo ${short}… compactado ✓ — venía de ${antes}; `
      + `${r.facts} hecho(s) guardados.`, "ok");
    await selectConversation(convId);
  } catch (e) {
    toast("No pude compactar: " + e.message, "err");
  } finally {
    setBusy(false);
  }
}

// Extraer hechos del hilo SIN cerrarlo ni compactarlo (2026-08-21).
// La diferencia con "Compactar" importa y por eso son dos botones: aquel
// recorta los turnos viejos, o sea que sacar los hechos costaba perder el
// detalle de la conversación. Este deja el hilo exactamente como estaba.
async function extractFactsFromConversation() {
  if (!activeChat || activeChat.readOnly) return;
  const convId = activeChat.convId;
  const short = convId.slice(0, 8);
  setBusy(true);
  toast(`Destilando hechos de ${short}… (es un LLM, tarda)`, "info");
  try {
    const r = await apiRoot(
      `/admin/api/conversations/${encodeURIComponent(convId)}/extract-facts`,
      { method: "POST" }, 300_000);
    if (!r.created) {
      toast("El compactador no sacó ningún hecho nuevo del hilo.", "info");
      return;
    }
    toast(`${r.created} hecho(s) destilados — quedan PENDIENTES en `
      + `Memoria → ${r.project}. El experto no los ve hasta que los apruebes.`,
      "ok");
  } catch (e) {
    toast("No pude extraer los hechos: " + e.message, "err");
  } finally {
    setBusy(false);
  }
}

// Cerrar la conversación activa (POST /conversations/{id}/close).
// Dispara la compactación (summary + facts) y, si la conv tenía rama de
// git-flow, abre el PR a develop. Regresión 2026-07-20f: al unificar el
// workspace se borró el modal viejo que tenía este botón y quedó sin
// forma de cerrar desde la UI — el 409 de "ya hay una abierta" mandaba a
// cerrarla sin decir cómo.
async function closeActiveConversation() {
  if (!activeChat || activeChat.readOnly) return;
  const short = activeChat.convId.slice(0, 8);

  // No se cierra con trabajo en vuelo (2026-08-24). Cerrar compacta la
  // memoria y abre el PR: hacerlo a mitad de camino deja el summary sin
  // lo que falta y el PR con el trabajo incompleto. Son dos frentes
  // distintos y ninguno lo cubría el otro: `activeChat.busy` ve los runs
  // del chat, y `planEnCurso()` ve el grafo, que no crea ningún run.
  const plan = planEnCurso();
  if (activeChat.busy || plan) {
    const qué = plan
      ? `El plan va ${plan.hechos}/${plan.total}`
        + (plan.reloj ? ` y lleva ${plan.reloj} trabajando` : "")
        + (plan.tareas.length ? `.\n\nAhora: ${plan.tareas.join(" · ")}` : ".")
      : "Hay un run del experto en curso.";
    await alertModal({
      title: "Todavía está trabajando",
      body: `${qué}\n\nCerrar compacta la memoria y abre el PR, así que `
        + `hacerlo ahora dejaría el resumen y el PR a medias. `
        + `Esperá a que termine`
        + (plan ? `, o pará el plan desde el panel (⏹ Parar el plan).` : "."),
      okText: "Entendido",
    });
    return;
  }

  if (!await confirmModal({
    title: `Cerrar conversación ${short}…`,
    body: "Se compacta la memoria (summary + facts) y, si la conversación "
      + "tiene rama de trabajo, se abre el PR a develop.\n\n"
      + "Cerrar es cerrar: no se puede reabrir (después abres una nueva).",
    // "Cerrar" vs "Cancelar" se leían como lo mismo: los dos suenan a
    // "salir de este diálogo". El botón dice qué hace, no cómo se llama
    // la acción.
    confirmText: "Sí, cerrar la conversación",
    cancelText: "Volver",
    danger: true,
  })) return;
  const convId = activeChat.convId;
  try {
    const r = await apiRoot(
      `/conversations/${encodeURIComponent(convId)}/close`,
      { method: "POST" }, 30_000);
    const bits = [];
    if (r.pr_url) bits.push(`PR abierto: ${r.pr_url}`);
    else if (r.pr === "running") bits.push("verificando y abriendo PR…");
    bits.push(r.compaction === "running"
      ? "compactando memoria en background"
      : "sin historial que compactar");
    toast(`Conversación ${short}… cerrada ✓ — ${bits.join(" · ")}`, "ok");
    await loadChats();                       // refrescar sidebar (pasa a Cerradas)
    await selectConversation(convId);        // re-render read-only + summary
    if (r.pr === "running") pollPrStatus(convId, short);
    // La rama local la borra el relay cuando el PR queda abierto (el
    // toast de pollPrStatus lo dice). Acá no se pregunta nada: el botón
    // del header sigue estando para las ramas viejas.
  } catch (e) {
    toast("No pude cerrar la conversación: " + e.message, "err");
  }
}

// Diff de la rama de la conversación. El visor vive en chat-diff.js
// (lista de archivos + diff por archivo + acciones git); acá quedó solo
// el punto de entrada, que es lo que usan el botón del header y /diff.

// Iter 10.3: estado de la rama local de la conv. Muestra un chip con el
// nombre y prende el botón "borrar rama local" según `exists` y `merged`.
// Solo se pinta cuando la conv está cerrada: en open la rama es la rama
// de trabajo activa y borrarla sería destructivo (igual el endpoint
// rechaza con 409, pero la UI no tiene por qué tentarte).
async function loadBranchBadge(meta, generation = chatSelectionGeneration) {
  const chip = $("#chat-panel-branch");
  const btn = $("#chat-panel-delete-branch");
  if (!chip || !btn) return;
  // Reset.
  chip.hidden = true; chip.textContent = ""; chip.className = "badge dim";
  chip.title = "";
  btn.hidden = true; btn.onclick = null; btn.disabled = false;
  if (!meta.branch) return;            // conv sin rama (no era repo git)
  if (meta.status === "open") {
    // Rama activa: chip informativo SIN merge status ni boton borrar.
    chip.hidden = false;
    chip.textContent = "⊞ " + meta.branch;
    chip.className = "badge dim";
    chip.title = "Rama de trabajo activa (se borra al cerrar si queres).";
    btn.hidden = true;
    return;
  }
  let info;
  try {
    info = await apiRoot(`/conversations/${encodeURIComponent(meta.id)}/branch`);
  } catch (e) {
    _dbg("loadBranchBadge falló", e.message);
    return;                            // chip queda apagado, sin botón
  }
  if (!selectionIsCurrent(generation, meta.id)) return;
  if (!info || info.error || !info.exists) {
    // Rama borrada a mano, o el endpoint no la ve: el chip igual avisa
    // cuál era, así el humano sabe que quedó un registro histórico.
    chip.hidden = false;
    chip.textContent = `🌿 ${info?.branch || meta.branch}`;
    chip.className = "badge dim";
    chip.title = "La rama local ya no existe en el repo.";
    btn.hidden = true;
    return;
  }
  // Rama existe local. Chip con clase según si está mergeada o no.
  chip.hidden = false;
  const branchName = info.branch;
  const ahead = info.ahead || 0;
  const mergedTag = info.merged ? "✓ mergeada" : `⚠ ${ahead} ahead`;
  chip.textContent = `🌿 ${branchName} · ${mergedTag}`;
  chip.className = "badge " + (info.merged ? "ok" : "warn");
  chip.title = info.merged
    ? `Rama local mergeada a ${info.base}. Segura de borrar.`
    : `Rama local con ${ahead} commit(s) sin mergear a ${info.base}. `
      + `Borrarla los pierde (la rama remota la maneja GitHub).`;
  // Botón. Deshabilitado si es la rama actualmente checked-out:
  // el endpoint rechazaría el delete con 409, mejor bloquear acá.
  btn.hidden = false;
  btn.disabled = !!info.is_current;
  btn.title = info.is_current
    ? `No se puede borrar: es la rama actual del checkout (${info.current_branch}).`
    : `Borrar la rama local ${branchName} (la publicada la manejas por GitHub).`;
  btn.onclick = () => deleteLocalBranch(meta, info);
}

// Borrar la rama local de una conv cerrada. Confirm con texto acorde al
// estado (safe si merged=True, destructivo si hay commits ahead). El
// endpoint acepta `?force=true` para `git branch -D`; acá lo mandamos
// solo si el humano lo confirma explícitamente.
async function deleteLocalBranch(meta, info) {
  const branchName = info?.branch || meta?.branch;
  if (!meta || !branchName) return;
  const short = meta.id.slice(0, 8);
  const force = !info.merged;          // merged=False → -D
  const aheadNote = info.merged ? ""
    : `\n\n⚠ La rama tiene ${info.ahead} commit(s) sin mergear a ${info.base}: `
      + `borrarla los pierde. (La rama remota en GitHub no se toca.)`;
  if (!await confirmModal({
    title: `Borrar rama local ${short}…`,
    body: `Vas a borrar la rama local \`${branchName}\` del repo.${aheadNote}\n\n`
      + `Esto NO toca la rama remota en GitHub — esa la manejas tú `
      + `(merge de develop→main, "delete branch" en el PR, etc.). Solo `
      + `limpia la copia local que se va acumulando.`,
    confirmText: force ? "Borrar igual" : "Borrar",
    danger: true,
  })) return;
  try {
    const r = await apiRoot(
      `/conversations/${encodeURIComponent(meta.id)}/branch`
      + (force ? "?force=true" : ""),
      { method: "DELETE" });
    toast(`Rama ${branchName} borrada ✓`, "ok");
    // El chip y el botón se van a re-evaluar solos la próxima vez que se
    // abra la conv, pero el sidebar también muestra la rama: lo refresco.
    await loadChats();
    // Re-pintar el chip del header: la rama ya no existe, debe pasar a
    // "existe=False" (estado dim sin botón).
    if (activeChat && activeChat.convId === meta.id) {
      await loadBranchBadge(meta);
    }
  } catch (e) {
    // apiRoot() tira Error con .message, .status y .body (texto crudo,
    // ver _fetch_json). Si el body es JSON válido parseamos el error
    // para ramificar el toast por código HTTP; si no, caemos al
    // message genérico.
    let body = null;
    if (e && typeof e.body === "string") {
      try { body = JSON.parse(e.body); } catch { body = null; }
    }
    const errMsg = (body && body.error) || (e && e.message) || String(e);
    if (e && e.status === 409 && body && /rama actual/.test(body.error || "")) {
      toast(`Haz checkout a otra rama antes (actual: ${body.current_branch || "?"}).`, "err");
    } else if (e && e.status === 409) {
      toast(`No se puede borrar: ${errMsg}`, "err");
    } else {
      toast("No pude borrar la rama: " + errMsg, "err");
    }
  }
}

// El PR corre en background (verify build+test + LLM redactando el body:
// minutos, no segundos — antes esto vivía dentro del POST /close y la UI
// cortaba a los 30s con "no pude cerrar" mientras el PR se abría igual).
// Poll cada 5s hasta 10 min; el resultado durable queda en la conv igual.
const PR_POLL_MS = 5_000, PR_POLL_MAX = 120;
const PR_STATE_TEXT = {
  verifying: "corriendo build y tests…",
  describing: "redactando la descripción del PR…",
  opening: "abriendo el PR…",
};

async function pollPrStatus(convId, short) {
  for (let i = 0; i < PR_POLL_MAX; i++) {
    await new Promise((res) => setTimeout(res, PR_POLL_MS));
    let r;
    try {
      r = await apiRoot(`/conversations/${encodeURIComponent(convId)}/pr`, {}, 15_000);
    } catch { continue; }                    // relay ocupado: reintenta
    if (r.state === "done") {
      const draft = r.draft ? " (draft: la verificación salió roja)" : "";
      const rama = r.branch_deleted
        ? ` · rama local ${r.branch || ""} borrada` : "";
      toast(`PR de ${short}… listo${draft}: ${r.pr_url}${rama}`,
            r.draft ? "warn" : "ok");
      await loadChats();
      return;
    }
    if (r.state === "error") {
      toast(`Sin PR para ${short}…: ${r.error || "error desconocido"}`, "err");
      return;
    }
    if (r.state === "unknown") return;       // relay reiniciado: nada que reportar
    if (i % 6 === 0 && PR_STATE_TEXT[r.state]) {
      toast(`PR de ${short}…: ${PR_STATE_TEXT[r.state]}`, "info");
    }
  }
  toast(`El PR de ${short}… sigue corriendo; mira GitHub en un rato.`, "warn");
}

// Render del hilo: los textos del experto son los separadores naturales
// ("Voy a revisar el proyecto…"), y las tool calls consecutivas entre esos
// textos se agrupan en UN bloque colapsable. Sin esto un run con 200 tool
// calls es scroll infinito de tarjetas planas.
function renderBubbles(messages) {
  const box = $("#chat-panel-messages");
  const html = [];
  let group = [];
  const flush = () => {
    if (group.length) { html.push(toolGroupHtml(group)); group = []; }
  };
  for (const [messageIndex, m] of messages.entries()) {
    if (m.role === "tool") { group.push(m); continue; }
    flush();
    html.push(bubbleHtml(m, messageIndex));
  }
  flush();
  box.innerHTML = html.length ? html.join("") : emptyState({title: "Sin mensajes todavía",
    sub: "Esta conversación no tiene mensajes guardados."});
  renderCompactBanner(lastCtx);     // el innerHTML se lo acaba de comer
  addCopyButtons(box);              // "copiar" en cada bloque de código
  box.scrollTop = box.scrollHeight;
}

// Bloque colapsable con las tools de un tramo. Colapsado por defecto (ese
// es el punto); si hubo ediciones lo marcamos en ámbar para que se note que
// adentro hay diffs que vale la pena abrir.
function toolGroupHtml(group) {
  if (group.length === 1) return bubbleHtml(group[0]);
  // Los turnos de tool-return llegan con tool_name genérico "tool"; no son
  // acciones, así que no cuentan para la descripción (sí se ven adentro).
  const actions = group.filter((m) => m.tool_name && m.tool_name !== "tool");
  const n = actions.length || group.length;
  const hasEdits = actions.some(
    (m) => m.tool_name === "edit_file" || m.tool_name === "write_file");
  const phrase = describeToolGroup(actions);
  const inner = group.map((m) => toolCardHtml({
    tool: m.tool_name, content: m.content, diff: m.diff, summary: m.summary,
  })).join("");
  return `<details class="chat-tool-group${hasEdits ? " has-edits" : ""}">
    <summary>${n} paso${n === 1 ? "" : "s"}${phrase ? ` · ${phrase}` : ""}</summary>
    <div class="ctg-body">${inner}</div>
  </details>`;
}

// "leyó 8 archivos · exploró 3 carpetas · ✏️ editó 2 archivos"
function describeToolGroup(actions) {
  const c = { read: 0, list: 0, edit: 0, shell: 0, search: 0, other: 0 };
  for (const m of actions) {
    const t = m.tool_name;
    if (t === "read_file") c.read++;
    else if (t === "list_dir" || t === "directory_tree") c.list++;
    else if (t === "edit_file" || t === "write_file") c.edit++;
    else if (t === "run_shell") c.shell++;
    else if (t === "cbm_query") c.search++;
    else c.other++;
  }
  const pl = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const bits = [];
  if (c.read) bits.push(`leyó ${pl(c.read, "archivo", "archivos")}`);
  if (c.list) bits.push(`exploró ${pl(c.list, "carpeta", "carpetas")}`);
  if (c.search) bits.push(pl(c.search, "búsqueda", "búsquedas"));
  if (c.shell) bits.push(pl(c.shell, "comando", "comandos"));
  if (c.edit) bits.push(`✏️ editó ${pl(c.edit, "archivo", "archivos")}`);
  if (c.other) bits.push(pl(c.other, "paso más", "pasos más"));
  return bits.join(" · ");
}

// Los adjuntos viajan dentro del texto del user como el bloque
// "## Adjuntos - att_<id>.png (N bytes) …" que arma el relay. Acá los ids
// de imagen se vuelven miniaturas: verlas es todo el punto de haberlas
// mandado. El resto del texto se escapa igual que siempre — el reemplazo
// corre DESPUÉS de escape(), sobre un id que es [0-9a-f]{16}, así que no
// hay forma de inyectar markup por esta vía.
const _ATT_IMG_RE = /att_[0-9a-f]{16}\.(?:png|jpe?g|gif|webp)/g;

function withThumbs(texto) {
  return escape(texto).replace(_ATT_IMG_RE, (nombre) => {
    // El endpoint resuelve por id PELADO; el bloque lo nombra con la
    // extensión con que quedó en disco (att_<hash>.png → 404 si la
    // mandás tal cual).
    const id = nombre.replace(/\.[a-z0-9]+$/i, "");
    return `<a href="/attachments/${id}" target="_blank" rel="noopener">` +
      `<img class="chat-attach-thumb" src="/attachments/${id}" alt="${nombre}" ` +
      `loading="lazy"></a> <code>${nombre}</code>`;
  });
}

// --- markdown del experto (2026-08-12) --------------------------------
// El experto SIEMPRE responde en markdown (títulos, tablas, listas, código)
// y hasta hoy la burbuja lo pintaba con escape() + pre-wrap: tablas como
// sopa de pipes, `**negrita**` literal, `##` literal. Eso es la "ensalada"
// que reportaron los devs. marked + DOMPurify ya estaban vendorizados
// (los usa el preview de tab-projects), así que acá solo se reusan.
let _md = null;   // {marked, purify} una vez cargados

async function ensureMd() {
  if (_md) return _md;
  const [m, p] = await Promise.all([
    import("./vendor-marked-18.0.7.esm.js"),
    import("./vendor-purify-3.4.15.es.js"),
  ]);
  _md = { marked: m.marked, purify: p.default };
  return _md;
}

// Basura de minimax: cuando el modelo malforma una tool call, el bloque
// crudo (`<tool_call> ]<]minimax[>[ …`) queda pegado al texto final. No es
// contenido, es ruido de protocolo: fuera antes de renderizar.
const _JUNK_RE = /<tool_call>[\s\S]*?(?:<\/tool_call>|$)|\]<\]minimax\[>\[/g;

// El bloque de pregunta que el relay pega al final de la respuesta
// (`_render_pregunta` en server.py): título, detalle, opciones y el id.
//
// Acá SOBRA y en Discord NO: allá no hay tarjeta, el texto es la única
// forma de ver la decisión. En este chat la tarjeta interactiva se pinta
// justo abajo del mensaje, así que dejarlo mostraba la misma decisión dos
// veces —una en prosa y otra en botones— y eso es lo que se sentía
// incómodo y repetido.
//
// Se saca de la VISTA, no del contenido: el .md archivado y Discord
// siguen teniendo la pregunta entera. Y la respuesta del humano la cita
// ("Respuesta a tu pregunta «…»"), así que el hilo no pierde el registro
// de qué se preguntó.
const _PREGUNTA_RE =
  /\n*(?:❓|📦) \*\*[\s\S]*?\n`q_[0-9a-f]{6,}`[ \t]*/g;

export function stripJunk(texto) {
  return (texto || "")
    .replace(_JUNK_RE, "")
    .replace(_PREGUNTA_RE, "\n")
    .trim();
}

// Sincrónico a propósito: bubbleHtml() arma strings. Si los módulos aún no
// están (primer render antes de que resuelva ensureMd) cae al texto plano
// de siempre — nunca queda vacío.
function mdHtml(texto) {
  const limpio = stripJunk(texto);
  if (!_md) return `<div class="whitespace-pre-wrap break-words">${escape(limpio)}</div>`;
  // `md-body` y NO `chat-md`: esa clase ya existe y es el visor del .md
  // CRUDO (font-mono, max-h-96 + overflow, border). Renderizar acá dentro
  // salía monoespaciado y con la respuesta cortada a 24rem con scroll
  // propio — el bug que hacía que el hilo siguiera leyéndose mal.
  return `<div class="md-body">${
    _md.purify.sanitize(_md.marked.parse(limpio))}</div>`;
}

// --- bloque "proceso" (2026-08-12) ------------------------------------
// Lo que el experto pensó y tocó durante el run. Vive en
// chats.progress_events desde siempre, pero el .md solo guarda
// Usuario/Respuesta: al recargar el hilo, el razonamiento y las tool calls
// desaparecían y quedaba la conclusión sin el cómo. Ahora el endpoint los
// manda en `steps[]` y acá se pintan COLAPSADOS arriba de la respuesta:
// están cuando los buscas, no compiten con la respuesta cuando no.
function thinkBlockHtml(steps) {
  if (!Array.isArray(steps) || !steps.length) return "";
  const inner = steps.map((s) => {
    if (s.kind === "say") return `<div class="ct-say">${mdHtml(s.message)}</div>`;
    if (s.kind === "steer") {
      return `<div class="ct-steer">🧭 corrección tuya: ${escape(s.message)}</div>`;
    }
    return toolCardHtml({
      tool: s.tool, summary: s.message, diff: s.diff,
      cmd: s.cmd, output: s.output,
    });
  }).join("");
  return `<details class="chat-think">
    <summary>${describeSteps(steps)}</summary>
    <div class="ct-body">${inner}</div>
  </details>`;
}

// "🧠 12 pensamientos · leyó 8 archivos · ✏️ editó 2 archivos"
function describeSteps(steps) {
  const says = steps.filter((s) => s.kind === "say").length;
  const tools = steps.filter((s) => s.kind === "tool")
    .map((s) => ({ tool_name: s.tool }));
  const bits = [];
  if (says) bits.push(`${says} pensamiento${says === 1 ? "" : "s"}`);
  const t = describeToolGroup(tools);
  if (t) bits.push(t);
  return `🧠 proceso · ${bits.join(" · ") || `${steps.length} pasos`}`;
}

// "· 8 tools" al lado del tag del experto (2026-08-13).
// El bloque "proceso" ya cuenta lo que hizo… cuando hizo algo: un turno
// con CERO tools no tiene steps, así que no pintaba nada y se leía igual
// que uno que trabajó. Justo el caso que dejó el hilo a292ca08 encadenando
// 14 respuestas que anunciaban una acción sin ejecutarla.
// Estilos inline a propósito: admin.css es un bundle de Tailwind que se
// regenera con build-css.ps1 (baja el CLI standalone). Una clase nueva
// obligaría a ese paso; dos colores no lo valen.
function toolTagHtml(m) {
  const n = m.tool_calls;
  if (n === undefined || n === null) return "";          // runs viejos
  if (n > 0) {
    return ` · <span style="color:#71717a" title="Tools que corrió este`
      + ` turno (el detalle está en 🧠 proceso).">🔧 ${n} tools</span>`;
  }
  const anuncio = m.phase_at_end === "announced_no_tools";
  const tip = anuncio
    ? "El experto describió una acción pero terminó el run sin llamar "
      + "ninguna tool. Casi siempre es contexto: compacta el hilo (🗜) "
      + "y repite el pedido."
    : "Este turno no tocó el repo: contestó con lo que ya tenía en el hilo.";
  return ` · <span style="color:${anuncio ? "#fbbf24" : "#71717a"}"`
    + ` title="${tip}">${anuncio ? "⚠️ anunció y no ejecutó" : "0 tools"}`
    + `</span>`;
}

// Quién pidió el turno (ADR-037). Tres casos, y los tres dicen algo
// distinto — por eso no se colapsan en uno:
//
//   un mail     → cruzó Access: esa persona lo pidió. Se muestra la
//                 parte antes del @ para no romper el ancho; el mail
//                 entero va en el tooltip.
//   "owner"     → el centinela de identity.py: bot, CLI o esta máquina.
//                 Se pinta "local" y no "owner" porque acá "owner" es
//                 además un ROL, y ver el rol donde va la identidad se
//                 lee como si el chat lo hubiera pedido un permiso.
//   sin dato    → los ~800 chats anteriores a la fase 1. "tú" es lo que
//                 decía antes: no sabemos, y no vamos a inventar.
function requesterLabel(m) {
  const who = (m.requested_by || "").trim();
  if (!who) return "tú";
  if (who === "owner") {
    return `<span title="Pedido desde esta máquina: el bot, la CLI o tú`
      + ` en localhost. No cruzó Cloudflare Access.">local</span>`;
  }
  const corto = who.split("@")[0] || who;
  return `<span title="${escape(who)}">${escape(corto)}</span>`;
}

// Una burbuja/tarjeta por turno. Los tool calls son tarjetas colapsables
// (Fase 2 les mete el diff cuando el endpoint lo exponga).
function bubbleHtml(m, messageIndex = null) {
  if (m.role === "user") {
    return `<div class="flex justify-end">
      <div class="max-w-[90%] rounded-2xl rounded-tr-sm bg-sky-900/30 px-3 py-2 text-sm text-zinc-100">
        <div class="mb-1 text-[10px] uppercase tracking-wider text-sky-300/70">${requesterLabel(m)}</div>
        <div class="whitespace-pre-wrap break-words font-mono text-xs">${withThumbs(m.content)}</div>
        ${m.truncated ? `<div class="mt-1 text-[10px] text-amber-400">truncado (${m.total_chars} chars)</div>` : ""}
      </div>
    </div>`;
  }
  if (m.role === "assistant") {
    // Sobre el texto YA limpio: un turno que era solo basura de protocolo
    // es un turno sin texto, y merece el mismo aviso que el vacío.
    const hasText = stripJunk(m.content).length > 0;
    const body = hasText
      ? mdHtml(m.content)
      : `<div class="text-xs italic text-zinc-400">El experto no devolvió texto (se quedó en el razonamiento). Prueba reformular o reenviar.</div>`;
    // El proceso va ARRIBA de la respuesta y colapsado: se lee en orden
    // (pensó → contestó) sin que el razonamiento le gane a la conclusión.
    return thinkBlockHtml(m.steps) + `<div class="chat-answer">
      <div class="chat-answer-tag">experto${toolTagHtml(m)}</div>
      ${runOutcomeHtml(m)}
      ${body}
      ${Number.isInteger(messageIndex) ? `<div class="mt-2"><button type="button" class="btn btn-xs chat-open-object" data-message-index="${messageIndex}">Abrir en workspace</button></div>` : ""}
      ${m.truncated ? `<div class="mt-1 text-[10px] text-amber-400">truncado (${m.total_chars} chars)</div>` : ""}
    </div>`;
  }
  if (m.role === "tool") {
    return toolCardHtml({ tool: m.tool_name, content: m.content, diff: m.diff,
                          summary: m.summary, cmd: m.cmd });
  }
  return "";
}

// Tarjeta de tool call inline colapsable.
//
// 2026-08-27: el cuerpo pasa a tener hasta tres bloques, en el orden en
// que pasan las cosas — `cmd` (el comando entero, sin recortar), `diff`
// (edit_file), `output` (lo que contestó la terminal). Antes de esto una
// llamada a la shell se veía como la palabra "shell" y nada más: no se
// podía saber qué estaba corriendo el experto ni si iba bien o mal sin
// esperar a la respuesta final.
//
// Estilos inline y no clases: admin.css es un bundle de Tailwind sellado
// por dos hashes (build-css.ps1 + sello_clases.py), así que una clase
// nueva obliga a rebuildear el CSS. Dos <pre> no lo valen — mismo
// criterio que el resto de los agregados chicos de este archivo.
const _PRE_CSS = "margin:0;white-space:pre-wrap;word-break:break-word;"
  + "font-family:ui-monospace,monospace;font-size:11px;line-height:1.45";

// El veredicto del comando. `shell` cierra su salida con `(exit=N)`
// (ver relay/shell.py), así que se puede pintar sin abrir la tarjeta:
// verde si salió 0, rojo si no. Es la mitad del pedido — "para saber si
// va bien o mal" — y sale de un regex, no de un campo nuevo.
function runOutcomeHtml(m) {
  if (!m.run_status) return ""; // Turnos antiguos: no inventar estado.
  const s = m.stages || {};
  const labels = { ok: "Ejecución terminada", error: "Ejecución con error",
    cancelled: "Ejecución cancelada", timeout: "Tiempo agotado", running: "En ejecución" };
  const results = { aprobado: "Tarea cumplida", pendiente: "Trabajo pendiente",
    intervencion: "Requiere intervención", desviado: "Fuera del plan" };
  const verified = s.resultado && s.resultado !== "sin_verificar" && s.verifier_verdict;
  const approved = m.run_status === "ok" && s.resultado === "aprobado" && s.verifier_verdict === "complete";
  const badge = (text, cls, title = "") => `<span class="badge ${cls}" title="${escape(title)}">${escape(text)}</span>`;
  const badges = [badge(labels[m.run_status] || m.run_status, m.run_status === "ok" ? "dim" : "warn"),
    badge(approved ? results.aprobado : (results[s.resultado] && s.resultado !== "aprobado"
      ? results[s.resultado] : "Cumplimiento sin confirmar"), approved ? "ok" : "warn"),
    badge(verified ? "Verificador: revisado" : "Sin verificar", verified ? "dim" : "warn", s.verifier_feedback || "")];
  if (m.duration_ms != null) badges.push(badge(fmtDuration(m.duration_ms), "dim", "Duración registrada"));
  if (m.export_pending) badges.push(badge("Exportación pendiente", "warn",
    m.export_error || "Respuesta guardada en SQLite; los archivos se reintentarán automáticamente."));
  return `<div class="flex flex-wrap gap-2 mb-2" role="group" aria-label="Estado del resultado">${badges.join("")}</div>`;
}

function exitBadge(output) {
  if (output.includes("background activo; disponibilidad sin verificar")) {
    return ' <span class="badge warn">Activo · disponibilidad sin verificar</span>';
  }
  const m = /\(exit=(-?\d+)\)/g;
  let last = null, hit;
  while ((hit = m.exec(output)) !== null) last = hit[1];
  if (last === null) return "";
  const ok = last === "0";
  return ` <span style="color:${ok ? "#4ade80" : "#f87171"}">${
    ok ? "✓" : `✗ exit=${escape(last)}`}</span>`;
}

function toolCardHtml({ tool, content, diff, summary, cmd, output }) {
  const label = (summary || `⚙ ${escape(tool || "tool")}`)
    + (output ? exitBadge(output) : "");
  const bloques = [];
  if (cmd) {
    bloques.push(`<pre style="${_PRE_CSS};color:#7dd3fc">${escape(cmd)}</pre>`);
  }
  if (diff) bloques.push(`<pre class="ctc-diff">${colorizeDiff(diff)}</pre>`);
  if (output) {
    bloques.push(`<pre style="${_PRE_CSS};color:#a1a1aa${
      cmd || diff ? ";margin-top:.4rem;padding-top:.4rem;"
        + "border-top:1px solid #ffffff14" : ""}">${escape(output)}</pre>`);
  } else if (content && content !== `(llamó ${tool})`) {
    bloques.push(`<div class="whitespace-pre-wrap break-words text-zinc-400">${escape(content)}</div>`);
  }
  if (!bloques.length) {
    // Sin cuerpo: tarjeta plana (no colapsable) para no invitar a un click vacío.
    return `<div class="chat-tool-card"><div class="ctc-flat">${label}</div></div>`;
  }
  return `<details class="chat-tool-card">
    <summary>▸ ${label}</summary>
    <div class="ctc-body">${bloques.join("")}</div>
  </details>`;
}

function colorizeDiff(diff) {
  return diff.split("\n").map((ln) => {
    const e = escape(ln);
    if (ln.startsWith("+") && !ln.startsWith("+++")) return `<span class="diff-add">${e}</span>`;
    if (ln.startsWith("-") && !ln.startsWith("---")) return `<span class="diff-del">${e}</span>`;
    if (ln.startsWith("@@") || ln.startsWith("+++") || ln.startsWith("---"))
      return `<span class="diff-meta">${e}</span>`;
    return e;
  }).join("\n");
}

// max_turns/content_cap explícitos: los defaults del endpoint (200 turnos,
// 4000 chars) son para un preview, no para el panel de chat. Un run con
// muchas tool calls pasa los 200 turnos fácil, y el endpoint trunca
// CORTANDO AL LLEGAR AL TOPE — o sea que se come los últimos turnos, justo
// donde está la respuesta final del experto. Bug reportado 2026-07-20:
// "respondió pero no se muestra en la UI" (223 turnos, default 200).
// 2000 es el cap duro del endpoint; 20k chars alcanza para respuestas largas.
async function loadMessages(convId) {
  const box = $("#chat-panel-messages");
  box.innerHTML = '<p class="p-3 text-xs text-zinc-400" role="status">Cargando mensajes…</p>';
  await ensureMd().catch(() => {});   // sin markdown ⇒ texto plano, no error
  try {
    const r = await apiRoot(
    `/conversations/${encodeURIComponent(convId)}/messages`
    + `?max_turns=2000&content_cap=20000`, null, 30_000);
    if (activeChat?.convId !== convId) return []; // respuesta tardía de otro hilo
    if (r.error) throw new Error(r.error);
    renderBubbles(r.messages || []);
    restorePendingUser(r.messages || []);
    return r.messages || [];
  } catch (e) {
    if (activeChat?.convId !== convId) return [];
    box.innerHTML = `<div class="p-3" role="alert"><p class="text-sm text-red-400">No se pudo leer la conversación: ${escape(e.message)}</p>
      <button class="btn mt-2" id="chat-messages-retry">Reintentar</button></div>`;
    $("#chat-messages-retry").addEventListener("click", () => loadMessages(convId));
    return [];
  }
}

// El relay persiste el historial de la conversación recién al TERMINAR el
// run — y NO lo persiste si el run falló (save_conversation_messages solo
// corre con status ok). Así que tras un re-render el turno del usuario
// puede no estar todavía: lo re-appendeamos para que nunca pierda lo que
// escribió. Bug reportado 2026-07-20: "el prompt se pegó como burbuja y
// luego desapareció".
function restorePendingUser(messages) {
  const pending = activeChat?.pendingUserText;
  if (!pending) return;
  const norm = (s) => (s || "").trim().slice(0, 200);
  // 2026-07-25: mirar TODOS los turnos del usuario, no solo el último.
  // Con un steer el último turno es la corrección, así que el prompt
  // original —ya persistido más arriba— se re-appendeaba como burbuja
  // duplicada al final del hilo.
  const persisted = messages.some(
    (m) => m.role === "user" && norm(m.content) === norm(pending));
  if (persisted) {
    activeChat.pendingUserText = null;   // ya quedó persistido
    return;
  }
  appendOptimisticUser(pending);
}

// Un solo listener para todas las respuestas, incluido el streaming que
// re-pinta el panel. El contenido no viaja en atributos: se toma del array
// que ya cargó esta conversación.
function wireChatObjects() {
  const box = $("#chat-panel-messages");
  if (!box || box.dataset.chatObjectsWired) return;
  box.dataset.chatObjectsWired = "1";
  box.addEventListener("click", async (ev) => {
    const button = ev.target.closest(".chat-open-object");
    if (!button || !activeChat?.convId) return;
    const index = Number(button.dataset.messageIndex);
    if (!Number.isInteger(index) || index < 0) return;
    const conversationId = activeChat.convId;
    try {
      const r = await apiRoot(
        `/conversations/${encodeURIComponent(conversationId)}/messages`
        + `?max_turns=2000&content_cap=100000`, null, 30_000);
      const message = r.messages?.[index];
      if (!message) throw new Error("la respuesta ya no está disponible");
      await openChatObject({
        conversationId,
        message,
        messageIndex: index,
      });
    } catch (e) {
      toast("No pude abrir el objeto: " + e.message, "err");
    }
  });
  if (!chatObjectBackWired) {
    chatObjectBackWired = true;
    window.addEventListener("chat-object-back", (ev) => {
      const id = ev.detail?.conversationId;
      if (id) selectConversation(id);
    });
  }
}

// =====================================================================
// ENVÍO + streaming en vivo
// =====================================================================

function setBusy(busy) {
  if (activeChat) activeChat.busy = busy;
  const send = $("#chat-panel-send");
  const b = $("#chat-panel-busy");
  const inp = $("#chat-panel-input");
  const steer = $("#chat-panel-steer");
  const stop = $("#chat-panel-stop");
  if (send) send.hidden = busy;
  if (b) { b.hidden = !busy; if (busy) b.textContent = "⏳ pensando…"; }
  if (steer) steer.hidden = !busy;
  if (stop) stop.hidden = !busy;
  // El composer YA NO se deshabilita mientras corre (2026-07-25): mirar
  // al experto irse por el camino equivocado sin poder decirle nada era
  // el agujero. Lo que escribes durante el run va por /experts/steer.
  if (inp) {
    inp.disabled = false;
    inp.placeholder = busy
      ? "Corrige el rumbo… (Ctrl+Enter; se aplica al terminar la tool en curso)"
      : "Mensaje… (Ctrl+Enter envía)";
  }
  // Al arrancar un run: limpiar banner + resetear el contador de pasos vivos.
  if (busy) {
    document.getElementById("chat-panel-continue-banner")?.remove();
    if (activeChat) activeChat.lastStepN = 0;
  }
}

// Feedback en vivo: /experts/status trae phase, last_tool, tool_calls,
// elapsed_s y (Fase 3b) `steps[]` — los pasos ricos con la línea legible
// y el diff de edit_file, los MISMOS datos que van al embed de Discord.
// Rendereamos cada step nuevo como la misma tarjeta que queda al persistir,
// así el hilo en vivo y el releído se ven idénticos.
function updateBusyStatus(s) {
  const b = $("#chat-panel-busy");
  if (b && !b.hidden) {
    const secs = Math.round(s.elapsed_s || 0);
    // Tokens del run EN CURSO: /experts/status ya los trae (el callback de
    // progreso los actualiza en cada vuelta). El medidor 🧠 del header es
    // del run ANTERIOR, así que sin esto no había forma de ver el gasto
    // mientras el experto trabaja.
    const tin = s.tokens_in || 0;
    const tok = tin ? ` · ${Math.round(tin / 1000)}k tok` : "";
    if (s.phase === "tool_call") {
      const n = s.tool_calls || 0;
      const tool = s.last_tool ? ` ${s.last_tool}` : "";
      b.textContent = `🔧${tool} · ${n} tool${n === 1 ? "" : "s"} · ${secs}s${tok}`;
    } else if (s.phase === "writing") {
      b.textContent = `✍️ escribiendo… ${secs}s${tok}`;
    } else {
      b.textContent = `⏳ pensando… ${secs}s${tok}`;
    }
  }
  // Pasos nuevos (n creciente). El relay capea la lista a los últimos 30;
  // si el poll se saltea alguno (run muy rápido) no pasa nada: al terminar
  // loadMessages() trae el historial completo con sus diffs.
  if (!activeChat || !Array.isArray(s.steps)) return;
  const seen = activeChat.lastStepN || 0;
  for (const step of s.steps) {
    if ((step.n || 0) > seen) {
      activeChat.lastStepN = step.n;
      appendLiveStep(step);
      continue;
    }
    // Un paso YA pintado que ahora trae salida: la terminal contesta
    // después de que la tool arranca, así que el `output` llega uno o
    // más polls más tarde y el dedup por `n` lo dejaría afuera. Sin
    // esto, mirar un run en vivo mostraba el comando y nunca la salida
    // — justo lo que se pidió ver (2026-08-27).
    if (step.output) fillLiveOutput(step);
  }
}

// Repinta la tarjeta de un paso vivo cuando llega su salida.
// Idempotente por `data-out`: el mismo step viene en cada poll mientras
// esté dentro de los últimos 30 que manda el relay.
function fillLiveOutput(step) {
  const box = $("#chat-panel-messages");
  const el = box?.querySelector(`[data-step-n="${step.n}"]`);
  if (!el || el.dataset.out) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = toolCardHtml({
    summary: step.message, diff: step.diff, tool: step.tool,
    cmd: step.cmd, output: step.output,
  });
  const nuevo = tmp.firstElementChild;
  if (!nuevo) return;
  nuevo.dataset.stepN = step.n;
  nuevo.dataset.out = "1";
  // Si el humano ya la había abierto para mirar el comando, no se la
  // cerramos en la cara justo cuando llega lo que estaba esperando.
  if (el.open && nuevo.tagName === "DETAILS") nuevo.open = true;
  el.replaceWith(nuevo);
}

// Tarjeta de paso en vivo (Fase 3b): misma tarjeta que el persistido
// (summary + diff colapsable). loadMessages() la reemplaza al terminar.
// `kind` (2026-07-25) distingue el QUÉ del POR QUÉ: "say" es la narración
// del modelo (antes se tiraba y el hilo vivo era una lista de tools sin
// hilo conductor), "steer" es la corrección que metió el humano.
function appendLiveStep(step) {
  const box = $("#chat-panel-messages");
  if (!box) return;
  const tmp = document.createElement("div");
  if (step.kind === "say") {
    tmp.innerHTML = `<div class="chat-live-say">💭 ${escape(step.message)}</div>`;
  } else if (step.kind === "steer") {
    tmp.innerHTML = `<div class="chat-live-steer">🧭 corrección tuya: ${
      escape(step.message)}</div>`;
  } else {
    tmp.innerHTML = toolCardHtml({
      summary: step.message, diff: step.diff, tool: step.tool,
      cmd: step.cmd, output: step.output,
    });
  }
  const el = tmp.firstElementChild;
  if (el) {
    // La marca por la que `fillLiveOutput` lo vuelve a encontrar cuando
    // llegue la salida de la terminal.
    el.dataset.stepN = step.n;
    if (step.output) el.dataset.out = "1";
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }
}

async function pollChat(chatId, convId, generation = chatSelectionGeneration) {
  const POLL_MS = 1500;
  while (selectionIsCurrent(generation, convId)
      && activeChat.currentChatId === chatId) {
    let finished = false, err = null;
    try {
      const s = await apiRoot(`/experts/status/${encodeURIComponent(chatId)}`);
      finished = !!s.finished;
      if (s.error) err = s.error;
      if (!selectionIsCurrent(generation, convId)) return;
      if (!finished) updateBusyStatus(s);
    } catch (e) {
      _dbg("pollChat: status 404/error → asumo terminado", e.message);
      finished = true;
    }
    if (finished) {
      if (!selectionIsCurrent(generation, convId)) return;
      setBusy(false);
      await loadMessages(convId);
      if (!selectionIsCurrent(generation, convId)) return;
      refreshContextBadge(convId);  // el medidor sale del historial recién guardado
      loadChats();  // refrescar el sidebar (status / última actividad)
      loadSuggestions(chatId, 4, generation);  // reintenta sola en background
      // El aviso sonoro espera al conteo de preguntas: "terminó" y "te
      // dejó una decisión pendiente" no son la misma noticia, y la
      // segunda es la que deja el run parado sin que nadie se entere.
      const pendientes = await refreshChatQuestions(convId, generation).catch(() => 0);
      if (!selectionIsCurrent(generation, convId)) return;
      // Un pedido grande no deja respuesta: deja un grafo corriendo. Sin
      // este poke el panel recién aparecería al reabrir la conversación.
      pokeGrafo().catch(() => {});
      if (err) {
        toast("El experto terminó con error: " + err, "err");
        showContinueBanner({ err, phaseAtEnd: activeChat?.lastPhase || null });
      }
      chime(err ? "error" : pendientes ? "question" : "done", convId,
            err || "");
      return;
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

function appendOptimisticUser(text) {
  const box = $("#chat-panel-messages");
  if (!box) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = bubbleHtml({ role: "user", content: text });
  box.appendChild(tmp.firstElementChild || tmp);
  box.scrollTop = box.scrollHeight;
}

// Respuesta de un `!comando`: misma burbuja que usa el experto, para que
// el markdown (tablas de !ayuda incluidas) se renderice igual.
function appendCommandReply(label, text) {
  const box = $("#chat-panel-messages");
  if (!box) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = bubbleHtml({ role: "assistant", content: `${label}\n\n${text}` });
  box.appendChild(tmp.firstElementChild || tmp);
  box.scrollTop = box.scrollHeight;
}

// Corrección de rumbo sobre el run en curso (2026-07-25). NO es un turno
// nuevo: se encola en el relay y el experto la aplica al cerrar la tool
// que tiene en vuelo, sobre el historial ya rescatado. La alternativa que
// existía era cancelar, que además de perder el hilo tiraba el avance.
async function steerCurrentRun(text) {
  const id = activeChat?.currentChatId;
  if (!id) { toast("El run recién arranca — prueba en un segundo", "warn"); return; }
  try {
    await apiRoot(`/experts/steer/${encodeURIComponent(id)}`, {
      method: "POST", body: JSON.stringify({ message: text }),
    });
    $("#chat-panel-input").value = "";
    // Sin burbuja optimista a propósito: el 🧭 lo pinta el poll cuando el
    // experto REALMENTE cortó y la tomó. Así ves si se aplicó o no.
    toast("Corrección encolada — se aplica al terminar la tool en curso", "ok");
  } catch (e) {
    if (e.status === 409 || e.status === 404) {
      // Terminó entre que escribiste y mandaste: es un mensaje normal.
      setBusy(false);
      await sendCurrentMessage();
      return;
    }
    toast("No se pudo mandar la corrección: " + e.message, "err");
  }
}

async function stopCurrentRun() {
  const id = activeChat?.currentChatId;
  if (!id) return;
  try {
    await apiRoot(`/experts/cancel/${encodeURIComponent(id)}`, { method: "POST" });
    toast("Run cortado — el avance queda guardado en el hilo", "ok");
  } catch (e) {
    toast("No se pudo cortar: " + e.message, "err");
  }
}

// ---------- adjuntos del composer (2026-07-31) ----------
// Se suben al elegirlos (no al enviar): así el error de tamaño o de tipo
// aparece en el acto y no después de escribir el mensaje. Viajan con el
// próximo envío y la bandeja se vacía.
let pendingAttachments = [];   // [{id, name, bytes, viewable, inline}]

function renderAttachTray() {
  const tray = $("#chat-attach-tray");
  if (!tray) return;
  tray.hidden = pendingAttachments.length === 0;
  tray.innerHTML = pendingAttachments.map((a, i) => {
    // El experto ve el texto inline y las imágenes; del resto solo el
    // nombre. Decirlo acá evita el "no puedo verlo" después del run.
    const nota = a.viewable
      ? (selectedModelSeesImages() ? "🖼 la ve" : "⚠ este modelo NO la ve")
      : a.inline ? "📄 la lee" : "⚠ solo nombre";
    return `<span class="chat-attach-chip" title="${escape(a.id)}">
      ${escape(a.name)} <em>${escape(nota)}</em>
      <button data-i="${i}" class="chat-attach-x" aria-label="Quitar">✕</button>
    </span>`;
  }).join("");
  $$(".chat-attach-x").forEach((b) => b.addEventListener("click", () => {
    pendingAttachments.splice(Number(b.dataset.i), 1);
    renderAttachTray();
  }));
}

// ¿El modelo que va a correr ve imágenes? Con "por proyecto" no lo
// sabemos desde acá (el default puede estar en el proyecto), así que
// devolvemos true y deja hablar al 400 del relay, que nombra el modelo
// real. Mentir para el otro lado sería asustar sin motivo.
function selectedModelSeesImages() {
  if (!chosenModel) return true;
  const m = modelCatalog.find((x) => x.spec === chosenModel);
  return m ? !!m.vision : true;
}

// Los tres roles NO ejecutores del runner por etapas. El ejecutor va
// aparte porque su <select> es el viejo #chat-model y su valor viaja en
// el campo `model` del POST, no en `stage_models`.
const ROLES_ETAPA = [
  ["planner", "#chat-role-planner"],
  ["verifier", "#chat-role-verifier"],
  ["documenter", "#chat-role-documenter"],
];
// Overrides por rol del PRÓXIMO mensaje. No se persisten en
// localStorage a propósito, al revés que `chosenModel`: mandar el
// verificador a otro modelo es una decisión de este turno, y que
// sobreviva a un F5 sin que se vea en la barra es justo cómo se
// terminan pagando runs con un modelo que nadie eligió hoy.
let chosenStageModels = {};

function opcionesDeModelos(vacio) {
  return `<option value="">${escape(vacio)}</option>` +
    // `notes` (la columna de la tabla `models`), no `note`: con el
    // nombre en singular el title salía siempre vacío. Y el texto cae
    // al spec — un modelo cargado a mano puede no tener label, y una
    // opción sin texto es una opción invisible.
    modelCatalog.map((m) =>
      `<option value="${escape(m.spec)}" title="${escape(m.notes || "")}">${
        m.vision ? "🖼 " : ""}${escape(m.label || m.spec)}</option>`).join("");
}

// Punto en el botón cuando hay algún override: sin esto, saber con qué
// va a correr el mensaje exige abrir el popover.
function pintarBotonModelo() {
  const wrap = $("#chat-model-btn")?.closest(".chat-model-wrap");
  if (!wrap) return;
  const hay = !!chosenModel || Object.values(chosenStageModels).some(Boolean);
  wrap.classList.toggle("tiene-override", hay);
}

async function loadModels() {
  const sel = $("#chat-model");
  if (!sel) return;
  try {
    const r = await api("models");
    modelCatalog = r.models || [];
    const porDefecto = modelCatalog.find((m) => m.spec === r.default);
    sel.innerHTML = opcionesDeModelos(
      `Modelo del proyecto${porDefecto
        ? ` (${porDefecto.label || porDefecto.spec})` : ""}`);
    if (chosenModel && !modelCatalog.some((m) => m.spec === chosenModel)) {
      chosenModel = "";                 // el modelo guardado ya no existe
      localStorage.removeItem("chat.model");
    }
    sel.value = chosenModel;
    for (const [rol, id] of ROLES_ETAPA) {
      const s2 = $(id);
      if (!s2) continue;
      s2.innerHTML = opcionesDeModelos("El de siempre");
      s2.value = chosenStageModels[rol] || "";
    }
    pintarBotonModelo();
  } catch (e) {
    _dbg("loadModels ERROR", e.message);
    // Sin catálogo el composer sigue andando: se esconde el botón
    // entero, no solo el <select> del ejecutor, para no dejar un
    // popover con cuatro desplegables vacíos.
    const wrap = $("#chat-model-btn")?.closest(".chat-model-wrap");
    if (wrap) wrap.hidden = true;
  }
}

function wireModelPopover() {
  const wrap = $("#chat-model-btn")?.closest(".chat-model-wrap");
  const btn = $("#chat-model-btn");
  if (!wrap || !btn) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const abierto = wrap.classList.toggle("open");
    btn.setAttribute("aria-expanded", abierto ? "true" : "false");
  });
  // Cerrar al tocar afuera o con Escape: un popover que solo cierra con
  // su propio botón tapa el composer justo cuando querés escribir.
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target) && wrap.classList.contains("open")) {
      wrap.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && wrap.classList.contains("open")) {
      wrap.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
    }
  });
  for (const [rol, id] of ROLES_ETAPA) {
    $(id)?.addEventListener("change", () => {
      const v = $(id)?.value || "";
      if (v) chosenStageModels[rol] = v;
      else delete chosenStageModels[rol];
      pintarBotonModelo();
    });
  }
}

function onModelChange() {
  chosenModel = $("#chat-model")?.value || "";
  if (chosenModel) localStorage.setItem("chat.model", chosenModel);
  else localStorage.removeItem("chat.model");
  pintarBotonModelo();
  // La bandeja dice "🖼 la ve" por adjunto: con un modelo ciego eso pasa
  // a ser mentira, así que se repinta.
  renderAttachTray();
  if (!selectedModelSeesImages() && pendingAttachments.some((a) => a.viewable)) {
    toast("Ese modelo no ve imágenes: el adjunto va a viajar como nombre "
          + "nomás. Elegí uno con 🖼 si querés que la mire.", "err", 7000);
  }
}

async function uploadAttachment(file) {
  const fd = new FormData();
  fd.append("file", file, file.name || "adjunto");
  try {
    const r = await apiRoot("/attachments", { method: "POST", body: fd }, 60_000);
    pendingAttachments.push({
      id: r.id, name: file.name || r.id, bytes: r.bytes,
      viewable: !!r.viewable, inline: !!r.inline,
    });
    renderAttachTray();
  } catch (e) {
    toast(`No pude subir ${file.name}: ${e.message}`, "err");
  }
}

function wireAttachments() {
  const input = $("#chat-attach-input");
  const btn = $("#chat-attach-btn");
  if (!input || !btn) return;
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    for (const f of input.files) await uploadAttachment(f);
    input.value = "";        // permite re-elegir el mismo archivo
  });
  // Pegar una captura del portapapeles: el caso más común (Win+Shift+S).
  $("#chat-panel-input")?.addEventListener("paste", async (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    for (const f of files) await uploadAttachment(f);
  });
  // Arrastrar sobre el composer.
  const bar = $("#chat-input-bar");
  if (bar) {
    bar.addEventListener("dragover", (e) => {
      e.preventDefault(); bar.classList.add("dragging");
    });
    bar.addEventListener("dragleave", () => bar.classList.remove("dragging"));
    bar.addEventListener("drop", async (e) => {
      e.preventDefault(); bar.classList.remove("dragging");
      for (const f of e.dataTransfer?.files || []) await uploadAttachment(f);
    });
  }
}

async function sendCurrentMessage() {
  if (!activeChat || activeChat.readOnly) return;
  const inp = $("#chat-panel-input");
  let text = inp.value.trim();

  // Comandos `/algo` de la UI (ver chat-tools.js). Se resuelven ACÁ y no
  // en el relay: son acciones del panel (compactar, cortar, ver diff) o
  // atajos que se expanden a un prompt. Los `!comando` del relay siguen
  // yendo al servidor como siempre.
  const hit = matchCommand(text);
  if (hit) {
    const out = hit.cmd.run(commandCtx(), hit.args);
    inp.value = "";
    if (out === false || out == null) return;   // el comando ya hizo lo suyo
    text = String(out);                          // se expandió a un prompt
    inp.value = text;
  }
  // Un adjunto sin texto es un mensaje válido ("mirá esta captura").
  if (!text && !pendingAttachments.length) return;
  if (activeChat.busy) { await steerCurrentRun(text); return; }
  inp.value = "";
  renderSuggestions([]);   // pertenecían al turno anterior
  setBusy(true);
  // Borrador (➕ Nuevo): la conversación se crea acá, con el prompt ya
  // escrito. Si falla, el texto vuelve al composer — nunca se pierde.
  if (activeChat.draft) {
    const convId = await createDraftConversation(activeChat.projectSlug);
    if (!convId) { inp.value = text; setBusy(false); return; }
    activeChat.convId = convId;
    activeChat.draft = false;
    $("#chat-draft-project").hidden = true;
    $("#chat-panel-messages").innerHTML = "";
    const meta = await apiRoot(`/conversations/${encodeURIComponent(convId)}`);
    if (meta && !meta.error) renderHeader(meta);
    loadChats();
  }
  // Los adjuntos se consumen en ESTE envío: se vacía la bandeja ya, para
  // que no se cuelen en el mensaje siguiente si el usuario manda dos
  // seguidos. Si el POST falla, vuelven junto con el texto.
  const enviados = pendingAttachments;
  pendingAttachments = [];
  renderAttachTray();
  // Queda pendiente hasta que el relay lo persista (al terminar el run).
  activeChat.pendingUserText = text;
  appendOptimisticUser(
    text + (enviados.length ? `\n📎 ${enviados.map((a) => a.name).join(", ")}` : ""));
  try {
    const r = await apiRoot("/experts/run", {
      method: "POST",
      body: JSON.stringify({
        target: activeChat.projectSlug,
        // Un adjunto sin texto es un mensaje válido acá arriba, pero
        // `/experts/run` exige `user` no vacío y devolvía 400 "user
        // requerido (string)": mandar una captura sin escribir nada no
        // llegaba nunca. El texto de relleno va SOLO en el POST — en
        // pantalla se sigue viendo el adjunto solo, que es lo que el
        // usuario mandó.
        user: text || "Mira el adjunto.",
        conversation: activeChat.convId, source: "ui", author: "ui",
        ...(chosenModel ? { model: chosenModel } : {}),
        ...(Object.keys(chosenStageModels).length
          ? { stage_models: { ...chosenStageModels } } : {}),
        ...(enviados.length ? { attachments: enviados.map((a) => a.id) } : {}),
      }),
    }, 30_000);
    // `!comando`: el relay lo resolvió de una (no hay run, no hay id que
    // pollear). Lo pintamos como respuesta y listo. No se persiste en la
    // conversación a propósito: el output de un comando no tiene por qué
    // viajar en el contexto de cada turno siguiente del experto — queda
    // en command_logs, que es donde se audita.
    if (r.command && typeof r.text === "string") {
      activeChat.pendingUserText = null;
      appendCommandReply(`!${r.command}`, r.text);
      setBusy(false);
      return;
    }
    // Etapas apagadas en el proyecto: el popover las ofrece igual porque
    // no conoce su config, así que el relay avisa cuál descartó. Sin
    // esto la elección se perdía en silencio.
    if (r.ignored_stages?.length) {
      const nombres = { planner: "planificador", verifier: "verificador",
                        documenter: "documentador" };
      toast(`${r.ignored_stages.map((s) => nombres[s] || s).join(", ")}: ` +
            "esa etapa está apagada en este proyecto, el modelo que " +
            "elegiste no se usó.", "warn");
    }
    activeChat.currentChatId = r.id;
    // OJO: NO llamar loadMessages() acá. La conversación se persiste recién
    // al terminar el run, así que re-renderizar ahora borra la burbuja
    // optimista y el usuario ve desaparecer su propio prompt. Los pasos en
    // vivo se appendean debajo; al terminar, pollChat hace el loadMessages
    // definitivo (con el historial completo + diffs).
    // El panel del plan se despierta acá y no solo al terminar: el
    // planificador deja su plan a los pocos segundos, y en un run largo
    // es justo mientras corre cuando querés mirarlo.
    pokeGrafo().catch(() => {});
    await pollChat(r.id, activeChat.convId);
  } catch (e) {
    toast("Error enviando mensaje: " + e.message, "err");
    // Devolvemos los adjuntos a la bandeja: ya están subidos, sería
    // absurdo hacer que el usuario los vuelva a elegir.
    pendingAttachments = enviados.concat(pendingAttachments);
    renderAttachTray();
    setBusy(false);
  }
}

// Lo que un comando `/algo` puede tocar. Se arma acá —y no se exporta el
// módulo entero— para que agregar un comando no dé acceso a todo el
// estado del panel: si un comando necesita algo nuevo, se suma una clave
// y queda a la vista en el diff.
function commandCtx() {
  return {
    conv: activeChat?.convId || "",
    project: activeChat?.projectSlug || "",
    busy: !!activeChat?.busy,
    compactar: () => compactActiveConversation(),
    cerrar: () => closeActiveConversation(),
    parar: () => stopCurrentRun(),
    verDiff: () => openConvDiff({ id: activeChat?.convId,
                                  branch: activeChat?.branch }),
    mostrarAyuda: () => appendCommandReply("/ayuda", helpText()),
    // Salida persistente en el hilo. Un toast se va solo, y hay comandos
    // que devuelven algo que necesitás DESPUÉS (un run_id, por ejemplo).
    responder: (label, texto) => appendCommandReply(label, texto),
    toast,
  };
}

// Banner persistente "continuar este hilo" cuando un run cortó.
function showContinueBanner({ err, phaseAtEnd }) {
  const box = $("#chat-panel-messages");
  if (!box) return;
  let banner = document.getElementById("chat-panel-continue-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "chat-panel-continue-banner";
    banner.className = "chat-banner";
    box.appendChild(banner);
  }
  const { title, hint } = explainRunError(err, phaseAtEnd);
  const raw = (err || "").trim();
  banner.innerHTML =
    `<div class="chat-banner-title">⚠ ${escape(title)}</div>
     <div class="chat-banner-hint">${hint}</div>` +
    (raw ? `<details class="chat-banner-raw">
       <summary>ver detalle técnico</summary>
       <pre>${escape(raw)}</pre></details>` : "");
  box.scrollTop = box.scrollHeight;
}

// Traduce el error crudo del run a algo humano + qué hacer. Sin esto la UI
// vomitaba el JSON del provider (`ModelHTTPError: status_code: 402, body:
// {...}`) en monoespaciado, que no le dice nada a nadie.
function explainRunError(err, phaseAtEnd) {
  const e = (err || "").toLowerCase();
  const cont = "Manda cualquier mensaje abajo para <strong>continuar este "
    + "hilo</strong>; el experto retoma donde cortó.";
  if (phaseAtEnd === "budget_exceeded")
    return { title: "Se acabó el presupuesto de pasos",
      hint: "La tarea era grande para un solo turno. " + cont };
  if (phaseAtEnd === "hard_timeout")
    return { title: "Superó el tope de tiempo",
      hint: "Venía trabajando, no colgado. " + cont };
  if (phaseAtEnd === "idle_timeout")
    return { title: "El provider dejó de responder",
      hint: "Quedó idle sin emitir eventos nuevos. " + cont };
  if (e.includes("insufficient balance") || e.includes("402"))
    return { title: "El provider del modelo se quedó sin saldo",
      hint: "Carga saldo en la cuenta del provider, o cambia de modelo "
        + "en <strong>Config</strong>. Después, " + cont.toLowerCase() };
  if (e.includes("429") || e.includes("rate limit"))
    return { title: "Rate limit del provider",
      hint: "Espera unos segundos. " + cont };
  if (e.includes("401") || e.includes("api key") || e.includes("unauthorized"))
    return { title: "Credencial del provider inválida",
      hint: "Revisa la API key en el <code>.env</code> del relay y reintenta." };
  if (e.includes("timeout"))
    return { title: "Timeout hablando con el provider", hint: cont };
  if (e === "cancelled")
    return { title: "Run cancelado",
      hint: "El avance hasta el corte quedó guardado en el hilo. " + cont };
  return { title: "El run cortó con un error", hint: cont };
}

async function maybeShowContinueBanner(meta, generation = chatSelectionGeneration) {
  try {
    const url = `/chats?project=${encodeURIComponent(meta.project_slug)}&limit=50`;
    const r = await apiRoot(url);
    if (!selectionIsCurrent(generation, meta.id)) return;
    const list = (r.chats || []).filter((c) => c.conversation_id === meta.id);
    if (!list.length) return;
    const last = list[list.length - 1];
    if (last.status === "error" || last.status === "cancelled") {
      showContinueBanner({ err: last.error || last.status, phaseAtEnd: last.phase_at_end || null });
    }
    // Al reabrir un hilo, los chips del último run siguen valiendo: es
    // exactamente el momento en que uno se pregunta "¿y ahora qué?".
    renderSuggestions(parseSuggestions(last));
  } catch (_) { /* best-effort */ }
}

// =====================================================================
// SUGERENCIAS DE CONTINUACIÓN (chips arriba del composer)
// =====================================================================
// El relay las genera al cerrar cada run y las guarda en la fila del
// chat (chats.suggestions, JSON array). Acá son botones: un click manda
// ese texto como mensaje nuevo. Escribir el próximo paso era la mayor
// fricción del hilo — sobre todo desde el celular (reporte 2026-07-26).

function parseSuggestions(chat) {
  try {
    const v = JSON.parse(chat?.suggestions || "[]");
    return Array.isArray(v) ? v : [];
  } catch (_) { return []; }
}

function renderSuggestions(list) {
  const box = $("#chat-suggestions");
  if (!box) return;
  box.innerHTML = "";
  const items = (list || []).filter((s) => (s || "").trim());
  box.hidden = !items.length || !!activeChat?.readOnly;
  if (box.hidden) return;
  for (const text of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chat-suggestion";
    b.textContent = text;
    b.title = "Mandar: " + text;
    b.addEventListener("click", () => {
      const inp = $("#chat-panel-input");
      if (!inp) return;
      inp.value = text;
      renderSuggestions([]);   // el chip ya se usó: no invita a doble tap
      sendCurrentMessage();
    });
    box.appendChild(b);
  }
}

// El relay escribe las sugerencias DESPUÉS de cerrar el run (es otro turno
// de LLM: tarda segundos), así que el primer GET tras `finished` casi
// siempre las trae vacías. Sin reintento los chips no aparecían hasta
// recargar la página (reporte 2026-07-27).
// Preguntas abiertas del experto (2026-08-16). Se pintan como tarjeta
// con botones: la pregunta también viaja dentro del texto de la
// respuesta, pero un click es menos fricción que escribir un párrafo.
// Responder manda el `resume_prompt` como turno siguiente, así el hilo
// retoma sin que el humano tenga que explicar de nuevo el contexto.
async function refreshChatQuestions(expectedConvId = null, expectedGeneration = null) {
  const convId = expectedConvId || activeChat?.convId;
  const isCurrent = () => !!activeChat?.convId && activeChat.convId === convId
    && !activeChat.readOnly
    && (expectedGeneration === null
      || expectedGeneration === chatSelectionGeneration);
  if (!isCurrent()) return 0;
  const box = $("#chat-panel-messages");
  const n = await refreshQuestions(box, convId, (resume) => {
    if (!isCurrent()) return;
    const inp = $("#chat-panel-input");
    if (inp && resume) { inp.value = resume; sendCurrentMessage(); }
  });
  if (!isCurrent()) return 0;
  if (n) renderSuggestions([]);   // primero contestá; los chips después
  return n;
}

async function loadSuggestions(chatId, tries = 4, generation = chatSelectionGeneration) {
  if (!chatId) return;
  for (let i = 0; i < tries; i++) {
    try {
      const list = parseSuggestions(
        await apiRoot(`/chats/${encodeURIComponent(chatId)}`));
      if (generation !== chatSelectionGeneration) return;
      if (list.length) { renderSuggestions(list); return; }
    } catch (_) { /* sin chips se escribe a mano, como siempre */ }
    if (generation !== chatSelectionGeneration
        || activeChat?.currentChatId !== chatId) return; // cambiaste de hilo
    await new Promise((r) => setTimeout(r, 3000));
  }
}

// =====================================================================
// raw .md viewer (lo abre "En curso" vía import)
// =====================================================================

export async function viewChat(id) {
  try {
    const r = await fetch(`/chats/${encodeURIComponent(id)}/md`);
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    const text = await r.text();
    // Saltar al tab Chat si no está visible.
    if ($("#tab-chat").hidden) document.querySelector('.tab[data-tab="chat"]')?.click();
    $("#chat-viewer-title").textContent = id;
    $("#chat-viewer-md").textContent = text;
    $("#chat-viewer").hidden = false;
  } catch (e) {
    toast("No se pudo leer el .md: " + e.message, "err");
  }
}

// =====================================================================
// Nuevo chat (borrador en el panel, sin modal)
// =====================================================================
// ➕ Nuevo abre el panel de burbujas vacío con el selector de proyecto en
// el header; el primer mensaje se escribe en el composer de siempre y la
// conversación (POST /conversations → rama git) se crea recién al enviar.
// Antes esto era un modal: un click afuera lo cerraba y se perdía el
// prompt tipeado, y crear la rama antes de escribir dejaba conversaciones
// vacías bloqueando el "ya hay una abierta" (409).

async function openNewChatDraft() {
  const sel = $("#chat-draft-project");
  if (!sel) { toast("UI de chats no montada (refresca)", "err"); return; }
  const generation = ++chatSelectionGeneration;
  if (sel.options.length === 0) {
    const { projects } = await api("projects");
    if (generation !== chatSelectionGeneration) return;
    sel.innerHTML = projects.filter((p) => p.enabled)
      .map((p) => `<option value="${escape(p.slug)}">${escape(p.slug)}</option>`).join("");
  }
  sel.value = $("#chat-project-filter")?.value || sel.options[0]?.value || "";
  if (!sel.value) { toast("No hay proyectos habilitados", "warn"); return; }

  activeChat = {
    convId: null, projectSlug: sel.value, currentChatId: null,
    busy: false, readOnly: false, draft: true,
  };
  sel.onchange = () => { if (activeChat?.draft) activeChat.projectSlug = sel.value; };

  showMainView("convo");
  renderConvList();
  detachGrafo();          // un borrador todavía no tiene plan
  sel.hidden = false;
  $("#chat-panel-title").textContent = "nueva conversación";
  $("#chat-convo-status").textContent = "borrador";
  $("#chat-convo-status").className = "badge dim";
  $("#chat-panel-meta").textContent = "se crea al mandar el primer mensaje";
  for (const id of ["#chat-panel-link-discord", "#chat-panel-unlink-discord",
                    "#chat-panel-follow-mobile", "#chat-panel-chime",
                    "#chat-panel-close-conv", "#chat-panel-vscode",
                    "#chat-panel-compact", "#chat-panel-context",
                    "#chat-panel-extract-facts",
                    "#chat-panel-diff"]) $(id).hidden = true;
  $("#chat-summary").hidden = true;
  $("#chat-panel-messages").innerHTML =
    `<div class="chat-banner"><div class="chat-banner-title">💬 Nueva conversación</div>
     <div class="chat-banner-hint">Escribe abajo el primer mensaje (Ctrl+Enter envía).
     Al enviarlo se abre la rama de trabajo del proyecto.</div></div>`;
  $("#chat-input-bar").hidden = false;
  $("#chat-readonly-note").hidden = true;
  setBusy(false);
  $("#chat-panel-input").focus();
}

// Crea la conversación del borrador. Devuelve el id, o null si falló
// (con el porqué en un toast — el texto del composer lo restaura el caller).
async function createDraftConversation(slug) {
  try {
    return (await apiRoot("/conversations", {
      method: "POST", body: JSON.stringify({ project: slug }),
    })).id;
  } catch (e) {
    toast(newChatErrorText(e, slug), "err", 9000);
    return null;
  }
}

function newChatErrorText(e, slug) {
  const msg = e?.message || String(e);
  if (e?.status === 422 || /working tree|sin commitear|rama de trabajo/i.test(msg)) {
    return `El repo ${slug} tiene cambios sin commitear, así que no puedo abrir `
      + `una rama de trabajo limpia. Haz commit o haz stash y reintenta.`;
  }
  if (e?.status === 409) {
    return `Ya hay una conversación abierta para ${slug}. Abrila desde el sidebar `
      + `(grupo Abiertas) y sigue ahí, o cerrala con 🔒 Cerrar en el header.`;
  }
  return "error: " + msg;
}

// =====================================================================
// Bridge Discord (vincular / desvincular)
// =====================================================================

async function refreshConvMeta(convId) {
  const r = await apiRoot(`/conversations/${encodeURIComponent(convId)}`);
  return (!r || r.error) ? null : r;
}

// Sonda del bot para el modal: pinta el estado y devuelve si el gateway
// está vivo. Sin esto el usuario aprieta Vincular, falla, y recién ahí se
// entera de que el bot estaba caído.
async function _preflightBot() {
  const botEl = $("#link-discord-bot"), startBtn = $("#link-discord-start-bot");
  const ok = $("#link-discord-ok");
  if (botEl) botEl.textContent = "sondeando el bot…";
  let st = null;
  try { st = await api("bot/status"); } catch (_) { /* sonda caída */ }
  const gw = st && st.gateway === "up";
  if (botEl) {
    botEl.textContent = gw
      ? "✓ bot conectado a Discord"
      : "✗ el bot no está conectado a Discord: " +
        ((st && st.detail) || "no responde la sonda");
    botEl.className = "mt-3 text-xs " + (gw ? "text-emerald-400" : "text-amber-400");
  }
  if (startBtn) startBtn.hidden = gw;
  if (ok) ok.disabled = !gw;
  return gw;
}

async function openLinkDiscordModal() {
  if (!activeChat) return;
  const userIdEl = $("#link-discord-user-id"), authorEl = $("#link-discord-author");
  const msgEl = $("#link-discord-msg"), ok = $("#link-discord-ok");
  if (!userIdEl || !authorEl || !msgEl || !ok) { toast("UI link-discord no montada (refresca)", "err"); return; }
  userIdEl.value = ""; authorEl.value = "";
  try {
    const meta = await refreshConvMeta(activeChat.convId);
    if (meta && meta.discord_user_id) {
      userIdEl.value = meta.discord_user_id;
      authorEl.value = meta.discord_author || "";
    }
  } catch (_) { /* modal vacío */ }
  msgEl.textContent = ""; msgEl.className = "mt-2 text-xs text-zinc-400"; ok.disabled = false;
  openModal("link-discord-modal");
  // Después del reset de arriba: la sonda es la última en escribir sobre
  // `ok.disabled`, si no la carrera la deja habilitada con el bot caído.
  _preflightBot();
  setTimeout(() => userIdEl.focus(), 50);

  if (ok._ldHandler) ok.removeEventListener("click", ok._ldHandler);
  const handler = async () => {
    const userId = userIdEl.value.trim(), author = authorEl.value.trim();
    if (!userId) {
      msgEl.textContent = "discord_user_id requerido (el número, no el @usuario)";
      msgEl.className = "mt-2 text-xs text-amber-400"; userIdEl.focus(); return;
    }
    if (!/^\d{15,25}$/.test(userId)) {
      msgEl.textContent = "el ID parece inválido (esperaba 17+ dígitos). Copy User ID en Discord.";
      msgEl.className = "mt-2 text-xs text-amber-400"; userIdEl.focus(); return;
    }
    ok.disabled = true; msgEl.textContent = "vinculando…"; msgEl.className = "mt-2 text-xs text-zinc-400";
    try {
      const r = await apiRoot(`/conversations/${encodeURIComponent(activeChat.convId)}/set-discord-user`,
        { method: "POST", body: JSON.stringify({ discord_user_id: userId, discord_author: author, create_thread: true }) });
      if (r.error) throw new Error(r.error);
      const meta = await refreshConvMeta(activeChat.convId);
      if (meta) renderHeader(meta);
      closeModal("link-discord-modal");
      // Desde 2026-08-16 el relay no vincula sin hilo: si llegamos acá,
      // el DM existe. Un 502 (sin vínculo escrito) cae en el catch.
      toast(`Vinculado a Discord ✓ · Hilo creado `
        + `(${(r.discord_thread_id || "").slice(0, 10)}…)`, "ok");
    } catch (e) {
      msgEl.textContent = "no se vinculó: " + e.message;
      msgEl.className = "mt-2 text-xs text-red-400";
      ok.disabled = false;
      // El 502 trae la sonda del bot en el body: si el gateway está
      // caído, ofrecemos arrancarlo sin salir del modal.
      _preflightBot();
    }
  };
  ok._ldHandler = handler;
  ok.addEventListener("click", handler);
}

// "Seguir en el celu": un click. El relay resuelve el usuario —el que la
// conversación ya tenga, o el default configurado— y pide el DM. Reemplaza
// al viejo "reintentar DM": reparar un vínculo sin hilo y vincular por
// primera vez terminan siendo la misma operación.
async function followMobile() {
  if (!activeChat) return;
  const btn = $("#chat-panel-follow-mobile");
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ creando DM…"; }
  try {
    await apiRoot(`/conversations/${encodeURIComponent(activeChat.convId)}/set-discord-user`,
      { method: "POST", body: { create_thread: true } });
    const fresh = await refreshConvMeta(activeChat.convId);
    if (fresh) renderHeader(fresh);
    toast("Listo: te llega al DM de Discord 📱", "ok");
  } catch (e) {
    // 400 needs_user_id = no hay default configurado. En vez de un error
    // sin salida, abrimos el modal, que es exactamente lo que falta.
    if (e.status === 400 && /needs_user_id|default/i.test(e.body || "")) {
      toast("No hay un Discord por default: elegí el usuario", "warn");
      openLinkDiscordModal();
    } else {
      toast("No se pudo: " + e.message, "err");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label || "📱 Seguir en el celu"; }
  }
}

// 🔔/🔕 por conversación. El estado lo manda localStorage, no el relay.
function renderChimeBtn(convId) {
  const btn = $("#chat-panel-chime");
  if (!btn) return;
  const muted = isMuted(convId);
  btn.hidden = false;
  btn.textContent = muted ? "🔕" : "🔔";
  btn.title = muted
    ? "Sin sonido en esta conversación — click para activarlo"
    : "Suena al terminar — click para silenciar esta conversación";
}

function onToggleChime() {
  if (!activeChat?.convId) return;
  const muted = toggleMute(activeChat.convId);
  renderChimeBtn(activeChat.convId);
  if (!muted) pedirPermisoNotificaciones();   // el permiso se pide acá, no en medio de un run
  toast(muted ? "Silenciada esta conversación 🔕" : "Aviso sonoro activado 🔔", "ok");
}

async function startBotFromModal() {
  const btn = $("#link-discord-start-bot");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ arrancando…"; }
  try {
    const r = await api("bot/start", { method: "POST" }, 30_000);
    if (!r.ok) toast("No arrancó: " + (r.detail || "sin detalle"), "err");
  } catch (e) {
    toast("No se pudo arrancar el bot: " + e.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Arrancar bot"; }
    _preflightBot();
  }
}

async function unlinkDiscord() {
  if (!activeChat) return;
  try {
    const r = await apiRoot(`/conversations/${encodeURIComponent(activeChat.convId)}/set-discord-user`,
      { method: "POST", body: JSON.stringify({ discord_user_id: null }) });
    if (r.error) throw new Error(r.error);
    const meta = await refreshConvMeta(activeChat.convId);
    if (meta) renderHeader(meta);
    toast("Desvinculado de Discord ✓", "ok");
  } catch (e) {
    toast("No se pudo desvincular: " + e.message, "err");
  }
}

async function onOpenInVSCode() {
  if (!activeChat) return;
  toast("Abriendo VS Code…", "ok");
  const r = await openProjectInVSCode(activeChat.projectSlug);
  if (!r.ok) toast("No se pudo abrir VS Code: " + r.error, "err");
}

// =====================================================================
// Nota rápida (atajo a /notes)
// =====================================================================

function openQuickNote() {
  const inp = $("#quick-note-input"), msg = $("#quick-note-msg"), ok = $("#quick-note-ok");
  if (!inp || !ok || !msg) { toast("UI quick-note no montada (refresca)", "err"); return; }
  inp.value = ""; msg.textContent = "";
  openModal("quick-note-modal");
  setTimeout(() => inp.focus(), 50);
  if (ok._qnHandler) ok.removeEventListener("click", ok._qnHandler);
  const handler = async () => {
    const text = inp.value.trim();
    if (!text) { msg.textContent = "escribe algo primero"; msg.className = "mt-2 text-xs text-amber-400"; return; }
    ok.disabled = true;
    try {
      await api("notes", { method: "POST", body: JSON.stringify({ user: text, system_prompt: "" }) });
      msg.textContent = "enviada ✓"; msg.className = "mt-2 text-xs text-emerald-400";
      setTimeout(() => closeModal("quick-note-modal"), 350);
    } catch (e) {
      msg.textContent = "error: " + e.message; msg.className = "mt-2 text-xs text-red-400";
    } finally { ok.disabled = false; }
  };
  ok._qnHandler = handler;
  ok.addEventListener("click", handler);
  if (inp._qnKeyHandler) inp.removeEventListener("keydown", inp._qnKeyHandler);
  const keyHandler = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handler(); }
  };
  inp._qnKeyHandler = keyHandler;
  inp.addEventListener("keydown", keyHandler);
}

// =====================================================================
// Memoria del proyecto (facts + FTS5) — movida de tab-conversations
// =====================================================================

function showMemoria() {
  showMainView("memoria");
  loadFacts();
  searchMemories(true);
}

const FACT_BADGES = {
  pending: '<span class="badge warn" title="todavía NO lo ve el experto">⏳ pendiente</span>',
  approved: '<span class="badge ok" title="se inyecta si el proyecto tiene facts_always_on">✓ aprobado</span>',
  rejected: '<span class="badge dim" title="descartado; queda para auditar al compactador">✕ rechazado</span>',
};

// El icono de tacho, que se repite en cada fila.
const ICONO_TACHO = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 4h12M5.3 4V2.7A.7.7 0 0 1 6 2h4a.7.7 0 0 1 .7.7V4m2 0v9.3a.7.7 0 0 1-.7.7H4a.7.7 0 0 1-.7-.7V4"/></svg>`;
// Gemelos del tacho para los otros dos botones de la fila de facts:
// ✓ y ✕ eran icon-only y un screen reader no los anuncia. SVG + aria.
const ICONO_CHECK = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5l3 3 7-7"/></svg>`;
const ICONO_X = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>`;

async function loadFacts() {
  const project = $("#chat-project-filter")?.value || "";
  const tbody = $("#chat-facts-table tbody");
  const summary = $("#chat-facts-summary");
  const estado = $("#chat-facts-status")?.value ?? "pending";
  if (!project) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">elige un proyecto en el filtro de la izquierda</td></tr>`;
    if (summary) summary.textContent = "";
    return;
  }
  try {
    const qs = `project=${encodeURIComponent(project)}&limit=100`
      + (estado ? `&status=${encodeURIComponent(estado)}` : "");
    const r = await api(`conversations/facts?${qs}`);
    const facts = r.facts || [];
    // El contador de pendientes va SIEMPRE, aunque estés mirando otro
    // filtro: una cola de aprobación que no se ve es una cola que no se
    // atiende (pasó con los 13 borradores de skills).
    if (summary) {
      const pend = r.pendientes ?? 0;
      summary.textContent = `${facts.length} facts`
        + (pend ? ` · ${pend} esperando aprobación` : "");
    }
    if (!facts.length) {
      const vacio = estado === "pending"
        ? `no hay facts esperando aprobación en ${escape(project)}.`
        : `${escape(project)} no tiene facts en este estado (el compactador los genera al cerrar o compactar una conversación).`;
      tbody.innerHTML = `<tr><td colspan="5" class="empty">${vacio}</td></tr>`;
      return;
    }
    tbody.innerHTML = facts.map((f) => {
      const st = f.status || "approved";
      const acciones = [
        st !== "approved"
          ? `<button class="btn btn-xs fact-ok" data-id="${f.id}" title="aprobar: el experto empieza a verlo"
              aria-label="aprobar">${ICONO_CHECK}</button>`
          : "",
        st !== "rejected"
          ? `<button class="btn btn-xs fact-no" data-id="${f.id}" title="rechazar: deja de inyectarse pero queda registrado"
              aria-label="rechazar">${ICONO_X}</button>`
          : "",
        `<button class="btn btn-xs danger fact-del" data-id="${f.id}" title="borrar">${ICONO_TACHO}</button>`,
      ].join(" ");
      return `<tr>
      <td>${escape(f.fact)}</td>
      <td class="whitespace-nowrap">${FACT_BADGES[st] || escape(st)}</td>
      <td><code>${escape(f.source_conversation ? f.source_conversation.slice(0, 8) + "…" : "a mano")}</code></td>
      <td class="whitespace-nowrap">${escape(f.created_at || "—")}</td>
      <td class="whitespace-nowrap">${acciones}</td>
    </tr>`;
    }).join("");
    tbody.querySelectorAll(".fact-del").forEach((b) =>
      b.onclick = () => deleteFact(Number(b.dataset.id)));
    tbody.querySelectorAll(".fact-ok").forEach((b) =>
      b.onclick = () => setFactStatus(Number(b.dataset.id), "approved"));
    tbody.querySelectorAll(".fact-no").forEach((b) =>
      b.onclick = () => setFactStatus(Number(b.dataset.id), "rejected"));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty fail">error: ${escape(e.message)}</td></tr>`;
  }
}

async function setFactStatus(id, status) {
  try {
    await api(`facts/${id}`, {
      method: "PATCH", body: JSON.stringify({ status }),
    });
    toast(status === "approved"
      ? `Fact #${id} aprobado — el experto ya lo ve.`
      : `Fact #${id} rechazado.`, "ok");
    loadFacts();
  } catch (e) {
    toast("No pude cambiar el estado: " + e.message, "err");
  }
}

// Alta manual. Nace aprobado: lo escribió una persona.
async function addFactManual() {
  const project = $("#chat-project-filter")?.value || "";
  const input = $("#chat-fact-nuevo");
  const fact = (input?.value || "").trim();
  if (!project) { toast("Elegí un proyecto primero.", "err"); return; }
  if (!fact) { toast("Escribí el fact.", "err"); return; }
  try {
    await api("conversations/facts", {
      method: "POST", body: JSON.stringify({ project, fact }),
    });
    input.value = "";
    toast("Fact agregado (aprobado).", "ok");
    // Saltar al filtro donde va a estar: si quedás en "pendientes" no lo
    // ves y parece que no se guardó.
    const sel = $("#chat-facts-status");
    if (sel && sel.value === "pending") sel.value = "approved";
    loadFacts();
  } catch (e) {
    toast("No pude agregar el fact: " + e.message, "err");
  }
}

async function deleteFact(id) {
  if (!await confirmModal({
    title: `Borrar fact #${id}`,
    body: "¿Borrar el fact? Deja de aparecer en el retrieval. No se puede deshacer.",
    confirmText: "Borrar", danger: true,
  })) return;
  try {
    await api(`facts/${id}`, { method: "DELETE" });
    toast(`Fact #${id} borrado.`, "info");
    loadFacts();
  } catch (e) {
    toast("No pude borrar el fact: " + e.message, "err");
  }
}

async function searchMemories(skipQuery) {
  const project = $("#chat-project-filter")?.value || "";
  const tbody = $("#chat-mem-table tbody");
  const summaryEl = $("#chat-mem-summary");
  if (!project) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">elige un proyecto primero</td></tr>`;
    if (summaryEl) summaryEl.textContent = "";
    return;
  }
  const q = skipQuery ? "" : ($("#chat-mem-query")?.value.trim() || "");
  tbody.innerHTML = `<tr><td colspan="4" class="empty">buscando…</td></tr>`;
  try {
    const url = `conversations/memories?project=${encodeURIComponent(project)}`
      + (q ? `&q=${encodeURIComponent(q)}` : "") + "&limit=10";
    const r = await api(url);
    const badge = $("#chat-mem-badge");
    if (badge) badge.innerHTML = r.fts_available
      ? `<span class="badge ok">FTS5 activo</span>`
      : `<span class="badge warn">FTS5 no compilado — cae a recientes</span>`;
    const hits = r.hits || [];
    if (summaryEl) summaryEl.textContent = `${hits.length} hits` + (q ? ` para "${q}"` : " (recientes)");
    if (!hits.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">sin hits${q ? ` para "${escape(q)}"` : ""}</td></tr>`;
      return;
    }
    tbody.innerHTML = hits.map((h) => {
      const cid = h.conversation_id || "";
      const rank = h.rank != null ? h.rank.toFixed(2) : "—";
      const summary = h.summary || "(sin summary)";
      const snippet = summary.length > 400 ? summary.slice(0, 400) + "…" : summary;
      return `<tr data-id="${escape(cid)}" class="mem-row" title="click para abrir">
        <td><code>${escape(cid.slice(0, 8))}…</code></td>
        <td class="tabular-nums">${escape(rank)}</td>
        <td class="text-zinc-400">${escape(snippet)}</td>
        <td><button class="btn btn-xs danger mem-del" data-id="${escape(cid)}" title="sacar del retrieval"><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 4h12M5.3 4V2.7A.7.7 0 0 1 6 2h4a.7.7 0 0 1 .7.7V4m2 0v9.3a.7.7 0 0 1-.7-.7H4a.7.7 0 0 1-.7-.7V4"/></svg></button></td>
      </tr>`;
    }).join("");
    $$(".mem-row").forEach((tr) =>
      tr.addEventListener("click", () => { showMainView("convo"); selectConversation(tr.dataset.id); }));
    $$(".mem-del").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); deleteMemory(b.dataset.id); }));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty fail">error: ${escape(e.message)}</td></tr>`;
    if (summaryEl) summaryEl.textContent = "error";
  }
}

async function deleteMemory(convId) {
  if (!await confirmModal({
    title: `Borrar memoria ${convId.slice(0, 8)}…`,
    body: "¿Sacar del retrieval? Borra summary + FTS5; el historial queda intacto.",
    confirmText: "Sacar", danger: true,
  })) return;
  try {
    await api(`conversations/${encodeURIComponent(convId)}/memory`, { method: "DELETE" });
    toast("Memoria borrada del retrieval.", "info");
    searchMemories(true);
  } catch (e) {
    toast("No pude borrar la memoria: " + e.message, "err");
  }
}

// =====================================================================
// wire-up
// =====================================================================

export function initChats() {
  ensureMd().catch(() => {});   // precarga: el primer hilo abre ya con markdown
  wireChatObjects();
  // Sidebar
  wireSidebarCollapse();
  wireChatDrawer();
  wireGrafoPanel();
  wireAttachments();
  loadModels();
  on("#chat-model", "change", onModelChange);
  wireModelPopover();
  on("#chat-new", "click", openNewChatDraft);
  on("#chat-refresh", "click", loadChats);
  on("#chat-quick-note", "click", openQuickNote);
  on("#chat-search", "input", renderConvList);
  // `input` y no solo `change`: elegir del datalist dispara `input` en
  // todos los navegadores, `change` recién al salir del campo. Con solo
  // `change`, clickear un proyecto de la lista no filtraba nada hasta
  // que movieras el foco. Con debounce porque `input` es por tecla, y
  // "sample-app" a pelo son seis GET /conversations.
  let filtroTimer = null;
  const filtrarProyecto = () => {
    clearTimeout(filtroTimer);
    filtroTimer = setTimeout(() => {
      loadChats();
      if (!$("#chat-memoria").hidden) { loadFacts(); searchMemories(true); }
    }, 250);
  };
  for (const ev of ["change", "input"]) {
    on("#chat-project-filter", ev, filtrarProyecto);
  }
  on("#chat-memoria-toggle", "click", showMemoria);
  on("#chat-memoria-back", "click", () =>
    showMainView(activeChat ? "convo" : "empty"));

  // Panel
  on("#chat-panel-send", "click", sendCurrentMessage);
  on("#chat-panel-steer", "click", sendCurrentMessage);  // busy ⇒ va a steer
  on("#chat-panel-stop", "click", stopCurrentRun);
  on("#chat-panel-vscode", "click", onOpenInVSCode);
  on("#chat-panel-link-discord", "click", openLinkDiscordModal);
  on("#chat-panel-unlink-discord", "click", unlinkDiscord);
  on("#chat-panel-follow-mobile", "click", followMobile);
  on("#chat-panel-chime", "click", onToggleChime);
  on("#link-discord-start-bot", "click", startBotFromModal);
  on("#chat-panel-compact", "click", compactActiveConversation);
  on("#chat-panel-close-conv", "click", closeActiveConversation);
  on("#chat-viewer-close", "click", () => { $("#chat-viewer").hidden = true; });

  // Memoria
  on("#chat-mem-search", "click", () => searchMemories(false));
  on("#chat-mem-recent", "click", () => searchMemories(true));
  on("#chat-mem-query", "keydown", (e) => {
    if (e.key === "Enter") searchMemories(false);
  });
  // Facts: filtro por estado + alta manual (2026-08-21).
  on("#chat-facts-status", "change", loadFacts);
  on("#chat-fact-add", "click", addFactManual);
  on("#chat-fact-nuevo", "keydown", (e) => {
    if (e.key === "Enter") addFactManual();
  });
  on("#chat-panel-extract-facts", "click", extractFactsFromConversation);

  // Ctrl+Enter envía. Si la paleta de comandos está abierta, el Enter es
  // suyo (elige la opción marcada) y no manda el mensaje.
  on("#chat-panel-input", "keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      if (paletteOpen()) return;
      e.preventDefault();
      sendCurrentMessage();
    }
  });
  // Paleta `/comando` en el composer (chat-tools.js). Es una lista
  // enchufable: sumar un comando allá lo hace aparecer acá solo.
  wireCommandPalette($("#chat-panel-input"), sendCurrentMessage);
}
