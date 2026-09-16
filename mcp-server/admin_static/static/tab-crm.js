// Tab CRM: espejo de las empresas + deals + contactos del CRM local
// (trycompai/crm en localhost:3000). Read-only — la edición se hace allá.
// Sin credenciales: el relay le lee el Postgres directo.
//
// La cadena que pinta este tab: cliente → deal → proyecto → git → kanban.
// Un proyecto ES un deal, y el deal cuelga de una empresa.

import { $, api, escape, _dbg, onClick } from "./api.js";
import { toast, openModal } from "./ui.js";
import { registerPoller } from "./pollers.js";

// Cliente abierto en el panel de cadena (null = ninguno). Se guarda para
// poder refrescar el detalle después de vincular/desvincular sin perder
// dónde estaba parado el usuario.
let _openClientId = null;

// Estado vivo del último job (para polling + para refrescar la grilla
// sin volver a leer el CRM).
let _pollHandle = null;

// Dónde vive la app del CRM. Lo dice el relay en /crm/check para no
// hardcodear el host acá; hasta que responda, el default del compose.
let _crmBase = "http://localhost:3000/crm";

function _crmUrl(path) {
  return `${_crmBase}${path}`;
}

/** Link externo al CRM, con el ícono de "se abre afuera". */
function _extLink(href, text, cls = "link") {
  return `<a class="${cls}" href="${escape(href)}" target="_blank"
     rel="noopener" onclick="event.stopPropagation()">${escape(text)}
     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" class="inline-block h-3 w-3 align-[-1px]"
       ><path d="M7 17L17 7M9 7h8v8" stroke-linecap="round"
       stroke-linejoin="round"/></svg></a>`;
}

function _stopPolling() {
  if (_pollHandle) {
    _pollHandle();
    _pollHandle = null;
  }
}

/** Semáforo de conexión al CRM. Sin esto, "Sincronizar" falla sin decir
 *  por qué: lo más común es que el stack del CRM esté apagado. */
async function _checkConn() {
  const el = $("#crm-conn");
  try {
    const r = await api("crm/check");
    if (r.app_url) {
      _crmBase = `${r.app_url}/${r.workspace || "crm"}`;
      $("#crm-open").href = r.app_url;
    }
    // El Postgres del CRM vuelve solo con Docker, el dev server no: se
    // puede estar conectado y con la app apagada.
    el.innerHTML =
      `<span class="badge ok">CRM conectado</span>` +
      (r.app_up ? "" : ` <span class="badge warn">app apagada</span>`);
    $("#crm-start").hidden = !!r.app_up;
    $("#crm-summary").textContent =
      `${r.companies} empresas · ${r.contacts} contactos · ${r.deals} deals ` +
      `en el CRM`;
    return true;
  } catch (e) {
    el.innerHTML =
      `<span class="badge err">CRM no responde</span> ` +
      `<span class="muted">${escape(e.message)}</span>`;
    $("#crm-start").hidden = false;
    return false;
  }
}

// ---------- levantar el stack del CRM ----------
// El CRM es otro stack (Postgres en Docker + `bun run dev`) y hay que
// arrancarlo a mano después de cada reinicio — ver docs/CRM_LOCAL.md.

const _START_TIMEOUT_MS = 180_000;  // turbo + Next en frío tardan ~40s

export async function startCrm() {
  const btn = $("#crm-start");
  const el = $("#crm-status");
  btn.disabled = true;
  el.innerHTML = `<span class="badge warn">levantando el CRM…</span>`;
  try {
    const r = await api("crm/start", { method: "POST", body: "{}" }, 60_000);
    const notas = (r.notes || []).join(" · ");
    if (r.already_up) {
      el.innerHTML = `<span class="badge ok">el CRM ya estaba arriba</span>`;
      await _checkConn();
      btn.disabled = false;
      return;
    }
    // El POST vuelve apenas larga el proceso: la app tarda mucho más que
    // cualquier timeout de fetch razonable, así que se poll-ea el check.
    const deadline = Date.now() + _START_TIMEOUT_MS;
    while (Date.now() < deadline) {
      el.innerHTML =
        `<span class="badge warn">levantando el CRM…</span> ` +
        `<span class="muted">${escape(notas)}</span>`;
      await new Promise((res) => setTimeout(res, 3000));
      let up = false;
      try {
        up = !!(await api("crm/check")).app_up;
      } catch (e) {
        _dbg("crm start poll", e.message);  // el Postgres puede tardar
      }
      if (up) {
        toast("CRM arriba", "ok");
        el.innerHTML = `<span class="badge ok">CRM levantado</span>`;
        await _checkConn();
        loadCrm();
        btn.disabled = false;
        return;
      }
    }
    el.innerHTML =
      `<span class="badge err">no levantó en 3 minutos</span> ` +
      `<span class="muted">log: ${escape(r.log || "")}</span>`;
  } catch (e) {
    el.innerHTML =
      `<span class="badge err">no se pudo levantar</span> ` +
      `<pre class="fail">${escape(e.message)}</pre>`;
  }
  btn.disabled = false;
}

