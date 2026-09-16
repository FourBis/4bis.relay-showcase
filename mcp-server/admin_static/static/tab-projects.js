// Tab Proyectos: CRUD de expertos + modales de system prompt, git diff,
// modo nocturno, edición, ejecución de experto, drilldown cbm y run report.
// Layout: 3 sub-grupos de acciones (Ver / Configurar / Peligrosas) con
// íconos + tooltips, todos uniformes.

import { $, $$, api, apiRoot, escape, _dbg, openProjectInVSCode, gitWebUrl, onClick } from "./api.js";
import { modalLoad, toast, confirmModal, openSidePanel, closeSidePanel,
         setSidePanelBody, isSidePanelCurrent, sidepanelGen } from "./ui.js";
import { dataTable } from "./ui-table.js";
import { refreshStatus } from "./tab-status.js";
import { renderColumnas } from "./board-view.js";
import { loadOrphans } from "./tab-orphans.js";

// ---- iconos de fila ----
//
// Eran emoji (\U0001f5a5 \U0001f419 \U0001f4cb ⚙ \U0001f9ea). Un emoji no hereda `currentColor`, no
// tiene estado disabled, cambia de forma segun la plataforma y no se
// puede etiquetar: como boton de accion es un dibujo, no un control.
// Mismo trazo que los iconos de la nav (24px, stroke 1.7).
const _svg = (d, cls = "h-4 w-4") =>
  `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
     aria-hidden="true">${d}</svg>`;

const ICONS = {
  vscode: '<rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8 20h8M12 16.5V20"/>',
  remoto: '<path d="M14 5h5v5M19 5l-8 8"/><path d="M18 13.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4.5"/>',
  tablero: '<rect x="3.5" y="4.5" width="5" height="15" rx="1"/><rect x="9.5" y="4.5" width="5" height="10" rx="1"/><rect x="15.5" y="4.5" width="5" height="13" rx="1"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M5.9 5.9l1.6 1.6M16.5 16.5l1.6 1.6M18.1 5.9l-1.6 1.6M7.5 16.5l-1.6 1.6"/>',
  correr: '<path d="M8 5.5l10 6.5-10 6.5z"/>',
  canal: '<path d="M9.5 4L7.5 20M16.5 4l-2 16M4.5 9h15M3.5 15h15"/>',
  si: '<path d="M20 6L9 17l-5-5"/>',
  no: '<path d="M6 12h12"/>',
};

// Celda booleana. Antes eran cuatro columnas que decian "si" en verde,
// que a la altura de la quinta fila es una pared de texto identico.
// El icono lleva su alternativa textual: sin `aria-label` una celda
// icon-only no dice nada a un lector de pantalla.
function boolCell(v, siTxt, noTxt) {
  const t = v ? siTxt : noTxt;
  return `<span class="inline-flex ${v ? "idx-yes" : "idx-no"}" role="img"
    title="${escape(t)}" aria-label="${escape(t)}">
    ${_svg(v ? ICONS.si : ICONS.no)}</span>`;
}

function iconBtn(icon, title, cls, slug) {
  return `<button class="icon-btn ${cls}" data-slug="${escape(slug)}"
    title="${escape(title)}" aria-label="${escape(title)}">${_svg(icon)}</button>`;
}

function actionGroup(buttons) {
  return `<span class="action-group">${buttons.join("")}</span>`;
}

// El guild sale del mismo GET que las filas, pero lo necesitan los
// `render` de las columnas, que se definen una sola vez.
let _guildId = "";
let _loadError = "";
let _tabla = null;

function _celdaCanal(p) {
  const dcId = p.discord_channel_id || "";
  const ico = _svg(ICONS.canal, "h-3.5 w-3.5");
  if (dcId && _guildId) {
    const url = `https://discord.com/channels/${encodeURIComponent(_guildId)}/${encodeURIComponent(dcId)}`;
    return `<a href="${url}" target="_blank" rel="noopener"
      class="idx-yes inline-flex items-center gap-1.5"
      title="abrir el canal en Discord (id: ${escape(dcId)})">${ico} vinculado</a>`;
  }
  if (dcId) {
    return `<span class="idx-yes inline-flex items-center gap-1.5"
      title="canal vinculado (id: ${escape(dcId)}) — seteá DISCORD_GUILD_ID en Config para linkearlo">${ico} vinculado</span>`;
  }
  return `<span class="idx-no inline-flex items-center gap-1.5"
    title="sin canal: el bot preguntará al admin al próximo run discord">${ico} sin canal</span>`;
}

function _celdaTablero(p) {
  const gp = p.github_project;
  return gp
    ? `<a href="${escape(gp.url || "#")}" target="_blank" rel="noopener"
         class="text-sky-300 hover:underline"
         title="${escape(gp.title || "")} (${escape(gp.owner || "")})">#${gp.number}</a>`
    : `<span class="idx-no" title="sin tablero: vinculalo desde la acción Tablero de la fila">—</span>`;
}

const COLUMNAS = [
  { key: "name", label: "Proyecto",
    render: (p) => `<span class="font-medium">${escape(p.name)}</span>` },
  { key: "enabled", label: "On", className: "text-center",
    value: (p) => (p.enabled ? 1 : 0),
    render: (p) => boolCell(p.enabled, "habilitado", "apagado") },
  // Las dos siguientes NO son booleanos de lectura sino switches que
  // escriben: siguen siendo checkbox. Ordenables igual, que es lo que
  // uno quiere para juntar los que están prendidos.
  { key: "include_in_index", label: "En índice", className: "text-center",
    value: (p) => (p.include_in_index !== false ? 1 : 0),
    render: (p) => `<input type="checkbox" class="include-toggle"
      data-slug="${escape(p.slug)}" ${p.include_in_index !== false ? "checked" : ""}
      aria-label="incluir ${escape(p.slug)} en el bulk index de cbm"
      title="incluir este repo en el bulk index de cbm">` },
  { key: "night_mode_enabled", label: "Nocturno", className: "text-center",
    value: (p) => (p.night_mode_enabled ? 1 : 0),
    render: (p) => `<input type="checkbox" class="night-toggle"
      data-slug="${escape(p.slug)}" ${p.night_mode_enabled ? "checked" : ""}
      aria-label="modo nocturno en ${escape(p.slug)}"
      title="habilitar modo nocturno (ADR-028)">` },
  { key: "indexed", label: "Indexado", className: "text-center",
    value: (p) => (p.indexed ? 1 : 0),
    render: (p) => boolCell(p.indexed, "indexado en cbm", "sin indexar") },
  { key: "has_git", label: "Git", className: "text-center",
    value: (p) => (p.has_git ? 1 : 0),
    render: (p) => boolCell(p.has_git, "es un repo git", "sin repo git") },
  { key: "discord_channel_id", label: "Canal",
    value: (p) => (p.discord_channel_id ? "vinculado" : "sin canal"),
    render: _celdaCanal },
  { key: "github_project", label: "Tablero", className: "tabular-nums",
    value: (p) => p.github_project?.number ?? null,
    render: _celdaTablero },
  { key: "slug", label: "", sortable: false, className: "cell-actions",
    // El slug entra igual en la búsqueda por esta columna aunque no se
    // muestre: uno filtra por slug más seguido que por nombre.
    render: (p) => actionGroup([
      iconBtn(ICONS.vscode, `abrir ${p.slug} en VS Code`, "vscode-btn", p.slug),
      ...(gitWebUrl(p.git_remote_url) ? [
        `<a class="icon-btn" href="${escape(gitWebUrl(p.git_remote_url))}"
           target="_blank" rel="noopener"
           title="abrir el repo remoto en el browser"
           aria-label="abrir el repo remoto de ${escape(p.slug)}">${_svg(ICONS.remoto)}</a>`,
      ] : []),
      iconBtn(ICONS.tablero, `seguimiento GitHub de ${p.slug}`, "github-btn", p.slug),
      iconBtn(ICONS.gear, `administrar ${p.slug}`, "admin-btn", p.slug),
      iconBtn(ICONS.correr, `correr experto sobre ${p.slug}`, "expert-btn", p.slug),
    ]) },
];

export async function loadProjects() {
  if (!_tabla) {
    _tabla = dataTable($("#projects-table"), {
      columns: COLUMNAS,
      rows: [],
      pageSize: 25,
      sort: { key: 0, dir: "asc" },
      searchPlaceholder: "filtrar proyectos…",
      rowAttrs: (p) => `data-slug="${escape(p.slug)}"`,
      // Función y no valor: "no se pudo cargar" no es lo mismo que "no
      // hay proyectos", y antes los dos casos se veían igual —el catch
      // hacía console.error y la tabla se quedaba en skeleton para
      // siempre, sin decir nada.
      empty: () => (_loadError
        ? { title: "No se pudieron cargar los proyectos",
            sub: _loadError,
            action: `<button class="btn" data-projects-retry>Reintentar</button>` }
        : { title: "Todavía no hay proyectos",
            sub: "Un proyecto es un experto sobre un repo: system_prompt + "
               + "mcp_servers. Creá el primero y va a poder invocarse vía "
               + "POST /experts/run.",
            action: `<button class="btn btn-primary" data-projects-new>Nuevo proyecto</button>` }),
    });
    _wireTabla();
  }
  _loadError = "";
  _tabla.setLoading(true);
  try {
    const { projects, discord_guild_id: guildId } = await api("projects");
    _guildId = guildId || "";
    _tabla.setRows(projects || []);
  } catch (e) {
    _dbg("loadProjects ERROR", e.message);
    _loadError = e.message;
    _tabla.setRows([]);
  }
}

// ---- wiring de los botones de la fila ----

// ponytail: reload-btn / disable-btn / delete-btn (handlers huérfanos, nunca
// renderizados en filas) — el admin modal ya tiene esos controles en sus
// tabs internos. Borrados para no acumular código muerto.
// Una sola vez, sobre el contenedor. Antes se re-wireaba fila por fila
// después de cada render; con dataTable el tbody se repinta al ordenar,
// filtrar o paginar, así que ese patrón dejaría los botones muertos a
// partir de la segunda pintada.
const _ACCIONES = {
  "admin-btn": (slug) => openAdminModal(slug),
  "expert-btn": (slug) => openExpertModal(slug),
  "github-btn": (slug) => openGithubModal(slug),
  "vscode-btn": async (slug) => {
    const r = await openProjectInVSCode(slug);
    if (!r.ok) toast("No se pudo abrir VS Code: " + r.error, "err");
  },
};

function _wireTabla() {
  const el = $("#projects-table");
  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-projects-new]")) { $("#project-new").click(); return; }
    if (e.target.closest("[data-projects-retry]")) { loadProjects(); return; }
    const btn = e.target.closest("button[data-slug]");
    if (!btn) return;
    for (const [cls, fn] of Object.entries(_ACCIONES)) {
      if (btn.classList.contains(cls)) { fn(btn.dataset.slug); return; }
    }
  });
  el.addEventListener("change", (e) => {
    const c = e.target;
    if (c.classList.contains("include-toggle")) togglePatch(e, "include_in_index");
    else if (c.classList.contains("night-toggle")) toggleNight(e);
  });
}

// night_mode_enabled es global del proyecto: prender cambia el
// comportamiento del relay para ese slug (aparece en night-runs, el
// polling lo trae a esta tab, etc). Un click accidental es caro.
// Apagar, en cambio, es recuperación — no necesita prompt.
async function toggleNight(e) {
  const slug = e.target.dataset.slug;
  const value = e.target.checked;
  if (value) {
    const ok = await confirmModal({
      title: `Prender modo nocturno en "${slug}"`,
      body: `Este proyecto va a aparecer en el tab Night Runs y el ` +
            `compactador va a poder usarlo como origen. ` +
            `¿Prenderlo?`,
      confirmText: "Sí, prender",
      cancelText: "Cancelar",
    });
    if (!ok) {
      e.target.checked = false;
      return;
    }
  }
  await togglePatch(e, "night_mode_enabled");
}

