# Setup local

Esta guía cubre el arranque local en Windows con PowerShell. Python 3.11 o posterior es requisito; la portabilidad a otros sistemas no está validada.

## 1. Crear el entorno

Desde la raíz del repositorio:

```powershell
python -m venv .\mcp-server\.venv
& ".\mcp-server\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install -e '.\mcp-server[dev]'
```

El paquete y sus dependencias se describen en [mcp-server/pyproject.toml](../mcp-server/pyproject.toml). No hay un comando separado de migración o seed: el relay inicializa su SQLite al arrancar.

## 2. Configuración

El servidor no carga `.env` por sí mismo. La plantilla de variables está en `.env.example`, en la raíz del repositorio. Conserva los secretos fuera del repositorio y deja los valores sensibles vacíos en ejemplos y documentación.

Para una instalación mínima, ejecuta `start.ps1` y abre la Admin UI. El script prepara las variables de arranque que necesita el proceso; la configuración operativa se completa desde el catálogo **Modelos** y el tab **Config** de la Admin UI.

```powershell
Start-Process http://127.0.0.1:8413/admin/
```

`start.ps1` fija `MCP_PORT` en `8413`. `MCP_HOST` se usa como fallback del primer arranque; después el valor persistido en `system_config.RELAY_HOST` tiene prioridad.

Para ejecutar expertos, registra o habilita un proveedor en **Modelos**, selecciona el modelo y guarda su API key en el catálogo. El código resuelve desde allí el proveedor, el modelo y el nombre de la variable de credencial; no dependas de establecer `FOURBIS_MODEL` o `MINIMAX_API_KEY` en la sesión para la configuración operativa.

La configuración operativa se persiste en `system_config` dentro de SQLite y se administra desde la Admin UI. `FOURBIS_DB_PATH` es la excepción de arranque: selecciona la base antes de consultar esa tabla. Si queda vacío, el valor predeterminado del código es `~/.4bis/relay.db`.

## 3. Arrancar y detener

```powershell
.\mcp-server\start.ps1
```

Ejecuta ese comando desde la raíz. El script entra en `mcp-server`, verifica que exista `.venv\Scripts\python.exe`, comprueba que el puerto 8413 esté libre y lanza `python -u -m relay.server`. La terminal queda ocupada mientras el relay está activo.

Para detenerlo desde otra terminal:

```powershell
cd mcp-server
.\stop.ps1
```

Abre la interfaz en <http://127.0.0.1:8413/admin/> y verifica el estado con:

```powershell
Invoke-RestMethod http://127.0.0.1:8413/health
```

## 4. Probar la API

```powershell
Invoke-RestMethod http://127.0.0.1:8413/admin/api/projects

$body = @{
    target = "demo"
    user = "consulta de ejemplo"
    source = "powershell"
    author = "local"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8413/experts/run `
    -ContentType "application/json" `
    -Body $body
```

La ejecución de un experto responde con `202` y un identificador; el resultado puede consultarse mediante los endpoints de estado y chat descritos en [docs/API.md](API.md). El proyecto indicado por `target` debe existir en la base local.

## 5. Ejecutar verificaciones

Desde la raíz del repositorio:

```powershell
python -m pip install -e '.\mcp-server[dev]'
.\mcp-server\.venv\Scripts\python.exe -m pytest -q
```

Los tests JavaScript de la interfaz se pueden ejecutar directamente si Node está disponible:

```powershell
node --test mcp-server/tests/*.test.mjs
```

Estos comandos son instrucciones de validación; su resultado depende del entorno y no se declara aquí como aprobado.

## Problemas frecuentes

- Si el puerto 8413 está ocupado, detén la instancia anterior con `mcp-server\stop.ps1`.
- Si `/experts/run` devuelve `503`, revisa el modelo seleccionado y la variable de clave del proveedor.
- Si `/experts/run` devuelve `404`, registra el proyecto desde la Admin UI o mediante `POST /admin/api/projects`.
- Si PowerShell bloquea la activación del entorno, revisa la política de ejecución de tu usuario antes de continuar.

Para entender la superficie HTTP y sus límites, consulta [docs/API.md](API.md), [docs/ADMIN_UI.md](ADMIN_UI.md), [docs/ARCHITECTURE.md](ARCHITECTURE.md) y la validación concreta en [docs/VALIDATION.md](VALIDATION.md).
