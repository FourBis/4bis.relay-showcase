// Workspace: los módulos conservan su DOM; cada chat separado tiene su
// propio contexto de navegador para aislar borrador, selección y polling.
import { toast, confirmModal } from './ui.js';
import { createConversationFrame } from './chat-window.js';

const STORAGE = '4bis.workspace.v1';
const windows = new Map();
let modules = new Map(), loaders = {}, activeId = '', restoreObject;
let stage, layer, dock, launcher;
let saveTimer;
let focusOrder = 0;
let closeOrder = 0;
let taskContext = { conversationId: '', projectSlug: '' };
let primaryTaskContext = taskContext;
const mobile = () => matchMedia('(max-width: 760px)').matches;
const descriptions = {
  chat: ['Conversar', 'Conversaciones, respuestas y memoria'],
  account: ['Cuenta', 'Conexiones personales y Gmail'],
  team: ['Trabajar', 'Integrantes y permisos de Relay'],
  projects: ['Trabajar', 'Proyectos, agentes y repositorios'],
  gestion: ['Trabajar', 'Tareas, responsables y seguimiento'],
  crm: ['Trabajar', 'Clientes y oportunidades'],
  diagrams: ['Trabajar', 'Arquitectura y relaciones del código'],
  running: ['Observar', 'Ejecuciones y decisiones pendientes'],
  status: ['Observar', 'Salud del relay y conexiones'],
  metrics: ['Observar', 'Rendimiento, tendencias y resultados'],
  report: ['Observar', 'Consumo por proyecto y modelo'],
  'night-runs': ['Observar', 'Trabajo nocturno y resultados'],
  zombies: ['Observar', 'Ejecuciones que necesitan atención'],
  logs: ['Observar', 'Registro de actividad en vivo'],
  skills: ['Preparar', 'Capacidades y conocimiento reutilizable'],
  voice: ['Preparar', 'Voces y reproducción de audio'],
  index: ['Preparar', 'Repositorios disponibles para los agentes'],
  orphans: ['Preparar', 'Índices sin proyecto asociado'],
  commands: ['Configurar', 'Comandos y herramientas de ejecución'],
  mcp: ['Configurar', 'Conexiones, herramientas y bases de datos'],
  models: ['Configurar', 'Catálogo y disponibilidad de modelos'],
  config: ['Configurar', 'Preferencias y comportamiento del relay'],
};

export function setTaskWorkspaceContext({ conversationId = '', projectSlug = '' } = {}) {
  primaryTaskContext = { conversationId: String(conversationId || ''),
    projectSlug: String(projectSlug || '') };
  if (!activeId || activeId === 'chat') taskContext = primaryTaskContext;
}

export function taskWorkspaceQuery(projectSlug, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== '' && value != null) query.set(key, value);
  }
  if (taskContext.conversationId && taskContext.projectSlug === projectSlug) {
    query.set('conversation', taskContext.conversationId);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}
const paths = {
  close: 'M6 6l12 12M6 18L18 6', minimize: 'M5 16h14',
  maximize: 'M8 3H3v5M16 3h5v5M21 16v5h-5M8 21H3v-5',
  restore: 'M8 8h13v13H8zM3 16V3h13',
  rename: 'M16 3l5 5L8 21H3v-5zM14 5l5 5',
};
const svg = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
const number = (n, fallback) => typeof n === 'number' && Number.isFinite(n) ? n : fallback;

export function fitRect(rect, bounds) {
  const width = Math.max(1, number(bounds.width, 1));
  const height = Math.max(1, number(bounds.height, 1));
  const w = Math.min(width, Math.max(Math.min(420, width), number(rect.w, 860)));
  const h = Math.min(height, Math.max(Math.min(300, height), number(rect.h, 640)));
  return { x: Math.round(Math.max(0, Math.min(width - w, number(rect.x, 0)))),
    y: Math.round(Math.max(0, Math.min(height - h, number(rect.y, 0)))),
    w: Math.round(w), h: Math.round(h) };
}

