// Tab Skills (autoaprendizaje, 2026-07-12): borradores que destila el
// compactador (tabla skill_drafts) + skills instaladas (~/.copilot/skills).
// Flujo: revisar/editar el borrador en el modal → aprobar lo copia al
// directorio canónico (el SkillCache lo inyecta en el próximo push) →
// o rechazar/borrar. Nada se instala sin click humano.

import { $, $$, api, escape, fmtNum, _dbg, onClick } from "./api.js";
import { toast, openModal, closeModal, modalLoad, confirmModal } from "./ui.js";
import { dataTable } from "./ui-table.js";
import { registerPoller } from "./pollers.js";

export async function loadSkillsTab() {
  loadDrafts();
  loadInstalled();
  loadBudget();
  loadCatalog();
}

// Badge del sidebar: lo pisa refreshStatus (health cada 15s) y también
// cada load de esta tab, así el número no queda stale tras aprobar.
export function updateSkillsBadge(n) {
  const b = $("#skills-tab-badge");
  if (!b) return;
  b.textContent = String(n);
  b.classList.toggle("hidden", !n);
}

const STATUS_CLS = { pending: "warn", approved: "ok", rejected: "dim" };

// ------- borradores -------

let _tablaBorradores = null;
let _errorBorradores = "";

async function loadDrafts() {
  const status = $("#drafts-status").value;
  if (!_tablaBorradores) {
    _tablaBorradores = dataTable($("#drafts-table"), {
      searchPlaceholder: "filtrar borradores…",
      sort: { key: 3, dir: "desc" },  // más recientes primero
      columns: [
        { key: "name", label: "nombre",
          render: (d) => `<code>${escape(d.name)}</code>`,
          value: (d) => d.name || "" },
        { key: "description", label: "descripción",
          className: "max-w-md text-zinc-400",
          render: (d) => escape((d.description || "").slice(0, 120)),
          value: (d) => d.description || "" },
        { key: "project_slug", label: "proyecto",
          render: (d) => `<code>${escape(d.project_slug || "—")}</code>`,
          value: (d) => d.project_slug || "" },
        { key: "created_at", label: "creado",
          className: "whitespace-nowrap",
          render: (d) => escape(d.created_at || "—"),
          // ISO 8601 ordena bien lexicográficamente; si viniera otro
          // formato, esto igual lo deja "más o menos" cronológico.
          value: (d) => d.created_at || "" },
        { key: "status", label: "status",
          render: (d) =>
            `<span class="badge ${STATUS_CLS[d.status] || "dim"}">${
              escape(d.status)}</span>`,
          value: (d) => d.status || "" },
        { key: "_actions", label: "", sortable: false, className: "row-actions",
          render: (d) => {
            const canAct = d.status === "pending";
            return `<div class="row-actions">
              <div class="action-group">
                <button class="btn btn-xs" data-act="view" data-id="${d.id}"
                  title="ver / editar el borrador">Revisar</button>
              </div>
              ${canAct ? `<div class="action-group">
                <button class="btn btn-xs" data-act="approve" data-id="${d.id}"
                  title="copiar a ~/.copilot/skills">Aprobar</button>
                <button class="btn btn-xs danger" data-act="reject" data-id="${d.id}">Rechazar</button>
              </div>` : ""}
              <div class="action-group">
                <button class="btn btn-xs danger" data-act="delete" data-id="${d.id}"
                  title="purgar la fila (no toca la skill instalada)"
                  aria-label="purgar la fila"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9.5 7V4.8h5V7M6.5 7l0.9 12.2h9.2L17.5 7"/></svg></button>
              </div>
            </div>`;
          } },
      ],
      empty: () => (_errorBorradores
        ? { title: "No se pudieron leer los borradores", sub: _errorBorradores,
            action: `<button class="btn" data-drafts-retry>Reintentar</button>` }
        : { title: "Sin borradores", sub: "Se generan al cerrar "
           + "conversaciones cuando el compactador detecta un "
           + "procedimiento reusable." }),
    });
    _wireBorradores();
  }
  _errorBorradores = "";
  _tablaBorradores.setLoading(true);
  try {
    const r = await api("skill-drafts?limit=100"
      + (status ? `&status=${encodeURIComponent(status)}` : ""));
    updateSkillsBadge(r.pending ?? 0);
    const drafts = r.drafts || [];
    $("#drafts-summary").textContent =
      `${drafts.length} borradores · ${r.pending ?? 0} pendientes`;
    _tablaBorradores.setRows(drafts);
  } catch (e) {
    _errorBorradores = e.message;
    $("#drafts-summary").textContent = "error: " + e.message;
    _tablaBorradores.setRows([]);
  }
}

