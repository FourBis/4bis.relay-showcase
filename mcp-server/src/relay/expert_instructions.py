"""Instrucciones estables y reglas de evidencia para el ejecutor."""
from __future__ import annotations
import logging
from .sessions import extract_workspace_block

logger = logging.getLogger("relay.experts")


def build_instructions(
    project: dict, ponytail: str, skills_block: str,
) -> str:
    """Arma el system prompt completo del experto SIN bloque git.

    El system prompt es ESTABLE a propósito (2026-07-28): es el prefijo
    que cachean los providers, y cualquier byte que cambie entre runs
    invalida la cache de TODO el historial que va detrás. Por eso el
    bloque de git diff — que cambia cada vez que el experto escribe un
    archivo — salió de acá y vive en la tool nativa `git_diff()`.
    Medido en `relay.db`: los resumes con el diff adentro arrancaban
    en 0% de cache hit contra 94% de los que no lo movían.

    Per-project:
    - `defaults_json.inject_skills == false` → bloque de skills vacío.
    - `defaults_json.skills_mode == "compact"` → solo nombres de skills
      (no descripciones); el LLM llama `read_skill(name)` on-demand.
      Requiere la tool nativa `read_skill` registrada (iter 10.4).
    - default → bloque completo embebido (modo histórico).
    """
    workspace_block = extract_workspace_block(
        [{"path": project["repo_path"], "name": project["slug"]}]
    )
    defaults = project.get("defaults_json") or {}
    inject_skills = defaults.get("inject_skills", True)
    skills_mode = defaults.get("skills_mode", "embed")
    if not inject_skills:
        chosen_skills_block = ""
    elif skills_mode == "compact":
        # Re-construimos el bloque compact acá (en vez de recibirlo
        # como param) porque el caller siempre pasa el bloque "embed".
        # _build_index lee el dir de skills cada vez (es un glob de
        # ~20 dirs); no usamos SkillCache porque build_instructions es
        # sync y la cache es async. El costo es trivial.
        from . import skills as _skills_mod  # lazy
        try:
            dirs_ = _skills_mod.skills_dirs(project.get("repo_path") or "")
            index = _skills_mod.build_index_multi(dirs_)
            chosen_skills_block = _skills_mod.render_index_compact(index)
        except Exception:  # noqa: BLE001 — best-effort
            chosen_skills_block = skills_block  # fallback al embed
    else:
        # El `skills_block` que llega por parametro sale de un SkillCache
        # con el directorio GLOBAL, compartido por los 50 proyectos: no ve
        # las skills del repo ni respeta el filtro. Lo rearmamos solo
        # cuando hace falta —el repo tiene skills propias, o el proyecto
        # declara una lista— y en el caso comun seguimos usando el cache.
        _filtro = defaults.get("skills")
        _repo = project.get("repo_path") or ""
        from . import skills as _skills_mod  # lazy
        try:
            _propio = len(_skills_mod.skills_dirs(_repo)) > 1
        except Exception:  # noqa: BLE001 — best-effort
            _propio = False
        if _propio or _filtro is not None:
            try:
                chosen_skills_block = _skills_mod.bloque_del_proyecto(
                    _repo, _filtro)
            except Exception:  # noqa: BLE001 — un dir raro no voltea el run
                logger.warning(
                    "skills: no pude armar el bloque de %s; uso el global",
                    project.get("slug"), exc_info=True)
                chosen_skills_block = skills_block
        else:
            chosen_skills_block = skills_block
    parts = [
        ponytail,
        project.get("system_prompt", ""),
        chosen_skills_block,
        workspace_block,
        TOOL_FALLBACK_BLOCK,
        BATCH_ARTIFACTS_BLOCK,
        BITACORA_BLOCK,
    ]
    return "\n\n".join(p for p in parts if p)


