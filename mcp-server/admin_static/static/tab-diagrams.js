// Tab Diagramas (2026-07-22): editor de diagramas Mermaid con render en vivo.
// Usa mermaid.js vendorizado (static/vendor-mermaid-11.17.2.relay1.min.js) — sin backend, sin red externa.
// ── pony: la feature la pide el usuario para documentar arquitectura rápido.
// ── pony: reescrito a DOM plain (previamente importaba html/render/useState
//    del bundle CLI de Tailwind, que no es una librería tipo Preact).
//    Mismo patrón que el resto de los tabs.
//
// Iter 10.4 (2026-07-24): selector de proyecto + auto-generadores
// (project card / directory tree / top files by degree / stats pie)
// y guardado a repo (.md envuelto en fence mermaid + .svg del preview).
// Reusa endpoints existentes: GET /admin/api/projects, /fs/browse, y
// PUT /admin/api/projects/{slug}/workspace/file (este último ya valida
// path traversal, crea dirs intermedias, respeta overwrite).
//
// 2026-07-25 — revisión a fondo. Los autogen viejos NO servían:
//   · "Top files" pegaba a /index/files, que devolvía [] siempre (el
//     parser del backend esperaba JSON y cbm imprime tablas de texto).
//     Además los nodos File de cbm tienen grado 0: rankear "top files by
//     degree" era imposible por construcción, no por falta de índice.
//   · "Directory tree" usaba fs/browse, que lista sub-REPOS, no dirs:
//     para SampleApp devolvía 2 nodos (website-demo, react-app).
//   · "Architecture" era `ls` del root como grafo estrella — el nombre
//     prometía arquitectura y entregaba un listado de carpetas.
//   · "Stats" metía nodes, edges y MB×100 en un pie: partes de ningún
//     todo, la torta no significa nada.
// Ahora todos salen de GET /admin/api/projects/{slug}/architecture, que
// expone lo que cbm ya calculaba y nadie estaba leyendo: capas, boundaries
// con peso de llamadas, clusters por cohesión, hotspots por fan-in y rutas.

import { escape, api, _dbg } from "./api.js";
import { toast } from "./ui.js";

// Los tipos sequence / class / state ya NO son plantillas en blanco: son
// autogeneradores con datos reales del grafo de cbm, disponibles para
// cualquier proyecto indexado (ver autoClasses / autoSequence / autoStates).
// Antes eran esqueletos con las tripas del 4bis.relay hardcodeadas, que no
// servían de nada al abrir el tab desde otro repo.

// El lienzo inicial tampoco puede ser el mapa del relay: es lo primero que
// ve alguien que entra al tab desde otro proyecto.
const DEFAULT_CODE = `graph TD
    A[Cliente] --> B[Mi servicio]
    B --> C[Capa de servicios]
    C --> D[(Base de datos)]`;

// Estado módulo-scoped: el tab es single-instance, no necesitamos
// árbol de componentes. Re-render = repintar el <div> preview.
let _mermaidTheme = 'dark';
let _state = null;     // {code, svg, error, theme, loading}
let _projects = [];    // [{slug, name, repo_path, indexed, ...}] cache

// Mapea la primera palabra del código Mermaid a un label legible para
// screen readers. El patrón es estable: cualquier mermaid empieza con
// `flowchart`, `graph`, `classDiagram`, etc. Si no matchea, fallback al
// genérico "diagrama Mermaid". Devuelve SOLO el string — el caller lo
// pone como aria-label del <svg> (junto con role="img").
function _ariaLabelForDiagram(code) {
  const first = (code || "").trim().split(/\s+/)[0] || "";
  const map = {
    flowchart: "diagrama de flujo",
    graph: "diagrama de flujo",
    classDiagram: "diagrama de clases",
    sequenceDiagram: "diagrama de secuencia",
    stateDiagram: "diagrama de estados",
    "stateDiagram-v2": "diagrama de estados",
    erDiagram: "diagrama entidad-relación",
    gantt: "diagrama de Gantt",
    pie: "gráfico de torta",
    gitGraph: "grafo de git",
    journey: "mapa de journey",
  };
  return map[first] || "diagrama Mermaid";
}

// Una sola promesa compartida: loadDiagrams() llamaba loadMermaid() y
// además renderDiagram(), que vuelve a llamarlo — el flag de "ya cargado"
// se seteaba DESPUÉS del await, así que las dos pasaban el guard y el
// bundle se bajaba dos veces (verificado en document.scripts).
let _mermaidPromise = null;

function loadMermaid() {
  if (_mermaidPromise) return _mermaidPromise;
  _mermaidPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    // Vendor local con la versión en el nombre (admin.py cachea vendor-*
    // 24h: sin eso, un upgrade queda invisible por un día). Flat porque
    // admin_static no sirve subdirectorios — cero red externa.
    script.src = '/admin/static/vendor-mermaid-11.17.2.relay1.min.js';
    script.onload = () => {
      window.mermaid.initialize({
        startOnLoad: false, theme: _mermaidTheme, securityLevel: 'strict' });
      resolve();
    };
    script.onerror = () => {
      // Que falle la carga no puede dejar la promesa cacheada en rejected
      // para siempre: sin esto, un corte de red mataba el tab hasta F5.
      _mermaidPromise = null;
      reject(new Error('no se pudo cargar mermaid.js (vendor local)'));
    };
    document.head.appendChild(script);
  });
  return _mermaidPromise;
}

// ── Viewport: zoom + pan (2026-07-25) ──
// Antes el SVG se metía en un div con overflow-auto y el `max-width` inline
// que mermaid le pone: un diagrama grande salía encogido a un ancho de
// columna, sin forma de acercarse a leer un nodo. Para apoyarse en el
// diagrama mientras te preguntan por un proyecto eso es inservible.
// ponytail: transform CSS sobre un wrapper, sin librería de pan/zoom.
const _MIN_SCALE = 0.1, _MAX_SCALE = 8;
let _view = { scale: 1, x: 0, y: 0 };

function applyView() {
  const stage = document.getElementById('mermaid-stage');
  if (stage) {
    stage.style.transform =
      `translate(${_view.x}px, ${_view.y}px) scale(${_view.scale})`;
  }
  const pct = document.getElementById('diag-zoom-pct');
  if (pct) pct.textContent = Math.round(_view.scale * 100) + '%';
}

function svgSize() {
  const svg = document.querySelector('#mermaid-stage svg');
  if (!svg) return null;
  const vb = svg.viewBox && svg.viewBox.baseVal;
  if (vb && vb.width) return { w: vb.width, h: vb.height };
  const r = svg.getBoundingClientRect();
  return r.width ? { w: r.width, h: r.height } : null;
}

function fitToView() {
  const box = document.getElementById('mermaid-preview');
  const size = svgSize();
  if (!box || !size) return;
  const pad = 24;
  const cw = box.clientWidth - pad * 2, ch = box.clientHeight - pad * 2;
  // Nunca agrandamos más allá de 1: un diagrama de 3 nodos ocupando toda
  // la pantalla se ve peor, no mejor.
  const scale = Math.max(_MIN_SCALE, Math.min(1, cw / size.w, ch / size.h));
  _view = { scale,
            x: (box.clientWidth - size.w * scale) / 2,
            y: (box.clientHeight - size.h * scale) / 2 };
  applyView();
}

function zoomAt(factor, cx, cy) {
  const box = document.getElementById('mermaid-preview');
  if (!box) return;
  const next = Math.max(_MIN_SCALE, Math.min(_MAX_SCALE, _view.scale * factor));
  const k = next / _view.scale;
  if (k === 1) return;
  const r = box.getBoundingClientRect();
  // Punto bajo el cursor queda fijo: zoom "hacia donde estás mirando".
  const px = (cx == null ? r.width / 2 : cx - r.left);
  const py = (cy == null ? r.height / 2 : cy - r.top);
  _view.x = px - (px - _view.x) * k;
  _view.y = py - (py - _view.y) * k;
  _view.scale = next;
  applyView();
}

