// Tab Modelos (2026-08-18): administra la tabla `models`.
//
// El catálogo dejó de ser una constante en experts.py y pasó a SQL para
// poder prender/apagar, cargar tarifas y sumar un provider pago sin
// tocar código. Esta pantalla es el "sin tocar código".
//
// Por qué arranca filtrado a los prendidos: importar el catálogo de
// NVIDIA mete >100 filas apagadas. Mostrarlas todas de entrada convierte
// la pantalla en un listado inútil donde no encontrás los cuatro que
// usás.

import { $, $$, on, api, escape, _dbg } from "./api.js";
import { toast, confirmModal } from "./ui.js";
import { dtView } from "./ui-table.js";

let cache = [];
let verTodos = false;

// Estado de orden/paginación local a este tab. Vive afuera de `render`
// para que un refresh del poller no le robe el orden ni la página al
// usuario. El sortKey es el índice en `columns` — usar el índice y no
// la `key` evita un lookup por fila y mantiene la convención de
// `dtView()`. Default: orden por Spec asc, sin paginar hasta tener más
// de `pageSize` filas (catálogo chico o catálogo de NVIDIA).
const _PAGE_SIZE = 25;
const _state = {
  sortKey: 1,    // Spec
  sortDir: "asc",
  page: 0,
};

// `value` se usa para ordenar y para el empty state — `render` devuelve
// el HTML y no sirve para ninguna de las dos. Sin `value`, `dtView` cae
// al `row[key]` y el costo numérico ordena como string ("10" < "2").
const _columns = [
  { key: "enabled",   label: "On" },
  { key: "spec",      label: "Spec",     value: (m) => m.spec },
  { key: "label",     label: "Nombre",   value: (m) => m.label || "" },
  { key: "vision",    label: "Visión",   value: (m) => m.vision },
  { key: "cost_in",   label: "In",       value: (m) => m.cost_in },
  { key: "cost_out",  label: "Out",      value: (m) => m.cost_out },
  { key: "api_key",   label: "Key",      value: (m) => m.api_key_hint || m.api_key_env || "" },
  { key: "_actions",  label: "",         sortable: false },
];

// vision es tri-estado y la UI tiene que mostrarlo así: "sin medir" NO
// es lo mismo que "no ve". Un modelo recién importado no fue probado por
// nadie, y decir que no ve sería inventar el dato.
function chipVision(v) {
  if (v === 1) return `<span class="badge ok" title="medido: ve imágenes">🖼 ve</span>`;
  if (v === 0) return `<span class="badge err" title="medido: NO ve imágenes">no ve</span>`;
  return `<span class="badge" title="nadie lo probó; se deja pasar">sin medir</span>`;
}

function fmtCosto(v) {
  if (v === null || v === undefined) return "—";
  return Number(v) === 0 ? "gratis" : `$${Number(v).toFixed(2)}`;
}

export async function loadModels() {
  const cuerpo = $("#models-tbody");
  if (!cuerpo) return;
  try {
    const r = await api(`models${verTodos ? "?all=1" : ""}`);
    cache = r.models || [];
    _lastDefault = r.default;
    render(r.default);
  } catch (e) {
    _dbg("loadModels ERROR", e.message);
    _lastDefault = null;
    cuerpo.innerHTML =
      `<tr><td colspan="8" class="p-3 text-xs text-red-400">error: ${
        escape(e.message)}</td></tr>`;
  }
}

