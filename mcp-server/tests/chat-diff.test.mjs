// Test del parser de diff de chat-diff.js.  node --test lo levanta solo
// (test_js_suite.py corre todos los *.test.mjs de esta carpeta).
//
// Mismo truco que chat-tools.test.mjs: el módulo se carga por data: URL
// neutralizando los imports, porque api.js/ui.js tocan el DOM. Lo que se
// prueba es lo que NO toca el DOM — que es justo la parte con lógica:
// convertir un diff unificado en líneas con número de los dos lados.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/chat-diff.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "")
  .replace(/^/, "const escape = (s) => String(s == null ? '' : s);\n"
    + "const apiRoot = () => {}; const toast = () => {};\n"
    + "const modalLoad = () => {}; const confirmModal = () => {};\n");

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(src).toString("base64"));
const { parseUnifiedDiff, tramoCambiado } = mod;

// ---- numeración de línea de los dos lados ----
// Es lo que el visor viejo no tenía y por lo que un diff no se podía leer
// contra el archivo real.

const SIMPLE = `diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,7 @@ def main():
     uno
     dos
-    viejo
+    nuevo
+    agregado
     tres
     cuatro`;

{
  const [f] = parseUnifiedDiff(SIMPLE);
  assert.equal(f.path, "src/app.py");
  assert.equal(f.oldPath, "src/app.py");
  assert.equal(f.hunks.length, 1);
  const h = f.hunks[0];
  assert.equal(h.oldStart, 10);
  assert.equal(h.newStart, 10);
  assert.equal(h.seccion, "def main():");

  const l = h.lines;
  assert.deepEqual(l.map((x) => x.type), [" ", " ", "-", "+", "+", " ", " "]);
  // contexto: avanzan los dos lados
  assert.deepEqual([l[0].oldNo, l[0].newNo], [10, 10]);
  assert.deepEqual([l[1].oldNo, l[1].newNo], [11, 11]);
  // borrada: solo lado viejo
  assert.deepEqual([l[2].oldNo, l[2].newNo], [12, null]);
  // agregadas: solo lado nuevo, consecutivas
  assert.deepEqual([l[3].oldNo, l[3].newNo], [null, 12]);
  assert.deepEqual([l[4].oldNo, l[4].newNo], [null, 13]);
  // y el contexto de después sigue alineado en los dos lados
  assert.deepEqual([l[5].oldNo, l[5].newNo], [13, 14]);
  assert.equal(l[3].text, "    nuevo");
}

// ---- varios archivos y varios hunks en un solo texto ----

{
  const dos = SIMPLE + "\n" + `diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # demo
+línea
@@ -10,2 +11,2 @@
-a
+b`;
  const files = parseUnifiedDiff(dos);
  assert.equal(files.length, 2);
  assert.equal(files[1].path, "README.md");
  assert.equal(files[1].hunks.length, 2);
  assert.equal(files[1].hunks[1].oldStart, 10);
  assert.equal(files[1].hunks[1].newStart, 11);
}

// ---- alta, baja y rename: el estado sale de la cabecera ----

{
  const alta = `diff --git a/nuevo.txt b/nuevo.txt
new file mode 100644
--- /dev/null
+++ b/nuevo.txt
@@ -0,0 +1,2 @@
+una
+dos`;
  const [f] = parseUnifiedDiff(alta);
  assert.equal(f.status, "A");
  assert.equal(f.path, "nuevo.txt");
  assert.deepEqual(f.hunks[0].lines.map((l) => l.newNo), [1, 2]);
  assert.deepEqual(f.hunks[0].lines.map((l) => l.oldNo), [null, null]);
}

{
  const baja = `diff --git a/ida.txt b/ida.txt
deleted file mode 100644
--- a/ida.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-una
-dos`;
  const [f] = parseUnifiedDiff(baja);
  assert.equal(f.status, "D");
  // el path del borrado sale del lado viejo: `+++ /dev/null` no lo tiene
  assert.equal(f.oldPath, "ida.txt");
}

{
  const rename = `diff --git a/viejo.py b/nuevo.py
similarity index 92%
rename from viejo.py
rename to nuevo.py
--- a/viejo.py
+++ b/nuevo.py
@@ -1 +1 @@
-print(1)
+print(2)`;
  const [f] = parseUnifiedDiff(rename);
  assert.equal(f.status, "R");
  assert.equal(f.oldPath, "viejo.py");
  assert.equal(f.path, "nuevo.py");
}

// ---- binario: se marca, no se inventa contenido ----

{
  const [f] = parseUnifiedDiff(`diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ`);
  assert.equal(f.binary, true);
  assert.equal(f.hunks.length, 0);
}

// ---- "\\ No newline at end of file" no rompe la numeración ----

{
  const sinNl = `diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 uno
-dos
\\ No newline at end of file
+dos y algo
\\ No newline at end of file`;
  const [f] = parseUnifiedDiff(sinNl);
  const l = f.hunks[0].lines;
  assert.deepEqual(l.map((x) => x.type), [" ", "-", "\\", "+", "\\"]);
  // la marca no consume número de línea de ningún lado
  assert.deepEqual([l[2].oldNo, l[2].newNo], [null, null]);
  assert.equal(l[3].newNo, 2);
}

// ---- líneas de contexto vacías (la del diff es un espacio pelado) ----

{
  const conVacia = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
    + "@@ -1,3 +1,3 @@\n uno\n\n-dos\n+DOS\n";
  const [f] = parseUnifiedDiff(conVacia);
  const l = f.hunks[0].lines;
  // La línea vacía se cuenta como contexto: si se descarta, todo lo que
  // sigue queda desfasado un número respecto del archivo real.
  assert.deepEqual(l.map((x) => x.type), [" ", " ", "-", "+"]);
  assert.deepEqual([l[2].oldNo, l[2].newNo], [3, null]);
}

// ---- texto vacío / basura: devuelve lista vacía, no explota ----

for (const basura of ["", null, undefined, "no soy un diff\n"]) {
  const r = parseUnifiedDiff(basura);
  assert.ok(Array.isArray(r), `${basura}: array`);
  if (basura === "no soy un diff\n") {
    // Sin cabecera igual arma un archivo, pero sin hunks: nada que pintar.
    assert.equal(r[0].hunks.length, 0);
  } else {
    assert.equal(r.length, 0);
  }
}

// ---- resalte intra-línea: prefijo y sufijo comunes ----

{
  // "const a = " es prefijo común (10) y "o(1);" sufijo común (5): el
  // tramo marcado es "viej"/"nuev", no la palabra entera. La `o` final
  // queda afuera porque de verdad no cambió.
  assert.deepEqual(tramoCambiado("const a = viejo(1);", "const a = nuevo(1);"),
                   [10, 14, 10, 14]);
  // sin nada en común: marca la línea entera de los dos lados
  assert.deepEqual(tramoCambiado("abc", "xyz"), [0, 3, 0, 3]);
  // idénticas: tramo vacío (el caller no marca nada)
  const [i, f] = tramoCambiado("igual", "igual");
  assert.equal(f <= i, true);
  // uno es prefijo del otro
  assert.deepEqual(tramoCambiado("foo", "foobar"), [3, 3, 3, 6]);
}

console.log("chat-diff: ok");
