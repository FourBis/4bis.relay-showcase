# Mantenimiento local del showcase, 2026-11-01

Candidato preparado desde `develop` público
`f84396f5504c3db2a4d0074de35168b2d04bc817`, en una copia aislada y rama
`codex/showcase-port-20261101`. Este documento describe el código candidato;
no acredita publicación, integración remota ni despliegue.

Se adaptaron correcciones de capacidades que ya estaban en esta edición.
No se copiaron configuraciones, historial Git, capturas, datos operativos ni
documentos internos. Los ejemplos públicos, versiones de dependencias incluidas,
licencias y configuración de instalación se conservan.

## Cambios por tema

| Tema | Comportamiento del candidato | Evidencia de implementación |
|---|---|---|
| Navegación y cuenta | Disposición de ventanas por cuenta; un chat separado permite volver a Mis conversaciones. Un fallo del sondeo dice «Actividad no disponible» sin afirmar que el servidor cayó. | `workspace.js`, `chat-window.js`, `main.js`, `watcher.js` |
| Respuestas tardías | Búsqueda, diffs, proyectos e instaladores comprueban que siguen perteneciendo a la vista y solicitud originales. | `search.js`, `chat-diff.js`, `tab-projects.js`, `tab-mcp.js`, `tab-skills.js`, `api.js` |
| Edición | Los editores de archivos y diagramas fijan su destino. Los cambios de flags conservan borradores por campo; una respuesta tardía de correo no vacía otra redacción. | `tab-projects.js`, `tab-diagrams.js`, `tab-account.js` |
| Estado incierto | Un guardado de archivo sin confirmación conserva contenido y aviso; abrir otra vista no convierte esa operación en un éxito. No equivale a reconciliación entre navegadores o recargas. | `tab-projects.js` |
| Decisiones del chat | Las preguntas pertenecen a su conversación, cuenta y vista visible. Responder no sustituye texto, adjuntos, subidas ni modelos elegidos mientras se esperaba. La lectura de preguntas usa la autoridad existente para controlar la tarea. | `chat-tools.js`, `tab-chats.js`, `server_graph_routes.py` |
| Actividad | Sesiones, ejecuciones y transcripciones acotadas muestran controles de página; un refresco anterior no sustituye al vigente. | `ui-table.js`, `tab-status.js`, `tab-running.js`, `tab-voice.js` |
| Adjuntos | Imágenes y texto se materializan en la vista de adjuntos de la conversación. Sólo los formatos pasivos de imagen admitidos se muestran inline; los demás se descargan con aislamiento del documento. | `attachments.py`, `server_questions.py` |
| Copias de consola | Las salidas de consola y previews aplican redacción al preparar sus copias. Se conservan los originales necesarios para ejecutar y los archivos fuente. La protección depende de patrones reconocidos. | `secret_redaction.py`, `shell.py`, `file_tools.py`, `expert_steps.py`, handlers de estado |
| Historial reciente y objetos | El chat devuelve los turnos más recientes del límite pedido. Un objeto heredado sin huella se vuelve a abrir desde su respuesta para acreditar el origen. | `server_conversation_routes.py`, `chat-objects.js` |
| Equipo y PR externos | Editar un perfil manteniendo rol y selección conserva asignaciones que faltan en el catálogo. Los formularios de registro/evidencia bloquean su edición durante el envío. Los snapshots de PR exigen base y head del mismo repositorio y refrescan la identidad del remote. | `tab-team.js`, `chat-external-pr.js`, `external_pr.py`, `github.py` |
| CRM y autocierre | La sincronización CRM se serializa por instancia. El autocierre revalida que el hilo siga inactivo y sin grafo activo ni pregunta abierta antes de cerrarlo. | `crm.py`, `db_conversations.py`, `server_conversation_jobs.py` |
| Procesamiento de transcripts | El guardado de transcripts es atómico. Al procesar o resumir uno, un recibo en memoria conserva el resultado conocido para reintentar sólo persistencia. No acredita idempotencia ASR, recuperación poskill ni generación de audio. | `voice.py`, `voice_routes.py` |
| Instalación MCP | Un job rechazado por el análisis no puede confirmarse. El análisis limita su propia llamada y no cambia el timeout global del proceso. | `mcp_install_orchestrator.py`, `mcp_install_scan.py` |
| Archivos, SQL y skills | Respeta descendientes protegidos y limita recorridos; los escáneres de repositorios y skills omiten enlaces y las búsquedas de archivos revalidan cada ruta; los diagnósticos SQL ocultan credenciales; publicar una skill conserva respaldo y serializa el destino. | `files.py`, `repo_reader.py`, `dbtool.py`, `sql_tools.py`, `skills_*` |
| Configuración | Cambios de defaults y registro MCP atómicos; comandos actualizados sin requerir recarga del proceso; limpieza de zombis condicionada al estado vigente. | `db_projects.py`, `admin_mcp.py`, `commands.py`, `db_chats.py`, `admin_runs.py` |
| Git y trabajo nocturno | Rutas literales y selección de staging; worktree por corrida nocturna, identidad retenida y comprobación del árbol final. Un gate de cierre incierto detiene publicación. | `git_actions.py`, `night_worker.py`, `night_gate_process.py`, `night_plan.py` |
| Resultado y presupuesto | Conserva snapshots completos antes de etapas posteriores y espera el cierre de IO ante cancelación. Presupuestos acumulados y topes públicos existentes se mantienen. | `expert_result.py`, `expert_runner.py`, `expert_staged_runner.py`, `persist.py`, `finalization.py` |
| Reintentos del plan | Respeta cero y máximos persistidos. Agotados esperan decisión explícita; la admisión provisional cancelada puede devolver su intento sólo bajo condiciones exactas. Retomar y responder revalidan controles antes de relanzar. | `grafo.py`, `db_graph.py`, `orchestrator_core.py`, `server_graph_routes.py` |

## Comprobaciones y límites

La revisión se hizo por lectura de código, comparación de diffs, AST Python,
`node --check`, comprobación de referencias relativas y `git diff --check`.
El CSS se regeneró con el Tailwind 3.4.17 ya disponible y se actualizaron sus
dos sellos. No se instalaron dependencias.

No se ejecutaron unitarias, suites, imports de Relay, aplicación, migraciones,
base de datos, navegador, proveedores ni comandos de ejemplo. No se reinició
ningún proceso. Las revisiones independientes son estáticas: no son aprobación
funcional, comprobación visual ni CI verde. Las cifras y capturas de revisiones
anteriores no validan este candidato.

La demo estática `site/` conserva sus interacciones anteriores. Estas mejoras
del servidor y workspace local no se presentan como acciones nuevas de la demo.

Quedan fuera de esta tanda la generación de audio/vídeo y su consentimiento,
compartición de conversaciones, revisión automática de PR y nuevas operaciones
de despliegue. Tampoco se declara completado el aislamiento de memoria por
origen, la identidad durable del trabajo nocturno, la recuperación íntegra de
envíos inciertos o el cierre durable completo del grafo. Esos grupos requieren
adaptaciones coordinadas y una revisión específica antes de incorporarlos.

«Tarea persistente» conserva su etiqueta actual; su claridad y la UX multimedia
siguen pendientes. No se afirma corregido un permiso de audio o soporte de
referencias visuales de generación que esta edición no implementa.

Antes de publicar este candidato corresponde aprobar su alcance concreto y
revisar la confidencialidad del diff final. La validación manual de ventanas,
editores, preguntas, instalación y recuperación sigue pendiente.
