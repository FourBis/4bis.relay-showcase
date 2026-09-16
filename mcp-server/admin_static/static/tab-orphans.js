// Tab Huérfanos cbm: repos en el knowledge graph sin experto en la DB.

import { $, api, escape } from "./api.js";
import { toast, confirmModal } from "./ui.js";
import { dataTable } from "./ui-table.js";
import { refreshStatus } from "./tab-status.js";
import { loadProjects } from "./tab-projects.js";

let _orphans = [];
let _tablaHuérfanos = null;
let _errorHuérfanos = "";

export async function loadOrphans() {
  if (!_tablaHuérfanos) {
    _tablaHuérfanos = dataTable($("#orphans-table"), {
      searchPlaceholder: "buscar huérfanos…",
      sort: { key: 0, dir: "asc" },
      columns: [
        { key: "root_path", label: "path", className: "path",
          value: (o) => o.root_path },
        { key: "suggested_slug", label: "sugerido",
          render: (o) => `<code>${escape(o.suggested_slug)}</code>`,
          value: (o) => o.suggested_slug },
        { key: "has_git", label: "git", className: "text-center",
          render: (o) => o.has_git
            ? '<span class="text-emerald-400" title="repo git">✓</span>'
            : '<span class="text-zinc-600" title="sin git">—</span>',
          value: (o) => (o.has_git ? 1 : 0) },
        { key: "nodes", label: "nodes", className: "tabular-nums",
          render: (o) => (o.nodes ?? 0).toLocaleString(),
          value: (o) => o.nodes ?? 0 },
        { key: "edges", label: "edges", className: "tabular-nums",
          render: (o) => (o.edges ?? 0).toLocaleString(),
          value: (o) => o.edges ?? 0 },
        { key: "_actions", label: "", sortable: false, className: "row-actions",
          render: (o) => `
            <div class="cell-actions">
              <button class="btn btn-xs btn-primary create-btn"
                data-cbm="${escape(o.cbm_name)}"
                data-slug="${escape(o.suggested_slug)}"
                data-name="${escape((o.root_path || "").split(/[\\/]/).pop() || "")}">
                + Crear experto
              </button>
              <button class="btn btn-xs ignore-btn" data-cbm="${escape(o.cbm_name)}"
                title="ocultar de la lista (persistente)"
                aria-label="ocultar de la lista"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"/><path d="M9.9 4.2A10 10 0 0 1 12 4c4.5 0 8.3 2.9 10 7a10 10 0 0 1-2.2 3.2M6.2 6.2A10 10 0 0 0 2 11c1.7 4.1 5.5 7 10 7 1.6 0 3.1-.3 4.5-.9"/></svg></button>
              <button class="btn btn-xs copy-path-btn" data-path="${escape(o.root_path)}"
                title="copia el path al portapapeles">Copiar path</button>
            </div>` },
      ],
      empty: () => (_errorHuérfanos
        ? { title: "No se pudo consultar cbm", sub: _errorHuérfanos,
            action: '<button class="btn" data-orphans-retry>Reintentar</button>' }
        : "Nada huérfano — todo lo de cbm ya tiene experto en la DB."),
      // Ponytail: dataTable viene con su propio .dt-search y .dt-count;
      // los botones "Refrescar" y "Crear todos los que tienen git" se
      // cuelgan vía `toolbar`. El summary contextual ("X huérfanos de Y
      // indexados") ya no entra acá porque pisaría al .dt-count; vive
      // en el <header> del panel.
      toolbar: `
        <button id="orphans-refresh" class="btn" aria-label="refrescar">
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true"><path d="M20 11a8 8 0 1 0-.7 3.3"/>
            <path d="M20.5 4.5v6.5h-6"/></svg>
          Refrescar
        </button>
        <button id="orphans-create-all-with-git" class="btn">
          ⚡ Crear todos los que tienen git
        </button>`,
    });
    _wireHuérfanos();
  }

  _errorHuérfanos = "";
  _tablaHuérfanos.setLoading(true);
  try {
    const r = await api("cbm/orphans");
    _orphans = r.orphans || [];
    _tablaHuérfanos.setRows(_orphans);
    const summary = $("#orphans-summary");
    if (summary) {
      summary.textContent = `${r.count ?? _orphans.length} huérfanos de ${
        r.indexed_total ?? "?"} indexados (${r.with_expert ?? "?"} ya en DB)`;
    }
  } catch (e) {
    // Hasta el 1/9/2026: fallaba /cbm/orphans y la tabla se quedaba
    // muda. dataTable() pinta el empty() con botón "Reintentar".
    _errorHuérfanos = e.message;
    _orphans = [];
    _tablaHuérfanos.setRows([]);
    const summary = $("#orphans-summary");
    if (summary) summary.textContent = "";
  }
}

