// Objetos extraídos de respuestas del chat para el workspace.
// El módulo conserva solo referencias pequeñas: el contenido siempre se
// vuelve a pedir al relay al restaurar.
import { apiRoot } from "./api.js";
import { openObject, openWorkspaceModule } from "./workspace.js";

let markdownRuntime = null;
let renderSequence = 0;
const JUNK_RE = /<tool_call>[\s\S]*?(?:<\/tool_call>|$)|\]<\]minimax\[>\[/g;
const QUESTION_RE = /\n*(?:❓|📦) \*\*[\s\S]*?\n`q_[0-9a-f]{6,}`[ \t]*/g;

export function cleanMarkdown(value) {
  return String(value ?? "").replace(JUNK_RE, "").replace(QUESTION_RE, "\n").trim();
}

async function markdown() {
  if (markdownRuntime) return markdownRuntime;
  const [marked, purify] = await Promise.all([
    import("./vendor-marked-18.0.7.esm.js"),
    import("./vendor-purify-3.4.15.es.js"),
  ]);
  markdownRuntime = { marked: marked.marked, purify: purify.default };
  return markdownRuntime;
}

/** Renderiza markdown en un HTMLElement seguro para entregar al workspace. */
export async function renderMarkdown(value) {
  const { marked, purify } = await markdown();
  const root = document.createElement("div");
  root.className = "md-body";
  root.innerHTML = purify.sanitize(marked.parse(cleanMarkdown(value)));
  // Una respuesta puede estar visible en el hilo y en varias ventanas.
  // Prefijar referencias internas evita IDs duplicados, incluso en SVG.
  const prefix = `workspace-content-${++renderSequence}-`;
  const ids = new Map([...root.querySelectorAll('[id]')].map(el => [el.id, prefix + el.id]));
  root.querySelectorAll('*').forEach(el => {
    if (el.id) el.id = ids.get(el.id);
    for (const attr of [...el.attributes]) {
      if (attr.name === 'id') continue;
      let value = attr.value.replace(/url\(#([^)]*)\)/g, (match, id) => ids.has(id) ? `url(#${ids.get(id)})` : match);
      if (['href', 'xlink:href'].includes(attr.name) && ids.has(value.slice(1)) && value.startsWith('#')) value = '#' + ids.get(value.slice(1));
      if (['aria-labelledby', 'aria-describedby'].includes(attr.name)) value = value.split(' ').map(id => ids.get(id) || id).join(' ');
      if (value !== attr.value) el.setAttribute(attr.name, value);
    }
  });
  return root;
}

export function objectReference(conversationId, messageIndex, kind = "response", itemIndex = 0) {
  return {
    conversationId: String(conversationId || ""),
    messageIndex: Number(messageIndex),
    kind: String(kind),
    itemIndex: Number(itemIndex),
  };
}

export function validObjectReference(reference) {
  return !!reference
    && typeof reference.conversationId === "string"
    && reference.conversationId.length > 0
    && Number.isInteger(reference.messageIndex) && reference.messageIndex >= 0
    && ["response", "table", "svg"].includes(reference.kind)
    && Number.isInteger(reference.itemIndex) && reference.itemIndex >= 0;
}

function normalizeReference(reference) {
  return {
    ...reference,
    kind: reference?.kind || "response",
    itemIndex: reference?.itemIndex ?? 0,
  };
}

async function messageSignature(message) {
  const bytes = new TextEncoder().encode(message.content || '');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
}

export function tableText(table) {
  return [...table.rows].filter((row) => !row.hidden).map((row) =>
    [...row.cells].map((cell) => cell.textContent.trim().replace(/\s+/g, " ")).join("\t")
  ).join("\n");
}

function copyButton(text, label = "Copiar") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-xs";
  button.textContent = label;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(typeof text === "function" ? text() : text);
      button.textContent = "Copiado";
      setTimeout(() => { button.textContent = label; }, 1200);
    } catch (_) {
      button.textContent = "No disponible";
      setTimeout(() => { button.textContent = label; }, 1600);
    }
  });
  return button;
}

function toolbar(label) {
  const el = document.createElement("div");
  el.className = "workspace-object-tools";
  const source = document.createElement("span");
  source.className = "text-xs text-zinc-400";
  source.textContent = label;
  el.append(source);
  return el;
}

