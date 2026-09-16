// Tab Gestión: el tablero de empresa (Projects v2) leído con `gh`.
// Fase 5 del seguimiento por GitHub (ADR-036). Read-only sobre los
// items: mover tarjetas se hace en GitHub.
//
// Vincular/crear un tablero NO se reimplementa acá: la lista de
// pendientes abre el MISMO modal 📋 del tab Proyectos, que ya tiene ese
// flujo con su desplegable y su botón de crear. (La v1 metía un select
// de 16 opciones dentro de una grilla de 4 columnas: el nombre del
// proyecto quedaba en 0px y el botón cortado. Inusable.)

import { $, api, escape, onClick } from "./api.js";
import { statCell, toast } from "./ui.js";
import { renderColumnas } from "./board-view.js";
import { openGithubModal } from "./tab-projects.js";

// Se acuerda entre renders si el usuario pidió ver los ocultos.
let _verOcultos = false;

export function initGestion() {
  onClick("#gestion-refresh", () => loadGestion());
  const sel = $("#gestion-board-sel");
  if (sel) sel.onchange = () => loadGestion();
}

export async function loadGestion() {
  const box = $("#gestion-board");
  if (!box) return;
  box.innerHTML = `<p class="muted">Consultando GitHub…</p>`;
  try {
    // El selector manda "owner/number"; vacío = el tablero principal de
    // system_config. El endpoint acepta los dos caminos.
    const elegido = ($("#gestion-board-sel")?.value || "").trim();
    const qs = elegido
      ? `?owner=${encodeURIComponent(elegido.split("/")[0])}`
        + `&number=${encodeURIComponent(elegido.split("/")[1])}`
      : "";
    const [r, { projects = [] }] = await Promise.all([
      api(`github/board${qs}`), api("projects"),
    ]);
    let boards = [];
    if (r.owner) {
      try {
        ({ boards = [] } = await api(
          `github/boards?owner=${encodeURIComponent(r.owner)}`));
      } catch (e) {
        // Falla blanda a proposito: el tablero principal se renderiza
        // igual y solo queda sin poblar el selector de "cambiar de
        // tablero". Pero blanda no es muda — sin esto el selector se veia
        // vacio y parecia que la organizacion no tenia tableros.
        toast("No pude listar los tableros de " + r.owner + ": " + e.message
              + ". El tablero actual se muestra igual.", "warn", 7000);
      }
    }
    box.innerHTML = renderBoard(r) + renderSinTablero(projects);
    wireSinTablero();
    llenarSelector(r.owner, boards, projects);
  } catch (e) {
    console.error(e);
    box.innerHTML = `<p class="warn-box">No se pudo leer el tablero: ${escape(e.message)}</p>`;
    toast("Error leyendo el tablero: " + e.message, "err");
  }
}

// Selector de tablero, anotado con el proyecto que tiene cada uno
// vinculado ("#16 — Demo · sample-app").
function llenarSelector(owner, boards, projects) {
  const sel = $("#gestion-board-sel");
  if (!sel || !owner || !boards.length) return;
  const porNumero = {};
  for (const p of projects || []) {
    const gp = p.github_project;
    if (gp && gp.number != null) {
      (porNumero[gp.number] = porNumero[gp.number] || []).push(p.slug);
    }
  }
  const actual = sel.value;
  sel.innerHTML = `<option value="">(principal)</option>`
    + boards.map((b) => {
        const duenos = porNumero[b.number];
        const etiqueta = `#${b.number} — ${b.title || ""}`
          + (duenos ? ` · ${duenos.join(", ")}` : "");
        return `<option value="${owner}/${b.number}"${
          actual === `${owner}/${b.number}` ? " selected" : ""
        }>${escape(etiqueta)}</option>`;
      }).join("");
}

// ---- pendientes: proyectos que todavía no tienen tablero ----

function fila(p, oculto) {
  return `<div class="pend-item">
    <span class="pend-nombre" title="${escape(p.name || p.slug)}">${escape(p.slug)}</span>
    ${oculto
      ? `<button class="btn btn-xs devolver skip-board" data-slug="${escape(p.slug)}"
                 data-skip="0" title="volver a la lista de pendientes">↩</button>`
      : `<button class="btn btn-xs abrir-board" data-slug="${escape(p.slug)}"
                 title="vincular un tablero existente o crear uno"
                 aria-label="vincular un tablero existente o crear uno"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M7.5 9l3 3-3 3M13 15h4" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
         <button class="btn btn-xs danger skip-board" data-slug="${escape(p.slug)}"
                 data-skip="1" title="este proyecto no lleva tablero"
                 aria-label="este proyecto no lleva tablero"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>`}
  </div>`;
}