// Delegación sobre el wrapper: dataTable() repinta el <tbody> y el
// toolbar en cada sort/page/filter/búsqueda, así que los listeners
// atados a los <tr>/<button> viejos quedan colgados. Atados al
// wrapper, capturan los botones nuevos sin rewirear.
//
// También wirea los botones del toolbar (#orphans-refresh,
// #orphans-create-all-with-git) por delegación — antes se hacía con
// onClick() en initOrphans, pero ese init corre en el boot y los
// botones no existen hasta el primer loadOrphans() (lazy mount del
// dataTable). Acá, que se ejecuta justo después de que el shell está
// en el DOM, el wireado siempre pega.
function _wireHuérfanos() {
  $("#orphans-table").addEventListener("click", async (e) => {
    if (e.target.closest("[data-orphans-retry]")) { loadOrphans(); return; }

    if (e.target.closest("#orphans-refresh")) {
      loadOrphans();
      loadIgnored();
      return;
    }
    if (e.target.closest("#orphans-create-all-with-git")) {
      createAllWithGit();
      return;
    }

    const createBtn = e.target.closest(".create-btn");
    if (createBtn) {
      // createFromCbm espera `e.target` con .dataset. Lo simulamos
      // pasándole el botón como target.
      createFromCbm({ target: createBtn });
      return;
    }

    const ignoreBtn = e.target.closest(".ignore-btn");
    if (ignoreBtn) {
      const cbm = ignoreBtn.dataset.cbm;
      if (!await confirmModal({
        title: "Ocultar huérfano",
        body: "¿Ocultar este huérfano de la lista?\n\n"
              + "Queda guardado en ignored_orphans. "
              + "Puedes traerlo de vuelta desde el panel de abajo.",
        confirmText: "Ocultar",
      })) return;
      try {
        await api("cbm/orphans/ignore", {
          method: "POST",
          body: JSON.stringify({ cbm_name: cbm }),
        });
        loadOrphans();
        loadIgnored();
      } catch (err) { toast("Error: " + err.message, "err"); }
      return;
    }

    const copyBtn = e.target.closest(".copy-path-btn");
    if (copyBtn) {
      const p = copyBtn.dataset.path;
      // Antes había un botón "VS Code" que mostraba un toast con
      // instrucciones para correr `code "…"` a mano. La auditoría lo
      // marcó como confuso: si VS Code no se abre desde acá, el botón
      // no debería decir "VS Code". Copiar el path es lo único que
      // podemos hacer confiable desde el browser sin un endpoint del
      // relay que abra el shell.
      try {
        await navigator.clipboard.writeText(p);
        toast("Path copiado al portapapeles ✓", "ok");
      } catch (_) {
        // clipboard API requiere HTTPS o localhost; si falla, fallback
        // al toast con el path para que el usuario lo copie a mano.
        toast(p, "info", 8000);
      }
      return;
    }
  });
}

