// Test del panel del grafo (F3). Node puro:
//   node mcp-server/tests/chat-grafo.test.mjs
//
// Mismo truco que chat-tools.test.mjs: se carga el módulo por data: URL
// neutralizando los imports, porque api.js/ui.js tocan el DOM. Lo que se
// prueba es lo único del módulo que NO lo toca y lo único donde un error
// pasa desapercibido: la geometría del dibujo. Un SVG torcido no tira
// excepción — se ve raro y nadie sabe por qué.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/chat-grafo.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "")
  .replace(/^/, "const escape = (s) => String(s ?? '').replace(/[&<>]/g,"
         + " (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n"
         + "const $ = () => null; const apiRoot = async () => ({});\n"
         + "const toast = () => {}; const confirmModal = async () => false;\n");

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(src).toString("base64"));
const { svgDelGrafo, resumenTexto, envolver, cronometro,
        arranqueMasViejo } = mod;

// ---- helpers ----

function grafo(tasks, capas) {
  return { tasks, capas, progreso: {} };
}
const tarea = (id, estado = "pendiente", deps = [], titulo = id) =>
  ({ id, titulo, estado, deps, detalle: "", resultado: "", error: "",
     intentos: 1, max_intentos: 2 });

{
  const parent = { ...tarea("parent", "fallado"), sustituido: true,
    presupuesto_agotado: true };
  const svg = svgDelGrafo(grafo([parent], [["parent"]]));
  assert.ok(svg.includes("subdividida"));
  assert.ok(!svg.includes('class="gnodo fallado'));
  assert.ok(!svg.includes("sin presupuesto"));
  assert.equal(resumenTexto({total: 5, hechos: 5, sustituidos: 1}),
    "5/5 hechas · 1 subdividida");
}

// Los `y` de cada nodo, en orden de aparición.
function ys(svg) {
  return [...svg.matchAll(/<rect x="([\d.]+)" y="([\d.]+)"/g)]
    .map((m) => Number(m[2]));
}
function anchoNodo(svg) {
  return Number(svg.match(/<rect[^>]*width="([\d.]+)"/)[1]);
}
function xs(svg) {
  return [...svg.matchAll(/<rect x="([\d.]+)" y="([\d.]+)"/g)]
    .map((m) => Number(m[1]));
}

// ---- el dibujo ----