// Delegación sobre el wrapper. Los botones son por fila y dataTable
// repinta el <tbody> en cada sort/filter/page: los listeners atados a
// <tr> viejos morirían. Wireado al wrapper, captura los botones nuevos.
function _wireBorradores() {
  $("#drafts-table").addEventListener("click", (e) => {
    if (e.target.closest("[data-drafts-retry]")) { loadDrafts(); return; }
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const id = Number(b.dataset.id);
    const handler = {
      view: () => openDraftModal(id),
      approve: () => approveDraft(id),
      reject: () => rejectDraft(id),
      delete: () => deleteDraft(id),
    }[b.dataset.act];
    if (handler) handler();
  });
}

function openDraftModal(id) {
  modalLoad({
    id: "skill-modal",
    title: `Borrador #${id}`,
    loader: () => api(`skill-drafts/${id}`),
    render: ({ draft: d }) => {
      const pending = d.status === "pending";
      const meta = [
        ["status", `<span class="badge ${STATUS_CLS[d.status] || "dim"}">${escape(d.status)}</span>`],
        ["proyecto", `<code>${escape(d.project_slug || "—")}</code>`],
        ["conversación", `<code>${escape((d.source_conversation || "—").slice(0, 8))}</code>`],
        ["creado", escape(d.created_at || "—")],
      ];
      if (d.approved_path) {
        meta.push(["instalada en", `<code class="break-all">${escape(d.approved_path)}</code>`]);
      }
      const ro = pending ? "" : "disabled";
      return `<table class="status-detail mb-4"><tbody>${
        meta.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")
      }</tbody></table>
      <div class="grid gap-3">
        <label class="flex flex-col gap-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">Nombre (kebab-case; se sanitiza al aprobar)
          <input id="skill-edit-name" type="text" class="input font-mono"
                 value="${escape(d.name)}" ${ro}>
        </label>
        <label class="flex flex-col gap-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">Descripción (una línea: cuándo aplicarla)
          <input id="skill-edit-desc" type="text" class="input"
                 value="${escape(d.description)}" ${ro}>
        </label>
        <label class="flex flex-col gap-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">Contenido (markdown del SKILL.md)
          <textarea id="skill-edit-content" class="input w-full !min-h-[260px]"
                    ${ro}>${escape(d.content)}</textarea>
        </label>
      </div>`;
    },
    after: ({ draft: d }) => {
      const pending = d.status === "pending";
      $("#skill-modal-save").hidden = !pending;
      $("#skill-modal-approve").hidden = !pending;
      $("#skill-modal-reject").hidden = !pending;
      if (!pending) return;
      // El botón lo comparten borrador y skill instalada: reponer la
      // etiqueta, que la otra rama la pisa.
      $("#skill-modal-save").textContent = "Guardar borrador";
      onClick("#skill-modal-save", () => saveDraft(id, false));
      onClick("#skill-modal-approve", () => saveDraft(id, true));
      onClick("#skill-modal-reject", async () => {
        await rejectDraft(id);
        closeModal("skill-modal");
      });
    },
  });
}

async function saveDraft(id, approveAfter) {
  const msg = $("#skill-modal-msg");
  msg.textContent = "guardando…";
  try {
    await api(`skill-drafts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#skill-edit-name").value,
        description: $("#skill-edit-desc").value,
        content: $("#skill-edit-content").value,
      }),
    });
    msg.textContent = "guardado ✓";
    if (approveAfter) {
      await approveDraft(id, msg);
      closeModal("skill-modal");
    } else {
      loadDrafts();
    }
  } catch (e) {
    msg.textContent = "error: " + e.message;
  }
}

async function approveDraft(id, msgEl) {
  const post = (overwrite) => api(`skill-drafts/${id}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overwrite }),
  });
  try {
    let r;
    try {
      r = await post(false);
    } catch (e) {
      // 409 needs_overwrite: ya hay una skill con ese nombre.
      if (!/ya existe una skill/.test(e.message)) throw e;
      if (!await confirmModal({
        body: "Ya existe una skill instalada con ese nombre.\n\n"
          + "¿Sobrescribirla con este borrador?",
        confirmText: "Sobrescribir",
        danger: true,
      })) {
        if (msgEl) msgEl.textContent = "aprobación cancelada";
        return;
      }
      r = await post(true);
    }
    toast(`Skill "${r.name}" instalada ✓\n${r.path}\n\nEl próximo push ya la inyecta.`, "ok");
    loadDrafts();
    loadInstalled();
  } catch (e) {
    toast("No pude aprobar: " + e.message, "err");
    if (msgEl) msgEl.textContent = "error: " + e.message;
  }
}

