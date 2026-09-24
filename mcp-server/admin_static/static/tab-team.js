// Equipo: administra permisos Relay sobre identidades ya confirmadas por Access.
import { $, api, escape, onClick } from "./api.js";
import { emptyRow, skeletonRows, toast } from "./ui.js";

let snapshot = { users: [], roles: [], role_labels: {}, me: null };
let editingEmail = "";
let actorIdentity = null;

export function canManageTeamUser(actor, user, users) {
  if (!canEditTeamUser(actor, user)) return false;
  return !isLastActiveAdmin(user, users);
}

export function canEditTeamUser(actor, user) {
  if (!actor || !user) return false;
  return actor.role === "owner" || (actor.role === "subadmin" && user.role === "member");
}

export function projectAccessLabel(role, projectSlugs = []) {
  if (role === "owner") return "Todos los proyectos";
  if (role === "finance") return "Sin escritura";
  return Array.isArray(projectSlugs) && projectSlugs.length ? "Proyectos asignados" : "Solo lectura";
}

export function teamUserPayload({ email, display_name, role, enabled, projectSlugs, canAssignProjects }) {
  const payload = { email, display_name, role, enabled };
  if (canAssignProjects) payload.project_slugs = projectSlugs;
  return payload;
}

function projectScopeDescription(role) {
  if (role === "owner") return "Administra el equipo y puede trabajar en todos los proyectos.";
  if (role === "subadmin") return "Puede editar, compilar y probar proyectos asignados; también gestionar integrantes Dev. No publica ni integra PR.";
  if (role === "member") return "Puede editar, compilar y probar solo proyectos asignados. No administra equipo ni publica o integra PR.";
  if (role === "finance") return "Consulta CRM e informes; no puede escribir en proyectos.";
  return "Sin permisos de escritura en proyectos.";
}

function projectNames(user) {
  const names = new Map((snapshot.projects || []).map((project) => [project.slug, project.name || project.slug]));
  return (user.project_slugs || []).map((slug) => names.get(slug) || slug);
}

function isEnabled(user) {
  return user.enabled !== false && user.enabled !== 0;
}

function isLastActiveAdmin(user, users) {
  return user.role === "owner" && isEnabled(user)
    && users.filter((entry) => entry.role === "owner" && isEnabled(entry)).length === 1;
}

function resetForm() {
  editingEmail = "";
  $("#team-form").innerHTML = formMarkup();
  $("#team-save").textContent = "Guardar integrante";
  $("#team-cancel").hidden = true;
}

function formMarkup(user = null) {
  const defaultRole = snapshot.roles.includes("member") ? "member" : snapshot.roles[0];
  const roles = snapshot.roles.map((role) => {
    const selected = role === (user?.role || defaultRole) ? " selected" : "";
    return `<option value="${escape(role)}"${selected}>${escape(snapshot.role_labels[role] || role)}</option>`;
  }).join("");
  const roleDisabled = user && isLastActiveAdmin(user, snapshot.users) ? " disabled" : "";
  const email = user
    ? `<input id="team-email" class="input" type="email" value="${escape(user.email)}" readonly>`
    : '<input id="team-email" class="input" type="email" autocomplete="off" placeholder="correo confirmado">';
  const projects = snapshot.can_assign_projects
    ? `<fieldset class="field min-w-0"><legend class="field-label">Proyectos con escritura</legend><p class="field-hint">Puede editar y ejecutar compilaciones y pruebas en los proyectos marcados. No incluye publicar ni integrar PR. Admin siempre tiene acceso a todos; Finanzas no tiene escritura.</p><div class="mt-2 grid gap-2 sm:grid-cols-2">${(snapshot.projects || []).map((project) => {
      const roleHasProjectWrites = !["owner", "finance"].includes(user?.role);
      const checked = roleHasProjectWrites && (user?.project_slugs || []).includes(project.slug) ? " checked" : "";
      const disabled = roleHasProjectWrites ? "" : " disabled";
      return `<label class="flex min-w-0 items-start gap-2 text-sm text-zinc-300"><input class="mt-1" type="checkbox" name="team-project" value="${escape(project.slug)}"${checked}${disabled}><span>${escape(project.name || project.slug)} <code class="text-xs text-zinc-500">${escape(project.slug)}</code></span></label>`;
    }).join("") || '<span class="muted text-sm">No hay proyectos disponibles.</span>'}</div></fieldset>`
    : "";
  return `<div class="field"><label class="field-label" for="team-name">Nombre</label><input id="team-name" class="input" value="${escape(user?.display_name || "")}" autocomplete="name" placeholder="Nombre visible"></div>
    <div class="field"><label class="field-label" for="team-email">Correo</label>${email}</div>
    <div class="field"><label class="field-label" for="team-role">Rol</label><select id="team-role" class="select"${roleDisabled}>${roles}</select><p id="team-role-scope" class="field-hint">${escape(projectScopeDescription(user?.role || defaultRole))}</p></div>${projects}`;
}

export function initTeam(actor) {
  actorIdentity = actor;
  resetForm();
  onClick("#team-save", saveTeamUser);
  onClick("#team-cancel", resetForm);
  $("#team-table tbody").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
    const user = snapshot.users.find((entry) => entry.email === button.dataset.email);
    if (!user || !canEditTeamUser(snapshot.me, user)) return;
    if (button.dataset.action === "edit") {
      editingEmail = user.email;
      $("#team-form").innerHTML = formMarkup(user);
      $("#team-save").textContent = "Guardar cambios";
      $("#team-cancel").hidden = false;
      $("#team-name").focus();
    } else {
      updateTeamUser(user, !user.enabled);
    }
  });
  $("#team-form").addEventListener("change", (event) => {
    if (event.target.id === "team-role") {
      const scope = $("#team-role-scope");
      if (scope) scope.textContent = projectScopeDescription(event.target.value);
      $("#team-form").querySelectorAll('input[name="team-project"]').forEach((input) => {
        if (["owner", "finance"].includes(event.target.value)) input.checked = false;
        input.disabled = ["owner", "finance"].includes(event.target.value);
      });
    }
  });
}