async function togglePatch(e, field) {
  const slug = e.target.dataset.slug;
  const value = e.target.checked;
  e.target.disabled = true;
  try {
    await api(`projects/${slug}`, {
      method: "PATCH",
      body: JSON.stringify({ [field]: value }),
    });
  } catch (err) {
    toast("No se pudo guardar: " + err.message, "err");
    e.target.checked = !value;
  } finally {
    e.target.disabled = false;
  }
}

// ============================================================
// Modales
// ============================================================

// ---- system prompt viewer ----

function openPromptModal(slug) {
  const modal = $("#prompt-modal");
  modal.dataset.assembled = "";
  modalLoad({
    id: "prompt-modal",
    title: `System prompt · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}/system-prompt`),
    render: renderPromptBlocks,
    after: (r) => {
      modal.dataset.assembled = r.assembled || "";
      $$(".prompt-block-header").forEach((h) =>
        h.addEventListener("click", () => {
          h.parentElement.classList.toggle("collapsed");
          const tog = h.querySelector(".prompt-block-toggle");
          if (tog) tog.textContent =
            h.parentElement.classList.contains("collapsed") ? "▸" : "▾";
        }));
    },
  });
}

function renderPromptBlocks(r) {
  const labels = {
    ponytail: "1 · Ponytail (filosofía del usuario)",
    system_prompt: "2 · projects.system_prompt (específico del repo)",
    skills: "3 · Índice de skills (ADR-010)",
    workspace: "4 · Workspace block (ADR-011)",
  };
  // El git diff salió del system prompt el 28/7 (rompía la cache del
  // provider en cada resume); ahora es la tool nativa `git_diff()`.
  const order = ["ponytail", "system_prompt", "skills", "workspace"];
  const blocks = r.blocks || {};
  const stats = r.stats || {};
  const totalKb = ((stats.total || 0) / 1024).toFixed(1);
  const parts = [
    `<p class="muted mb-3">Total ensamblado: <strong class="text-zinc-200">${totalKb} KB</strong>
      (${stats.total || 0} chars). Estos son los bloques que el relay
      mete en <code>instructions=</code> del agente, en orden.
      Click en cualquier bloque para colapsarlo.</p>`,
  ];
  for (const k of order) {
    const text = blocks[k] || "";
    const sizeKb = ((stats[k] || 0) / 1024).toFixed(1);
    const open = k === "system_prompt";
    parts.push(`<div class="prompt-block${open ? "" : " collapsed"}">
      <div class="prompt-block-header">
        <span><span class="prompt-block-toggle">${open ? "▾" : "▸"}</span>
          <span class="prompt-block-title">${escape(labels[k])}</span></span>
        <span class="prompt-block-meta">${stats[k] || 0} chars · ${sizeKb} KB</span>
      </div>
      ${text
        ? `<div class="prompt-block-body">${escape(text)}</div>`
        : `<div class="prompt-block-empty">(vacío)</div>`}
    </div>`);
  }
  return parts.join("");
}

async function copyPromptAssembled() {
  const modal = $("#prompt-modal");
  const msg = $("#prompt-modal-msg");
  const text = modal.dataset.assembled || "";
  if (!text) {
    msg.textContent = "(nada para copiar)";
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    msg.textContent = "copiado ✓";
    setTimeout(() => { msg.textContent = ""; }, 2000);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      msg.textContent = "copiado ✓";
    } catch (_) {
      msg.textContent = "error: " + e.message;
    } finally {
      document.body.removeChild(ta);
      setTimeout(() => { msg.textContent = ""; }, 2000);
    }
  }
}

// ---- git diff preview ----

function openDiffModal(slug) {
  modalLoad({
    id: "diff-modal",
    title: `Git diff · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}/git-diff`,
      null, 30_000),
    render: renderDiffDetail,
    watchdogDetail: `El subprocess de git puede tardar en repos grandes.
      Timeout duro a los 30s; puedes cerrar con ✕ o Esc mientras tanto.`,
  });
}

function renderDiffDetail(r) {
  if (!r.ok) {
    const reason = r.status === "not_git_repo"
      ? "este proyecto no es un repo git (no tiene <code>.git</code>)"
      : `git no disponible (status: ${escape(r.status || "?")})`;
    return `<p class="muted">No se puede leer el diff: ${reason}.
      El LLM tampoco lo ve — el bloque ADR-020 se salta automáticamente.</p>`;
  }
  const statusLines = (r.status || "").split("\n").filter((l) => l.trim());
  const diffText = r.diff || "";
  const fullSize = r.full_size ?? diffText.length;
  const truncated = !!r.truncated;
  const VISUAL_MAX = 32_000;
  let shown = diffText;
  let visualTruncated = false;
  if (shown.length > VISUAL_MAX) {
    shown = shown.slice(0, VISUAL_MAX);
    const lastNl = shown.lastIndexOf("\n");
    if (lastNl > 0) shown = shown.slice(0, lastNl);
    visualTruncated = true;
  }
  let html = `<table class="status-detail"><tbody>
    <tr><th>branch</th><td><code>${escape(r.branch || "?")}</code></td></tr>
    <tr><th>HEAD</th><td><code>${escape(r.sha || "?")}</code></td></tr>
    <tr><th>status</th><td>${statusLines.length
      ? `${statusLines.length} archivos modificados`
      : '<span class="muted">working tree clean</span>'}</td></tr>
    <tr><th>diff size</th><td>${fullSize.toLocaleString()} chars total
      ${truncated ? ' <span class="badge warn">truncado</span>' : ''}
      ${visualTruncated ? ' <span class="badge warn">cap visual 32KB</span>' : ''}
    </td></tr>
  </tbody></table>`;
  if (statusLines.length) {
    html += `<h4>Status</h4>
      <pre class="chat-md">${escape(statusLines.join("\n"))}</pre>`;
  }
  if (shown) {
    html += `<h4>Diff</h4>
      <pre class="chat-md">${escape(shown)}</pre>`;
  } else if (!statusLines.length) {
    html += `<p class="muted mt-3">
      Working tree clean. Sin cambios pendientes.</p>`;
  }
  if (truncated || visualTruncated) {
    const sha = r.sha || "HEAD";
    html += `<p class="muted mt-2">
      Diff grande (${fullSize.toLocaleString()} chars total). El response
      del endpoint capeó a 64KB y la vista a 32KB para no colgar el
      browser. Para el resto, abre una terminal y corre
      <code>git show ${escape(sha)}</code> o
      <code>git diff ${escape(sha)}~1..${escape(sha)}</code>.</p>`;
  }
  return html;
}

// ---- remoto GitHub (vincular / cambiar el origin del repo local) ----

function renderGitRemote(project) {
  const url = project.git_remote_url || "";
  if (!project.has_git) {
    return `<div class="warn-box mb-4"><p class="text-sm">Este proyecto no es
      un repo git (no tiene <code>.git</code>), así que no se puede
      configurar un remoto. Inicializá git primero (p. ej.
      <code>git init</code> en una terminal del repo).</p></div>`;
  }
  return `<div class="mb-4">
    <h4>Remoto GitHub (<code>origin</code>)</h4>
    <p class="muted text-xs mb-2">${url
      ? `Actual: <code>${escape(url)}</code>.`
      : "Sin remoto <code>origin</code>."} Guardar corre
      <code>git remote ${url ? "set-url" : "add"} origin</code> en el repo
      local; el seguimiento de Issues/PRs deriva de acá.</p>
    <div class="flex gap-2 items-center flex-wrap">
      <input id="git-remote-input" type="text" class="input w-full flex-1"
        style="min-width:22rem"
        placeholder="https://github.com/owner/repo.git o git@github.com:owner/repo.git"
        value="${escape(url)}">
      <button id="git-remote-save" class="btn btn-xs">${
        url ? "Cambiar" : "Vincular"}</button>
    </div>
    <p id="git-remote-msg" class="text-xs mt-2"></p>
  </div>`;
}

function wireGitRemote(slug) {
  const btn = document.getElementById("git-remote-save");
  if (!btn) return;
  const input = document.getElementById("git-remote-input");
  const msg = document.getElementById("git-remote-msg");
  const save = async () => {
    const url = (input.value || "").trim();
    if (!url) { msg.textContent = "Escribí una URL."; msg.className = "text-xs mt-2 fail"; return; }
    btn.disabled = true;
    msg.textContent = "Guardando…"; msg.className = "text-xs mt-2 muted";
    try {
      const r = await api(`projects/${encodeURIComponent(slug)}/git-remote`, {
        method: "PUT",
        body: JSON.stringify({ url }),
      });
      msg.textContent = `✓ Remoto configurado: ${r.repo}`;
      msg.className = "text-xs mt-2 ok";
      // Reflejar en el ctx vivo y en la tabla de fondo (link 🐙).
      if (_adminCtx?.data?.project) _adminCtx.data.project.git_remote_url = r.url;
      loadProjects();
      toast("Remoto de GitHub actualizado ✓", "ok");
    } catch (err) {
      msg.textContent = "Error: " + err.message;
      msg.className = "text-xs mt-2 fail";
    } finally {
      btn.disabled = false;
    }
  };
  btn.addEventListener("click", save);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
  });
}

// ---- modo nocturno ----

function openNightModal(slug) {
  modalLoad({
    id: "night-modal",
    title: `Modo nocturno · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}/night`),
    render: renderNight,
    after: (r) => wireNight(slug, r),
    watchdogDetail: `Leyendo la config y las últimas corridas (tabla
      night_runs). Si tarda, el relay está ocupado; cierra con ✕ o Esc.`,
  });
}

// Estado VIVO del run activo (viene del snapshot del orquestador en
// memoria, no de la fila de la DB). Muestra en qué anda la tarea actual:
// el paso de Fase 2 y, si el experto trabaja, su phase/tool/idle_s.
// idle_s es el detector de cuelgue: si crece sin parar, el experto no
// produce nodos; si se resetea, avanza. Con esto no hace falta adivinar
// el timeout — se ve si sigue vivo.
function renderNightLive(active) {
  const task = active.current_task;
  const counts = `<span class="tabular-nums">done ${active.tasks_done ?? 0}</span> ·
    <span class="tabular-nums">pend ${active.tasks_pending ?? 0}</span> ·
    <span class="tabular-nums">desc ${active.tasks_discarded ?? 0}</span> ·
    <span class="tabular-nums">PRs ${active.prs_opened ?? 0}</span>`;
  if (!task) {
    return `<p class="text-xs muted mb-2">Sin tarea en curso (planificando o
      entre tareas). ${counts}</p>`;
  }
  const live = active.live || {};
  const exp = live.expert;
  let expLine = "";
  if (exp) {
    // idle alto = el experto espera al modelo (turno lento) o está trabado.
    const idle = exp.idle_s ?? 0;
    const idleCls = idle > 120 ? "fail" : (idle > 45 ? "warn" : "muted");
    expLine = `<div class="text-xs mt-1">
      experto: <strong>${escape(exp.phase || "?")}</strong>
      ${exp.last_tool ? `· 🔧 <code>${escape(exp.last_tool)}</code>` : ""}
      · <span class="tabular-nums">${exp.tool_calls ?? 0} calls</span>
      · <span class="tabular-nums">${Math.round(exp.elapsed_s ?? 0)}s</span>
      · idle <span class="${idleCls} tabular-nums">${Math.round(idle)}s</span>
    </div>`;
  }
  const step = live.step
    ? `<span class="badge">${escape(live.step)}</span>
       <span class="tabular-nums muted">${Math.round(live.step_elapsed_s ?? 0)}s</span>`
    : "";
  return `<div class="status-detail mb-2" style="padding:.5rem">
    <div class="text-xs"><strong>Tarea actual:</strong> ${escape(task)}</div>
    <div class="text-xs mt-1">paso: ${step}</div>
    ${expLine}
    <div class="text-xs mt-1 muted">${counts}</div>
  </div>`;
}