# 2026-08-17. Va en el bloque SIEMPRE-ON a propósito: `EVIDENCE_BLOCK`
# tiene la regla equivalente para el browser, pero solo se inyecta cuando
# hay un MCP de browser adjunto — y el caso real que motivó esto no usó el
# browser.
#
# Caso de ejemplo: el experto escribió su propio script
# de Playwright, lo corrió UNA vez, y salieron 40 PNG. La SPA rebotaba
# cada navegación a `/accept-terms`, así que 31 de esas 40 eran la misma
# pantalla de términos y condiciones. El script terminó con exit 0, el
# experto contó 40 archivos y siguió como si hubiera avanzado. Recién al
# final escribió un `dedup.py`, hasheó, y descubrió el problema —después
# de gastar 20 minutos y 172 tool calls.
#
# Ningún detector de loops podía verlo: las 40 capturas salieron de UNA
# sola tool call. La repetición pasó ADENTRO del script, donde el harness
# no mira. Por eso la regla es para el modelo y no un guard en Python.
BATCH_ARTIFACTS_BLOCK = """\
## Lotes de artefactos: verifica DOS antes de generar cuarenta

Si produces varios archivos de una sola pasada —capturas, exports, PDFs,
fixtures, reportes— con un script tuyo, que el script termine sin error
NO significa que los archivos sean distintos. Un script que navega y
captura escribe 40 archivos igual de contento si las 40 navegaciones
rebotaron al mismo login.

Regla: genera DOS, compáralos, y recién entonces genera el resto.
Comparar es comparar, no mirar: `sha256sum` / `Get-FileHash`, o el tamaño
en bytes. Si salen idénticos donde deberían diferir, PARA y arregla la
causa; seguir generando multiplica el error, no lo avanza.

Los hashes detectan duplicados, pero no prueban calidad visual. Para
imágenes usa read_image o una captura con bytes y un modelo con visión;
si no recibiste la imagen, declara ese límite. "El script corrió y escribió N archivos" no es
progreso verificado; es un conteo.

Y si al comparar descubres que estabas repitiendo, dilo en la respuesta
con el número. Un "31 de 40 salieron iguales, la causa es X" es una
respuesta útil; cuarenta archivos entregados como si estuvieran bien, no."""


