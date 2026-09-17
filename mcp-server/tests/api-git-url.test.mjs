import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

globalThis.location = { href: "" };
const src = readFileSync(
  new URL("../admin_static/static/api.js", import.meta.url), "utf8");
const { gitWebUrl } = await import(
  "data:text/javascript," + encodeURIComponent(src));

test("normaliza remotes scp y ssh sin usuario", () => {
  assert.equal(gitWebUrl("git@github.com:FourBis/relay.git"),
               "https://github.com/FourBis/relay");
  assert.equal(gitWebUrl("ssh://github.com/FourBis/relay.git"),
               "https://github.com/FourBis/relay");
  assert.equal(gitWebUrl("ssh://git@github.com/FourBis/relay.git"),
               "https://github.com/FourBis/relay");
});

test("conserva HTTPS limpio y rechaza userinfo o componentes peligrosos", () => {
  assert.equal(gitWebUrl("https://github.com/FourBis/relay.git"),
               "https://github.com/FourBis/relay");
  assert.equal(gitWebUrl("http://git.example.test:8080/FourBis/relay.git"),
               "http://git.example.test:8080/FourBis/relay");
  assert.equal(gitWebUrl("https://git.example.test:8443/FourBis/relay"),
               "https://git.example.test:8443/FourBis/relay");
  assert.equal(gitWebUrl("https://user:secret@github.com/FourBis/relay"), null);
  assert.equal(gitWebUrl("https://github.com/FourBis/relay?tab=code"), null);
  assert.equal(gitWebUrl("javascript:alert(1)"), null);
  assert.equal(gitWebUrl("git@[bad:host/FourBis/relay"), null);
});
