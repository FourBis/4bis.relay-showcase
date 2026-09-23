# Relay: decisiones de ingeniería detrás de un workspace local

*Relay reúne conversaciones, tareas y herramientas para trabajar sobre varios repositorios. La [demo estática](https://fourbis.github.io/4bis.relay-showcase/) ilustra la interfaz con datos ficticios; no ejecuta modelos ni se conecta a una instalación.*

## El problema de interacción

Una conversación de desarrollo rara vez termina en el chat: también hay que consultar archivos, seguir una tarea, mirar un grafo y revisar resultados. El workspace de Relay parte de esa continuidad. Las herramientas se abren como ventanas dentro del mismo espacio, de modo que el chat puede seguir visible mientras se inspecciona otra parte del trabajo. Cada herramienta tiene una instancia; las ventanas se pueden mover, redimensionar, minimizar y recuperar. En pantallas estrechas se muestra una ventana a la vez. Los controles de teclado complementan el arrastre.

La decisión reutiliza la API y los módulos existentes. No requirió migraciones de datos ni dependencias para reorganizar la interfaz. La disposición pertenece al navegador: `localStorage` conserva geometría y referencias, mientras que conversaciones y resultados se recuperan desde el servidor. Así, cerrar una ventana no significa borrar una tarea. Los borradores sin enviar no sobreviven a una recarga.

## Continuidad y evidencia

Para tareas de escritura, Relay puede asociar una conversación con un worktree y mantenerlo entre mensajes. La validación se relaciona con el commit probado; si cambia el contenido o el `HEAD`, esa evidencia queda obsoleta. La cola de eventos y sus recibos viven en SQLite para conservar el orden y permitir reconciliar una interrupción antes de repetir un efecto. El seguimiento de una PR es opcional y tiene límites de tiempo, iteraciones y consumo.

Estas decisiones reducen confusiones de estado, pero no convierten el worktree en un sandbox. Comparte Git, procesos, puertos y credenciales del equipo. El servicio está pensado para uso local de un propietario; el repositorio no acredita aislamiento multiusuario. Del mismo modo, una prueba con proveedor simulado no demuestra una integración real.

## Costes y límites

Relay usa Python, una API HTTP local y SQLite, y sirve el workspace web desde el mismo proceso. Esto evita introducir un servicio de datos separado para una instalación local, pero deja responsabilidades operativas en un único proceso. Los proveedores de modelos y servidores MCP son opcionales; al activarlos, el contenido incluido en una ejecución puede salir de la máquina. La instalación documentada y comprobada es Windows con PowerShell. No se afirma compatibilidad validada con otros sistemas.

El código se publica bajo MIT y conserva por separado los avisos de dependencias de terceros. La evidencia actual incluye pruebas con repositorios temporales, datos ficticios y proveedores simulados; tampoco acredita despliegue. Las limitaciones completas y los comandos de reproducción están en [Validación](VALIDATION.md), [Seguridad](../SECURITY.md), [Arquitectura](ARCHITECTURE.md), [Workspace](UI_WORKSPACE.md) y [Tareas persistentes](PERSISTENT_TASKS.md).

Relay busca hacer visible el estado del trabajo y mantener juntos contexto y herramientas. Su alcance actual es deliberadamente local y experimental: antes de usar repositorios o credenciales reales, hay que revisar las fronteras de seguridad y configurar cada integración con conocimiento de sus efectos.

---

**English introduction:** Relay is an experimental local workspace for keeping repository conversations, tasks, and tools in view together. Its static demo uses fictional data and does not run AI or connect to a Relay instance. The engineering choices, evidence, and limits described above are documented in the linked architecture, workspace, persistent-task, validation, and security guides.
