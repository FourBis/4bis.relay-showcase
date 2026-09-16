// Render del kanban de un tablero Projects v2 (ADR-036).
// Vive acá y no en un tab porque lo pintan DOS vistas: el tab Gestión y
// el modal 📋 de cada proyecto. Módulo aparte para que ninguno de los
// dos tenga que importar al otro (import circular).

import { escape } from "./api.js";

// Orden canónico: lo pendiente a la izquierda, lo terminado a la
// derecha. GitHub no devuelve orden de columna y alfabético dejaba
// "Done" primero — justo lo que nadie mira. Lo no reconocido va al final.
const ORDEN = ["backlog", "todo", "ready", "in progress", "en curso",
               "in review", "blocked", "done"];

const TONO = {
  "backlog": "dim", "todo": "dim", "ready": "info",
  "in progress": "warn", "en curso": "warn",
  "in review": "info", "blocked": "err", "done": "ok",
};

export function ordenarColumnas(columns) {
  return Object.entries(columns || {}).sort(([a], [b]) => {
    const ia = ORDEN.indexOf(a.toLowerCase());
    const ib = ORDEN.indexOf(b.toLowerCase());
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
}

// Iniciales para el avatar: "UsuarioDemo" → "UD". Sin foto: el avatar
// real exige otra request por usuario.
function iniciales(login) {
  const s = String(login || "").replace(/[^A-Za-z0-9]/g, " ").trim();
  const partes = s.split(/\s+|(?=[A-Z][a-z])/).filter(Boolean);
  return ((partes[0]?.[0] || "") + (partes[1]?.[0] || partes[0]?.[1] || ""))
    .toUpperCase() || "?";
}

function tarjeta(it) {
  const asignados = it.assignees || [];
  const repo = String(it.repository || "").split("/").pop();
  return `<li class="board-card">
    <div class="board-card-title">
      <a href="${escape(it.url || "#")}" target="_blank" rel="noopener"
         class="board-card-link">${escape(it.title || "(sin título)")}</a>
    </div>
    <div class="board-card-meta">
      ${it.number != null ? `<span class="board-num">#${it.number}</span>` : ""}
      ${repo ? `<span class="badge dim" title="${escape(it.repository)}">${escape(repo)}</span>` : ""}
      ${asignados.length
        ? asignados.map((a) =>
            `<span class="avatar" title="${escape(a)}">${escape(iniciales(a))}</span>`).join("")
        : `<span class="badge warn" title="nadie lo tiene tomado">sin asignar</span>`}
    </div>
  </li>`;
}

export function renderColumnas(columns) {
  const cols = ordenarColumnas(columns);
  if (!cols.length) return `<p class="muted">El tablero está vacío.</p>`;
  return `<div class="board-cols">
    ${cols.map(([nombre, its]) => `
      <section class="board-col">
        <header class="board-col-head">
          <span class="badge ${TONO[nombre.toLowerCase()] || "dim"}">${escape(nombre)}</span>
          <span class="board-col-count">${its.length}</span>
        </header>
        <ul class="board-col-list">${its.map(tarjeta).join("")}</ul>
      </section>`).join("")}
  </div>`;
}