function wireViewport() {
  const box = document.getElementById('mermaid-preview');
  if (!box || box.dataset.wired) return;
  box.dataset.wired = '1';
  box.addEventListener('wheel', (e) => {
    if (!document.getElementById('mermaid-stage')) return;
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
  }, { passive: false });
  // Atajos de teclado para el zoom (sin esto era solo mouse wheel).
  // `+`/`-` (con o sin Shift) acercan/alejan; `0` resetea a 100%.
  // Ignorar si el foco está en un input/textarea (escribir "+" no
  // debe triggear zoom). `keydown` captura antes de que el carácter
  // se inserte en el input.
  box.addEventListener('keydown', (e) => {
    if (!document.getElementById('mermaid-stage')) return;
    const tag = (e.target?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "+" || e.key === "=" /* sin Shift es "=" en teclado US */) {
      e.preventDefault();
      zoomAt(1.15);
    } else if (e.key === "-" || e.key === "_") {
      e.preventDefault();
      zoomAt(1 / 1.15);
    } else if (e.key === "0") {
      e.preventDefault();
      fitToView();
    }
  });
  // box necesita tabindex para recibir keydown; ver `wireViewport`
  // (tabindex ya estaba, pero por si lo cambiaron).
  if (!box.hasAttribute("tabindex")) box.setAttribute("tabindex", "0");
  let drag = null;
  box.addEventListener('mousedown', (e) => {
    if (!document.getElementById('mermaid-stage')) return;
    drag = { x: e.clientX - _view.x, y: e.clientY - _view.y };
    box.style.cursor = 'grabbing';
    e.preventDefault();
  });
  // En window, no en el div: si sueltas fuera del preview el drag muere igual.
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    _view.x = e.clientX - drag.x;
    _view.y = e.clientY - drag.y;
    applyView();
  });
  window.addEventListener('mouseup', () => {
    if (!drag) return;
    drag = null;
    box.style.cursor = 'grab';
  });
  box.addEventListener('dblclick', fitToView);
}

function paintPreview() {
  const preview = document.getElementById('mermaid-preview');
  if (!preview || !_state) return;
  const { error, svg, loading } = _state;
  const btn = document.getElementById('mermaid-render');
  if (btn) {
    btn.disabled = loading;
    btn.textContent = loading ? 'Renderizando...' : 'Render';
  }
  const msg = (html) =>
    `<span class="absolute inset-0 flex items-center justify-center p-4">${html}</span>`;
  if (loading) {
    preview.innerHTML = msg('<span class="text-zinc-400 text-sm">Renderizando…</span>');
  } else if (error) {
    preview.innerHTML = `<div class="absolute inset-0 overflow-auto p-4"><div class="text-red-400 text-sm p-4 bg-red-950/40 rounded border border-red-800"><pre class="whitespace-pre-wrap">${escape(error)}</pre></div></div>`;
  } else if (svg) {
    // Mermaid renderiza en modo strict con el sanitizador actualizado.
    // Mermaid no setea role ni aria-label en el <svg>: el screen reader
    // no anuncia el diagrama. Lo agregamos después de inyectar.
    preview.innerHTML =
      `<div id="mermaid-stage" style="position:absolute;top:0;left:0;`
      + `transform-origin:0 0;will-change:transform">${svg}</div>`;
    const svgEl = preview.querySelector('svg');
    if (svgEl) {
      svgEl.setAttribute("role", "img");
      svgEl.setAttribute("aria-label", _ariaLabelForDiagram(_state.code));
    }
    // Mermaid le mete max-width inline y width=100%: hay que sacarlos o el
    // SVG se re-encoge al ancho de la columna y el zoom no hace nada.
    const el = preview.querySelector('svg');
    const size = svgSize();
    if (el && size) {
      el.style.maxWidth = 'none';
      el.style.width = size.w + 'px';
      el.style.height = size.h + 'px';
    }
    wireViewport();
    fitToView();
  } else {
    preview.innerHTML = msg('<span class="text-zinc-400 text-sm">Escribe sintaxis y presiona Render</span>');
  }
}

async function renderDiagram() {
  if (!_state || !_state.code.trim()) return;
  _state.loading = true;
  _state.error = '';
  _state.svg = '';
  paintPreview();
  try {
    await loadMermaid();
    window.mermaid.initialize({ startOnLoad: false, theme: _state.theme, securityLevel: 'strict' });
    const id = 'mermaid-' + Math.random().toString(36).slice(2, 8);
    const { svg } = await window.mermaid.render(id, _state.code);
    _state.svg = svg;
  } catch (e) {
    _state.error = (e && e.message) || 'Error de sintaxis Mermaid';
  } finally {
    _state.loading = false;
    paintPreview();
  }
}

// ── Helpers compartidos por auto-generadores ──

// `kind` alimenta el nombre sugerido al guardar (sample-app-arquitectura, …)
// para no volver al `{slug}-{timestamp}` que no decía qué era el archivo.
function setCodeAndRender(code, kind) {
  if (!_state) return;
  _state.code = code;
  if (kind) {
    const p = getSelectedProject();
    _state.name = `${p ? p.slug : "diagrama"}-${kind}`;
  }
  const codeEl = document.getElementById('mermaid-code');
  if (codeEl) codeEl.value = code;
  renderDiagram();
}

function safeId(s) {
  // mermaid ids: alfanum + underscore. Sacamos todo lo que no.
  //
  // El truncado a 40 chars COLISIONABA: los qualified_name de cbm comparten
  // prefijos largos, así que EfUnitOfWork.ExecuteInTransactionAsync y
  // EfUnitOfWork.SaveChangesAsync daban el mismo id y mermaid los fusionaba
  // en un nodo (medido en SampleApp: 16 nodos declarados → 11 en el SVG, 5
  // símbolos desaparecían sin error). Sufijo de hash sobre la cadena
  // COMPLETA: sigue corto y ya no pisa.
  const raw = String(s || '');
  const clean = raw.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  let h = 5381;
  for (let i = 0; i < raw.length; i++) h = (((h << 5) + h) ^ raw.charCodeAt(i)) >>> 0;
  return 'n_' + (clean.slice(0, 36) || 'x') + '_' + h.toString(36);
}

function getSelectedProject() {
  const slug = document.getElementById('diag-slug')?.value;
  if (!slug) return null;
  return _projects.find((p) => p.slug === slug) || null;
}

function tsSlug() {
  // 2026-07-24T12-34-56 (sin ':' porque algunos FS se quejan)
  return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
}

async function loadProjects() {
  try {
    const r = await api("projects");
    _projects = (r.projects || []).filter((p) => p.repo_path);
  } catch (e) {
    _projects = [];
    _dbg("diagrams: /projects falló", e.message);
  }
  // Hidratar el <select> que ya está en el DOM (loadDiagrams lo creó).
  const sel = document.getElementById('diag-slug');
  if (sel) {
    const cur = sel.value;
    // Indexados primero y marcados: los autogen dependen del índice cbm,
    // así que el usuario tiene que poder ver de un vistazo cuáles sirven.
    const sorted = [..._projects].sort((a, b) =>
      (b.indexed ? 1 : 0) - (a.indexed ? 1 : 0)
      || String(a.name || a.slug).localeCompare(String(b.name || b.slug)));
    sel.innerHTML =
      `<option value="">— elegir proyecto —</option>`
      + sorted.map((p) =>
          // escape() no toca comillas y esto va dentro de un atributo.
          `<option value="${escape(p.slug)}" title="${escape(p.repo_path).replace(/"/g, "&quot;")}">`
          + `${p.indexed ? "" : "○ "}${escape(p.name || p.slug)}`
          + `${p.indexed ? "" : " (sin índice)"}</option>`).join("");
    if (cur && _projects.find((p) => p.slug === cur)) sel.value = cur;
    updateAutoButtons();
  }
}

//: id del botón → si necesita índice cbm (todos menos la ficha).
const _AUTO_BTNS = {
  'diag-auto-card': false, 'diag-auto-arch': true, 'diag-auto-hot': true,
  'diag-auto-clusters': true, 'diag-auto-routes': true, 'diag-auto-tree': true,
  'diag-auto-classes': true, 'diag-auto-seq': true, 'diag-auto-states': true,
};