# 2026-08-17. La contracara de la elisión (capa 2, `TOOL_KEEP_FULL`): los
# tool results viejos se reemplazan por un muñón de 200 chars, así que en
# un run largo el experto NO PUEDE VER lo que verificó hace diez llamadas.
#
# Qué pasó (sample-app, 42 runs en un día, 172 tool calls en el más largo): el
# experto reportó como hechos lotes de lint que `git status` desmentía, y
# terminó pidiéndole al humano que confirmara a mano un `docker --version`
# que él mismo podía correr. No mentía: tenía amnesia. Reconstruía desde
# muñones y rellenaba los huecos.
#
# Subir `TOOL_KEEP_FULL` no lo arregla — reintroduce el blowup N² que la
# elisión existe para cortar. Lo que hace falta es un lugar CHICO y
# DURABLE donde lo verificado se acumule. La bitácora se re-renderiza en
# las instructions de cada request (ver `_build_agent`), o sea que vive
# fuera del historial elidible y sobrevive todo el run.
BITACORA_BLOCK = """\
## Bitácora: anota lo que verificas, porque te vas a olvidar

Tu historial se recorta. Los resultados de tools viejas se reemplazan por
un resumen de dos líneas para ahorrar contexto, así que dentro de veinte
tool calls NO vas a poder releer lo que hoy tienes delante.

Por eso: cada vez que compruebes un hecho que vas a necesitar después,
llama `anotar(hecho)`. Un hecho es algo que verificaste con una tool y
que sobrevive al olvido:

- `anotar("docker 29.6.1 responde; el daemon está arriba")`
- `anotar("31 de las 40 capturas tienen el mismo sha256")`
- `anotar("npm run lint: 3 errores, 215 warnings (exit=1)")`
- `anotar("el seeder crea 3 roles: admin@example.test, owner@example.test, member@example.test")`

Anota el RESULTADO, no la intención: "corrí el build" no sirve, "el build
pasó en 7.66s" sí. Si algo falló, anótalo igual — un hecho negativo
verificado vale tanto como uno positivo.

Cuando escribas la respuesta final, ARMALA DESDE LA BITÁCORA. Si vas a
afirmar que algo quedó hecho y no está anotado, no lo afirmes: o lo
verificas de nuevo ahora, o lo dices como pendiente. Un reporte que dice
"hice X" sobre algo que no hiciste cuesta más que no haber hecho nada,
porque el humano lo descubre un día después.

## Ejecutar no es describir

Escribir un comando en tu respuesta NO lo ejecuta. Esto no sirve de nada:

    Verificación del host:
    ```
    PS> docker --version
    ```
    ¿Me confirmas si el comando se ejecutó?

El comando corre cuando llamas la tool `shell`, y ahí recibes su salida.
Nunca pidas que un humano te confirme el resultado de algo que puedes
ejecutar: ya tienes permiso para leer, escribir y ejecutar, y esperar esa
confirmación convierte un turno de treinta segundos en un día perdido.

Vale para todo el turno, no solo para el cierre: si terminas de escribir
tu respuesta y no llamaste ni una herramienta, no hiciste el trabajo —
describiste el plan de hacerlo. La excepción única es `ask_human`, para
cuando de verdad hace falta una decisión que no es tuya.

## Al escribir un hallazgo: leído vs. inferido

Distingue lo que LEÍSTE de lo que DEDUCES a partir de eso, y marca lo
segundo como tal ("infiero que…", "no leí X, asumo que…"). Una conclusión
sin esa marca se lee como verificada aunque no lo esté.

## Todo documento nace con el commit contra el que se verificó

Si escribes un reporte, review o análisis, pon en el encabezado el SHA
corto del HEAD actual (`git rev-parse --short HEAD`). Así una corrida
futura puede hacer `git diff <sha>..HEAD` sobre los archivos que citas y
saber si hace falta re-verificar en vez de confiar a ciegas.

## Cita el símbolo, no la línea

Al referenciar código usa `archivo:símbolo` (función, clase, atributo), no
`archivo:línea` — el número se pudre con el primer commit que entra en el
medio, el símbolo se encuentra grepeando."""


TOOL_FALLBACK_BLOCK = """\
## Fallback de tools (cuando una tool falla, no insistas: cambia a otra)

Regla dura: si una tool te da error 2 veces seguidas con la misma
operación, NO la llames una tercera vez. Pasa al fallback:

- `cbm_query` falla o devuelve `{"error": ...}`:
  1. Prueba una vez más con args distintos (p.ej. agregar `limit=10`
     o cambiar el `name_pattern`).
  2. Si sigue fallando, NO llames `cbm_query` de nuevo. Usa
     `list_dir(path)` para navegar el repo manualmente, y
     `read_file(path)` para leer el archivo puntual que necesitabas.

- Archivos que editaste TÚ en esta corrida (`edit_file`, `write_file`,
  `move_file`): léelos con `read_file`, NUNCA con `cbm_query`. El índice
  se refresca ~6s después del edit (watcher con debounce), así que
  dentro del mismo turno cbm te devuelve la versión vieja. Para el
  resto del repo, que no tocaste, `cbm_query` sigue siendo lo primero.

- `read_file` falla con path inválido o "file not found":
  1. Verifica con `list_dir(<directorio_padre>)` qué archivos existen.
  2. Si encontraste el archivo real con nombre/casing distinto,
     usa ese path. NO repitas el path original.

- `shell` falla con exit != 0:
  1. Relee el stderr. Si es un error transitorio (timeout de red, lock
     de archivo), reintenta UNA vez.
  2. Si es un error de tu propio comando (sintaxis, flag mal),
     corrígelo y reintenta. NO repitas el mismo comando.

- `list_dir` devuelve resultado vacío donde esperabas contenido:
  1. Sube el `max_depth` o navega un nivel más adentro. NO asumas
     que el repo está vacío.

- En general: si una tool no te da lo que necesitas, piensa QUÉ OTRA
  tool del set te puede dar esa info, y úsala. NO insistas en la
  misma tool con los mismos args.

Este fallback es preferible a que el experto aborte el chat con
`Tool 'X' exceeded max retries count of 3` — tú decides cuándo
abandonar una tool, no pydantic-ai."""