function renderNight(r) {
  const cfg = r.config || {};
  const active = r.active_run;
  const runs = r.runs || [];

  if (!r.night_mode_enabled) {
    return `<p class="muted">El modo nocturno está <strong>apagado</strong>
      para este proyecto. Activa el toggle 🌙 en la fila antes de arrancar.</p>`;
  }
  let warn = "";
  if (!cfg.test_cmd) {
    warn = `<p class="warn-box">⚠ Sin <code>test_cmd</code>
      configurado ni auto-detectable: el worker rollbackea toda tarea (TDD
      estricto). Configura <code>night_config.test_cmd</code> primero.</p>`;
  }

  const cfgRows = `<table class="status-detail"><tbody>
    <tr><th>build_cmd</th><td>${cfg.build_cmd ? `<code>${escape(cfg.build_cmd)}</code>` : '<span class="muted">—</span>'}</td></tr>
    <tr><th>test_cmd</th><td>${cfg.test_cmd ? `<code>${escape(cfg.test_cmd)}</code>` : '<span class="fail">—</span>'}</td></tr>
    <tr><th>max_diff_lines</th><td class="tabular-nums">${cfg.max_diff_lines}</td></tr>
    <tr><th>base_branch</th><td><code>${escape(cfg.base_branch || "main")}</code></td></tr>
  </tbody></table>`;

  let ctrl;
  if (active) {
    ctrl = `<div class="warn-box">
      <p class="mb-2">🌙 Corrida activa
        <code>${escape(active.id)}</code> — deadline ${escape(active.deadline_at || "?")}.</p>
      ${renderNightLive(active)}
      <button id="night-stop" class="btn btn-xs danger" data-run="${escape(active.id)}"
        title="detener el loop después de la tarea actual">⏹ Detener</button>
      <button id="night-refresh" class="btn btn-xs" title="refrescar este modal">↻ Refrescar</button>
    </div>`;
  } else {
    // FIX: directiva obligatoria. El botón arranca disabled hasta que
    // el textarea tenga algo (esto evita el bug del iter 5 v1: runs
    // arrancados sin directiva → LLM devuelve 0 drafts → no_tasks).
    ctrl = `<div class="mb-4">
      <label class="label mb-1" for="night-directive">
        Directiva (Fase 1) <span class="fail">*</span>
      </label>
      <textarea id="night-directive" class="input w-full" required
        placeholder="ej: extrae el timeout hardcodeado de OrderService a appsettings.json y cubrilo con un test de integración"></textarea>
      <p class="muted text-xs mt-1">
        Obligatoria. La Fase 1 la usa como semilla para el plan; sin
        directiva el LLM devuelve 0 tareas y el run termina al toque
        con <code>no_tasks</code>.
      </p>
      <div class="toolbar mt-2">
        <button id="night-start" class="btn btn-primary btn-xs" disabled
          title="arranca el night run con la directiva de arriba">🌙 Arrancar corrida</button>
        <span class="muted">deadline por default: próximas 7am</span>
      </div>
    </div>`;
  }

  // Runs históricos: columna nueva "📄 reporte" si report_path existe.
  const runsTable = runs.length
    ? `<table class="status-detail"><thead><tr>
        <th class="th">run</th><th class="th">fin</th><th class="th">motivo</th>
        <th class="th">PRs</th><th class="th">done</th><th class="th">desc</th>
        <th class="th">📄</th>
      </tr></thead><tbody>${runs.map((x) => {
        const hasReport = !!x.report_path;
        return `<tr>
          <td><code>${escape(x.id)}</code></td>
          <td>${x.ended_at ? escape(x.ended_at) : '<span class="badge warn">activa</span>'}</td>
          <td>${escape(x.end_reason || "—")}</td>
          <td class="tabular-nums">${x.prs_opened ?? 0}</td>
          <td class="tabular-nums">${x.tasks_done ?? 0}</td>
          <td class="tabular-nums">${x.tasks_discarded ?? 0}</td>
          <td>${hasReport
            ? `<button class="btn btn-xs report-btn" data-run="${escape(x.id)}"
                title="abrir el reporte .md en otro modal">📄</button>`
            : '<span class="muted">—</span>'}</td>
        </tr>`;
      }).join("")}</tbody></table>`
    : `<p class="muted">Sin corridas todavía.</p>`;

  return `${warn}<h4>Config efectiva</h4>${cfgRows}
    <h4 class="mt-4">Control</h4>${ctrl}
    <h4>Últimas corridas</h4>${runsTable}`;
}

function wireNight(slug, r) {
  const msg = $("#night-modal-msg");
  const start = $("#night-start");
  const ta = $("#night-directive");

  // FIX: habilitar Arrancar solo si hay directiva no vacía.
  if (start && ta) {
    const refresh = () => { start.disabled = !ta.value.trim(); };
    refresh();
    ta.addEventListener("input", refresh);
    onClick("#night-start", async () => {
      const directive = ta.value.trim();
      if (!directive) {
        toast("La directiva es obligatoria.", "err");
        return;
      }
      start.disabled = true;
      msg.textContent = "arrancando…";
      try {
        const res = await apiRoot("/night-mode/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project: slug, directive }),
        });
        toast(`Night run ${res.run_id} arrancado (deadline ${res.deadline_at}).`, "ok");
        openNightModal(slug);
      } catch (err) {
        msg.textContent = "";
        toast("No se pudo arrancar: " + err.message, "err");
        start.disabled = false;
      }
    });
  }
  const stop = $("#night-stop");
  onClick("#night-stop", async () => {
    stop.disabled = true;
    try {
      await apiRoot("/night-mode/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: stop.dataset.run }),
      });
      toast("Detención pedida (para tras la tarea actual).", "ok");
      openNightModal(slug);
    } catch (err) {
      toast("No se pudo detener: " + err.message, "err");
      stop.disabled = false;
    }
  });
  onClick("#night-refresh", () => openNightModal(slug));

  // 📄 reporte — abre el run report modal
  $$(".report-btn").forEach((b) =>
    b.addEventListener("click", () => openRunReportModal(b.dataset.run)));
}

// ---- run report viewer ----

function openRunReportModal(runId) {
  modalLoad({
    id: "report-modal",
    title: `Reporte · ${runId}`,
    loader: () => api(`night-runs/${encodeURIComponent(runId)}/report`),
    render: renderRunReport,
    watchdogDetail: `Leyendo el .md del reporte (state/morning-reports/...).
      Cap a 256 KB; el archivo completo está en el path que muestra abajo.`,
  });
}

function renderRunReport(r) {
  if (r.error) {
    return `<p class="fail">${escape(r.error)}</p>`;
  }
  const sizeKb = ((r.size_bytes || 0) / 1024).toFixed(1);
  return `<table class="status-detail"><tbody>
    <tr><th>proyecto</th><td><code>${escape(r.project_slug || "?")}</code></td></tr>
    <tr><th>motivo</th><td>${escape(r.end_reason || "?")}</td></tr>
    <tr><th>inicio</th><td>${escape(r.started_at || "?")}</td></tr>
    <tr><th>fin</th><td>${escape(r.ended_at || "?")}</td></tr>
    <tr><th>archivo</th><td><code>${escape(r.report_path || "?")}</code>
      · ${sizeKb} KB</td></tr>
  </tbody></table>
  <h4 class="mt-4">Contenido</h4>
  <pre class="chat-md">${escape(r.report_md || "")}</pre>`;
}

// ---- cbm stats ----

function openCbmModal(slug) {
  modalLoad({
    id: "cbm-modal",
    title: `cbm · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}/cbm`),
    render: renderCbmDetail,
    after: () => wireCbmReindex(slug),
  });
}

function renderCbmDetail(r) {
  if (!r.installed) {
    return `<p class="warn-box">⚠ <code>codebase-memory-mcp</code> no
      instalado. El wrapper <code>cbm_query</code> de los expertos va a
      degradarse a grep+regex (ver ADR-017).</p>`;
  }
  const s = r.stats || {};
  if (!s.nodes && !s.edges) {
    return `<table class="status-detail"><tbody>
      <tr><th>proyecto cbm</th><td><code>${escape(r.cbm_project_name || "?")}</code></td></tr>
      <tr><th>estado</th><td><span class="badge warn">no indexado</span></td></tr>
    </tbody></table>
    <p class="muted mt-3">Toca <strong>Reindex</strong> abajo para
      mapear el repo al grafo cbm (puede tardar minutos en repos
      grandes).</p>`;
  }
  const sizeMb = ((s.size_bytes || 0) / 1024 / 1024).toFixed(2);
  return `<table class="status-detail"><tbody>
    <tr><th>proyecto cbm</th><td><code>${escape(r.cbm_project_name || "?")}</code></td></tr>
    <tr><th>estado</th><td><span class="badge ok">indexado</span></td></tr>
    <tr><th>nodes</th><td class="tabular-nums">${(s.nodes ?? 0).toLocaleString()}</td></tr>
    <tr><th>edges</th><td class="tabular-nums">${(s.edges ?? 0).toLocaleString()}</td></tr>
    <tr><th>cache size</th><td class="tabular-nums">${sizeMb} MB</td></tr>
    <tr><th>repo_path</th><td><code>${escape(r.repo_path || "?")}</code></td></tr>
  </tbody></table>
  ${r.note ? `<p class="muted mt-2">${escape(r.note)}</p>` : ""}`;
}

function wireCbmReindex(slug) {
  const btn = $("#cbm-reindex");
  onClick("#cbm-reindex", async () => {
    btn.disabled = true;
    const msg = $("#cbm-modal-msg");
    msg.textContent = "arrancando reindex…";
    try {
      const r = await api(`projects/${encodeURIComponent(slug)}/reindex`,
        { method: "POST" });
      toast(`Reindex ${r.job_id} arrancado. Espera unos minutos…`, "ok");
      msg.textContent = `job_id=${r.job_id} — verifica el tab Estado para el progreso`;
    } catch (err) {
      msg.textContent = "";
      toast("Error: " + err.message, "err");
      btn.disabled = false;
    }
  });
}

// ---- seguimiento GitHub (fase 2 del plan) ----
// Read-only: issues y PRs abiertos del repo + las columnas del tablero
// Projects v2 si el proyecto tiene uno mapeado. La verdad vive en
// GitHub; acá no se guarda ni se duplica nada.

// `onChange` lo pasa el tab Gestión: cuando desde acá se vincula, se crea
// o se desvincula un tablero, su lista de pendientes se refresca sola.
export function openGithubModal(slug, onChange) {
  modalLoad({
    id: "github-modal",
    title: `Seguimiento · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}/github`),
    render: renderGithub,
    after: () => wireGithubRefresh(slug, onChange),
    // Cada bloque es un spawn de `gh`; en frío (sin caché) son ~3s.
    watchdogMs: 12000,
    watchdogDetail: `Consultando GitHub con <code>gh</code>. La primera
      lectura de un repo tarda unos segundos; las siguientes salen del
      caché de 60s.`,
  });
}

function ghItem(icon, num, title, url, meta) {
  const label = num != null ? `#${num}` : "";
  return `<li class="flex items-baseline gap-2 py-1">
    <span>${icon}</span>
    <a href="${escape(url || "#")}" target="_blank" rel="noopener"
       class="text-sky-300 hover:underline">${escape(label)}</a>
    <span class="flex-1 truncate">${escape(title || "")}</span>
    ${meta ? `<span class="muted text-xs shrink-0">${escape(meta)}</span>` : ""}
  </li>`;
}

