// Tab MCPs: CRUD del catálogo (F3) + pipeline de install desde GitHub
// (F2). Dos zonas de la UI comparten el archivo: el catálogo (lista +
// form) y el panel de install (job en vuelo + propuesta + confirm).

import { $, $$, api, escape, onClick } from "./api.js";
import { toast, confirmModal, emptyRow, skeletonRows } from "./ui.js";
import { registerPoller } from "./pollers.js";

let _mcps = [];        // último GET (para editar sin re-fetch)
let _editing = null;   // nombre original cuando editas

const HEALTH_BADGE = {
  ok: '<span class="badge ok">ok</span>',
  handshake_failed: '<span class="badge err">handshake ✗</span>',
};
const VET_BADGE = {
  safe: '<span class="badge ok">safe</span>',
  suspect: '<span class="badge warn">suspect</span>',
  rejected: '<span class="badge err">rejected</span>',
};

// ---------- catálogo F3 ----------

export async function loadMcp() {
  const tbody = $("#mcp-table tbody");
  tbody.innerHTML = skeletonRows(9);
  try {
    const r = await api("mcp");
    _mcps = r.mcp_servers || [];
    if (!_mcps.length) {
      tbody.innerHTML = emptyRow(9, {
        title: "No hay MCPs registrados",
        sub: "Un MCP le da tools al experto (docs, browser, SQL). Podés "
           + "agregar uno a mano o buscarlo en el registry.",
        action: `<button class="btn btn-primary" data-mcp-new>Nuevo MCP</button>`,
      });
      $("#mcp-summary").textContent = "";
      return;
    }
    tbody.innerHTML = _mcps.map((m) => {
      const modo = m.on_demand
        ? '<span class="badge dim">on-demand</span>'
        : '<span class="badge ok">always-on</span>';
      const ro = m.read_only ? "" : ' <span class="badge warn">rw</span>';
      const projs = m.project_slugs.length
        ? escape(m.project_slugs.join(", "))
        : '<span class="text-zinc-400">global</span>';
      const health = HEALTH_BADGE[m.health]
        || '<span class="badge dim">?</span>';
      const vet = VET_BADGE[m.vet_verdict]
        || '<span class="badge dim">manual</span>';
      const on = m.enabled
        ? '<span class="badge ok">on</span>'
        : '<span class="badge dim">off</span>';
      return `<tr data-name="${escape(m.name)}">
        <td><code>${escape(m.name)}</code></td>
        <td>${escape(m.capability)}</td>
        <td>${escape(m.transport)}</td>
        <td>${modo}${ro}</td>
        <td>${projs}</td>
        <td>${health}</td>
        <td>${vet}</td>
        <td>${on}</td>
        <td class="row-actions">
          <button class="btn btn-xs mcp-toggle-btn">${m.enabled ? "off" : "on"}</button>
          <button class="btn btn-xs mcp-health-btn"
            title="levanta el proceso y rehace el handshake ahora">health</button>
          <button class="btn btn-xs mcp-edit-btn">editar</button>
          <button class="btn btn-xs danger mcp-del-btn">borrar</button>
        </td>
      </tr>`;
    }).join("");
    $("#mcp-summary").textContent =
      `${_mcps.length} MCPs · ${_mcps.filter((m) => m.enabled).length} habilitados`;
    $$(".mcp-edit-btn").forEach((b) =>
      b.addEventListener("click", (e) =>
        mcpEdit(_byRow(e))));
    $$(".mcp-del-btn").forEach((b) =>
      b.addEventListener("click", (e) =>
        mcpDelete(_byRow(e).name)));
    $$(".mcp-toggle-btn").forEach((b) =>
      b.addEventListener("click", (e) =>
        mcpToggle(_byRow(e))));
    $$(".mcp-health-btn").forEach((b) =>
      b.addEventListener("click", (e) =>
        mcpHealth(_byRow(e), e.target)));
  } catch (e) {
    // El toast avisa, pero se va solo: la tabla tambien tiene que decirlo
    // y ofrecer el reintento.
    tbody.innerHTML = emptyRow(9, {
      title: "No se pudieron cargar los MCPs",
      sub: e.message,
      action: `<button class="btn" data-mcp-retry>Reintentar</button>`,
    });
    $("#mcp-summary").textContent = "";
    toast("Error cargando MCPs: " + e.message, "err");
  }
}

