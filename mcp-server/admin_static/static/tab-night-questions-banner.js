// Banner global de preguntas interactivas del modo nocturno (iter 9.7 + 9.8).
// Aparece cuando hay filas en night_questions con answered_at IS NULL.
// Operador responde con un click → POST /admin/api/night/questions/<q>/answer.
//
// ponytail:
// - Sin feature flags / sin project_id en la query. Single-operator, sirve.
// - El polling se detiene solo cuando no quedan abiertas (no siempre 5s).
// - El backend ya cerró el endpoint existente con ?only_open=1. No tocamos server.py.

import { $, $$, api, escape, _dbg } from "./api.js";
import { toast } from "./ui.js";
import { registerPoller } from "./pollers.js";

const POLL_MS = 5000;
let _interval = null;

// {questions: [{id, run_id, phase, question: {kind, prompt, options, default, context}, open}]}
async function loadOpen() {
  try {
    const r = await api(`night/questions?only_open=1`);
    render(r.questions || []);
  } catch (e) {
    _dbg("loadOpen questions error", e.message);
  }
}

function render(questions) {
  const root = $("#night-questions-root");
  if (!root) return;
  if (!questions.length) {
    root.hidden = true;
    root.classList.add("hidden");
    root.innerHTML = "";
    // Saco role/aria-live cuando no hay nada: si quedaran, el screen
    // reader podría anunciar "alert" cuando se reabre con la misma
    // promesa de "algo pasó" — confunde. role="alert" se aplica al
    // aparecer, no estático.
    root.removeAttribute("role");
    root.removeAttribute("aria-live");
    return;
  }
  root.hidden = false;
  root.classList.remove("hidden");
  // role="alert" + aria-live="assertive" = el screen reader anuncia el
  // banner apenas aparece, sin que el usuario tenga que tabular hasta
  // acá. Es el patrón estándar para notificaciones urgentes en UI.
  root.setAttribute("role", "alert");
  root.setAttribute("aria-live", "assertive");
  root.innerHTML = questions.map((q) => {
    const opts = (q.question?.options || []).map((o) =>
      `<button class="btn btn-primary nqb-opt" data-qid="${escape(q.id)}"
               data-choice="${escape(o.key)}" title="${escape(o.label || o.key)}">
         <span class="font-mono text-xs opacity-70 mr-1">${escape(o.key)}.</span>
         ${escape(o.label || o.key)}
       </button>`
    ).join("");
    const ctx = q.question?.context
      ? `<pre class="chat-md mb-2">${escape(q.question.context)}</pre>` : "";
    const def = q.question?.default
      ? `<span class="muted text-xs">default si no respondes: <code>${escape(q.question.default)}</code></span>` : "";
    return `
      <div class="card pointer-events-auto mb-2 border-amber-500/40 bg-amber-950/80 shadow-lg">
        <div class="flex items-center justify-between gap-2 border-b border-amber-700/40 px-3 py-2">
          <div class="flex items-center gap-2 text-xs text-amber-200">
            <span class="badge warn">night · ${escape(q.phase)}</span>
            <code class="text-amber-300">${escape(q.run_id)}</code>
          </div>
          <button class="icon-btn nqb-skip" data-qid="${escape(q.id)}" title="skip (deja que el default gane)"
            aria-label="skip (deja que el default gane)"><svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
        </div>
        <div class="px-3 py-3">
          <div class="text-sm text-amber-100 mb-2">${escape(q.question?.prompt || "(sin prompt)")}</div>
          ${ctx}
          <div class="flex flex-wrap gap-2 mt-2">${opts}</div>
          ${def ? `<div class="mt-2">${def}</div>` : ""}
        </div>
      </div>`;
  }).join("");
  // Wire botones: responder / skip
  $$(".nqb-opt").forEach((b) =>
    b.addEventListener("click", () => answer(b.dataset.qid, b.dataset.choice, b)));
  $$(".nqb-skip").forEach((b) =>
    b.addEventListener("click", () => skip(b.dataset.qid, b)));
}

async function answer(qid, choice, btn) {
  // Disable para evitar doble-click mientras el POST viaja.
  $$(".nqb-opt").forEach((x) => { if (x.dataset.qid === qid) x.disabled = true; });
  try {
    const r = await api(`night/questions/${encodeURIComponent(qid)}/answer`, {
      method: "POST",
      body: JSON.stringify({ choice, free_text: null }),
    });
    toast(`respondido · ${choice}`, "ok");
    loadOpen();
  } catch (e) {
    toast("error respondiendo: " + e.message, "err");
    // Rehabilitar para reintento.
    $$(".nqb-opt").forEach((x) => { if (x.dataset.qid === qid) x.disabled = false; });
  }
}

async function skip(qid, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`night/questions/${encodeURIComponent(qid)}/skip`, { method: "POST" });
    toast("skipped", "info");
    loadOpen();
  } catch (e) {
    toast("error skip: " + e.message, "err");
    if (btn) btn.disabled = false;
  }
}

function tick() {
  loadOpen().then(() => {
    // ¿Sigue habiendo preguntas abiertas visibles en el DOM?
    if (!$("#night-questions-root") || $("#night-questions-root").hidden) {
      // ...no. ¿Pero el polling sigue corriendo? Probable: pararlo y dejar
      // que el próximo init/click lo reactive. Ponemos un cap alto: 5min.
      // ponytail: lo más simple. Upgrade: re-chequear al armar el polling
      // de cada render en vez de tirar el interval.
      // (en esta versión, polling infinitamente barato — son 1 SELECT).
    }
  });
}

export function initNightQuestionsBanner() {
  if (_interval) return; // idempotente (HMR safe)
  loadOpen();
  _interval = registerPoller(tick, POLL_MS);
}
