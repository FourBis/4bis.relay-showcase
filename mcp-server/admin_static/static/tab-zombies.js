// Tab Zombies (Iter 5.3): chats running viejos sin proceso vivo.
// Endpoint GET /admin/api/chats/zombies + DELETE por chat_id.

import { $, $$, api, escape, _dbg, onClick } from "./api.js";
import { confirmModal } from "./ui.js";

export async function loadZombies() {
  const olderThan = $("#zombies-older-than").value;
  try {
    // Endpoint vive en /admin/api/ (ver admin.py:3021). api() lo prepende;
    // apiRoot() pegaría a la raíz y 404'ea.
    const r = await api(`chats/zombies?older_than_s=${olderThan}`);
    _dbg("loadZombies ok", "count:", r.count);
    const tbody = $("#zombies-table tbody");
    if (!r.zombies.length) {
      tbody.innerHTML = "";
      $("#zombies-empty").hidden = false;
      $("#zombies-summary").textContent = "0 zombies";
      updateBadge(0);
      return;
    }
    $("#zombies-empty").hidden = true;
    tbody.innerHTML = r.zombies.map((z) => `
      <tr data-chat="${escape(z.id)}">
        <td><code class="text-xs">${escape(z.id)}</code></td>
        <td><code>${escape(z.project_slug || "—")}</code></td>
        <td class="whitespace-nowrap">${escape(z.started_at || "—")}</td>
        <td>${escape(z.source || "—")}</td>
        <td class="row-actions">
          <button class="btn btn-xs zombie-delete-btn" data-chat="${escape(z.id)}"
                  title="hard delete de la fila (irreversible)">Eliminar</button>
        </td>
      </tr>`).join("");
    $$(".zombie-delete-btn").forEach((b) =>
      b.addEventListener("click", () => deleteZombie(b.dataset.chat)));
    $("#zombies-summary").textContent =
      `${r.count} zombie${r.count === 1 ? "" : "s"}`;
    updateBadge(r.count);
  } catch (e) {
    _dbg("loadZombies ERROR", e.message);
    $("#zombies-summary").textContent = "error: " + e.message;
  }
}

export async function deleteZombie(chatId) {
  if (!await confirmModal({
    title: `Eliminar chat ${chatId.slice(0, 12)}…`,
    body: "Esto borra la fila de DB + el .md asociado (best-effort). "
      + "Es irreversible.",
    confirmText: "Sí, eliminar",
    danger: true,
  })) return;
  try {
    await api(`chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
    });
    loadZombies();
  } catch (e) {
    $("#zombies-summary").textContent = "error: " + e.message;
  }
}

function updateBadge(count) {
  const b = $("#zombies-tab-badge");
  if (!b) return;
  if (count > 0) {
    b.textContent = String(count);
    b.classList.remove("hidden");
  } else {
    b.classList.add("hidden");
  }
}

export function initZombies() {
  onClick("#zombies-refresh", loadZombies);
  $("#zombies-older-than").onchange = loadZombies;
}