{
  const svg = svgDelGrafo(grafo(
    [tarea("a"), tarea("b"), tarea("c", "pendiente", ["a", "b"])],
    [["a", "b"], ["c"]]));

  // Dos filas: los de la misma capa comparten `y`, el de la siguiente no.
  const [ya, yb, yc] = ys(svg);
  assert.equal(ya, yb, "los nodos de una misma capa van a la misma altura");
  assert.ok(yc > ya, "la capa siguiente va más abajo");
  // …y los de la misma fila NO se superponen.
  const [xa, xb] = xs(svg);
  assert.notEqual(xa, xb, "dos nodos de la misma fila no se pisan");
  // El ancho del nodo sale del módulo, no de una constante copiada acá:
  // así cambiar la geometría no deja este test mintiendo en verde.
  assert.ok(Math.abs(xa - xb) >= anchoNodo(svg),
            "y no se solapan (ancho del nodo)");

  // Una arista por dependencia.
  assert.equal((svg.match(/class="garista/g) || []).length, 2);
}

{
  // Una fila de un solo nodo queda centrada: si no, una cadena se lee
  // como una escalera pegada a la izquierda.
  const svg = svgDelGrafo(grafo(
    [tarea("a"), tarea("b"), tarea("c", "pendiente", ["a", "b"])],
    [["a", "b"], ["c"]]));
  const [xa, xb, xc] = xs(svg);
  assert.ok(xc > xa && xc < xb, "el nodo solo va centrado entre los dos");
}

{
  // Toda arista baja: sale del borde inferior del padre y entra por el
  // superior del hijo. Es lo que hace que el dibujo se lea de arriba
  // abajo sin flechas que vuelven.
  const svg = svgDelGrafo(grafo(
    [tarea("a"), tarea("b", "pendiente", ["a"])], [["a"], ["b"]]));
  const d = svg.match(/class="garista[^"]*" d="M([\d.]+) ([\d.]+) C[^"]*? ([\d.]+) ([\d.]+)"/);
  assert.ok(d, "la arista tiene path");
  assert.ok(Number(d[4]) > Number(d[2]), "la arista va hacia abajo");
}

// ---- el estado se ve ----

{
  const svg = svgDelGrafo(grafo(
    [tarea("a", "hecho"), tarea("b", "corriendo", ["a"]),
     tarea("c", "bloqueado", ["b"])],
    [["a"], ["b"], ["c"]]));
  assert.match(svg, /class="gnodo hecho"/);
  assert.match(svg, /class="gnodo corriendo"/);
  assert.match(svg, /class="gnodo bloqueado"/);
  // La arista de una dependencia YA cumplida se prende: así se ve por
  // dónde pasó el plan.
  assert.equal((svg.match(/garista lista/g) || []).length, 1,
               "solo la arista que sale de un nodo hecho va prendida");
}

{
  // El título del nodo es texto de un LLM: entra en un SVG, así que si
  // no se escapa rompe el dibujo entero (o algo peor).
  const svg = svgDelGrafo(grafo(
    [tarea("a", "pendiente", [], "<script>x</script>"), tarea("b")],
    [["a", "b"]]));
  assert.ok(!svg.includes("<script>"), "el título va escapado");
}

{
  // Un grafo vacío no dibuja nada en vez de reventar.
  assert.equal(svgDelGrafo(grafo([], [])), "");
}

{
  // Un título largo se recorta: 132px no dan para una frase, y el texto
  // desbordado se monta sobre el nodo de al lado.
  const largo = "migrar el esquema completo de la base de produccion";
  const svg = svgDelGrafo(grafo(
    [tarea("a", "pendiente", [], largo), tarea("b")], [["a", "b"]]));
  // Solo las del PRIMER nodo: el svg trae los tspans de todos, y
  // contarlos juntos daba 4 y hacía fallar el test por su propio bug.
  const primero = svg.match(/<g class="gnodo[^"]*" data-id="a">.*?<\/g>/s)[0];
  const lineas = [...primero.matchAll(/<tspan[^>]*>([^<]*)<\/tspan>/g)]
    .map((m) => m[1]);
  assert.ok(lineas.length <= 2, "como mucho dos líneas por nodo");
  for (const ln of lineas) assert.ok(ln.length <= 16, `"${ln}" desborda`);
  assert.ok(lineas.join("").endsWith("…"), "y se marca que está recortado");
  // …pero el título entero sigue disponible en el tooltip.
  assert.ok(svg.includes(largo), "el <title> lleva el texto completo");
}

// ---- el texto adentro del nodo ----
//
// Es lo único del dibujo que el humano LEE. Si acá se corta mal, el
// nodo dice "Migrar las tab…" y hay que hacer click para saber qué es
// cada cosa — o sea, el dibujo deja de servir para lo único que tiene
// que servir, que es entender el plan de un vistazo.

{
  assert.deepEqual(envolver("Migrar las tablas", 16, 2),
                   ["Migrar las"      , "tablas"]);
  // Corta por palabra, no por caracter.
  for (const ln of envolver("Verificar contra produccion", 16, 2)) {
    assert.ok(ln.length <= 16, `"${ln}" entra en la línea`);
  }
}

{
  // Lo que no entra en dos líneas se marca. Sin el `…` el humano lee un
  // título truncado creyendo que está completo, que es peor que
  // truncarlo: cambia lo que entiende que hace la tarea.
  const l = envolver(
    "Migrar todas las tablas de clientes y sus indices", 16, 2);
  assert.equal(l.length, 2);
  assert.ok(l[1].endsWith("…"), `la última marca el corte: ${l.join(" | ")}`);
}

{
  // Un título corto NO lleva `…`.
  assert.deepEqual(envolver("Leer", 16, 2), ["Leer"]);
  assert.ok(!envolver("Migrar las tablas", 16, 2).join("").includes("…"));
}

{
  // Una palabra sola más larga que la línea —una ruta, un identificador—
  // se corta igual: dejarla entera desbordaría el nodo.
  const l = envolver("src/relay/orquestador_de_tareas.py", 16, 2);
  for (const ln of l) assert.ok(ln.length <= 16, `"${ln}" desborda`);
}

{
  // Nada rompe con vacío.
  assert.deepEqual(envolver("", 16, 2), [""]);
  assert.deepEqual(envolver(null, 16, 2), [""]);
}

// ---- el resumen de arriba ----

{
  assert.equal(resumenTexto({ total: 5, hechos: 2 }), "2/5 hechas");
  assert.equal(
    resumenTexto({ total: 5, hechos: 2, corriendo: 2, fallados: 1 }),
    "2/5 hechas · 2 corriendo · 1 fallada");
  assert.match(resumenTexto({ total: 3, hechos: 0, bloqueados: 2 }),
               /2 bloqueadas/);
  // Lo que espera al humano es lo que más importa que se lea.
  assert.match(resumenTexto({ total: 3, hechos: 1, esperando_humano: 1 }),
               /te espera/);
}

// ---- el cronómetro del nodo vivo (2026-08-24) ----
//
// El panel se veía congelado durante los nodos largos: el contador de
// hechas solo se mueve cuando una tarea CIERRA, así que 25 minutos de
// capturas dejaban "3/12 hechas" quieto. Pasó de verdad — el nodo estaba
// vivo escribiendo PNGs, lo dimos por muerto, y se mandaron mensajes al
// hilo mientras tanto. Por eso el formato es de cronómetro y no "hace
// 26m": tiene que verse MOVER cada segundo.

{
  const t0 = Date.parse("2026-08-24T00:00:00Z");
  const en = (s) => cronometro("2026-08-24T00:00:00Z", t0 + s * 1000);
  assert.equal(en(0), "0:00");
  assert.equal(en(4), "0:04");
  assert.equal(en(59), "0:59");
  assert.equal(en(60), "1:00");
  assert.equal(en(26 * 60 + 4), "26:04");
  assert.equal(en(59 * 60 + 59), "59:59");
  assert.equal(en(3600), "1:00:00");
  assert.equal(en(3 * 3600 + 7 * 60 + 9), "3:07:09");

  // Truncar y no redondear: un cronómetro que muestra 0:01 con 600ms
  // corridos adelanta, y el número tiene que poder compararse con el
  // `started_at` que se ve en la base.
  assert.equal(cronometro("2026-08-24T00:00:00Z", t0 + 1900), "0:01");

  // Sin fecha no se inventa nada: un "56 años" por parsear mal es peor
  // que no decir nada.
  assert.equal(cronometro(""), "");
  assert.equal(cronometro(null), "");
  assert.equal(cronometro("cualquier cosa"), "");
  // Reloj del cliente atrasado respecto del servidor: nunca negativo.
  assert.equal(cronometro("2026-08-24T00:00:10Z", t0), "0:00");
}

// ---- de quién es el reloj cuando hay varios nodos ----

{
  // Con dos en paralelo manda el que arrancó PRIMERO: es el que dice si
  // esto avanza o está trabado.
  assert.equal(arranqueMasViejo([
    { estado: "corriendo", started_at: "2026-08-24T00:50:00Z" },
    { estado: "corriendo", started_at: "2026-08-24T00:30:00Z" },
  ]), "2026-08-24T00:30:00Z");

  // Una tarea ya cerrada no presta su reloj aunque sea más vieja.
  assert.equal(arranqueMasViejo([
    { estado: "hecho", started_at: "2026-08-24T00:10:00Z" },
    { estado: "corriendo", started_at: "2026-08-24T00:44:35Z" },
  ]), "2026-08-24T00:44:35Z");

  assert.equal(arranqueMasViejo([]), "");
  assert.equal(arranqueMasViejo([{ estado: "pendiente" }]), "");
}

// ---- el conteo de arriba lleva el reloj ----

{
  const orig = Date.now;
  Date.now = () => Date.parse("2026-08-24T01:05:00Z");
  try {
    // El caso real: t4 arrancó 00:44:35 y el panel decía "3/12 hechas"
    // sin nada más durante veinte minutos.
    assert.equal(
      resumenTexto({ total: 12, hechos: 3, corriendo: 1 }, [
        { estado: "hecho", started_at: "2026-08-24T00:23:30Z" },
        { estado: "corriendo", started_at: "2026-08-24T00:44:35Z" },
      ]),
      "3/12 hechas · 1 corriendo · ⏱ 20:25");
  } finally {
    Date.now = orig;
  }
}

// Sin `tasks` sigue andando: el segundo parámetro es opcional y hay
// callers que no lo pasan.
assert.equal(resumenTexto({ total: 5, hechos: 2, corriendo: 1 }),
             "2/5 hechas · 1 corriendo");
assert.equal(resumenTexto({ total: 5, hechos: 2 }), "2/5 hechas");

console.log("chat-grafo: ok");
