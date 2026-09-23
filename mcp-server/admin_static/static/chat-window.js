// ponytail: cada chat reutiliza la vista completa; si muchas ventanas pesan,
// separar el controlador por instancia antes de reemplazar los iframes.
const ID_RE = /^[A-Za-z0-9_-]{1,160}$/;
let visible = true;
let lastContext = null;

export function getEmbeddedConversation() {
  const value = new URLSearchParams(globalThis.location?.search || "")
    .get("chat-window") || "";
  return ID_RE.test(value) ? value : "";
}

export function createConversationFrame(conversationId, title = "Conversación Relay") {
  if (!ID_RE.test(String(conversationId || ""))) return null;
  const frame = document.createElement("iframe");
  frame.className = "workspace-conversation-frame";
  frame.src = `/admin/?chat-window=${encodeURIComponent(conversationId)}#/chat`;
  frame.title = String(title || "Conversación Relay").slice(0, 160);
  frame.setAttribute("aria-label", frame.title);
  return frame;
}

export function announceEmbeddedChat({ conversationId, projectSlug = "", title = "" } = {}) {
  if (!ID_RE.test(String(conversationId || ""))) return false;
  lastContext = {
    conversationId: String(conversationId),
    projectSlug: String(projectSlug || "").slice(0, 160),
    title: String(title || "").slice(0, 160),
  };
  if (getEmbeddedConversation() && globalThis.parent && globalThis.parent !== globalThis) {
    globalThis.parent.postMessage({ type: "relay-chat-context", ...lastContext },
      globalThis.location.origin);
  }
  return true;
}

export function isChatViewVisible() {
  return !getEmbeddedConversation() || visible;
}

export async function bootEmbeddedChat({ initChats, selectConversation,
  initPollers, projectSlug = "", title = "" } = {}) {
  const conversationId = getEmbeddedConversation();
  if (!conversationId || typeof initChats !== "function"
      || typeof selectConversation !== "function") return false;
  document.body.classList.add("chat-embedded");
  const main = document.getElementById("main");
  const chat = document.getElementById("tab-chat");
  if (main && chat) {
    main.append(chat);
    chat.hidden = false;
    chat.classList.add("active");
  }
  initChats();
  window.addEventListener("message", event => {
    if (event.origin === location.origin && event.source === window.parent &&
        event.data?.type === "relay-chat-visibility" && typeof event.data.visible === "boolean") {
      visible = event.data.visible;
    }
  });
  initPollers?.(() => new Set(visible ? ["chat"] : []));
  const context = { conversationId, projectSlug, title };
  const announce = () => announceEmbeddedChat(lastContext || context);
  document.addEventListener("pointerdown", announce, { passive: true });
  document.addEventListener("focusin", announce, { passive: true });
  document.addEventListener("keydown", event => {
    if (window.parent !== window && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      window.parent.postMessage({ type: "relay-chat-shortcut", shift: event.shiftKey }, location.origin);
    }
  });
  announce();
  if (await selectConversation(conversationId) === false) {
    throw new Error("La conversación no está disponible");
  }
  announce();
  return true;
}
