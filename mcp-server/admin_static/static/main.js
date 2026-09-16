// 4bis.relay admin UI — entry point (ES module).
// Orquesta: navegación por tabs (con hash routing), wiring de modales,
// boot y polling de health. La lógica de cada tab vive en tab-*.js.

import { $, $$, DEBUG, _dbg } from "./api.js";
import { wireModals, wireSidePanel } from "./ui.js";
import { wirePanelResize } from "./panel-resize.js";
import { refreshStatus } from "./tab-status.js";
import { initRunning, loadRunning } from "./tab-running.js";
// Iter 9.8: tab-consults desapareció. Las notas/bitácora viven en el
// tab Chats con filtro target=notes + un mini-editor inline. El
// endpoint legacy /admin/api/consults queda como alias por compat
// con clientes viejos (Discord bot). Ver docs/CHANGELOG.md iter 9.8.
import { initChats, loadChats } from "./tab-chats.js";
import { initVoice, loadVoice } from "./tab-voice.js";
import { initProjects, loadProjects } from "./tab-projects.js";
import { initOrphans, loadOrphans, loadIgnored } from "./tab-orphans.js";
import { initIndex, loadIndexPanel, initFromConfig } from "./tab-index.js";
import { initCommands, loadCommands } from "./tab-commands.js";
import { initMcp, loadMcp, loadDbConns } from "./tab-mcp.js";
import { initModels, loadModels } from "./tab-models.js";
import { initConfig, loadConfig, loadExpertTimeout, loadToolTimeout, loadUsers, loadTemplates } from "./tab-config.js";
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
  config: () => { loadConfig(); loadExpertTimeout(); loadToolTimeout(); loadUsers(); loadTemplates(); },
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

// Cada init se wirea aislado. Antes eran llamadas sueltas al top-level
// del módulo: un `$("#id-que-no-existe").onclick = ...` en CUALQUIERA
// tiraba TypeError y abortaba el resto del archivo, dejando muertos los
// botones de todos los tabs de abajo — sin error visible en la UI, solo
// clicks que no hacen nada. Pasó de verdad: un `#chat-panel-steer` que
// quedó en el JS antes que en el HTML se llevó puestos MCPs, Config,
// Skills, Logs y Métricas. El try/catch degrada a "ese tab pierde sus
// handlers" en vez de "medio admin no responde".
for (const [name, fn] of [
  ["running", initRunning],
  ["chats", initChats], ["voice", initVoice], ["projects", initProjects],
  ["orphans", initOrphans], ["index", initIndex], ["commands", initCommands],
  ["mcp", initMcp], ["models", initModels],
  ["config", initConfig], ["fromConfig", initFromConfig],
  ["nightRuns", initNightRuns], ["zombies", initZombies],
  ["skills", initSkills], ["nightBanner", initNightQuestionsBanner],
  ["report", initReport], ["logs", initLogs], ["diagrams", initDiagrams],
  ["metrics", initMetrics], ["gestion", initGestion], ["crm", initCrm],
]) {
  try {
    fn();
  } catch (e) {
    console.error(`[admin-ui] init "${name}" falló; ese tab queda sin `
      + `handlers (el resto sigue):`, e);
  }
}

initWorkspace({ moduleLoaders: TAB_LOADERS, restoreChatObject });
refreshStatus();
// Los módulos visibles se refrescan aunque otro tenga el foco. Minimizar
// o cerrar una herramienta pausa sus pollers sin cancelar trabajos reales.
initPollers(visibleWorkspaceModules);
registerPoller(refreshStatus, 15000);

// Buscador global (Ctrl+K) y watcher del topbar (indicadores running/tokens/
// chats + navegación por click). Ambos son inits GLOBALES de boot (no tabs),
// y ambos perdieron su llamada en el refactor de Sprint 1 —el import quedó
// pero el `initX()` se borró—, así que Ctrl+K era letra muerta y el topbar
// quedaba clavado en "…". Van en el boot top-level: el DOM ya está listo
// (showTab/initPollers de arriba tocan elementos).
initSearch();
initWatcher();
