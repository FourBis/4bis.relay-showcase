# FourBis Relay

**Español** · [English](README.en.md)

**Trabajo largo, con un plan que puede cambiar mientras avanza.** FourBis Relay organiza pedidos de desarrollo en tareas con dependencias, puede subdividir una tarea durante la ejecución y mantiene conversación, rama, workspace y cambios asociados al trabajo.

**Explora la [demo estática](https://fourbis.github.io/4bis.relay-showcase/)**: una interfaz simulada con datos ficticios. No se conecta a Relay, no llama a un proveedor de IA ni ejecuta herramientas. Relay es software experimental para uso local.

Proyecto de portafolio de FourBis y Jeremías Badilla, publicado bajo la licencia MIT. Consulta [LICENSE](LICENSE) para la licencia del proyecto y [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) para los avisos de terceros.

## Qué puedes explorar

- **Un grafo que se adapta durante la ejecución:** al agotar el presupuesto de una tarea con trabajo pendiente, Relay puede dividirla en subtareas y actualizar sus dependientes, conservando el avance reportado.
- **Equipo y permisos por proyecto:** Admin, Subadmin, Dev y Finanzas con alcances explícitos; asignación de proyectos separada de la conexión personal de GitHub y de habilitar escritura en una tarea.
- **Continuidad y revisión:** consultar el diff desde la conversación, conservar el worktree entre turnos y continuar sobre la misma tarea y PR.
- Conversaciones persistentes y contexto de proyecto con búsqueda.
- Validación asociada al commit probado, con seguimiento opcional de feedback en la misma PR.
- Ejecuciones opcionales de expertos organizadas en etapas de planificación, ejecución, verificación y documentación.
- Un workspace web donde se pueden mantener a la vista chats, grafos de tareas, tablas y otras herramientas.
- Herramientas locales y servidores MCP opcionales para archivos de repositorios, shell, SQL, navegador y flujos de GitHub.

Un caso observado el 24 de septiembre de 2026: un grafo de diez tareas incorporó cuatro subtareas para una de ellas y siguió ejecutándose. El trabajo completo seguía en curso. Lee [el recorrido y sus límites](docs/LONG_RUNNING_WORK.md): la subdivisión es acotada, no recursiva sin límite. La demo recrea el comportamiento con datos ficticios.

Tres ejemplos concretos:

1. **Revisar un cambio de repositorio:** pedir a un experto que inspeccione un proyecto, mantener la tarea en su worktree y revisar la validación asociada al commit.
2. **Seguir una migración extensa:** mantener la conversación junto al grafo, ver nuevas subtareas durante la ejecución y revisar dependencias, avance y resultados.
3. **Comparar trabajo de proyectos:** abrir conversaciones separadas, ordenarlas en pantalla y conservar cada borrador y contexto de proyecto por separado.

Estos ejemplos describen flujos disponibles; no implican que haya proveedores configurados, servicios externos validados ni preparación para producción. Consulta la [guía del workspace](docs/UI_WORKSPACE.md) y la guía de [tareas persistentes](docs/PERSISTENT_TASKS.md) para conocer su comportamiento y límites.

## Presentación

![Workspace de FourBis Relay](docs/images/workspace.png)

Vista del workspace local.

![Conversaciones abiertas en ventanas separadas](docs/images/workspace-chats.png)

Conversaciones simultáneas con borradores independientes.

![Grafo de una tarea](docs/images/workflow-graph.png)

Dependencias y avance de una tarea.

![Equipo: proyectos y permisos explícitos](docs/images/team-access.png)

Equipo y asignación de proyectos, con identidades ficticias.

*Las capturas muestran una demostración con datos ficticios. No representan actividad de proveedores externos ni una instalación pública.*

## Inicio rápido en Windows

Requisitos: Python 3.11 o posterior y PowerShell. Windows es el único destino de instalación validado; la portabilidad a otros sistemas no está establecida.

```powershell
cd C:\ruta\al\repositorio\mcp-server
python -m venv .venv
& ".\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
pip install -e .
.\start.ps1
```

En otra ventana de PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8413/health
Start-Process http://127.0.0.1:8413/admin/
```

`start.ps1` define defaults locales, incluido `GOOGLE_REAL=0`, antes de iniciar el servidor. No lee automáticamente un archivo `.env`. Si quieres ejecutar un experto, configura las credenciales del modelo en el catálogo **Modelos** de la Admin UI. El servidor puede iniciar sin proveedor de modelos, pero la ejecución del experto no podrá completarse. Los proveedores remotos opcionales reciben el contenido incluido en una ejecución.

## Documentación

- [Instalación completa](docs/SETUP.md)
- [API HTTP](docs/API.md)
- [Admin UI](docs/ADMIN_UI.md)
- [Comportamiento del workspace y acceso por teclado](docs/UI_WORKSPACE.md)
- [Tareas persistentes, validación y límites](docs/PERSISTENT_TASKS.md)
- [Trabajo largo y subdivisión durante la ejecución](docs/LONG_RUNNING_WORK.md)
- [Equipo y asignación de proyectos](docs/TEAM_ACCESS.md)
- [Mi cuenta: GitHub y Google/Gmail](docs/USER_ACCOUNTS.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [Evidencia de validación y límites conocidos](docs/VALIDATION.md)
- [Límites de seguridad](SECURITY.md)
- [Contribuir](CONTRIBUTING.md)

## Estado y comentarios

Relay es software experimental para uso local. La instalación y validación documentadas tienen como destino Windows. Los resultados registrados usan datos ficticios y proveedores simulados; no acreditan preparación para producción, aislamiento multiusuario, integración real con proveedores ni despliegue. Lee [VALIDATION.md](docs/VALIDATION.md) y [SECURITY.md](SECURITY.md) antes de usarlo con repositorios o credenciales reales.

¿Encontraste un problema o tienes una sugerencia concreta? [Abre un issue](https://github.com/FourBis/4bis.relay-showcase/issues).

## Atribución y licencia

FourBis · Jeremías Badilla. El proyecto se distribuye bajo la licencia MIT; los componentes de terceros conservan sus propias licencias y avisos.
