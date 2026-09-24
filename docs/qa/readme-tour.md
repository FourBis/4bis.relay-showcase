# Recorrido visual del README

Revisión del 24 de septiembre de 2026. El README se contrastó con código de modelos, ejecución, grafos, indexación, memoria, tareas, permisos, CRM, cuentas, GitHub e interfaz. El [mapa de capacidades](../CAPABILITY_MAP.md) conserva las fuentes.

## Qué muestran las capturas

| Archivo | Procedencia | Qué se comprueba |
|---|---|---|
| `models.png` | Catálogo real, respuestas ficticias | Filas, habilitación y visión triestado. |
| `model-roles.png` | Sección real de Config, respuestas ficticias | Cinco roles, selectores y modelos efectivos. |
| `repository-index.png` | Panel real Indexación, respuestas ficticias | Browse, selección por lote y archivos indexados. |
| `projects.png` | Panel real Proyectos, respuestas ficticias | Proyectos, índice y conexiones. |
| `crm-projects.png` | Panel real CRM, respuestas ficticias | Empresa, oportunidad y proyecto relacionado. |
| `team-access.png` | Captura existente de Equipo con API ficticia | Roles y asignación explícita; reproducible con el check de Equipo. |
| `workspace.png`, `workflow-graph.png`, `workflow-metrics.png` | Capturas de demostración conservadas de la presentación anterior | Ilustran workspace, grafo y consumo con datos ficticios; no son ejecuciones nuevas de proveedores en esta revisión. |

Los paneles nuevos usan el HTML, CSS y módulos JavaScript del producto. El harness añade un rótulo y un contenedor de captura; Config muestra la sección de roles, sin otros apartados. No se genera una maqueta que sustituya la lógica de renderizado. Los valores de modelos, precios, índices, clientes y proyectos son ficticios. No se consultan datos de una instalación operativa.

## Reproducir los paneles nuevos

Con Node, Playwright y Chromium disponibles en tu entorno de pruebas:

```powershell
node docs/qa/product-tour.mjs
node docs/qa/commercial-tour.mjs
```

Los scripts sirven los assets desde un puerto loopback efímero, interceptan la API con fixtures y cierran servidor y navegador al terminar. Bloquean destinos externos, mutaciones y endpoints no previstos. Las capturas se guardan en `docs/images/`; volver a ejecutar los scripts reemplaza esas imágenes de demostración.

Comprobaciones: datos esperados en las tablas/controles, imágenes generadas, ausencia de errores de navegador y ausencia de llamadas inesperadas o escrituras. Los scripts no prueban que CBM indexe, que un modelo responda, que exista una cuenta OAuth o que el CRM esté conectado.

Para comprobar los mismos paneles contra otra copia compatible de Relay:

```powershell
$env:RELAY_UI_ROOT = 'C:\ruta\a\otra\copia\de\relay'
node docs/qa/product-tour.mjs
node docs/qa/commercial-tour.mjs
Remove-Item Env:RELAY_UI_ROOT
```

El destino también recibe sus capturas; usa una copia de trabajo de documentación. Se conservan diferencias propias de cada edición.

## Verificación de esta revisión

- Se revisaron los enlaces relativos a archivos y las anclas del índice de ambos README.
- Se renderizó Markdown y se comprobaron imágenes y diagramas Mermaid en navegador local.
- Se inspeccionaron visualmente las capturas nuevas.
- `product-tour.mjs` pasó contra ambas ediciones: cuatro capturas, cero escrituras, cero solicitudes inesperadas y cero errores de página/consola.
- `commercial-tour.mjs` pasó contra ambas ediciones: detalle de cliente y proyectos, seis lecturas de API simulada, cero solicitudes inesperadas y cero errores de página.
- El render de ambos README públicos y del README local cargó nueve imágenes y cuatro diagramas Mermaid por documento, sin errores de página ni desbordamiento horizontal a 1200 px.
- La inspección y los recorridos usaron la base pública `bf93c17` y la local `ab9b0f6`, más estos cambios de documentación. No se modificó código de ejecución ni configuración operativa.
- En la edición local pasaron además los 12 casos de `test_graph_node_acceptance.py` y `test_graph_budget_resume.py`. Son pruebas focalizadas del comportamiento local ya existente, no una ejecución real de proveedores ni validación de toda la suite.

La evidencia anterior de funciones del sistema está en [VALIDATION.md](../VALIDATION.md). Una presentación correcta, un check sintético y una integración real son comprobaciones distintas.

## Diferencias de edición

La versión local puede incorporar cambios antes que el showcase. En la base inspeccionada para esta revisión, la local ya incluye recuperación adicional de verificación de nodos y reanudación de grafos detenidos por presupuesto; la pública no contiene todos esos cambios. Su README conserva los límites de su propia implementación. Esta revisión de documentación no traslada código del motor, credenciales ni datos operativos entre repositorios.