export function tileRects(count, bounds) {
  if (!count) return [];
  // ponytail: mosaico regular, sin solver de packing. Si crece el volumen,
  // agrupar por proyecto antes de incorporar un canvas infinito.
  const columns = Math.min(count, Math.max(1, Math.floor(bounds.width / 440)));
  const rows = Math.ceil(count / columns), gap = 12;
  const w = (bounds.width - gap * (columns - 1)) / columns;
  const h = (bounds.height - gap * (rows - 1)) / rows;
  return Array.from({ length: count }, (_, i) => fitRect({
    x: (i % columns) * (w + gap), y: Math.floor(i / columns) * (h + gap), w, h,
  }, bounds));
}

export function readLayout(raw, validModules) {
  try {
    const data = JSON.parse(raw || '{}');
    if (data.version !== 1 || !Array.isArray(data.windows)) return [];
    const seen = new Set();
    return data.windows.slice(0, 40).filter(w => {
      if (!w || typeof w.id !== 'string' || w.id.length > 240 || seen.has(w.id)) return false;
      if (w.module ? !validModules.includes(w.module) || w.id !== w.module :
        !w.restore || typeof w.restore.conversationId !== 'string' || w.restore.conversationId.length > 160) return false;
      if (!w.module && w.restore.kind === 'conversation' &&
        (!/^[\w-]{1,160}$/.test(w.restore.conversationId) ||
          w.id !== `object:conversation:${w.restore.conversationId}`)) return false;
      seen.add(w.id); return true;
    }).map(w => ({ id: w.id, module: w.module || null,
      title: w.restore?.kind === 'conversation'
        ? conversationTitle(w.title, w.restore.conversationId, w.customTitle)
        : typeof w.title === 'string' ? w.title.slice(0, 120) : 'Resultado del chat',
      customTitle: typeof w.customTitle === 'string' ? w.customTitle.trim().slice(0, 120) : '',
      restore: w.module ? null : w.restore,
      rect: { x: number(w.rect?.x, 0), y: number(w.rect?.y, 0), w: number(w.rect?.w, 860), h: number(w.rect?.h, 640) },
      closed: w.closed === true, closedAt: number(w.closedAt, 0),
      minimized: w.minimized === true, maximized: w.maximized === true,
    }));
  } catch { return []; }
}

function bounds() { return { width: stage.clientWidth, height: stage.clientHeight }; }
function announce(text) { document.getElementById('workspace-announcement').textContent = text; }
function conversationTitle(title, id, customTitle) {
  if (typeof customTitle === 'string' && customTitle.trim()) return customTitle.trim().slice(0, 120);
  const label = typeof title === 'string' && title ? title : 'Conversación';
  // Los layouts anteriores incluían el ID corto en el nombre visible.
  const suffix = ` · ${id.slice(0, 8)}`;
  return (label.endsWith(suffix) ? label.slice(0, -suffix.length) : label).slice(0, 120);
}

function setWindowTitle(w, title) {
  if (w.title === title) return;
  w.title = title;
  w.el.setAttribute('aria-label', title);
  w.el.querySelector('.workspace-window-title').textContent = title;
  w.el.querySelector('.workspace-window-head').setAttribute('aria-label',
    `${title}. Flechas para mover; Mayús y flechas para redimensionar; Enter para expandir.`);
  w.el.querySelectorAll('[data-window-action]').forEach(button =>
    button.setAttribute('aria-label', `${button.title} ${title}`));
  if (w.frame) { w.frame.title = title; w.frame.setAttribute('aria-label', title); }
  renderDock(); persist();
}

function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const saved = [...windows.values()].filter(w => w.module || w.restore).map(w => ({
      id: w.id, module: w.module, title: w.title, customTitle: w.customTitle, restore: w.restore,
      rect: w.rect, closed: w.closed, closedAt: w.closedAt, minimized: w.minimized, maximized: w.maximized,
    }));
    try { localStorage.setItem(STORAGE, JSON.stringify({ version: 1, windows: saved.slice(-40) })); }
    catch { /* El workspace sigue funcionando aunque el storage no esté disponible. */ }
  }, 120);
}

