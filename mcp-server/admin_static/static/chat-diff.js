// Visor de diff de la rama de una conversación.
//
// Antes esto eran dos <pre> con TODO el diff pegado y coloreado a tres
// regex: servía para confirmar "algo cambió" y nada más. Un diff de 15
// archivos era un muro de 4000 líneas sin forma de saltar a uno, sin
// números de línea, y cortado por el cap en la mitad de un archivo.
//
// Acá está la forma en que un dev mira un diff:
//   - la LISTA de archivos primero (una llamada, `?view=files`), con
//     estado, contadores y de dónde viene el cambio;
//   - el diff de UN archivo a demanda (`?path=…`), con números de línea
//     de los dos lados, hunks colapsables y unificado ⇄ lado a lado;
//   - y las acciones que uno hace después de mirar: commit, push, PR,
//     merge, sync de la base, descartar un archivo.
//
// Módulo aparte de tab-chats.js (que ya tiene 2200 líneas) con los
// estilos INYECTADOS acá: admin.css es un bundle de Tailwind que se
// regenera con build-css.ps1 y un visor no justifica ese paso. Mismo
// criterio que chat-tools.js.
//
// Los paths NUNCA viajan en atributos HTML: las filas llevan `data-i`
// (índice en `S.rows`). Un archivo llamado `" onclick="` no es un caso
// hipotético cuando el que escribe los archivos es un LLM.

import { apiRoot, escape } from "./api.js";
import { toast, modalLoad, confirmModal } from "./ui.js";

// =====================================================================
// Parser de diff unificado
// =====================================================================
//
// La única parte con lógica de verdad del módulo, y por eso la única
// exportada aparte: la prueba chat-diff.test.mjs sin DOM.

