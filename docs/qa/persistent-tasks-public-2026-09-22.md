# Tareas persistentes y varias conversaciones: evidencia pública

Validación local del 22 de septiembre de 2026. Los repositorios, conversaciones,
remotos Git y respuestas del proveedor usados por las pruebas son temporales o
simulados. No se incluye información de instalaciones reales.

## Comprobaciones reproducibles

Con las dependencias de desarrollo instaladas, desde la raíz del repositorio:

```powershell
python -m pytest mcp-server/tests/test_task_persistence.py `
  mcp-server/tests/test_task_workspace.py mcp-server/tests/test_task_pr.py `
  mcp-server/tests/test_task_controls.py mcp-server/tests/test_task_continuity.py `
  mcp-server/tests/test_task_callers.py mcp-server/tests/test_task_index.py `
  mcp-server/tests/test_project_privacy.py mcp-server/tests/test_module_boundaries.py -q
```

Estas pruebas comprueban identidad durable, cola e idempotencia, recuperación de
eventos inciertos, worktrees aislados, protección de ramas, evidencia ligada al
commit, límites de seguimiento, permisos de publicación y privacidad de los
resúmenes accesibles a miembros. Los comentarios de PR se tratan como datos.

Los recorridos visuales requieren Node.js, Playwright y Chrome disponibles:

```powershell
node docs/qa/multi-chat-visual.mjs
node docs/qa/task-panel-visual.mjs
```

Ambos recorridos se ejecutaron correctamente. El primero comprueba dos chats
visibles, borradores independientes, recuperación por identidad, renombrado con
el modal del workspace, nombres largos sin desbordamiento y navegación móvil a
390 px. No hubo errores JavaScript ni peticiones destructivas. El segundo
comprueba controles y foco por teclado, estados y errores visibles, seguimiento
opcional y presentación móvil a 360 px. Sus acciones se resuelven en una API
simulada y no crean PR ni ejecuciones reales.

Las capturas se guardan por defecto en carpetas temporales. Se puede elegir otro
destino mediante `MULTI_CHAT_VISUAL_OUT` o `TASK_PANEL_VISUAL_OUT`. La captura de
varios chats incluida en el README procede del mismo escenario sintético.

## Límites de la evidencia

Los resultados de la suite y del control de contenido exportable se registran en
[VALIDATION.md](../VALIDATION.md). Las pruebas locales no demuestran despliegue,
aprobación humana ni un ciclo completo con proveedores o repositorios reales.
La publicación de una PR requiere autorización de la tarea; merge y despliegue
siguen fuera de esa autorización. Bash-it y Open SWE se usan como referencias de
diseño, sin copiar código ni añadir dependencias.