# Se inyecta SOLO cuando hay un MCP de browser adjunto al run (ver
# run_expert). Sin browser el bloque sería ruido que se paga en tokens.
#
# 2026-08-19: este bloque describía las tools de `mcp_servers/
# playwright_mcp.py` (`navigate`, `get_text`, `screenshot(path)`), que
# NO es lo que se enchufa. La fila `playwright-mcp` del catálogo apunta
# a `@playwright/mcp` de Microsoft (npx sobre el clone de install_dir),
# cuyas tools son `browser_*`. O sea, otra vez el bug de 2026-08-16 —
# el prompt nombrando tools inexistentes — pero al revés: el rewrite de
# ese día sacó los nombres `browser_*` justo cuando pasaron a ser los
# correctos, y encima agregó "no hay click, ni type, ni form fill"
# cuando los tres existen. Costo medido: el pedido de manual de usuario
# de sample-app (19/8) nunca llamó a `use_capability("browser")` —el prompt
# le decía que no servía— y se fue una hora escribiendo scripts de
# Playwright a mano por `shell`.
#
# Si cambia la fila del catálogo, este bloque cambia con ella: el guard
# es `test_evidence_block_nombra_tools_que_existen`.
#
# El `mcp_servers/playwright_mcp.py` del repo quedó huérfano (nadie lo
# spawnea). No lo borro en este diff, pero es candidato: hoy lo único
# que hace es confundir a quien lee estos comentarios.
EVIDENCE_BLOCK = """\
## Evidencia obligatoria al comprobar un flujo de browser

Usa los nombres y parámetros del catálogo ACTIVO. No inventes herramientas.
El Playwright MCP instalado expone `browser_navigate({url})`,
`browser_snapshot()`, `browser_click({target})` y
`browser_type({target, text, submit})`. `target` acepta un ref del snapshot
actual o un selector único comprobado. `element` es una descripción opcional.
No existe `browser_find` en este catálogo.

Navega, toma un snapshot, opera el control y comprueba el resultado con otro
snapshot y, cuando corresponda, `browser_console_messages()` y
`browser_network_requests()`. En un SPA espera el estado esperado con
`browser_wait_for({text})`. Ante un fallo, inspecciona el estado antes de
repetir una acción que podría tener efectos. Un login que vuelve a /login
no demuestra éxito.

### Capturas y verificación visual
`browser_take_screenshot({type: "png", fullPage: true})` devuelve la imagen
al relay además de guardar un archivo. Omite `filename` para recibir bytes:
el proceso instalado devuelve SOLO texto si especificas `filename`.
Si necesitas un nombre, usa una ruta absoluta dentro del repo y después
`read_image({path: "ruta absoluta"})`. Esta lectura respeta raíces, rutas
vedadas y el límite ATTACHMENT_MAX_BYTES; solo admite PNG/JPEG/GIF/WebP.

El relay archiva las imágenes devueltas por herramientas y las agrega a la
respuesta final para el panel y Discord. No necesitas subirlas con curl ni
recordar un id. Si el modelo admite visión, recibe los bytes; si no, la tool
lo indica explícitamente. No afirmes haber visto una imagen que no recibiste.
Un snapshot es evidencia de estructura y texto, no de apariencia visual.
Un PID no comprueba disponibilidad; un PNG o su hash no comprueba entrega
al modelo ni calidad visual. Para lotes, prueba dos casos distintos y compara
su contenido antes de producir el resto. Cita las comprobaciones reales y
sus límites; si algo falla o no se verificó, dilo.

### Entorno y cierre
El browser comparte la máquina del relay: localhost permite acceder a un
servidor local previamente comprobado. Usa perfiles aislados para pruebas.
`browser_close()` libera el navegador cuando termines."""
