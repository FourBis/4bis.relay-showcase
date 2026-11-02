// Secondary controls stay reachable without competing with the conversation.
export function initChatChrome(root = document) {
  root.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const options = event.target?.closest?.("details[data-chat-options][open]");
    if (!options) return;
    event.preventDefault(); event.stopPropagation();
    options.open = false;
    options.querySelector("summary")?.focus();
  });
}