async function _pollStatus(jobId) {
  try {
    const r = await api(`crm/sync/status?job_id=${encodeURIComponent(jobId)}`);
    const el = $("#crm-status");
    if (r.status === "running") {
      el.innerHTML = `<span class="badge warn">sincronizando…</span>`;
      return;
    }
    if (r.status === "ok") {
      _stopPolling();
      const bajas = (r.removed || r.gone)
        ? ` · ${r.removed || 0} dados de baja` +
          (r.gone ? `, ${r.gone} marcados` : "")
        : "";
      el.innerHTML =
        `<span class="badge ok">sync ok</span> ` +
        `<span class="muted">${r.companies ?? 0} empresas, ` +
        `${r.deals ?? 0} deals, ${r.contacts ?? 0} contactos${escape(bajas)}</span>`;
      loadCrm();
      _checkConn();
      return;
    }
    _stopPolling();
    el.innerHTML =
      `<span class="badge err">sync error</span> ` +
      `<pre class="fail">${escape(r.error || "(sin mensaje)")}</pre>`;
  } catch (e) {
    _dbg("crm poll ERROR", e.message);
  }
}

// ---------- grilla de clientes ----------

function _initials(name) {
  const clean = (name || "").replace(/^www\./, "");
  const parts = clean.split(/[\s.\-_]+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

// Color estable por nombre: mismo cliente, mismo color en cada render.
const _TONES = [
  "bg-emerald-500/15 text-emerald-300",
  "bg-sky-500/15 text-sky-300",
  "bg-amber-500/15 text-amber-300",
  "bg-violet-500/15 text-violet-300",
  "bg-rose-500/15 text-rose-300",
  "bg-teal-500/15 text-teal-300",
];

function _tone(name) {
  let hash = 0;
  for (const ch of name || "") hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return _TONES[hash % _TONES.length];
}

/** Etapa de deal → color del badge. Las cerradas mandan señal fuerte. */
function _stageBadge(stage) {
  if (!stage) return "dim";
  if (stage === "CLOSED_WON") return "ok";
  if (stage === "CLOSED_LOST") return "err";
  if (stage === "CONTRACT_SENT" || stage === "DECISION_MAKER_BOUGHT_IN") {
    return "info";
  }
  return "warn";
}

function _stageLabel(stage) {
  return (stage || "").replace(/_/g, " ").toLowerCase();
}

function _money(amount) {
  const n = Number(amount) || 0;
  return n ? `$${n.toLocaleString("es-CL", { maximumFractionDigits: 0 })}` : "";
}

// Mismo umbral que CRM_STALE_DAYS en crm.py (default). Acá es solo para
// el badge barato de la lista, calculado en el cliente sin pegarle a
// GitHub; el número real (con issues/PRs) sale del digest.
const _STALE_DAYS = 14;

function _daysSince(iso) {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  return Math.max(0, Math.floor(ms / 86400000));
}

/** Badge de silencio para clientes con proyecto vinculado. `null` de
 *  `_daysSince` (nunca hubo actividad) es la señal más fuerte, no un 0. */
function _silenceBadge(c) {
  if (!c.project_count) return "";
  const days = _daysSince(c.last_activity_at);
  if (days === null) {
    return `<span class="badge err" title="Sin actividad registrada con este cliente">sin contacto</span>`;
  }
  if (days >= _STALE_DAYS) {
    return `<span class="badge err" title="Último email o reunión hace ${days} días">${days}d sin hablar</span>`;
  }
  return "";
}

/** Una fila de cliente. Columnas fijas para que el ojo baje comparando:
 *  identidad | etapas | deals | proyectos | monto | link. */
function _clientRow(c) {
  const deals = c.deals || [];
  const total = deals.reduce((acc, d) => acc + (Number(d.amount) || 0), 0);
  const gone = c.last_sync_status === "gone";

  // Hasta 2 etapas distintas; el resto se resume en "+N" para que la
  // columna no empuje a las de la derecha.
  const stages = [...new Set(deals.map((d) => d.stage).filter(Boolean))];
  const chips = stages.slice(0, 2)
    .map((s) => `<span class="badge ${_stageBadge(s)}">${escape(_stageLabel(s))}</span>`)
    .join(" ") +
    (stages.length > 2
      ? ` <span class="badge dim">+${stages.length - 2}</span>` : "");

  return `<div class="crm-row group flex cursor-pointer items-center gap-3 px-4 py-2.5 transition-colors hover:bg-zinc-800/40"
      data-id="${escape(String(c.id))}"
      title="Ver proyectos, git y tablero de este cliente">

    <div class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[10px] font-bold ${_tone(c.name)}"
      >${escape(_initials(c.name))}</div>

    <div class="min-w-0 flex-1">
      <div class="truncate text-sm font-medium text-zinc-100">${escape(c.name || "")}</div>
      ${c.domain && c.domain !== c.name
        ? `<div class="truncate text-xs text-zinc-500">${escape(c.domain)}</div>` : ""}
    </div>

    <div class="hidden w-44 shrink-0 items-center gap-1 md:flex">
      ${chips || `<span class="text-xs text-zinc-600">—</span>`}
      ${gone ? `<span class="badge warn" title="Borrado en el CRM, pero tiene proyectos vinculados acá">huérfano</span>` : ""}
      ${_silenceBadge(c)}
    </div>

    <div class="w-16 shrink-0 text-right text-xs tabular-nums ${deals.length ? "text-zinc-300" : "text-zinc-600"}"
      >${deals.length} deal${deals.length === 1 ? "" : "s"}</div>

    <div class="w-20 shrink-0 text-right text-xs tabular-nums ${c.project_count ? "text-emerald-300" : "text-zinc-600"}"
      >${c.project_count || 0} proy.</div>

    <div class="w-24 shrink-0 text-right text-sm tabular-nums ${total ? "text-zinc-200" : "text-zinc-700"}"
      >${total ? _money(total) : "—"}</div>

    <div class="w-5 shrink-0 text-right">
      <a class="text-zinc-600 opacity-0 transition-opacity hover:text-emerald-300 group-hover:opacity-100"
         href="${escape(_crmUrl(`/companies/${c.ext_id}`))}" target="_blank"
         rel="noopener" title="Abrir en el CRM"
         onclick="event.stopPropagation()"
        ><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" class="h-3.5 w-3.5"><path d="M7 17L17 7M9 7h8v8"
          stroke-linecap="round" stroke-linejoin="round"/></svg></a>
    </div>
  </div>`;
}

export async function loadCrm() {
  // El semáforo va acá y no en `initCrm`: se necesita cuando MIRÁS el
  // tab, no cuando se carga el admin.
  _checkConn();
  try {
    const r = await api("crm/clients");
    const rows = r.clients || [];
    const grid = $("#crm-grid");
    if (!rows.length) {
      grid.innerHTML = "";
      $("#crm-empty").hidden = false;
      return;
    }
    $("#crm-empty").hidden = true;
    // Primero los que tienen proyectos: son los clientes que importan
    // para cruzar con el trabajo.
    const sorted = [...rows].sort((a, b) =>
      (b.project_count || 0) - (a.project_count || 0) ||
      (a.name || "").localeCompare(b.name || ""));
    grid.innerHTML = sorted.map(_clientRow).join("");
    grid.querySelectorAll(".crm-row").forEach((el) => {
      el.onclick = () => openClient(Number(el.dataset.id));
    });
    if (_openClientId !== null && !$("#crm-detail").innerHTML) {
      openClient(_openClientId);
    }
  } catch (e) {
    _dbg("loadCrm ERROR", e.message);
    $("#crm-summary").textContent = "error: " + e.message;
  }
}

// ---------- cadena: cliente → deal → proyecto → git → kanban ----------

function _closeDetail() {
  _openClientId = null;
  const el = $("#crm-detail");
  el.classList.remove("open");   // display por clase, nunca [hidden]
  el.innerHTML = "";
  // Sacar el resaltado de la fila que estaba abierta.
  document.querySelectorAll(".crm-row.is-open")
    .forEach((r) => r.classList.remove("is-open", "bg-zinc-800/60"));
}

/** Selector de deal de un proyecto. Un proyecto ES un deal: el <select>
 *  lista los deals del cliente y "—" desvincula. */
function _dealPicker(p, deals) {
  const opts = [`<option value="">— sin deal —</option>`]
    .concat(deals.map((d) => {
      const sel = String(d.id) === String(p.deal_id || "") ? " selected" : "";
      return `<option value="${escape(String(d.id))}"${sel}
              >${escape(d.name || d.id)}</option>`;
    }))
    .join("");
  return `<select class="select select-xs crm-deal" data-slug="${escape(p.slug)}"
          >${opts}</select>`;
}

/** Fila de un proyecto dentro del panel de cadena. */
function _projectRow(p, deals) {
  const gp = p.github_project;
  const kanban = gp
    ? _extLink(gp.url || `https://github.com/orgs/${gp.owner}/projects/${gp.number}`,
               `tablero #${gp.number}`, "text-xs text-zinc-400 hover:text-emerald-300")
    : `<span class="badge dim" title="Vinculá un tablero desde el tab Proyectos">sin tablero</span>`;
  const git = p.has_git
    ? `<span class="badge ok" title="${escape(p.git_remote_url || p.repo_path || "")}">git</span>`
    : `<span class="badge dim">sin git</span>`;
  const deal = p.deal_id
    ? `<div class="mt-1">${_extLink(_crmUrl(`/deals/${p.deal_id}`),
        p.deal_name || p.deal_id, "text-[11px] text-zinc-500 hover:text-emerald-300")}</div>`
    : "";
  return `<tr data-slug="${escape(p.slug)}">
    <td><strong class="text-zinc-100">${escape(p.name || p.slug)}</strong>
        <div class="truncate text-xs text-zinc-500">${escape(p.repo_path || "")}</div></td>
    <td>${_dealPicker(p, deals)}${deal}</td>
    <td>${git}</td>
    <td>${kanban}</td>
    <td class="crm-gh muted" data-slug="${escape(p.slug)}">…</td>
    <td><button class="btn btn-xs crm-unlink" data-slug="${escape(p.slug)}"
        >desvincular</button></td>
  </tr>`;
}

/** Issues y PRs abiertos por proyecto. Se piden DESPUÉS del render.
 *
 * El endpoint de GitHub hace spawn de `gh` (con caché de 60s); pedirlos
 * en serie antes de pintar dejaría el panel en blanco varios segundos.
 * Cada celda se rellena cuando llega su respuesta.
 */
async function _fillGithubCounts(slugs) {
  await Promise.all(slugs.map(async (slug) => {
    const cell = document.querySelector(`.crm-gh[data-slug="${CSS.escape(slug)}"]`);
    if (!cell) return;
    try {
      const g = await api(`projects/${encodeURIComponent(slug)}/github`);
      if (!g.configured) {
        cell.innerHTML = `<span class="badge dim">sin repo/gh</span>`;
        return;
      }
      const ni = (g.issues || []).length;
      const np = (g.pulls || []).length;
      cell.innerHTML =
        `<span class="badge ${ni ? "warn" : "dim"}">${ni} issues</span> ` +
        `<span class="badge ${np ? "ok" : "dim"}">${np} PR</span>`;
    } catch (e) {
      cell.innerHTML = `<span class="badge err" title="${escape(e.message)}">error</span>`;
    }
  }));
}

/** Desplegable para vincular un proyecto que todavía no tiene cliente. */
function _linkPicker(allProjects, clientId) {
  const free = allProjects.filter((p) => !p.client_id);
  if (!free.length) {
    return `<p class="mt-3 text-xs text-zinc-500">Todos los proyectos ya
            tienen cliente asignado.</p>`;
  }
  const opts = free
    .map((p) => `<option value="${escape(p.slug)}">${escape(p.name || p.slug)}</option>`)
    .join("");
  return `<div class="mt-4 flex flex-wrap items-center gap-2 border-t border-zinc-800 pt-4">
    <span class="text-xs text-zinc-500">Sumar un proyecto a este cliente:</span>
    <select id="crm-link-slug" class="select select-xs">${opts}</select>
    <button id="crm-link-btn" class="btn btn-xs"
            data-client="${escape(String(clientId))}">Vincular</button>
  </div>`;
}

export async function openClient(clientId) {
  _openClientId = clientId;
  const el = $("#crm-detail");
  el.classList.add("open");
  el.innerHTML = `<div class="side-panel-body"><p class="muted">cargando…</p></div>`;
  // Marcar en la lista cuál está abierto: el panel no tapa la grilla, así
  // que sin esto se pierde de vista de quién es el detalle.
  document.querySelectorAll(".crm-row.is-open")
    .forEach((r) => r.classList.remove("is-open", "bg-zinc-800/60"));
  const row = document.querySelector(`.crm-row[data-id="${CSS.escape(String(clientId))}"]`);
  if (row) row.classList.add("is-open", "bg-zinc-800/60");
  try {
    const [detail, projResp] = await Promise.all([
      api(`crm/clients/${encodeURIComponent(clientId)}`),
      api("projects"),
    ]);
    const c = detail.client || {};
    const projects = detail.projects || [];
    const contacts = c.contacts || [];
    const deals = c.deals || [];

    const rows = projects.length
      ? `<div class="mt-4 overflow-x-auto"><table class="w-full"><thead><tr>
           <th class="th">Proyecto</th><th class="th">Deal</th>
           <th class="th">Git</th><th class="th">Kanban</th>
           <th class="th">Issues / PR</th><th class="th"></th>
         </tr></thead><tbody>
         ${projects.map((p) => _projectRow(p, deals)).join("")}
         </tbody></table></div>`
      : `<p class="mt-4 text-sm text-zinc-500">Este cliente todavía no tiene
         proyectos vinculados.</p>`;

    // Deals sin proyecto: trabajo vendido que nadie está haciendo, o un
    // proyecto que falta vincular. Vale la pena que se vea.
    const usados = new Set(projects.map((p) => String(p.deal_id || "")));
    const sueltos = deals.filter((d) => !usados.has(String(d.id)));
    const sueltosHtml = sueltos.length
      ? `<div class="mt-4 border-t border-zinc-800 pt-3">
           <div class="text-xs text-zinc-500">Deals sin proyecto vinculado</div>
           <div class="mt-2 flex flex-wrap gap-2">
             ${sueltos.map((d) => `<span class="badge ${_stageBadge(d.stage)}"
               >${escape(d.name || d.id)}</span>`).join("")}
           </div>
         </div>`
      : "";

    el.innerHTML = `
      <div class="side-panel-header">
        <div class="flex min-w-0 items-start gap-3">
          <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-sm font-bold ${_tone(c.name)}"
            >${escape(_initials(c.name))}</div>
          <div class="min-w-0">
            <h3 class="truncate text-base font-semibold text-zinc-100">${escape(c.name || "")}</h3>
            <p class="truncate text-xs text-zinc-500">${escape(c.domain || "")} ·
              ${contacts.length} contactos · ${deals.length} deals ·
              ${projects.length} proyectos</p>
          </div>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          ${_extLink(_crmUrl(`/companies/${c.ext_id}`), "Ver en el CRM", "btn btn-xs")}
          <button id="crm-detail-close" class="btn btn-xs" title="Cerrar (Esc)"
            aria-label="Cerrar (Esc)"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
        </div>
      </div>
      <div class="side-panel-body">
        ${rows}
        ${sueltosHtml}
        ${_linkPicker(projResp.projects || [], clientId)}
      </div>`;

    onClick("#crm-detail-close", _closeDetail);

    el.querySelectorAll(".crm-deal").forEach((sel) => {
      sel.onchange = async () => {
        const slug = sel.dataset.slug;
        sel.disabled = true;
        try {
          const r = await api(`projects/${encodeURIComponent(slug)}/deal`,
                    { method: "PUT",
                      body: JSON.stringify({ deal_id: sel.value || null }) });
          toast(r.deal_id ? `${slug} → ${r.deal_name || r.deal_id}`
                          : `${slug} sin deal`, "ok");
          await openClient(clientId);
        } catch (e) {
          toast("no se pudo vincular el deal: " + e.message, "err");
          sel.disabled = false;
        }
      };
    });

    el.querySelectorAll(".crm-unlink").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api(`projects/${encodeURIComponent(b.dataset.slug)}/client`,
                    { method: "PUT", body: JSON.stringify({ client_id: null }) });
          toast(`${b.dataset.slug} desvinculado`, "ok");
          await openClient(clientId);
          loadCrm();
        } catch (e) {
          toast("no se pudo desvincular: " + e.message, "err");
          b.disabled = false;
        }
      };
    });

    const linkBtn = $("#crm-link-btn");
    onClick("#crm-link-btn", async () => {
      const slug = $("#crm-link-slug").value;
      linkBtn.disabled = true;
      try {
        await api(`projects/${encodeURIComponent(slug)}/client`,
                  { method: "PUT", body: JSON.stringify({ client_id: clientId }) });
        toast(`${slug} vinculado a ${c.name || "el cliente"}`, "ok");
        await openClient(clientId);
        loadCrm();
      } catch (e) {
        toast("no se pudo vincular: " + e.message, "err");
        linkBtn.disabled = false;
      }
    });

    _fillGithubCounts(projects.map((p) => p.slug));
  } catch (e) {
    _dbg("openClient ERROR", e.message);
    el.innerHTML = `<pre class="fail">${escape(e.message)}</pre>`;
  }
}

