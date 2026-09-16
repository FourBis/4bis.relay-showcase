// Tab Comandos: CRUD de comandos dinámicos + runner de prueba.

import { $, $$, api, escape, onClick } from "./api.js";
import { toast, confirmModal, emptyRow, skeletonRows } from "./ui.js";
import { refreshStatus } from "./tab-status.js";

let _editingCmd = null;  // nombre original cuando editas
// Los comandos de la última carga. El runner los necesita para armar el
// placeholder de args con las claves REALES del comando elegido.
let _cmds = [];

/** Ejemplo de args para el placeholder, sacado del args_schema.
 *
 * Cada comando usa su propia clave —`build` toma `project`, `memoria`
 * toma `target`, `cancel` toma `chat`— así que un ejemplo fijo servía
 * para unos y mentía para otros. Exportada para poder testearla sin DOM.
 */
export function ejemploArgs(cmd) {
  const props = (cmd?.args_schema || {}).properties || {};
  const nombres = Object.keys(props);
  if (!nombres.length) return "sin args";
  const req = new Set((cmd.args_schema || {}).required || []);
  const ej = {};
  for (const n of nombres) {
    const tipo = props[n].type;
    ej[n] = tipo === "integer" || tipo === "number" ? 10 : `<${n}>`;
  }
  const opcionales = nombres.filter((n) => !req.has(n));
  return JSON.stringify(ej)
    + (opcionales.length ? `  (opcionales: ${opcionales.join(", ")})` : "");
}

function sincronizarPlaceholder() {
  const campo = $("#cmd-run-args");
  const cmd = _cmds.find((c) => c.name === $("#cmd-run-name")?.value);
  if (campo) campo.placeholder = ejemploArgs(cmd);
}

export async function loadCommands() {
  const tbody = $("#commands-table tbody");
  tbody.innerHTML = skeletonRows(6);
  try {
    const r = await api("commands");
    _cmds = r.commands || [];
    if (!_cmds.length) {
      tbody.innerHTML = emptyRow(6, {
        title: "No hay comandos registrados",
        sub: "Un comando es un handler de Python que el bot puede invocar "
           + "por nombre. Creá el primero y aparece acá y en el selector "
           + "de abajo.",
        action: `<button class="btn btn-primary" data-cmd-new>Nuevo comando</button>`,
      });
      $("#cmd-summary").textContent = "";
      $("#cmd-run-name").innerHTML = "";
      return;
    }
    tbody.innerHTML = r.commands.map((c) => {
      const schema = c.args_schema
        ? `<pre>${escape(JSON.stringify(c.args_schema, null, 2))}</pre>`
        : '<span class="text-zinc-600">—</span>';
      const on = c.enabled
        ? '<span class="badge ok">on</span>'
        : '<span class="badge dim">off</span>';
      return `<tr data-name="${escape(c.name)}">
        <td><code>${escape(c.name)}</code></td>
        <td>${escape(c.description || "")}</td>
        <td><code>${escape(c.handler)}</code></td>
        <td>${schema}</td>
        <td>${on}</td>
        <td class="row-actions">
          <button class="btn btn-xs cmd-edit-btn">editar</button>
          <button class="btn btn-xs danger cmd-del-btn">borrar</button>
        </td>
      </tr>`;
    }).join("");
    $("#cmd-summary").textContent = `${r.commands.length} comandos`;
    // Llena el selector del "probar un comando".
    const sel = $("#cmd-run-name");
    sel.innerHTML = r.commands
      .filter((c) => c.enabled)
      .map((c) => `<option value="${escape(c.name)}">${escape(c.name)}</option>`)
      .join("");
    sincronizarPlaceholder();
    $$(".cmd-edit-btn").forEach((b) =>
      b.addEventListener("click", (e) => {
        const name = e.target.closest("tr").dataset.name;
        const c = r.commands.find((x) => x.name === name);
        cmdEdit(c);
      })
    );
    $$(".cmd-del-btn").forEach((b) =>
      b.addEventListener("click", (e) => {
        const name = e.target.closest("tr").dataset.name;
        cmdDelete(name);
      })
    );
  } catch (e) {
    // Antes: `console.error(e)` y nada mas. La tabla quedaba con lo de la
    // carga anterior (o vacia) y no habia forma de saber que fallo.
    tbody.innerHTML = emptyRow(6, {
      title: "No se pudieron cargar los comandos",
      sub: e.message,
      action: `<button class="btn" data-cmd-retry>Reintentar</button>`,
    });
    $("#cmd-summary").textContent = "";
  }
}

