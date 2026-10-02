# Continuidad y mantenimiento del showcase — 2 de octubre de 2026

Revisión local de la edición pública, sobre `develop` en `b98844b`.
Este registro corresponde al primer cierre en `9ae91ad`; la
[segunda revisión](showcase-review-2026-10-02.md) añade protección de la
limpieza ante cancelaciones simultáneas y peticiones interrumpidas.
Se trabajó en un checkout aislado. No se modificaron protecciones, cuentas,
integraciones externas ni procesos de otras instalaciones. Esta revisión no
incluye publicación, despliegue ni integración de ramas.

## Inventario comprobado

| Pendiente | Estado y evidencia | Acción |
|---|---|---|
| Cancelar grafos antiguos sin conversación | Vigente en la base: `get_conversation_task(None)` devolvía error y dos pruebas existentes fallaban. | Se comprueba el permiso de escritura del proyecto antes de cancelar; Owner también puede cancelar grafos históricos sin proyecto. Nueve regresiones cubren repetición, permisos, tarea en ejecución y conservación de resultados. |
| Orden de “Cerrados recientemente” | Ya corregido en la base: usa `closedAt` persistido y restaura el contador máximo. | Se amplió el recorrido de navegador con cierres en orden distinto de apertura, reapertura y F5. |
| Aviso del botón de proyectos ocultos ausente | Ya corregido en la base: el handler antiguo no se inicializa. | Se comprueba la ausencia de ese aviso en el recorrido de navegador. |
| Reanudar un grafo con tareas detenidas | [PR #9](https://github.com/FourBis/4bis.relay-showcase/pull/9) abierto, cabeza `bf215a9`, con checks de PR correctos al revisar. | No se duplicó ni integró su cambio. La decisión de integración corresponde al propietario. |
| Proyectos tomados por el watcher CBM al arrancar | Limitación deliberada, descrita por `cbm_watcher.py` y el mapa de capacidades. | Se conserva. Cambiar la actualización de proyectos requiere discutir el diseño. |
| Entrega del digest CRM | El contrato externo documentado rechaza `crm-digest:`. `notify._acepto` detecta la respuesta descartada; no define un catálogo de prefijos. | Se precisó el mapa de capacidades. Resolver la entrega requiere revisar el contrato del bot externo. |

No había issues abiertos al consultar el repositorio. El único PR abierto era
#9; los pendientes históricos no se trataron como bugs nuevos sin comprobarlos.

## Cambios y alcance

`POST /graphs/{id}/cancel` conserva la autorización de conversaciones
vinculadas. Si falta `conversation_id`, comprueba Owner o permiso de escritura
del proyecto. La denegación ocurre antes de detener el worker o cambiar el
estado. Cancelar dos veces no relanza tareas ni borra nodos y resultados. No
se cambió la reanudación de grafos ni su arquitectura.

Las pruebas antiguas de Git y Night Runs ahora simulan la cuenta personal
requerida por el contrato actual. Usan repositorios y remotos locales temporales,
sin credenciales reales ni publicación en GitHub. La cobertura independiente
de bloqueo cuando falta una cuenta sigue en `test_github_actor.py`.

Se corrigieron las expectativas antiguas de protección del último Owner y
del mensaje de escritura de una tarea `read_only`. El entorno de pruebas
desactiva también el contexto CBM de Night Runs, además del warmup que ya
estaba aislado. Las pruebas específicas pueden proporcionar sus propios fakes.
El workflow incorpora regresiones de interrupción de grafos y contratos
históricos, incluida limpieza de procesos hijos en Windows.

La prueba de rutas Windows en bash también usa un repositorio Git temporal
con un archivo rastreado. Antes consultaba el checkout de la suite: al ejecutar
fuera del sandbox, Git rechazaba su propietario distinto. La fixture nueva
comprueba la conversión de la ruta y la salida real de Git sin modificar
`safe.directory`, propietarios ni permisos. Pasa en ambos contextos.

## Validación local

Se usaron Python 3.12.0, Node 24.21.0, Git 2.52.0 para Windows, Chrome y
dependencias ya instalados, con las fuentes de este checkout público y modelos
reales desactivados. No se instaló software ni se conectaron cuentas. Los datos
nuevos de prueba son ficticios. El workflow remoto usa Python 3.12 y Node 22;
su ejecución sobre estos commits queda pendiente de la publicación autorizada.

- Suite general del primer cierre (`9ae91ad`): **2453 passed, 11 skipped y 18 subtests passed**,
  767,18 s, sin fallos ni errores de limpieza. Dos avisos
  `PytestAssertRewriteWarning` corresponden a dependencias importadas antes
  de iniciar pytest mediante el runner local. Los casos omitidos no se
  presentan como comprobados por esta ejecución.
- Checks enfocados originales, antes de la ampliación: **437 passed y
  6 subtests passed**, 191,04 s.
- Comandos exactos del workflow: subdivisión **10 passed, 98 deselected**,
  2,09 s; interrupción y repetición de grafos **47 passed**, 64,81 s;
  contratos y limpieza, incluida la fixture final de rutas Windows,
  **137 passed**, 83,53 s.
- Cancelación y permisos: **9 passed**, 1,53 s. Antes de la corrección, cuatro
  de las ocho regresiones iniciales fallaban.
- Acciones Git y contrato de actor: **36 passed**, 51,31 s.
- Worker de ramas Night Runs: **11 passed**, 10,93 s.
- Workspace en Chrome local: **1 passed**, 20,30 s. Incluye los casos de cierre,
  reapertura, recarga y aviso ausente, además del recorrido existente.
- Demo pública ES/EN: **PASS**, 44 peticiones, sin errores. Siete capacidades,
  ocho controles de captura, teclado, imágenes locales y anchos de 320, 390,
  768 y 1440 px. Se ejecutó `public-demo.mjs` con el Playwright instalado y
  Chrome local; no se usó túnel ni Chrome bridge.
- Cancelación externa del shell y proceso en background: **2 passed**,
  10,78 s, con autorización del entorno para finalizar sus propios procesos
  temporales. Dentro del sandbox, `taskkill` devolvía “Acceso denegado”; esa
  restricción no se corrigió alterando permisos ni código de producción.

Los conjuntos anteriores se superponen y no deben sumarse. La primera suite
general dejó **2420 passed, 31 failed y 12 skipped**, con un error de limpieza
temporal. Permitió localizar las fixtures desactualizadas descritas arriba;
el fallo de cancelación del shell se reprodujo como restricción del entorno.
Una segunda ejecución terminó con **2452 passed, 1 failed, 11 skipped y
18 subtests passed**, 753,61 s. El único fallo era el rechazo de propietario
del checkout en la prueba de rutas Windows, reproducido por separado y resuelto
con la fixture temporal. La prueba corregida pasó dentro del sandbox (**1
passed**, 0,74 s) y bajo el usuario externo (**1 passed**, 2,50 s).
La repetición final descrita arriba pasó con todas las correcciones de fixtures.

Para reproducir la suite, desde la raíz y con las dependencias de desarrollo
instaladas en el entorno elegido:

```powershell
python -m pytest mcp-server/tests -q
$env:RELAY_TEST_UI = "1"
python -m pytest mcp-server/tests/test_workspace_browser.py -q
Remove-Item Env:RELAY_TEST_UI
node docs/qa/public-demo.mjs
```

El recorrido opt-in requiere Chrome y Playwright disponibles. Los comandos
enfocados de CI están completos en `.github/workflows/checks.yml`.

Las capturas y logs locales quedan como evidencia de la revisión; no se
exportan logs del entorno al repositorio público. No se volvió a ejecutar
Gitleaks: no estaba disponible en esta sesión. La revisión del diff no añade
identidades operativas, credenciales ni fuentes privadas.

## Siguiente paso

Revisar los commits locales y decidir con el propietario la publicación de
la rama y su PR. Revisar #9 por separado. Los cambios de arquitectura del
watcher y del contrato externo CRM quedan pendientes de discusión.
