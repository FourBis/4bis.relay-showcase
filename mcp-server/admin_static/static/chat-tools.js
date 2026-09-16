// Herramientas del chat: tarjeta de pregunta + paleta de comandos.
//
// Dos cosas que el chat no tenía y que llegan juntas porque comparten la
// misma idea: que el humano pueda RESPONDERLE al experto sin tener que
// escribir un párrafo.
//
//   1. `renderQuestionCard` — cuando el experto llama `ask_human`, la
//      pregunta deja de ser texto suelto dentro de la respuesta y pasa a
//      ser una tarjeta con botones. Un click contesta y el hilo sigue.
//
//   2. `chatCommands` — un registro de comandos `/algo` que corren del
//      lado de la UI. Es una LISTA, no un switch: agregar uno es
//      empujar un objeto acá y aparece solo en la paleta, en la ayuda y
//      en el autocompletado. Esa es la parte "enchufable": nadie tiene
//      que tocar el composer para sumar una herramienta.
//
// Módulo aparte de tab-chats.js a propósito: ese archivo ya tiene 1800
// líneas y hace de todo. Acá entra lo nuevo, con sus estilos inline —
// admin.css es un bundle de Tailwind que se regenera con build-css.ps1
// (PowerShell + descarga del CLI), y este módulo no justifica ese paso.

import { $, api, apiRoot, escape } from "./api.js";
import { toast } from "./ui.js";

// =====================================================================
// 1. TARJETA DE PREGUNTA
// =====================================================================

// Un solo lugar donde vive el color, para que la tarjeta se lea igual en
// las dos variantes sin repetir hexadecimales por todo el archivo.
const TONO = {
  install: { borde: "#f59e0b", fondo: "#f59e0b14", icono: "📦",
             etiqueta: "instalación" },
  choice:  { borde: "#38bdf8", fondo: "#38bdf814", icono: "❓",
             etiqueta: "decisión" },
  text:    { borde: "#38bdf8", fondo: "#38bdf814", icono: "❓",
             etiqueta: "pregunta" },
};

/**
 * Pinta la pregunta abierta del experto como tarjeta accionable.
 *
 * `onAnswered(resumePrompt)` se llama con el texto que retoma el hilo —
 * el caller decide si lo manda solo o lo deja escrito en el composer.
 * La tarjeta se saca del DOM apenas se responde: una pregunta ya
 * contestada que sigue con botones invita a contestarla dos veces.
 */
