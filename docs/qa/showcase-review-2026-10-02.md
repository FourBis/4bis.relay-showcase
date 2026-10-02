# Segunda revisión del diff — 2 de octubre de 2026

Se revisó `b98844b..9ae91ad` y se comprobaron sus cambios de permisos,
cancelación, persistencia, fixtures y CI contra el código público. Esta
revisión produjo una corrección adicional; `9ae91ad` por sí solo no es el
estado propuesto para publicar. No se integró ni publicó código.

## Hallazgo corregido

Dos peticiones de cancelación simultáneas podían llamar dos veces a
`Task.cancel()`: la segunda interrumpía el `finally` del worker mientras
limpiaba recursos. Interrumpir la petición que esperaba ese worker también
propagaba una nueva cancelación a su limpieza. Ambas regresiones fallaron
antes de la corrección.

La corrección local `b21bb93` pide la cancelación sólo si no hay una pendiente y protege
la espera con `asyncio.shield`. Los reintentos esperan la limpieza existente;
la interrupción de la petición no vuelve a cancelar al worker. Se conserva
el tratamiento anterior de las respuestas y no se cambia el orquestador.

Las pruebas comprueban que el worker termina su limpieza, las peticiones
repetidas responden 200, no se relanza trabajo y los resultados de nodos
terminados permanecen intactos. La autorización se evalúa antes de cancelar
o mutar el estado. Los checks adicionales abarcan cuentas deshabilitadas,
Finanzas, usuario no registrado, asignación revocada, proyecto deshabilitado
y proyecto `read_only`; Subadmin asignado conserva su acceso permitido.

En conversaciones vinculadas, la regla preexistente de cancelación de tareas
`read_only` se conserva. La revisión no introduce ni amplía esa regla; cambiar
la política de acceso a consultas requiere una decisión separada.

## Fixtures y CI

- La cuenta GitHub ficticia tiene alcance de módulo/test. No modifica la
  autorización de producción ni la prueba separada de ausencia de actor y
  bloqueo del fallback a credenciales de máquina.
- El stub de `gh` evita llamadas externas. Se añadieron assertions de que no
  se invoca antes de validar método, rama y commits. Un recibo ficticio de PR
  a `main` o `master` se rechaza tras consultar sus datos, sin invocar merge.
  No se valida aquí una integración real con GitHub.
- Night Runs sigue probando commits, gates y rollback sobre Git temporal.
  Desactivar el binario CBM evita tocar índices de una instalación real. Dos
  pruebas adicionales comprueban que no se llama sin binario y que un fake
  explícito ejercita búsqueda, timeout, truncado y descarte de errores.
- La protección del último Owner ahora se afirma antes de crear el Owner de
  respaldo. La escritura `read_only` afirma tanto el error como la ausencia
  del archivo. Ninguna de esas correcciones elimina controles.
- El workflow mantiene sus checks originales y añade conjuntos. No se
  añadieron `skip`, `xfail` ni `continue-on-error` para esconder fallos.

## Las once omisiones

Se reconstruyeron los once node IDs de la suite registrada y se ejecutaron
de nuevo con `-rs`: **11 skipped**, 3,08 s. Son condiciones que ya existían
en la base, no omisiones añadidas por este diff.

