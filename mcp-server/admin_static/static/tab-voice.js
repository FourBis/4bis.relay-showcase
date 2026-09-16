// Tab Voz (D-PEND-003, 2026-07-13 + Iter 8.5): transcripts de voice
// input (docs/VOICE_INPUT.md). Lista GET /voice/transcripts y muestra
// el texto completo + processed en un modal. Botón "📝 Issue" en cada
// fila aplica la skill on-demand `create-github-issue-from-transcript`
// (ADR-031): corre el LLM sin tools, devuelve markdown listo para
// pegar como issue de GitHub o historia de usuario. NO abre nada en
// GitHub — el operador decide dónde pegar el markdown.
//
// Solo lectura para todo lo demás (transcript + processed).

import { $, $$, api, apiRoot, escape, _dbg, onClick } from "./api.js";
import { modalLoad, toast } from "./ui.js";
import { dtView } from "./ui-table.js";

const ISSUE_SKILL = "create-github-issue-from-transcript";

// Estado local de orden. La tabla se recarga on-click (no hay poller)
// pero el orden lo conservamos entre refreshes. Default: fecha desc.
const _voiceState = { sortKey: 1, sortDir: "desc", page: 0 };
const _VOICE_COLUMNS = [
  { key: "id", label: "trx_id", value: (t) => t.id || "" },
  { key: "ts", label: "fecha", className: "whitespace-nowrap",
    value: (t) => t.ts || "" },
  { key: "author", label: "autor", value: (t) => t.author || "" },
  { key: "mode", label: "modo", value: (t) => t.mode || "" },
  { key: "duration", label: "duración", sortable: false,
    value: (t) => t.duration_s ?? 0 },
  { key: "project", label: "proyecto", value: (t) => t.related_project || "" },
  { key: "_actions", label: "", sortable: false, className: "row-actions" },
];

