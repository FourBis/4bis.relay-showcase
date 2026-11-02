// Listas efímeras por formulario; registrar sigue verificando GitHub en servidor.
import { apiRoot, escape } from "./api.js";

const attr = value => escape(String(value ?? "")).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const labels = kind => kind === "pulls"
  ? {name:"PR abiertos", singular:"PR abierto", select:"Elegir PR abierto", manual:"pegar la URL del PR"}
  : {name:"Issues abiertas", singular:"issue abierta", select:"Elegir issue abierta", manual:"ingresar un número o URL"};

function incompatibleDraft(value, previousRepo, repo, kind) {
  try {
    const url = new URL(value);
    const match = url.pathname.match(/^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/(pull|issues)\/[1-9]\d*\/?$/);
    if (url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username
        && !url.password && !url.search && !url.hash && match
        && match[3] === (kind === "pulls" ? "pull" : "issues")) {
      return `${match[1]}/${match[2]}`.toLowerCase() !== repo.toLowerCase();
    }
  } catch { /* Un borrador incompleto sigue disponible para editarlo. */ }
  return kind === "issues" && /^#?[1-9]\d*$/.test(value.trim())
    && previousRepo && previousRepo.toLowerCase() !== repo.toLowerCase();
}

export function canonicalRepositoryItem(item, repo, kind) {
  if (!item || !Number.isSafeInteger(item.number) || item.number < 1 || item.state !== "OPEN") return null;
  try {
    const url = new URL(item.url);
    const path = `/${repo}/${kind === "pulls" ? "pull" : "issues"}/${item.number}`;
    if (url.protocol !== "https:" || url.hostname !== "github.com" || url.port || url.username
        || url.password || url.search || url.hash || url.pathname !== path
        || (item.repo && item.repo.toLowerCase() !== repo.toLowerCase())) return null;
    return {...item, url:url.href};
  } catch { return null; }
}

export function repositoryPickerHtml({ id, kind = "issues" } = {}) {
  const text = labels(kind);
  return `<div data-repository-picker="${attr(id)}">
    <label class="field-label" for="${attr(id)}-search">Buscar por número o título</label>
    <div class="flex gap-2"><input id="${attr(id)}-search" class="input min-w-0" type="search" maxlength="120" autocomplete="off"><button type="button" class="btn btn-xs" data-picker-search>Buscar</button></div>
    <label class="field-label" for="${attr(id)}-results">${text.name} del repositorio</label>
    <select id="${attr(id)}-results" class="select w-full" data-picker-results aria-describedby="${attr(id)}-status" disabled><option value="">Cargando…</option></select>
    <div class="flex flex-wrap items-center gap-2"><button type="button" class="btn btn-xs" data-picker-previous disabled>Anterior</button><span class="field-hint" data-picker-page></span><button type="button" class="btn btn-xs" data-picker-next disabled>Siguiente</button><button type="button" class="btn btn-xs" data-picker-retry>Actualizar lista</button></div>
    <p id="${attr(id)}-status" class="field-hint" data-issue-picker-status role="status" aria-live="polite">Cargando ${text.name.toLowerCase()} del proyecto.</p>
  </div>`;
}