export async function syncCrm() {
  $("#crm-status").innerHTML = `<span class="badge warn">arrancando…</span>`;
  try {
    const r = await fetch("/admin/api/crm/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`HTTP ${r.status}: ${t.slice(0, 200)}`);
    }
    const { job_id: jobId } = await r.json();
    _stopPolling();
    _pollHandle = registerPoller(() => _pollStatus(jobId), 1500, { tabId: "crm" });
    _pollStatus(jobId);
  } catch (e) {
    $("#crm-status").innerHTML =
      `<span class="badge err">error</span> ` +
      `<pre class="fail">${escape(e.message)}</pre>`;
  }
}

// ---------- digest: silencio + resumen, on-demand a Discord ----------
// Ver docs/CRM_DIGEST.md — este botón es la vía UI de la misma acción
// que documenta el curl de POST /admin/api/crm/digest.

/** Texto del digest → HTML. Los **bold** son sintaxis para Discord; acá
 *  se muestran igual (sin parsear markdown) para que lo que se ve en el
 *  modal sea EXACTAMENTE lo que se manda, sin sorpresas de formato. */
function _digestPreview() {
  const body = $("#crm-digest-body");
  const msg = $("#crm-digest-msg");
  $("#crm-digest-send").disabled = true;
  body.innerHTML = `<p class="muted">generando preview…</p>`;
  msg.textContent = "";
  // 30s: recorre TODOS los proyectos de TODOS los clientes vinculados
  // (un spawn de `gh` por repo); con pocos clientes es rápido, pero no
  // hay que pisar el timeout default de 15s a medida que crezca.
  api("crm/digest", { method: "POST", body: JSON.stringify({ dry_run: true }) }, 30_000)
    .then((r) => {
      body.innerHTML = `<pre class="whitespace-pre-wrap text-sm">${escape(r.text)}</pre>`;
      msg.textContent =
        `${r.client_count} clientes · ${r.stale_count} sin contacto hace ` +
        `${r.stale_days}+ días · se enviaría a ${r.channel}`;
      $("#crm-digest-send").disabled = false;
      $("#crm-digest-send").dataset.channel = r.channel;
    })
    .catch((e) => {
      body.innerHTML = `<pre class="fail">${escape(e.message)}</pre>`;
    });
}