function render(porDefecto) {
  const cuerpo = $("#models-tbody");
  if (!cuerpo) return;

  // Filtro + orden + paginación via dtView (la misma lógica que usa
  // dataTable, sin pisar el shell HTML existente: el toolbar custom
  // con "ver todos" + "Nuevo" + contador + filtro vive afuera del
  // <tbody> y queda intacto). El query del input sigue siendo el mismo
  // input #models-filter que ya estaba; se lo pasamos al estado.
  const q = ($("#models-filter")?.value || "").trim();
  // dtView sólo pagina si pageSize > 0 y hay más filas que pageSize.
  // Con el catálogo chico (≤25) no aparece paginación: no estorba.
  const { slice, all, pages, page } = dtView(_columns, {
    rows: cache,
    query: q,
    sortKey: _state.sortKey,
    sortDir: _state.sortDir,
    page: _state.page,
  }, cache.length > _PAGE_SIZE ? _PAGE_SIZE : 0);
  _state.page = page;

  const contador = $("#models-count");
  if (contador) {
    // 3 de 5 prendidos · pág 2/4 — el contador del toolbar refleja
    // el filtro y la paginación. "prendidos" desaparece cuando el
    // usuario prendió "ver todos" (la lista ya no está filtrada).
    const pagesTxt = pages > 1 ? ` · pág ${page + 1}/${pages}` : "";
    const scopeTxt = !verTodos && all.length !== cache.length
      ? `${all.length} de ${cache.length} prendidos`
      : `${all.length}${verTodos ? "" : " prendidos"}`;
    contador.textContent = `${scopeTxt}${pagesTxt}`;
  }

  if (!all.length) {
    cuerpo.innerHTML =
      `<tr><td colspan="8" class="empty">${
        q ? `sin resultados para "${escape(q)}"`
           : (verTodos ? "no hay modelos cargados"
                       : "no hay modelos prendidos — probá 'ver todos'")}</td></tr>`;
    _renderPager(pages, page);
    return;
  }
  cuerpo.innerHTML = slice.map((m) => `
    <tr data-spec="${escape(m.spec)}">
      <td>
        <input type="checkbox" class="models-enabled" ${m.enabled ? "checked" : ""}
               title="Prendido = aparece en el selector del chat">
      </td>
      <td class="font-mono text-[11px]">${escape(m.spec)}${
        m.spec === porDefecto
          ? ` <span class="badge" title="FOURBIS_MODEL">default</span>` : ""}</td>
      <td>${escape(m.label || "")}</td>
      <td>${chipVision(m.vision)}</td>
      <td class="text-right">${fmtCosto(m.cost_in)}</td>
      <td class="text-right">${fmtCosto(m.cost_out)}</td>
      <td class="text-[11px]">${
        m.api_key_set
          ? `<span title="cargada en la tabla">🔑 ${escape(m.api_key_hint)}</span>`
          : m.api_key_env
            ? `<span class="text-zinc-500" title="sale del .env">${
                escape(m.api_key_env)}</span>`
            : `<span class="text-red-400">sin key</span>`}</td>
      <td class="text-right whitespace-nowrap">
        <button class="btn btn-xs models-test"
                title="Hace un turno real de 8 tokens y muestra qué contestó el proveedor">Probar</button>
        <button class="btn btn-xs models-edit">Editar</button>
        <button class="btn btn-xs models-del" title="Borrar del catálogo"
                aria-label="borrar del catálogo"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
      </td>
    </tr>`).join("");

  // Marcar la columna ordenada en el thead. aria-sort vive en <th> y
  // lo lee el screen reader. La flecha ↕/↑/↓ la pone CSS
  // (.th-sort[aria-sort=...]) — sin tocar clases desde JS.
  const ths = document.querySelectorAll("#tab-models thead .th");
  ths.forEach((th, i) => {
    if (!_columns[i] || _columns[i].sortable === false) {
      th.removeAttribute("aria-sort");
      return;
    }
    if (i === _state.sortKey) {
      th.setAttribute("aria-sort",
        _state.sortDir === "desc" ? "descending" : "ascending");
    } else {
      th.setAttribute("aria-sort", "none");
    }
  });

  $$(".models-enabled").forEach((c) => c.addEventListener("change", async () => {
    const spec = c.closest("tr").dataset.spec;
    await guardar(spec, { enabled: c.checked }, `${spec} ${
      c.checked ? "prendido" : "apagado"}`);
  }));
  $$(".models-edit").forEach((b) => b.addEventListener("click", () =>
    abrirEditor(b.closest("tr").dataset.spec)));
  $$(".models-del").forEach((b) => b.addEventListener("click", () =>
    borrar(b.closest("tr").dataset.spec)));
  $$(".models-test").forEach((b) => b.addEventListener("click", () =>
    probar(b, b.closest("tr").dataset.spec)));

  _renderPager(pages, page);
}

// Paginación chiquita al final de la tabla. El shell HTML no tiene un
// footer dedicado, así que va como última fila del <tbody> con colspan
// y estilos inline (sin clase nueva: el test del bundle detecta
// cualquier clase que no esté en admin.css y falla). Si pages ≤ 1
// no se renderiza.
function _renderPager(pages, page) {
  const cuerpo = $("#models-tbody");
  if (!cuerpo || pages <= 1) return;
  const tr = document.createElement("tr");
  // .dt-footer y .dt-pages ya existen (las usa dataTable); el row
  // necesita ser <tr> y el inner va en un <td> con colspan. Sin clase
  // nueva en el <tr>: el CSS de .dt-footer igual aplica dentro.
  tr.innerHTML = `
    <td colspan="8" style="border-top:1px solid var(--c-zinc-800,#27272a);background:transparent">
      <div class="dt-footer">
        <span>página ${page + 1} de ${pages}</span>
        <span class="dt-pages">
          <button class="btn btn-xs" data-page="prev"
            ${page === 0 ? "disabled" : ""}>anterior</button>
          <button class="btn btn-xs" data-page="next"
            ${page >= pages - 1 ? "disabled" : ""}>siguiente</button>
        </span>
      </div>
    </td>`;
  cuerpo.appendChild(tr);
  tr.addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (!b || b.disabled) return;
    _state.page += b.dataset.page === "next" ? 1 : -1;
    render(_lastDefault);
  });
}