export function renderQuestionCard(box, pregunta, onAnswered) {
  if (!box || !pregunta) return null;
  const q = pregunta.question || {};
  const tono = TONO[pregunta.kind] || TONO.choice;
  const opciones = q.options || [];

  const card = document.createElement("div");
  card.className = "chat-question-card";
  card.dataset.qid = pregunta.id;
  card.style.cssText = `
    border:1px solid ${tono.borde}; border-left-width:3px;
    background:${tono.fondo}; border-radius:8px;
    padding:14px 16px; margin:14px 0; display:flex;
    flex-direction:column; gap:10px;`;

  const detalle = (q.detail || "").trim();
  card.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span style="font-size:15px">${tono.icono}</span>
      <span style="font-size:10px;text-transform:uppercase;letter-spacing:.09em;
                   color:${tono.borde};font-weight:600">${tono.etiqueta}</span>
      <span style="margin-left:auto;font-size:10px;font-family:ui-monospace,monospace;
                   opacity:.5">${escape(pregunta.id)}</span>
    </div>
    <div style="font-weight:600;line-height:1.45">${escape(q.title || "El experto necesita una decisión")}</div>
    ${detalle ? `<div style="font-size:13px;opacity:.85;white-space:pre-wrap;
                             line-height:1.5">${escape(detalle)}</div>` : ""}
    <div class="q-actions" style="display:flex;gap:8px;flex-wrap:wrap"></div>
    <div class="q-free" style="display:flex;gap:8px">
      <input type="text" class="q-text" placeholder="${
        opciones.length ? "…o contesta con tus palabras" : "Tu respuesta"}"
        style="flex:1;min-width:0;padding:7px 10px;border-radius:6px;
               border:1px solid #ffffff26;background:#00000026;color:inherit;
               font-size:13px">
      <button type="button" class="q-send" style="padding:7px 14px;border-radius:6px;
        border:1px solid ${tono.borde};background:${tono.borde};color:#0b1220;
        font-weight:600;font-size:13px;cursor:pointer">Responder</button>
    </div>
    <div class="q-status" style="font-size:12px;opacity:.7" hidden></div>`;

  const acciones = card.querySelector(".q-actions");
  for (const opt of opciones) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = opt.label || opt.key;
    b.style.cssText = `padding:7px 13px;border-radius:6px;cursor:pointer;
      border:1px solid ${tono.borde}66; background:transparent; color:inherit;
      font-size:13px; font-weight:500;`;
    b.addEventListener("mouseenter", () => { b.style.background = tono.fondo; });
    b.addEventListener("mouseleave", () => { b.style.background = "transparent"; });
    b.addEventListener("click", () => responder(card, pregunta.id,
                                               { choice: opt.key }, onAnswered));
    acciones.appendChild(b);
  }
  if (!opciones.length) acciones.remove();

  const input = card.querySelector(".q-text");
  const enviar = () => {
    const texto = input.value.trim();
    if (!texto) { input.focus(); return; }
    responder(card, pregunta.id, { text: texto }, onAnswered);
  };
  card.querySelector(".q-send").addEventListener("click", enviar);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); enviar(); }
  });

  box.appendChild(card);
  // Que se vea sin scrollear: si el humano no la ve, no la contesta.
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return card;
}

async function responder(card, qId, body, onAnswered) {
  const estado = card.querySelector(".q-status");
  card.querySelectorAll("button, input").forEach((el) => { el.disabled = true; });
  estado.hidden = false;
  estado.textContent = "Enviando…";
  try {
    const r = await apiRoot(`/questions/${encodeURIComponent(qId)}/answer`, {
      method: "POST", body: JSON.stringify(body),
    });
    if (r && r.error) {
      // 409: la pregunta ya no está abierta. Dos motivos: alguien más
      // contestó (Discord, otra pestaña), o el experto preguntó otra cosa
      // después y esta quedó vieja. En el segundo caso el mensaje dice
      // "mirá la última", así que se la mostramos en vez de dejarlo
      // buscándola: `_recargar` la repinta desde la API.
      estado.textContent = r.error;
      setTimeout(() => {
        const box = card.parentElement;
        card.remove();
        if (r.status === "superseded" && box && _recargar) _recargar(box);
      }, 2500);
      return;
    }
    card.remove();
    if (typeof onAnswered === "function") onAnswered(r.resume_prompt || "");
  } catch (e) {
    estado.textContent = "No se pudo enviar: " + e.message;
    card.querySelectorAll("button, input").forEach((el) => { el.disabled = false; });
  }
}

// Cómo repintar la decisión vigente después de un 409 por `superseded`.
// Se guarda en el módulo y no se pasa por parámetro para no cambiar la
// firma pública de `renderQuestionCard`, que los tests y el E2E usan
// directo con una pregunta armada a mano.
let _recargar = null;

/** Trae las preguntas abiertas del hilo y pinta la primera. */
export async function refreshQuestions(box, convId, onAnswered) {
  if (!box || !convId) return 0;
  _recargar = (b) => refreshQuestions(b, convId, onAnswered);
  box.querySelectorAll(".chat-question-card").forEach((c) => c.remove());
  let abiertas = [];
  try {
    const r = await apiRoot(
      `/questions?conversation=${encodeURIComponent(convId)}&only_open=1`);
    abiertas = (r && r.questions) || [];
  } catch (_) {
    return 0;   // sin tarjeta, la pregunta igual está en el texto
  }
  // Una a la vez: el experto pregunta una cosa por turno, y una pila de
  // tarjetas sería ruido. La más nueva primero (la API ya ordena DESC).
  if (abiertas.length) renderQuestionCard(box, abiertas[0], onAnswered);
  return abiertas.length;
}

// =====================================================================
// 2. PALETA DE COMANDOS
// =====================================================================
//
// Cada comando es `{name, args?, desc, run(ctx, args)}`. `ctx` trae lo
// que la UI del chat sabe del hilo activo y las acciones que ya existen,
// así que un comando nuevo casi siempre es una línea.
//
// Contrato de `run`: devolver `false` cancela el envío del mensaje
// (el comando ya hizo lo suyo). Devolver un string lo REEMPLAZA — así un
// comando puede expandirse a un prompt.

export const chatCommands = [
  {
    name: "/abrir",
    args: "<herramienta>",
    desc: "Abre una herramienta junto al chat, sin enviar un mensaje",
    run: (_ctx, args) => {
      import('./workspace.js').then(({ openWorkspaceModule, openWorkspaceLauncher }) => {
        const query = (args || '').trim().toLocaleLowerCase();
        const button = [...document.querySelectorAll('#workspace-modules .tab')].find(b =>
          b.dataset.tab === query || b.querySelector('.workspace-module-label')?.firstChild?.textContent.trim().toLocaleLowerCase() === query);
        if (button) openWorkspaceModule(button.dataset.tab);
        else openWorkspaceLauncher(query);
      }).catch(e => toast(`No se pudo abrir la herramienta: ${e.message}`, 'err'));
      return false;
    },
  },
  {
    name: "/compactar",
    desc: "Destila el hilo a un resumen y libera contexto",
    run: (ctx) => { ctx.compactar(); return false; },
  },
  {
    name: "/cerrar",
    desc: "Cierra la conversación (compacta y libera la rama)",
    run: (ctx) => { ctx.cerrar(); return false; },
  },
  {
    name: "/parar",
    desc: "Corta el run en curso sin perder lo avanzado",
    run: (ctx) => { ctx.parar(); return false; },
  },
  {
    name: "/diff",
    desc: "Muestra el diff de la rama de este hilo",
    run: (ctx) => { ctx.verDiff(); return false; },
  },
  {
    name: "/continuar",
    desc: "Retoma donde quedó el turno anterior",
    run: () => "continuá",
  },
  {
    name: "/noche",
    args: "<directiva> | @plantilla",
    desc: "Arranca el modo nocturno en el proyecto de este hilo",
    run: (ctx, args) => { arrancarNoche(ctx, args); return false; },
  },
  {
    name: "/plantillas",
    desc: "Lista las directivas guardadas para el modo nocturno",
    run: (ctx) => { listarPlantillas(ctx); return false; },
  },
  {
    name: "/noche-parar",
    args: "<run_id>",
    desc: "Corta el modo nocturno en curso",
    run: (ctx, args) => { pararNoche(ctx, args); return false; },
  },
  {
    name: "/resumen",
    desc: "Pide un resumen de lo hecho hasta acá",
    run: () => "Resumí en cinco líneas qué hiciste en este hilo, qué quedó "
               + "pendiente y qué archivos tocaste.",
  },
  {
    name: "/tests",
    desc: "Corre la suite del proyecto y reporta",
    run: () => "Corré la suite de tests del proyecto y decime el resultado. "
               + "Si algo falla, pegá las líneas del error.",
  },
  {
    name: "/revisar",
    desc: "Revisión del diff sin commitear",
    run: () => "Revisá el diff actual buscando bugs de correctitud y cosas "
               + "que se puedan simplificar. No commitees nada.",
  },
  {
    name: "/explicar",
    args: "<archivo o función>",
    desc: "Explica una parte del repo",
    run: (_ctx, args) => args
      ? `Explicame ${args}: qué hace, quién lo llama y por qué existe.`
      : "Explicame la arquitectura de este repo en diez líneas.",
  },
  {
    name: "/bases",
    desc: "Qué bases SQL puede consultar el experto",
    run: () => "Listá las conexiones SQL disponibles con `db_connections` y "
               + "decime qué hay en cada una. No consultes nada todavía.",
  },
  {
    name: "/sql",
    args: "<pregunta o consulta>",
    desc: "Consulta una base registrada y muestra las filas",
    run: (_ctx, args) => args
      ? `Con \`db_query\` resolvé esto contra la base que corresponda: ${args}. `
        + "Si no estás seguro de cuál es, mirá primero `db_connections` y "
        + "preguntame antes de consultar."
      : "Mostrame con `db_connections` qué bases hay y para qué sirve cada una.",
  },
  {
    name: "/ayuda",
    desc: "Lista estos comandos",
    run: (ctx) => {
      ctx.mostrarAyuda();
      return false;
    },
  },
];

