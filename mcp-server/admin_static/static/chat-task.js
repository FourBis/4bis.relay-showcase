// Estado durable de una tarea de conversación.
// El backend es la fuente de verdad; este módulo sólo pinta y envía acciones.
import { apiRoot, escape } from "./api.js";
import { toast, confirmModal } from "./ui.js";
import { registerPoller } from "./pollers.js";

const ACTIONS = {
  continue: "Continuar",
  pause: "Pausar",
  cancel: "Cancelar",
  publish: "Publicar PR",
  enable_write: "Habilitar escritura",
};

const requestIds = new Map();
function newRequestId() {
  return globalThis.crypto?.randomUUID?.()
    || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
export function stableTaskRequestId(convId, action) {
  const key = `${convId}:${action}`;
  if (!requestIds.has(key)) requestIds.set(key, newRequestId());
  return requestIds.get(key);
}
export function taskActionPayload(convId, action, extra = {}) {
  const key = action === "track" ? `${action}:${String(!!extra.enabled)}` : action;
  return { action, request_id: stableTaskRequestId(convId, key), ...extra };
}
export function requiresUncertainAcknowledgement(task) {
  return !!task?.uncertain_events?.length;
}

export function taskActionLabel(action) { return ACTIONS[action] || action; }

function stateText(task) {
  if (!task) return "sin tarea persistente";
  const labels = { ready: "lista", running: "en curso", queued: "en cola",
    paused: "pausada", error: "con error", cancelled: "cancelada",
    provisioning: "preparando workspace", implemented: "implementada",
    validating: "validando", publishing: "publicando", review: "pendiente de revisión",
    finished: "finalizada", cleaned: "workspace limpiado", blocked: "bloqueada" };
  return labels[task.state] || task.state || task.mode || "tarea";
}

function attrEscape(value) {
  return String(value || "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) { return ""; }
}

function taskAllows(task, action) {
  return Array.isArray(task?.allowed_actions)
    ? task.allowed_actions.includes(action) : task?.can_control !== false;
}

export function panelHtml(task) {
  if (!task) return `<div class="chat-task-empty">Sin tarea persistente para este hilo.</div>`;
  const tracking = task.tracking || {};
  const terminal = ["cancelled", "finished", "cleaned"].includes(task.state);
  const writeTask = task.mode === "write";
  const hasActionContract = Array.isArray(task.allowed_actions);
  const granted = (action) => taskAllows(task, action);
  const authorized = granted;
  const trackAllowed = authorized("track") && writeTask && !!task.pr_url && !!task.publish_allowed
    && !terminal && !["paused", "provisioning"].includes(task.state);
  const busy = ["running", "validating", "publishing", "provisioning"].includes(task.state);
  const actionAllowed = (action) => authorized(action) && !terminal
    && (action !== "publish" || (writeTask && !!task.publish_allowed
      && !["blocked", "paused", "provisioning"].includes(task.state)))
    && (action !== "continue" || !busy)
    && (action !== "pause" || task.state !== "paused");
  const enableWrite = authorized("enable_write") && !writeTask && !busy && !terminal;
  const limits = tracking.enabled
    ? `<span class="badge warn">seguimiento activo · ${tracking.iterations || 0}/${tracking.max_iterations || 0} iteraciones</span>`
    : `<span class="badge dim">seguimiento apagado</span>`;
  const actions = ["continue", "pause", "cancel", "publish"]
    .filter((a) => (a !== "publish" || writeTask) && (!hasActionContract || granted(a)))
    .map((a) => `<button type="button" class="btn btn-xs${a === "cancel" ? " danger" : a === "publish" ? " btn-primary" : ""}"
      ${actionAllowed(a) ? "" : "disabled"}
      data-task-action="${a}">${taskActionLabel(a)}</button>`).join("");
  const validation = task.validation
    ? `<span class="chat-task-detail">Validación: ${escape(({ok:"correcta",failed:"fallida",stale:"obsoleta",running:"en curso",unconfigured:"sin configurar"})[task.validation.status] || task.validation.status)} · ${escape(task.validation.head_sha || "—")}<br>${escape((task.validation.detail || "").slice(0, 280))}</span>`
    : "";
  const pr = safeHttpUrl(task.pr_url);
  return `<div class="chat-task-head"><strong>Tarea persistente</strong>${limits}</div>
    <div class="chat-task-meta"><span>${escape(stateText(task))}</span>
      <span title="${attrEscape(task.workspace_path)}">${escape(task.branch || task.workspace_path || "sin workspace")}</span>
      ${pr ? `<a href="${attrEscape(pr)}" target="_blank" rel="noopener">PR</a>` : ""}</div>
    ${validation}${task.pending_events ? `<span class="chat-task-detail">${Number(task.pending_events)} mensaje(s) en cola</span>` : ""}<div class="chat-task-actions">${actions}
      ${enableWrite ? `<button type="button" class="btn btn-xs btn-primary" data-task-action="enable_write">${taskActionLabel("enable_write")}</button>` : ""}
      ${writeTask && (!hasActionContract || granted("track")) ? `<label class="chat-task-track"><input type="checkbox" data-task-track ${trackAllowed ? "" : "disabled"}
        ${tracking.enabled ? "checked" : ""} aria-label="Activar seguimiento automático">
        Seguimiento automático <span class="text-xs text-zinc-500">3 iteraciones · 50K tokens · 60 min</span></label>` : ""}</div>
    ${task.error ? `<p class="chat-task-error">${escape(task.error)}</p>` : ""}`;
}

export function mountTaskPanel({ convId, onContinue, onTaskUpdate } = {}) {
  const panel = document.getElementById("chat-task-panel");
  if (!panel) return () => {};
  let current = null;
  let loading = false;
  let disposed = false;
  let unregister = null;
  const render = (task) => {
    const focused = document.activeElement;
    const focusKey = focused?.matches?.("[data-task-action]")
      ? `action:${focused.dataset.taskAction}`
      : focused?.matches?.("[data-task-track]") ? "track" : "";
    current = task;
    panel.hidden = !task;
    panel.innerHTML = panelHtml(task);
    panel.querySelectorAll("[data-task-action]").forEach((button) => {
      button.addEventListener("click", () => runAction(button.dataset.taskAction));
    });
    panel.querySelector("[data-task-track]")?.addEventListener("change", (event) =>
      runTracking(event.target.checked));
    if (focusKey) {
      const target = focusKey === "track"
        ? panel.querySelector("[data-task-track]")
        : panel.querySelector(`[data-task-action="${focusKey.slice(7)}"]`);
      if (target && !target.disabled) target.focus();
      else panel.querySelector("button:not(:disabled)")?.focus();
    }
  };
  const refresh = async () => {
    if (!convId || loading || disposed) return;
    loading = true;
    try {
      const task = await apiRoot(`/conversations/${encodeURIComponent(convId)}/task`);
      if (disposed) return;
      const usable = task?.mode || task?.creation_failed ? task : null;
      const previous = current;
      render(usable);
      await onTaskUpdate?.(usable, previous);
    } catch (e) {
      if (disposed) return;
      panel.hidden = false;
      panel.innerHTML = `<div class="chat-task-error">No se pudo leer la tarea: ${escape(e.message)}</div>`;
    } finally { loading = false; }
  };
  const runAction = async (action) => {
    if (!current || !taskAllows(current, action) || !ACTIONS[action]) return;
    let extra = {};
    if (action === "continue" && requiresUncertainAcknowledgement(current)) {
      const events = current.uncertain_events;
      const approved = await confirmModal({
        title: "Hay un evento incierto",
        body: `El run se interrumpió con ${events.length} evento(s) pendiente(s). Revisa los archivos y el estado antes de continuar.`,
        confirmText: "Revisé y continuar",
        danger: true,
      });
      if (!approved) return;
      extra = { acknowledge_uncertain: true };
    }
    if (action === "enable_write" && !await confirmModal({
      title: "Habilitar escritura para esta tarea",
      body: "Se creará una rama y un worktree de trabajo que conservan el historial de esta tarea. El plan no se ejecutará automáticamente; tendrás que pedir que continúe después.",
      confirmText: "Habilitar escritura",
    })) return;
    if (action === "cancel" && !await confirmModal({
      title: "Cancelar tarea", body: "Se detiene el seguimiento y se conservan los archivos del workspace.",
      confirmText: "Cancelar", danger: true,
    })) return;
    try {
      const next = await apiRoot(`/conversations/${encodeURIComponent(convId)}/task`, {
        method: "POST", body: taskActionPayload(convId, action, extra),
      });
      requestIds.delete(`${convId}:${action}`);
      if (disposed) return;
      render(next?.id ? next : current);
      toast(`${taskActionLabel(action)}: actualizado`, "ok");
      if (action === "continue") onContinue?.();
    } catch (e) { toast(`No se pudo ${taskActionLabel(action).toLowerCase()}: ${e.message}`, "err"); }
  };
  const runTracking = async (enabled) => {
    if (!current || !taskAllows(current, "track")) return;
    try {
      const next = await apiRoot(`/conversations/${encodeURIComponent(convId)}/task`, {
        method: "POST", body: taskActionPayload(convId, "track", {
          enabled, max_iterations: 3, max_tokens: 50000, duration_minutes: 60,
        }),
      });
      requestIds.delete(`${convId}:track:${String(enabled)}`);
      if (disposed) return;
      render(next?.id ? next : { ...current, tracking: { ...(current.tracking || {}), enabled } });
      toast(enabled ? "Seguimiento activado con límites" : "Seguimiento apagado", "ok");
    } catch (e) {
      render(current);
      toast("No se pudo cambiar el seguimiento: " + e.message, "err");
    }
  };
  refresh();
  unregister = registerPoller(refresh, 10_000, { tabId: "chat" });
  return { refresh, destroy: () => {
    disposed = true; unregister?.(); panel.hidden = true; panel.innerHTML = "";
  } };
}
