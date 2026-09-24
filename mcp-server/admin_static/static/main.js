// 4bis.relay admin UI — entry point (ES module).
// Orquesta: navegación por tabs (con hash routing), wiring de modales,
// boot y polling de health. La lógica de cada tab vive en tab-*.js.

import { $, $$, DEBUG, _dbg, api } from "./api.js";
import { wireModals, wireSidePanel } from "./ui.js";
import { wirePanelResize } from "./panel-resize.js";
import { refreshStatus } from "./tab-status.js";
import { initRunning, loadRunning } from "./tab-running.js";
// Iter 9.8: tab-consults desapareció. Las notas/bitácora viven en el
// tab Chats con filtro target=notes + un mini-editor inline. El
// endpoint legacy /admin/api/consults queda como alias por compat
// con clientes viejos (Discord bot). Ver docs/CHANGELOG.md iter 9.8.
import { initChats, loadChats, selectConversation } from "./tab-chats.js";
import { bootEmbeddedChat, getEmbeddedConversation } from "./chat-window.js";
import { initVoice, loadVoice } from "./tab-voice.js";
import { initProjects, loadProjects } from "./tab-projects.js";
import { initOrphans, loadOrphans, loadIgnored } from "./tab-orphans.js";
import { initIndex, loadIndexPanel, initFromConfig } from "./tab-index.js";
import { initCommands, loadCommands } from "./tab-commands.js";
import { initMcp, loadMcp, loadDbConns } from "./tab-mcp.js";
import { initModels, loadModels } from "./tab-models.js";
import { initConfig, loadConfig, loadExpertTimeout, loadToolTimeout, loadTemplates } from "./tab-config.js";
import { initTeam, loadTeam } from "./tab-team.js";
import { initAccount, loadAccount } from "./tab-account.js";
import { initNightRuns, loadNightRuns } from "./tab-night-runs.js";
import { initZombies, loadZombies } from "./tab-zombies.js";
import { initCrm, loadCrm } from "./tab-crm.js";
import { initSkills, loadSkillsTab } from "./tab-skills.js";
import { initNightQuestionsBanner } from "./tab-night-questions-banner.js";
// UI 2026-07-20: informe de tokens, logs en vivo y watcher global
// (topbar + toasts de ciclo de vida de runs).
import { initReport, loadReport } from "./tab-report.js";
import { initLogs, loadLogs } from "./tab-logs.js";
import { initDiagrams, loadDiagrams } from "./tab-diagrams.js";
import { initMetrics, loadMetrics } from "./tab-metrics.js";
import { initGestion, loadGestion } from "./tab-gestion.js";
import { initPollers, registerPoller } from "./pollers.js";
import { initWatcher } from "./watcher.js";
// UI 2026-07-20: buscador global en topbar (Ctrl+K).
import { initSearch } from "./search.js";
import { initWorkspace, visibleWorkspaceModules } from './workspace.js';
import { restoreChatObject } from './chat-objects.js';

_dbg("admin.js (módulos) cargado, debug:",
     DEBUG ? "?debug=1" : "agrega ?debug=1 a la URL");
if (DEBUG) {
  console.log("[admin-ui] timestamp:", new Date().toISOString());
  console.log("[admin-ui] user-agent:", navigator.userAgent.slice(0, 80));
}

// ------- tabs (hash routing: #/running sobrevive al F5) -------

const TAB_LOADERS = {
  status: refreshStatus,
  running: loadRunning,
  chat: loadChats,
  voice: loadVoice,
  projects: loadProjects,
  orphans: () => { loadOrphans(); loadIgnored(); },
  index: loadIndexPanel,
  commands: loadCommands,
  mcp: () => { loadMcp(); loadDbConns(); },
  models: loadModels,
  config: () => { loadConfig(); loadExpertTimeout(); loadToolTimeout(); loadTemplates(); },
  team: loadTeam,
  account: loadAccount,
  "night-runs": loadNightRuns,
  zombies: loadZombies,
  skills: loadSkillsTab,
  report: loadReport,
  logs: loadLogs,
  diagrams: loadDiagrams,
  metrics: loadMetrics,
  gestion: loadGestion,
  crm: loadCrm,
};

// Boot de componentes; el workspace conserva sus nodos al moverlos.
wireModals();
wireSidePanel();
wirePanelResize();

