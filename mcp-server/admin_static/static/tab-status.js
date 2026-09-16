// Tab Estado: health del relay, stats del día, sesiones VS Code.

import { $, api, apiRoot, escape } from "./api.js";
import { statCell, toast, emptyRow, confirmModal, alertModal } from "./ui.js";
import { dtView } from "./ui-table.js";
import { updateSkillsBadge } from "./tab-skills.js";

// El health repartido en dos niveles: lo que contesta "¿anda?" arriba,
// y el inventario —que cambia una vez por semana— abajo, chico. Antes
// los cinco valores iban en una grilla de KPI idéntica a la de
// "actividad de hoy", una arriba de la otra, y no destacaba ninguno.
const _dato = (v, k) =>
  `<span class="text-[13px] text-zinc-300"><span
     class="font-semibold tabular-nums text-zinc-100">${v}</span> ${k}</span>`;

export async function refreshStatus() {
  const dot = $("#status-health-dot"), txt = $("#status-health-text");
  const meta = $("#status-health-meta"), inv = $("#status-inventory");
  try {
    const h = await api("health");
    $("#status-indicator").classList.remove("down");
    if (dot) dot.className = "h-2 w-2 shrink-0 rounded-full bg-emerald-400";
    if (txt) { txt.textContent = "Relay operativo"; txt.className =
      "text-base font-semibold text-zinc-100"; }
    if (meta) {
      meta.innerHTML = [
        `versión <span class="font-mono text-zinc-200">${escape(String(h.relay_version ?? "?"))}</span>`,
        `<span class="h-3 w-px bg-zinc-800"></span>`,
        `<span>${h.vscode_sessions_live} ${h.vscode_sessions_live === 1
          ? "sesión de VS Code" : "sesiones de VS Code"}</span>`,
      ].join("");
    }
    if (inv) {
      inv.hidden = false;
      inv.innerHTML = [
        `<span class="text-[11px] font-medium uppercase tracking-wider text-zinc-400">inventario</span>`,
        `<span class="h-4 w-px bg-zinc-800"></span>`,
        _dato(h.projects_total, "proyectos"),
        _dato(h.projects_indexed, "indexados (cbm)"),
        `<span class="inline-flex items-center gap-1.5 text-[13px] ${
          h.cbm_binary ? "text-zinc-300" : "text-amber-300"}">${
          h.cbm_binary ? _CHECK : _CRUZ} binario cbm</span>`,
      ].join("");
    }
    // Badge de borradores de skill pendientes (autoaprendizaje):
    // health se pollea cada 15s, así el sidebar avisa sin abrir la tab.
    updateSkillsBadge(h.skill_drafts_pending ?? 0);
  } catch (e) {
    $("#status-indicator").classList.add("down");
    if (dot) dot.className = "h-2 w-2 shrink-0 rounded-full bg-red-400";
    if (txt) { txt.textContent = "El relay no responde"; txt.className =
      "text-base font-semibold text-red-300"; }
    if (meta) meta.innerHTML = `<span class="text-red-300">${escape(e.message)}</span>`;
    // Un inventario viejo al lado de "no responde" es peor que ninguno:
    // parece que sigue midiendo algo.
    if (inv) inv.hidden = true;
  }
  refreshStatusExtras();
  refreshBotStatus();
}

