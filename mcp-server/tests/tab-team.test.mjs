// Límite de edición de identidades del tab Equipo. Node puro, sin deps.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const src = readFileSync(
  new URL("../admin_static/static/tab-team.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");
const { canEditTeamUser, canManageTeamUser, projectAccessLabel, teamUserPayload } = await import(
  "data:text/javascript," + encodeURIComponent(src));

const actor = (role) => ({ role });
const user = (role, enabled = true) => ({ role, enabled });

test("único Admin permite editar nombre pero protege rol y estado", () => {
  const admin = user("owner");
  assert.equal(canManageTeamUser(actor("owner"), admin, [admin]), false);
  assert.equal(canEditTeamUser(actor("owner"), admin), true);
});

test("estado legacy 0 se trata como deshabilitado", () => {
  const disabled = { role: "owner", enabled: 0 };
  assert.equal(canManageTeamUser(actor("owner"), disabled, [disabled]), true);
});

test("owner puede editar el Admin si hay otro Admin activo", () => {
  assert.equal(canManageTeamUser(actor("owner"), user("owner"), [user("owner"), user("owner")]), true);
});

test("subadmin sólo puede editar integrantes Dev", () => {
  assert.equal(canManageTeamUser(actor("subadmin"), user("member"), [user("member")]), true);
  assert.equal(canManageTeamUser(actor("subadmin"), user("finance"), [user("finance")]), false);
  assert.equal(canManageTeamUser(actor("subadmin"), user("subadmin"), [user("subadmin")]), false);
  assert.equal(canManageTeamUser(actor("subadmin"), user("owner"), [user("owner")]), false);
});

test("cuenta sin rol no administra identidades", () => {
  assert.equal(canManageTeamUser(null, user("member"), [user("member")]), false);
});

test("proyectos sin asignación son de solo lectura; finanzas y owner conservan su alcance", () => {
  assert.equal(projectAccessLabel("member", []), "Solo lectura");
  assert.equal(projectAccessLabel("subadmin", ["relay"]), "Proyectos asignados");
  assert.equal(projectAccessLabel("finance", ["relay"]), "Sin escritura");
  assert.equal(projectAccessLabel("owner", []), "Todos los proyectos");
});

test("PUT limpia asignaciones vacías solo cuando el actor puede asignar proyectos", () => {
  const base = { email: "sam@example.test", display_name: "Dev", role: "member", enabled: true };
  assert.deepEqual(teamUserPayload({ ...base, projectSlugs: [], canAssignProjects: true }), {
    ...base, project_slugs: [],
  });
  assert.deepEqual(teamUserPayload({ ...base, projectSlugs: undefined, canAssignProjects: false }), base);
});
