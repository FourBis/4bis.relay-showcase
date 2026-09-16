// Tab Night Runs (Iter 5.4): lista + detalle de TODAS las corridas del
// modo nocturno. El backend ya tenía /admin/api/projects/{slug}/night
// con limit=5 — esto es la vista dedicada que faltaba: paginación,
// filtro por proyecto, detalle con plan ledger + tasks + branch + PR.

import { $, $$, api, apiRoot, escape, _dbg, onClick } from "./api.js";
import { modalLoad, confirmModal } from "./ui.js";

// Render de la tabla principal (lista de corridas).
export async function loadNightRuns() {
  const limit = $("#night-runs-limit").value;
  const project = $("#night-runs-project").value;
  const params = new URLSearchParams({ limit });
  if (project) params.set("project", project);
  try {
    // Endpoint vive en /admin/api/ (ver admin.py). api() lo prepende;
    // apiRoot() pegaría a la raíz y 404'ea.
    const r = await api(`night-runs?${params}`);
    _dbg("loadNightRuns ok", "count:", r.count);
    const tbody = $("#night-runs-table tbody");
    if (!r.runs.length) {
      tbody.innerHTML = "";
      $("#night-runs-empty").hidden = false;
      $("#night-runs-summary").textContent = "0 corridas";
      return;
    }
    $("#night-runs-empty").hidden = true;
    tbody.innerHTML = r.runs.map((run) => {
      const cls = run.end_reason === "completed" ? "ok"
                : run.end_reason === "crashed" ? "err"
                : run.end_reason === "deadline" ? "warn"
                : "dim";
      const pr = run.pr_url
        ? `<a href="${escape(run.pr_url)}" target="_blank" rel="noopener">PR ↗</a>`
        : "—";
      return `<tr data-run="${escape(run.id)}">
        <td class="whitespace-nowrap">${escape(run.started_at || "")}</td>
        <td><code>${escape(run.project_slug || "")}</code></td>
        <td><code class="text-xs">${escape(run.id || "")}</code></td>
        <td><span class="badge ${cls}">${escape(run.end_reason || "?")}</span></td>
        <td class="tabular-nums">${run.tasks_done}/${run.tasks_discarded}</td>
        <td>${pr}</td>
        <td><code class="text-xs">${escape(run.branch || "—")}</code></td>
        <td class="row-actions">
          <button class="btn btn-xs night-run-detail-btn" data-run="${escape(run.id)}"
                  title="ver detalle">Detalle</button>
        </td>
      </tr>`;
    }).join("");
    $$(".night-run-detail-btn").forEach((b) =>
      b.addEventListener("click", () => openNightRunDetail(b.dataset.run)));
    $("#night-runs-summary").textContent =
      `${r.count} corrida${r.count === 1 ? "" : "s"}`;
  } catch (e) {
    _dbg("loadNightRuns ERROR", e.message);
    $("#night-runs-summary").textContent = "error: " + e.message;
  }
}

// Detalle de una corrida: tasks del plan, branch, PR, reporte, snapshot vivo.
export function openNightRunDetail(runId) {
  modalLoad({
    id: "night-run-detail-modal",
    title: `Night run · ${runId.slice(0, 12)}…`,
    loader: () => api(`night-runs/${encodeURIComponent(runId)}`),
    render: renderNightRunDetail,
  });
}

