// Tab Config: system_config en SQLite (red, paths, timeouts).

import { $, $$, api, on, onClick, escape } from "./api.js";
import { statCell, confirmModal, toast } from "./ui.js";
import { refreshStatus } from "./tab-status.js";

// Roles del runner por etapas. El orden es el del turno real: primero
// se planifica, después se ejecuta, después se verifica y se documenta.
const ROLES = ["executor", "planner", "verifier", "documenter", "compactor"];
// Mapa rol → clave de system_config. El backend lo manda en cada GET
// (`model_roles.keys`) y pisa esto, que es solo el piso: sin él, un GET
// que falle dejaría el PUT mandando claves `undefined` y el backend lo
// rechazaría entero.
let ROLE_KEYS = {
  executor: "FOURBIS_MODEL",
  planner: "FOURBIS_PLANNER_MODEL",
  verifier: "FOURBIS_VERIFIER_MODEL",
  documenter: "FOURBIS_DOCUMENTER_MODEL",
  compactor: "FOURBIS_COMPACTOR_MODEL",
};

// Deja separado el catálogo de las opciones que se muestran: un modelo
// guardado fuera del catálogo debe conservarse para no borrarlo al guardar.
export function roleModelState(cfg, modelos, rol, catalogAvailable = true) {
  const roles = cfg.model_roles || { keys: {}, effective: {} };
  const key = roles.keys?.[rol] || ROLE_KEYS[rol];
  const guardado = (cfg.config?.[key] || "").trim();
  const catalogSpecs = modelos.map((m) => m.spec);
  const specs = [...catalogSpecs];
  if (guardado && !specs.includes(guardado)) specs.push(guardado);
  const efectivo = roles.effective?.[rol] || "?";
  return {
    key, guardado, specs, efectivo,
    apagado: catalogAvailable && efectivo !== "?" && !catalogSpecs.includes(efectivo),
  };
}

// Los selects se llenan con el catálogo de modelos PRENDIDOS (el mismo
// que come el selector del chat), no con texto libre: un spec tipeado a
// mano que no existe deja el rol tirando ModelUnavailable en cada run.
async function fillRoleSelects(cfg) {
  let modelos = [];
  let catalogAvailable = true;
  try {
    modelos = (await api("models")).models || [];
  } catch { catalogAvailable = false; }
  const roles = cfg.model_roles || { keys: {}, effective: {} };
  if (roles.keys && Object.keys(roles.keys).length) {
    ROLE_KEYS = { ...ROLE_KEYS, ...roles.keys };
  }
  for (const rol of ROLES) {
    const sel = $(`#cfg-role-${rol}`);
    if (!sel) continue;
    const { guardado, specs, efectivo, apagado } = roleModelState(
      cfg, modelos, rol, catalogAvailable);
    // El valor guardado va como opción aunque el catálogo no conteste o
    // el modelo se haya apagado después: si no, el select se abre en
    // "(cascada)" y guardar borraría la config sin que nadie lo pidiera.
    sel.innerHTML = `<option value="">(cascada)</option>` + specs.map((sp) => {
      const m = modelos.find((x) => x.spec === sp);
      const etiqueta = m ? `${m.label || m.spec}` : `${sp} (fuera del catálogo)`;
      const sel_ = sp === guardado ? " selected" : "";
      return `<option value="${escape(sp)}"${sel_}>${escape(etiqueta)}</option>`;
    }).join("");
    // El efectivo puede ser un modelo que NO está en el desplegable: el
    // .env apunta a uno apagado en el catálogo (caso real: documentador
    // y compactador en nemotron-3-super, enabled=0). Sin el aviso, el
    // form muestra un valor que no se puede elegir ni volver a guardar.
    const eff = $(`#cfg-role-${rol}-eff`);
    if (eff) {
      eff.textContent = `→ base global: ${efectivo}`
        + (apagado ? " ⚠ apagado en el catálogo" : "");
    }
  }
}

