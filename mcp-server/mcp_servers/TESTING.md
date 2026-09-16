# Flujo de prueba obligatorio: mock → smoke → E2E

> Regla: nunca saltear niveles. Si el test mock no pasa, no ir a smoke.
> Si el smoke no pasa, no ir a E2E. El E2E con el LLM real consume
> API y tiempo; un error que un mock podría haber detectado es tirar
> la API a la basura.

## Nivel 1 — Test mock (sin red, sin LLM)

Qué prueba: la lógica del wrapper en aislamiento. El wrapper se carga
como módulo y se llaman las funciones internas directamente, monkeypatcheando
clientes externos (httpx, playwright, psycopg).

Herramientas: `unittest.mock.patch`, fixtures in-memory.

Ejemplo (Playwright):
```python
# tests/mcp_servers/test_playwright_mcp_mock.py
from unittest.mock import patch, AsyncMock
from mcp_servers.playwright_mcp import _ensure_page, navigate

async def test_navigate_uses_headless():
    with patch("mcp_servers.playwright_mcp.async_playwright") as pw:
        # pw.start() → fake con browser.new_page() → fake page
        ...
```

Salida esperada: exit 0, sin warnings, sin tocar la red.

Si falla acá: bug en el wrapper. **No ir al nivel 2.**

## Nivel 2 — Smoke (wrapper contra el spec, sin LLM)

Qué prueba: el wrapper cumple el spec MCP. Cliente MCP stdio
(`mcp.client.stdio`) se conecta, llama `initialize`, llama cada tool,
valida la respuesta. Sin LLM, sin base de datos, sin red salvo lo
estrictamente necesario para el tool (httpx a una API real es OK).

Herramientas: `mcp.client.stdio.stdio_client` + `ClientSession`.

Ejemplo (Playwright):
```bash
python smoke_pw.py
```

Salida esperada:
```
[smoke] conectando al MCP...
[smoke] inicializado OK
[smoke] tools (7): navigate, get_title, ...
[smoke] navigate('https://httpbin.org/html') → OK
[smoke] get_text('h1') → Herman Melville - Moby-Dick
...
[smoke] OK
```

Si falla acá: bug en el handshake MCP, en el formato de respuesta, o
en la integración con la lib externa (httpx, playwright, etc).

## Nivel 3 — E2E con el relé (catálogo + acquire)

Qué prueba: el wrapper se carga en el catálogo del relé, el `McpPool`
lo acquire, y un run experto real (con LLM) lo usa. **Consume API
key del LLM** — no entrar a este nivel si el 1 o el 2 no están
verdes.

Herramientas: `e2e_gh_mcp.py`, `e2e_pw_mcp.py` (plantilla en este dir).

Pasos:
1. Levantar el relé con `.env` que tenga `MINIMAX_API_KEY` válida.
2. POST `/admin/api/mcp` con la fila del wrapper.
3. PATCH enabled=True.
4. POST `/experts/run` con `--con <capability>`.
5. Polling hasta done (max 180s).
6. GET `/chats/{id}/md` y verificar que el LLM ejecutó el tool.
7. DELETE cleanup.

Si falla acá: bug en el path de pydantic-ai, en el handshake via
`MCPToolset`, o en el sistema de toolsets. **No es bug del wrapper**
(ya validado en nivel 2).

## Regla de oro: documentar antes de cerrar

Cuando un nivel cierra verde:
1. Capturar el output completo (stdout + exit code).
2. Commit del cambio con un mensaje que diga qué nivel cerró.
3. Actualizar este TESTING.md con el resultado real, no aspiracional.
4. Si el nivel N+1 falla: anotar por qué, no seguir.

## Estado actual (2026-07-09)

| Wrapper | Nivel 1 (mock) | Nivel 2 (smoke) | Nivel 3 (E2E) |
|---|---|---|---|
| `playwright_mcp.py` | ✓ 11/11 (`tests/test_playwright_mcp.py`) | ✓ (`smoke_pw.py` 7/7 tools, httpbin real) | parcial (acquire OK, run LLM se cuelga) |
| `github_mcp.py` | parcial | ✓ (`smoke_gh.py` 7/7 tools, cpython real) | parcial (acquire OK, run LLM se cuelga) |
| `henkey-postgres` (externo) | n/a | ✓ (handshake 0.72s, 10 issues/5 PRs/5 commits) | parcial (acquire OK, run LLM no probado) |

### Detalle nivel 1 — `test_playwright_mcp.py` (11 tests)

Cubren:
- `TestNavigateMock`: navigate devuelve formato correcto con title/url/status, maneja excepción.
- `TestGetTextMock`: default selector `body`, error path cuando no matchea.
- `TestGetHtmlMock`: trunca a `max_chars`, pasa cuando es corto.
- `TestScreenshotMock`: escribe PNG real en `tempfile`, valida bytes y path.
- `TestSingletonLifecycle`: `_ensure_page` crea browser, reusa singleton.
- `TestCloseAndLocking`: `close()` resetea singletons, `navigate` acquire/release del lock.

Output: `11 passed in 0.97s`.

**Bug conocido que bloquea nivel 3**: el run experto con LLM real se
queda en `phase=tool_call` indefinidamente. Debug parcial mostró:
- `pool.acquire` funciona (0.5-1s)
- `async with toolset` funciona
- `get_tools(None)` falla con `AttributeError: 'NoneType' object has no attribute 'max_retries'` (mi test malo)
- `get_tools(ctx)` con ctx real de pydantic-ai → no testeado

**Causa probable**: pydantic-ai 2.5.1 + FastMCP stdio async — un
componente está bloqueando un future. Workaround conocido: usar
`ServerSession` directamente sin FastMCP, o downgrade a pydantic-ai 1.x.