function updateAutoButtons() {
  const p = getSelectedProject();
  for (const [id, needsIndex] of Object.entries(_AUTO_BTNS)) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.disabled = !p || (needsIndex && !p.indexed);
    // Decir POR QUÉ está gris. Antes un botón deshabilitado no daba
    // ninguna pista de que faltaba indexar el repo.
    if (p && needsIndex && !p.indexed) {
      b.title = `"${p.name || p.slug}" no está indexado en cbm — indexalo `
        + `desde el tab Índice para habilitar este diagrama.`;
    } else if (b.dataset.tip) {
      b.title = b.dataset.tip;
    }
  }
  // Copiar/guardar dependen del contenido, NO del proyecto: copiar un
  // diagrama escrito a mano sin proyecto elegido es legítimo.
  const hasCode = !!(_state && _state.code.trim());
  const hasSvg = !!(_state && _state.svg);
  for (const [id, ok] of [['diag-save-md', hasCode && !!p],
                          ['diag-save-svg', hasSvg && !!p],
                          ['diag-copy', hasCode],
                          ['diag-download', hasSvg],
                          // Regenerar solo tiene sentido tras un autogen.
                          ['diag-regen', !!p && !!_lastKind],
                          // LLM: proyecto indexado + último diagrama generado.
                          ['diag-llm-gen', !!p && !!p.indexed && !!_lastKind]]) {
    const b = document.getElementById(id);
    if (b) b.disabled = !ok;
  }
}

// ── Auto-generadores (Iter 10.4) ──

function autoProjectCard() {
  const p = getSelectedProject();
  if (!p) return;
  const idx = p.indexed && p.index_stats
    ? `${p.index_stats.nodes.toLocaleString()} nodes / ${p.index_stats.edges.toLocaleString()} edges`
    : "no indexado";
  const tools = (p.native_tools || []).join(", ") || "ninguna";
  const code = [
    "graph TD",
    `  P["${(p.name || p.slug).replace(/"/g, "'")} <br/><i>${p.slug}</i>"] --> R["${(p.repo_path || "").replace(/"/g, "'")}"]`,
    `  P --> I["indexado: ${idx.replace(/"/g, "'")}"]`,
    `  P --> G["git: ${p.has_git ? "sí" : "no"}"]`,
    `  P --> T["native_tools: ${tools.replace(/"/g, "'")}"]`,
    `  P --> N["night_mode: ${p.night_mode_enabled ? "on" : "off"}"]`,
  ].join("\n");
  setCodeAndRender(code, "ficha");
}

// Ruido de builtins: en repos Python cbm cuenta `str`/`len`/`list` como
// nodos con fan-in altísimo, y sin filtrarlos el diagrama de capas queda
// dominado por ellos (medido en 4bis.relay: 8 de 10 "capas core" eran
// builtins). No son arquitectura del repo.
const _NOISE = new Set([
  "str", "len", "list", "dict", "int", "float", "bool", "set", "tuple",
  "print", "range", "type", "super", "isinstance", "getattr", "setattr",
  "object", "Exception", "sorted", "enumerate", "zip", "map", "filter",
  "console", "Object", "Array", "String", "Number", "JSON", "Math",
]);

const _LAYER_ORDER = ["api", "entry", "internal", "core", "otros"];
const _LAYER_LABEL = {
  api: "🌐 API / rutas", entry: "🚪 Entrada", internal: "⚙️ Interno",
  core: "🧱 Núcleo (alto fan-in)", otros: "❔ Otros",
};

// Trae los aspects pedidos de cbm. Devuelve null (y ya avisó por toast)
// si el proyecto no está indexado o el endpoint falla.
async function fetchArchitecture(p, aspects) {
  if (!p.indexed) {
    toast(`"${p.name || p.slug}" no está indexado en cbm — indexalo desde el `
      + `tab Índice y vuelve.`, "warn");
    return null;
  }
  try {
    // El spawn de cbm puede tardar; el default de 15s del fetch es justo.
    return await api(
      `projects/${encodeURIComponent(p.slug)}/architecture`
      + `?aspects=${encodeURIComponent(aspects.join(","))}`,
      undefined, 45_000);
  } catch (e) {
    toast(`No se pudo leer la arquitectura: ${e.message}`, "err");
    return null;
  }
}

// Saca el prefijo del proyecto de un qualified_name de cbm:
// "C-Users-...-SampleApp.Shared.RetornoVM.RetornoVM" → "Shared.RetornoVM.RetornoVM"
function stripQn(qn, cbmProject) {
  let s = String(qn || "");
  if (cbmProject && s.startsWith(cbmProject + ".")) s = s.slice(cbmProject.length + 1);
  return s;
}