/**
 * ¿El texto es un comando de la paleta? Devuelve `{cmd, args}` o null.
 *
 * Los `!comando` del relay (que resuelve el backend) NO pasan por acá:
 * son otra cosa y siguen yendo al servidor como siempre.
 */
export function matchCommand(texto) {
  const t = (texto || "").trim();
  if (!t.startsWith("/")) return null;
  // El nombre es el primer token; el resto va TAL CUAL. Antes esto era
  // `split(/\s+/)` + `join(" ")`, que aplastaba los saltos de línea — y
  // hay comandos donde el salto es semántico. El que lo destapó: la
  // directiva de `/noche`. El planificador del modo nocturno detecta los
  // puntos enumerados SOLO a principio de línea (`_POINT_RE` en
  // night.py exige principio de línea), así que una directiva
  // aplastada perdía todos los puntos menos el primero
  // — en silencio, y encima
  // rompiendo el chequeo de cobertura que existe para avisar justo eso.
  const m = /^(\S+)([\s\S]*)$/.exec(t);
  const head = m[1];
  const cmd = chatCommands.find(
    (c) => c.name.toLowerCase() === head.toLowerCase());
  return cmd ? { cmd, args: (m[2] || "").trim() } : null;
}

export function helpText() {
  const ancho = Math.max(...chatCommands.map((c) => c.name.length)) + 2;
  const filas = chatCommands.map((c) => {
    const uso = c.args ? `${c.name} ${c.args}` : c.name;
    return `${uso.padEnd(ancho + (c.args ? c.args.length + 1 : 0))}  ${c.desc}`;
  });
  return "**Comandos del chat**\n\n```\n" + filas.join("\n") + "\n```\n"
    + "_Los `!comando` del relay (`!proyectos`, `!memoria`…) siguen "
    + "funcionando igual._";
}