function renderGithub(r) {
  if (!r.configured) {
    return `<p class="warn-box">⚠ Sin datos de GitHub para este proyecto.</p>
      <p class="muted mt-2">Puede ser que el repo no tenga remote de GitHub,
      que <code>gh</code> no esté instalado, o que no esté autenticado en el
      entorno donde corre el relay (<code>gh auth status</code>).</p>`;
  }
  const issues = r.issues || [], pulls = r.pulls || [];
  const issuesHtml = issues.length
    ? `<ul>${issues.map((i) => ghItem("🟢", i.number, i.title, i.url,
        (i.assignees || []).map((a) => a.login || a).join(", "))).join("")}</ul>`
    : `<p class="muted">Sin issues abiertos.</p>`;
  const pullsHtml = pulls.length
    ? `<ul>${pulls.map((p) => ghItem(p.isDraft ? "📝" : "🔀", p.number, p.title,
        p.url, p.headRefName || "")).join("")}</ul>`
    : `<p class="muted">Sin PRs abiertos.</p>`;

  // Tablero. Sin mapear: desplegable de tableros de la org + crear uno.
  // Mapeado: las columnas + link para abrir y mover en GitHub.
  let boardHtml;
  if (!r.mapping) {
    const owner = (r.repo || "").split("/")[0] || "";
    boardHtml = `<p class="muted">Este proyecto todavía no tiene tablero.</p>
      <div class="toolbar mt-2">
        <label class="label">org:
          <input id="gh-owner" type="text" class="input w-[160px]"
                 value="${escape(owner)}">
        </label>
        <button id="gh-load-boards" class="btn">🔍 Buscar</button>
        <select id="gh-board-sel" class="select w-[300px]">
          <option value="">(elegí un tablero)</option>
        </select>
        <button id="gh-link" class="btn">🔗 Vincular</button>
      </div>
      <div class="toolbar mt-2">
        <label class="label">o crear uno nuevo:
          <input id="gh-new-title" type="text" class="input w-[260px]"
                 placeholder="título del tablero">
        </label>
        <button id="gh-create" class="btn btn-primary">➕ Crear tablero</button>
      </div>
      <p id="gh-board-msg" class="muted mt-2"></p>`;
  } else {
    const m = r.mapping;
    const head = `<p class="mb-2">
      <a href="${escape(m.url)}" target="_blank" rel="noopener"
         class="text-sky-300 hover:underline">
        ↗ Abrir tablero #${m.number}${m.title ? ` — ${escape(m.title)}` : ""}
      </a>
      <span class="muted"> · arrastrá las tarjetas allá; acá es solo lectura</span>
      <button id="gh-unlink" class="btn btn-xs ml-2">desvincular</button>
    </p>`;
    boardHtml = head + (!r.board
      ? `<p class="warn-box">No pude leer el tablero (¿existe el
         #${m.number} en <code>${escape(m.owner)}</code>?).</p>`
      // Mismo kanban que el tab Gestión: una sola implementación.
      : renderColumnas(r.board.columns));
  }

  const repoLink = r.repo_url
    ? ` · <a href="${escape(r.repo_url)}" target="_blank" rel="noopener"
         class="text-sky-300 hover:underline">↗ abrir repo</a>
        · <a href="${escape(r.repo_url)}/issues/new" target="_blank"
             rel="noopener" class="text-sky-300 hover:underline">+ nuevo issue</a>`
    : "";
  return `<p class="muted mb-3">Repo: <code>${escape(r.repo)}</code>${repoLink}</p>
    <h4 class="mb-1 font-semibold">Issues abiertos (${issues.length})</h4>
    ${issuesHtml}
    <h4 class="mb-1 mt-4 font-semibold">PRs abiertos (${pulls.length})</h4>
    ${pullsHtml}
    <h4 class="mb-1 mt-4 font-semibold">Tablero</h4>
    ${boardHtml}`;
}

function wireGithubRefresh(slug, onChange) {
  onClick("#github-refresh", () => openGithubModal(slug, onChange));

  const msg = (t) => { const el = $("#gh-board-msg"); if (el) el.textContent = t; };
  const put = async (payload) => {
    await api(`projects/${encodeURIComponent(slug)}/github-project`,
      { method: "PUT", body: JSON.stringify(payload) });
    openGithubModal(slug, onChange);
    if (onChange) onChange();
  };

  const load = $("#gh-load-boards");
  onClick("#gh-load-boards", async () => {
    const owner = $("#gh-owner").value.trim();
    if (!owner) { msg("poné la organización"); return; }
    load.disabled = true;
    msg("consultando GitHub…");
    try {
      const r = await api(`github/boards?owner=${encodeURIComponent(owner)}`);
      $("#gh-board-sel").innerHTML = `<option value="">(elegí un tablero)</option>`
        + (r.boards || []).map((b) =>
            `<option value="${b.number}" data-url="${escape(b.url || "")}"
                     data-title="${escape(b.title || "")}">#${b.number} — ${escape(b.title || "")}</option>`).join("");
      msg(r.error ? `⚠ ${r.error}` : `${(r.boards || []).length} tableros`);
    } catch (e) { msg("error: " + e.message); }
    finally { load.disabled = false; }
  });

  onClick("#gh-link", async () => {
    const sel = $("#gh-board-sel");
    const opt = sel.selectedOptions[0];
    if (!sel.value) { msg("elegí un tablero primero"); return; }
    try {
      await put({ owner: $("#gh-owner").value.trim(), number: Number(sel.value),
                  title: opt?.dataset.title || "", url: opt?.dataset.url || "" });
      toast("Tablero vinculado", "ok");
    } catch (e) { msg("error: " + e.message); }
  });

  const create = $("#gh-create");
  onClick("#gh-create", async () => {
    const owner = $("#gh-owner").value.trim();
    if (!owner) { msg("poné la organización"); return; }
    create.disabled = true;
    msg("creando el tablero en GitHub…");
    try {
      const r = await api(`projects/${encodeURIComponent(slug)}/github-project`, {
        method: "POST",
        body: JSON.stringify({ owner, title: $("#gh-new-title").value.trim() }),
      });
      toast(`Tablero #${r.created.number} creado`
        + (r.linked_to_repo
            ? " y linkeado al repo"
            : `\n(sin linkear al repo: ${r.link_error || "motivo desconocido"})`),
        r.linked_to_repo ? "ok" : "warn", 8000);
      openGithubModal(slug, onChange);
      if (onChange) onChange();
    } catch (e) { msg("error: " + e.message); create.disabled = false; }
  });

  onClick("#gh-unlink", async () => {
    try {
      await put({ number: null });
      toast("Tablero desvinculado (no se borró nada en GitHub)", "ok");
    } catch (e) { toast("error: " + e.message, "err"); }
  });
}

// ---- nuevo proyecto (alta directa, POST /admin/api/projects) ----

function openNewModal() {
  modalLoad({
    id: "new-modal",
    title: "Nuevo proyecto",
    loader: async () => ({}),
    render: renderNewForm,
    after: wireNewForm,
  });
}

function renderNewForm() {
  return `<form id="new-form" class="flex flex-col gap-3">
    <p class="muted text-sm mt-0">Alta directa de un experto: registra un
      repo existente o parte uno de cero (carpeta nueva + README seed +
      git init). Si el repo ya está indexado en cbm sin experto, es más
      corto desde el tab <strong>Huérfanos</strong>.</p>
    <div>
      <label class="label mb-1" for="new-repo">Repo path (absoluto)
        <span class="fail">*</span></label>
      <input id="new-repo" class="input w-full"
        placeholder="C:\\Users\\developer\\source\\repos\\mi-proyecto">
    </div>
    <div class="grid grid-cols-2 gap-2">
      <div>
        <label class="label mb-1" for="new-slug">Slug</label>
        <input id="new-slug" class="input w-full" placeholder="(auto del basename)">
      </div>
      <div>
        <label class="label mb-1" for="new-name">Nombre</label>
        <input id="new-name" class="input w-full" placeholder="(auto del basename)">
      </div>
    </div>
    <div>
      <label class="label mb-1" for="new-desc">Descripción</label>
      <input id="new-desc" class="input w-full"
        placeholder="una línea: qué es este proyecto">
    </div>
    <div class="flex flex-col gap-1 text-sm">
      <label class="flex items-center gap-2">
        <input type="checkbox" id="new-create-dir">
        crear la carpeta si no existe (escribe un README.md seed si queda vacía)
      </label>
      <label class="flex items-center gap-2">
        <input type="checkbox" id="new-git-init" checked>
        <code>git init</code> si falta <code>.git</code> (lo usan el diff
        block ADR-020 y el modo nocturno)
      </label>
      <label class="flex items-center gap-2">
        <input type="checkbox" id="new-index" checked>
        indexar en cbm ahora (knowledge graph para <code>cbm_query</code>)
      </label>
    </div>
    <p class="muted text-xs">Después del alta: el system_prompt sale de un
      template mínimo — refinálo con ✏️. Los MCPs extra se asocian en el
      tab MCPs; las skills (ADR-010) son globales y aplican solas. Guía
      completa: <code>docs/NUEVO_PROYECTO.md</code>.</p>
  </form>`;
}

function wireNewForm() {
  const save = $("#new-save");
  const msg = $("#new-modal-msg");
  const repo = $("#new-repo");
  const slug = $("#new-slug");
  if (!save || !repo) return;

  const refresh = () => { save.disabled = !repo.value.trim(); };
  refresh();
  repo.addEventListener("input", () => {
    refresh();
    // Sugerir slug desde el basename (solo placeholder, no pisa lo tipeado).
    const base = repo.value.trim().split(/[\\/]/).filter(Boolean).pop() || "";
    slug.placeholder = base
      ? base.toLowerCase().replace(/[^a-z0-9-]+/g, "-")
          .replace(/-+/g, "-").replace(/^-|-$/g, "")
      : "(auto del basename)";
  });

  onClick("#new-save", async () => {
    const repoPath = repo.value.trim();
    if (!repoPath) { toast("El repo path es obligatorio.", "err"); return; }
    save.disabled = true;
    msg.textContent = "creando…";
    try {
      const r = await api("projects", {
        method: "POST",
        body: JSON.stringify({
          repo_path: repoPath,
          slug: slug.value.trim(),
          name: $("#new-name").value.trim(),
          description: $("#new-desc").value.trim(),
          create_dir: $("#new-create-dir").checked,
          git_init: $("#new-git-init").checked,
          index_now: $("#new-index").checked,
        }),
      }, 30_000);
      const extra = (r.notes || []).join(" · ");
      toast(`Proyecto ${r.project.slug} creado ✓`
        + (extra ? `\n${extra}` : ""), "ok", 7000);
      if (r.index_job_id) {
        toast(`Indexación cbm corriendo (${r.index_job_id}) — el progreso `
          + `se ve en el tab Estado o con ↻ en la fila.`, "info", 7000);
      }
      $("#new-modal").classList.remove("open");
      loadProjects();
      refreshStatus();
    } catch (err) {
      msg.textContent = "";
      toast("Error: " + err.message, "err");
      save.disabled = false;
    }
  });
}

// ---- flags del proyecto (2026-08-16) ----
//
// Hasta ahora `read_only`, `sandbox`, `native_files` y compañía se
// editaban escribiendo JSON contra la API. Cambian lo que el experto
// PUEDE hacer, y la gente no configura lo que no encuentra.
//
// Se guardan de a uno, apenas se tocan. Un "guardar todo" al final
// suena más prolijo, pero si el PATCH falla a mitad quedan permisos
// aplicados y otros no, y nadie sabe cuáles — con permisos eso es peor
// que pedir un click más.

const FLAG_GRUPOS = [
  ["Tools", ["native_files", "native_shell", "sql_tools"]],
  ["Permisos de archivo", ["read_only", "sandbox", "rutas_extra",
                           "rutas_vedadas"]],
  ["Etapas del runner", ["three_stage", "verifier", "documenter"]],
  // Cada etapa con su modelo. El planificador y el documentador son
  // turnos cortos: correrlos con el modelo pesado del ejecutor era tirar
  // tokens, y hasta ahora solo se podía cambiar por SQL.
  ["Modelo por etapa", ["model", "planner_model", "verifier_model",
                        "documenter_model"]],
  ["Skills", ["inject_skills", "skills_mode"]],
  // 2026-08-21. El backend ya exponía el flag y acá no estaba, así que
  // no se podía prender desde ningún lado: `renderFlags` solo dibuja lo
  // que figura en esta lista. Agregar un flag al whitelist de Python sin
  // agregarlo acá es dejarlo apagado para siempre.
  ["Memoria", ["facts_always_on"]],
];