function _byRow(e) {
  const name = e.target.closest("tr").dataset.name;
  return _mcps.find((x) => x.name === name);
}

function mcpNew() {
  _editing = null;
  $("#mcp-form-title").textContent = "Nuevo MCP";
  $("#mcp-name").value = ""; $("#mcp-name").disabled = false;
  $("#mcp-capability").value = "db";
  $("#mcp-transport").value = "stdio";
  $("#mcp-command").value = "";
  $("#mcp-url").value = "";
  $("#mcp-args").value = "";
  $("#mcp-env").value = "";
  $("#mcp-projects").value = "";
  $("#mcp-idle").value = "300";
  $("#mcp-ondemand").checked = true;
  $("#mcp-readonly").checked = true;
  $("#mcp-enabled").checked = false;
  $("#mcp-form-msg").textContent = "";
  $("#mcp-form-wrap").hidden = false;
  $("#mcp-name").focus();
}

function mcpEdit(m) {
  _editing = m.name;
  $("#mcp-form-title").textContent = "Editar " + m.name;
  $("#mcp-name").value = m.name; $("#mcp-name").disabled = true;
  $("#mcp-capability").value = m.capability;
  $("#mcp-transport").value = m.transport;
  $("#mcp-command").value = m.command || "";
  $("#mcp-url").value = m.url || "";
  $("#mcp-args").value = m.args && m.args.length
    ? JSON.stringify(m.args) : "";
  $("#mcp-env").value = Object.keys(m.env || {}).length
    ? JSON.stringify(m.env, null, 2) : "";
  $("#mcp-projects").value = m.project_slugs.join(", ");
  $("#mcp-idle").value = m.idle_timeout_s;
  $("#mcp-ondemand").checked = !!m.on_demand;
  $("#mcp-readonly").checked = !!m.read_only;
  $("#mcp-enabled").checked = !!m.enabled;
  $("#mcp-form-msg").textContent = "";
  $("#mcp-form-wrap").hidden = false;
}