// `escape` de api.js cubre &<> — alcanza para texto, no para atributos.
// Los paths los escribe un LLM, así que un `"` en un `title=` es un caso
// real, no teórico.
const attr = (s) => escape(s).replace(/"/g, "&quot;");

const RE_HUNK = /^@@+ (.+?) @@(.*)$/;
const RE_RANGO = /^-(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?$/;

/**
 * Diff unificado (`git diff`) → estructura navegable.
 *
 * Devuelve `[{path, oldPath, status, binary, meta[], hunks[]}]` donde
 * cada hunk es `{header, oldStart, newStart, lines[]}` y cada línea
 * `{type: " "|"+"|"-"|"\\", oldNo, newNo, text}`. `oldNo`/`newNo` son
 * null del lado donde la línea no existe — eso es lo que hace que se
 * puedan pintar las dos columnas de números.
 *
 * Los paths salen de `---`/`+++` cuando están (son inequívocos) y del
 * `diff --git` solo como fallback: un archivo con ` b/` en el nombre
 * rompe el parseo de esa línea y no hay forma de desambiguarla.
 */
export function parseUnifiedDiff(texto) {
  const files = [];
  let f = null;
  let h = null;
  let oldNo = 0;
  let newNo = 0;
  const nuevoArchivo = (path = "", oldPath = "") => {
    f = { path, oldPath, status: "M", binary: false, meta: [], hunks: [] };
    files.push(f);
    h = null;
    return f;
  };
  const lineas = String(texto || "").split("\n");
  // El diff termina en \n: el split deja un "" final que NO es una línea
  // del archivo. Si se cuela, cada archivo cierra con una fila de
  // contexto fantasma numerada. Se saca UNA sola — un "" en el medio sí
  // es una línea de contexto vacía a la que le comieron el espacio.
  if (lineas.length && lineas[lineas.length - 1] === "") lineas.pop();
  for (const linea of lineas) {
    if (linea.startsWith("diff --git ")) {
      const m = linea.match(/^diff --git a\/(.*) b\/(.*)$/);
      nuevoArchivo(m ? m[2] : "", m ? m[1] : "");
      continue;
    }
    if (!f) {
      // Diff sin cabecera `diff --git` (p.ej. `git diff` con
      // --no-prefix, o un fragmento pegado a mano). Igual se parsea.
      if (!linea.trim()) continue;
      nuevoArchivo();
    }
    if (h === null && !linea.startsWith("@@")) {
      // Zona de cabecera del archivo: modos, índices, ---/+++.
      if (linea.startsWith("--- ")) {
        const p = despegar(linea.slice(4));
        if (p) f.oldPath = p; else f.status = "A";
        continue;
      }
      if (linea.startsWith("+++ ")) {
        const p = despegar(linea.slice(4));
        if (p) f.path = p; else f.status = "D";
        continue;
      }
      if (linea.startsWith("new file")) f.status = "A";
      else if (linea.startsWith("deleted file")) f.status = "D";
      else if (linea.startsWith("rename ")) f.status = "R";
      else if (linea.startsWith("Binary files ")) f.binary = true;
      if (linea) f.meta.push(linea);
      continue;
    }
    const mh = linea.match(RE_HUNK);
    if (mh) {
      const mr = mh[1].match(RE_RANGO);
      oldNo = mr ? parseInt(mr[1], 10) : 0;
      newNo = mr ? parseInt(mr[3], 10) : 0;
      h = { header: linea, seccion: (mh[2] || "").trim(),
            oldStart: oldNo, newStart: newNo, lines: [] };
      f.hunks.push(h);
      continue;
    }
    if (!h) continue;
    const tipo = linea[0] || " ";
    const texto_ = linea.slice(1);
    if (tipo === "\\") {                       // "\ No newline at end of file"
      h.lines.push({ type: "\\", oldNo: null, newNo: null, text: linea });
    } else if (tipo === "+") {
      h.lines.push({ type: "+", oldNo: null, newNo: newNo++, text: texto_ });
    } else if (tipo === "-") {
      h.lines.push({ type: "-", oldNo: oldNo++, newNo: null, text: texto_ });
    } else {
      h.lines.push({ type: " ", oldNo: oldNo++, newNo: newNo++, text: texto_ });
    }
  }
  return files;
}

/** `a/src/x.py` → `src/x.py`; `/dev/null` → "" (archivo que no existe). */
function despegar(p) {
  let s = (p || "").trim().split("\t")[0];
  if (s === "/dev/null") return "";
  if (s.startsWith('"') && s.endsWith('"')) s = s.slice(1, -1);
  return s.replace(/^[ab]\//, "");
}

/**
 * Tramo cambiado entre dos líneas emparejadas: [iniA, finA, iniB, finB].
 *
 * Prefijo y sufijo comunes, sin LCS: en un cambio real (renombrar una
 * variable, tocar un argumento) marca exactamente el pedazo distinto, y
 * en el peor caso marca la línea entera — que es lo que se ve hoy.
 */
export function tramoCambiado(a, b) {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  let j = 0;
  while (j < n - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  return [i, a.length - j, i, b.length - j];
}

// =====================================================================
// Estado del visor
// =====================================================================

const S = {
  conv: null, branch: "", base: "", prUrl: null,
  mode: "all", ctx: 3, split: false, wrap: false,
  rows: [], sel: -1, filtro: "", seen: new Set(),
  cache: new Map(), meta: null,
};

const MODOS = [
  ["all", "Todo", "merge-base → working tree: lo que va a quedar en el PR"],
  ["committed", "Commiteado", "solo los commits de la rama"],
  ["pending", "Sin commitear", "solo lo que el experto no commiteó"],
];

const ICONO = { A: "＋", D: "－", M: "±", R: "→", C: "⧉", "?": "?" };
const TITULO_ESTADO = { A: "agregado", D: "borrado", M: "modificado",
                        R: "renombrado", C: "copiado", "?": "sin trackear" };

/** Abre el visor para la conversación `meta` (`{id, branch}`). */
export function openConvDiff(meta) {
  inyectarCss();
  const otraConv = S.conv !== (meta && meta.id);
  S.conv = meta && meta.id;
  S.meta = meta || {};
  S.sel = -1;
  S.filtro = "";
  S.cache.clear();
  // Las marcas de "revisado" son del humano: sobreviven al cambio de modo
  // (que re-abre el visor) y se van solo al cambiar de conversación.
  if (otraConv) S.seen.clear();
  modalLoad({
    id: "diff-modal",
    title: `Diff · ${(meta && meta.branch) || "rama"}`,
    loader: () => pedirLista(S.mode),
    render: renderShell,
    after: montar,
    watchdogDetail: `git puede tardar en repos grandes. Timeout duro a los
      60s; puedes cerrar con ✕ o Esc mientras tanto.`,
  });
}

function pedirLista(mode) {
  return apiRoot(
    `/conversations/${encodeURIComponent(S.conv)}/diff?view=files`
    + `&mode=${encodeURIComponent(mode)}`, null, 60_000);
}

// =====================================================================
// Render
// =====================================================================

function renderShell(r) {
  if (r.error && !r.exists) {
    const pr = r.pr_url
      ? `<p class="mt-2 text-sm">El diff completo está en el PR:
         <a class="text-sky-400 underline" href="${escape(r.pr_url)}"
            target="_blank" rel="noopener">${escape(r.pr_url)}</a></p>`
      : "";
    return `<div class="cdv-vacio"><p class="muted">${escape(r.error)}</p>${pr}</div>`;
  }
  S.branch = r.branch || "";
  S.base = r.base || "";
  S.prUrl = r.pr_url || null;
  cargarFilas(r);
  return `
    <div class="cdv">
      <div class="cdv-top">
        <div class="cdv-ident">
          <code class="cdv-rama" title="rama de la conversación">${escape(r.branch)}</code>
          <span class="cdv-flecha">→</span>
          <code class="cdv-base" title="base del PR">${escape(r.base || "?")}</code>
          ${chipsEstado(r)}
        </div>
        <div class="cdv-modos" role="tablist">${MODOS.map(([k, lbl, tip]) => `
          <button type="button" class="cdv-modo${S.mode === k ? " on" : ""}"
                  data-mode="${k}" title="${attr(tip)}">${lbl}</button>`).join("")}
        </div>
        <div class="cdv-acciones">
          <button type="button" class="cdv-btn" data-act="sync"
                  title="git fetch + fast-forward de la base">⇣ Sync base</button>
          <button type="button" class="cdv-btn" data-act="commit-abrir"
                  title="Commitear en la rama del hilo">✓ Commit</button>
          <button type="button" class="cdv-btn" data-act="push"
                  title="git push -u origin de la rama">↑ Push</button>
          <button type="button" class="cdv-btn" data-act="pr"
                  title="Abrir PR a develop sin cerrar el hilo">⑂ PR</button>
          <button type="button" class="cdv-btn danger" data-act="merge"
                  title="Mergear el PR (solo si va a develop)">⇥ Merge</button>
          <button type="button" class="cdv-btn" data-act="recargar"
                  title="Volver a leer el repo">⟳</button>
        </div>
      </div>
      <div class="cdv-commit" hidden>
        <input type="text" class="cdv-commit-msg" placeholder="mensaje del commit">
        <label class="cdv-commit-sel"><input type="checkbox" class="cdv-commit-todo" checked>
          todos los archivos</label>
        <button type="button" class="cdv-btn ok" data-act="commit">Commitear</button>
        <button type="button" class="cdv-btn" data-act="commit-cerrar">Cancelar</button>
      </div>
      <div class="cdv-main">
        <aside class="cdv-lado">
          <div class="cdv-filtro-caja">
            <input type="text" class="cdv-filtro" placeholder="filtrar archivos  (/)">
          </div>
          <div class="cdv-lista"></div>
        </aside>
        <div class="cdv-split-handle" title="arrastrá para redimensionar"></div>
        <section class="cdv-vista">
          <div class="cdv-vacio muted">Elegí un archivo de la izquierda.</div>
        </section>
      </div>
      <div class="cdv-ayuda muted">
        <kbd>j</kbd>/<kbd>k</kbd> archivo · <kbd>n</kbd>/<kbd>p</kbd> hunk ·
        <kbd>x</kbd> revisado · <kbd>s</kbd> lado a lado · <kbd>w</kbd> wrap ·
        <kbd>/</kbd> filtrar
      </div>
    </div>`;
}

function chipsEstado(r) {
  const chips = [];
  chips.push(`<span class="cdv-chip">${r.commits} commit${r.commits === 1 ? "" : "s"}</span>`);
  const t = r.totals || {};
  chips.push(`<span class="cdv-chip"><b class="add">+${t.added || 0}</b>
    <b class="del">−${t.removed || 0}</b> en ${t.files || 0} archivo${
      (t.files || 0) === 1 ? "" : "s"}</span>`);
  if (r.remote_ahead === -1)
    chips.push(`<span class="cdv-chip warn" title="la rama no está en origin">sin pushear</span>`);
  else if (r.remote_ahead > 0)
    chips.push(`<span class="cdv-chip warn">${r.remote_ahead} sin pushear</span>`);
  if (!r.is_current)
    chips.push(`<span class="cdv-chip warn" title="HEAD está en otra rama: no puedo
      mirar el working tree, solo los commits">no es la rama actual</span>`);
  if (r.pr_url)
    chips.push(`<a class="cdv-chip link" href="${escape(r.pr_url)}" target="_blank"
      rel="noopener">PR abierto ↗</a>`);
  return chips.join("");
}

/** Junta archivos del diff + untracked en UNA lista con índice estable. */
function cargarFilas(r) {
  const filas = (r.files || []).map((f) => ({ ...f, untracked: false }));
  if (S.mode !== "committed") {
    for (const p of r.untracked || []) {
      filas.push({ path: p, old_path: "", status: "?", added: 0, removed: 0,
                   binary: false, pending: true, untracked: true });
    }
  }
  S.rows = filas;
}

function renderLista() {
  const q = S.filtro.trim().toLowerCase();
  const visibles = S.rows
    .map((f, i) => ({ f, i }))
    .filter(({ f }) => !q || f.path.toLowerCase().includes(q));
  if (!visibles.length) {
    return `<p class="cdv-nada muted">${S.rows.length
      ? "ningún archivo coincide con el filtro"
      : "la rama no tiene cambios en este modo"}</p>`;
  }
  // Agrupado por carpeta: 40 archivos planos no se leen, agrupados sí.
  const porDir = new Map();
  for (const v of visibles) {
    const corte = v.f.path.lastIndexOf("/");
    const dir = corte < 0 ? "" : v.f.path.slice(0, corte);
    if (!porDir.has(dir)) porDir.set(dir, []);
    porDir.get(dir).push(v);
  }
  let html = "";
  for (const [dir, items] of porDir) {
    if (dir) html += `<div class="cdv-dir" title="${attr(dir)}">${escape(dir)}</div>`;
    for (const { f, i } of items) {
      const base = f.path.slice(f.path.lastIndexOf("/") + 1);
      const st = f.status || "M";
      html += `
        <div class="cdv-fila${i === S.sel ? " sel" : ""}${
              S.seen.has(f.path) ? " visto" : ""}" data-i="${i}"
             title="${attr(f.old_path ? f.old_path + " → " + f.path : f.path)}">
          <input type="checkbox" class="cdv-check" data-i="${i}"
                 title="incluir en el commit">
          <span class="cdv-st st-${st}" title="${attr(TITULO_ESTADO[st] || st)}">${
            ICONO[st] || "±"}</span>
          <span class="cdv-nombre">${escape(base)}</span>
          <span class="cdv-nums">${f.untracked
            ? `<span class="cdv-tag nuevo">nuevo</span>`
            : (f.binary ? `<span class="cdv-tag">bin</span>`
               : `<b class="add">+${f.added}</b><b class="del">−${f.removed}</b>`)}</span>
          ${f.pending && !f.untracked
            ? `<span class="cdv-tag pend" title="tiene cambios sin commitear">wt</span>` : ""}
        </div>`;
    }
  }
  return html;
}

// ---------------------------------------------------------------- diff

function renderArchivo(fila, datos) {
  const cab = `
    <div class="cdv-cab">
      <div class="cdv-cab-path">
        ${fila.old_path ? `<span class="muted">${escape(fila.old_path)} →</span> ` : ""}
        <code>${escape(fila.path)}</code>
        ${fila.untracked ? `<span class="cdv-tag nuevo">sin trackear</span>` : ""}
      </div>
      <div class="cdv-cab-btns">
        <button type="button" class="cdv-btn" data-act="wrap">${
          S.wrap ? "↵ wrap on" : "↵ wrap"}</button>
        <button type="button" class="cdv-btn" data-act="split">${
          S.split ? "▥ lado a lado" : "▤ unificado"}</button>
        <button type="button" class="cdv-btn" data-act="ctx"
                title="líneas de contexto">±${S.ctx}</button>
        <button type="button" class="cdv-btn" data-act="copiar">⧉ path</button>
        <button type="button" class="cdv-btn" data-act="visto">${
          S.seen.has(fila.path) ? "✓ revisado" : "marcar revisado"}</button>
        <button type="button" class="cdv-btn danger" data-act="restore"
                title="Descartar los cambios sin commitear de este archivo">
          ⨯ Descartar</button>
      </div>
    </div>`;
  if (datos.error) return cab + `<p class="cdv-nada fail">${escape(datos.error)}</p>`;
  if (datos.binary)
    return cab + `<p class="cdv-nada muted">Archivo binario: git no muestra
      el contenido y el visor tampoco lo inventa.</p>`;
  const archivos = parseUnifiedDiff(datos.diff);
  const f = archivos[0];
  if (!f || !f.hunks.length)
    return cab + `<p class="cdv-nada muted">Sin cambios de texto en este modo.</p>`;
  const cuerpo = f.hunks.map((h, i) => renderHunk(h, i)).join("");
  const aviso = datos.truncated
    ? `<p class="cdv-aviso">⚠ diff cortado (${(datos.full_size / 1024).toFixed(1)} KB
       en total): para el resto, <code>git diff -- ${escape(fila.path)}</code>.</p>`
    : "";
  return cab + aviso
    + `<div class="cdv-diff${S.wrap ? " wrap" : ""}${S.split ? " split" : ""}">${cuerpo}</div>`;
}

function renderHunk(h, i) {
  const filas = S.split ? filasSplit(h.lines) : filasUnificadas(h.lines);
  return `
    <details class="cdv-hunk" data-h="${i}" open>
      <summary><span class="cdv-hunk-rango">${escape(h.header.split("@@")[1] || "")}</span>
        ${h.seccion ? `<span class="cdv-hunk-sec">${escape(h.seccion)}</span>` : ""}</summary>
      <table class="cdv-tabla">${filas}</table>
    </details>`;
}

function filasUnificadas(lineas) {
  let html = "";
  for (let i = 0; i < lineas.length; i++) {
    const l = lineas[i];
    if (l.type === "\\") {
      html += `<tr class="l-meta"><td class="n"></td><td class="n"></td>
               <td class="t">${escape(l.text)}</td></tr>`;
      continue;
    }
    let texto = escape(l.text);
    // Resalte intra-línea: solo cuando un `-` y un `+` se emparejan
    // uno a uno. Con dos borradas y una agregada no hay pareja obvia y
    // marcar cualquier cosa miente más de lo que ayuda.
    const par = pareja(lineas, i);
    if (par) texto = marcar(l.text, par.otro, l.type === "+");
    html += `<tr class="l${l.type === "+" ? "-add" : l.type === "-" ? "-del" : "-ctx"}">
      <td class="n">${l.oldNo ?? ""}</td><td class="n">${l.newNo ?? ""}</td>
      <td class="t"><span class="s">${l.type === " " ? " " : l.type}</span>${texto}</td></tr>`;
  }
  return html;
}

/** Si `lineas[i]` es la única `-`/`+` de su bloque, su contraparte. */
function pareja(lineas, i) {
  const l = lineas[i];
  if (l.type !== "+" && l.type !== "-") return null;
  if (l.type === "-") {
    const solo = lineas[i + 1] && lineas[i + 1].type === "+"
      && (!lineas[i + 2] || lineas[i + 2].type !== "+")
      && (!lineas[i - 1] || lineas[i - 1].type !== "-");
    return solo ? { otro: lineas[i + 1].text } : null;
  }
  const solo = lineas[i - 1] && lineas[i - 1].type === "-"
    && (!lineas[i - 2] || lineas[i - 2].type !== "-")
    && (!lineas[i + 1] || lineas[i + 1].type !== "+");
  return solo ? { otro: lineas[i - 1].text } : null;
}

function marcar(texto, otro, esAdd) {
  const [ia, fa, ib, fb] = esAdd ? tramoCambiado(otro, texto)
                                 : tramoCambiado(texto, otro);
  const ini = esAdd ? ib : ia;
  const fin = esAdd ? fb : fa;
  if (fin <= ini) return escape(texto);
  return escape(texto.slice(0, ini))
    + `<mark class="${esAdd ? "m-add" : "m-del"}">${escape(texto.slice(ini, fin))}</mark>`
    + escape(texto.slice(fin));
}

/** Lado a lado: los `-` a la izquierda, los `+` a la derecha, alineados. */
function filasSplit(lineas) {
  const filas = [];
  let i = 0;
  while (i < lineas.length) {
    const l = lineas[i];
    if (l.type === " " || l.type === "\\") {
      filas.push([l, l]);
      i++;
      continue;
    }
    const del = [];
    const add = [];
    while (i < lineas.length && lineas[i].type === "-") del.push(lineas[i++]);
    while (i < lineas.length && lineas[i].type === "+") add.push(lineas[i++]);
    const n = Math.max(del.length, add.length);
    for (let k = 0; k < n; k++) filas.push([del[k] || null, add[k] || null]);
  }
  const celda = (l, lado) => {
    if (!l) return `<td class="n"></td><td class="t vacia"></td>`;
    const cls = (l.type === " " || l.type === "\\") ? ""
      : (lado === "izq" ? "c-del" : "c-add");
    return `<td class="n">${(lado === "izq" ? l.oldNo : l.newNo) ?? ""}</td>
            <td class="t ${cls}">${escape(l.text)}</td>`;
  };
  return filas.map(([a, b]) =>
    `<tr>${celda(a, "izq")}${celda(b, "der")}</tr>`).join("");
}

// =====================================================================
// Wiring
// =====================================================================

function montar(r) {
  if (r.error && !r.exists) return;
  const raiz = document.querySelector("#diff-modal-body .cdv");
  if (!raiz) return;
  const lista = raiz.querySelector(".cdv-lista");
  const vista = raiz.querySelector(".cdv-vista");
  const filtro = raiz.querySelector(".cdv-filtro");
  lista.innerHTML = renderLista();

  raiz.addEventListener("click", (e) => {
    const modo = e.target.closest("[data-mode]");
    if (modo) return cambiarModo(modo.dataset.mode);
    const acc = e.target.closest("[data-act]");
    if (acc) return accion(acc.dataset.act, raiz);
    if (e.target.closest(".cdv-check")) return;      // el check no navega
    const fila = e.target.closest(".cdv-fila");
    if (fila) seleccionar(Number(fila.dataset.i));
  });
  filtro.addEventListener("input", () => {
    S.filtro = filtro.value;
    lista.innerHTML = renderLista();
  });
  raiz.addEventListener("keydown", (e) => {
    if (e.target === filtro) {
      if (e.key === "Escape") { filtro.value = ""; S.filtro = ""; lista.innerHTML = renderLista(); filtro.blur(); }
      return;
    }
    if (e.target.matches("input, textarea")) return;
    atajos(e, raiz);
  });
  // El foco arranca en la raíz para que los atajos anden sin un click.
  raiz.tabIndex = -1;
  raiz.focus({ preventScroll: true });
  arrastrarDivisor(raiz);
  if (S.rows.length) seleccionar(0);
  else vista.innerHTML = `<div class="cdv-vacio muted">La rama no tiene cambios
    en este modo${r.is_current ? "" : " (y HEAD está en otra rama)"}.</div>`;
}

function atajos(e, raiz) {
  const k = e.key;
  if (k === "/") { e.preventDefault(); raiz.querySelector(".cdv-filtro").focus(); return; }
  if (k === "j" || k === "ArrowDown") { e.preventDefault(); mover(1); return; }
  if (k === "k" || k === "ArrowUp") { e.preventDefault(); mover(-1); return; }
  if (k === "n" || k === "p") { e.preventDefault(); saltarHunk(k === "n" ? 1 : -1, raiz); return; }
  if (k === "x") { e.preventDefault(); accion("visto", raiz); return; }
  if (k === "s") { e.preventDefault(); accion("split", raiz); return; }
  if (k === "w") { e.preventDefault(); accion("wrap", raiz); return; }
}

function mover(delta) {
  const q = S.filtro.trim().toLowerCase();
  const idx = S.rows.map((f, i) => i)
    .filter((i) => !q || S.rows[i].path.toLowerCase().includes(q));
  if (!idx.length) return;
  const pos = idx.indexOf(S.sel);
  const next = idx[Math.min(Math.max((pos < 0 ? 0 : pos + delta), 0), idx.length - 1)];
  if (next !== S.sel) seleccionar(next);
}

function saltarHunk(delta, raiz) {
  const vista = raiz.querySelector(".cdv-vista");
  const hunks = Array.from(vista.querySelectorAll(".cdv-hunk"));
  if (!hunks.length) return;
  const tope = vista.getBoundingClientRect().top;
  const pos = hunks.findIndex((h) => h.getBoundingClientRect().top > tope + 4);
  const actual = pos < 0 ? hunks.length - 1 : Math.max(0, pos - (delta > 0 ? 0 : 1));
  const destino = hunks[Math.min(Math.max(actual + (delta > 0 ? 1 : -1), 0), hunks.length - 1)]
    || hunks[0];
  destino.scrollIntoView({ block: "start", behavior: "smooth" });
}

async function seleccionar(i) {
  const fila = S.rows[i];
  if (!fila) return;
  S.sel = i;
  const raiz = document.querySelector("#diff-modal-body .cdv");
  if (!raiz) return;
  raiz.querySelectorAll(".cdv-fila").forEach((el) =>
    el.classList.toggle("sel", Number(el.dataset.i) === i));
  raiz.querySelector(`.cdv-fila[data-i="${i}"]`)
    ?.scrollIntoView({ block: "nearest" });
  const vista = raiz.querySelector(".cdv-vista");
  const clave = `${S.mode}|${S.ctx}|${fila.path}`;
  if (S.cache.has(clave)) {
    vista.innerHTML = renderArchivo(fila, S.cache.get(clave));
    vista.scrollTop = 0;
    return;
  }
  vista.innerHTML = `<p class="cdv-nada muted">cargando ${escape(fila.path)}…</p>`;
  try {
    const datos = await apiRoot(
      `/conversations/${encodeURIComponent(S.conv)}/diff`
      + `?path=${encodeURIComponent(fila.path)}&mode=${S.mode}&context=${S.ctx}`
      // El origen de un rename va en el pathspec o git lo muestra como
      // alta completa: la detección corre después de limitar por path.
      + (fila.old_path ? `&old=${encodeURIComponent(fila.old_path)}` : ""),
      null, 60_000);
    S.cache.set(clave, datos);
    if (S.sel !== i) return;                    // el humano ya saltó a otro
    vista.innerHTML = renderArchivo(fila, datos);
    vista.scrollTop = 0;
  } catch (e) {
    vista.innerHTML = `<p class="cdv-nada fail">no pude traer el diff: ${
      escape(e.message)}</p>`;
  }
}

function cambiarModo(mode) {
  if (mode === S.mode) return;
  S.mode = mode;
  S.cache.clear();
  openConvDiff(S.meta);
}

function arrastrarDivisor(raiz) {
  const handle = raiz.querySelector(".cdv-split-handle");
  const lado = raiz.querySelector(".cdv-lado");
  if (!handle || !lado) return;
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const x0 = e.clientX;
    const w0 = lado.getBoundingClientRect().width;
    const mover_ = (ev) => {
      lado.style.width = `${Math.min(Math.max(w0 + ev.clientX - x0, 160), 640)}px`;
    };
    const soltar = () => {
      handle.removeEventListener("pointermove", mover_);
      handle.removeEventListener("pointerup", soltar);
    };
    handle.addEventListener("pointermove", mover_);
    handle.addEventListener("pointerup", soltar);
  });
}

// =====================================================================
// Acciones
// =====================================================================

function msg(texto, kind = "") {
  const el = document.querySelector("#diff-modal-msg");
  if (!el) return;
  el.textContent = texto || "";
  el.className = kind === "err" ? "fail" : kind === "ok" ? "ok" : "muted";
}

function seleccionados() {
  const raiz = document.querySelector("#diff-modal-body .cdv");
  if (!raiz) return [];
  return Array.from(raiz.querySelectorAll(".cdv-check:checked"))
    .map((c) => S.rows[Number(c.dataset.i)])
    .filter(Boolean).map((f) => f.path);
}

async function accion(act, raiz) {
  const fila = S.rows[S.sel];
  switch (act) {
    case "recargar":
      S.cache.clear();
      return openConvDiff(S.meta);
    case "wrap":
      S.wrap = !S.wrap;
      return fila && repintar(fila);
    case "split":
      S.split = !S.split;
      return fila && repintar(fila);
    case "ctx": {
      const pasos = [3, 8, 20, 0];
      S.ctx = pasos[(pasos.indexOf(S.ctx) + 1) % pasos.length];
      S.cache.clear();
      return seleccionar(S.sel);
    }
    case "visto":
      if (!fila) return;
      S.seen.has(fila.path) ? S.seen.delete(fila.path) : S.seen.add(fila.path);
      raiz.querySelector(".cdv-lista").innerHTML = renderLista();
      return repintar(fila);
    case "copiar":
      if (!fila) return;
      try {
        await navigator.clipboard.writeText(fila.path);
        msg(`copiado: ${fila.path}`, "ok");
      } catch { msg("el browser no dejó copiar", "err"); }
      return;
    case "commit-abrir": {
      const caja = raiz.querySelector(".cdv-commit");
      caja.hidden = false;
      const sel = seleccionados();
      caja.querySelector(".cdv-commit-todo").checked = !sel.length;
      const input = caja.querySelector(".cdv-commit-msg");
      if (!input.value) input.value = `${S.branch}: cambios del hilo`;
      input.focus();
      input.select();
      return;
    }
    case "commit-cerrar":
      raiz.querySelector(".cdv-commit").hidden = true;
      return;
    case "commit": {
      const caja = raiz.querySelector(".cdv-commit");
      const message = caja.querySelector(".cdv-commit-msg").value.trim();
      const todo = caja.querySelector(".cdv-commit-todo").checked;
      const paths = todo ? [] : seleccionados();
      if (!todo && !paths.length)
        return msg("marcá los archivos con el check, o volvé a 'todos'", "err");
      const r = await correr("commit", { message, paths });
      if (r) {
        caja.hidden = true;
        toast(`commit ${r.sha} (${r.files} archivo${r.files === 1 ? "" : "s"}) ✓`, "ok");
        recargarSuave();
      }
      return;
    }
    case "push": {
      const r = await correr("push", {});
      if (r) { toast(`push de ${S.branch} ✓`, "ok"); recargarSuave(); }
      return;
    }
    case "pr": {
      const r = await correr("pr", { title: `${S.branch}: cambios del hilo` });
      if (r) {
        S.prUrl = r.pr_url;
        toast(r.created ? `PR abierto: ${r.pr_url}` : `PR ya existía: ${r.pr_url}`, "ok");
        recargarSuave();
      }
      return;
    }
    case "merge": {
      if (!await confirmModal({
        title: "Mergear el PR",
        body: `Voy a mergear con squash el PR de ${S.branch} y borrar la rama `
            + `remota. Solo funciona si el PR va a develop: a main no se `
            + `mergea desde acá.`,
        confirmText: "Sí, mergear",
        cancelText: "Volver",
        danger: true,
      })) return;
      const r = await correr("merge", { method: "squash" });
      if (r) { toast(`PR mergeado a develop ✓`, "ok"); recargarSuave(); }
      return;
    }
    case "sync": {
      const r = await correr("sync-base", {});
      if (r) {
        toast(r.behind ? `base ${r.base} adelantada ${r.behind} commit(s) ✓`
                       : `base ${r.base} ya estaba al día`, "ok");
        recargarSuave();
      }
      return;
    }
    case "restore": {
      if (!fila) return;
      if (fila.untracked)
        return msg("es un archivo sin trackear: git no tiene de dónde volver "
                   + "(borralo a mano si sobra)", "err");
      if (!await confirmModal({
        title: "Descartar cambios",
        body: `Se pierden los cambios sin commitear de ${fila.path} `
            + `(staged incluido). No hay undo.`,
        confirmText: "Sí, descartar",
        cancelText: "Volver",
        danger: true,
      })) return;
      const r = await correr("restore", { paths: [fila.path] });
      if (r) { toast(`${fila.path} vuelto a HEAD ✓`, "ok"); recargarSuave(); }
      return;
    }
    default:
      return;
  }
}

function repintar(fila) {
  const vista = document.querySelector("#diff-modal-body .cdv-vista");
  const datos = S.cache.get(`${S.mode}|${S.ctx}|${fila.path}`);
  if (vista && datos) vista.innerHTML = renderArchivo(fila, datos);
}

/** POST a /conversations/{id}/git/{accion}. Devuelve el payload o null. */
async function correr(accion_, body) {
  msg(`${accion_}…`);
  const botones = document.querySelectorAll("#diff-modal-body .cdv-btn");
  botones.forEach((b) => { b.disabled = true; });
  try {
    const r = await apiRoot(
      `/conversations/${encodeURIComponent(S.conv)}/git/${accion_}`,
      { method: "POST", body: body || {} }, 180_000);
    msg("");
    return r;
  } catch (e) {
    msg(e.message, "err");
    toast(`git ${accion_}: ${e.message}`, "err");
    return null;
  } finally {
    botones.forEach((b) => { b.disabled = false; });
  }
}

/** Vuelve a leer el repo sin perder el archivo abierto. */
async function recargarSuave() {
  const raiz = document.querySelector("#diff-modal-body .cdv");
  if (!raiz) return;
  const abierto = S.rows[S.sel]?.path;
  S.cache.clear();
  try {
    const r = await pedirLista(S.mode);
    S.prUrl = r.pr_url || null;
    cargarFilas(r);
    const top = raiz.querySelector(".cdv-ident");
    if (top) top.innerHTML = `
      <code class="cdv-rama">${escape(r.branch)}</code>
      <span class="cdv-flecha">→</span>
      <code class="cdv-base">${escape(r.base || "?")}</code>${chipsEstado(r)}`;
    raiz.querySelector(".cdv-lista").innerHTML = renderLista();
    const i = S.rows.findIndex((f) => f.path === abierto);
    if (i >= 0) seleccionar(i);
    else if (S.rows.length) seleccionar(0);
    else raiz.querySelector(".cdv-vista").innerHTML =
      `<div class="cdv-vacio muted">Ya no quedan cambios en este modo.</div>`;
  } catch (e) {
    msg("no pude recargar la lista: " + e.message, "err");
  }
}

// =====================================================================
// CSS (inyectado: ver la nota de arriba)
// =====================================================================

function inyectarCss() {
  if (document.getElementById("chat-diff-css")) return;
  const st = document.createElement("style");
  st.id = "chat-diff-css";
  st.textContent = `
#diff-modal .modal-card { max-width: min(1600px, 96vw); max-height: 92vh; height: 92vh; }
#diff-modal .modal-body { padding: 0; overflow: hidden; display: flex; }
.cdv { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0;
       font-size: 12px; outline: none; }
.cdv-vacio { padding: 24px; }
.cdv-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
           padding: 8px 12px; border-bottom: 1px solid #27272a; }
.cdv-ident { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
.cdv-rama { color: #7dd3fc; } .cdv-base { color: #a1a1aa; } .cdv-flecha { opacity: .5; }
.cdv-chip { border: 1px solid #27272a; border-radius: 999px; padding: 1px 8px;
            font-size: 10.5px; color: #a1a1aa; white-space: nowrap; }
.cdv-chip.warn { border-color: #a1620033; background: #f59e0b14; color: #fbbf24; }
.cdv-chip.link { color: #7dd3fc; text-decoration: none; }
.cdv-chip .add { color: #4ade80; } .cdv-chip .del { color: #f87171; margin-left: 4px; }
.cdv-modos { display: flex; border: 1px solid #27272a; border-radius: 7px; overflow: hidden; }
.cdv-modo { padding: 3px 10px; background: transparent; border: 0; color: #a1a1aa;
            cursor: pointer; font-size: 11px; }
.cdv-modo:hover { background: #ffffff0d; color: #e4e4e7; }
.cdv-modo.on { background: #38bdf826; color: #7dd3fc; }
.cdv-acciones { display: flex; gap: 5px; margin-left: auto; flex-wrap: wrap; }
.cdv-btn { border: 1px solid #3f3f46; background: #18181b; color: #d4d4d8;
           border-radius: 6px; padding: 3px 9px; font-size: 11px; cursor: pointer; }
.cdv-btn:hover:not(:disabled) { background: #27272a; border-color: #52525b; }
.cdv-btn:disabled { opacity: .45; cursor: progress; }
.cdv-btn.danger { border-color: #7f1d1d; color: #fca5a5; }
.cdv-btn.danger:hover:not(:disabled) { background: #7f1d1d33; }
.cdv-btn.ok { border-color: #15803d; color: #86efac; }
.cdv-commit { display: flex; gap: 8px; align-items: center; padding: 8px 12px;
              border-bottom: 1px solid #27272a; background: #18181b80; }
.cdv-commit[hidden] { display: none; }
.cdv-commit-msg { flex: 1; min-width: 0; background: #0b0b0d; color: inherit;
                  border: 1px solid #3f3f46; border-radius: 6px; padding: 5px 9px;
                  font-size: 12px; }
.cdv-commit-sel { display: flex; align-items: center; gap: 5px; color: #a1a1aa;
                  font-size: 11px; white-space: nowrap; }
.cdv-main { display: flex; flex: 1; min-height: 0; }
.cdv-lado { width: 300px; display: flex; flex-direction: column; min-width: 160px;
            border-right: 1px solid #27272a; }
.cdv-filtro-caja { padding: 6px; border-bottom: 1px solid #1f1f23; }
.cdv-filtro { width: 100%; background: #0b0b0d; color: inherit; font-size: 11.5px;
              border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 8px; }
.cdv-lista { flex: 1; overflow: auto; padding-bottom: 8px; }
.cdv-dir { position: sticky; top: 0; z-index: 1; background: #0f0f11; color: #71717a;
           font-size: 10px; padding: 4px 8px; border-bottom: 1px solid #1f1f23;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cdv-fila { display: flex; align-items: center; gap: 6px; padding: 3px 8px 3px 4px;
            cursor: pointer; border-left: 2px solid transparent; }
.cdv-fila:hover { background: #ffffff0a; }
.cdv-fila.sel { background: #38bdf81f; border-left-color: #38bdf8; }
.cdv-fila.visto .cdv-nombre { color: #52525b; text-decoration: line-through; }
.cdv-check { accent-color: #38bdf8; }
.cdv-st { width: 12px; text-align: center; font-size: 11px; }
.st-A { color: #4ade80; } .st-D { color: #f87171; } .st-M { color: #fbbf24; }
.st-R, .st-C { color: #a78bfa; } .st-\\? { color: #38bdf8; }
.cdv-nombre { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; font-family: ui-monospace, monospace; font-size: 11.5px; }
.cdv-nums { font-size: 10px; white-space: nowrap; }
.cdv-nums .add { color: #4ade80; } .cdv-nums .del { color: #f87171; margin-left: 4px; }
.cdv-tag { border: 1px solid #3f3f46; border-radius: 4px; padding: 0 4px;
           font-size: 9px; color: #a1a1aa; }
.cdv-tag.nuevo { border-color: #0369a1; color: #7dd3fc; }
.cdv-tag.pend { border-color: #a16207; color: #fbbf24; }
.cdv-split-handle { width: 5px; cursor: col-resize; background: transparent; }
.cdv-split-handle:hover { background: #38bdf84d; }
.cdv-vista { flex: 1; min-width: 0; overflow: auto; }
.cdv-nada { padding: 16px; }
.cdv-aviso { margin: 8px 12px; color: #fbbf24; font-size: 11px; }
.cdv-cab { position: sticky; top: 0; z-index: 2; display: flex; align-items: center;
           gap: 10px; flex-wrap: wrap; padding: 7px 12px; background: #0f0f11;
           border-bottom: 1px solid #27272a; }
.cdv-cab-path { min-width: 0; overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; font-size: 11.5px; }
.cdv-cab-btns { display: flex; gap: 5px; margin-left: auto; }
.cdv-hunk { border-bottom: 1px solid #1f1f23; }
.cdv-hunk > summary { cursor: pointer; padding: 3px 12px; background: #17171a;
                      color: #71717a; font-size: 10.5px; list-style: none;
                      display: flex; gap: 10px; }
.cdv-hunk > summary::-webkit-details-marker { display: none; }
.cdv-hunk > summary:hover { color: #a1a1aa; background: #1f1f23; }
.cdv-hunk-rango { font-family: ui-monospace, monospace; }
.cdv-hunk-sec { color: #52525b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cdv-tabla { width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace;
             font-size: 11.5px; line-height: 1.5; }
.cdv-tabla td.n { width: 1%; min-width: 34px; text-align: right; padding: 0 6px;
                  color: #3f3f46; user-select: none; vertical-align: top;
                  border-right: 1px solid #1f1f23; }
.cdv-tabla td.t { padding: 0 8px; white-space: pre; }
.cdv-diff.wrap .cdv-tabla td.t { white-space: pre-wrap; word-break: break-word; }
.cdv-tabla td.t .s { color: #52525b; margin-right: 2px; }
.cdv-tabla tr.l-add td.t { background: #16a34a1a; color: #bbf7d0; }
.cdv-tabla tr.l-del td.t { background: #dc26261a; color: #fecaca; }
.cdv-tabla tr.l-meta td.t { color: #52525b; font-style: italic; }
.cdv-tabla td.t.c-add { background: #16a34a1a; color: #bbf7d0; }
.cdv-tabla td.t.c-del { background: #dc26261a; color: #fecaca; }
.cdv-tabla td.t.vacia { background: #ffffff05; }
.cdv-diff.split .cdv-tabla td.t { width: 50%; }
.cdv-tabla mark { background: transparent; padding: 0; border-radius: 2px; }
.cdv-tabla mark.m-add { background: #16a34a4d; color: #dcfce7; }
.cdv-tabla mark.m-del { background: #dc26264d; color: #fee2e2; }
.cdv-ayuda { padding: 4px 12px; border-top: 1px solid #27272a; font-size: 10px; }
.cdv-ayuda kbd { border: 1px solid #3f3f46; border-radius: 3px; padding: 0 3px;
                 font-family: ui-monospace, monospace; }
@media (max-width: 820px) {
  .cdv-main { flex-direction: column; }
  .cdv-lado { width: auto !important; max-height: 34vh; border-right: 0;
              border-bottom: 1px solid #27272a; }
  .cdv-split-handle { display: none; }
}`;
  document.head.appendChild(st);
}