// Flags donde el valor "peligroso" no es el mismo para todos: apagar el
// sandbox abre el disco, pero apagar el documenter solo ahorra tokens.
// Solo los primeros piden confirmación.
const FLAG_AVISOS = {
  sandbox: {
    cuando: false,
    titulo: "Apagar el sandbox de archivos",
    texto: "Las tools de archivo van a poder leer y escribir en CUALQUIER "
      + "ruta del disco, no solo en el repo. read_only y las rutas vedadas "
      + "siguen aplicando. Un path mal calculado deja de tener freno.",
  },
  native_shell: {
    cuando: false,
    titulo: "Apagar el shell",
    texto: "El experto pierde la tool `shell`: no va a poder correr tests, "
      + "builds ni git en este proyecto.",
  },
};

let _flagsSlug = null;
// Catálogo de modelos PRENDIDOS, para los flags de tipo `model`. Se pide
// una vez por carga de la pantalla: es la misma lista para los cuatro
// selects.
let _modelosPrendidos = [];

export async function loadFlags(slug) {
  _flagsSlug = slug;
  const box = $("#edit-flags");
  if (!box) return;
  try {
    // Los dos en paralelo: los flags no dependen del catálogo, pero
    // renderFlags necesita los dos para pintar los selects de modelo.
    const [r, cat] = await Promise.all([
      api(`projects/${encodeURIComponent(slug)}/flags`),
      api("models").catch(() => ({ models: [] })),   // sin catálogo, el
    ]);                                             // select queda con
    _modelosPrendidos = cat.models || [];           // heredado y ya
    renderFlags(box, r.flags || {});
  } catch (e) {
    box.innerHTML = `<span class="muted text-xs">error cargando flags: ${
      escape(e.message)}</span>`;
  }
}

// Las <option> de un flag de tipo `model`. Pura (y exportada) para que
// model-options.test.mjs pueda correrla sin DOM.
//
// El "(heredado)" nombra con qué corre si no elegís nada: un select
// vacío no contesta la única pregunta que uno tiene ahí.
//
// El caso que obliga a la tercera rama: el proyecto tiene un spec fijo
// que YA NO está prendido en el catálogo (lo apagaron desde Modelos, o
// lo puso alguien por SQL). Sin una option propia ninguna queda
// `selected`, el browser cae en la primera — y la pantalla dice "el
// global" mientras el run sigue usando el spec viejo. La pantalla que
// existe para decir con qué corre no puede ser la que miente.
export function opcionesModelo(f, prendidos) {
  const valor = f.valor || "";
  const ops = [`<option value=""${!valor ? " selected" : ""}>heredado (${
    escape(f.default || "sin definir")})</option>`];
  for (const m of prendidos) {
    ops.push(`<option value="${escape(m.spec)}"${
      m.spec === valor ? " selected" : ""}>${
      m.vision === 1 ? "🖼 " : ""}${escape(m.label || m.spec)}</option>`);
  }
  if (valor && !prendidos.some((m) => m.spec === valor)) {
    ops.push(`<option value="${escape(valor)}" selected>⚠ ${escape(valor)}`
           + ` — apagado en el catálogo</option>`);
  }
  return ops;
}

function renderFlags(box, flags) {
  box.innerHTML = FLAG_GRUPOS.map(([titulo, nombres]) => {
    const filas = nombres.filter((n) => flags[n]).map((n) => {
      const f = flags[n];
      const marca = f.explicito
        ? '<span class="badge dim" title="puesto a mano en este proyecto">fijo</span>'
        : '<span class="badge dim" title="sigue el default del relay">default</span>';
      const reset = f.explicito
        ? `<button class="btn btn-xs flag-reset" data-flag="${escape(n)}"
             title="volver al default del relay">↺</button>`
        : "";
      let control;
      if (f.tipo === "bool") {
        control = `<input type="checkbox" class="flag-bool" data-flag="${
          escape(n)}"${f.valor ? " checked" : ""}>`;
      } else if (String(f.tipo).startsWith("enum:")) {
        const ops = String(f.tipo).split(":")[1].split(",");
        control = `<select class="select text-xs flag-enum" data-flag="${escape(n)}">
          ${ops.map((o) => `<option${o === f.valor ? " selected" : ""}>${
            escape(o)}</option>`).join("")}</select>`;
      } else if (f.tipo === "model") {
        control = `<select class="select text-xs flag-model" data-flag="${
          escape(n)}">${opcionesModelo(f, _modelosPrendidos).join("")}</select>`;
      } else {
        control = `<input type="text" class="input text-xs font-mono flag-list"
          data-flag="${escape(n)}" value="${escape((f.valor || []).join(", "))}"
          placeholder="separadas por coma">`;
      }
      return `<div class="flex items-start gap-2 py-1">
        <div class="pt-0.5">${control}</div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <code class="text-xs">${escape(n)}</code>${marca}${reset}
          </div>
          <p class="muted text-xs">${escape(f.descripcion || "")}</p>
        </div>
      </div>`;
    }).join("");
    return `<div>
      <h5 class="text-[11px] font-semibold uppercase tracking-wider text-zinc-400 mb-1">${
        escape(titulo)}</h5>
      ${filas}
    </div>`;
  }).join("");

  $$(".flag-bool").forEach((el) =>
    el.addEventListener("change", () =>
      saveFlag(el.dataset.flag, el.checked, el)));
  $$(".flag-enum").forEach((el) =>
    el.addEventListener("change", () => saveFlag(el.dataset.flag, el.value, el)));
  // "" en un flag de modelo significa "volver al global", que es
  // exactamente lo que el backend hace con null.
  $$(".flag-model").forEach((el) =>
    el.addEventListener("change", () =>
      saveFlag(el.dataset.flag, el.value || null, el)));
  // Las listas van por `change` (blur/Enter) y no por `input`: un PATCH
  // por tecla es ruido en el log y una carrera con el propio tipeo.
  $$(".flag-list").forEach((el) =>
    el.addEventListener("change", () => saveFlag(
      el.dataset.flag,
      el.value.split(",").map((x) => x.trim()).filter(Boolean), el)));
  $$(".flag-reset").forEach((el) =>
    el.addEventListener("click", () => saveFlag(el.dataset.flag, null, el)));
}

async function saveFlag(nombre, valor, el) {
  const aviso = FLAG_AVISOS[nombre];
  if (aviso && valor === aviso.cuando) {
    if (!await confirmModal({
      title: aviso.titulo, body: aviso.texto,
      confirmText: "Sí, apagalo", danger: true,
    })) {
      // Revertir el checkbox: el usuario dijo que no, la UI tiene que
      // mostrar lo que quedó guardado y no lo que llegó a clickear.
      if (el && el.type === "checkbox") el.checked = !valor;
      return;
    }
  }
  try {
    const r = await api(`projects/${encodeURIComponent(_flagsSlug)}/flags`, {
      method: "PATCH", body: JSON.stringify({ [nombre]: valor }),
    });
    if (r.error) { toast("Error: " + r.error, "err"); await loadFlags(_flagsSlug); return; }
    toast(`${nombre}: ${valor === null ? "vuelve al default" : JSON.stringify(valor)}`,
          nombre === "sandbox" && valor === false ? "warn" : "ok");
    // Re-render: cambia el badge fijo/default y aparece o desaparece el ↺.
    renderFlags($("#edit-flags"), r.flags || {});
  } catch (e) {
    toast("Error: " + e.message, "err");
    await loadFlags(_flagsSlug);
  }
}

// ---- editar proyecto ----

function openEditModal(slug) {
  modalLoad({
    id: "edit-modal",
    title: `Editar · ${slug}`,
    loader: () => api(`projects/${encodeURIComponent(slug)}`),
    render: renderEditForm,
    after: (r) => { wireEditForm(slug, r.project); loadFlags(slug); },
  });
}

function renderEditForm(r) {
  const p = r.project || {};
  const nc = parseNightConfig(p.night_config);
  return `<form id="edit-form" class="flex flex-col gap-3" data-pure-form>
    <div>
      <label class="label mb-1" for="edit-name">Nombre</label>
      <input id="edit-name" class="input w-full" value="${escape(p.name || "")}">
    </div>
    <div>
      <label class="label mb-1" for="edit-desc">Descripción</label>
      <input id="edit-desc" class="input w-full" value="${escape(p.description || "")}">
    </div>
    <div>
      <label class="label mb-1" for="edit-repo">Repo path</label>
      <input id="edit-repo" class="input w-full" value="${escape(p.repo_path || "")}">
      <p class="muted text-xs mt-1">Cambiar el repo path rompe el índice cbm
        del anterior — reindexa después si lo mueves.</p>
    </div>
    <div>
      <label class="label mb-1" for="edit-sp">System prompt (específico del repo)</label>
      <textarea id="edit-sp" class="input w-full" rows="6"
        placeholder="Reglas + contexto del repo que recibe el LLM. Se prependea al bloque ADR-010/011/020.">${escape(p.system_prompt || "")}</textarea>
    </div>
    <div>
      <label class="label mb-1" for="edit-discord-channel">Canal Discord default (Iter 10.1)</label>
      <input id="edit-discord-channel" class="input w-full"
        value="${escape(p.discord_channel_id || "")}"
        placeholder="ID del canal (string vacío = sin canal; manda null para limpiar)">
      <p class="muted text-xs mt-1">Si está vacío, el bot pregunta por DM al admin
        cuando llega un run de Discord sin canal mapeado.</p>
    </div>
    <hr>
    <h4 class="text-sm font-semibold text-zinc-300 mt-0">Flags del proyecto</h4>
    <p class="muted text-xs mb-2">Qué puede hacer el experto en este repo.
      Se guardan al toque (no esperan al 💾) porque cambian permisos, no
      texto: media edición aplicada sería peor que ninguna. Un flag en
      <em>default</em> sigue el valor del relay; uno tocado queda fijo aunque
      el default cambie.</p>
    <div id="edit-flags" class="flex flex-col gap-2">
      <span class="muted text-xs">cargando flags…</span>
    </div>
    <hr>
    <h4 class="text-sm font-semibold text-zinc-300 mt-0">Modo nocturno · night_config (JSON)</h4>
    <div class="grid grid-cols-2 gap-2">
      <div>
        <label class="label text-xs">build_cmd</label>
        <input id="edit-nc-build" class="input w-full text-sm"
               value="${escape(nc.build_cmd || "")}"
               placeholder="auto-detect si vacío">
      </div>
      <div>
        <label class="label text-xs">test_cmd</label>
        <input id="edit-nc-test" class="input w-full text-sm"
               value="${escape(nc.test_cmd || "")}"
               placeholder="auto-detect si vacío">
      </div>
      <div>
        <label class="label text-xs">max_diff_lines</label>
        <input id="edit-nc-diff" type="number" class="input w-full text-sm"
               value="${nc.max_diff_lines ?? 200}">
      </div>
      <div>
        <label class="label text-xs">discord_channel</label>
        <input id="edit-nc-channel" class="input w-full text-sm"
               value="${escape(nc.discord_channel || "#equipo-demo")}">
      </div>
      <div class="col-span-2">
        <label class="label text-xs">base_branch</label>
        <input id="edit-nc-base" class="input w-full text-sm"
               value="${escape(nc.base_branch || "main")}">
      </div>
    </div>
    <div class="toolbar mt-3">
      <button type="button" id="edit-save-inline" class="btn btn-primary"
        title="PATCH a /admin/api/projects/{slug}">Guardar cambios</button>
      <span id="edit-modal-msg-inline" class="muted"></span>
    </div>
  </form>`;
}

function parseNightConfig(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch { return {}; }
}