export async function loadConfig() {
  try {
    const c = await api("config");
    $("#cfg-relay-host").value = c.config.RELAY_HOST;
    $("#cfg-repos-root").value = c.config.FOURBIS_REPOS_ROOT || "";
    // Seguimiento GitHub (ADR-036). El número se elige de un desplegable
    // que se llena con `gh project list`, pero el valor guardado tiene que
    // sobrevivir aunque `gh` no conteste: lo metemos como única opción
    // hasta que el usuario toque "Buscar tableros".
    const owner = c.config.GITHUB_BOARD_OWNER || "";
    const number = c.config.GITHUB_BOARD_NUMBER || "";
    $("#cfg-gh-owner").value = owner;
    const sel = $("#cfg-gh-number");
    sel.innerHTML = `<option value="">(sin configurar)</option>`
      + (number ? `<option value="${escape(number)}" selected>#${escape(number)}</option>` : "");
    // Guild de Discord: habilita el link al canal en el tab Proyectos.
    $("#cfg-discord-guild").value = c.config.DISCORD_GUILD_ID || "";
    // Tarifas por modelo. Se re-indenta para que sea legible en el
    // textarea: el backend lo guarda compacto tras validarlo.
    const rawPrices = c.config.MODEL_PRICES || "";
    let pretty = rawPrices;
    try {
      if (rawPrices) pretty = JSON.stringify(JSON.parse(rawPrices), null, 2);
    } catch { /* si está roto se muestra crudo para poder arreglarlo */ }
    $("#cfg-model-prices").value = pretty;
    await fillRoleSelects(c);
    const eff = c.effective;
    $("#cfg-host-effective").textContent =
      `bind actual: ${eff.bind_host}` +
      (eff.localhost_guard_active ? " · guard localhost ACTIVO" : "");
    $("#cfg-effective-grid").innerHTML = [
      statCell(escape(eff.bind_host), "bind actual"),
      statCell(eff.localhost_guard_active ? "ON" : "off", "guard localhost"),
      statCell(escape(eff.repos_root), "repos root efectivo", "!text-sm break-all"),
      statCell(escape(eff.version), "versión relay"),
    ].join("");
    updateLanWarning();
  } catch (e) {
    $("#cfg-msg").textContent = "error: " + e.message;
  }
}

function updateLanWarning() {
  $("#cfg-lan-warning").hidden = $("#cfg-relay-host").value !== "0.0.0.0";
}

async function saveConfig() {
  const host = $("#cfg-relay-host").value;
  const root = $("#cfg-repos-root").value.trim();
  if (host === "0.0.0.0" && !await confirmModal({
    title: "Exponer el relay a la LAN",
    body: "⚠ Vas a exponer el relay a la LAN.\n\n" +
    "/experts/run y /commands/*/run ejecutan procesos en esta máquina " +
    "y NO tienen auth (ADR-002). /admin/* queda protegido por el guard " +
    "de localhost, el resto no.\n\n¿Seguro?",
    confirmText: "Sí, exponer",
    danger: true,
  })) {
    return;
  }
  $("#cfg-save").disabled = true;
  $("#cfg-msg").textContent = "guardando...";
  try {
    const r = await api("config", {
      method: "PUT",
      body: JSON.stringify({
        RELAY_HOST: host, FOURBIS_REPOS_ROOT: root,
        GITHUB_BOARD_OWNER: $("#cfg-gh-owner").value.trim(),
        GITHUB_BOARD_NUMBER: $("#cfg-gh-number").value,
        DISCORD_GUILD_ID: $("#cfg-discord-guild").value.trim(),
        MODEL_PRICES: $("#cfg-model-prices").value.trim(),
        ...Object.fromEntries(ROLES.map((rol) => [
          ROLE_KEYS[rol], $(`#cfg-role-${rol}`)?.value ?? "",
        ])),
      }),
    });
    $("#cfg-msg").textContent = "guardado ✓";
    // Los roles resuelven de nuevo al guardar (el efectivo cambia
    // aunque el select no: apagar un override lo baja a la cascada).
    await loadConfig();
    $("#cfg-restart-note").hidden = !r.restart_required;
  } catch (e) {
    $("#cfg-msg").textContent = "error: " + e.message;
  } finally {
    $("#cfg-save").disabled = false;
  }
}

// Los dos timeouts salen de `config/timeouts`, NO de `config`.
//
// Leian de `api("config")`, que no trae ninguna de las dos claves: el
// campo de expert quedaba en `undefined` (input vacio + un warning del
// browser, "the specified value undefined cannot be parsed") y el de
// tool caia siempre al 60 hardcodeado del `??`, aunque el efectivo
// fuera otro. O sea: los dos campos mostraban algo que no era la
// config. El endpoint correcto devuelve el valor RESUELTO (override de
// system_config, si no el del env) y ademas su default, que sirve para
// decir de donde sale.
export async function loadExpertTimeout() {
  try {
    const t = await api("config/timeouts");
    $("#cfg-expert-timeout").value = t.expert_timeout_s ?? "";
    _notaDefault("#cfg-expert-timeout-msg", t.expert_timeout_s,
                 t.expert_timeout_default_s);
  } catch (_) { /* admin-only field */ }
}

