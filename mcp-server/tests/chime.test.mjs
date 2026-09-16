// Test de chime.js (aviso sonoro al terminar un run).
// Node puro, sin deps:  node mcp-server/tests/chime.test.mjs
//
// Se puede importar el módulo directo —no tiene imports y su cuerpo
// top-level no toca el DOM—: alcanza con un localStorage de mentira.
// Lo que se prueba es la parte que decide (mute por conversación); el
// oscilador y el parpadeo del título necesitan navegador y se ejercitan
// a mano en la Admin UI.
import assert from "node:assert/strict";

let store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

const { isMuted, toggleMute, chime } = await import(
  new URL("../admin_static/static/chime.js", import.meta.url));

// --- default: suena ---
// El pedido fue explícito: "parte por default encendido". Una
// conversación que nunca se tocó tiene que sonar.
assert.equal(isMuted("conv-a"), false, "una conversación nueva NO nace muteada");

// --- mute por conversación, no global ---
assert.equal(toggleMute("conv-a"), true, "el primer toggle mutea");
assert.equal(isMuted("conv-a"), true);
assert.equal(isMuted("conv-b"), false, "mutear una NO puede mutear al resto");

// --- vuelve atrás ---
assert.equal(toggleMute("conv-a"), false, "el segundo toggle desmutea");
assert.equal(isMuted("conv-a"), false);

// --- persiste en localStorage, no en memoria del módulo ---
toggleMute("conv-c");
assert.deepEqual(JSON.parse(store["4bis.chime.muted"]), ["conv-c"],
  "el mute tiene que sobrevivir a un F5");

// --- sin convId no explota (chats sin conversación todavía) ---
assert.equal(isMuted(undefined), false);
assert.equal(toggleMute(undefined), false, "sin id no hay nada que mutear");

// --- localStorage corrupto no puede romper el chat ---
store["4bis.chime.muted"] = "{no es json";
assert.equal(isMuted("conv-a"), false, "un storage corrupto degrada a 'no muteado'");

// --- chime() sin DOM ni audio: no debe lanzar ---
// En el navegador esto suena; acá lo que importa es que un entorno sin
// AudioContext ni document no tire una excepción que corte el poller
// del chat justo cuando el run terminó.
store = {};
globalThis.document = { hidden: false };
chime("done", "conv-a");
chime("error", "conv-a");
chime("question", "conv-a");
chime("kind-que-no-existe", "conv-a");

// --- una conversación muteada no intenta nada ---
toggleMute("conv-muda");
chime("done", "conv-muda");

console.log("chime.test.mjs OK");