function wireEditForm(slug, project) {
  // Iter 4.7: el botón guardar vive DENTRO del form renderizado
  // (#edit-save-inline), no en el footer estático del modal (#edit-save).
  // Esto permite reusar renderEditForm en el admin-shell (que no tiene
  // footer) y también arregla el bug del iter 4.6: el footer estático
  // quedaba con save.disabled=true al cerrar el modal.
  const save = $("#edit-save-inline");
  const msg = $("#edit-modal-msg-inline");
  if (!save) return;
  // FIX: el footer del modal es estático (vive en index.html), entonces
  // si el handler anterior dejó save.disabled=true (éxito que cerró el
  // modal sin resetear), la próxima apertura lo encuentra bloqueado y
  // el click no dispara nada. Resetear acá al recablear.
  save.disabled = false;
  onClick("#edit-save-inline", async () => {
    save.disabled = true;
    msg.textContent = "guardando…";
    try {
      // FIX: mergear night_config existente con los overrides del form.
      // Antes se reconstruía desde cero con los 5 inputs → el PATCH
      // (que hace replace, no merge) borraba cualquier clave extra
      // seteada por API o feature futura. Comportamiento "campo vacío"
      // se conserva (delete de la clave) — eso lo decide el usuario.
      const existing = parseNightConfig(project.night_config) || {};
      const overrides = {
        build_cmd: $("#edit-nc-build").value.trim(),
        test_cmd: $("#edit-nc-test").value.trim(),
        max_diff_lines: parseInt($("#edit-nc-diff").value, 10) || 200,
        discord_channel: $("#edit-nc-channel").value.trim() || "#equipo-demo",
        base_branch: $("#edit-nc-base").value.trim() || "main",
      };
      const night_config = { ...existing };
      for (const k of Object.keys(overrides)) {
        if (overrides[k] === "" || overrides[k] == null) {
          delete night_config[k];
        } else {
          night_config[k] = overrides[k];
        }
      }
      // Iter 10.1: discord_channel_id se manda SIEMPRE en el body
      // para que la UI refleje el estado real: empty input → null
      // (clear), valor → trim. Si el PATCH general no distingue
      // string vacío de no-cambio, podemos usar el endpoint dedicado
      // /admin/api/projects/{slug}/discord-channel.
      const dcRaw = ($("#edit-discord-channel").value || "").trim();
      const body = {
        name: $("#edit-name").value.trim(),
        description: $("#edit-desc").value.trim(),
        repo_path: $("#edit-repo").value.trim(),
        system_prompt: $("#edit-sp").value,
        night_config,
        discord_channel_id: dcRaw === "" ? null : dcRaw,
      };
      const r = await api(`projects/${encodeURIComponent(slug)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      if (r.error) {
        msg.textContent = "";
        toast("Error: " + r.error, "err");
        save.disabled = false;
        return;
      }
      toast("Proyecto guardado ✓", "ok");
      loadProjects();
      refreshStatus();
      // Si estamos en el modal de edición viejo (#edit-modal), ciérralo;
      // si estamos en el admin-modal (tab "general"), el modal queda
      // abierto porque podría querer editar más campos. Eso lo decide
      // el usuario con el ✕.
      const modal = $("#edit-modal");
      if (modal && modal.classList.contains("open")) {
        modal.classList.remove("open");
      }
      // FIX: resetear también en éxito. Antes solo el catch lo hacía →
      // segundo guardado fallaba en silencio.
      save.disabled = false;
    } catch (err) {
      msg.textContent = "";
      toast("Error: " + err.message, "err");
      save.disabled = false;
    }
  });
}

// ---- experto (corre /experts/run desde la UI) ----

function openExpertModal(slug) {
  modalLoad({
    id: "expert-modal",
    title: `Experto · ${slug}`,
    loader: () => Promise.all([
      api(`projects/${encodeURIComponent(slug)}`),
      api(`projects/${encodeURIComponent(slug)}/system-prompt`),
    ]).then(([proj, sp]) => ({ project: proj.project, system_prompt: sp })),
    render: renderExpertForm,
    after: (r) => wireExpertForm(slug, r),
    watchdogDetail: `Leyendo el system_prompt efectivo del proyecto y los
      bloques ADR-010/011/020. Tarda unos segundos la primera vez.`,
  });
}

function renderExpertForm(r) {
  const p = r.project || {};
  const sp = r.system_prompt || {};
  const blocks = sp.blocks || {};
  const totalKb = ((sp.stats?.total || 0) / 1024).toFixed(1);
  const order = ["ponytail", "system_prompt", "skills", "workspace"];
  const labels = {
    ponytail: "1 · Ponytail",
    system_prompt: "2 · projects.system_prompt",
    skills: "3 · Skills (ADR-010)",
    workspace: "4 · Workspace (ADR-011)",
  };
  const preview = order
    .filter((k) => blocks[k])
    .map((k) => `<div class="prompt-block collapsed">
      <div class="prompt-block-header">
        <span><span class="prompt-block-toggle">▸</span>
          <span class="prompt-block-title">${escape(labels[k])}</span></span>
        <span class="prompt-block-meta">${(sp.stats?.[k] || 0)} chars</span>
      </div>
      <div class="prompt-block-body">${escape(blocks[k])}</div>
    </div>`).join("");
  return `<p class="muted mb-2">Vas a mandar un prompt al experto de
    <code>${escape(p.slug)}</code>. El system prompt efectivo totaliza
    <strong>${totalKb} KB</strong>. El LLM lo recibe ensamblado (los
    bloques de abajo) más tu mensaje.</p>
  <details class="mb-3"><summary class="cursor-pointer text-sm text-zinc-400">
    Ver system prompt ensamblado (${totalKb} KB)</summary>
    <div class="mt-2">${preview || '<p class="muted">(vacío)</p>'}</div>
  </details>
  <div>
    <label class="label mb-1" for="expert-input">Tu mensaje al experto
      <span class="fail">*</span></label>
    <textarea id="expert-input" class="input w-full" rows="5" required
      placeholder="ej: listame los controllers de Orders que tengan más de 200 líneas y sugerime un split"></textarea>
  </div>
  <div class="toolbar mt-2 items-center gap-4">
    <label class="text-xs text-zinc-400 flex items-center gap-2">
      <input type="checkbox" id="expert-async"> async (no esperar, devuelve chat_id)
    </label>
    <label class="text-xs text-zinc-400 flex items-center gap-2">
      <input type="checkbox" id="expert-conv" checked> 🧵 mantener conversación
    </label>
    <span class="text-xs text-zinc-400" id="expert-conv-info"></span>
    <button class="btn btn-xs hidden" id="expert-conv-reset" type="button">nueva conversación</button>
  </div>
  <div id="expert-output" class="mt-4 hidden">
    <h4>Output</h4>
    <pre class="chat-md" id="expert-output-text"></pre>
  </div>`;
}

//: Techo del fetch del modal de experto en modo sync. El server hace
//: polling hasta 320s (`deadline` en api_project_expert_run); el margen
//: es para que corte SIEMPRE el server, que sabe qué pasó con el run, y
//: no el cliente, que solo puede decir "no contestó".
const EXPERT_SYNC_TIMEOUT_MS = 330_000;


function wireExpertForm(slug, r) {
  const run = $("#expert-run");
  const input = $("#expert-input");
  const isAsync = $("#expert-async");
  const output = $("#expert-output");
  const outputText = $("#expert-output-text");
  const convCheck = $("#expert-conv");
  const convInfo = $("#expert-conv-info");
  const convReset = $("#expert-conv-reset");
  if (!run) return;

  // Conversación viva del modal (2026-07-12): la primera corrida con
  // "🧵 mantener conversación" crea una en el relay y las siguientes
  // la replayan — mismo mecanismo ADR-025 que los hilos de Discord.
  let convId = null;
  const refreshConvUi = () => {
    if (convInfo) convInfo.textContent = convId ? `conv: ${convId.slice(0, 8)}…` : "";
    if (convReset) convReset.classList.toggle("hidden", !convId);
  };
  onClick("#expert-conv-reset", () => { convId = null; refreshConvUi(); toast("Conversación reiniciada.", "ok"); });

  const refresh = () => { run.disabled = !input.value.trim(); };
  refresh();
  input.addEventListener("input", refresh);

  onClick("#expert-run", async () => {
    const prompt = input.value.trim();
    if (!prompt) { toast("El mensaje es obligatorio.", "err"); return; }
    run.disabled = true;
    output.classList.add("hidden");
    outputText.textContent = isAsync.checked ? "arrancando (async)…" : "corriendo…";
    output.classList.remove("hidden");
    try {
      const keep = convCheck ? convCheck.checked : false;
      // El tercer argumento NO es opcional acá. Sin él, `api()` usa su
      // default de 15s (FETCH_TIMEOUT_MS), que está bien para un GET de
      // una tabla y es absurdo para esto: en modo sync el server hace
      // polling hasta 320s esperando que el experto termine
      // (`api_project_expert_run` en admin.py). Medido el 2026-09-04: un
      // run trivial —"respondé ok", cero tools— tarda 31s, así que el
      // camino por DEFAULT (el checkbox async viene desmarcado) SIEMPRE
      // moría a los 15s con "el server no respondió"… mientras el run
      // seguía vivo y terminaba bien del otro lado, tocando archivos y
      // shell sin que el usuario se enterara. Peor que no hacer nada.
      const data = await api(`projects/${encodeURIComponent(slug)}/expert-run`, {
        method: "POST",
        body: JSON.stringify({
          prompt,
          async: isAsync.checked,
          conversation: keep ? convId : null,
          new_conversation: keep && !convId,
        }),
      }, isAsync.checked ? undefined : EXPERT_SYNC_TIMEOUT_MS);
      if (data.error) {
        outputText.textContent = `Error: ${data.error}`;
        toast("Error: " + data.error, "err");
      } else {
        if (data.conversation_id) { convId = data.conversation_id; refreshConvUi(); }
        if (isAsync.checked) {
          outputText.textContent = `chat_id: ${data.chat_id || data.id || "?"}\n` +
            (convId ? `conversation: ${convId}\n` : "") +
            `(async: ve al tab Estado o espera el notify en Discord)`;
        } else {
          const out = data.output || data.response || JSON.stringify(data, null, 2);
          outputText.textContent = out;
        }
      }
    } catch (err) {
      // En sync no tenemos chat_id —el server lo devuelve recién al
      // terminar—, así que si igual se corta hay que decir DÓNDE mirar:
      // el run sigue corriendo del otro lado y avisar "error" a secas
      // hace creer que no pasó nada.
      const corto = /timeout/i.test(err.message || "");
      outputText.textContent = corto && !isAsync.checked
        ? `${err.message}

OJO: el run NO se canceló, sigue corriendo en el `
          + `relay. Buscalo en el tab "En curso" o en "Chats".`
        : `Error: ${err.message}`;
      toast("Error: " + err.message, "err");
    } finally {
      run.disabled = false;
    }
  });
}

// ============================================================
// Modal de administración del proyecto (iter 4.7)
// ============================================================
// Colapsa los 10 botones de la fila en un shell con tabs internos.
// Cada tab reusa los renders que ya existían (renderEditForm,
// renderPromptBlocks, renderCbmDetail, renderDiffDetail, renderNight,
// renderExpertForm). El tab Workspace es nuevo (file CRUD + scaffold LLM).
//
// Diseño: el modal carga TODOS los datos de una al abrir (project detail
// + system_prompt + cbm + git-diff + night + workspace), y los muestra
// on-demand según el tab activo. No recargamos entre tabs — el server
// no se entera de los cambios de tab, solo cuando guardamos.

const ADMIN_TABS = [
  { key: "general",    label: "⚙️ General",     icon: "⚙️" },
  { key: "prompt",     label: "📝 Prompt",      icon: "📝" },
  { key: "workspace",  label: "📁 Workspace",   icon: "📁" },
  { key: "index",      label: "📊 Index cbm",   icon: "📊" },
  { key: "git",        label: "📜 Git",         icon: "📜" },
  { key: "night",      label: "🌙 Nocturno",    icon: "🌙" },
  { key: "expert",     label: "🧪 Experto",     icon: "🧪" },
  { key: "danger",     label: "⚠️ Peligro",     icon: "⚠️" },
];

// Estado vivo del modal abierto. Lo seteamos al abrir y lo leemos en
// los handlers internos. Más simple que andar pasando (slug, data, tab).
let _adminCtx = null;

function openAdminModal(slug) {
  // UI 2026-07-20: sidepanel en vez de modal fullscreen. Mantiene
  // el contexto del tab visible (la lista de proyectos queda detrás).
  // Si el viewport es <md, sigue funcionando (el panel ocupa 92vw).
  _adminCtx = { slug, data: null, tab: "general" };
  const gen = openSidePanel(`Administrar · ${slug}`);
  Promise.all([
    api(`projects/${encodeURIComponent(slug)}`),
    api(`projects/${encodeURIComponent(slug)}/system-prompt`),
    api(`projects/${encodeURIComponent(slug)}/cbm`),
    api(`projects/${encodeURIComponent(slug)}/git-diff`,
        null, 30_000).catch(() => ({ ok: false })),
    api(`projects/${encodeURIComponent(slug)}/night`),
  ]).then(([proj, sp, cbm, diff, night]) => {
    if (!isSidePanelCurrent(gen)) return;
    const r = { project: proj.project, system_prompt: sp, cbm, diff, night };
    _adminCtx.data = r;
    setSidePanelBody(renderAdminShell(r));
    wireAdminTabs();
    wireAdminDanger();
    activateAdminTab("general");
  }).catch((e) => {
    if (!isSidePanelCurrent(gen)) return;
    setSidePanelBody(
      `<p class="fail p-4 text-sm">error: ${escape(e.message)}</p>`);
  });
}

function renderAdminShell(r) {
  const tabs = ADMIN_TABS.map((t) =>
    `<button class="btn btn-xs admin-tab" data-tab="${t.key}"
      aria-selected="${t.key === "general" ? "true" : "false"}">${t.label}</button>`
  ).join("");
  return `<div class="flex flex-col gap-3">
    <div id="admin-tabs" class="flex flex-wrap gap-1 border-b border-zinc-800 pb-2">
      ${tabs}
    </div>
    <div id="admin-panel" class="min-h-[24rem]"></div>
  </div>`;
}

function wireAdminTabs() {
  const root = document.getElementById("admin-tabs");
  if (!root) return;
  root.addEventListener("click", (e) => {
    const btn = e.target.closest(".admin-tab");
    if (!btn) return;
    activateAdminTab(btn.dataset.tab);
  });
}

function activateAdminTab(key) {
  if (!_adminCtx) return;
  _adminCtx.tab = key;
  // Marcar tab activo
  document.querySelectorAll("#admin-tabs .admin-tab").forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.tab === key ? "true" : "false"));
  // Render del panel
  const panel = document.getElementById("admin-panel");
  if (!panel) return;
  const data = _adminCtx.data;
  const slug = _adminCtx.slug;
  switch (key) {
    case "general":   panel.innerHTML = renderEditForm({ project: data.project });
                       wireEditForm(slug, data.project);
                       // El form trae la sección de flags, que se llena
                       // por su propio GET. Sin esto queda en "cargando…"
                       // para siempre en el admin-shell.
                       loadFlags(slug);
                       return;
    case "prompt":    panel.innerHTML = renderPromptBlocks(data.system_prompt);
                       wirePromptBlocks();
                       return;
    case "workspace": panel.innerHTML = renderWorkspace(data.project);
                       wireWorkspace(slug, data.project);
                       return;
    case "index":     panel.innerHTML = renderCbmDetail(data.cbm);
                       wireCbmReindex(slug);
                       return;
    case "git":       panel.innerHTML = renderGitRemote(data.project)
                         + renderDiffDetail(data.diff);
                       wireGitRemote(slug);
                       return;
    case "night":     panel.innerHTML = renderNight(data.night);
                       wireNight(slug, data.night);
                       return;
    case "expert":    panel.innerHTML = renderExpertForm({
                         project: data.project,
                         system_prompt: data.system_prompt,
                       });
                       wireExpertForm(slug, { project: data.project,
                                              system_prompt: data.system_prompt });
                       return;
    case "danger":    panel.innerHTML = renderDanger(data.project);
                       return;
  }
}

function wirePromptBlocks() {
  // Mismo patrón que el modal original de prompt — toggles por bloque.
  document.querySelectorAll("#admin-panel .prompt-block-header").forEach((h) =>
    h.addEventListener("click", () => {
      h.parentElement.classList.toggle("collapsed");
      const tog = h.querySelector(".prompt-block-toggle");
      if (tog) tog.textContent =
        h.parentElement.classList.contains("collapsed") ? "▸" : "▾";
    }));
}

function renderDanger(project) {
  const slug = project.slug;
  return `<div class="warn-box">
    <p class="text-sm mb-3">
      <strong>Zona de peligro.</strong> Estas acciones afectan el proyecto
      <code>${escape(slug)}</code> de forma persistente. No hay vuelta atrás.
    </p>
    <div class="flex flex-wrap gap-2">
      <button id="admin-disable" class="btn btn-xs danger"
        title="soft delete (enabled=false, reversible)">Soft delete (desactivar)</button>
      <button id="admin-delete" class="btn btn-xs danger"
        title="purga hard de la fila de la DB (no se recupera)">Purga hard</button>
    </div>
    <p class="muted text-xs mt-3">
      El soft delete pone <code>enabled=false</code>; puedes reactivarlo
      con PATCH. El hard delete borra la fila de la DB (incluyendo
      <code>system_prompt</code>, <code>mcp_servers</code>, etc); si el
      repo sigue en cbm, vuelve a aparecer como huérfano.
    </p>
  </div>`;
}

function wireAdminDanger() {
  document.addEventListener("click", async (e) => {
    const slug = _adminCtx?.slug;
    if (!slug) return;
    if (e.target.id === "admin-disable") {
      if (!await confirmModal({
        body: `¿Desactivar "${slug}"? (soft, reversible)`,
        confirmText: "Desactivar",
      })) return;
      try {
        await api(`projects/${slug}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: false }),
        });
        toast("Desactivado ✓", "ok");
        loadProjects();
        refreshStatus();
        // refrescar el ctx
        const proj = await api(`projects/${slug}`);
        if (_adminCtx) _adminCtx.data.project = proj.project;
        activateAdminTab("danger");
      } catch (err) { toast("Error: " + err.message, "err"); }
    }
    if (e.target.id === "admin-delete") {
      const ok = await confirmModal({
        title: `PURGAR "${slug}"`,
        body: "Borra la fila entera de la DB. Si el repo sigue en cbm, "
          + "vuelve a aparecer como huérfano. Acción NO reversible.",
        confirmText: "Sí, purgar",
        danger: true,
      });
      if (!ok) return;
      try {
        const r = await api(`projects/${slug}`, { method: "DELETE" });
        if (r.error) { toast("Error: " + r.error, "err"); return; }
        toast("Purgado ✓", "ok");
        loadProjects();
        loadOrphans();
        refreshStatus();
        closeModal("admin-modal");
      } catch (err) { toast("Error: " + err.message, "err"); }
    }
  });
}