// ---- autocompletado ----
//
// Aparece al tipear `/` al principio del composer. Es el mismo gesto que
// en Claude Code o en Discord: escribes, filtra, Enter elige.

let menuEl = null;
let menuIdx = 0;
let menuItems = [];

export function wireCommandPalette(input, onPick) {
  if (!input) return;
  input.addEventListener("input", () => {
    const t = input.value;
    if (!t.startsWith("/") || t.includes("\n")) return cerrarMenu();
    const q = t.slice(1).split(/\s+/)[0].toLowerCase();
    menuItems = chatCommands.filter(
      (c) => c.name.slice(1).toLowerCase().startsWith(q));
    menuItems.length ? abrirMenu(input, onPick) : cerrarMenu();
  });
  input.addEventListener("keydown", (e) => {
    if (!menuEl) return;
    if (e.key === "Escape") { e.preventDefault(); return cerrarMenu(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      menuIdx = (menuIdx + (e.key === "ArrowDown" ? 1 : -1) + menuItems.length)
        % menuItems.length;
      pintarMenu(onPick);
      return;
    }
    // Enter y Tab eligen. Ctrl+Enter no: ese es "mandar", y si el humano
    // ya escribió el comando entero no queremos secuestrarle el envío.
    if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey) || e.key === "Tab") {
      e.preventDefault();
      elegir(menuItems[menuIdx], input, onPick);
    }
  });
  input.addEventListener("blur", () => setTimeout(cerrarMenu, 150));
}

function abrirMenu(input, onPick) {
  if (!menuEl) {
    menuEl = document.createElement("div");
    menuEl.className = "chat-cmd-menu";
    menuEl.style.cssText = `position:absolute; bottom:100%; left:0; right:0;
      margin-bottom:6px; background:#0f172a; border:1px solid #ffffff1f;
      border-radius:8px; box-shadow:0 8px 24px #0008; overflow:hidden;
      z-index:40; max-height:260px; overflow-y:auto;`;
    const cont = input.closest(".chat-composer") || input.parentElement;
    if (cont && getComputedStyle(cont).position === "static") {
      cont.style.position = "relative";
    }
    (cont || document.body).appendChild(menuEl);
    menuIdx = 0;
  }
  pintarMenu(onPick);
}