async function mcpToggle(m) {
  try {
    const r = await api(`mcp/${encodeURIComponent(m.name)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !m.enabled }),
    });
    if (r.error) { toast("Error: " + r.error, "err"); return; }
    loadMcp();
  } catch (e) {
    toast("Error: " + e.message, "err");
  }
}

// Levanta el proceso de verdad (npx/uvx incluido su descarga la primera
// vez), así que puede tardar unos segundos: el botón queda deshabilitado
// mientras tanto para no disparar cinco probes del mismo MCP.
async function mcpHealth(m, btn) {
  const antes = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const r = await api(`mcp/${encodeURIComponent(m.name)}/health`,
                        { method: "POST" });
    if (r.error && !r.health) { toast("Error: " + r.error, "err"); return; }
    if (r.health === "ok") {
      toast(`${m.name}: handshake ok ✓`, "ok");
    } else {
      // El error del handshake es LA información para arreglarlo (un
      // ImportError, un binario que no está en el PATH, una credencial
      // vacía). Un "falló" pelado obliga a ir a buscar los logs.
      toast(`${m.name}: handshake ✗\n${r.error || "sin detalle"}`, "err");
    }
    loadMcp();
  } catch (e) {
    toast("Error: " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = antes;
  }
}

async function mcpDelete(name) {
  if (!await confirmModal({ body: `¿Borrar el MCP "${name}" del catálogo?`, danger: true })) return;
  try {
    const r = await api(`mcp/${encodeURIComponent(name)}`,
                        { method: "DELETE" });
    if (r.error) { toast("Error: " + r.error, "err"); return; }
    loadMcp();
  } catch (e) {
    toast("Error: " + e.message, "err");
  }
}

function _parseJsonField(sel, what, fallback) {
  const text = $(sel).value.trim();
  if (!text) return fallback;
  try { return JSON.parse(text); }
  catch (e) { throw new Error(`JSON inválido en ${what}: ${e.message}`); }
}

async function mcpSave() {
  const name = $("#mcp-name").value.trim();
  if (!name) {
    $("#mcp-form-msg").textContent = "name es obligatorio";
    return;
  }
  let args, env;
  try {
    args = _parseJsonField("#mcp-args", "args", []);
    env = _parseJsonField("#mcp-env", "env", {});
  } catch (e) {
    $("#mcp-form-msg").textContent = e.message;
    return;
  }
  const body = {
    name,
    capability: $("#mcp-capability").value,
    transport: $("#mcp-transport").value,
    command: $("#mcp-command").value.trim() || null,
    url: $("#mcp-url").value.trim() || null,
    args, env,
    idle_timeout_s: parseInt($("#mcp-idle").value, 10) || 300,
    on_demand: $("#mcp-ondemand").checked,
    read_only: $("#mcp-readonly").checked,
    enabled: $("#mcp-enabled").checked,
    project_slugs: $("#mcp-projects").value
      .split(",").map((s) => s.trim()).filter(Boolean),
  };
  $("#mcp-save").disabled = true;
  try {
    const r = await api("mcp", {
      method: "POST", body: JSON.stringify(body),
    });
    if (r.error) {
      $("#mcp-form-msg").textContent = "Error: " + r.error;
      return;
    }
    $("#mcp-form-wrap").hidden = true;
    loadMcp();
  } catch (e) {
    $("#mcp-form-msg").textContent = "Error: " + e.message;
  } finally {
    $("#mcp-save").disabled = false;
  }
}

// ---------- pipeline F2: install desde GitHub ----------

let _activeJobId = null;
let _pollHandle = null;

// Pipeline visible: cada paso se marca al pasar.
const STEPS = [
  "cloning", "cloned",
  "scanning", "scanned",
  "vetting", "vetted",
  "awaiting_confirm",
];
// Estados terminales.
const TERMINAL = new Set([
  "healthy", "handshake_failed", "install_failed",
  "rejected", "failed",
]);

function _formatState(s) {
  return (s || "").replace(/_/g, " ");
}

function _stateBadge(s) {
  if (s === "healthy") return '<span class="badge ok">healthy ✓</span>';
  if (s === "handshake_failed")
    return '<span class="badge err">handshake ✗</span>';
  if (s === "install_failed")
    return '<span class="badge err">install ✗</span>';
  if (s === "rejected")
    return '<span class="badge err">rejected</span>';
  if (s === "failed") return '<span class="badge err">failed</span>';
  if (s === "awaiting_confirm")
    return '<span class="badge warn">awaiting confirm</span>';
  if (s === "installing")
    return '<span class="badge warn">installing</span>';
  if (["vetting","scanning","cloning","scanned","vetted"].includes(s))
    return `<span class="badge warn">${escape(_formatState(s))}</span>`;
  if (s === "pending")
    return '<span class="badge dim">pending</span>';
  return `<span class="badge dim">${escape(_formatState(s))}</span>`;
}

function _renderSteps(currentState) {
  const idx = STEPS.indexOf(currentState);
  const list = $("#mcp-job-steps");
  list.innerHTML = STEPS.map((s, i) => {
    let css = "text-xs text-zinc-400";
    if (idx >= 0 && i <= idx) css = "text-xs text-emerald-300";
    if (idx >= 0 && i === idx) css = "text-xs font-semibold text-emerald-200";
    return `<li class="${css}">${escape(_formatState(s))}</li>` +
      (i < STEPS.length - 1
        ? `<li class="text-xs text-zinc-700">·</li>` : "");
  }).join("");
}

function _renderJob(job) {
  $("#mcp-install-panel").hidden = false;
  $("#mcp-job-id").textContent = job.id || "";
  $("#mcp-job-state").innerHTML = _stateBadge(job.state);
  $("#mcp-job-updated").textContent =
    job.updated_at ? `actualizado ${escape(job.updated_at)}` : "";
  $("#mcp-job-url").textContent = job.url || "";
  $("#mcp-job-slug").textContent = job.slug || "";
  $("#mcp-job-commit").textContent = job.source_commit || "—";
  $("#mcp-job-dir").textContent = job.install_dir || "—";

  const errBox = $("#mcp-job-error");
  if (job.error) { errBox.textContent = job.error; errBox.hidden = false; }
  else { errBox.hidden = true; }

  _renderSteps(job.state);

  const findings = job.scan_findings || [];
  $("#mcp-scan-findings").innerHTML = findings.length
    ? findings.map((f) => `<li>⚠ ${escape(f)}</li>`).join("")
    : `<li class="italic text-zinc-400">sin hallazgos</li>`;

  const vetBadge = VET_BADGE[job.vet_verdict] ||
    '<span class="badge dim">manual</span>';
  $("#mcp-vet-verdict").innerHTML = vetBadge;
  $("#mcp-vet-report").textContent = job.vet_report || "—";

  const prop = job.proposal || {};
  $("#mcp-prop-cmd").value = prop.command || "";
  $("#mcp-prop-args").value = prop.args && prop.args.length
    ? JSON.stringify(prop.args) : "";
  const caps = ["browser","db","docs","github","aws","files"];
  $("#mcp-prop-cap").value = caps.includes(prop.capability)
    ? prop.capability : "browser";
  $("#mcp-prop-name").value = prop.name || "";

  const needsManual = !!prop.needs_manual || (prop.command || "") === "";
  $("#mcp-needs-manual-hint").hidden = !needsManual;

  const ready = job.state === "awaiting_confirm";
  $("#mcp-install-confirm").disabled = !ready;
  $("#mcp-install-discard").disabled =
    !(TERMINAL.has(job.state) || job.state === "rejected"
      || job.state === "failed");
  $("#mcp-install-msg").textContent = ready
    ? "Revisa propuesta + override y confirma."
    : (TERMINAL.has(job.state) ? "Job terminado." : "Esperando…");

  return ready;
}

async function _pollJob() {
  if (!_activeJobId) return;
  try {
    const job = await api(`mcp/install/${_activeJobId}`);
    const ready = _renderJob(job);

    if (ready || TERMINAL.has(job.state)) {
      _stopPolling();
      if (ready) return;
      if (job.state === "healthy") {
        toast(`MCP "${job.slug}" instalado ✓`, "ok");
        loadMcp();
      } else if (job.state === "handshake_failed") {
        toast("Handshake falló — fila creada pero deshabilitada", "warn");
        loadMcp();
      } else {
        toast(`Job terminó en ${job.state}`, "err");
      }
    }
  } catch (e) {
    _stopPolling();
    toast("Job expiró (¿se reinició el relay?). Reintenta.", "err");
  }
}

function _startPolling(jobId) {
  _stopPolling();
  _activeJobId = jobId;
  _pollHandle = registerPoller(_pollJob, 800, { tabId: "mcp" });
  _pollJob();
}

function _stopPolling() {
  if (_pollHandle !== null) {
    _pollHandle(); // unregister
    _pollHandle = null;
  }
}

async function mcpInstallStart() {
  const url = $("#mcp-gh-url").value.trim();
  if (!url) { toast("Pega una URL de GitHub", "err"); return; }
  $("#mcp-install-btn").disabled = true;
  $("#mcp-install-panel").hidden = false;
  $("#mcp-install-msg").textContent = "clonando + veteando…";
  try {
    const r = await api("mcp/install", {
      method: "POST", body: JSON.stringify({ url }),
    });
    if (r.error) {
      $("#mcp-install-msg").textContent = "Error: " + r.error;
      return;
    }
    _startPolling(r.job_id);
  } catch (e) {
    $("#mcp-install-msg").textContent = "Error: " + e.message;
  } finally {
    $("#mcp-install-btn").disabled = false;
  }
}

async function mcpInstallConfirm() {
  if (!_activeJobId) return;
  let args;
  try {
    args = _parseJsonField("#mcp-prop-args", "args", []);
  } catch (e) {
    $("#mcp-install-msg").textContent = e.message;
    return;
  }
  const override = {
    command: $("#mcp-prop-cmd").value.trim(),
    args,
    capability: $("#mcp-prop-cap").value,
    name: $("#mcp-prop-name").value.trim() || undefined,
  };
  $("#mcp-install-confirm").disabled = true;
  $("#mcp-install-msg").textContent = "instalando + handshake…";
  try {
    const r = await api(`mcp/install/${_activeJobId}/confirm`, {
      method: "POST", body: JSON.stringify(override),
    });
    // Ojo: el job trae `error` como parte de su estado (p.ej.
    // handshake_failed) — solo es error de request si NO hay `state`.
    if (r.error && !r.state) {
      $("#mcp-install-msg").textContent = "Error: " + r.error;
      $("#mcp-install-confirm").disabled = false;
      return;
    }
    _startPolling(r.id || _activeJobId);
  } catch (e) {
    $("#mcp-install-msg").textContent = "Error: " + e.message;
    $("#mcp-install-confirm").disabled = false;
  }
}

function mcpInstallDiscard() {
  _stopPolling();
  _activeJobId = null;
  $("#mcp-install-panel").hidden = true;
  toast("Job liberado de la UI (sigue en el relay hasta que expire)",
        "info");
}

function mcpInstallClose() {
  $("#mcp-install-panel").hidden = true;
}

// ---------- conexiones SQL (2026-08-16) ----------
//
// Viven en el tab de MCPs porque para el humano son la misma pregunta:
// a qué cosa externa puede llegar el experto. Lo que la UI NUNCA muestra
// es el DSN entero — el GET ya lo devuelve redactado desde el relay, así
// que la contraseña no está ni en el HTML.

let _conns = [];       // último GET, para saber el permiso actual

export async function loadDbConns() {
  try {
    const r = await api("db-connections");
    const filas = r.connections || [];
    const tbody = $("#db-table tbody");
    tbody.innerHTML = filas.map((c) => {
      const permiso = c.escribir
        ? '<span class="badge warn">escritura</span>'
        : '<span class="badge ok">lectura</span>';
      // `relay` no se borra: no es una conexión que alguien dio de alta,
      // es la base del propio relay. Lo que sí se le cambia es el
      // permiso, con el mismo botón que las demás.
      const borrar = c.reservada
        ? '<span class="muted text-xs">reservada</span>'
        : '<button class="btn btn-xs danger db-del-btn">borrar</button>';
      return `<tr data-alias="${escape(c.alias)}">
        <td><code>${escape(c.alias)}</code></td>
        <td>${escape(c.motor)}</td>
        <td class="font-mono text-xs">${escape(c.dsn)}</td>
        <td>${permiso}</td>
        <td>${escape(c.descripcion || "")}</td>
        <td class="row-actions">
          <button class="btn btn-xs db-perm-btn">
            ${c.escribir ? "🔒 solo lectura" : "permitir escribir"}</button>
          <button class="btn btn-xs db-test-btn">⚡ probar</button>
          ${borrar}
        </td>
      </tr>`;
    }).join("");
    _conns = filas;
    $("#db-summary").textContent = filas.length
      ? `${filas.length} conexión${filas.length === 1 ? "" : "es"} + \`relay\` (reservada)`
      : "solo `relay` (la base del propio relay)";
    $$(".db-perm-btn").forEach((b) =>
      b.addEventListener("click", (e) => dbTogglePermiso(_alias(e))));
    $$(".db-test-btn").forEach((b) =>
      b.addEventListener("click", (e) => dbTest(_alias(e))));
    $$(".db-del-btn").forEach((b) =>
      b.addEventListener("click", (e) => dbDelete(_alias(e))));
  } catch (e) {
    console.error(e);
    toast("Error cargando conexiones SQL: " + e.message, "err");
  }
}

function _alias(e) {
  return e.target.closest("tr").dataset.alias;
}

async function dbSave() {
  const alias = $("#db-alias").value.trim();
  const dsn = $("#db-dsn").value.trim();
  if (!alias || !dsn) {
    $("#db-msg").textContent = "alias y cadena son obligatorios";
    return;
  }
  try {
    const r = await api("db-connections", {
      method: "POST",
      body: JSON.stringify({
        alias, dsn,
        descripcion: $("#db-desc").value.trim(),
        escribir: $("#db-escribir").checked,
      }),
    });
    if (r.error) { $("#db-msg").textContent = "Error: " + r.error; return; }
    // Se limpia el DSN del input apenas se guarda: no tiene por qué
    // quedar una contraseña a la vista en la pantalla del humano.
    $("#db-dsn").value = "";
    $("#db-alias").value = "";
    $("#db-desc").value = "";
    $("#db-escribir").checked = false;
    $("#db-msg").textContent = "";
    toast(`Conexión \`${alias}\` guardada`, "ok");
    await loadDbConns();
    await dbTest(alias);   // el DSN malo se descubre ahora, no en un run
  } catch (e) {
    $("#db-msg").textContent = "Error: " + e.message;
  }
}

async function dbTogglePermiso(alias) {
  const c = _conns.find((x) => x.alias === alias);
  if (!c) return;
  const prender = !c.escribir;
  if (prender) {
    const aviso = alias === "relay"
      ? "El experto va a poder ESCRIBIR en la base del propio relay: "
        + "chats, proyectos, runs, tokens. Un UPDATE mal apuntado se lleva "
        + "puesto el estado del relay, y no hay deshacer."
      : `El experto va a poder ESCRIBIR en "${alias}": INSERT, UPDATE, `
        + "DELETE. No hay deshacer.";
    if (!await confirmModal({
      title: `Habilitar escritura en "${alias}"`,
      body: aviso, confirmText: "Habilitar escritura", danger: true,
    })) return;
  }
  try {
    // SIN dsn, a propósito: lo que tenemos acá es el REDACTADO
    // (`postgres://…@host/base`), así que reenviarlo guardaría esa
    // cadena como si fuera la real y rompería la conexión. El relay
    // conserva la que ya tiene cuando el body no trae una.
    const r = await api("db-connections", {
      method: "POST",
      body: JSON.stringify({
        alias, escribir: prender, descripcion: c.descripcion || "",
      }),
    });
    if (r.error) { toast("Error: " + r.error, "err"); return; }
    toast(`\`${alias}\`: ${prender ? "escritura habilitada" : "solo lectura"}`,
          prender ? "warn" : "ok");
    await loadDbConns();
  } catch (e) {
    toast("Error: " + e.message, "err");
  }
}

async function dbTest(alias) {
  $("#db-msg").textContent = `probando \`${alias}\`…`;
  try {
    const r = await api(`db-connections/${encodeURIComponent(alias)}/test`,
                        { method: "POST" });
    $("#db-msg").textContent = "";
    if (r.ok) toast(`\`${alias}\` conecta (${r.motor})`, "ok");
    else toast(`\`${alias}\` NO conecta: ${r.salida || r.error}`, "err");
  } catch (e) {
    $("#db-msg").textContent = "Error: " + e.message;
  }
}

async function dbDelete(alias) {
  if (!await confirmModal({
    title: "Borrar conexión",
    body: `¿Borrar la conexión "${alias}"? El experto deja de poder `
          + "consultarla; la base no se toca.",
    danger: true,
  })) return;
  try {
    await api(`db-connections/${encodeURIComponent(alias)}`,
              { method: "DELETE" });
    toast(`Conexión \`${alias}\` borrada`, "ok");
    await loadDbConns();
  } catch (e) {
    toast("Error: " + e.message, "err");
  }
}

export function initMcp() {
  onClick("#mcp-refresh", loadMcp);
  onClick("#mcp-new", mcpNew);
  onClick("#mcp-cancel", () => { $("#mcp-form-wrap").hidden = true; });
  onClick("#mcp-save", mcpSave);
  onClick("#mcp-install-btn", mcpInstallStart);
  onClick("#mcp-install-confirm", mcpInstallConfirm);
  onClick("#mcp-install-discard", mcpInstallDiscard);
  onClick("#mcp-install-close", mcpInstallClose);
  onClick("#db-refresh", loadDbConns);
  onClick("#db-save", dbSave);
}