export async function loadVoice() {
  const tbody = $("#voice-table tbody");
  try {
    // Filtrar la skill del listado general. Si no existe, deshabilita
    // los botones con un toast explicativo al primer click.
    let skillAvailable = false;
    try {
      const { skills } = await api("skills");
      skillAvailable = (skills || []).some((s) => s.name === ISSUE_SKILL);
    } catch (e) {
      _dbg("loadVoice: no pude listar skills", e.message);
    }

    const r = await apiRoot("/voice/transcripts");
    const items = r.transcripts || [];
    $("#voice-summary").textContent =
      `${items.length} transcripts`
      + (skillAvailable ? "" : ` · ⚠ skill "${ISSUE_SKILL}" no instalada`);
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">
        Sin transcripts todavía. Genera uno con /listen (audio adjunto)
        o /join + /leave (grabación VC) desde Discord.</td></tr>`;
      return;
    }
    const { slice } = dtView(_VOICE_COLUMNS, {
      rows: items,
      query: "",
      sortKey: _voiceState.sortKey,
      sortDir: _voiceState.sortDir,
      page: _voiceState.page,
    }, items.length > 25 ? 25 : 0);
    tbody.innerHTML = slice.map((t) => {
      const skillTitle = skillAvailable
        ? "Crear issue / historia de usuario desde este transcript"
        : `Skill "${ISSUE_SKILL}" no instalada — aprobala primero (tab Skills)`;
      return `<tr data-id="${escape(t.id)}" class="voice-row"
        title="click para ver el texto">
        <td><code>${escape(t.id)}</code></td>
        <td class="whitespace-nowrap">${escape(t.ts || "—")}</td>
        <td>${escape(t.author || "—")}</td>
        <td><span class="badge ${t.mode === "vc" ? "warn" : "dim"}">${escape(t.mode || "?")}</span></td>
        <td class="tabular-nums">${t.duration_s != null ? Number(t.duration_s).toFixed(1) + "s" : "—"}</td>
        <td>${t.related_project ? `<code>${escape(t.related_project)}</code>` : "—"}
            ${t.processed ? ' <span class="badge ok">procesado</span>' : ""}</td>
        <td class="row-actions">
          <button class="btn btn-xs voice-issue-btn" data-id="${escape(t.id)}"
            title="${escape(skillTitle)}"
            ${skillAvailable ? "" : "disabled"}>Issue</button>
        </td>
      </tr>`;
    }).join("");
    // aria-sort en el <th>.
    const ths = document.querySelectorAll("#voice-table thead .th");
    ths.forEach((th, i) => {
      const col = _VOICE_COLUMNS[i];
      if (!col || col.sortable === false) {
        th.removeAttribute("aria-sort");
        return;
      }
      th.setAttribute("aria-sort",
        i === _voiceState.sortKey
          ? (_voiceState.sortDir === "desc" ? "descending" : "ascending")
          : "none");
    });
  } catch (e) {
    _dbg("loadVoice ERROR", e.message);
    $("#voice-summary").textContent = "error: " + e.message;
    tbody.innerHTML = `<tr><td colspan="7" class="empty fail">
      error: ${escape(e.message)}</td></tr>`;
  }
}

function openVoiceModal(trxId) {
  modalLoad({
    id: "voice-modal",
    title: `Transcript · ${trxId}`,
    loader: () => apiRoot(`/voice/transcripts/${encodeURIComponent(trxId)}`),
    render: renderVoiceDetail,
    watchdogDetail: "Leyendo el JSONL del transcript en disco.",
  });
}

function renderVoiceDetail(t) {
  const stt = t.stt || {};
  const metaRows = [
    ["id", `<code>${escape(t.id || "—")}</code>`],
    ["fecha", escape(t.ts || "—")],
    ["autor", escape(t.author || "—")],
    ["modo", escape(t.mode || "—")],
    ["canal", escape(t.discord_channel || "—")],
    ["duración", t.audio?.duration_s != null
      ? `${Number(t.audio.duration_s).toFixed(1)}s` : "—"],
    ["participantes", escape((t.participants || []).join(", ") || "—")],
    ["tema", escape(t.topic || "—")],
    ["proyecto", escape(t.related_project || "—")],
    ["STT", `${escape(stt.provider || "?")} · ${escape(stt.model || "?")}
      · ${escape(stt.language || "?")} · ${stt.duration_ms_stt ?? "?"}ms`],
  ];
  let html = `<table class="status-detail"><tbody>${
    metaRows.map(([k, v]) =>
      `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody></table>`;
  html += `<h4>Transcript</h4>
    <pre class="chat-md">${escape(t.transcript || "(vacío)")}</pre>`;
  if (t.processed?.summary) {
    html += `<h4>Procesado (${escape(t.processed.model || "?")}
      · ${escape(t.processed.ts || "")})</h4>
      <pre class="chat-md">${escape(t.processed.summary)}</pre>`;
  }
  return html;
}

// ---- botón "📝 Issue" -----------------------------------------------
//
// Modal chico: modo (issue | user_story) + repo opcional. Al enviar
// dispara el endpoint on-demand y muestra el markdown generado en un
// SEGUNDO modal con botón "Copiar".

function openIssueModal(trxId) {
  const body = `
    <p class="muted mb-3">Skill <code>${escape(ISSUE_SKILL)}</code>
      aplicada al transcript <code>${escape(trxId)}</code>. El LLM
      emite markdown listo para pegar — NO abre nada en GitHub.</p>
    <label class="label mb-2">modo
      <select id="issue-mode" class="select w-full mt-1">
        <option value="issue">Issue de GitHub (resumen + comportamiento esperado)</option>
        <option value="user_story">Historia de usuario (Como/Quiero/Para + criterios de aceptación)</option>
      </select>
    </label>
    <label class="label mb-2">repo <span class="muted">(opcional — formato <code>owner/name</code>)</span>
      <input id="issue-repo" type="text" class="input w-full mt-1"
        placeholder="ej: AuroraDemo/PortalDemo">
    </label>
    <p class="muted text-xs mt-1">Si el transcript menciona un repo
      explícito, ese gana. Si no, queda como "Pendiente de confirmar"
      en el markdown.</p>
    <div class="flex items-center justify-end gap-2 mt-4">
      <button class="btn" data-close>Cancelar</button>
      <button class="btn btn-primary" id="issue-submit">Generar</button>
    </div>`;
  modalLoad({
    id: "status-modal",
    title: `Crear issue / historia · ${trxId}`,
    loader: () => Promise.resolve({}),
    render: () => body,
    after: () => {
      onClick("#issue-submit", async () => {
        const mode = $("#issue-mode").value;
        const repo = $("#issue-repo").value.trim();
        const btn = $("#issue-submit");
        btn.disabled = true;
        btn.textContent = "Generando…";
        try {
          const resp = await api(
            `skills/${encodeURIComponent(ISSUE_SKILL)}/apply-to-transcript`,
            {
              method: "POST",
              body: JSON.stringify({
                transcript_id: trxId, mode, repo,
              }),
            },
            120_000,  // el LLM puede tardar; subimos el watchdog
          );
          // cerramos el modal de inputs y abrimos el de resultado
          $("#status-modal").classList.remove("open");
          openIssueResultModal(trxId, mode, repo, resp);
        } catch (e) {
          toast("error al generar: " + e.message, "err");
          btn.disabled = false;
          btn.textContent = "Generar";
        }
      });
    },
  });
}

function openIssueResultModal(trxId, mode, repo, resp) {
  const output = resp.output || "(vacío)";
  const meta = [
    ["transcript", `<code>${escape(trxId)}</code>`],
    ["modo", escape(mode)],
    ["repo", escape(repo || "(no provisto)")],
    ["modelo", `<code>${escape(resp.model || "?")}</code>`],
    ["tokens in/out", `${resp.tokens_in ?? "?"} / ${resp.tokens_out ?? "?"}`],
    ["duración", resp.duration_ms != null ? `${resp.duration_ms}ms` : "—"],
  ];
  const metaHtml = `<table class="status-detail"><tbody>${
    meta.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")
  }</tbody></table>`;
  const body = `
    ${metaHtml}
    <h4>Markdown generado (copia y pega en GitHub)</h4>
    <pre id="issue-output" class="chat-md">${escape(output)}</pre>
    <div class="flex items-center justify-end gap-2 mt-3">
      <button class="btn" data-close>Cerrar</button>
      <button class="btn btn-primary" id="issue-copy">Copiar markdown</button>
    </div>`;
  modalLoad({
    id: "status-modal",
    title: `Issue generado · ${trxId}`,
    loader: () => Promise.resolve({}),
    render: () => body,
    after: () => {
      onClick("#issue-copy", async () => {
        try {
          await navigator.clipboard.writeText(output);
          toast("copiado al portapapeles ✓", "ok", 2000);
        } catch (e) {
          // fallback a textarea hack
          const ta = document.createElement("textarea");
          ta.value = output; document.body.appendChild(ta);
          ta.select();
          try {
            document.execCommand("copy");
            toast("copiado ✓", "ok", 2000);
          } catch (_) {
            toast("no se pudo copiar: " + e.message, "err");
          } finally {
            document.body.removeChild(ta);
          }
        }
      });
    },
  });
}

export function initVoice() {
  onClick("#voice-refresh", loadVoice);
  // Delegación sobre el wrapper: el tbody se reescribe en cada refresh,
  // los listeners por fila morirían. Atados al wrapper, capturan los
  // botones nuevos sin rewirear.
  const el = $("#voice-table");
  if (el && !el.dataset.wired) {
    el.dataset.wired = "1";
    el.addEventListener("click", (e) => {
      const issueBtn = e.target.closest(".voice-issue-btn");
      if (issueBtn) {
        e.stopPropagation();
        if (issueBtn.disabled) {
          toast(`Aprueba la skill "${ISSUE_SKILL}" primero (tab Skills)`, "warn");
          return;
        }
        openIssueModal(issueBtn.dataset.id);
        return;
      }
      const row = e.target.closest(".voice-row");
      if (row) openVoiceModal(row.dataset.id);
    });
    // Orden por <th>: click y teclado (Enter/Espacio), igual que
    // ui-table.js#_sortFrom — el thead es estático en index.html, así
    // que no migramos a dataTable(), pero el wiring copia el mismo
    // patrón de accesibilidad.
    const thead = el.querySelector("thead");
    if (thead && !thead.dataset.sortWired) {
      thead.dataset.sortWired = "1";
      const sortFrom = (target) => {
        const th = target.closest?.(".th-sort");
        if (!th) return;
        const i = Number(th.dataset.col);
        const col = _VOICE_COLUMNS[i];
        if (!col || col.sortable === false) return;
        if (_voiceState.sortKey === i) {
          _voiceState.sortDir = _voiceState.sortDir === "asc" ? "desc" : "asc";
        } else {
          _voiceState.sortKey = i;
          _voiceState.sortDir = "asc";
        }
        _voiceState.page = 0;
        loadVoice();
      };
      thead.addEventListener("click", (e) => sortFrom(e.target));
      thead.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortFrom(e.target); }
      });
    }
  }
}