function renderNightRunDetail(r) {
  const run = r.run || {};
  const tasks = r.plan_tasks || [];
  const isCrash = run.end_reason === "crashed";
  // ponytail: cuando el run crasheó SIN haber escrito plan.md (caso típico:
  // falla en Fase 0/branch prep, ej: working tree sucio), el "error" del
  // DB es lo único diagnóstico. Subirlo arriba en bloque own-line en lugar
  // de una mini-row de tabla, que el usuario tiene que cazar con la lupa.
  let html = "";
  if (isCrash && tasks.length === 0) {
    html +=
      `<div class="rounded-md border border-red-700/60 bg-red-950/40 p-3 mb-4">
         <div class="text-xs uppercase tracking-wide text-red-300 mb-1">
           Error de arranque
         </div>
         <pre class="whitespace-pre-wrap text-sm text-red-200 m-0 font-mono">${
           escape(run.error || "(sin mensaje — ver reporte y logs del server)")
         }</pre>
       </div>`;
  }
  const cells = [];
  cells.push(["run_id", `<code>${escape(run.id)}</code>`]);
  cells.push(["proyecto", `<code>${escape(run.project_slug)}</code>`]);
  cells.push(["started", escape(run.started_at || "—")]);
  cells.push(["deadline", escape(run.deadline_at || "—")]);
  cells.push(["ended", escape(run.ended_at || "— (vivo)")]);
  cells.push(["end_reason", `<span class="badge ${run.end_reason === 'completed' ? 'ok' : run.end_reason === 'crashed' ? 'err' : 'warn'}">${escape(run.end_reason || "?")}</span>`]);
  cells.push(["tareas", `${run.tasks_done || 0} done · ${run.tasks_discarded || 0} discarded`]);
  cells.push(["PRs", run.prs_opened || 0]);
  cells.push(["branch", `<code class="text-xs">${escape(run.branch || "—")}</code>`]);
  if (run.pr_url) {
    cells.push(["PR", `<a href="${escape(run.pr_url)}" target="_blank" rel="noopener">${escape(run.pr_url)}</a>`]);
  }
  if (run.report_path) {
    cells.push(["reporte", `<code class="text-xs">${escape(run.report_path)}</code>`]);
  }
  if (run.error && !(isCrash && tasks.length === 0)) {
    // runs con plan + error residual (ej: tareas que fallaron de a una)
    cells.push(["error", `<span class="fail">${escape(run.error)}</span>`]);
  }
  html += `<table class="status-detail"><tbody>${
    cells.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody></table>`;

  // Plan ledger: tasks parseadas del espejo state/. Render con badges
  // por estado (done / pending / discarded).
  if (tasks.length) {
    html += `<h4>Plan ledger (${tasks.length} tareas)</h4>
      <table class="status-detail"><thead>
        <tr><th class="th">id</th><th class="th">título</th><th class="th">refs</th><th class="th">status</th><th class="th">nota</th></tr>
      </thead><tbody>`;
    for (const t of tasks) {
      const cls = t.status === "done" ? "ok" : t.status === "discarded" ? "err" : "warn";
      html += `<tr>
        <td><code>${escape(t.id)}</code></td>
        <td>${escape(t.title)}</td>
        <td class="text-xs">${(t.refs || []).map(r => `<code>${escape(r)}</code>`).join("<br>")}</td>
        <td><span class="badge ${cls}">${escape(t.status)}</span></td>
        <td class="text-xs">${escape(t.note || "—")}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<p class="muted mt-3">No hay plan ledger todavía (¿sigue corriendo
      o falló antes de Fase 1?).</p>`;
  }

  // Snapshot vivo (si el orquestador corre este run ahora).
  if (r.snapshot) {
    html += `<h4>Snapshot vivo</h4>
      <pre class="chat-md">${escape(JSON.stringify(r.snapshot, null, 2))}</pre>`;
  }

  // Botón para abrir el reporte .md en otra tab.
  if (run.report_path) {
    html += `<div class="mt-4 flex gap-2">
      <button class="btn" id="night-run-open-md">Ver reporte completo (.md)</button>
    </div>`;
    // El wire del botón lo hace modalLoad.after si existe, pero como
    // render se ejecuta una sola vez, atacheo acá directamente:
    setTimeout(() => {
      onClick("#night-run-open-md", () => openNightRunReport(run.id));
    }, 0);
  }
  return html;
}

async function openNightRunReport(runId) {
  try {
    const r = await api(`night-runs/${encodeURIComponent(runId)}/report`);
    // El reporte viene como JSON con campo `text` o `error`. Si tiene
    // text, abrimos el raw md en una ventana nueva.
    if (r.text) {
      const blob = new Blob([r.text], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    }
  } catch (e) {
    $("#night-runs-summary").textContent = "error reporte: " + e.message;
  }
}

// Carga proyectos con night_mode_enabled=1 para el filtro dropdown.
export async function loadNightProjectsForFilter() {
  try {
    const { projects } = await api("projects");
    const sel = $("#night-runs-project");
    const previous = sel.value;
    // Mantener el placeholder y agregar solo proyectos con night habilitado.
    const enabled = (projects || []).filter((p) => p.night_mode_enabled);
    sel.innerHTML = `<option value="">— todos los proyectos —</option>` +
      enabled.map((p) =>
        `<option value="${escape(p.slug)}">${escape(p.name || p.slug)}</option>`
      ).join("");
    if (previous && enabled.some((p) => p.slug === previous)) {
      sel.value = previous;
    }
  } catch (e) {
    _dbg("loadNightProjectsForFilter error", e.message);
  }
}

// Botón "Arrancar night mode" desde este tab: muestra el mismo modal
// que usa Proyectos pero con selector limitado a proyectos con
// night_mode_enabled=1. Reutilizamos el flujo de /night-mode/start.
export async function startNightFromTab(slug) {
  const ok = await confirmModal({
    title: `Arrancar night mode · ${slug}`,
    body: "El orquestador va a planificar y ejecutar las tareas hasta el deadline. "
    + "Puedes seguir el progreso en esta misma pantalla.",
    confirmText: "Arrancar",
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await apiRoot("/night-mode/start", {
      method: "POST",
      body: JSON.stringify({ project: slug }),
    });
    $("#night-runs-summary").textContent =
      `arrancado: run_id=${r.run_id}, deadline=${r.deadline_at}`;
    loadNightRuns();
  } catch (e) {
    $("#night-runs-summary").textContent = "error start: " + e.message;
  }
}

export function initNightRuns() {
  onClick("#night-runs-refresh", () => {
    loadNightProjectsForFilter();
    loadNightRuns();
  });
  $("#night-runs-project").onchange = loadNightRuns;
  $("#night-runs-limit").onchange = loadNightRuns;
  // Carga inicial del dropdown de proyectos (no falla la lista si
  // todavía no se cargó).
  loadNightProjectsForFilter();
}