async function rejectDraft(id) {
  try {
    await api(`skill-drafts/${id}/reject`, { method: "POST" });
    toast(`Borrador #${id} rechazado.`, "info");
    loadDrafts();
  } catch (e) {
    toast("No pude rechazar: " + e.message, "err");
  }
}

async function deleteDraft(id) {
  if (!await confirmModal({
    title: `Borrar borrador #${id}`,
    body: "Solo purga la fila; si ya se aprobó, la skill instalada queda "
      + "(se borra abajo).",
    confirmText: "Sí, borrar",
    danger: true,
  })) return;
  try {
    await api(`skill-drafts/${id}`, { method: "DELETE" });
    loadDrafts();
  } catch (e) {
    toast("No pude borrar: " + e.message, "err");
  }
}

// ------- instaladas -------

// Iconos de fila. Eran ✏️ y 🗑: un emoji no hereda el color del boton
// (el de borrar se veia igual que el de editar) ni se puede etiquetar.
const _svg = (d) =>
  `<svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
     aria-hidden="true">${d}</svg>`;
const I_EDIT = '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="M14.5 6.5l3 3"/>';
const I_BORRAR = '<path d="M4 7h16M9.5 7V4.8h5V7M6.5 7l0.9 12.2h9.2L17.5 7"/>';

const ORDEN_ESTADO = { auto: 0, manual: 1, off: 2 };

const COL_SKILLS = [
  { key: "state", label: "Estado",
    // Ordena por cuánto pesa en el prompt, no alfabético: inyectadas
    // primero es lo que uno quiere ver junto.
    value: (s) => ORDEN_ESTADO[s.state || (s.enabled ? (s.manual ? "manual" : "auto") : "off")],
    render: _stateSelect },
  { key: "name", label: "Nombre",
    render: (s) => `<code>${escape(s.name)}</code>${s.valid ? ""
      : ' <span class="badge err" title="frontmatter roto: no se inyecta">rota</span>'}` },
  { key: "description", label: "Descripción", className: "max-w-md text-zinc-400",
    render: (s) => escape((s.description || "").slice(0, 140)) },
  { key: "modified_at", label: "Modificada",
    className: "whitespace-nowrap text-zinc-400" },
  { key: "size", label: "Tamaño", className: "tabular-nums text-zinc-400",
    render: (s) => `${(s.size / 1024).toFixed(1)} KB` },
  { key: "bullet_tokens", label: "Tokens",
    // Las que no se inyectan no suman tokens: el valor de orden es 0,
    // no null, porque cero es un dato y no un "sin medir".
    value: (s) => (s.state === "auto" ? s.bullet_tokens || 0 : 0),
    className: "tabular-nums",
    render: (s) => s.state === "auto"
      ? `<span title="lo que suma al system prompt (bullet)">${s.bullet_tokens}</span>`
      : '<span class="text-zinc-600">—</span>' },
  { key: "dir", label: "", sortable: false, className: "cell-actions",
    render: (s) => `<div>
      <button class="icon-btn" data-act="view" data-dir="${escape(s.dir)}"
        title="ver y editar el SKILL.md de ${escape(s.name)}"
        aria-label="editar ${escape(s.name)}">${_svg(I_EDIT)}</button>
      <button class="icon-btn danger" data-act="delete" data-dir="${escape(s.dir)}"
        title="borrar la skill (el directorio entero)"
        aria-label="borrar ${escape(s.name)}">${_svg(I_BORRAR)}</button>
    </div>` },
];

let _tablaSkills = null;
let _errorSkills = "";