const _SVG = (d) => `<svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2.2" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const _CHECK = _SVG('<path d="M20 6L9 17l-5-5"/>');
const _CRUZ = _SVG('<path d="M18 6L6 18M6 6l12 12"/>');

// Estado de orden local de la tabla de sesiones VS Code. dtView es
// puro y conserva el estado entre refreshes del poller: un usuario que
// ordenó por "último handshake" sigue viendo esa vista después del
// siguiente /stats + /sessions. Sin esto el orden se perdía a cada
// poll (15s).
const _SESS_COLUMNS = [
  { key: "name", label: "nombre", value: (s) => s.name || "" },
  { key: "target", label: "target", value: (s) => s.last_target || "" },
  { key: "sse", label: "SSE", sortable: false,
    value: (s) => (s.connected ? 1 : 0) },
  { key: "paused", label: "pausada", sortable: false,
    value: (s) => (s.paused ? 1 : 0) },
  { key: "handshake", label: "último handshake",
    value: (s) => s.last_handshake_ts || "" },
];
const _sessState = { sortKey: 4, sortDir: "desc", page: 0 };  // handshake desc

// Stats del día + sesiones VS Code (endpoints raíz del relay).
async function refreshStatusExtras() {
  try {
    const s = await apiRoot("/stats");
    $("#stats-grid").innerHTML = [
      statCell(s.chats_today ?? 0, "chats hoy"),
      statCell((s.tokens_in_today ?? 0).toLocaleString(), "tokens in"),
      statCell((s.tokens_out_today ?? 0).toLocaleString(), "tokens out"),
      statCell(s.commands_today ?? 0, "comandos hoy"),
      statCell(s.experts_running ?? 0, "expertos corriendo"),
    ].join("");
    // Badge del sidebar (En curso) — vive acá porque /stats se pollea
    // cada 15s; loadRunning lo pisa con el número exacto al refrescar.
    updateRunningBadge(s.experts_running ?? 0);
  } catch (_) { /* best-effort */ }
  try {
    const r = await apiRoot("/sessions");
    const tbody = $("#sessions-table tbody");
    if (!r.sessions.length) {
      tbody.innerHTML = emptyRow(5, {
        title: "Ninguna ventana de VS Code conectada",
        sub: "La extensión abre la sesión sola al arrancar VS Code sobre un "
           + "repo indexado. Si tienes una abierta y no aparece, revisa que el "
           + "relay esté en el puerto que espera la extensión.",
        icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
          class="h-8 w-8"><rect x="3" y="4.5" width="18" height="12" rx="2"/>
          <path d="M8 20h8M12 16.5V20"/></svg>`,
      });
      return;
    }
    // dtView: sort por columna + paginación si >25 (cabe un admin con
    // 20 repos abiertos y 2 VS Code por repo). Sin estado local el
    // orden elegido se perdía con cada /sessions del poller.
    const { slice } = dtView(_SESS_COLUMNS, {
      rows: r.sessions,
      sortKey: _sessState.sortKey,
      sortDir: _sessState.sortDir,
      page: _sessState.page,
    }, r.sessions.length > 25 ? 25 : 0);
    tbody.innerHTML = slice.map((s) => `
      <tr>
        <td>${escape(s.name)}</td>
        <td><code>${escape(s.last_target || "—")}</code></td>
        <td>${s.connected
          ? '<span class="badge ok">SSE vivo</span>'
          : '<span class="badge dim">solo handshake</span>'}</td>
        <td>${s.paused
          ? '<span class="badge warn">pausada</span>'
          : '<span class="text-zinc-600">—</span>'}</td>
        <td>${escape(s.last_handshake_ts || "—")}</td>
      </tr>`).join("");
  } catch (_) { /* best-effort */ }
}

// ---------- bot de Discord ----------
// Dos semáforos, no uno: el /health del bot devuelve 200 mientras el
// PROCESO viva, sin mirar el gateway. Un bot "verde" que no está
// conectado a Discord es justo el estado que dejaba vínculos fantasma.

// Un chip por eslabón, con su nombre adentro. Antes era
// `relay ✓ → proceso ✓ → Discord ✓`: texto suelto con emoji, donde el
// nombre y su estado quedaban a distinta altura y la flecha era un
// carácter más. Ahora cada eslabón es una unidad que se lee sola.
function _eslabon(nombre, up) {
  return `<span class="inline-flex items-center gap-1.5 rounded-lg border
    border-zinc-800 bg-zinc-950 px-2.5 py-1 text-[13px] text-zinc-200">
    <span class="h-1.5 w-1.5 shrink-0 rounded-full ${
      up ? "bg-emerald-400" : "bg-red-400"}"></span>${escape(nombre)}</span>`;
}

const _FLECHA = `<svg class="h-4 w-4 shrink-0 text-zinc-600" viewBox="0 0 24 24"
  fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`;

const _cadena = (...pasos) => pasos.join(_FLECHA);

export async function refreshBotStatus() {
  const chain = $("#bot-chain"), detail = $("#bot-detail"), btn = $("#bot-start-btn");
  if (!chain) return null;
  let st;
  try {
    st = await api("bot/status");
  } catch (e) {
    chain.innerHTML = _cadena(_eslabon("relay", true))
      + `<span class="badge err">la sonda del bot falló</span>`;
    if (detail) detail.textContent = e.message;
    return null;
  }
  const proc = st.process === "up", gw = st.gateway === "up";
  chain.innerHTML = _cadena(
    _eslabon("relay", true), _eslabon("proceso", proc), _eslabon("Discord", gw));
  if (detail) detail.textContent = gw ? `conectado · ${st.url}` : (st.detail || "");
  if (btn) {
    btn.hidden = gw;
    // El texto dice qué va a hacer: reconectar el gateway de un proceso
    // vivo no es lo mismo que levantar el .exe.
    btn.textContent = proc ? "Conectar a Discord" : "Arrancar bot";
  }
  return st;
}