function place(w) {
  if (!mobile()) w.rect = fitRect(w.rect, bounds());
  const r = w.maximized || mobile() ? { x: 0, y: 0, w: stage.clientWidth, h: stage.clientHeight } : w.rect;
  Object.assign(w.el.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
  w.el.classList.toggle('is-maximized', w.maximized);
  const maximize = w.el.querySelector('[data-window-action="maximize"]');
  maximize.innerHTML = svg(w.maximized ? 'restore' : 'maximize');
  maximize.setAttribute('aria-label', `${w.maximized ? 'Restaurar tamaño de' : 'Expandir'} ${w.title}`);
  maximize.title = w.maximized ? 'Restaurar tamaño' : 'Expandir';
}

function visibility() {
  let visible = 0;
  for (const w of windows.values()) {
    const hidden = w.closed || w.minimized || (mobile() && activeId !== w.id);
    w.el.hidden = hidden;
    w.el.classList.toggle('is-active', activeId === w.id);
    w.el.classList.toggle('is-minimized', w.minimized);
    w.frame?.contentWindow?.postMessage({ type: 'relay-chat-visibility', visible: !hidden }, location.origin);
    if (w.module) modules.get(w.module).panel.hidden = hidden;
    if (!hidden) visible++;
  }
  document.getElementById('workspace-welcome').hidden = visible > 0;
  renderDock();
}

function activate(w, focus = false) {
  w.closed = false; w.minimized = false; activeId = w.id;
  // Cambiar apilado sin reinsertar el DOM: mover el nodo quitaría el foco
  // de inputs al llegar a una ventana de atrás con Tab.
  w.order = ++focusOrder;
  if (w.module === 'chat') taskContext = primaryTaskContext;
  else if (w.context) taskContext = w.context;
  [...windows.values()].sort((a, b) => (a.order || 0) - (b.order || 0))
    .forEach((entry, i) => { entry.el.style.zIndex = i + 1; });
  visibility(); place(w);
  if (w.module) {
    const current = location.hash.match(/^#\/([a-z-]+)/)?.[1];
    if (current !== w.module) history.replaceState(null, '', w.el.dataset.route || `#/${w.module}`);
  }
  if (focus) w.el.querySelector('.workspace-window-head').focus({ preventScroll: true });
  persist();
}

function nextActive() {
  const w = [...windows.values()].filter(w => !w.closed && !w.minimized)
    .sort((a, b) => (b.order || 0) - (a.order || 0))[0];
  const el = w?.el;
  activeId = w?.id || '';
  if (w?.module === 'chat') taskContext = primaryTaskContext;
  else if (w?.context) taskContext = w.context;
  history.replaceState(null, '', `#/${w?.module || 'workspace'}`);
  visibility();
  if (el) el.querySelector('.workspace-window-head').focus({ preventScroll: true });
  else document.getElementById('workspace-launcher').focus();
}

function renderDock() {
  // Mantener nodos de botones: no perder foco al activar una ventana con teclado.
  const open = [...windows.values()].filter(w => !w.closed);
  for (const child of [...dock.children]) if (!open.some(w => w.id === child.dataset.windowId)) child.remove();
  for (const w of open) {
    let btn = [...dock.children].find(b => b.dataset.windowId === w.id);
    if (!btn) {
      btn = document.createElement('button'); btn.className = 'workspace-dock-item';
      btn.dataset.windowId = w.id;
      btn.append(document.createElement('span'));
      btn.addEventListener('click', () => activate(w, true)); dock.append(btn);
    }
    btn.firstElementChild.textContent = w.title;
    btn.title = `${w.minimized ? 'Restaurar' : 'Mostrar'} ${w.title}`;
    btn.setAttribute('aria-pressed', String(!w.minimized && activeId === w.id));
    btn.classList.toggle('is-minimized', w.minimized);
  }
}

function closeWindow(w) {
  if (!w.closed) w.closedAt = ++closeOrder;
  w.closed = true;
  if (w.module) modules.get(w.module).panel.hidden = true;
  w.onClose?.();
  nextActive(); persist(); announce(`${w.title} cerrado. Puedes volver a abrirlo desde Herramientas.`);
}

function maximizeWindow(w) { w.maximized = !w.maximized; activate(w); persist(); }

function pointerGesture(w, target, resize) {
  target.addEventListener('pointerdown', e => {
    if (e.button !== 0 || mobile() || e.target.closest('button, a, input')) return;
    if (w.maximized) return;
    e.preventDefault(); activate(w);
    const start = { ...w.rect }, x = e.clientX, y = e.clientY;
    target.setPointerCapture(e.pointerId); w.el.classList.add('is-moving');
    const move = ev => {
      w.rect = fitRect(resize ? { ...start, w: start.w + ev.clientX - x, h: start.h + ev.clientY - y }
        : { ...start, x: start.x + ev.clientX - x, y: start.y + ev.clientY - y }, bounds());
      place(w);
    };
    const finish = () => {
      target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', finish);
      target.removeEventListener('pointercancel', finish); target.removeEventListener('lostpointercapture', finish);
      if (target.hasPointerCapture(e.pointerId)) target.releasePointerCapture(e.pointerId);
      w.el.classList.remove('is-moving'); persist();
    };
    target.addEventListener('pointermove', move); target.addEventListener('pointerup', finish);
    target.addEventListener('pointercancel', finish); target.addEventListener('lostpointercapture', finish);
  });
}

function makeWindow(options) {
  const count = [...windows.values()].filter(w => !w.closed).length;
  const b = bounds(), chat = options.module === 'chat';
  const w = { rect: fitRect({ x: chat ? (b.width - Math.min(960, b.width - 48)) / 2 : 28 + count * 32,
    y: chat ? 18 : 28 + count * 24, w: chat ? 960 : 850, h: b.height - (chat ? 36 : 72) }, b),
    closed: false, minimized: false, maximized: false, ...options };
  const el = document.createElement('article'); el.className = 'workspace-window';
  el.dataset.windowId = w.id;
  if (w.module) el.dataset.module = w.module;
  el.setAttribute('role', 'region'); el.setAttribute('aria-label', w.title);
  const head = document.createElement('header'); head.className = 'workspace-window-head'; head.tabIndex = 0;
  head.setAttribute('aria-label', `${w.title}. Flechas para mover; Mayús y flechas para redimensionar; Enter para expandir.`);
  const heading = document.createElement('div'); heading.className = 'workspace-window-heading';
  const kind = document.createElement('span'); kind.className = 'workspace-window-kind';
  kind.textContent = w.source || descriptions[w.module]?.[0] || 'Desde el chat';
  const title = document.createElement('h2'); title.className = 'workspace-window-title'; title.textContent = w.title;
  heading.append(kind, title); head.append(heading);
  const actions = document.createElement('div'); actions.className = 'workspace-window-actions';
  const controls = [['minimize', 'Minimizar'], ['maximize', 'Expandir'], ['close', 'Cerrar']];
  if (w.restore?.kind === 'conversation') controls.unshift(['rename', 'Renombrar']);
  for (const [action, label] of controls) {
    const button = document.createElement('button'); button.className = 'workspace-icon';
    button.dataset.windowAction = action; button.innerHTML = svg(action); button.title = label;
    button.setAttribute('aria-label', `${label} ${w.title}`);
    button.onclick = async () => {
      if (action === 'rename') {
        const confirmed = confirmModal({ title: 'Renombrar ventana', body: 'Nombre de la ventana',
          confirmText: 'Guardar' });
        const input = document.createElement('input'); input.className = 'input w-full';
        input.setAttribute('aria-label', 'Nombre de la ventana');
        input.maxLength = 120; input.value = w.title;
        document.getElementById('confirm-modal-body').append(input);
        setTimeout(() => input.focus(), 60);
        if (!await confirmed) return;
        const name = input.value.trim();
        if (name) { w.customTitle = name; setWindowTitle(w, name); persist(); }
      }
      else if (action === 'close') closeWindow(w);
      else if (action === 'maximize') maximizeWindow(w);
      else { w.minimized = true; nextActive(); persist(); }
    };
    actions.append(button);
  }
  head.append(actions);
  const body = document.createElement('div'); body.className = 'workspace-window-body';
  const grip = document.createElement('div'); grip.className = 'workspace-resize'; grip.setAttribute('aria-hidden', 'true');
  el.append(head, body, grip); w.el = el; w.body = body;
  windows.set(w.id, w); layer.append(el);
  pointerGesture(w, head, false); pointerGesture(w, grip, true);
  head.addEventListener('dblclick', e => { if (!e.target.closest('button')) maximizeWindow(w); });
  head.addEventListener('keydown', e => {
    if (e.target !== head || mobile()) return;
    if (e.key === 'Enter') { e.preventDefault(); maximizeWindow(w); return; }
    const delta = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }[e.key];
    if (!delta || w.maximized) return;
    e.preventDefault();
    if (e.shiftKey) { w.rect.w += delta[0]; w.rect.h += delta[1]; }
    else { w.rect.x += delta[0]; w.rect.y += delta[1]; }
    place(w); persist();
  });
  el.addEventListener('pointerdown', () => { if (activeId !== w.id) activate(w); }, true);
  el.addEventListener('focusin', () => { if (activeId !== w.id) activate(w); });
  place(w); return w;
}

function enhanceModule(name, panel) {
  if (panel.dataset.workspaceReady) return;
  panel.dataset.workspaceReady = 'true';
  if (name === 'metrics') {
    const toolbar = panel.querySelector(':scope > .toolbar');
    const details = document.createElement('details'); details.className = 'workspace-filters';
    const summary = document.createElement('summary'); summary.textContent = 'Filtrar y elegir fechas';
    const fields = document.createElement('div'); fields.className = 'toolbar';
    for (const child of [...toolbar.children]) {
      if (child.id === 'metrics-refresh' || child.id === 'metrics-status' || child.querySelector('#metrics-days')) continue;
      fields.append(child);
    }
    details.append(summary, fields); toolbar.after(details);
  }
  if (name !== 'config') return;
  // Mostrar decisiones de configuración por tema; controles e IDs permanecen
  // en el DOM, incluyendo los de secciones cerradas al guardar.
  panel.querySelectorAll('.form-section').forEach((section, i) => {
    const header = section.querySelector(':scope > .form-section-head');
    if (!header || header.querySelector('button, input, select')) return;
    const details = document.createElement('details'); details.className = 'workspace-inspector'; details.open = i === 0;
    const summary = document.createElement('summary'); summary.append(...header.childNodes);
    header.remove(); const body = document.createElement('div'); body.className = 'workspace-inspector-body';
    body.append(...section.childNodes); details.append(summary, body); section.append(details);
  });
}

async function loadModule(w) {
  try { await loaders[w.module]?.(); }
  catch (e) { toast(`No se pudo cargar ${w.title}: ${e.message}`, 'err'); }
}

export function openWorkspaceModule(name, saved) {
  if (!layer && window.parent !== window) {
    window.parent.postMessage({ type: 'relay-chat-module', name }, location.origin);
    return true;
  }
  if (!modules.has(name)) return false;
  const entry = modules.get(name);
  let w = windows.get(name), needsLoad = !w || w.closed;
  if (!w) {
    w = makeWindow({ ...saved, id: name, module: name, title: entry.title });
    entry.panel.hidden = false; w.body.append(entry.panel); enhanceModule(name, entry.panel);
  }
  launcher?.close();
  activate(w, true);
  if (needsLoad) loadModule(w);
  return true;
}

export function openConversationWindow({ id, project_slug, title } = {}, saved) {
  if (!/^[\w-]{1,160}$/.test(id || '')) return null;
  const windowId = `object:conversation:${id}`;
  let w = windows.get(windowId);
  if (!w) w = makeWindow({ ...saved, id: windowId, module: null,
    title: conversationTitle(project_slug || title, id, saved?.customTitle), source: 'Chat',
    restore: { kind: 'conversation', conversationId: id } });
  if (!w.frame) {
    w.frame = createConversationFrame(id, w.title);
    w.body.classList.add('workspace-conversation-body');
    w.body.replaceChildren(w.frame);
    w.frame.addEventListener('load', visibility);
  }
  if (project_slug) w.context = { conversationId: id, projectSlug: project_slug };
  launcher?.close(); activate(w, true);
  return w.el;
}

export function openObject(options) {
  if (!layer && window.parent !== window && options.restore) {
    window.parent.postMessage({ type: 'relay-chat-object', restore: options.restore }, location.origin);
    return null;
  }
  if (!(options.content instanceof HTMLElement) || typeof options.id !== 'string') return null;
  // Los identificadores externos nunca pueden suplantar un módulo del sistema.
  const id = `object:${options.id.replace(/^object:/, '')}`;
  let w = windows.get(id);
  if (!w) w = makeWindow({ ...options, id, module: null });
  w.body.replaceChildren(options.content); w.restore = options.restore || w.restore;
  activate(w, true); return w.el;
}

async function restoreSavedObject(saved) {
  if (saved.restore?.kind === 'conversation') {
    openConversationWindow({ id: saved.restore.conversationId, title: saved.title }, saved);
    return;
  }
  let w = windows.get(saved.id);
  if (!w) w = makeWindow({ ...saved, module: null });
  const message = document.createElement('p'); message.className = 'muted'; message.textContent = 'Recuperando resultado de la conversación…';
  w.body.replaceChildren(message); activate(w);
  try {
    const result = await restoreObject(saved.restore);
    if (!(result?.content instanceof HTMLElement)) throw new Error('El resultado ya no está disponible en la conversación.');
    w.body.replaceChildren(result.content);
    w.el.querySelector('.workspace-window-kind').textContent = result.source || 'Desde el chat';
  } catch (e) {
    message.textContent = `No se pudo recuperar el resultado: ${e.message}`;
    const retry = document.createElement('button'); retry.className = 'btn'; retry.textContent = 'Reintentar';
    retry.onclick = () => restoreSavedObject(saved); w.body.append(retry);
  }
}

function renderRecent() {
  const root = document.getElementById('workspace-recent'); root.replaceChildren();
  const closed = recentlyClosed([...windows.values()]);
  if (!closed.length) return;
  const title = document.createElement('h3'); title.className = 'workspace-group-title'; title.textContent = 'Cerrados recientemente'; root.append(title);
  closed.forEach(w => {
    const b = document.createElement('button'); b.className = 'workspace-recent-item'; b.textContent = w.title;
    b.onclick = () => {
      launcher.close();
      if (w.module) openWorkspaceModule(w.module);
      else if (!w.body.childElementCount) restoreSavedObject(w);
      else activate(w, true);
    }; root.append(b);
  });
}

export function recentlyClosed(entries) {
  return entries.map((w, index) => ({ w, index })).filter(entry => entry.w.closed)
    .sort((a, b) => (b.w.closedAt || 0) - (a.w.closedAt || 0) || b.index - a.index)
    .slice(0, 8).map(entry => entry.w);
}

export function openWorkspaceLauncher(query = '') {
  if (!layer && window.parent !== window) {
    window.parent.postMessage({ type: 'relay-chat-launcher', query }, location.origin);
    return;
  }
  document.getElementById('search-modal')?.classList.remove('open');
  const filter = document.getElementById('workspace-filter'); filter.value = query;
  renderRecent(); filterTools();
  if (!launcher.open) launcher.showModal(); filter.focus();
}

function filterTools() {
  const q = document.getElementById('workspace-filter').value.trim().toLocaleLowerCase();
  let count = 0;
  for (const entry of modules.values()) {
    const match = `${entry.title} ${entry.description} ${entry.name}`.toLocaleLowerCase().includes(q);
    entry.button.hidden = !match; if (match) count++;
  }
  document.querySelectorAll('.workspace-module-group').forEach(group => {
    group.hidden = ![...group.querySelectorAll('.tab')].some(b => !b.hidden);
  });
  document.getElementById('workspace-empty-filter').hidden = count > 0;
}

export function visibleWorkspaceModules() {
  return new Set([...windows.values()].filter(w => w.module && !w.el.hidden).map(w => w.module));
}

export function initWorkspace({ moduleLoaders, restoreChatObject, allowedTabs = null, accessMessage = "" }) {
  const originalHash = location.hash;
  loaders = moduleLoaders; restoreObject = restoreChatObject;
  stage = document.getElementById('main'); layer = document.getElementById('workspace-windows');
  dock = document.getElementById('workspace-items'); launcher = document.getElementById('workspace-launcher-dialog');
  const nav = document.getElementById('workspace-modules');
  window.addEventListener('message', event => {
    if (event.origin !== location.origin) return;
    const w = [...windows.values()].find(entry => entry.frame?.contentWindow === event.source);
    if (!w || w.closed) return;
    if (event.data?.type === 'relay-chat-shortcut' && typeof event.data.shift === 'boolean') {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, shiftKey: event.data.shift }));
      return;
    }
    if (event.data?.type === 'relay-chat-module' && typeof event.data.name === 'string') {
      openWorkspaceModule(event.data.name); return;
    }
    if (event.data?.type === 'relay-chat-launcher' && typeof event.data.query === 'string') {
      openWorkspaceLauncher(event.data.query.slice(0, 160)); return;
    }
    if (event.data?.type === 'relay-chat-object' &&
        event.data.restore?.conversationId === w.restore?.conversationId) {
      restoreObject(event.data.restore).then(openObject)
        .catch(error => toast(`No se pudo abrir el resultado: ${error.message}`, 'err'));
      return;
    }
    if (event.data?.type !== 'relay-chat-context' || event.data.conversationId !== w.restore?.conversationId) return;
    w.context = { conversationId: event.data.conversationId,
      projectSlug: typeof event.data.projectSlug === 'string' ? event.data.projectSlug : '' };
    if (!w.customTitle && w.context.projectSlug) setWindowTitle(w, w.context.projectSlug.slice(0, 120));
    if (document.activeElement === w.frame) activate(w);
    if (activeId === w.id) taskContext = w.context;
  });
  const buttons = [...nav.querySelectorAll('.tab')]
    .filter(button => allowedTabs === null || allowedTabs.has(button.dataset.tab)); nav.replaceChildren();
  document.getElementById('workspace-launcher').hidden = buttons.length === 0;
  document.getElementById('workspace-access-message').hidden = buttons.length > 0;
  document.getElementById('workspace-access-detail').textContent = accessMessage
    || 'Tu cuenta todavía no tiene herramientas asignadas. Pide al administrador que revise Equipo.';
  document.getElementById('workspace-welcome').hidden = buttons.length === 0;
  for (const groupName of ['Conversar', 'Trabajar', 'Observar', 'Preparar', 'Configurar', 'Cuenta']) {
    const group = document.createElement('div'); group.className = 'workspace-module-group';
    const title = document.createElement('h3'); title.className = 'workspace-group-title'; title.textContent = groupName; group.append(title);
    for (const b of buttons) {
      const name = b.dataset.tab, description = descriptions[name];
      if (description?.[0] !== groupName) continue;
      const label = [...b.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('').trim();
      const detail = document.createElement('span'); detail.className = 'workspace-module-description'; detail.textContent = description[1];
      const text = document.createElement('span'); text.className = 'workspace-module-label'; text.textContent = label; text.append(detail);
      [...b.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).forEach(n => n.remove()); b.append(text);
      b.onclick = () => openWorkspaceModule(name); b.tabIndex = 0;
      modules.set(name, { name, title: label, description: description[1], button: b, panel: document.getElementById(`tab-${name}`) });
      group.append(b);
    }
    if (group.children.length > 1) nav.append(group);
  }
  document.getElementById('workspace-launcher').onclick = () => openWorkspaceLauncher();
  document.getElementById('workspace-launcher-close').onclick = () => launcher.close();
  document.getElementById('workspace-filter').addEventListener('input', filterTools);
  launcher.addEventListener('click', e => { if (e.target === launcher) { const r = launcher.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) launcher.close(); } });
  launcher.addEventListener('keydown', e => {
    const list = [...nav.querySelectorAll('.tab')].filter(b => !b.hidden);
    if (e.key === 'Enter' && e.target.id === 'workspace-filter') { e.preventDefault(); list[0]?.click(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); const i = list.indexOf(document.activeElement), d = e.key === 'ArrowDown' ? 1 : -1;
      list[(i + d + list.length) % list.length]?.focus();
    }
  });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); openWorkspaceLauncher(); }
  });
  document.querySelectorAll('[data-workspace-open]').forEach(b => {
    b.hidden = !modules.has(b.dataset.workspaceOpen);
    b.onclick = () => openWorkspaceModule(b.dataset.workspaceOpen);
  });
  document.getElementById('workspace-chat-new').hidden = !modules.has('chat');
  document.getElementById('workspace-chat-new').onclick = () => document.getElementById('chat-new').click();
  document.getElementById('workspace-home').onclick = () => {
    for (const w of windows.values()) if (!w.closed) w.minimized = true;
    activeId = ''; history.replaceState(null, '', '#/workspace'); visibility(); persist();
  };
  document.getElementById('workspace-arrange').onclick = () => {
    if (mobile()) { announce('En esta pantalla se muestra una herramienta a la vez. Usa la bandeja para cambiar.'); return; }
    const open = [...windows.values()].filter(w => !w.closed && !w.minimized);
    const capacity = Math.max(1, Math.floor(stage.clientWidth / 432)) * Math.max(1, Math.floor(stage.clientHeight / 312));
    if (open.length > capacity) {
      toast(`Para ordenar sin superponer, deja hasta ${capacity} ventanas visibles. Minimiza las que no estés usando.`, 'info');
      return;
    }
    const rects = tileRects(open.length, bounds());
    open.forEach((w, i) => { w.maximized = false; w.rect = rects[i]; place(w); });
    persist(); announce(`${open.length} ventanas ordenadas.`);
  };
  new ResizeObserver(() => { for (const w of windows.values()) place(w); visibility(); }).observe(stage);
  let saved = []; try { saved = readLayout(localStorage.getItem(STORAGE), [...modules.keys()]); } catch { /* storage bloqueado */ }
  if (!modules.has('chat')) saved = saved.filter(entry => entry.module || !entry.restore);
  closeOrder = Math.max(closeOrder, ...saved.map(entry => entry.closedAt || 0));
  for (const entry of saved) {
    if (entry.module) {
      if (!entry.closed) { openWorkspaceModule(entry.module, entry); const w = windows.get(entry.id); w.minimized = entry.minimized; }
      else { const w = makeWindow(entry); w.body.append(modules.get(entry.module).panel); enhanceModule(entry.module, modules.get(entry.module).panel); }
    } else if (entry.closed) makeWindow(entry);
    else { restoreSavedObject(entry); windows.get(entry.id).minimized = entry.minimized; }
  }
  const route = () => {
    let name = location.hash.match(/^#\/([a-z-]+)/)?.[1];
    if (['consults', 'chats', 'conversations'].includes(name)) name = 'chat';
    return name;
  };
  // Leer la ruta original antes de que activar ventanas modifique el hash.
  let initial = originalHash.match(/^#\/([a-z-]+)/)?.[1];
  if (['consults', 'chats', 'conversations'].includes(initial)) initial = 'chat';
  if (originalHash) history.replaceState(null, '', originalHash);
  if (modules.size && initial === 'workspace' && saved.length) nextActive();
  else if (modules.size) openWorkspaceModule(modules.has(initial) ? initial : modules.keys().next().value);
  window.addEventListener('hashchange', () => {
    if (modules.size) openWorkspaceModule(modules.has(route()) ? route() : modules.keys().next().value);
  });
  visibility();
}