export async function loadTeam() {
  const tbody = $("#team-table tbody");
  tbody.innerHTML = skeletonRows(4, 3);
  try {
    snapshot = await api("users");
    snapshot.users ||= [];
    snapshot.roles ||= [];
    snapshot.role_labels ||= {};
    snapshot.projects ||= [];
    snapshot.me = actorIdentity;
    renderUsers();
    if (!editingEmail) resetForm();
  } catch (error) {
    tbody.innerHTML = emptyRow(4, { title: "No se pudo cargar el equipo", sub: error.message });
  }
}

function renderUsers() {
  const tbody = $("#team-table tbody");
  if (!snapshot.users.length) {
    tbody.innerHTML = emptyRow(4, {
      title: "Todavía no hay integrantes configurados",
      sub: "Agrega una identidad usando el correo que confirma Access.",
    });
    return;
  }
  tbody.innerHTML = snapshot.users.map((user) => {
    const label = snapshot.role_labels[user.role] || user.role;
    const enabled = isEnabled(user);
    const actions = canEditTeamUser(snapshot.me, user)
      ? `<div class="cell-actions"><button class="btn btn-xs" data-action="edit" data-email="${escape(user.email)}">Editar</button>${canManageTeamUser(snapshot.me, user, snapshot.users) ? `<button class="btn btn-xs${enabled ? " danger" : ""}" data-action="toggle" data-email="${escape(user.email)}">${enabled ? "Deshabilitar" : "Activar"}</button>` : ""}</div>`
      : '<span class="muted text-xs">—</span>';
    const slugs = user.project_slugs || [];
    const assignedProjects = projectNames(user);
    const projectAccess = projectAccessLabel(user.role, slugs);
    const projectDetail = user.role === "owner" ? "" : user.role === "finance" || !slugs.length
      ? "" : `<ul class="mt-1 list-inside list-disc text-xs text-zinc-400">${assignedProjects.map((name) => `<li>${escape(name)}</li>`).join("")}</ul>`;
    const scope = user.role === "member" && !slugs.length
      ? "Sin proyectos asignados; solo puede consultar. No administra el equipo ni publica o integra cambios."
      : projectScopeDescription(user.role);
    return `<tr><td><div class="font-medium text-zinc-200">${escape(user.display_name || "—")}</div><code class="block max-w-full truncate text-xs text-zinc-400" title="${escape(user.email)}">${escape(user.email)}</code><div class="mt-1"><span class="badge ${user.role === "owner" ? "ok" : "dim"}">${escape(label)}</span></div></td><td><strong class="text-sm">${projectAccess}</strong>${projectDetail}<p class="mt-1 text-xs text-zinc-400">${escape(scope)}</p></td><td><span class="badge ${enabled ? "ok" : "dim"}">${enabled ? "Activo" : "Deshabilitado"}</span></td><td class="text-right">${actions}</td></tr>`;
  }).join("");
}

async function saveTeamUser() {
  const message = $("#team-message");
  const email = $("#team-email").value.trim().toLowerCase();
  const display_name = $("#team-name").value.trim();
  const role = $("#team-role").value;
  if (!/^\S+@\S+\.\S+$/.test(email)) { message.textContent = "Ingresa un correo válido."; $("#team-email").focus(); return; }
  if (!display_name) { message.textContent = "Ingresa el nombre visible."; $("#team-name").focus(); return; }
  if (!snapshot.roles.includes(role)) { message.textContent = "El rol no está disponible para tu cuenta."; return; }
  const current = snapshot.users.find((user) => user.email === editingEmail);
  if (current && !canEditTeamUser(snapshot.me, current)) {
    message.textContent = "No puedes modificar esta cuenta."; return;
  }
  if (current && isLastActiveAdmin(current, snapshot.users) && role !== current.role) {
    message.textContent = "No se puede cambiar el rol del único Admin activo."; return;
  }
  try {
    const projectSlugs = !snapshot.can_assign_projects ? undefined
      : role === "finance" ? []
      : Array.from(document.querySelectorAll('#team-form input[name="team-project"]:checked'), (input) => input.value);
    await api("users", { method: "PUT", body: teamUserPayload({
      email, display_name, role, enabled: !current || isEnabled(current),
      projectSlugs, canAssignProjects: snapshot.can_assign_projects === true,
    }) });
    toast(current ? "Integrante actualizado" : "Integrante agregado", "ok");
    message.textContent = "";
    resetForm();
    await loadTeam();
  } catch (error) { message.textContent = `Error: ${error.message}`; }
}

async function updateTeamUser(user, enabled) {
  const message = $("#team-message");
  try {
    await api("users", { method: "PUT", body: teamUserPayload({
      email: user.email, display_name: user.display_name || "",
      role: user.role, enabled,
      canAssignProjects: false,
    }) });
    toast(enabled ? "Acceso al Relay activado" : "Acceso al Relay deshabilitado", "ok");
    await loadTeam();
  } catch (error) { message.textContent = `Error: ${error.message}`; }
}
