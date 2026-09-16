// 4bis.relay admin UI — tabla de datos con orden, filtro y paginacion.
//
// El estado previo era: cada tab arma su <tbody> a mano y listo. Con 53
// proyectos (o 60 skills, o los MCPs) eso son filas planas sin ordenar,
// sin buscar y sin cortar — el usuario scrollea y busca con Ctrl+F.
//
// La tabla se pinta en DOS tiempos a proposito:
//   - el shell (toolbar + thead + footer) se monta UNA vez;
//   - `paint()` solo reescribe el <tbody>, el contador y la paginacion.
// Si se reescribiera todo, cada refresh de un poller le robaria el foco
// al input de busqueda y borraria lo tipeado a mitad de palabra.

import { escape } from "./api.js";
import { emptyRow, skeletonRows } from "./ui.js";

// Valor crudo de una celda: sirve para ordenar y para buscar. `render`
// devuelve HTML y no sirve para ninguna de las dos cosas.
const cellValue = (c, row) => (c.value ? c.value(row) : row[c.key]);

/**
 * Filtrar -> ordenar -> paginar. Pura a proposito: es la unica logica no
 * trivial de este archivo, y separada del DOM se puede testear sin
 * inventar medio navegador (ver tests/ui-table.test.mjs).
 * Devuelve { slice, all, pages, page } — `page` puede venir corregida si
 * la que pedia el estado se quedo sin filas (filtrar hasta que la pagina
 * 4 deja de existir es el caso normal, no un error).
 */
export function dtView(columns, state, pageSize) {
  let all = state.rows;
  const q = (state.query || "").trim().toLowerCase();
  if (q) {
    all = all.filter((r) => columns.some((c) => {
      const v = cellValue(c, r);
      return v != null && String(v).toLowerCase().includes(q);
    }));
  }
  if (state.sortKey != null) {
    const c = columns[state.sortKey];
    const dir = state.sortDir === "desc" ? -1 : 1;
    // slice(): ordenar in-place mutaria el array del caller, que en
    // varios tabs es el mismo objeto que guarda el poller.
    all = all.slice().sort((a, b) => {
      const va = cellValue(c, a), vb = cellValue(c, b);
      // Los vacios van al final en LAS DOS direcciones: invertir el
      // orden no deberia subir a la cima las filas que no tienen dato.
      if (va == null || va === "") return vb == null || vb === "" ? 0 : 1;
      if (vb == null || vb === "") return -1;
      return dir * (typeof va === "number" && typeof vb === "number"
        ? va - vb
        : String(va).localeCompare(String(vb), "es", { numeric: true }));
    });
  }
  const pages = pageSize ? Math.max(1, Math.ceil(all.length / pageSize)) : 1;
  const page = Math.min(Math.max(0, state.page || 0), pages - 1);
  const slice = pageSize
    ? all.slice(page * pageSize, (page + 1) * pageSize)
    : all;
  return { slice, all, pages, page };
}

/**
 * dataTable(mount, opts) -> controlador
 *
 * columns  [{ key, label, sortable?, className?, thClass?,
 *             render?(row) -> HTML, value?(row) -> valor para ordenar
 *                                                  y buscar }]
 * rows     filas iniciales (o [] y despues setRows()).
 * pageSize 0 = sin paginar. Default 25.
 * empty    string, { title, sub, action } (ver ui.emptyState), o una
 *          funcion que devuelva cualquiera de las dos: se evalua en cada
 *          pintada, asi el caller puede contar otra cosa cuando la carga
 *          falla ("no se pudo cargar" != "no hay nada").
 * search   false para ocultar el buscador.
 * rowAttrs (row) -> atributos extra del <tr> (clases, data-*).
 * sort     { key, dir } inicial; dir "asc" | "desc".
 * toolbar  HTML extra que va a la izquierda del buscador.
 *
 * Devuelve { setRows, setLoading, paint, el, state }.
 * El caller sigue wireando sus botones por delegacion sobre `el`, que es
 * lo que ya hacen todos los tabs.
 */
