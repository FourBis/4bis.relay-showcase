// Workspace: los módulos conservan un único DOM y sus handlers. No iframes,
// duplicados de formularios ni copias locales de datos del servidor.
import { toast } from './ui.js';

const STORAGE = '4bis.workspace.v1';
const windows = new Map();
let modules = new Map(), loaders = {}, activeId = '', restoreObject;
let stage, layer, dock, launcher;
let saveTimer;
let focusOrder = 0;
const mobile = () => matchMedia('(max-width: 760px)').matches;
const descriptions = {
  chat: ['Conversar', 'Conversaciones, respuestas y memoria'],
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
const paths = {
  close: 'M6 6l12 12M6 18L18 6', minimize: 'M5 16h14',
  maximize: 'M8 3H3v5M16 3h5v5M21 16v5h-5M8 21H3v-5',
  restore: 'M8 8h13v13H8zM3 16V3h13',
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
      seen.add(w.id); return true;
    }).map(w => ({ id: w.id, module: w.module || null,
      title: typeof w.title === 'string' ? w.title.slice(0, 120) : 'Resultado del chat',
      restore: w.module ? null : w.restore,
      rect: { x: number(w.rect?.x, 0), y: number(w.rect?.y, 0), w: number(w.rect?.w, 860), h: number(w.rect?.h, 640) },
      closed: w.closed === true, minimized: w.minimized === true, maximized: w.maximized === true,
    }));
  } catch { return []; }
}

function bounds() { return { width: stage.clientWidth, height: stage.clientHeight }; }
function announce(text) { document.getElementById('workspace-announcement').textContent = text; }
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const saved = [...windows.values()].filter(w => w.module || w.restore).map(w => ({
      id: w.id, module: w.module, title: w.title, restore: w.restore,
      rect: w.rect, closed: w.closed, minimized: w.minimized, maximized: w.maximized,
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
      btn.addEventListener('click', () => activate(w, true)); dock.append(btn);
    }
    btn.textContent = w.title;
    btn.title = `${w.minimized ? 'Restaurar' : 'Mostrar'} ${w.title}`;
    btn.setAttribute('aria-pressed', String(!w.minimized && activeId === w.id));
    btn.classList.toggle('is-minimized', w.minimized);
  }
}

function closeWindow(w) {
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
  for (const [action, label] of [['minimize', 'Minimizar'], ['maximize', 'Expandir'], ['close', 'Cerrar']]) {
    const button = document.createElement('button'); button.className = 'workspace-icon';
    button.dataset.windowAction = action; button.innerHTML = svg(action); button.title = label;
    button.setAttribute('aria-label', `${label} ${w.title}`);
    button.onclick = () => {
      if (action === 'close') closeWindow(w);
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

export function openObject(options) {
  if (!(options.content instanceof HTMLElement) || typeof options.id !== 'string') return null;
  // Los identificadores externos nunca pueden suplantar un módulo del sistema.
  const id = `object:${options.id.replace(/^object:/, '')}`;
  let w = windows.get(id);
  if (!w) w = makeWindow({ ...options, id, module: null });
  w.body.replaceChildren(options.content); w.restore = options.restore || w.restore;
  activate(w, true); return w.el;
}

async function restoreSavedObject(saved) {
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
  const closed = [...windows.values()].filter(w => w.closed);
  if (!closed.length) return;
  const title = document.createElement('h3'); title.className = 'workspace-group-title'; title.textContent = 'Cerrados recientemente'; root.append(title);
  closed.slice(-8).reverse().forEach(w => {
    const b = document.createElement('button'); b.className = 'workspace-recent-item'; b.textContent = w.title;
    b.onclick = () => {
      launcher.close();
      if (w.module) openWorkspaceModule(w.module);
      else if (!w.body.childElementCount) restoreSavedObject(w);
      else activate(w, true);
    }; root.append(b);
  });
}

export function openWorkspaceLauncher(query = '') {
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

export function initWorkspace({ moduleLoaders, restoreChatObject }) {
  const originalHash = location.hash;
  loaders = moduleLoaders; restoreObject = restoreChatObject;
  stage = document.getElementById('main'); layer = document.getElementById('workspace-windows');
  dock = document.getElementById('workspace-items'); launcher = document.getElementById('workspace-launcher-dialog');
  const nav = document.getElementById('workspace-modules');
  const buttons = [...nav.querySelectorAll('.tab')]; nav.replaceChildren();
  for (const groupName of ['Conversar', 'Trabajar', 'Observar', 'Preparar', 'Configurar']) {
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
  document.querySelectorAll('[data-workspace-open]').forEach(b => b.onclick = () => openWorkspaceModule(b.dataset.workspaceOpen));
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
  if (initial === 'workspace' && saved.length) nextActive();
  else openWorkspaceModule(modules.has(initial) ? initial : 'chat');
  window.addEventListener('hashchange', () => openWorkspaceModule(route() || 'chat'));
  visibility();
}