const embeddedConversation = getEmbeddedConversation();
const actor = await api("me").catch((error) => {
  _dbg("No se pudo cargar api/me", error.message);
  return null;
});
const allowedTabs = actor?.role === "owner" && actor.allowed_tabs === null ? null
  : new Set(Array.isArray(actor?.allowed_tabs) ? actor.allowed_tabs : []);
if (embeddedConversation && (allowedTabs === null || allowedTabs.has("chat"))) {
  bootEmbeddedChat({ initChats, selectConversation, initPollers }).catch(error => {
    console.error("No se pudo abrir la conversación", error);
    const message = document.createElement('p'); message.className = 'chat-task-error';
    message.textContent = `No se pudo abrir el chat: ${error.message}. `;
    const retry = document.createElement('button'); retry.className = 'btn btn-xs'; retry.textContent = 'Reintentar';
    retry.onclick = () => location.reload(); message.append(retry);
    document.getElementById('main').prepend(message);
  });
} else if (embeddedConversation) {
  $("#main").textContent = "Tu cuenta no tiene permiso para abrir conversaciones del Relay.";
} else {

// La identidad y su lista de tabs vienen del servidor. La navegación se
// construye después, de modo que ni la URL ni el layout guardado abran tabs
// que este rol no tiene permitidos.
const canObserveGlobally = allowedTabs === null || allowedTabs.has("status");
if (!canObserveGlobally) {
  $("#topbar").hidden = true;
  $("#search-trigger").hidden = true;
}

// Cada init se wirea aislado. Antes eran llamadas sueltas al top-level
// del módulo: un `$("#id-que-no-existe").onclick = ...` en CUALQUIERA
// tiraba TypeError y abortaba el resto del archivo, dejando muertos los
// botones de todos los tabs de abajo — sin error visible en la UI, solo
// clicks que no hacen nada. Pasó de verdad: un `#chat-panel-steer` que
// quedó en el JS antes que en el HTML se llevó puestos MCPs, Config,
// Skills, Logs y Métricas. El try/catch degrada a "ese tab pierde sus
// handlers" en vez de "medio admin no responde".
for (const [tab, name, fn] of [
  ["running", "running", initRunning],
  ["chat", "chats", initChats], ["voice", "voice", initVoice],
  ["projects", "projects", initProjects], ["orphans", "orphans", initOrphans],
  ["index", "index", initIndex], ["commands", "commands", initCommands],
  ["mcp", "mcp", initMcp], ["models", "models", initModels],
  ["team", "team", () => initTeam(actor)],
  ["account", "account", () => initAccount(actor)],
  ["config", "config", initConfig], ["config", "fromConfig", initFromConfig],
  ["night-runs", "nightRuns", initNightRuns], ["zombies", "zombies", initZombies],
  ["skills", "skills", initSkills], ["night-runs", "nightBanner", initNightQuestionsBanner],
  ["report", "report", initReport], ["logs", "logs", initLogs],
  ["diagrams", "diagrams", initDiagrams], ["metrics", "metrics", initMetrics],
  ["gestion", "gestion", initGestion], ["crm", "crm", () => initCrm(actor)],
]) {
  if (allowedTabs !== null && !allowedTabs.has(tab)) continue;
  try {
    fn();
  } catch (e) {
    console.error(`[admin-ui] init "${name}" falló; ese tab queda sin `
      + `handlers (el resto sigue):`, e);
  }
}

initWorkspace({ moduleLoaders: TAB_LOADERS, restoreChatObject, allowedTabs,
  accessMessage: actor ? "Tu cuenta todavía no tiene herramientas asignadas. Pide al administrador que revise Equipo." : "No se pudo verificar tu acceso. Recarga la página o contacta al administrador." });
if (canObserveGlobally) refreshStatus();
// Los módulos visibles se refrescan aunque otro tenga el foco. Minimizar
// o cerrar una herramienta pausa sus pollers sin cancelar trabajos reales.
initPollers(visibleWorkspaceModules);
if (canObserveGlobally) registerPoller(refreshStatus, 15000);

// Buscador global (Ctrl+K) y watcher del topbar (indicadores running/tokens/
// chats + navegación por click). Ambos son inits GLOBALES de boot (no tabs),
// y ambos perdieron su llamada en el refactor de Sprint 1 —el import quedó
// pero el `initX()` se borró—, así que Ctrl+K era letra muerta y el topbar
// quedaba clavado en "…". Van en el boot top-level: el DOM ya está listo
// (showTab/initPollers de arriba tocan elementos).
if (canObserveGlobally) {
  initSearch();
  initWatcher();
}
}