async function loadInstalled() {
  if (!_tablaSkills) {
    _tablaSkills = dataTable($("#skills-table"), {
      columns: COL_SKILLS,
      rows: [],
      searchPlaceholder: "filtrar skills…",
      sort: { key: 0, dir: "asc" },
      empty: () => (_errorSkills
        ? { title: "No se pudieron leer las skills", sub: _errorSkills,
            action: `<button class="btn" data-skills-retry>Reintentar</button>` }
        : { title: "No hay skills instaladas todavía",
            sub: "Instalá una desde GitHub arriba, aprobá un borrador, o "
               + "creá el directorio a mano." }),
    });
    _wireTablaSkills();
  }
  _errorSkills = "";
  _tablaSkills.setLoading(true);
  try {
    const r = await api("skills");
    $("#skills-dir").textContent = r.dir || "?";
    const skills = r.skills || [];
    const on = skills.filter((s) => s.enabled && !s.manual).length;
    $("#skills-summary").textContent = skills.length
      ? `${skills.length} instaladas · ${on} inyectadas`
      : "";
    _tablaSkills.setRows(skills);
  } catch (e) {
    _errorSkills = e.message;
    $("#skills-summary").textContent = "error: " + e.message;
    _tablaSkills.setRows([]);
  }
}

// Delegación sobre el contenedor: dataTable repinta el tbody al ordenar
// o filtrar, así que un listener por fila moriría en la segunda pintada.
function _wireTablaSkills() {
  const el = $("#skills-table");
  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-skills-retry]")) { loadInstalled(); return; }
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    if (b.dataset.act === "view") openInstalledModal(b.dataset.dir);
    else deleteInstalled(b.dataset.dir);
  });
  el.addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-dir]");
    if (sel) setSkillState(sel.dataset.dir, sel.value, sel);
  });
}

// Tres estados, no dos: los dos primeros son "prendida", y lo que cambia
// es CUÁNTO entra al system prompt (auto = name+description, manual =
// solo el nombre). Un select los deja elegir directo; antes había un
// toggle de dos posiciones y `manual` solo se podía poner editando el
// frontmatter a mano.
const STATES = [
  ["auto", "Inyectada", "name + description en el prompt de cada push"],
  ["manual", "On-demand", "solo el nombre (~3 tokens); el experto pide el cuerpo con read_skill"],
  ["off", "Apagada", "no se inyecta ni se ofrece; el archivo queda"],
];

function _stateSelect(s) {
  const cur = s.state || (s.enabled ? (s.manual ? "manual" : "auto") : "off");
  return `<select class="select select-xs select-state" data-state="${cur}"
    data-dir="${escape(s.dir)}" title="${escape(
      STATES.find(([v]) => v === cur)?.[2] || "")}">${
    STATES.map(([v, label]) =>
      `<option value="${v}"${v === cur ? " selected" : ""}>${label}</option>`
    ).join("")}</select>`;
}

async function setSkillState(dir, state, sel) {
  const prev = sel.dataset.state;
  sel.disabled = true;
  try {
    await api(`skills/${encodeURIComponent(dir)}`, {
      method: "PATCH", body: { state },
    });
    sel.dataset.state = state;
    loadInstalled();
    loadBudget();
  } catch (e) {
    sel.value = prev;                 // revertir: el disco no cambió
    toast("No pude cambiar el estado: " + e.message, "err");
  } finally {
    sel.disabled = false;
  }
}

// Editable desde 2026-07-26. Antes era un <pre> de solo lectura: el
// medidor de presupuesto te decía "recorta la descripción de esta skill"
// y la única forma de hacerlo era abrir el archivo en disco.
function openInstalledModal(dir) {
  modalLoad({
    id: "skill-modal",
    title: `Skill · ${dir}`,
    loader: () => api(`skills/${encodeURIComponent(dir)}`),
    render: (r) => `
      <p class="muted mb-2 text-xs">Se edita el <code>SKILL.md</code> entero,
        frontmatter incluido (<code>name</code>, <code>description</code>,
        <code>enabled</code>, <code>when</code>). Guardar deja un
        <code>.bak</code> y el próximo run ya lo usa.
        <kbd class="rounded bg-zinc-800 px-1">Ctrl</kbd>+<kbd
          class="rounded bg-zinc-800 px-1">S</kbd> guarda.</p>
      <textarea id="skill-edit-content" class="input w-full !min-h-[55vh] font-mono text-xs"
                spellcheck="false">${escape(r.content)}</textarea>`,
    after: () => {
      $("#skill-modal-approve").hidden = true;
      $("#skill-modal-reject").hidden = true;
      const save = $("#skill-modal-save");
      save.hidden = false;
      save.textContent = "Guardar SKILL.md";
      onClick("#skill-modal-save", () => saveInstalledSkill(dir));
      $("#skill-edit-content").addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
          e.preventDefault();
          saveInstalledSkill(dir);
        }
      });
    },
  });
}