| Prueba, bajo `mcp-server/tests/` | Causa comprobada | Comprobación separada |
|---|---|---|
| `test_admin_browser_real.py::test_chat_states_and_images_in_real_browser` | `RELAY_TEST_UI` no es `1` en la suite general. | Pasó en el conjunto opt-in de cuatro pruebas. |
| `test_browser_real.py::test_browser_capture_reaches_model_and_user` | `RELAY_TEST_PLAYWRIGHT_MCP` no está configurado. Requiere un CLI MCP instalado y Chrome. | No se instaló ni habilitó el CLI para esta revisión. |
| `test_relay_api_smoke.py::test_health_endpoint_responds_2xx` | La conexión al Relay de `127.0.0.1:8413` fue rechazada. | No se levantó una instalación para evitar tocar su estado. |
| `test_shell_y_preguntas.py::test_stdin_cerrado_no_cuelga` | Usa comandos POSIX y la plataforma es Windows. | Pendiente de ejecución en POSIX. |
| `test_shell_y_preguntas.py::test_exit_code_va_al_final_de_la_salida` | Usa comandos POSIX y la plataforma es Windows. | Pendiente de ejecución en POSIX. |
| `test_shell_y_preguntas.py::test_timeout_corta_y_lo_dice` | Usa comandos POSIX y la plataforma es Windows. | Pendiente de ejecución en POSIX. |
| `test_shell_y_preguntas.py::test_salida_gigante_no_deadlockea` | Usa comandos POSIX y la plataforma es Windows. | Pendiente de ejecución en POSIX. |
| `test_shell_y_preguntas.py::test_corre_en_el_cwd_pedido` | Usa comandos POSIX y la plataforma es Windows. | Pendiente de ejecución en POSIX. |
| `test_vendor_browser.py::test_diagrams_and_html_sanitizer` | `RELAY_TEST_UI` no es `1`. El motivo abreviado dice “requiere Chrome”; no demuestra que falte Chrome. | Pasó en el conjunto opt-in. |
| `test_workflow_browser.py::test_workflow_metrics_and_replaced_parent_in_real_browser` | `RELAY_TEST_UI` no es `1`. | Pasó en el conjunto opt-in. |
| `test_workspace_browser.py::test_workspace_modules_windows_objects_and_responsive` | `RELAY_TEST_UI` no es `1`. | Pasó en el conjunto opt-in, además de la revisión anterior. |

El conjunto opt-in de esos cuatro recorridos UI terminó con **4 passed**,
31,70 s, usando Chrome headless local, datos temporales y proveedores simulados.
No se reanudaron túneles ni Chrome bridge. Las cuatro omisiones siguen contando
en el resultado de la suite general; la ejecución separada es evidencia distinta.

## Secretos y material privado

Se revisaron los archivos modificados y todas las líneas añadidas desde
`b98844b`, con Git, Python y revisión manual. El escaneo comprueba formatos de
tokens GitHub/AWS/Google/proveedores, claves privadas, JWT, URLs con contraseña,
literales de alta entropía, correos operativos, rutas de usuario y direcciones
internas. No encontró coincidencias. Los tokens, cuentas, URLs de recibos y
resultados nuevos son ficticios; no se añadieron archivos binarios ni logs del
entorno al repositorio.

Gitleaks y TruffleHog no estaban disponibles en PATH; no se instalaron. Este
escaneo acotado al diff no sustituye una auditoría especializada de todo el
historial ni certifica ausencia de secretos. Los resultados anteriores de
Gitleaks documentados para la base no se presentan como un escaneo nuevo.

## Validación y entrega

- Cancelación, permisos y reintentos: **26 passed**, 3,32 s.
- Grafos, orquestador, permisos y continuidad: **197 passed**, 106,45 s,
  antes de añadir todos los casos de acceso y de interrupción de petición.
- Fixtures Git/Night, ausencia de actor, último Owner y archivos:
  **135 passed**, 62,27 s, antes de los dos checks adicionales de contexto CBM.
- Worker Night y contextos CBM: **13 passed**, 11,37 s.
- Comandos exactos del workflow tras los cambios: subdivisión **10 passed,
  98 deselected**; grafos **64 passed**, 73,09 s; contratos y limpieza
  **140 passed**, 75,65 s. No se añadió una exclusión de casos.

La suite general sobre la corrección `b21bb93` terminó con **2473 passed,
11 skipped y 18 subtests passed**, 737,58 s, sin fallos ni errores de limpieza.
Se ejecutó con `-rs` y confirmó las once causas de omisión de la tabla.
Hay dos `PytestAssertRewriteWarning` del runner local por dependencias cargadas
antes de pytest. Los conjuntos anteriores se superponen y no deben sumarse.

Tras la corrección y esta validación, no quedan hallazgos materiales abiertos
en el diff revisado. La propuesta queda lista para aprobación de publicación.
La validación remota con Node 22 sigue pendiente de publicar la rama autorizada;
las comprobaciones locales usaron Node 24 y Python 3.12 ya instalados.

Watcher, digest externo, protecciones y PR #9 permanecen fuera de esta corrección.
La publicación de la rama y su PR queda pendiente de aprobación del propietario.
