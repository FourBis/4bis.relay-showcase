// Pruebas puras de validación de destinatarios, idempotencia y redirect OAuth.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/tab-account.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");
const moduleUrl = "data:text/javascript;base64," + Buffer.from(src).toString("base64");
const mod = await import(`${moduleUrl}#first-load`);

assert.deepEqual(mod.parseRecipients(" alex@example.test; sam@example.test, "), {
  ok: true, recipients: ["alex@example.test", "sam@example.test"],
});
assert.deepEqual(mod.parseRecipients("", true), {
  ok: false, recipients: [], error: "Ingresa al menos un destinatario.",
});
assert.equal(mod.parseRecipients("no-es-correo", true).ok, false);
assert.deepEqual(mod.parseRecipients("  "), { ok: true, recipients: [] });

let ids = 0;
const makeId = () => `00000000-0000-4000-8000-${String(++ids).padStart(12, "0")}`;
const storage = {
  values: new Map(),
  getItem(key) { return this.values.get(key) ?? null; },
  setItem(key, value) { this.values.set(key, value); },
};
const hash = async value => {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
};
const draft = { from: "alex@example.test", to: ["sam@example.test"], cc: [], bcc: [],
  subject: "Hola", body: "Texto" };
const first = await mod.requestIdForDraft(draft, "alex@example.test", storage, makeId, hash);
assert.equal(first.id, "00000000-0000-4000-8000-000000000001");
const stored = storage.getItem("relay.account.mail-send-ids.v1");
assert.doesNotMatch(stored, /alex@example\.test|sam@example\.test|Texto|Hola/,
  "localStorage conserva solo hashes y UUID, sin correo ni cuerpo");
const reloaded = await import(`${moduleUrl}#reload`);
assert.equal((await reloaded.requestIdForDraft({ ...draft }, "alex@example.test", storage, makeId, hash)).id,
  first.id, "tras recargar, el mismo contenido conserva el request_id incierto");
const userB = await mod.requestIdForDraft(draft, "sam@example.test", storage, makeId, hash);
assert.notEqual(userB.id, first.id, "el historial de reintentos está separado por usuario");
const changed = await mod.requestIdForDraft({ ...draft, body: "Otro texto" }, "alex@example.test",
  storage, makeId, hash);
assert.equal(changed.id, "00000000-0000-4000-8000-000000000003",
  "cambiar contenido crea otra solicitud");
assert.equal(mod.forgetConfirmedSend(first.fingerprint, storage), true);
assert.equal((await reloaded.requestIdForDraft({ ...draft }, "alex@example.test", storage, makeId, hash)).id,
  "00000000-0000-4000-8000-000000000004",
  "solo una confirmación permite enviar deliberadamente el mismo contenido de nuevo");
assert.equal(await mod.requestIdForDraft(draft, "alex@example.test", null, makeId, hash).then(x => !!x.error),
  true, "sin almacenamiento persistente bloquea el envío antes del HTTP");

assert.equal(mod.isAuthorizationUrl("github", "https://github.com/login/oauth/authorize?state=x"), true);
assert.equal(mod.isAuthorizationUrl("github", "http://github.com/login/oauth/authorize"), false);
assert.equal(mod.isAuthorizationUrl("github", "https://github.com.evil.test/login/oauth/authorize"), false);
assert.equal(mod.isAuthorizationUrl("google", "https://accounts.google.com/o/oauth2/v2/auth?state=x"), true);
assert.equal(mod.isAuthorizationUrl("google", "https://accounts.google.com.evil.test/o/oauth2/v2/auth"), false);
assert.equal(mod.isAuthorizationUrl("other", "https://accounts.google.com/o/oauth2/v2/auth"), false);

console.log("account: recipients, request id y redirect OAuth ok");