// `porDefecto` viene del backend y se necesita para el badge "default"
// en cada fila. Como `render` lo recibe como argumento, lo guardamos
// acá para que `_renderPager` (que también llama `render`) no lo pierda.
// Ponytail: trusco local — la alternativa era pasar `porDefecto` por
// estado global y este cambio era más chico. Si este tab suma más
// callers de `render`, mover a `_state`.
let _lastDefault = null;

// El error del proveedor va COMPLETO y sin traducir: el caso que motivó
// este botón fue una key válida contra una cuenta sin créditos, y la
// única frase útil —con el link para arreglarlo— la escribía xAI. Un
// "no se pudo conectar" nuestro habría tapado justo eso.
async function probar(boton, spec) {
  const antes = boton.textContent;
  boton.disabled = true;
  boton.textContent = "…";
  try {
    // 50s a propósito: el backend corta a los 45 y devuelve "no contestó
    // en 45s". Con el default de 15 el fetch abortaba antes y el usuario
    // leía "el server no respondió", que culpa al relay de un proveedor
    // lento. El timeout de afuera siempre va por encima del de adentro.
    const r = await api(`models/${encodeURIComponent(spec)}/test`,
                        { method: "POST" }, 50_000);
    if (r.ok) {
      const t = [r.tokens_in, r.tokens_out].every((n) => n != null)
        ? `, ${r.tokens_in}→${r.tokens_out} tokens` : "";
      toast(`${spec} responde ✓ (${r.ms} ms${t})`, "ok");
    } else {
      const cabeza = r.status ? `HTTP ${r.status}` : (r.kind || "falló");
      toast(`${spec} — ${cabeza}\n${r.error}`, "err", 15000);
    }
  } catch (e) {
    toast(`No pude probar ${spec}: ${e.message}`, "err", 7000);
  } finally {
    boton.disabled = false;
    boton.textContent = antes;
  }
}

async function guardar(spec, campos, mensajeOk) {
  try {
    await api(`models/${encodeURIComponent(spec)}`,
              { method: "PUT", body: JSON.stringify(campos) });
    toast(mensajeOk || `${spec} guardado ✓`, "ok");
    await loadModels();
    await asegurarVisible(spec);
  } catch (e) {
    toast(`No pude guardar ${spec}: ${e.message}`, "err", 7000);
    await loadModels();          // repinta el estado real, no el optimista
  }
}

async function borrar(spec) {
  if (!await confirmModal({
    title: "Borrar del catálogo",
    // Sin HTML: confirmModal escapea el `body` cuando es string, así que
    // un <code> acá se veía con los tags puestos. Y la opción se llama
    // `confirmText` — con `ok` el botón decía "Confirmar".
    body: `Se borra la fila de ${spec}. Los chats que ya corrieron con él `
        + `no se tocan. Si es un modelo del seed, vuelve a aparecer al `
        + `reiniciar el relay.`,
    confirmText: "Borrar", danger: true,
  })) return;
  try {
    await api(`models/${encodeURIComponent(spec)}`, { method: "DELETE" });
    toast(`${spec} borrado`, "ok");
    await loadModels();
  } catch (e) {
    toast(`No pude borrar: ${e.message}`, "err");
  }
}

// El modelo que acabás de guardar tiene que quedar a la vista (2026-08-26).
//
// La tabla `models` crea con `enabled DEFAULT 0` y esta pantalla arranca
// filtrada a los prendidos: las dos decisiones son correctas por
// separado y juntas hacen que TODO modelo nuevo desaparezca justo
// después de guardarlo. El síntoma no es "está apagado" —eso se
// entendería— sino "no está", que se lee como que no se guardó. Pasó de
// verdad con la fila de grok.
//
// Vale igual al apagar uno existente desde el checkbox: la fila se
// esfuma bajo el mouse. En los dos casos la respuesta es la misma —
// mostrar los apagados— y se dice, para que el filtro no cambie a
// espaldas del operador.
async function asegurarVisible(spec) {
  if (verTodos || cache.some((m) => m.spec === spec)) return;
  verTodos = true;
  const chk = $("#models-all");
  if (chk) chk.checked = true;
  await loadModels();
  toast(`${spec} está apagado — prendí "ver todos" para que lo veas.`, "warn");
}