function pintarMenu(onPick) {
  if (!menuEl) return;
  if (menuIdx >= menuItems.length) menuIdx = 0;
  menuEl.innerHTML = "";
  menuItems.forEach((c, i) => {
    const fila = document.createElement("div");
    fila.style.cssText = `padding:7px 12px; cursor:pointer; display:flex;
      gap:12px; align-items:baseline; font-size:13px;
      background:${i === menuIdx ? "#ffffff14" : "transparent"};`;
    fila.innerHTML =
      `<span style="font-family:ui-monospace,monospace;color:#7dd3fc">${
        escape(c.name)}${c.args ? " " + escape(c.args) : ""}</span>
       <span style="opacity:.7;margin-left:auto;text-align:right">${
        escape(c.desc)}</span>`;
    fila.addEventListener("mouseenter", () => { menuIdx = i; pintarMenu(onPick); });
    // mousedown y no click: el blur del input llega antes que el click.
    fila.addEventListener("mousedown", (e) => {
      e.preventDefault();
      elegir(c, document.getElementById("chat-panel-input"), onPick);
    });
    menuEl.appendChild(fila);
  });
}

function elegir(cmd, input, onPick) {
  if (!cmd || !input) return cerrarMenu();
  const resto = input.value.trim().split(/\s+/).slice(1).join(" ");
  input.value = cmd.args && !resto ? `${cmd.name} ` : `${cmd.name}${resto ? " " + resto : ""}`;
  cerrarMenu();
  input.focus();
  // Un comando sin argumentos se ejecuta de una: pedirle al humano que
  // apriete Enter dos veces para lo mismo es fricción sin motivo.
  if (!cmd.args && typeof onPick === "function") onPick();
}

function cerrarMenu() {
  if (menuEl) { menuEl.remove(); menuEl = null; }
  menuItems = [];
  menuIdx = 0;
}

export function paletteOpen() {
  return !!menuEl;
}

// =====================================================================
// 3. Extras chicos que se notan
// =====================================================================

/** Botón "copiar" en cada bloque de código de una burbuja. */
export function addCopyButtons(scope) {
  const bloques = (scope || document).querySelectorAll("pre:not([data-copy])");
  for (const pre of bloques) {
    pre.dataset.copy = "1";
    if (getComputedStyle(pre).position === "static") pre.style.position = "relative";
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "copiar";
    b.style.cssText = `position:absolute; top:6px; right:6px; font-size:10px;
      padding:3px 8px; border-radius:5px; border:1px solid #ffffff26;
      background:#0f172acc; color:inherit; cursor:pointer; opacity:0;
      transition:opacity .12s;`;
    pre.addEventListener("mouseenter", () => { b.style.opacity = "1"; });
    pre.addEventListener("mouseleave", () => { b.style.opacity = "0"; });
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText.replace(/\ncopiar$/, ""));
        b.textContent = "✓ copiado";
        setTimeout(() => { b.textContent = "copiar"; }, 1500);
      } catch (_) { toast("El navegador no dejó copiar", "err"); }
    });
    pre.appendChild(b);
  }
}

// ---------------------------------------------------------------------
// Modo nocturno desde el chat
//
// Por qué acá y no un endpoint nuevo: `/night-mode/start` ya existe y ya
// valida todo (proyecto habilitado, un solo run activo, deadline, cierre
// de filas colgadas). Esto es la boca del chat sobre eso, no una segunda
// implementación.
//
// El dispatcher de tab-chats.js hace `hit.cmd.run(...)` SIN await: si
// `run` devolviera una promesa, la rama `out === false` no matchea y el
// composer le manda "[object Promise]" al experto. Por eso el comando
// dispara el trabajo y devuelve `false` en el acto; la respuesta entra
// al hilo con `ctx.responder` cuando llega.
// ---------------------------------------------------------------------