async function _digestSend() {
  const btn = $("#crm-digest-send");
  btn.disabled = true;
  try {
    const r = await api("crm/digest", { method: "POST", body: JSON.stringify({}) }, 30_000);
    if (r.sent) {
      toast(`Digest enviado a ${r.channel}`, "ok");
      $("#crm-digest-msg").textContent = `✓ enviado a ${r.channel}`;
    } else {
      toast("El bot no confirmó el envío (¿está corriendo?)", "err");
      btn.disabled = false;
    }
  } catch (e) {
    toast("no se pudo enviar: " + e.message, "err");
    btn.disabled = false;
  }
}

export function initCrm() {
  onClick("#crm-sync", syncCrm);
  onClick("#crm-start", startCrm);
  onClick("#crm-digest-btn", () => {
    openModal("crm-digest-modal");
    _digestPreview();
  });
  onClick("#crm-digest-refresh", _digestPreview);
  onClick("#crm-digest-send", _digestSend);
  // Escape cierra el panel lateral, como los modales del admin.
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && $("#crm-detail").classList.contains("open")) {
      _closeDetail();
    }
  });
  // `init*` solo cablea; los datos los pide `loadCrm`, que main.js ya
  // llama al abrir la pestaña. Pedirlos también acá hacía que CADA carga
  // del admin pegara al Postgres del CRM aunque estuvieras en Chat — y
  // con el stack del CRM apagado (lo normal, se levanta a mano) eso
  // dejaba un 503 rojo en la consola en cada refresh, mezclado con
  // errores de verdad.
}