const _botStartBtn = document.getElementById("bot-start-btn");
if (_botStartBtn) {
  _botStartBtn.addEventListener("click", async () => {
    const label = _botStartBtn.textContent;
    _botStartBtn.disabled = true;
    _botStartBtn.textContent = "⏳ arrancando…";
    try {
      // 30s: el handler bloquea hasta que el gateway conecte (o hasta su
      // propio techo de 25s), así el botón no miente al volver.
      const r = await api("bot/start", { method: "POST" }, 30_000);
      await refreshBotStatus();
      toast(r.ok ? "Bot conectado a Discord ✓"
                 : "No arrancó: " + (r.detail || "sin detalle"),
            r.ok ? "ok" : "err");
    } catch (e) {
      toast("No se pudo arrancar el bot: " + e.message, "err");
    } finally {
      _botStartBtn.disabled = false;
      _botStartBtn.textContent = label;
      refreshBotStatus();
    }
  });
}

const _botRefreshBtn = document.getElementById("bot-refresh-btn");
if (_botRefreshBtn) _botRefreshBtn.addEventListener("click", () => refreshBotStatus());

export function updateRunningBadge(n) {
  const b = $("#running-tab-badge");
  if (!b) return;
  b.textContent = String(n);
  b.classList.toggle("hidden", !n);
}

// Apretar "Reiniciar relay" → confirma → POST /admin/api/restart.
// El server escribe state/restart-requested-at.txt y devuelve 200.
// NO mata el proceso: eso lo hace restart-4bis-relay.ps1 o el
// supervisor externo leyendo el marker. La UI muestra un banner
// claro con instrucciones en vez de hacer reload automático.
const _restartBtn = document.getElementById("restart-btn");
const _restartLabel = _restartBtn?.textContent;
if (_restartBtn) {
  _restartBtn.addEventListener("click", async () => {
    const ok = await confirmModal({
      title: "¿Reiniciar el relay?",
      body:
        "<ol style=\"margin:0 0 0 1.25rem;padding:0;line-height:1.55\">"
        + "<li>El server escribe un marker file (no se reinicia solo).</li>"
        + "<li>Corré <code>restart-4bis-relay.ps1</code> cuando veas este banner.</li>"
        + "<li>El script mata el proceso viejo y arranca uno nuevo.</li>"
        + "</ol>"
        + "<p style=\"margin:.75rem 0 0\">¿Continuar?</p>",
      confirmText: "Sí, reiniciar",
      cancelText: "Cancelar",
      danger: true,
    });
    if (!ok) return;
    _restartBtn.disabled = true;
    _restartBtn.textContent = "⏳ Marker escrito, esperando script…";
    try {
      const r = await api("restart", { method: "POST" });
      // El server vive, escribió el marker, NO se mató.
      // Mostramos banner persistente con instrucciones; el usuario
      // corre el .ps1 (o lo hace manual) y recarga la UI después.
      _restartBtn.textContent = "✅ Marker escrito — ejecuta el .ps1";
      _restartBtn.disabled = false;
      // Banner con info del server (PID + path del marker)
      const banner = document.createElement("div");
      banner.className = "banner banner-info";
      banner.innerHTML =
        `<strong>Reinicio solicitado.</strong> ` +
        `PID <code>${r.pid}</code>, marker: <code>${r.marker_path}</code>. ` +
        `<br>Ejecutá <code>restart-4bis-relay.ps1</code> para reiniciar el relay. ` +
        `<br>Cuando vuelva, recargá esta página (F5).`;
      _restartBtn.parentElement.insertBefore(banner, _restartBtn.nextSibling);
    } catch (e) {
      _restartBtn.disabled = false;
      _restartBtn.textContent = _restartLabel;
      await alertModal({
        title: "No se pudo escribir el marker",
        body: escape(e?.message || String(e)),
        okText: "Cerrar",
      });
    }
  });
}
