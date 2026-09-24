# Interfaz web

Abre `http://127.0.0.1:8413/admin/` tras iniciar Relay. El chat es el punto
de entrada. **Herramientas** abre un catálogo con búsqueda; cada módulo aparece
como una ventana del mismo workspace.

| Tarea | Herramientas |
| --- | --- |
| Conversar y consultar | Chat, Proyectos, Skills, Diagramas |
| Seguir ejecuciones | Estado, En curso, Informe, Métricas, Logs |
| Organizar trabajo | Gestión, CRM, Night Runs, Zombies |
| Administrar accesos | Equipo, Mi cuenta |
| Preparar el entorno | Indexación, Huérfanos, Voz, Comandos, MCPs, Modelos, Config |

## Primer uso

1. En **Modelos**, configura un proveedor, su endpoint y su clave local si aplica.
2. En **Proyectos**, registra una carpeta de trabajo y selecciona el modelo.
3. Abre **Chat**, selecciona ese proyecto y crea una conversación.
4. Mantén **En curso** o **Métricas** abiertos si necesitas consultar la ejecución.

Un servicio sin modelos configurados puede mostrar la interfaz; las ejecuciones
que requieran un proveedor necesitan esa configuración.

En **Equipo**, el Admin edita roles y asigna proyectos. **Mi cuenta** conecta
GitHub y Google/Gmail por persona. Una tarea de solo lectura requiere después
**Habilitar escritura** y **Continuar** desde sus controles. Consulta
[Equipo](TEAM_ACCESS.md) para el recorrido y los límites de cada rol.

## Ventanas y resultados

Mueve una ventana desde su título o redimensiónala desde la esquina. Puedes
expandirla, minimizarla, cerrarla y recuperarla desde la bandeja o el catálogo.
El botón de mosaico organiza las ventanas visibles.

**Abrir en workspace** convierte una respuesta en un objeto separado. Desde
ese objeto se pueden extraer tablas y gráficos SVG; las tablas permiten filtrar
y copiar datos. **Ir a la conversación** vuelve al origen.

`Ctrl+Mayús+K` abre herramientas y `Ctrl+K` abre la búsqueda. Las cabeceras admiten
flechas para mover, Mayús+flechas para cambiar tamaño y Enter para expandir.
En móvil se muestra una ventana activa con acceso al resto desde la bandeja.

Consulta [UI_WORKSPACE.md](UI_WORKSPACE.md) para persistencia y límites, y
[SETUP.md](SETUP.md) para instalación y pruebas.