async function createFromCbm(e) {
  const btn = e.target;
  const cbm_name = btn.dataset.cbm;
  const slug = btn.dataset.slug;
  const name = btn.dataset.name;
  btn.disabled = true;
  btn.textContent = "creando...";
  try {
    const r = await api("projects/from-cbm", {
      method: "POST",
      body: JSON.stringify({ cbm_name, slug, name }),
    });
    if (r.error) {
      toast("Error: " + r.error, "err");
      btn.disabled = false;
      btn.textContent = "+ Crear experto";
      return;
    }
    btn.textContent = "✓ creado";
    loadOrphans();
    loadProjects();
    refreshStatus();
  } catch (e) {
    toast("Error: " + e.message, "err");
    btn.disabled = false;
    btn.textContent = "+ Crear experto";
  }
}

async function createAllWithGit() {
  const withGit = _orphans.filter((o) => o.has_git);
  if (!withGit.length) {
    toast("No hay huérfanos con git.", "warn");
    return;
  }
  if (!await confirmModal({
    title: `Crear ${withGit.length} expertos`,
    body: "Vas a crear expertos desde todos los huérfanos con git del knowledge graph.\n\n"
          + "Cada uno recibe system_prompt template + native_tools=[cbm], "
          + "sin ningún MCP asociado (filesystem/shell ya son nativos).\n\n¿Seguimos?",
    confirmText: "Crear todos",
  })) return;
  let ok = 0, fail = 0;
  for (const o of withGit) {
    try {
      const r = await api("projects/from-cbm", {
        method: "POST",
        body: JSON.stringify({
          cbm_name: o.cbm_name, slug: o.suggested_slug, name: o.suggested_slug,
        }),
      });
      if (r.error) fail++;
      else ok++;
    } catch (_) { fail++; }
  }
  toast(`Listo: ${ok} OK, ${fail} FAIL`, fail ? "warn" : "ok");
  loadOrphans();
  loadProjects();
  refreshStatus();
}

let _tablaIgnorados = null;
let _errorIgnorados = "";

export async function loadIgnored() {
  if (!_tablaIgnorados) {
    _tablaIgnorados = dataTable($("#ignored-table"), {
      searchPlaceholder: "buscar en ignorados…",
      sort: { key: 0, dir: "asc" },
      columns: [
        { key: "cbm_name", label: "cbm_name", className: "path" },
        { key: "reason", label: "Razón",
          render: (i) => escape(i.reason || "—") },
        { key: "cbm_name", label: "", sortable: false, className: "cell-actions",
          render: (i) => `<div><button class="btn btn-xs unignore-btn"
            data-cbm="${escape(i.cbm_name)}">traer de vuelta</button></div>` },
      ],
      empty: () => (_errorIgnorados
        ? { title: "No se pudo leer la lista de ignorados", sub: _errorIgnorados,
            action: `<button class="btn" data-ignored-retry>Reintentar</button>` }
        : "nada ignorado"),
    });
    _wireIgnorados();
  }
  _errorIgnorados = "";
  _tablaIgnorados.setLoading(true);
  try {
    const r = await api("cbm/orphans/ignored");
    _tablaIgnorados.setRows(r.ignored || []);
  } catch (e) {
    // Antes era `console.error(e)` a secas: la tabla se quedaba muda y
    // el usuario no distinguía "no hay nada ignorado" de "falló".
    _errorIgnorados = e.message;
    _tablaIgnorados.setRows([]);
  }
}

// Delegación: dataTable repinta el tbody al ordenar o filtrar.
function _wireIgnorados() {
  $("#ignored-table").addEventListener("click", async (e) => {
    if (e.target.closest("[data-ignored-retry]")) { loadIgnored(); return; }
    const b = e.target.closest(".unignore-btn");
    if (!b) return;
    try {
      await api(`cbm/orphans/ignore?cbm_name=${encodeURIComponent(b.dataset.cbm)}`,
                { method: "DELETE" });
      loadIgnored();
      loadOrphans();
    } catch (err) { toast("Error: " + err.message, "err"); }
  });
}

export function initOrphans() {
  // onClick() sobre #orphans-refresh / #orphans-create-all-with-git ya
  // no va acá: esos botones no existen en el DOM hasta el primer
  // loadOrphans() (dataTable se monta lazy y reescribe el shell).
  // _wireHuérfanos() los ataja por delegación cuando el shell aparece.
}
