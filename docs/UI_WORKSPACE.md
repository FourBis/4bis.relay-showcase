# Relay: espacio de trabajo

Rediseño del frontend, 16 de septiembre de 2026. Se reutilizan los endpoints,
los datos y las acciones existentes. No requiere migraciones ni dependencias.

## Información y tareas

La navegación anterior dividía veinte módulos en páginas excluyentes. El usuario
necesita mantener la conversación mientras consulta datos, inspecciona una ejecución
o configura una herramienta. Las unidades de navegación pasan a ser herramientas
abiertas en un espacio compartido.

| Necesidad | Información y acciones existentes | Acceso nuevo |
| --- | --- | --- |
| Conversar | Hilos, respuestas, herramientas, memoria, planes y diffs | Chat como entrada; historial contextual |
| Trabajar | Proyectos, tareas, clientes y diagramas | Herramientas que pueden verse junto al chat |
| Observar | Ejecuciones, salud, consumo, métricas, nocturnos, zombies y logs | Ventanas simultáneas; indicadores compactos globales |
| Preparar | Skills, voz, índices y repositorios huérfanos | Catálogo de herramientas buscable |
| Configurar | Comandos, MCPs, modelos y preferencias | Formularios existentes; configuración por secciones desplegables |
| Reutilizar un resultado | Respuestas, tablas y SVG de la conversación | Objetos independientes con origen explícito |

## Arquitectura de interacción

- Una barra compacta para abrir herramientas y buscar; desaparece el menú lateral
  permanente. `Ctrl+Mayús+K` abre herramientas; `Ctrl+K` conserva búsqueda global.
- Cada herramienta tiene una única instancia. Abrirla otra vez la trae al frente;
  no duplica formularios, listeners ni requests de guardado.
- Mover por la cabecera, redimensionar por la esquina, expandir, minimizar o cerrar.
  La bandeja inferior permite recuperar ventanas minimizadas; el catálogo muestra
  las cerradas recientemente. El botón de mosaico las ordena.
- La cabecera es accesible con teclado: flechas mueven, Mayús+flechas redimensionan,
  Enter expande/restaura. Los botones ofrecen alternativas al arrastre.
- El chat mantiene el contenido y el compositor. La lista de conversaciones aparece
  bajo demanda. `/abrir proyectos` abre Proyectos localmente, sin enviar al modelo.
- Chat y grafo comparten el ancho disponible sin superponerse. Ampliar el grafo
  conserva al menos 320 px para la conversación; en ventanas de hasta 700 px
  se muestra uno a la vez y cerrar el plan recupera el chat.
- «Abrir en workspace» conserva una respuesta fuera del hilo. Desde ella se pueden
  separar tablas y gráficos SVG. Las tablas permiten filtrar filas y copiar TSV;
  las respuestas se copian como texto. «Ir a la conversación» recupera el origen.
- En móvil se muestra una ventana a la vez y la bandeja funciona como selector.
  La disposición de escritorio queda guardada y se recupera al ampliar la pantalla.

## Presentación

Superficies de trabajo sobrias, títulos con jerarquía, tablas sin tarjetas anidadas,
formularios agrupados y controles secundarios contextuales. El contenido ocupa el
espacio principal. No hay una nueva grilla de tarjetas de navegación.

## Persistencia y límites explícitos

`4bis.workspace.v1` en localStorage guarda geometría, estados e identificadores;
no guarda el texto de las respuestas ni los datos de las tablas. Los objetos se
recuperan desde la conversación y comprueban la huella de la respuesta antes de
mostrarla. Si el origen cambia o desaparece, muestran un error con recuperación.

La disposición pertenece a este navegador. El workspace usa el área visible, no
un canvas infinito. Las herramientas mantienen una instancia; las confirmaciones
destructivas siguen siendo modales. Los objetos de chat son respuestas existentes,
no formularios o código ejecutable generados arbitrariamente por un modelo.

Los refrescos registrados de módulos se ejecutan mientras su ventana esté visible
y se pausan al minimizarla/cerrarla. El seguimiento de una conversación en ejecución
continúa para conservar sus eventos. Ocultar una ventana no cancela trabajos del servidor.
El CSS y JS se sirven en vivo por el relay: cambios de frontend no requieren reinicio.

## Verificación runnable

```powershell
pwsh -NoProfile -File mcp-server/admin_static/build-css.ps1
mcp-server/.venv/Scripts/python.exe -m pytest mcp-server/tests/test_js_suite.py mcp-server/tests/test_admin_js_sintaxis.py mcp-server/tests/test_admin_css_build.py -q
$env:RELAY_TEST_UI = '1'
mcp-server/.venv/Scripts/python.exe -m pytest mcp-server/tests/test_workspace_browser.py mcp-server/tests/test_admin_browser_real.py -q
```

Las pruebas de navegador usan Chrome y una base temporal; no envían conversaciones
a proveedores ni modifican los datos reales. Cubren el flujo de ventanas, objetos,
recuperación, foco y tamaños de pantalla.

Resultados y límites de esta entrega: [Validación](VALIDATION.md).