export function dataTable(mount, opts) {
  const el = typeof mount === "string" ? document.querySelector(mount) : mount;
  const {
    columns, rows = [], pageSize = 25, empty = "sin datos",
    search = true, rowAttrs = null, sort = null,
    searchPlaceholder = "filtrar…", toolbar = "",
  } = opts;

  const state = {
    rows, query: "", page: 0,
    // `??` y no `||`: la columna 0 es un índice válido y con `||` se
    // caía a null, así que la primera columna nunca podía ser el orden
    // inicial — justo la que uno quiere ordenar por default.
    sortKey: sort?.key ?? null,
    sortDir: sort?.dir || "asc",
    loading: false,
  };

  el.innerHTML = `
    <div class="dt-toolbar">
      ${toolbar}
      ${search ? `<input type="search" class="input dt-search w-56"
                    placeholder="${escape(searchPlaceholder)}"
                    aria-label="${escape(searchPlaceholder)}">` : ""}
      <span class="dt-count" aria-live="polite"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>${columns.map((c, i) => {
          const sortable = c.sortable !== false;
          return `<th class="th ${c.thClass || ""}${sortable ? " th-sort" : ""}"
            ${sortable ? `data-col="${i}" tabindex="0" role="button"` : ""}
            >${escape(c.label)}</th>`;
        }).join("")}</tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="dt-footer" hidden></div>`;

  const $tbody = el.querySelector("tbody");
  const $count = el.querySelector(".dt-count");
  const $footer = el.querySelector(".dt-footer");
  const $search = el.querySelector(".dt-search");

  function paint() {
    if (state.loading) {
      $tbody.innerHTML = skeletonRows(columns.length);
      $count.textContent = "";
      $footer.hidden = true;
      return;
    }
    const { slice, all, pages, page } = dtView(columns, state, pageSize);
    state.page = page;

    $tbody.innerHTML = slice.length
      ? slice.map((r) => `<tr ${rowAttrs ? rowAttrs(r) : ""}>${
          columns.map((c) => `<td class="${c.className || ""}">${
            c.render ? c.render(r) : escape(String(cellValue(c, r) ?? "—"))
          }</td>`).join("")}</tr>`).join("")
      : emptyRow(columns.length,
          // Filtrar hasta quedarse sin nada no es lo mismo que no tener
          // datos: el vacio del caller no aplica y confunde.
          state.query ? `sin resultados para "${state.query}"`
                      : (typeof empty === "function" ? empty() : empty));

    $count.textContent = all.length === state.rows.length
      ? `${all.length}`
      : `${all.length} de ${state.rows.length}`;

    el.querySelectorAll(".th-sort").forEach((th) => {
      const i = Number(th.dataset.col);
      th.setAttribute("aria-sort", i === state.sortKey
        ? (state.sortDir === "desc" ? "descending" : "ascending") : "none");
    });

    $footer.hidden = pages <= 1;
    if (pages > 1) {
      $footer.innerHTML = `
        <span>página ${state.page + 1} de ${pages}</span>
        <span class="dt-pages">
          <button class="btn btn-xs" data-page="prev"
            ${state.page === 0 ? "disabled" : ""}>anterior</button>
          <button class="btn btn-xs" data-page="next"
            ${state.page >= pages - 1 ? "disabled" : ""}>siguiente</button>
        </span>`;
    }
  }

  el.querySelector("thead").addEventListener("click", (e) => _sortFrom(e.target));
  el.querySelector("thead").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); _sortFrom(e.target); }
  });
  function _sortFrom(target) {
    const th = target.closest?.(".th-sort");
    if (!th) return;
    const i = Number(th.dataset.col);
    if (state.sortKey === i) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = i;
      state.sortDir = "asc";
    }
    state.page = 0;
    paint();
  }

  $search?.addEventListener("input", (e) => {
    state.query = e.target.value;
    state.page = 0;
    paint();
  });

  $footer.addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (!b) return;
    state.page += b.dataset.page === "next" ? 1 : -1;
    paint();
  });

  paint();

  return {
    el, state, paint,
    // Mantiene orden, filtro y pagina — un refresh del poller no puede
    // devolver al usuario a la pagina 1 sin que haya tocado nada.
    setRows(next) { state.rows = next; state.loading = false; paint(); },
    setLoading(v = true) { state.loading = v; paint(); },
  };
}