function mmLabel(s) {
  // Mermaid rompe con comillas dobles dentro de ["..."]; los corchetes y
  // el pipe también cortan el parser.
  return String(s == null ? "" : s)
    .replace(/"/g, "'").replace(/[[\]|{}]/g, "").trim();
}

// ── Diagramas guardados por proyecto (2026-07-25) ──
// Los autogen recalculan contra cbm cada vez; eso pisa cualquier retoque a
// mano y paga el spawn de cbm de nuevo. Si ya guardaste el diagrama en el
// repo, mostramos ESE y solo recalculamos si lo pides (⟳ Regenerar).
// Sin backend nuevo: workspace/files + workspace/file ya existen.

const DIAG_DIR = "docs/diagrams";
let _saved = [];        // [{name, path}] .md de docs/diagrams del proyecto
let _lastKind = null;   // último autogen corrido (para ⟳ Regenerar)

async function loadSavedDiagrams() {
  _saved = [];
  const p = getSelectedProject();
  if (p) {
    try {
      const r = await api(`projects/${encodeURIComponent(p.slug)}`
        + `/workspace/files?subdir=${encodeURIComponent(DIAG_DIR)}`);
      _saved = (r.entries || [])
        .filter((e) => !e.is_dir && e.name.endsWith(".md"))
        .map((e) => ({ name: e.name.replace(/\.md$/, ""), path: e.path,
                       modified_at: e.modified_at }));
    } catch (e) {
      // 404 = el repo todavía no tiene docs/diagrams. Es lo normal, no error.
      if (e.status !== 404) _dbg("diagrams: listado guardados falló", e.message);
    }
  }
  paintSavedChips();
  updateAutoButtons();
}

function paintSavedChips() {
  const box = document.getElementById('diag-saved');
  if (!box) return;
  if (!_saved.length) {
    box.innerHTML = `<span class="text-[11px] text-zinc-400">`
      + `sin diagramas guardados en ${DIAG_DIR}/</span>`;
    return;
  }
  box.innerHTML = `<span class="text-[11px] text-zinc-400">Guardados:</span> `
    + _saved.map((s) =>
        `<button data-saved="${escape(s.path).replace(/"/g, "&quot;")}" `
        + `class="rounded bg-sky-900/50 border border-sky-800 px-2 py-0.5 text-[11px] `
        + `text-sky-100 hover:bg-sky-800 transition-colors" `
        + `title="Abrir ${escape(s.path)}">📄 ${escape(s.name)}</button>`).join(" ");
}

// El .md guardado es `# titulo\n\n```mermaid\n<código>\n````. Sacamos el fence.
function extractMermaid(md) {
  const m = String(md || "").match(/```mermaid\s*\n([\s\S]*?)```/);
  return m ? m[1].trimEnd() : String(md || "").trim();
}

async function openSavedDiagram(path, kind) {
  const p = getSelectedProject();
  if (!p) return;
  try {
    const r = await api(`projects/${encodeURIComponent(p.slug)}`
      + `/workspace/file?path=${encodeURIComponent(path)}`);
    if (!_state) return;
    _state.code = extractMermaid(r.content);
    _state.name = path.split("/").pop().replace(/\.md$/, "");
    const codeEl = document.getElementById('mermaid-code');
    if (codeEl) codeEl.value = _state.code;
    if (kind) _lastKind = kind;
    renderDiagram();
    toast(`Mostrando el guardado ${path} — ⟳ Regenerar para recalcular.`, "ok");
  } catch (e) {
    toast(`No se pudo abrir ${path}: ${e.message}`, "err");
  }
}

function savedPathFor(kind) {
  const p = getSelectedProject();
  if (!p) return null;
  const want = `${p.slug}-${kind}`;
  return (_saved.find((s) => s.name === want) || {}).path || null;
}

//: kind → generador. El kind es también el sufijo del archivo guardado.
const AUTOGEN = {
  arquitectura: autoArchitecture, hotspots: autoHotspots,
  modulos: autoClusters, rutas: autoRoutes, arbol: autoFileTree,
  clases: autoClasses, secuencia: autoSequence, estados: autoStates,
  ficha: autoProjectCard,
};

// Punto único de entrada de los autogen: decide guardado vs recalcular.
async function runAutogen(kind, force) {
  if (!AUTOGEN[kind]) return;
  if (!getSelectedProject()) {
    toast("Elige un proyecto primero en el selector de arriba.", "warn");
    return;
  }
  _lastKind = kind;
  updateAutoButtons();
  if (!force) {
    const hit = savedPathFor(kind);
    if (hit) { await openSavedDiagram(hit, kind); return; }
  }
  await AUTOGEN[kind]();
}

// ── Auto-generadores (2026-07-25, sobre /architecture) ──

// EL diagrama del módulo: capas reales + boundaries con peso de llamadas.
async function autoArchitecture() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchArchitecture(p, ["layers", "boundaries"]);
  if (!r) return;

  const layers = (r.layers || []).filter(
    (l) => l.name && l.name !== "-" && !_NOISE.has(l.name));
  const bounds = (r.boundaries || []).filter(
    (b) => b.from && b.to && !_NOISE.has(b.from) && !_NOISE.has(b.to));
  if (!layers.length && !bounds.length) {
    toast("cbm no detectó capas ni límites en este repo (¿índice viejo? "
      + "reindexa desde el tab Índice).", "warn");
    return;
  }

  // Agrupar por capa; los extremos de boundaries que no estén en `layers`
  // caen en "otros" para que ninguna arista quede colgando.
  const layerOf = new Map(layers.map((l) => [l.name, l.layer || "otros"]));
  const reasonOf = new Map(layers.map((l) => [l.name, l.reason || ""]));
  for (const b of bounds) {
    if (!layerOf.has(b.from)) layerOf.set(b.from, "otros");
    if (!layerOf.has(b.to)) layerOf.set(b.to, "otros");
  }
  const byLayer = new Map();
  for (const [name, layer] of layerOf) {
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer).push(name);
  }

  const lines = ["flowchart LR"];
  const ordered = [..._LAYER_ORDER.filter((k) => byLayer.has(k)),
                   ...[...byLayer.keys()].filter((k) => !_LAYER_ORDER.includes(k))];
  for (const layer of ordered) {
    lines.push(`  subgraph L_${safeId(layer)}["${mmLabel(_LAYER_LABEL[layer] || layer)}"]`);
    for (const name of byLayer.get(layer).sort()) {
      const why = reasonOf.get(name);
      lines.push(`    ${safeId(name)}["${mmLabel(name)}`
        + `${why ? `<br/><i>${mmLabel(why)}</i>` : ""}"]`);
    }
    lines.push("  end");
  }
  // Aristas con el conteo de llamadas: es lo que convierte el dibujo en
  // información (qué acopla con qué y cuánto).
  for (const b of bounds) {
    lines.push(`  ${safeId(b.from)} -->|${mmLabel(b.calls)}| ${safeId(b.to)}`);
  }
  setCodeAndRender(lines.join("\n"), "arquitectura");
}

// Símbolos más usados del repo (fan-in real de cbm), agrupados por paquete.
async function autoHotspots() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchArchitecture(p, ["hotspots"]);
  if (!r) return;
  const spots = (r.hotspots || [])
    .map((h) => ({ qn: stripQn(h.qn, r.cbm_project), fan: +h.fan_in || 0 }))
    .filter((h) => h.qn && !_NOISE.has(h.qn) && !h.qn.startsWith("builtins."))
    .slice(0, 12);
  if (!spots.length) {
    toast("cbm no reportó hotspots para este repo.", "warn");
    return;
  }
  const max = Math.max(...spots.map((s) => s.fan)) || 1;
  const lines = ["flowchart LR", `  ROOT["${mmLabel(p.name || p.slug)}<br/>`
    + `<i>símbolos más usados</i>"]`];
  const seen = new Set();
  for (const s of spots) {
    const parts = s.qn.split(".");
    const sym = parts.pop();
    const pkg = parts.slice(-2).join(".") || "(raíz)";
    const pkgId = safeId("pkg_" + pkg);
    if (!seen.has(pkgId)) {
      seen.add(pkgId);
      lines.push(`  ROOT --> ${pkgId}["📦 ${mmLabel(pkg)}"]`);
    }
    const id = safeId("h_" + s.qn);
    lines.push(`  ${pkgId} --> ${id}["${mmLabel(sym)}<br/><i>fan-in ${s.fan}</i>"]`);
    // Los 3 más pesados se resaltan: sin esto todo pesa igual visualmente.
    if (s.fan >= max * 0.5) lines.push(`  style ${id} stroke:#f59e0b,stroke-width:2px`);
  }
  setCodeAndRender(lines.join("\n"), "hotspots");
}

// Módulos que cbm detecta por cohesión de llamadas.
async function autoClusters() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchArchitecture(p, ["clusters"]);
  if (!r) return;
  const cl = (r.clusters || []).slice(0, 10);
  if (!cl.length) {
    toast("cbm no detectó clusters en este repo.", "warn");
    return;
  }
  const lines = ["flowchart TD",
    `  ROOT["${mmLabel(p.name || p.slug)}<br/><i>módulos por cohesión</i>"]`];
  for (const c of cl) {
    const id = safeId("c_" + c.id);
    const coh = c.cohesion ? Number(c.cohesion).toFixed(2) : "?";
    lines.push(`  ROOT --> ${id}["${mmLabel(c.label)} #${mmLabel(c.id)}`
      + `<br/><i>${mmLabel(c.members)} miembros · cohesión ${coh}</i>"]`);
    // cbm repite nombres dentro de top_nodes (p.ej. "LogAsync;LogAsync"):
    // sin dedupe emitíamos dos veces la misma arista.
    const tops = [...new Set(String(c.top_nodes || "").split(";").filter(Boolean))];
    for (const n of tops.slice(0, 3)) {
      lines.push(`  ${id} --> ${safeId("cn_" + c.id + "_" + n)}("${mmLabel(n)}")`);
    }
  }
  setCodeAndRender(lines.join("\n"), "modulos");
}

// Superficie HTTP del repo, agrupada por recurso.
async function autoRoutes() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchArchitecture(p, ["routes"]);
  if (!r) return;
  const routes = (r.routes || []).filter((x) => x.path && x.path !== "-");
  if (!routes.length) {
    toast("cbm no encontró rutas HTTP en este repo.", "warn");
    return;
  }
  const byRes = new Map();
  for (const rt of routes.slice(0, 40)) {
    const res = (rt.path.split("/").filter(Boolean)[0]) || "/";
    if (!byRes.has(res)) byRes.set(res, []);
    byRes.get(res).push(rt);
  }
  const lines = ["flowchart LR",
    `  API["🌐 ${mmLabel(p.name || p.slug)}<br/><i>${routes.length} rutas</i>"]`];
  for (const [res, rts] of byRes) {
    const rid = safeId("r_" + res);
    lines.push(`  API --> ${rid}["/${mmLabel(res)}"]`);
    for (const rt of rts) {
      // method viene "-" cuando cbm no lo pudo inferir: no inventamos GET.
      const m = rt.method && rt.method !== "-" ? rt.method : "?";
      lines.push(`  ${rid} --> ${safeId("rt_" + rt.path + m)}`
        + `("${mmLabel(m)} ${mmLabel(rt.path)}")`);
    }
  }
  setCodeAndRender(lines.join("\n"), "rutas");
}