// ---- tab Workspace (file CRUD + scaffold LLM) ----

function renderWorkspace(project) {
  const slug = project.slug;
  return `<div class="flex flex-col gap-3">
    <p class="muted text-sm">
      Archivos de texto dentro del repo <code>${escape(project.repo_path)}</code>.
      Los cambios se persisten en el repo (no en la DB) — así cbm los
      indexa y el experto los lee en el system prompt.
    </p>

    <div class="toolbar">
      <select id="ws-subdir" class="select text-sm">
        <option value="">(raíz)</option>
      </select>
      <button id="ws-refresh" class="btn btn-xs" title="refrescar lista">↻ Refrescar</button>
      <button id="ws-new-file" class="btn btn-xs" title="archivo nuevo en el subdir actual">➕ Archivo</button>
      <button id="ws-scaffold" class="btn btn-xs" title="generar README + docs vía LLM">🌱 Scaffold con LLM</button>
    </div>

    <div id="ws-list" class="card p-2 max-h-48 overflow-auto">
      <p class="muted text-xs">cargando…</p>
    </div>

    <div id="ws-editor" class="card p-3 hidden">
      <div class="flex items-center justify-between mb-2">
        <code id="ws-file-path" class="text-sm text-zinc-300"></code>
        <div class="flex gap-1">
          <button id="ws-preview-btn" class="btn btn-xs hidden"
            title="Vista previa del Markdown">Vista previa</button>
          <button id="ws-cancel" class="btn btn-xs" title="cerrar sin guardar">Cancelar</button>
          <button id="ws-save" class="btn btn-xs btn-primary"
            title="PUT /admin/api/projects/{slug}/workspace/file">Guardar</button>
        </div>
      </div>
      <textarea id="ws-content" class="input w-full font-mono text-xs"
        rows="14"></textarea>
      <div id="ws-preview" class="card p-4 hidden max-w-none
        [&_h1]:text-lg [&_h2]:text-base [&_h3]:text-sm
        [&_h1]:text-zinc-100 [&_h2]:text-zinc-200 [&_h3]:text-zinc-300
        [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold
        [&_h1]:mt-4 [&_h2]:mt-3 [&_h3]:mt-2 [&_h1]:mb-2 [&_h2]:mb-1 [&_h3]:mb-1
        [&_p]:text-zinc-300 [&_p]:text-sm [&_p]:leading-relaxed
        [&_code]:bg-zinc-800 [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-xs
        [&_pre]:bg-zinc-900 [&_pre]:p-3 [&_pre]:rounded [&_pre]:overflow-auto
        [&_pre_code]:bg-transparent [&_pre_code]:p-0
        [&_a]:text-emerald-400 [&_a]:underline
        [&_ul]:text-zinc-300 [&_ul]:text-sm [&_ul]:list-disc [&_ul]:pl-5
        [&_ol]:text-zinc-300 [&_ol]:text-sm [&_ol]:list-decimal [&_ol]:pl-5
        [&_li]:my-0.5
        [&_blockquote]:border-l-2 [&_blockquote]:border-zinc-600
        [&_blockquote]:pl-3 [&_blockquote]:text-zinc-400 [&_blockquote]:italic
        [&_table]:w-full [&_table]:text-sm [&_table]:text-zinc-300
        [&_th]:text-left [&_th]:text-zinc-200 [&_th]:px-2 [&_th]:py-1 [&_th]:border-b [&_th]:border-zinc-700
        [&_td]:px-2 [&_td]:py-1 [&_td]:border-b [&_td]:border-zinc-800
        [&_img]:max-w-full [&_img]:rounded
        [&_hr]:border-zinc-700 [&_hr]:my-4"></div>
      <p id="ws-msg" class="muted text-xs mt-1"></p>
    </div>

    <div id="ws-scaffold-panel" class="card p-3 hidden">
      <h4 class="text-sm font-semibold text-zinc-200 mt-0 mb-2">🌱 Scaffold con LLM</h4>
      <textarea id="ws-scaffold-prompt" class="input w-full font-mono text-xs"
        rows="4"
        placeholder="ej: SaaS .NET 8 backend + React 18 frontend, con PostgreSQL, deploy en Azure. Stack real, no placeholders."></textarea>
      <div class="toolbar mt-2">
        <label class="text-xs text-zinc-400 flex items-center gap-2">
          <input type="checkbox" id="ws-scaffold-overwrite">
          sobrescribir README.md / docs/PLAN.md si existen
        </label>
        <button id="ws-scaffold-generate" class="btn btn-xs btn-primary"
          title="POST /admin/api/projects/{slug}/workspace/scaffold">Generar preview</button>
      </div>
      <div id="ws-scaffold-output" class="mt-2"></div>
    </div>
  </div>`;
}

async function wsLoadSubdirs(project) {
  // Walk nivel 1: lista dirs del repo para popular el select.
  // Si falla, el select queda con la opción "(raíz)".
  try {
    const r = await api(
      `projects/${encodeURIComponent(project.slug)}/workspace/files`);
    const dirs = (r.entries || [])
      .filter((e) => e.is_dir)
      .map((e) => `<option value="${escape(e.path)}">${escape(e.path)}/</option>`)
      .join("");
    const sel = document.getElementById("ws-subdir");
    if (sel) sel.innerHTML = `<option value="">(raíz)</option>${dirs}`;
  } catch (_) { /* best-effort */ }
}