function renderSinTablero(projects) {
  const activos = projects.filter((p) => p.enabled);
  const conTablero = activos.filter((p) => p.github_project);
  const ocultos = activos.filter((p) => !p.github_project && p.github_project_skip);
  const sin = activos.filter((p) => !p.github_project && !p.github_project_skip);

  const pie = ocultos.length
    ? `<p class="muted mt-3">${ocultos.length} fuera de la lista.
        <button id="toggle-ocultos" class="btn btn-xs">
          ${_verOcultos ? "ocultar" : "ver"}</button></p>
       ${_verOcultos
          ? `<div class="pend-grid mt-2">${ocultos.map((p) => fila(p, true)).join("")}</div>`
          : ""}`
    : "";

  const cabecera = `<h3 class="section-title">Proyectos sin tablero
    (${sin.length} de ${activos.length}${
      conTablero.length ? ` · ${conTablero.length} con tablero` : ""})</h3>`;

  if (!sin.length) {
    return cabecera + `<p class="muted">Ninguno pendiente.</p>` + pie;
  }
  return cabecera
    + `<p class="muted mb-3">📋 abre el panel del proyecto para vincular un
       tablero existente o crear uno. ✕ lo saca de esta lista (no borra
       nada).</p>
       <div class="pend-grid">${sin.map((p) => fila(p, false)).join("")}</div>`
    + pie;
}

function wireSinTablero() {
  document.querySelectorAll(".abrir-board").forEach((b) => {
    // Reusa el modal del tab Proyectos; al vincular/crear ahí, esta
    // lista se refresca sola.
    b.onclick = () => openGithubModal(b.dataset.slug, loadGestion);
  });

  document.querySelectorAll(".skip-board").forEach((b) => {
    b.onclick = async () => {
      const skip = b.dataset.skip === "1";
      b.disabled = true;
      try {
        await api(`projects/${encodeURIComponent(b.dataset.slug)}/github-project`,
                  { method: "PUT", body: JSON.stringify({ skip }) });
        toast(skip ? `${b.dataset.slug} fuera de la lista`
                   : `${b.dataset.slug} de vuelta en la lista`, "ok");
        loadGestion();
      } catch (e) {
        toast(`No se pudo: ${e.message}`, "err");
        b.disabled = false;
      }
    };
  });

  onClick("#toggle-ocultos", () => { _verOcultos = !_verOcultos; loadGestion(); });
}

// ---- tablero ----

function renderBoard(r) {
  if (!r.configured) {
    const lista = (r.boards || []).length
      ? `<p class="muted mt-3">Tableros de <code>${escape(r.owner)}</code>:</p>
         <ul class="mt-1">${r.boards.map((b) =>
           `<li class="muted"><code>#${b.number}</code> — ${escape(b.title || "")}</li>`).join("")}</ul>`
      : "";
    return `<div class="card p-5">
      <p class="warn-box">⚠ Tablero de empresa sin configurar${
        r.error ? `: ${escape(r.error)}` : ""}.</p>
      <p class="muted mt-3">Se elige en el tab <strong>Config</strong> →
        <em>Seguimiento GitHub</em>: poné la organización, tocá
        <strong>Buscar tableros</strong> y elegí uno.</p>${lista}
    </div>`;
  }

  const items = Object.values(r.columns || {}).flat();
  const hechos = (r.columns?.["Done"] || []).length;
  const sinAsignar = items.filter((i) => !(i.assignees || []).length).length;
  const pct = items.length ? Math.round((hechos / items.length) * 100) : 0;

  const kpis = `<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
    ${statCell(items.length, "items en el tablero")}
    ${statCell(`${pct}%`, "completado", pct >= 66 ? "text-emerald-400"
      : pct >= 33 ? "text-amber-400" : "")}
    ${statCell(sinAsignar, "sin asignar",
      sinAsignar ? "text-amber-400" : "text-emerald-400")}
    ${statCell((r.repos || []).length, "repos involucrados")}
  </div>`;

  return `<div class="mb-1 flex flex-wrap items-baseline gap-3">
      <h3 class="text-lg font-semibold text-zinc-100">
        ${escape(r.title || `Tablero #${r.number}`)}
      </h3>
      <a href="${escape(r.url || "#")}" target="_blank" rel="noopener"
         class="text-sm text-sky-300 hover:underline">↗ abrir en GitHub</a>
      <span class="muted">· mover tarjetas se hace allá</span>
    </div>
    ${kpis}
    <div class="progress mt-4" title="${hechos} de ${items.length} en Done">
      <div class="progress-fill" style="width:${pct}%"></div>
    </div>
    <div class="mt-5">${renderColumnas(r.columns)}</div>`;
}