// ── Clases / Secuencia / Estados: los tipos que antes eran plantillas
// vacías con el relay hardcodeado. Ahora salen del grafo de cbm y están
// disponibles para cualquier proyecto indexado. ──

async function fetchGraph(p, kind, qs) {
  try {
    return await api(`projects/${encodeURIComponent(p.slug)}/graph/${kind}`
      + (qs || ""), undefined, 60_000);
  } catch (e) {
    toast(`No se pudo leer el grafo (${kind}): ${e.message}`, "err");
    return null;
  }
}

// classDiagram con clases reales, sus métodos y la herencia del repo.
async function autoClasses() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchGraph(p, "classes");
  if (!r) return;

  const byClass = new Map();
  for (const row of r.methods || []) {
    const c = row["c.name"], m = row["m.name"];
    if (!c || !m) continue;
    if (!byClass.has(c)) byClass.set(c, []);
    // El constructor viene repetido como método homónimo de la clase.
    if (m !== c && !byClass.get(c).includes(m)) byClass.get(c).push(m);
  }
  const inh = (r.inherits || []).map((x) => [x["a.name"], x["b.name"]])
    .concat((r.implements || []).map((x) => [x["a.name"], x["b.name"]]))
    .filter(([a, b]) => a && b);
  if (!byClass.size && !inh.length) {
    toast("cbm no tiene clases indexadas para este repo (¿es un proyecto "
      + "sin clases, o falta reindexar?).", "warn");
    return;
  }
  // Las 10 clases con más métodos: un classDiagram con 500 clases es ilegible.
  // Los tests van último aunque tengan más métodos que nadie: son las clases
  // con más métodos del repo (medido en SampleApp: las 4 primeras eran *Tests) y
  // no es lo que quieres ver cuando te preguntan cómo funciona el sistema.
  const isTest = (c) => /tests?$|spec$|^test|fixture|mock/i.test(c);
  const top = [...byClass.entries()]
    .sort((a, b) => (isTest(a[0]) - isTest(b[0])) || b[1].length - a[1].length)
    .slice(0, 10);
  const shown = new Set(top.map(([c]) => c));
  const lines = ["classDiagram"];
  for (const [cls, methods] of top) {
    lines.push(`  class ${safeId(cls)}["${mmLabel(cls)}"] {`);
    for (const m of methods.slice(0, 8)) lines.push(`    +${mmLabel(m)}()`);
    if (methods.length > 8) lines.push(`    ..+${methods.length - 8} más..`);
    lines.push("  }");
  }
  // Solo relaciones entre clases dibujadas, o mermaid inventa nodos sueltos.
  let rel = 0;
  for (const [a, b] of inh) {
    if (!shown.has(a)) continue;
    if (!shown.has(b)) {
      lines.push(`  class ${safeId(b)}["${mmLabel(b)}"]`);
      shown.add(b);
    }
    lines.push(`  ${safeId(b)} <|-- ${safeId(a)}`);
    if (++rel >= 20) break;
  }
  setCodeAndRender(lines.join("\n"), "clases");
}

// sequenceDiagram del camino de llamadas REAL de una función.
let _seqFn = "";   // qualified_name elegido (vacío = top hotspot)