async function wsLoadFiles(slug, subdir) {
  const list = document.getElementById("ws-list");
  if (!list) return;
  list.innerHTML = `<p class="muted text-xs">cargando…</p>`;
  try {
    const q = subdir ? `?subdir=${encodeURIComponent(subdir)}` : "";
    const r = await api(`projects/${encodeURIComponent(slug)}/workspace/files${q}`);
    const entries = r.entries || [];
    if (entries.length === 0) {
      list.innerHTML = `<p class="muted text-xs">(vacío)</p>`;
      return;
    }
    list.innerHTML = entries.map((e) => {
      if (e.is_dir) {
        return `<button class="ws-entry block w-full text-left px-2 py-1 rounded hover:bg-zinc-800 text-sm text-zinc-400"
          data-dir="${escape(e.path)}">📁 ${escape(e.name)}/</button>`;
      }
      const sizeKb = ((e.size || 0) / 1024).toFixed(1);
      return `<button class="ws-entry block w-full text-left px-2 py-1 rounded hover:bg-zinc-800 text-sm"
        data-path="${escape(e.path)}">📄 ${escape(e.name)}
          <span class="muted text-xs ml-2">${sizeKb} KB</span></button>`;
    }).join("");
  } catch (e) {
    list.innerHTML = `<p class="fail text-xs">Error: ${escape(e.message)}</p>`;
  }
}

async function wsOpenFile(slug, path) {
  const editor = document.getElementById("ws-editor");
  const pathEl = document.getElementById("ws-file-path");
  const content = document.getElementById("ws-content");
  const preview = document.getElementById("ws-preview");
  const previewBtn = document.getElementById("ws-preview-btn");
  if (!editor || !pathEl || !content) return;
  try {
    const r = await api(
      `projects/${encodeURIComponent(slug)}/workspace/file?path=${encodeURIComponent(path)}`);
    pathEl.textContent = r.path;
    content.value = r.content;
    content.dataset.path = r.path;
    editor.classList.remove("hidden");
    // Show preview button for .md files
    if (preview && previewBtn) {
      preview.classList.add("hidden");
      content.classList.remove("hidden");
      if (path.toLowerCase().endsWith(".md")) {
        previewBtn.classList.remove("hidden");
        previewBtn.dataset.active = "0";
        previewBtn.textContent = "Vista previa";
      } else {
        previewBtn.classList.add("hidden");
      }
    }
  } catch (e) {
    toast("No se pudo abrir: " + e.message, "err");
  }
}

async function wsSaveFile(slug) {
  const content = document.getElementById("ws-content");
  const msg = document.getElementById("ws-msg");
  const saveBtn = document.getElementById("ws-save");
  if (!content || !saveBtn) return;
  const path = content.dataset.path;
  if (!path) return;
  saveBtn.disabled = true;
  msg.textContent = "guardando…";
  try {
    const r = await api(
      `projects/${encodeURIComponent(slug)}/workspace/file`, {
        method: "PUT",
        body: JSON.stringify({ path, content: content.value, overwrite: true }),
      });
    msg.textContent = `guardado ✓ (${r.size} chars)`;
    toast("Archivo guardado ✓", "ok");
  } catch (e) {
    msg.textContent = "";
    toast("Error al guardar: " + e.message, "err");
  } finally {
    saveBtn.disabled = false;
  }
}

// marked + DOMPurify vendorizados como static/vendor-* con la versión
// EN EL NOMBRE (admin.py les da Cache-Control de 24h: si el nombre no
// cambia, un upgrade queda invisible hasta un día). Flat porque
// admin_static no sirve subdirectorios. Lazy import la primera vez que
// se pide el preview — cero red externa en ningún caso.
let _markedMod = null;
let _purifyMod = null;

/** Toggle between markdown editor and rendered preview. */
async function wsTogglePreview() {
  const content = document.getElementById("ws-content");
  const preview = document.getElementById("ws-preview");
  const btn = document.getElementById("ws-preview-btn");
  if (!content || !preview || !btn) return;
  const active = btn.dataset.active === "1";
  if (active) {
    // Back to editor
    preview.classList.add("hidden");
    content.classList.remove("hidden");
    btn.dataset.active = "0";
    btn.textContent = "Vista previa";
    return;
  }
  // Load marked + DOMPurify (vendor local) if not loaded yet
  if (!_markedMod) {
    btn.textContent = "⏳ cargando…";
    [_markedMod, _purifyMod] = await Promise.all([
      import("./vendor-marked-18.0.7.esm.js"),
      import("./vendor-purify-3.4.15.es.js"),
    ]);
  }
  // Render markdown — sanitizado: esto va directo a innerHTML.
  const md = content.value || "";
  preview.innerHTML = _purifyMod.default.sanitize(_markedMod.marked.parse(md));
  preview.classList.remove("hidden");
  content.classList.add("hidden");
  btn.dataset.active = "1";
  btn.textContent = "✏️ Editar";
}

async function wsScaffold(slug) {
  const prompt = document.getElementById("ws-scaffold-prompt")?.value.trim();
  const overwrite = !!document.getElementById("ws-scaffold-overwrite")?.checked;
  const out = document.getElementById("ws-scaffold-output");
  const btn = document.getElementById("ws-scaffold-generate");
  if (!prompt) { toast("Prompt requerido.", "err"); return; }
  if (!out || !btn) return;
  btn.disabled = true;
  out.innerHTML = `<p class="muted text-xs">generando (puede tardar 5-60s, el LLM a veces se toma su tiempo)…</p>`;
  try {
    // LLM one-shot puede colgarse fácil: timeout holgado (60s). El backend
    // no tiene guard — si el LLM tarda más, el server sigue gastando API
    // hasta terminar o fallar; el cliente se rinde y muestra retry hint.
    const r = await api(
      `projects/${encodeURIComponent(slug)}/workspace/scaffold`, {
        method: "POST",
        body: JSON.stringify({ prompt, apply: false, overwrite }),
      }, 60_000);
    if (r.error) {
      out.innerHTML = renderScaffoldError(r.error);
      return;
    }
    const fileRows = (r.files || []).map((f) =>
      `<tr>
        <td class="text-xs"><code>${escape(f.path)}</code></td>
        <td class="tabular-nums text-xs muted">${f.size} chars</td>
        <td class="text-xs"><button class="ws-preview btn btn-xs"
          data-path="${escape(f.path)}" data-content="${escape(f.content)}">
          Ver</button></td>
      </tr>`
    ).join("");
    const rejectedRows = (r.rejected || []).map((p) =>
      `<li><code class="text-xs">${escape(p)}</code></li>`).join("");
    out.innerHTML = `
      <p class="muted text-xs mb-2"><strong>${escape(r.rationale || "(sin rationale)")}</strong></p>
      <table class="status-detail"><thead><tr>
        <th class="th">path</th><th class="th">size</th><th class="th"></th>
      </tr></thead><tbody>${fileRows}</tbody></table>
      ${rejectedRows ? `<p class="warn-box text-xs">rechazados:
        <ul>${rejectedRows}</ul></p>` : ""}
      <div class="toolbar mt-3">
        <button id="ws-scaffold-apply" class="btn btn-xs btn-primary"
          ${(r.files || []).length === 0 ? "disabled" : ""}
          title="escribir los archivos al repo">Aplicar al repo</button>
        <span class="muted text-xs">apply=true usa overwrite=${overwrite ? "true" : "false"}</span>
      </div>`;
    onClick("#ws-scaffold-apply", async () => {
      try {
        const r2 = await api(
          `projects/${encodeURIComponent(slug)}/workspace/scaffold`, {
            method: "POST",
            body: JSON.stringify({ prompt, apply: true, overwrite }),
          }, 60_000);
        if (r2.error) {
          toast(r2.error, "err", 6000);
          return;
        }
        toast(`Aplicados ${r2.written.length} archivos ✓`, "ok");
        // refrescar lista
        const subdir = document.getElementById("ws-subdir")?.value || "";
        await wsLoadFiles(slug, subdir);
      } catch (e) {
        toast(toastMessage(e.message), "err", 6000);
      }
    });
    document.querySelectorAll("#ws-scaffold-output .ws-preview").forEach((b) =>
      b.addEventListener("click", () => {
        const path = b.dataset.path;
        const content = b.dataset.content;
        const editor = document.getElementById("ws-editor");
        const pathEl = document.getElementById("ws-file-path");
        const ta = document.getElementById("ws-content");
        if (editor && pathEl && ta) {
          pathEl.textContent = path;
          ta.value = content;
          ta.dataset.path = path;
          editor.classList.remove("hidden");
        }
      }));
  } catch (e) {
    out.innerHTML = renderScaffoldError(e.message);
  } finally {
    btn.disabled = false;
  }
}

// Errores del scaffold: timeouts del LLM son transitorios (el server
// puede estar lento o la API del vendor overloaded). El mensaje tiene
// que sugerir retry, no "refresca la página o revisa el relay" como si
// fuera un bug del sistema. Otros errores (502 del backend, paths
// inválidos, etc.) sí merecen el tono de "algo se rompió".
function renderScaffoldError(msg) {
  const isTimeout = /timeout/i.test(msg || "");
  if (isTimeout) {
    return `<div class="warn-box text-sm">
      <p class="mb-1"><strong>⏱ El LLM no respondió a tiempo.</strong></p>
      <p class="muted text-xs">${escape(msg)}</p>
      <p class="muted text-xs mt-1">Esto suele ser pasajero (vendor lento,
        cola de requests). Espera medio minuto y vuelve a generar el
        preview — el botón ya está liberado.</p>
    </div>`;
  }
  return `<p class="fail text-sm">Error: ${escape(msg)}</p>`;
}

// Misma idea que renderScaffoldError pero compacta para toast().
// Devuelve un string listo para mostrar.
function toastMessage(msg) {
  if (/timeout/i.test(msg || "")) {
    return `LLM no respondió a tiempo — espera medio minuto y reintenta. (${msg})`;
  }
  return msg;
}

function wireWorkspace(slug, project) {
  const sel = document.getElementById("ws-subdir");
  const scaffoldPanel = document.getElementById("ws-scaffold-panel");

  wsLoadSubdirs(project);
  wsLoadFiles(slug, "");

  if (sel) sel.onchange = () => wsLoadFiles(slug, sel.value);
  onClick("#ws-refresh", () => wsLoadFiles(slug, sel?.value || ""));
  onClick("#ws-new-file", () => {
    const name = prompt("Path del archivo nuevo (relativo al repo):");
    if (!name) return;
    const editor = document.getElementById("ws-editor");
    const pathEl = document.getElementById("ws-file-path");
    const ta = document.getElementById("ws-content");
    if (editor && pathEl && ta) {
      pathEl.textContent = name;
      ta.value = "";
      ta.dataset.path = name;
      editor.classList.remove("hidden");
    }
  });
  onClick("#ws-scaffold", () => {
    scaffoldPanel.classList.toggle("hidden");
  });
  onClick("#ws-cancel", () => {
    document.getElementById("ws-editor").classList.add("hidden");
  });
  onClick("#ws-save", () => wsSaveFile(slug));
  onClick("#ws-preview-btn", () => wsTogglePreview());
  onClick("#ws-scaffold-generate", () => wsScaffold(slug));

  // clicks en la lista: si es archivo, abrir; si es dir, navegar.
  onClick("#ws-list", (e) => {
    const entry = e.target.closest(".ws-entry");
    if (!entry) return;
    if (entry.dataset.path) wsOpenFile(slug, entry.dataset.path);
    else if (entry.dataset.dir && sel) {
      sel.value = entry.dataset.dir;
      wsLoadFiles(slug, sel.value);
    }
  });
}

export function initProjects() {
  onClick("#project-new", openNewModal);
  onClick("#projects-refresh", loadProjects);
  onClick("#projects-orphans", () => {
    document.querySelector('.tab[data-tab="orphans"]').click();
  });
  onClick("#prompt-modal-copy", copyPromptAssembled);
  // Los handlers reales los cablean wireXxxForm después del render del modal.
  // Estos noop evitan que init corra antes de que el modal exista.
  onClick("#expert-run", () => {});
  // (#edit-save ya no se usa como id — el botón vive dentro del form)
  onClick("#new-save", () => {});
  onClick("#cbm-reindex", () => {});
}