// Los botones del estado vacio/error viven dentro del tbody, que se
// reescribe entero en cada carga: delegacion, no listener por boton.
function _wireEstadosCmd() {
  $("#commands-table").addEventListener("click", (e) => {
    if (e.target.closest("[data-cmd-new]")) cmdNew();
    else if (e.target.closest("[data-cmd-retry]")) loadCommands();
  });
}

function cmdNew() {
  _editingCmd = null;
  $("#cmd-form-title").textContent = "Nuevo comando";
  $("#cmd-name").value = ""; $("#cmd-name").disabled = false;
  $("#cmd-description").value = "";
  $("#cmd-handler").value = "";
  $("#cmd-schema").value = "";
  $("#cmd-enabled").checked = true;
  $("#cmd-form-msg").textContent = "";
  $("#cmd-form-wrap").hidden = false;
  $("#cmd-name").focus();
}

function cmdEdit(c) {
  _editingCmd = c.name;
  $("#cmd-form-title").textContent = "Editar " + c.name;
  $("#cmd-name").value = c.name; $("#cmd-name").disabled = true;
  $("#cmd-description").value = c.description || "";
  $("#cmd-handler").value = c.handler || "";
  $("#cmd-schema").value = c.args_schema
    ? JSON.stringify(c.args_schema, null, 2) : "";
  $("#cmd-enabled").checked = !!c.enabled;
  $("#cmd-form-msg").textContent = "";
  $("#cmd-form-wrap").hidden = false;
}

async function cmdDelete(name) {
  if (!await confirmModal({ body: `¿Borrar el comando "${name}"?`, danger: true })) return;
  try {
    const r = await api(`commands/${encodeURIComponent(name)}`,
                        { method: "DELETE" });
    if (r.error) {
      toast("Error: " + r.error, "err");
      return;
    }
    loadCommands();
  } catch (e) {
    toast("Error: " + e.message, "err");
  }
}

async function cmdSave() {
  const name = $("#cmd-name").value.trim();
  const handler = $("#cmd-handler").value.trim();
  if (!name || !handler) {
    $("#cmd-form-msg").textContent = "name y handler son obligatorios";
    return;
  }
  let schema = null;
  const schemaText = $("#cmd-schema").value.trim();
  if (schemaText) {
    try { schema = JSON.parse(schemaText); }
    catch (e) {
      $("#cmd-form-msg").textContent = "JSON inválido en schema: " + e.message;
      return;
    }
  }
  const body = {
    name, handler,
    description: $("#cmd-description").value.trim(),
    enabled: $("#cmd-enabled").checked,
    args_schema: schema,
  };
  $("#cmd-save").disabled = true;
  try {
    const r = await api("commands", {
      method: "POST", body: JSON.stringify(body),
    });
    if (r.error) {
      $("#cmd-form-msg").textContent = "Error: " + r.error;
      return;
    }
    $("#cmd-form-wrap").hidden = true;
    loadCommands();
    refreshStatus();
  } catch (e) {
    $("#cmd-form-msg").textContent = "Error: " + e.message;
  } finally {
    $("#cmd-save").disabled = false;
  }
}

async function cmdRun() {
  const name = $("#cmd-run-name").value;
  const argsText = $("#cmd-run-args").value.trim();
  let args = {};
  if (argsText) {
    try { args = JSON.parse(argsText); }
    catch (e) {
      toast("JSON inválido en args: " + e.message, "err");
      return;
    }
  }
  const out = $("#command-run-output");
  out.hidden = false;
  out.textContent = "ejecutando...";
  try {
    const r = await api(`commands/${encodeURIComponent(name)}/run`, {
      method: "POST",
      // Sin `project`: el endpoint lo leía y lo tiraba. El proyecto
      // viaja DENTRO de args, con la clave que pida cada comando.
      body: JSON.stringify({ args }),
    });
    if (r.error) {
      out.textContent = "ERROR: " + r.error;
    } else {
      out.textContent =
        `[${r.duration_ms}ms]\n\n${r.output || "(sin output)"}`;
    }
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

export function initCommands() {
  _wireEstadosCmd();
  onClick("#cmd-refresh", loadCommands);
  onClick("#cmd-new", cmdNew);
  onClick("#cmd-cancel", () => { $("#cmd-form-wrap").hidden = true; });
  onClick("#cmd-save", cmdSave);
  onClick("#cmd-run-btn", cmdRun);
  $("#cmd-run-name").addEventListener("change", sincronizarPlaceholder);
}
