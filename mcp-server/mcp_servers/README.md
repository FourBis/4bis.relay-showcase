# 4bis.relay — MCP servers propios (wrappers stdio)

Wrappers Python que exponen capacidades reales al catálogo `mcp_servers`
del relé. Cada uno es un servidor MCP stdio que el relé puede `acquire()`
vía `McpPool` y usar como toolset de un experto pydantic-ai.

## Patrón

Cada wrapper es:

1. Un único archivo Python sin dependencias raras (solo stdlib + 1-2 libs
   específicas del dominio).
2. Async API si toca I/O (Playwright, HTTP, etc.) — la **sync API
   no se puede usar dentro del event loop de FastMCP** ("It looks like
   you are using Playwright Sync API inside the asyncio loop").
3. Singleton con `asyncio.Lock` (los MCP stdio son lineales, así que el
   lock raramente contiende, pero está).
4. Errores → texto en el resultado del tool, no excepción. El LLM
   prefiere ver el error a quedarse mudo.
5. Read-only por diseño (nada que modifique estado del mundo).

## Cómo se enchufa al relé

```json
POST /admin/api/mcp {
  "name": "playwright-mcp",
  "capability": "browser",
  "transport": "stdio",
  "command": "<path al venv python>",
  "args": ["<path al wrapper>"],
  "env": {},
  "read_only": true,
  "on_demand": true,
  "enabled": true,
  "project_slugs": ["<slug>"]   // o vacío = global
}
```

Después un experto lo usa con `--con browser` (o el capability que
corresponda) y F1+F3 del plan MCP_REGISTRY se encargan del resto.

## Lista

| Archivo | Capability | Tools | Notas |
|---|---|---|---|
| `playwright_mcp.py` | `browser` | navigate, get_title, get_url, get_text, get_html, screenshot, close | Necesita `pip install playwright` + `playwright install chromium-headless-shell` |

## Cómo agregar uno nuevo

1. Copiar `playwright_mcp.py` como molde.
2. Cambiar el `FastMCP("nombre")` y la lista de tools.
3. Reusar el patrón de `_ensure_page`/`_close` con tu singleton
   (cliente HTTP, conexión DB, etc.).
4. Probar directo: `python mcp_servers/wrapper.py` en una terminal con
   un cliente MCP (Claude desktop, etc.) — si responde a `initialize`,
   el handshake anda.
5. Cargar en el catálogo del relé con la API.
6. Probar el handshake desde el relé:
   `python -m pytest tests/test_mcp_ondemand.py -q` (con un monkeypatch
   o usando el e2e manual `scripts/e2e_mcp_install.ps1`).