export function mountRepositoryPicker(root, { id, kind = "issues", projectSlug = "", onChange, onSelect } = {}) {
  const input = root?.querySelector(`#${id}`);
  const box = root?.querySelector(`[data-repository-picker="${id}"]`);
  if (!input || !box) return {setProject() {}, destroy() {}};
  const results = box.querySelector("[data-picker-results]"), search = box.querySelector(`#${id}-search`);
  const status = box.querySelector("[data-issue-picker-status]"), pageLabel = box.querySelector("[data-picker-page]");
  const previous = box.querySelector("[data-picker-previous]"), next = box.querySelector("[data-picker-next]");
  const retry = box.querySelector("[data-picker-retry]"), searchButton = box.querySelector("[data-picker-search]");
  const text = labels(kind), cache = new Map(), listeners = [];
  let project = projectSlug, query = "", page = 1, version = 0, disposed = false, suspended = false;
  let controller, entries = new Map(), selectedUrl = "", observer, repo = "", inheritedDraft = null, edits = 0;
  const current = generation => !disposed && !suspended && generation === version && root.isConnected !== false;
  const listen = (node, event, handler) => { node.addEventListener(event, handler); listeners.push([node,event,handler]); };
  const changed = source => onChange?.(input.value, {source});
  const clearResults = message => {
    entries.clear(); results.innerHTML = `<option value="">${escape(message)}</option>`;
    results.disabled = true; previous.disabled = true; next.disabled = true; pageLabel.textContent = "";
  };
  const load = async (force = false) => {
    if (disposed || suspended) return;
    const generation = ++version;
    controller?.abort(); controller = new AbortController();
    clearResults("Cargando…"); retry.disabled = true;
    if (!project) { status.textContent = "Elige un proyecto para consultar su repositorio."; retry.disabled = false; return; }
    status.textContent = `Cargando ${text.name.toLowerCase()} del proyecto.`;
    const key = JSON.stringify([project,kind,query,page]);
    try {
      const cached = cache.get(key);
      const data = !force && cached && Date.now() - cached.at < 30_000 ? cached.data
        : await apiRoot(`/projects/${encodeURIComponent(project)}/${kind}?${new URLSearchParams({page:String(page),q:query})}`,
          {signal:controller.signal,cache:"no-store"});
      if (!current(generation)) return;
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(data.repo || "") || !Array.isArray(data[kind])) {
        throw new Error("Respuesta del repositorio no verificable");
      }
      const items = data[kind].map(item => canonicalRepositoryItem(item,data.repo,kind));
      if (items.some(item => !item)) throw new Error("La lista contiene una referencia incompatible con el repositorio");
      if (inheritedDraft && inheritedDraft.edits === edits && input.value === inheritedDraft.value
          && incompatibleDraft(input.value,inheritedDraft.repo,data.repo,kind)) {
        input.value = ""; changed("project");
      }
      inheritedDraft = null; repo = data.repo;
      cache.set(key,{data,at:Date.now()});
      if (cache.size > 20) cache.delete(cache.keys().next().value);
      entries = new Map(items.map(item => [item.url,item]));
      results.innerHTML = `<option value="">${text.select}</option>` + items.map(item =>
        `<option value="${attr(item.url)}">${escape(`#${item.number} — ${item.title || "Sin título"}${item.draft ? " · Draft" : ""}`)}</option>`).join("");
      results.disabled = items.length === 0;
      results.value = entries.has(input.value) ? input.value : "";
      previous.disabled = page <= 1; next.disabled = data.pagination?.has_next !== true;
      pageLabel.textContent = `Página ${page}`;
      status.textContent = items.length ? `${items.length} ${text.name.toLowerCase()} en ${data.repo}.`
        : `No hay ${text.name.toLowerCase()}${query ? " con esta búsqueda" : ""} en ${data.repo}.`;
      status.textContent += ` Puedes ${text.manual}.`;
      if (data.pagination?.limited) status.textContent += " GitHub limita los resultados; acota la búsqueda o usa la entrada manual.";
    } catch (error) {
      if (!current(generation) || controller.signal.aborted) return;
      clearResults("Lista no disponible");
      if ([401,403,409].includes(error.status)) cache.clear();
      let code;
      try { code = JSON.parse(error.body || "{}").code; } catch { /* Error de transporte sin JSON. */ }
      if (code === "project_changed" && selectedUrl && input.value === selectedUrl) {
        input.value = selectedUrl = ""; changed("project");
      }
      const reason = error.status === 429 ? "GitHub alcanzó su límite de consultas"
        : code === "project_changed" ? "El repositorio del proyecto cambió; actualiza la lista"
        : error.status === 409 ? "Conecta o reconecta tu cuenta GitHub"
        : [403,404].includes(error.status) ? "No se pudo acceder al repositorio con tu cuenta"
        : "No se pudo cargar la lista";
      status.textContent = `${reason}: ${error.message}. Puedes reintentar o ${text.manual}; el dato escrito se conserva.`;
    } finally { if (current(generation)) retry.disabled = false; }
  };
  const applySearch = () => { query = search.value.trim(); page = 1; load(); };
  listen(searchButton,"click",applySearch);
  listen(search,"keydown",event => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); applySearch(); } });
  listen(previous,"click",() => { if (page > 1) { --page; load(); } });
  listen(next,"click",() => { if (!next.disabled) { ++page; load(); } });
  listen(retry,"click",() => { cache.clear(); load(true); });
  listen(input,"input",() => { ++edits; inheritedDraft = null; selectedUrl = ""; results.value = entries.has(input.value) ? input.value : ""; changed("manual"); });
  listen(results,"change",() => {
    if (suspended || disposed) return;
    const item = entries.get(results.value);
    if (!item) return;
    ++edits; inheritedDraft = null; input.value = selectedUrl = item.url; changed("selection"); onSelect?.(item);
  });
  const destroy = () => {
    if (disposed) return;
    disposed = true; ++version; controller?.abort(); cache.clear(); observer?.disconnect();
    listeners.forEach(([node,event,handler]) => node.removeEventListener(event,handler));
  };
  const modal = root.closest?.(".modal");
  if (modal && globalThis.MutationObserver) {
    observer = new MutationObserver(() => { if (!root.isConnected || !modal.classList.contains("open")) destroy(); });
    observer.observe(modal,{attributes:true,attributeFilter:["class"]});
    if (root.parentElement) observer.observe(root.parentElement,{childList:true});
  }
  load();
  return {
    setProject(value) {
      if (value === project) return;
      inheritedDraft = {value:input.value,edits,repo:repo || inheritedDraft?.repo || ""};
      project = value; query = ""; page = 1; search.value = ""; cache.clear();
      repo = "";
      if (selectedUrl && input.value === selectedUrl) { input.value = ""; changed("project"); }
      selectedUrl = ""; load(true);
    },
    setBusy(value) {
      if (disposed || suspended === value) return;
      suspended = value;
      if (value) { ++version; controller?.abort(); }
      else load(true);
    },
    destroy,
  };
}