function abrirEditor(spec) {
  const m = cache.find((x) => x.spec === spec)
    || { spec: "", label: "", base_url: "", api_key_env: "", vision: null,
         cost_in: null, cost_out: null, cost_cache_in: null,
         context_tokens: null, context_warn_tokens: null,
         notes: "", enabled: false };
  const nuevo = !spec;
  const dlg = $("#models-editor");
  if (!dlg) return;
  $("#models-ed-title").textContent = nuevo ? "Nuevo modelo" : m.spec;
  $("#models-ed-spec").value = m.spec;
  $("#models-ed-spec").disabled = !nuevo;   // el spec es la PK
  $("#models-ed-label").value = m.label || "";
  $("#models-ed-url").value = m.base_url || "";
  $("#models-ed-keyenv").value = m.api_key_env || "";
  $("#models-ed-key").value = "";
  $("#models-ed-key").placeholder = m.api_key_set
    ? `hay una cargada (${m.api_key_hint}) — vacío la deja como está`
    : "en blanco = usa la env var de al lado";
  $("#models-ed-vision").value =
    m.vision === 1 ? "1" : m.vision === 0 ? "0" : "";
  $("#models-ed-cin").value = m.cost_in ?? "";
  $("#models-ed-cout").value = m.cost_out ?? "";
  $("#models-ed-ccache").value = m.cost_cache_in ?? "";
  $("#models-ed-ctx").value = m.context_tokens ?? "";
  $("#models-ed-ctxwarn").value = m.context_warn_tokens ?? "";
  $("#models-ed-notes").value = m.notes || "";
  dlg.classList.add("open");
}

async function guardarEditor() {
  const spec = $("#models-ed-spec").value.trim();
  if (!spec.includes(":")) {
    toast("El spec va como provider:modelo (ej: deepseek:deepseek-chat)", "err");
    return;
  }
  const num = (sel) => {
    const v = $(sel).value.trim();
    return v === "" ? null : Number(v);
  };
  const vis = $("#models-ed-vision").value;
  const campos = {
    label: $("#models-ed-label").value.trim(),
    provider: spec.split(":")[0],
    base_url: $("#models-ed-url").value.trim() || null,
    api_key_env: $("#models-ed-keyenv").value.trim() || null,
    vision: vis === "" ? null : Number(vis),
    cost_in: num("#models-ed-cin"),
    cost_out: num("#models-ed-cout"),
    // Vacío manda null a propósito: "no sé la tarifa de caché" es un
    // estado real y distinto de "es 0". Con null, `cost_usd` cobra esos
    // tokens a `cost_in` — caro de más, nunca de menos.
    cost_cache_in: num("#models-ed-ccache"),
    context_tokens: num("#models-ed-ctx"),
    context_warn_tokens: num("#models-ed-ctxwarn"),
    notes: $("#models-ed-notes").value.trim(),
  };
  // Vacío NO borra la key: el editor no la muestra, así que mandar ""
  // dejaría sin key a cualquiera que solo vino a tocar la tarifa.
  const key = $("#models-ed-key").value.trim();
  if (key) campos.api_key = key;
  $("#models-editor")?.classList.remove("open");
  await guardar(spec, campos);
}

export function initModels() {
  on("#models-refresh", "click", loadModels);
  on("#models-filter", "input", () => { _state.page = 0; render(_lastDefault); });
  on("#models-new", "click", () => abrirEditor(""));
  on("#models-ed-save", "click", guardarEditor);
  on("#models-ed-cancel", "click", () =>
    $("#models-editor")?.classList.remove("open"));
  on("#models-all", "change", () => {
    verTodos = $("#models-all").checked;
    _state.page = 0;
    loadModels();
  });

  // Orden por <th>: click y teclado (Enter/Espacio), igual que
  // ui-table.js#_sortFrom. El thead es estático en index.html (esta
  // tabla ya tiene su propio toolbar con "ver todos"/filtro/Nuevo y su
  // propio pager de <tbody>), así que no migra a dataTable() — se porta
  // a mano el mismo patrón de accesibilidad.
  const thead = document.querySelector("#tab-models thead");
  if (thead && !thead.dataset.sortWired) {
    thead.dataset.sortWired = "1";
    const sortFrom = (target) => {
      const th = target.closest?.(".th-sort");
      if (!th) return;
      const i = Number(th.dataset.col);
      const col = _columns[i];
      if (!col || col.sortable === false) return;
      if (_state.sortKey === i) {
        _state.sortDir = _state.sortDir === "asc" ? "desc" : "asc";
      } else {
        _state.sortKey = i;
        _state.sortDir = "asc";
      }
      _state.page = 0;
      render(_lastDefault);
    };
    thead.addEventListener("click", (e) => sortFrom(e.target));
    thead.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortFrom(e.target); }
    });
  }
}