function tableObject(table) {
  const content = document.createElement("div");
  content.className = "workspace-object-content";
  const bar = toolbar("Tabla extraída de la respuesta");
  const filter = document.createElement("input");
  filter.type = "search";
  filter.className = "input input-sm";
  filter.placeholder = "Filtrar filas…";
  filter.setAttribute("aria-label", "Filtrar filas");
  const copyTarget = table.cloneNode(true);
  bar.append(filter, copyButton(() => tableText(copyTarget), "Copiar tabla"));
  content.append(bar, copyTarget);
  filter.addEventListener("input", () => {
    const term = filter.value.trim().toLowerCase();
    [...copyTarget.tBodies].forEach((tbody) => {
      [...tbody.rows].forEach((row) => {
        row.hidden = !!term && !row.textContent.toLowerCase().includes(term);
      });
    });
  });
  return content;
}

function svgObject(svg) {
  const content = document.createElement("div");
  content.className = "workspace-object-content";
  content.append(toolbar("Gráfico extraído de la respuesta"), svg.cloneNode(true));
  return content;
}

function responseObject(root, rawText) {
  const content = document.createElement("div");
  content.className = "workspace-object-content";
  const bar = toolbar("Respuesta del experto");
  bar.append(copyButton(rawText, "Copiar respuesta"));
  content.append(bar, root);
  return content;
}

/**
 * Construye el objeto para un mensaje ya validado y renderizado.
 * `itemIndex` permite elegir una tabla/SVG concreto sin inventar datos.
 */
export async function messageObject(message, reference) {
  if (!message || message.role !== "assistant") {
    throw new Error("El objeto ya no apunta a una respuesta del experto");
  }
  const root = await renderMarkdown(message.content || "");
  if (reference.kind === "table") {
    const table = root.querySelectorAll("table")[reference.itemIndex];
    if (!table) throw new Error("La tabla ya no está disponible en la respuesta");
    return tableObject(table);
  }
  if (reference.kind === "svg") {
    const svg = root.querySelectorAll("svg")[reference.itemIndex];
    if (!svg) throw new Error("El gráfico ya no está disponible en la respuesta");
    return svgObject(svg);
  }
  return responseObject(root, cleanMarkdown(message.content || ""));
}

function sourceLabel(reference) {
  return `Conversación ${reference.conversationId.slice(0, 8)}… · respuesta ${reference.messageIndex + 1}`;
}

function returnToConversation(conversationId) {
  window.dispatchEvent(new CustomEvent("chat-object-back", {
    detail: { conversationId },
  }));
}

export async function openChatObject({ conversationId, message, messageIndex }) {
  const ref = objectReference(conversationId, messageIndex);
  const descriptor = await chatObjectDescriptor(message, ref);
  return openObject(descriptor);
}

async function chatObjectDescriptor(message, reference) {
  reference = { ...reference, signature: await messageSignature(message) };
  const content = await messageObject(message, reference);
  const bar = content.querySelector('.workspace-object-tools');
  const back = document.createElement('button'); back.className = 'btn btn-xs'; back.textContent = 'Ir a la conversación';
  back.onclick = () => { openWorkspaceModule('chat'); returnToConversation(reference.conversationId); };
  bar.append(back);
  if (reference.kind === 'response') {
    for (const kind of ['table', 'svg']) {
      content.querySelectorAll(kind).forEach((_node, itemIndex) => {
        const button = document.createElement('button'); button.className = 'btn btn-xs';
        button.textContent = `Separar ${kind === 'table' ? 'tabla' : 'gráfico'} ${itemIndex + 1}`;
        button.onclick = async () => {
          const descriptor = await chatObjectDescriptor(message, { ...reference, kind, itemIndex });
          openObject(descriptor);
        };
        bar.append(button);
      });
    }
  }
  return {
    id: `chat-object:${reference.conversationId}:${reference.messageIndex}:${reference.kind}:${reference.itemIndex}`,
    title: reference.kind === "table" ? "Tabla de respuesta"
      : reference.kind === "svg" ? "Gráfico de respuesta" : "Respuesta del experto",
    source: sourceLabel(reference),
    content,
    restore: reference,
  };
}

/** Devuelve el descriptor; workspace.js decide si lo abre o reemplaza. */
export async function restoreChatObject(reference) {
  reference = normalizeReference(reference);
  if (!validObjectReference(reference)) throw new Error("Referencia de objeto inválida");
  const response = await apiRoot(
    `/conversations/${encodeURIComponent(reference.conversationId)}/messages`
    + "?max_turns=2000&content_cap=100000", null, 30_000);
  const message = response?.messages?.[reference.messageIndex];
  if (!message || message.role !== "assistant") {
    throw new Error("La respuesta de la conversación ya no está disponible");
  }
  if (reference.signature && reference.signature !== await messageSignature(message)) {
    throw new Error('La respuesta de origen cambió. Vuelve a abrir el objeto desde la conversación.');
  }
  return chatObjectDescriptor(message, reference);
}

export function openChatWorkspace() {
  return openWorkspaceModule("chat");
}