async function autoSequence() {
  const p = getSelectedProject();
  if (!p) return;
  let fn = (document.getElementById('diag-seq-fn')?.value || "").trim() || _seqFn;
  // Loading visual mientras esperamos dos llamadas API (~2.5s).
  const btn = document.getElementById('diag-auto-seq');
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Buscando…'; }
  try {
  if (!fn) {
    // Sin función elegida: arrancamos por el símbolo más usado del repo.
    const arch = await fetchArchitecture(p, ["hotspots"]);
    if (!arch) return;
    const top = (arch.hotspots || [])
      .filter((h) => h.qn && !h.qn.startsWith("builtins."))[0];
    if (!top) { toast("cbm no reportó hotspots para elegir un punto de partida.", "warn"); return; }
    fn = top.qn;
  }
  const r = await fetchGraph(p, "sequence",
    `?function=${encodeURIComponent(fn)}`);
  if (!r) return;

  if (r.ambiguous) {
    // Varias funciones con ese nombre: que elija, no adivinamos.
    paintSeqSuggestions(r.suggestions || []);
    toast(`${(r.suggestions || []).length} funciones se llaman así — elige una abajo.`, "warn");
    return;
  }
  paintSeqSuggestions([]);
  const callers = r.callers || [], callees = r.callees || [];
  if (!callers.length && !callees.length) {
    toast(`Sin llamadas registradas para ${fn.split(".").pop()}.`, "warn");
    return;
  }
  const short = (qn) => (String(qn).split(".").pop() || qn);
  const target = short(fn);
  const tId = safeId("T_" + target);
  const lines = ["sequenceDiagram", "  autonumber"];
  const parts = new Map([[tId, target]]);
  const addPart = (g) => {
    const id = safeId("P_" + g);
    if (!parts.has(id)) parts.set(id, short(g));
    return id;
  };
  // Entrantes primero (quién lo llama), después salientes por hop.
  const inRows = callers.slice(0, 8).map((c) => ({ id: addPart(c.group), ...c }));
  const outRows = [...callees].sort((a, b) => (a.hop || 1) - (b.hop || 1))
    .slice(0, 14).map((c) => ({ id: addPart(c.group), ...c }));
  for (const [id, label] of parts) {
    lines.push(`  participant ${id} as ${mmLabel(label)}`);
  }
  for (const c of inRows) lines.push(`  ${c.id}->>${tId}: ${mmLabel(c.name)}()`);
  for (const c of outRows) {
    lines.push(`  ${tId}->>${c.id}: ${mmLabel(c.name)}()`
      + (c.hop > 1 ? ` [hop ${c.hop}]` : ""));
  }
  setCodeAndRender(lines.join("\n"), "secuencia");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

function paintSeqSuggestions(list) {
  const box = document.getElementById('diag-seq-sug');
  if (!box) return;
  box.innerHTML = !list.length ? "" :
    `<span class="text-[11px] text-zinc-400">Elige cuál:</span> `
    + list.slice(0, 20).map((s) =>
        `<button data-fn="${escape(s.qualified_name).replace(/"/g, "&quot;")}" `
        + `class="rounded bg-violet-900/50 border border-violet-800 px-2 py-0.5 `
        + `text-[11px] text-violet-100 hover:bg-violet-800" `
        + `title="${escape(s.file_path || "")}">${escape(s.file_path || s.qualified_name)}</button>`
      ).join(" ");
}

// Enums del repo y quién los consume.
// OJO: cbm NO indexa los miembros de un enum (solo el nodo y sus usos), así
// que esto NO es una máquina de estados con transiciones — es el mapa de qué
// estados existen y quién los toca. Fingir transiciones sería inventar datos.
async function autoStates() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchGraph(p, "states");
  if (!r) return;
  // Dedupe por nombre: el mismo enum suele estar en varios archivos (el
  // LeaseStatus de C# y el del front TS), y sin esto salía repetido.
  const enums = [...new Set((r.enums || []).map((e) => e["e.name"]).filter(Boolean))];
  if (!enums.length) {
    toast("Este repo no tiene enums indexados.", "warn");
    return;
  }
  const users = new Map();
  for (const u of r.usage || []) {
    const e = u["e.name"], x = u["x.name"];
    // Los caches del indexador no son consumidores reales del enum.
    if (!e || !x || /centrality_cache|\.pkl$|^\./.test(x)) continue;
    if (!users.has(e)) users.set(e, new Set());
    users.get(e).add(String(x).split(/[\\/]/).pop());
  }
  const ranked = enums.map((e) => ({ e, n: (users.get(e) || new Set()).size }))
    .sort((a, b) => b.n - a.n).slice(0, 12);
  const lines = ["flowchart LR",
    `  ROOT["🎛️ ${mmLabel(p.name || p.slug)}<br/><i>${enums.length} enums / estados</i>"]`];
  for (const { e, n } of ranked) {
    const id = safeId("e_" + e);
    lines.push(`  ROOT --> ${id}["${mmLabel(e)}<br/><i>${n} consumidores</i>"]`);
    for (const u of [...(users.get(e) || [])].slice(0, 4)) {
      lines.push(`  ${id} --> ${safeId("u_" + e + "_" + u)}("${mmLabel(u)}")`);
    }
  }
  setCodeAndRender(lines.join("\n"), "estados");
}

// Árbol de archivos REAL (file_tree de cbm), no el scan de sub-repos.
async function autoFileTree() {
  const p = getSelectedProject();
  if (!p) return;
  const r = await fetchArchitecture(p, ["file_tree"]);
  if (!r) return;
  const all = (r.file_tree || []).filter((e) => e.path);
  if (!all.length) {
    toast("cbm no devolvió árbol de archivos para este repo.", "warn");
    return;
  }
  // Hasta 2 niveles: más profundo el SVG se vuelve ilegible.
  const entries = all.filter((e) => e.path.split("/").length <= 2)
    .filter((e) => !e.path.split("/").some((s) => s.startsWith(".")))
    .slice(0, 60);
  if (!entries.length) {
    toast("El árbol quedó vacío tras filtrar ocultos.", "warn");
    return;
  }
  const lines = ["flowchart LR", `  ROOT["📦 ${mmLabel(p.name || p.slug)}"]`];
  const emitted = new Set(["ROOT"]);
  for (const e of entries) {
    const segs = e.path.split("/");
    const id = safeId(e.path);
    if (emitted.has(id)) continue;
    emitted.add(id);
    const parent = segs.length > 1 ? safeId(segs.slice(0, -1).join("/")) : "ROOT";
    const icon = e.type === "dir" ? "📁" : "📄";
    lines.push(`  ${emitted.has(parent) ? parent : "ROOT"} --> `
      + `${id}["${icon} ${mmLabel(segs[segs.length - 1])}"]`);
  }
  if (all.length > entries.length) {
    lines.push(`  ROOT --> _more["… +${all.length - entries.length} entradas más"]`);
  }
  setCodeAndRender(lines.join("\n"), "arbol");
}

// ── Guardado a repo (Iter 10.4) ──
// Reusa PUT /admin/api/projects/{slug}/workspace/file — el endpoint
// ya valida path traversal, crea dirs y respeta overwrite.

// El nombre lo elige el usuario. Antes era `{slug}-{ts}.md` con overwrite
// false: cada guardado dejaba un archivo nuevo con un timestamp ilegible,
// y volver a guardar el MISMO diagrama corregido creaba otro archivo en vez
// de pisarlo. docs/diagrams/ terminaba lleno de basura sin poder saber cuál
// era cuál.
async function saveCurrentDiagram(kind /* "md" | "svg" */) {
  const p = getSelectedProject();
  if (!p) { toast("Elige un proyecto primero.", "warn"); return; }
  if (!_state) return;
  if (kind === "md" && !_state.code.trim()) {
    toast("Nada que guardar: escribe o genera un diagrama primero.", "warn");
    return;
  }
  if (kind === "svg" && !_state.svg) {
    toast("Todavía no hay SVG renderizado — toca Render.", "warn");
    return;
  }

  const suggested = (_state.name || `${p.slug}-${tsSlug().slice(0, 10)}`);
  const raw = prompt(
    `Nombre del archivo (sin extensión). Se guarda en `
    + `docs/diagrams/ del repo de ${p.name || p.slug}:`, suggested);
  if (raw === null) return;                       // cancelado
  const base = raw.trim().replace(/\.(md|svg)$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
  if (!base) { toast("Nombre vacío.", "warn"); return; }
  _state.name = base;                             // recordado para el próximo

  const path = `docs/diagrams/${base}.${kind}`;
  const content = kind === "md"
    // Fence mermaid: GitHub lo renderiza solo y queda editable a mano.
    ? `# ${base}\n\n` + "```mermaid\n" + _state.code + "\n```\n"
    : _state.svg;

  const btn = document.getElementById(
    kind === "md" ? "diag-save-md" : "diag-save-svg");
  if (btn) btn.disabled = true;
  const put = (overwrite) => api(
    `projects/${encodeURIComponent(p.slug)}/workspace/file`,
    { method: "PUT", body: { path, content, overwrite } });
  try {
    let r;
    try {
      r = await put(false);
    } catch (e) {
      if (e.status !== 409) throw e;
      // Existe: preguntamos en vez de fallar. Pisar un archivo del repo
      // del usuario sin avisar no, pero obligarlo a inventar otro nombre
      // tampoco — es exactamente el caso "corrige el diagrama y guardo".
      if (!confirm(`Ya existe ${path}. ¿Sobrescribir?`)) {
        toast("Guardado cancelado.", "warn");
        return;
      }
      r = await put(true);
    }
    toast(`Guardado: ${r.path} (${r.size.toLocaleString()} bytes)`, "ok");
    // El .md guardado pasa a ser lo que se muestra la próxima vez.
    if (kind === "md") await loadSavedDiagrams();
  } catch (e) {
    if (e.status === 413) {
      toast("El diagrama supera el cap de 64KB del endpoint — guarda el .md "
        + "(mucho más chico) o usa ⬇ Descargar para el SVG.", "err");
    } else {
      toast(`Error al guardar: ${e.message}`, "err");
    }
  } finally {
    updateAutoButtons();
  }
}

// Descarga local del SVG: el PUT al repo tiene cap de 64KB y un diagrama
// grande lo pasa fácil. Sin esto no había forma de sacar el render.
function downloadSvg() {
  if (!_state || !_state.svg) { toast("No hay SVG renderizado.", "warn"); return; }
  const p = getSelectedProject();
  const name = (_state.name || (p ? p.slug : "diagrama")) + ".svg";
  const url = URL.createObjectURL(
    new Blob([_state.svg], { type: "image/svg+xml" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  toast(`Descargado ${name}`, "ok");
}

async function copyCodeToClipboard() {
  if (!_state || !_state.code.trim()) {
    toast("Nada que copiar.", "warn");
    return;
  }
  try {
    await navigator.clipboard.writeText(_state.code);
    toast("Sintaxis copiada al portapapeles.", "ok");
  } catch (e) {
    toast(`No se pudo copiar: ${e.message}`, "err");
  }
}

export const label = '📊 Diagramas';

export function initDiagrams() {
  // no-op: se monta al primer loadDiagrams.
  // Watchdog: el <section id="tab-diagrams"> arranca vacío y loadDiagrams
  // lo llena con innerHTML. Si algo rompe el armado (excepción síncrona
  // antes del return), el usuario ve una pantalla en blanco sin señal.
  // loadDiagrams se llama desde main.js cuando se abre el tab; 200ms es
  // más que suficiente para que el shell aparezca. Si a los 200ms sigue
  // vacío, pintamos un error visible con botón de reintentar.
  setTimeout(() => {
    const root = document.getElementById('tab-diagrams');
    if (!root || root.innerHTML.trim() !== "") return;
    root.innerHTML =
      `<div class="p-6 max-w-5xl mx-auto">
         <div class="card p-4 border-red-800 bg-red-950/40">
           <strong class="text-red-300">No se pudo cargar Diagramas</strong>
           <p class="text-xs text-red-200 mt-2">
             El shell no se montó dentro de los 200ms esperados.
             Si acabás de tocar el código, mirá la consola.
           </p>
           <button class="btn btn-xs mt-3" data-diagrams-retry>Reintentar</button>
         </div>
       </div>`;
    const btn = root.querySelector("[data-diagrams-retry]");
    if (btn) btn.addEventListener("click", () => loadDiagrams());
  }, 200);
}

export function loadDiagrams() {
  const root = document.getElementById('tab-diagrams');
  if (!root) return;

  root.innerHTML = `
    <div class="p-6 max-w-5xl mx-auto space-y-4">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <h2 class="text-lg font-semibold text-zinc-100">Diagramas Mermaid</h2>
        <div class="flex flex-wrap gap-2">
          <select id="mermaid-theme" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200">
            <option value="dark">🌙 Dark</option>
            <option value="default">☀️ Default</option>
            <option value="neutral">◻️ Neutral</option>
            <option value="forest">🌲 Forest</option>
          </select>
          <button id="mermaid-render" class="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 transition-colors disabled:opacity-60 disabled:cursor-not-allowed">Render</button>
        </div>
      </div>

      <div class="rounded bg-zinc-900/40 border border-zinc-800 p-3 space-y-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-xs font-medium text-zinc-400">Proyecto</span>
          <select id="diag-slug" class="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 min-w-[200px]">
            <option value="">— cargando proyectos —</option>
          </select>
          <span id="diag-hint" class="text-[11px] text-zinc-400">elige un proyecto y después toca un autogen ↓</span>
        </div>
        <div class="flex flex-wrap gap-2">
          <button id="diag-auto-arch"     data-tip="Capas reales del repo + límites entre ellas con el número de llamadas (índice cbm)" class="rounded bg-emerald-800/60 border border-emerald-700 px-3 py-1 text-xs text-emerald-50 hover:bg-emerald-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🏗️ Arquitectura</button>
          <button id="diag-auto-hot"      data-tip="Los símbolos más usados del repo por fan-in real, agrupados por paquete" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🔥 Hotspots</button>
          <button id="diag-auto-clusters" data-tip="Módulos que cbm detecta por cohesión de llamadas, con sus nodos principales" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🧩 Módulos</button>
          <button id="diag-auto-routes"   data-tip="Superficie HTTP del repo agrupada por recurso" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🛣️ Rutas</button>
          <button id="diag-auto-classes"  data-tip="classDiagram con las clases reales del repo, sus métodos y la herencia indexada" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🧬 Clases</button>
          <button id="diag-auto-seq"      data-tip="sequenceDiagram del camino de llamadas real de una función (trace_call_path)" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">⏱️ Secuencia</button>
          <button id="diag-auto-states"   data-tip="Enums del repo y quién los consume (cbm no indexa miembros de enum: no hay transiciones)" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🎛️ Estados</button>
          <button id="diag-auto-tree"     data-tip="Árbol de archivos real del índice (2 niveles, sin ocultos)" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">🌳 Árbol</button>
          <button id="diag-auto-card"     data-tip="Ficha del proyecto: repo, índice, git, tools, night_mode" class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-400 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">📇 Ficha</button>
          <button id="diag-regen" class="rounded bg-zinc-800 border border-zinc-600 px-3 py-1 text-xs text-zinc-300 hover:text-white hover:border-zinc-500 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Recalcula el último diagrama contra cbm, ignorando el guardado">⟳ Regenerar</button>
        </div>
        <div id="diag-saved" class="flex flex-wrap items-center gap-1 pt-1"></div>
        <div class="flex flex-wrap items-center gap-2 pt-1 border-t border-zinc-800">
          <span class="text-[11px] font-medium text-zinc-400">🧠 LLM</span>
          <input id="diag-llm-prompt" placeholder="Foco opcional: solo el flujo de pagos, agrupá por bounded context…"
                 class="flex-1 min-w-[220px] bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 focus:border-purple-500 focus:outline-none" />
          <button id="diag-llm-gen" class="rounded bg-purple-700/80 border border-purple-600 px-3 py-1 text-xs text-purple-50 hover:bg-purple-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Genera el diagrama con LLM (MiniMax M3). Interpreta los datos, agrupa por dominio, filtra ruido.">🧠 Generar con LLM</button>
          <span id="diag-llm-status" class="text-[11px] text-zinc-400 hidden"></span>
        </div>
        <div id="diag-llm-explain" class="hidden rounded bg-purple-950/30 border border-purple-900/50 p-3 text-xs text-purple-100 leading-relaxed"></div>
        <div class="flex flex-wrap gap-2 pt-1 border-t border-zinc-800">
          <button id="diag-save-md"   class="rounded bg-amber-700/70 border border-amber-700 px-3 py-1 text-xs text-amber-50 hover:bg-amber-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Guarda el Mermaid en docs/diagrams/ del repo (pide nombre)">💾 .md al repo</button>
          <button id="diag-save-svg"  class="rounded bg-amber-700/70 border border-amber-700 px-3 py-1 text-xs text-amber-50 hover:bg-amber-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Guarda el SVG renderizado en docs/diagrams/ del repo (pide nombre)">🖼️ .svg al repo</button>
          <button id="diag-download"  class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Descarga el SVG a tu máquina (sin el cap de 64KB del repo)">⬇ Descargar SVG</button>
          <button id="diag-copy"      class="rounded bg-zinc-800 border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:text-zinc-100 hover:border-zinc-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed" title="Copia la sintaxis Mermaid al portapapeles">Copiar</button>
        </div>
      </div>

      <div class="rounded bg-zinc-900/40 border border-zinc-800 p-3 space-y-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-[11px] font-medium text-zinc-400">Secuencia desde</span>
          <input id="diag-seq-fn" placeholder="nombre de función (vacío = el símbolo más usado)"
                 class="flex-1 min-w-[260px] bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 focus:border-emerald-500 focus:outline-none" />
          <span class="text-[11px] text-zinc-400">Enter para trazar</span>
        </div>
        <div id="diag-seq-sug" class="flex flex-wrap items-center gap-1"></div>
      </div>

      <div id="diag-split" class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div id="diag-editor-col">
          <label class="block text-xs font-medium text-zinc-400 mb-1">Sintaxis Mermaid</label>
          <textarea id="mermaid-code" class="w-full h-[32rem] rounded bg-zinc-900 border border-zinc-700 px-4 py-3 text-sm text-zinc-200 font-mono focus:border-emerald-500 focus:outline-none resize-y" spellcheck="false"></textarea>
        </div>
        <div id="diag-preview-col">
          <div class="flex items-center justify-between mb-1 gap-2">
            <label class="block text-xs font-medium text-zinc-400">Vista previa</label>
            <div class="flex items-center gap-1">
              <button id="diag-zoom-out" class="rounded bg-zinc-800 border border-zinc-700 w-7 h-7 text-sm text-zinc-300 hover:text-white" title="Alejar (o rueda del mouse)">−</button>
              <span id="diag-zoom-pct" class="text-[11px] text-zinc-400 w-10 text-center tabular-nums">100%</span>
              <button id="diag-zoom-in" class="rounded bg-zinc-800 border border-zinc-700 w-7 h-7 text-sm text-zinc-300 hover:text-white" title="Acercar (o rueda del mouse)">+</button>
              <button id="diag-zoom-fit" class="rounded bg-zinc-800 border border-zinc-700 px-2 h-7 text-[11px] text-zinc-300 hover:text-white" title="Ajustar a la ventana">⤢ Ajustar</button>
              <button id="diag-expand" class="rounded bg-zinc-800 border border-zinc-700 px-2 h-7 text-[11px] text-zinc-300 hover:text-white" title="Ocultar el editor y usar todo el ancho">⛶ Ampliar</button>
            </div>
          </div>
          <!-- overflow-hidden + transform en el wrapper: el pan lo maneja
               el drag, no la scrollbar (arrastrar es lo natural en un
               diagrama, y con scrollbars el zoom se desancla del cursor). -->
          <div id="mermaid-preview" class="relative w-full h-[32rem] rounded bg-zinc-900 border border-zinc-700 overflow-hidden select-none" style="cursor:grab">
            <span class="text-zinc-400 text-sm absolute inset-0 flex items-center justify-center">Cargando…</span>
          </div>
          <div class="text-[11px] text-zinc-400 mt-1">Rueda = zoom · arrastrar = mover · doble click = ajustar</div>
        </div>
      </div>
    </div>
  `;

  _state = { code: DEFAULT_CODE, svg: '', error: '', theme: 'dark',
             loading: false, name: '' };

  const codeEl = document.getElementById('mermaid-code');
  codeEl.value = _state.code;
  codeEl.addEventListener('input', (e) => {
    _state.code = e.target.value;
    updateAutoButtons();
  });

  document.getElementById('mermaid-render').addEventListener('click', renderDiagram);

  document.getElementById('mermaid-theme').addEventListener('change', (e) => {
    _state.theme = e.target.value;
    _mermaidTheme = e.target.value;
    renderDiagram();
  });

  // Secuencia: Enter en el input traza esa función; un chip de sugerencia
  // fija el qualified_name cuando el nombre corto era ambiguo.
  document.getElementById('diag-seq-fn').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { _seqFn = ''; runAutogen('secuencia', true); }
  });
  document.getElementById('diag-seq-sug').addEventListener('click', (e) => {
    const fn = e.target && e.target.dataset && e.target.dataset.fn;
    if (!fn) return;
    _seqFn = fn;
    document.getElementById('diag-seq-fn').value = fn;
    runAutogen('secuencia', true);
  });

  // Cambiar de proyecto NO toca el editor. La versión anterior pisaba
  // _state.code con DEFAULT_CODE en cada cambio: si estabas escribiendo un
  // diagrama a mano y tocabas el selector, perdías todo sin confirmación ni
  // undo. El selector solo elige contra qué repo corren los autogen; el
  // "preview desactualizado" que motivó ese reset se arregla con el aviso
  // de abajo, no borrando trabajo del usuario.
  document.getElementById('diag-slug').addEventListener('change', () => {
    updateAutoButtons();
    _lastKind = null;
    _seqFn = '';
    const fnInput = document.getElementById('diag-seq-fn');
    if (fnInput) fnInput.value = '';
    paintSeqSuggestions([]);
    loadSavedDiagrams();
    const hint = document.getElementById('diag-hint');
    const p = getSelectedProject();
    if (hint) {
      hint.textContent = p
        ? (p.indexed
            ? `${p.name || p.slug} — toca un autogen ↓`
            : `${p.name || p.slug} no está indexado: solo 📇 Ficha disponible`)
        : "elige un proyecto y después toca un autogen ↓";
    }
  });

  // Autogen + save + copy. Todos pasan por runAutogen: si ya hay un
  // diagrama guardado de ese tipo en el repo, se muestra ese.
  for (const [id, kind] of [['diag-auto-arch', 'arquitectura'],
                            ['diag-auto-hot', 'hotspots'],
                            ['diag-auto-clusters', 'modulos'],
                            ['diag-auto-routes', 'rutas'],
                            ['diag-auto-tree', 'arbol'],
                            ['diag-auto-classes', 'clases'],
                            ['diag-auto-seq', 'secuencia'],
                            ['diag-auto-states', 'estados'],
                            ['diag-auto-card', 'ficha']]) {
    document.getElementById(id)
      .addEventListener('click', () => runAutogen(kind, false));
  }
  document.getElementById('diag-regen').addEventListener('click', () => {
    if (_lastKind) runAutogen(_lastKind, true);
  });

  // Zoom / encuadre.
  document.getElementById('diag-zoom-in').addEventListener('click', () => zoomAt(1.25));
  document.getElementById('diag-zoom-out').addEventListener('click', () => zoomAt(1 / 1.25));
  document.getElementById('diag-zoom-fit').addEventListener('click', fitToView);
  document.getElementById('diag-expand').addEventListener('click', (e) => {
    // Ampliar = esconder el editor. Para revisar un diagrama en una llamada
    // el textarea no aporta, y el ancho es todo.
    const col = document.getElementById('diag-editor-col');
    const split = document.getElementById('diag-split');
    const on = col.classList.toggle('hidden');
    split.classList.toggle('lg:grid-cols-2', !on);
    e.target.textContent = on ? '⛶ Restaurar' : '⛶ Ampliar';
    document.getElementById('mermaid-preview').classList.toggle('h-[32rem]', !on);
    document.getElementById('mermaid-preview').classList.toggle('h-[75vh]', on);
    // El contenedor cambió de tamaño: re-encuadrar o el diagrama queda corrido.
    setTimeout(fitToView, 50);
  });
  // Chips de guardados (delegación: se repintan al cambiar de proyecto).
  document.getElementById('diag-saved').addEventListener('click', (e) => {
    const path = e.target && e.target.dataset && e.target.dataset.saved;
    if (path) openSavedDiagram(path);
  });
  document.getElementById('diag-save-md').addEventListener('click', () => saveCurrentDiagram("md"));
  document.getElementById('diag-save-svg').addEventListener('click', () => saveCurrentDiagram("svg"));
  document.getElementById('diag-download').addEventListener('click', downloadSvg);
  document.getElementById('diag-copy').addEventListener('click', copyCodeToClipboard);

  // ── LLM diagram generation (Iter 10.4) ──
  // Mapea el kind de autogen al type que espera el endpoint.
  const _LLM_KINDS = {
    arquitectura: "architecture", hotspots: "hotspots", modulos: "modules",
    rutas: "routes", clases: "classes", estados: "states",
    secuencia: "sequence",
  };
  // El endpoint recibe `function` para secuencia; lo tomamos del input.
  async function generateWithLLM() {
    const p = getSelectedProject();
    if (!p) { toast("Elige un proyecto primero.", "warn"); return; }
    if (!p.indexed) { toast("El proyecto no está indexado en cbm.", "warn"); return; }
    const kind = _lastKind;
    const apiType = _LLM_KINDS[kind];
    if (!apiType) { toast("Primero generá un diagrama con algún botón (🏗️ Arquitectura, 🔥 Hotspots…).", "warn"); return; }
    const prompt = (document.getElementById('diag-llm-prompt')?.value || "").trim();
    const btn = document.getElementById('diag-llm-gen');
    const status = document.getElementById('diag-llm-status');
    const explain = document.getElementById('diag-llm-explain');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Generando…'; }
    if (status) { status.classList.remove('hidden'); status.textContent = 'Consultando cbm…'; }
    if (explain) explain.classList.add('hidden');
    try {
      const body = { type: apiType };
      if (prompt) body.prompt = prompt;
      if (apiType === 'sequence') {
        const fn = (document.getElementById('diag-seq-fn')?.value || _seqFn || "").trim();
        if (!fn) { toast("Escribe un nombre de función para el diagrama de secuencia.", "warn"); return; }
        body.function = fn;
      }
      if (status) status.textContent = 'LLM interpretando…';
      const r = await api(`projects/${encodeURIComponent(p.slug)}/diagrams/llm`,
        { method: "POST", body }, 90_000);
      if (!_state) return;
      _state.code = r.mermaid || '';
      _state.name = `${p.slug}-${kind}-llm`;
      const codeEl = document.getElementById('mermaid-code');
      if (codeEl) codeEl.value = _state.code;
      await renderDiagram();
      if (r.explanation) {
        if (explain) {
          explain.innerHTML = `<strong>💡 Interpretación del LLM</strong><br>`
            + escape(r.explanation).replace(/\n/g, '<br>');
          explain.classList.remove('hidden');
        }
      }
      const tok = r.tokens_in ? `${r.tokens_in}+${r.tokens_out || '?'} tok` : '';
      toast(`Diagrama generado con LLM${tok ? ` (${tok})` : ''}.`, "ok");
    } catch (e) {
      toast(`Error del LLM: ${e.message}`, "err");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = origText; }
      if (status) { status.classList.add('hidden'); status.textContent = ''; }
      updateAutoButtons();
    }
  }
  document.getElementById('diag-llm-gen').addEventListener('click', generateWithLLM);
  document.getElementById('diag-llm-prompt').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); generateWithLLM(); }
  });

  updateAutoButtons();

  // Carga inicial: mermaid (vendor local) + primer render + lista de proyectos.
  loadMermaid().catch((e) => {
    _state.error = 'No se pudo cargar mermaid.js: ' + (e && e.message || e);
    paintPreview();
  });
  renderDiagram();
  loadProjects();
}
