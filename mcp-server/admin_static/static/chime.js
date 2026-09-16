// Aviso sonoro cuando un run termina.
//
// Por qué existe: un run largo se lanza y uno se va a hacer otra cosa. La
// pestaña queda atrás y la respuesta espera en una pantalla que nadie
// mira. El sonido es la señal más barata de "ya está".
//
// Encendido por default, mute POR CONVERSACIÓN (una conversación de
// prueba que corre cada dos minutos no tiene por qué sonar). La
// preferencia vive en localStorage: es del navegador, no del relay —
// nada que migrar ni endpoint que agregar.
//
// Sin archivos de audio: WebAudio genera los tonos. Un .mp3 sería un
// asset más que servir, cachear y versionar para tres pitidos.

const MUTE_KEY = "4bis.chime.muted";

function mutedSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem(MUTE_KEY) || "[]"));
  } catch (_) {
    return new Set();   // localStorage corrupto no puede romper el chat
  }
}

export function isMuted(convId) {
  return !!convId && mutedSet().has(convId);
}

/** Alterna el mute de una conversación. Devuelve el estado nuevo. */
export function toggleMute(convId) {
  if (!convId) return false;
  const s = mutedSet();
  s.has(convId) ? s.delete(convId) : s.add(convId);
  try { localStorage.setItem(MUTE_KEY, JSON.stringify([...s])); } catch (_) { /* modo privado */ }
  return s.has(convId);
}

// Tres avisos distintos porque son tres noticias distintas. El de
// `question` es el más importante: un run que quedó esperando una
// decisión no avanza solo, y hasta ahora eso no avisaba nada.
const TONOS = {
  done: [[660, 0, 0.09], [880, 0.1, 0.14]],            // dos notas que suben
  error: [[440, 0, 0.11], [330, 0.12, 0.20]],          // dos que bajan
  question: [[880, 0, 0.07], [880, 0.13, 0.07], [1046, 0.26, 0.13]],
};

let ctx = null;

function beep(kind) {
  const notas = TONOS[kind] || TONOS.done;
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    // El navegador suspende el contexto hasta que haya una interacción.
    // En la Admin UI siempre hubo un click antes, pero si no, esto lo
    // reanuda en vez de fallar en silencio.
    if (ctx.state === "suspended") ctx.resume();
    for (const [hz, at, dur] of notas) {
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = hz;
      // Ataque y caída suaves: un gain que salta de 0 a 1 hace "click".
      const t0 = ctx.currentTime + at;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.18, t0 + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    }
  } catch (_) { /* sin audio el resto del aviso sigue funcionando */ }
}

// Parpadeo del título: cubre el caso de la ventana atrás de otra, donde
// el sonido puede perderse pero el gusano de la barra de tareas no.
let blinkTimer = null, tituloReal = null;
function blinkTitle(texto) {
  if (!document.hidden) return;
  tituloReal = tituloReal ?? document.title;
  clearInterval(blinkTimer);
  let on = false;
  blinkTimer = setInterval(() => {
    document.title = (on = !on) ? texto : tituloReal;
  }, 900);
  const parar = () => {
    clearInterval(blinkTimer);
    if (tituloReal) document.title = tituloReal;
    tituloReal = null;
    document.removeEventListener("visibilitychange", parar);
  };
  document.addEventListener("visibilitychange", parar);
}

const TEXTOS = {
  done: ["✅ El experto terminó", "Terminó"],
  error: ["🔴 El run terminó con error", "Error"],
  question: ["❓ El experto necesita una decisión", "Te pregunta algo"],
};

/**
 * Avisa que un run terminó: sonido + título + notificación de escritorio.
 * `convId` decide el mute; sin él, suena igual.
 */
export function chime(kind, convId, detalle = "") {
  if (isMuted(convId)) return;
  const [largo, corto] = TEXTOS[kind] || TEXTOS.done;
  beep(kind);
  blinkTitle(`🔔 ${corto}`);
  try {
    // Solo si YA está concedido: pedir el permiso de motu propio en
    // medio de un run es el patrón que hace que la gente lo bloquee
    // para siempre. El botón 🔔 del chat es el que lo pide.
    if (window.Notification && Notification.permission === "granted") {
      new Notification(largo, { body: detalle.slice(0, 160) || undefined, tag: convId || "4bis" });
    }
  } catch (_) { /* notificaciones bloqueadas: ya sonó */ }
}

/** Pide el permiso de notificaciones. Lo llama el botón, no el poller. */
export async function pedirPermisoNotificaciones() {
  try {
    if (!window.Notification || Notification.permission !== "default") return;
    await Notification.requestPermission();
  } catch (_) { /* navegador sin soporte */ }
}