async function arrancarNoche(ctx, directiva) {
  if (!ctx.project) {
    ctx.toast("Este hilo no tiene proyecto asociado", "err");
    return;
  }
  const d = (directiva || "").trim();
  // `@nombre` (UNA sola palabra) = plantilla guardada; el backend la
  // resuelve —primero la del proyecto, si no la global—. Se exige que sea
  // la palabra entera a propósito: una directiva que empieza con @ pero
  // sigue con más texto es una directiva, no el nombre de una plantilla.
  const tpl = /^@([\w.-]+)$/.exec(d);
  if (tpl) { arrancarNocheCon(ctx, { template: tpl[1] }, "@" + tpl[1]); return; }
  if (!d) {
    ctx.responder("/noche", [
      "Necesito una directiva: `/noche <qué querés que haga esta noche>`.",
      "",
      "Numerá los puntos (P1., P2., ...): el planificador los extrae y "
      + "después chequea que cada uno haya generado una tarea.",
    ].join("\n"));
    return;
  }
  await arrancarNocheCon(ctx, { directive: d }, "");
}

async function arrancarNocheCon(ctx, extra, etiqueta) {
  try {
    // 60s: la Fase 1 no corre acá (el server contesta 202 y planifica en
    // background), pero el arranque toca git y la DB.
    const r = await apiRoot("/night-mode/start",
      { method: "POST", body: { project: ctx.project, ...extra } },
      60_000);
    ctx.responder("/noche", [
      "🌙 Modo nocturno arrancado en **" + escape(ctx.project) + "**"
      + (etiqueta ? " con la plantilla `" + escape(etiqueta) + "`" : "") + ".",
      "",
      "- run: `" + escape(r.run_id) + "`",
      "- deadline: " + escape(r.deadline_at || "7am"),
      "",
      "Para cortarlo: `/noche-parar " + escape(r.run_id) + "`",
      "",
      "⚠️ El run vive en el proceso del relay. **Si reiniciás el relay se "
      + "muere** y la fila queda colgada — reiniciá ANTES de arrancarlo.",
    ].join("\n"));
  } catch (e) {
    // El 400 de night_mode_enabled=0 es el más probable y se arregla con
    // un click, así que se nombra en vez de mostrar el crudo.
    const msg = e.status === 400 && /night_mode_enabled/.test(e.message || "")
      ? "este proyecto no tiene el modo nocturno habilitado. Prendé el "
        + "toggle 🌙 en la pestaña Proyectos y volvé a intentar."
      : e.message;
    ctx.responder("/noche", "No pude arrancarlo — " + escape(msg));
  }
}

async function pararNoche(ctx, runId) {
  const id = (runId || "").trim();
  if (!id) {
    ctx.responder("/noche-parar",
      "Necesito el run: `/noche-parar run_xxxxxxxx` (te lo dio `/noche`).");
    return;
  }
  try {
    await apiRoot("/night-mode/stop", { method: "POST", body: { run_id: id } });
    ctx.responder("/noche-parar",
      "🌙 Corté `" + escape(id) + "`. Termina la tarea en curso y emite "
      + "el reporte.");
  } catch (e) {
    ctx.responder("/noche-parar", "No pude pararlo — " + escape(e.message));
  }
}


async function listarPlantillas(ctx) {
  try {
    const q = ctx.project
      ? "night/templates?project=" + encodeURIComponent(ctx.project)
      : "night/templates";
    const r = await api(q);
    const t = r.templates || [];
    if (!t.length) {
      ctx.responder("/plantillas", [
        "No hay plantillas guardadas todavía.",
        "",
        "Se crean en **Config → Modo nocturno**, o guardando la directiva "
        + "de un run que te haya salido bien.",
      ].join("\n"));
      return;
    }
    const filas = t.map((x) =>
      "- `@" + escape(x.nombre) + "`"
      + (x.global ? " _(global)_" : " _(" + escape(x.project_slug) + ")_")
      + (x.notas ? " — " + escape(x.notas) : ""));
    ctx.responder("/plantillas", [
      "**Plantillas del modo nocturno**", "",
      ...filas, "",
      "Para usar una: `/noche @nombre`",
    ].join("\n"));
  } catch (e) {
    ctx.responder("/plantillas", "No pude listarlas — " + escape(e.message));
  }
}