async function saveInstalledSkill(dir) {
  const ta = $("#skill-edit-content");
  const msg = $("#skill-modal-msg");
  const btn = $("#skill-modal-save");
  if (!ta) return;
  btn.disabled = true;
  msg.textContent = "guardando…";
  try {
    const r = await api(`skills/${encodeURIComponent(dir)}`, {
      method: "PUT", body: { content: ta.value },
    });
    // El backend releé por el mismo camino que el runtime: si el
    // frontmatter quedó roto, la skill deja de inyectarse y hay que
    // decirlo acá y no en el próximo run.
    if (r.valid) {
      msg.textContent = `guardado ✓ · estado ${r.state}`
        + (r.bullet_tokens != null ? ` · ${r.bullet_tokens} tokens de índice` : "");
      toast(`Skill "${dir}" guardada. El próximo run ya la usa.`, "ok");
    } else {
      msg.textContent = "guardado, pero el frontmatter quedó roto: NO se inyecta";
      toast(`"${dir}": frontmatter inválido — la skill no se inyecta.`, "warn");
    }
    loadInstalled();
    loadBudget();
  } catch (e) {
    msg.textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function deleteInstalled(dir) {
  if (!await confirmModal({
    title: `Borrar skill "${dir}"`,
    body: "Se borra el directorio entero de ~/.copilot/skills. No hay "
      + "papelera; deja de inyectarse en el próximo push.",
    confirmText: "Sí, borrar",
    danger: true,
  })) return;
  try {
    await api(`skills/${encodeURIComponent(dir)}`, { method: "DELETE" });
    toast(`Skill "${dir}" borrada.`, "info");
    loadInstalled();
  } catch (e) {
    toast("No pude borrar: " + e.message, "err");
  }
}

// ------- presupuesto de tokens -------
// Lo que se paga en CADA push antes de escribir una palabra útil:
// ponytail (copilot-instructions.md) + índice de skills + fallback de
// tools. La barra es apilada y la línea punteada marca el umbral; si se
// pasa, el segmento de skills se pinta rojo (es el único que puedes
// recortar con un click).

const BUDGET_PARTS = [
  ["ponytail", "copilot-instructions.md"],
  ["skills", "índice de skills"],
  ["tool_fallback", "fallback de tools"],
];

// El bloque de skills se arma POR PROYECTO desde 2026-09-08 (las del
// repo entran siempre, `defaults_json.skills` filtra las globales), asi
// que un solo numero global ya no describe lo que recibe ningun proyecto
// en particular. El selector elige que se mide; `scope` en la respuesta
// dice que se midio, para no mostrar un numero sin decir de que es.
async function llenarSelectorDeProyectos() {
  const sel = $("#budget-project");
  if (!sel || sel.dataset.listo) return;
  try {
    const ps = await api("projects");
    const items = Array.isArray(ps) ? ps : (ps?.projects || []);
    for (const p of items) {
      const o = document.createElement("option");
      o.value = p.slug;
      o.textContent = p.slug;
      sel.appendChild(o);
    }
    sel.dataset.listo = "1";
  } catch (e) {
    // Sin la lista el selector queda solo con "(global)": se degrada al
    // comportamiento anterior en vez de dejar la card sin presupuesto.
    console.warn("budget: no pude listar proyectos", e);
  }
}

async function loadBudget() {
  const bar = $("#budget-bar");
  const slug = $("#budget-project")?.value || "";
  try {
    llenarSelectorDeProyectos();
    const b = await api(
      slug ? `skills/budget?project=${encodeURIComponent(slug)}` : "skills/budget");
    const total = b.total || 0;
    const budget = b.prompt_budget || 1;
    // Escala: el máximo entre lo usado y el umbral, así la marca del
    // umbral siempre entra en la barra.
    const scale = Math.max(total, budget);
    const over = total > budget;
    bar.classList.toggle("budget-over", over);
    bar.innerHTML = BUDGET_PARTS.map(([k, label]) => {
      const v = b.parts?.[k] || 0;
      const pct = (v / scale) * 100;
      return `<div class="budget-seg budget-seg-${k}" style="width:${pct}%"
        title="${escape(label)}: ${v} tokens">${pct > 12 ? v : ""}</div>`;
    }).join("")
      + `<div class="budget-seg budget-seg-free" style="flex:1"></div>`
      + `<div class="budget-threshold" style="left:${(budget / scale) * 100}%"
           title="umbral: ${budget} tokens"></div>`;

    const c = b.counts || {};
    $("#budget-summary").innerHTML =
      `<strong class="${over ? "fail" : "ok"}">${fmtNum(total)}</strong>`
      + ` / ${fmtNum(budget)} tokens por push`
      + ` · ${c.injected ?? 0} skills inyectadas`
      + (c.off ? ` · ${c.off} apagadas` : "")
      + (c.manual ? ` · ${c.manual} on-demand` : "")
      + (over ? " · <strong class=\"fail\">te pasaste del umbral</strong>" : "");

    const skillsTokens = b.parts?.skills || 0;
    const skillsOver = skillsTokens > (b.skills_budget || Infinity);
    $("#budget-legend").innerHTML = BUDGET_PARTS.map(([k, label]) =>
      `<span class="text-zinc-400"><span class="budget-dot budget-seg-${k}"></span>${
        escape(label)}: <span class="tabular-nums text-zinc-200">${
        fmtNum(b.parts?.[k] || 0)}</span></span>`).join("")
      + `<span class="${skillsOver ? "fail" : "muted"}">skills: ${
        fmtNum(skillsTokens)} / ${fmtNum(b.skills_budget)} del sub-umbral</span>`
      + `<span class="muted">medido en: <code>${
        escape(b.scope || "global")}</code></span>`;

    const top = b.top || [];
    $("#budget-top").innerHTML = !top.length ? "" :
      `<details><summary class="cursor-pointer text-xs text-zinc-400">
         Las que más pesan (recorta la descripción o apagalas)</summary>
       <ul class="mt-2 grid gap-1 text-xs">${top.map((s) =>
        `<li class="flex gap-2"><span class="tabular-nums w-10 text-right
          text-zinc-400">${s.tokens}</span>
         <code class="text-zinc-300">${escape(s.name)}</code></li>`).join("")}
       </ul></details>`;
  } catch (e) {
    $("#budget-summary").textContent = "error: " + e.message;
    bar.innerHTML = "";
  }
}

// ------- editor del copilot-instructions.md -------

async function openInstructions() {
  const msg = $("#instructions-msg");
  msg.textContent = "";
  $("#instructions-content").value = "cargando…";
  openModal("instructions-modal");
  try {
    const r = await api("instructions");
    $("#instructions-path").textContent = r.path || "?";
    const ta = $("#instructions-content");
    ta.value = r.content || "";
    $("#instructions-tokens").textContent = `~${r.tokens} tokens`;
    if (!ta.dataset.wired) {
      ta.dataset.wired = "1";
      ta.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
          e.preventDefault();
          saveInstructions();
        }
      });
    }
    if (!r.exists) {
      msg.textContent = "el archivo no existe todavía; guardar lo crea";
    }
  } catch (e) {
    $("#instructions-content").value = "";
    msg.textContent = "error: " + e.message;
  }
}