export async function loadToolTimeout() {
  try {
    const t = await api("config/timeouts");
    $("#cfg-tool-timeout").value = t.tool_timeout_s ?? "";
    _notaDefault("#cfg-tool-timeout-msg", t.tool_timeout_s,
                 t.tool_timeout_default_s);
  } catch (_) { /* admin-only field */ }
}

// "es el default" vs "alguien lo piso" es la diferencia que importa
// cuando uno abre Config a ver por que un experto se corta.
function _notaDefault(sel, efectivo, porDefecto) {
  const el = $(sel);
  if (!el || efectivo == null) return;
  el.textContent = Number(efectivo) === Number(porDefecto)
    ? "(default)"
    : `(pisado; el default es ${porDefecto})`;
}

// Llena el desplegable con los tableros del owner. Es una llamada a
// `gh project list` (un spawn), así que va a demanda con botón, no en
// cada carga del tab.
async function loadBoards() {
  const owner = $("#cfg-gh-owner").value.trim();
  const msg = $("#cfg-gh-msg");
  if (!owner) { msg.textContent = "poné la organización primero"; return; }
  const btn = $("#cfg-gh-load");
  btn.disabled = true;
  msg.textContent = "consultando GitHub…";
  try {
    const r = await api(`github/boards?owner=${encodeURIComponent(owner)}`);
    const actual = $("#cfg-gh-number").value;
    const boards = r.boards || [];
    $("#cfg-gh-number").innerHTML =
      `<option value="">(sin configurar)</option>`
      + boards.map((b) => `<option value="${b.number}"${
          String(b.number) === actual ? " selected" : ""
        }>#${b.number} — ${escape(b.title || "")}</option>`).join("");
    msg.textContent = r.error
      ? `⚠ ${r.error}`
      : `${boards.length} tableros — elegí uno y guardá`;
  } catch (e) {
    msg.textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ---- usuarios / roles (2026-08-21) ----
//
// La tabla `users` nombra a los OWNERS; quien no esta es member. El
// backend recarga `identity._roles` en cada escritura, asi que el cambio
// vale al instante y no al proximo reinicio — que es como estaba y por
// eso "no habia gestion".

export async function loadUsers() {
  const tbody = $("#cfg-users-table tbody");
  if (!tbody) return;
  try {
    const r = await api("users");
    const users = r.users || [];
    if (!users.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">nadie cargado: todos entran como member</td></tr>`;
      return;
    }
    const owners = users.filter((u) => u.role === "owner").length;
    tbody.innerHTML = users.map((u) => {
      const yo = u.email === r.me
        ? ' <span class="badge dim" title="sos vos">vos</span>' : "";
      // Al unico owner no se le ofrece el boton: el backend igual lo
      // frena con un 409, pero un boton que solo sirve para mostrar un
      // error es peor que no tenerlo.
      const ultimo = u.role === "owner" && owners === 1;
      const quitar = ultimo
        ? '<span class="muted text-xs" title="es el unico owner">—</span>'
        : `<button class="btn btn-xs danger user-del" data-email="${escape(u.email)}"
             title="volver a member">quitar</button>`;
      return `<tr>
        <td><code>${escape(u.email)}</code>${yo}</td>
        <td><span class="badge ${u.role === "owner" ? "ok" : "dim"}">${escape(u.role)}</span></td>
        <td class="whitespace-nowrap">${escape((u.created_at || "").slice(0, 10) || "—")}</td>
        <td>${quitar}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll(".user-del").forEach((b) =>
      b.onclick = () => deleteUser(b.dataset.email));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty fail">error: ${escape(e.message)}</td></tr>`;
  }
}

async function saveUser() {
  const email = ($("#cfg-user-email").value || "").trim().toLowerCase();
  const role = $("#cfg-user-role").value;
  const msg = $("#cfg-user-msg");
  if (!email.includes("@")) { msg.textContent = "email invalido"; return; }
  try {
    await api("users", {
      method: "PUT", body: JSON.stringify({ email, role }),
    });
    $("#cfg-user-email").value = "";
    msg.textContent = `${email} → ${role} (aplica ya, sin reiniciar)`;
    loadUsers();
  } catch (e) {
    msg.textContent = "error: " + e.message;
  }
}

async function deleteUser(email) {
  if (!await confirmModal({
    title: `Quitar a ${email}`,
    body: "Vuelve a ser `member`: va a poder correr expertos y ver los "
      + "resultados, pero no tocar configuración.\n\nNo pierde el acceso al "
      + "relay: eso lo decide la policy de Access, no esta tabla.",
    confirmText: "Quitar", danger: true,
  })) return;
  try {
    await api(`users/${encodeURIComponent(email)}`, { method: "DELETE" });
    $("#cfg-user-msg").textContent = `${email} volvio a member`;
    loadUsers();
  } catch (e) {
    $("#cfg-user-msg").textContent = "error: " + e.message;
  }
}

// ---------------------------------------------------------------------
// Plantillas de directiva del modo nocturno (2026-08-27)
//
// El chat ya sabe USARLAS (`/noche @nombre`) y listarlas
// (`/plantillas`). Faltaba dónde escribirlas: una directiva buena es
// larga y multilínea, y eso no entra en un composer que manda con Enter.
// ---------------------------------------------------------------------

let _tpls = [];

function tplScope() {
  return $("#tpl-proyecto")?.value || "";
}

export async function loadTemplates() {
  const cuerpo = $("#tpl-tbody");
  if (!cuerpo) return;
  // El selector de proyecto se llena una sola vez, con "" (global) primero.
  const sel = $("#tpl-proyecto");
  if (sel && !sel.options.length) {
    try {
      const p = await api("projects");
      sel.innerHTML = `<option value="">(global)</option>`
        + (p.projects || []).map((x) =>
            `<option value="${escape(x.slug)}">${escape(x.slug)}</option>`).join("");
    } catch { sel.innerHTML = `<option value="">(global)</option>`; }
  }
  try {
    const r = await api("night/templates");
    _tpls = r.templates || [];
  } catch (e) {
    cuerpo.innerHTML = `<tr><td colspan="4" class="p-3 text-xs text-red-400">${
      escape(e.message)}</td></tr>`;
    return;
  }
  if (!_tpls.length) {
    cuerpo.innerHTML = `<tr><td colspan="4" class="p-3 text-xs text-zinc-500">`
      + `sin plantillas todavía</td></tr>`;
    return;
  }
  cuerpo.innerHTML = _tpls.map((t) => `
    <tr data-n="${escape(t.nombre)}" data-p="${escape(t.project_slug)}">
      <td class="font-mono text-[11px]">@${escape(t.nombre)}</td>
      <td>${t.global
        ? `<span class="badge">global</span>`
        : escape(t.project_slug)}</td>
      <td class="text-zinc-400">${escape(t.notas || "")}</td>
      <td class="text-right whitespace-nowrap">
        <button class="btn btn-xs tpl-edit">Editar</button>
        <button class="btn btn-xs tpl-del" aria-label="eliminar plantilla"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
      </td>
    </tr>`).join("");

  $$(".tpl-edit").forEach((b) => b.addEventListener("click", () => {
    const tr = b.closest("tr");
    const t = _tpls.find((x) => x.nombre === tr.dataset.n
                             && (x.project_slug || "") === tr.dataset.p);
    if (!t) return;
    $("#tpl-nombre").value = t.nombre;
    $("#tpl-proyecto").value = t.project_slug || "";
    $("#tpl-notas").value = t.notas || "";
    $("#tpl-directiva").value = t.directiva || "";
    $("#tpl-estado").textContent = `editando @${t.nombre}`;
  }));
  $$(".tpl-del").forEach((b) => b.addEventListener("click", async () => {
    const tr = b.closest("tr");
    if (!await confirmModal({
      title: "Borrar plantilla",
      body: `Se borra @${tr.dataset.n}${tr.dataset.p
        ? ` del proyecto ${tr.dataset.p}` : " (global)"}. `
        + `Los runs que ya la usaron no se tocan.`,
      confirmText: "Borrar", danger: true,
    })) return;
    try {
      await api(`night/templates/${encodeURIComponent(tr.dataset.n)}`
        + `?project=${encodeURIComponent(tr.dataset.p)}`, { method: "DELETE" });
      toast("Plantilla borrada", "ok");
      await loadTemplates();
    } catch (e) { toast(`No pude borrarla: ${e.message}`, "err"); }
  }));
}

async function guardarTemplate() {
  const nombre = ($("#tpl-nombre")?.value || "").trim();
  const directiva = ($("#tpl-directiva")?.value || "").trim();
  if (!nombre) { toast("Ponele un nombre", "err"); return; }
  if (!directiva) { toast("La directiva no puede ir vacía", "err"); return; }
  // Aviso, no bloqueo: el planificador extrae los puntos SOLO a principio
  // de línea, así que una directiva sin ellos pierde el chequeo de
  // cobertura. Puede ser deliberado (una directiva de un solo punto), por
  // eso se avisa y se guarda igual.
  if (!/^\s*(P\d|\d+\.)/m.test(directiva)) {
    toast("Ojo: no veo puntos numerados (P1., P2.…) a principio de línea. "
      + "Sin eso el planificador no puede avisarte si se olvidó alguno.",
      "warn", 9000);
  }
  try {
    await api("night/templates", {
      method: "PUT",
      body: JSON.stringify({
        nombre, directiva,
        project_slug: tplScope(),
        notas: ($("#tpl-notas")?.value || "").trim(),
      }),
    });
    toast(`@${nombre} guardada ✓`, "ok");
    $("#tpl-estado").textContent = "";
    await loadTemplates();
  } catch (e) { toast(`No pude guardarla: ${e.message}`, "err", 7000); }
}

function limpiarTemplate() {
  for (const id of ["#tpl-nombre", "#tpl-notas", "#tpl-directiva"]) {
    const el = $(id); if (el) el.value = "";
  }
  const est = $("#tpl-estado"); if (est) est.textContent = "";
}

export function initConfig() {
  on("#tpl-guardar", "click", guardarTemplate);
  on("#tpl-nueva", "click", limpiarTemplate);

  onClick("#cfg-save", saveConfig);
  onClick("#cfg-user-add", saveUser);
  $("#cfg-user-email").onkeydown = (e) => { if (e.key === "Enter") saveUser(); };
  $("#cfg-relay-host").onchange = updateLanWarning;
  onClick("#cfg-gh-load", loadBoards);

  onClick("#cfg-expert-timeout-save", async () => {
    const v = parseInt($("#cfg-expert-timeout").value, 10);
    if (isNaN(v) || v < 30 || v > 3600) {
      $("#cfg-expert-timeout-msg").textContent = "rango 30..3600";
      return;
    }
    try {
      const r = await api("config/expert-timeout", {
        method: "PUT",
        body: JSON.stringify({ value: v }),
      });
      $("#cfg-expert-timeout-msg").textContent =
        `guardado: ${r.expert_timeout_s}s (próximo chat ya lo usa)`;
      refreshStatus();
    } catch (e) {
      $("#cfg-expert-timeout-msg").textContent = "error: " + e.message;
    }
  });
  onClick("#cfg-expert-timeout-reset", async () => {
    try {
      const r = await api("config/expert-timeout", {
        method: "PUT",
        body: JSON.stringify({ value: null }),
      });
      $("#cfg-expert-timeout").value = r.expert_timeout_s;
      $("#cfg-expert-timeout-msg").textContent =
        `reset: vuelve a ${r.expert_timeout_s}s (env o default)`;
      refreshStatus();
    } catch (e) {
      $("#cfg-expert-timeout-msg").textContent = "error: " + e.message;
    }
  });

  onClick("#cfg-tool-timeout-save", async () => {
    const v = parseInt($("#cfg-tool-timeout").value, 10);
    if (isNaN(v) || v < 5 || v > 600) {
      $("#cfg-tool-timeout-msg").textContent = "rango 5..600";
      return;
    }
    try {
      const r = await api("config/tool-timeout", {
        method: "PUT",
        body: JSON.stringify({ value: v }),
      });
      $("#cfg-tool-timeout-msg").textContent =
        `guardado: ${r.tool_timeout_s}s (próximo run ya lo usa)`;
      refreshStatus();
    } catch (e) {
      $("#cfg-tool-timeout-msg").textContent = "error: " + e.message;
    }
  });
  onClick("#cfg-tool-timeout-reset", async () => {
    try {
      const r = await api("config/tool-timeout", {
        method: "PUT",
        body: JSON.stringify({ value: null }),
      });
      $("#cfg-tool-timeout").value = r.tool_timeout_s;
      $("#cfg-tool-timeout-msg").textContent =
        `reset: vuelve a ${r.tool_timeout_s}s (env o default)`;
      refreshStatus();
    } catch (e) {
      $("#cfg-tool-timeout-msg").textContent = "error: " + e.message;
    }
  });
}