async function saveInstructions() {
  const msg = $("#instructions-msg");
  const btn = $("#instructions-save");
  btn.disabled = true;
  msg.textContent = "guardando…";
  try {
    const r = await api("instructions", {
      method: "PUT", body: { content: $("#instructions-content").value },
    });
    $("#instructions-tokens").textContent = `~${r.tokens} tokens`;
    msg.textContent = "guardado ✓ (backup en .md.bak)";
    toast("copilot-instructions.md guardado. El próximo run ya lo usa.", "ok");
    loadBudget();
  } catch (e) {
    msg.textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ------- alta desde GitHub -------

let _browseJob = null;   // {job_id, skills[]}
let _browsePoll = null;

async function loadCatalog() {
  const sel = $("#skills-catalog");
  if (sel.options.length > 1) return;   // ya cargado
  try {
    const r = await api("skills/catalog");
    for (const c of r.catalog || []) {
      const o = document.createElement("option");
      o.value = c.url;
      o.textContent = c.label;
      sel.appendChild(o);
    }
  } catch (e) { _dbg("catalog", e.message); }
}

async function browseStart() {
  const url = $("#skills-repo-url").value.trim();
  if (!url) { toast("Pega una URL de repo o elige una del dropdown.", "err"); return; }
  _browseStopPoll();
  $("#skills-browse-panel").hidden = false;
  $("#skills-browse-controls").hidden = true;
  $("#skills-browse-table").querySelector("tbody").innerHTML = "";
  $("#skills-browse-url").textContent = url;
  $("#skills-browse-msg").textContent = "clonando…";
  $("#skills-browse-btn").disabled = true;
  try {
    const job = await api("skills/browse", { method: "POST", body: { url } });
    _browseJob = job;
    _renderBrowse(job);
    _browsePoll = registerPoller(browsePoll, 1200, { tabId: "skills" });
  } catch (e) {
    $("#skills-browse-msg").textContent = "error: " + e.message;
  } finally {
    $("#skills-browse-btn").disabled = false;
  }
}

async function browsePoll() {
  if (!_browseJob) return _browseStopPoll();
  try {
    const job = await api(`skills/browse/${_browseJob.job_id}`);
    _browseJob = job;
    _renderBrowse(job);
    if (job.state === "ready" || job.state === "failed") _browseStopPoll();
  } catch (e) {
    $("#skills-browse-msg").textContent = "error: " + e.message;
    _browseStopPoll();
  }
}

function _browseStopPoll() {
  if (_browsePoll) { _browsePoll(); _browsePoll = null; }
}

function _renderBrowse(job) {
  const badge = $("#skills-browse-state");
  badge.textContent = job.state;
  badge.className = "badge " + ({ ready: "ok", failed: "err" }[job.state] || "warn");
  $("#skills-browse-commit").textContent = (job.commit || "").slice(0, 8);
  if (job.state === "failed") {
    $("#skills-browse-msg").innerHTML =
      `<span class="fail">${escape(job.error || "falló el clon")}</span>`;
    return;
  }
  if (job.state !== "ready") {
    $("#skills-browse-msg").textContent =
      job.state === "scanning" ? "buscando SKILL.md…" : "clonando…";
    return;
  }
  const skills = job.skills || [];
  $("#skills-browse-msg").textContent = skills.length
    ? `${skills.length} skills encontradas — marca las que quieras`
    : "no encontré ningún SKILL.md en este repo";
  $("#skills-browse-controls").hidden = !skills.length;
  _renderBrowseRows(skills);
}

function _renderBrowseRows(skills, filter = "") {
  const tbody = $("#skills-browse-table").querySelector("tbody");
  const f = filter.trim().toLowerCase();
  const rows = !f ? skills : skills.filter((s) =>
    (s.name + " " + s.description).toLowerCase().includes(f));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">sin coincidencias</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((s) => `<tr>
    <td><input type="checkbox" class="accent-emerald-500 skill-pick"
          data-path="${escape(s.rel_path)}" ${s.too_big ? "disabled" : ""}></td>
    <td><code>${escape(s.name)}</code>${s.valid ? ""
      : ' <span class="badge err" title="frontmatter roto">rota</span>'}</td>
    <td class="max-w-md text-zinc-400">${escape((s.description || "").slice(0, 160))}</td>
    <td><code class="text-[10px] text-zinc-400">${escape(s.rel_path)}</code></td>
    <td class="tabular-nums">${(s.size / 1024).toFixed(0)} KB${
      s.too_big ? ' <span class="badge err" title="demasiado pesada">✕</span>' : ""}</td>
    <td class="tabular-nums">${s.bullet_tokens}</td>
    <td class="cell-actions"><div>
      <button class="icon-btn" data-preview="${escape(s.rel_path)}"
        title="leer el SKILL.md antes de instalarlo"
        aria-label="leer el SKILL.md antes de instalarlo"><svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button></div></td>
  </tr>`).join("");
  tbody.querySelectorAll("button[data-preview]").forEach((b) => {
    b.onclick = () => previewFromClone(b.dataset.preview);
  });
}

function previewFromClone(relPath) {
  if (!_browseJob) return;
  modalLoad({
    id: "skill-modal",
    title: `Preview · ${relPath}`,
    loader: () => api(`skills/browse/${_browseJob.job_id}/preview`
      + `?path=${encodeURIComponent(relPath)}`),
    render: (r) => `<pre class="chat-md max-h-[60vh]">${escape(r.content)}</pre>`,
    after: () => {
      $("#skill-modal-save").hidden = true;
      $("#skill-modal-approve").hidden = true;
      $("#skill-modal-reject").hidden = true;
    },
  });
}

async function browseInstall() {
  if (!_browseJob) return;
  const paths = $$(".skill-pick").filter((c) => c.checked)
    .map((c) => c.dataset.path);
  if (!paths.length) { toast("No tildaste ninguna skill.", "err"); return; }
  const enabled = $("#skills-browse-enabled").checked;
  if (!await confirmModal({
    title: `Instalar ${paths.length} skill${paths.length === 1 ? "" : "s"}`,
    body: `Se copian a ~/.copilot/skills.\n\n`
      + (enabled
        ? "Entran PRENDIDAS: cada una suma su bullet al system prompt de "
          + "cada push. Mira el presupuesto después."
        : "Entran APAGADAS: quedan en disco pero no se inyectan hasta "
          + "que las prendas una por una."),
    confirmText: "Instalar",
  })) return;

  $("#skills-browse-install").disabled = true;
  $("#skills-browse-msg").textContent = "instalando…";
  try {
    const r = await api(`skills/browse/${_browseJob.job_id}/install`, {
      method: "POST",
      body: { paths, overwrite: $("#skills-browse-overwrite").checked, enabled },
    }, 60_000);
    const ok = (r.installed || []).length;
    const errs = r.errors || [];
    $("#skills-browse-msg").innerHTML = `${ok} instaladas`
      + (errs.length ? ` · <span class="fail">${errs.length} con error</span>` : "");
    if (errs.length) {
      toast(`${ok} instaladas, ${errs.length} fallaron:\n`
        + errs.map((e) => `· ${e.path}: ${e.error}`).join("\n"), "err", 9000);
    } else {
      toast(`${ok} skills instaladas ✓`, "ok");
    }
    loadInstalled();
    loadBudget();
  } catch (e) {
    $("#skills-browse-msg").innerHTML = `<span class="fail">${escape(e.message)}</span>`;
  } finally {
    $("#skills-browse-install").disabled = false;
  }
}

async function browseDiscard() {
  _browseStopPoll();
  const job = _browseJob;
  _browseJob = null;
  $("#skills-browse-panel").hidden = true;
  if (job) {
    try { await api(`skills/browse/${job.job_id}`, { method: "DELETE" }); }
    catch (e) { _dbg("discard", e.message); }
  }
}

export function initSkills() {
  onClick("#drafts-refresh", loadDrafts);
  $("#drafts-status").onchange = loadDrafts;
  onClick("#skills-refresh", () => { loadInstalled(); loadBudget(); });
  onClick("#budget-refresh", loadBudget);
  // Cambiar de proyecto recalcula: obligar a apretar "Recalcular"
  // despues de elegir deja el numero viejo bajo la etiqueta nueva.
  $("#budget-project")?.addEventListener("change", loadBudget);
  onClick("#instructions-edit", openInstructions);
  onClick("#instructions-save", saveInstructions);
  $("#skills-catalog").onchange = (e) => {
    if (e.target.value) $("#skills-repo-url").value = e.target.value;
  };
  onClick("#skills-browse-btn", browseStart);
  onClick("#skills-browse-close", browseDiscard);
  onClick("#skills-browse-install", browseInstall);
  $("#skills-browse-filter").oninput = (e) =>
    _renderBrowseRows(_browseJob?.skills || [], e.target.value);
  onClick("#skills-browse-all", () =>
    $$(".skill-pick").forEach((c) => { if (!c.disabled) c.checked = true; }));
  onClick("#skills-browse-none", () =>
    $$(".skill-pick").forEach((c) => { c.checked = false; }));
}
