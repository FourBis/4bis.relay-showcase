"""Consultas SQL desde el chat: SQLite y Postgres (2026-08-16).

Pedido: *"el sqlite está con la cadena de relay y la idea es que se pueda
usar desde el chat cuando se necesite; agrega el de postgres para poder
usarlo también, la cadena de conexión que venga del llm o chat"*.

**Por qué nativo.** El driver ya está —`asyncpg` es dependencia del
relay desde el CRM, y `sqlite3` es stdlib—, así que para *ejecutar una
consulta* un MCP sería un subprocess envolviendo una librería que ya
tenemos importada. Esta tool cubre las dos bases (SQLite **y** Postgres)
con un solo catálogo de conexiones y un solo criterio de permisos.

**Convive con `postgres-mcp`** (capability `postgres`, on-demand, desde
el 2026-08-16 — ver docs/MCPS.md). No se pisan porque no hacen lo mismo:
acá está el acceso de todos los días a cualquier base registrada; allá
están EXPLAIN, salud del motor y sugerencia de índices, que necesitan
`pg_stat_statements` y un parser SQL que no vamos a reimplementar. Para
un Postgres donde el análisis importa, `--con postgres` es mejor
herramienta que esta — incluido su read-only, que es una transacción de
verdad y no el regex de `exigir_lectura`.

**Las dos formas de conectarse**, y por qué existen las dos:

1. **Alias** (`db_query("crm", …)`): la conexión vive en la tabla
   `db_connections` y el modelo nunca ve la contraseña. Es la forma
   preferida para las bases de siempre.
2. **DSN pegado** (`db_query("postgres://u:p@host/db", …)`): para la
   base que aparece una vez y no vale la pena registrar. El pedido era
   explícito —que la cadena pueda venir del chat—, así que se soporta,
   pero el DSN queda **fuera del historial**: lo que se guarda es el
   alias o el host, nunca las credenciales (ver `redactar`).

**Read-only por default.** Una conexión solo ejecuta `SELECT` / `WITH` /
`EXPLAIN` / `PRAGMA` salvo que esté marcada `escribir=1`. No es
paranoia: el modelo puede equivocarse de base, y un `DELETE` sin `WHERE`
contra la producción de un cliente no tiene deshacer. Para escribir, el
humano marca esa conexión en la Admin UI — el mismo criterio que
`read_only` en los archivos.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("relay.dbtool")

MAX_ROWS = int(os.environ.get("FOURBIS_SQL_MAX_ROWS", "200"))
MAX_CHARS = int(os.environ.get("FOURBIS_SQL_MAX_CHARS", "20000"))
QUERY_TIMEOUT_S = float(os.environ.get("FOURBIS_SQL_TIMEOUT", "30"))

# Lo único que corre una conexión de solo lectura. `WITH` entra porque un
# CTE que termina en SELECT es una consulta; si termina en INSERT (que
# Postgres permite) lo ataja el chequeo de verbos prohibidos de abajo.
_LECTURA = re.compile(r"^\s*(SELECT|WITH|EXPLAIN|PRAGMA|SHOW|DESCRIBE)\b",
                      re.IGNORECASE)
# Verbos que no pasan en read-only ni escondidos dentro de un CTE.
_ESCRITURA = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"REPLACE|MERGE|VACUUM|ATTACH|COPY)\b", re.IGNORECASE)


class SqlNoPermitido(PermissionError):
    """La consulta existe pero esta conexión no puede ejecutarla."""


class ConexionDesconocida(ValueError):
    """El alias no está registrado y el string no parece un DSN."""


def redactar(dsn: str) -> str:
    """DSN → algo que se pueda loguear y guardar en el historial.

    `postgres://usuario:secreto@host:5432/base` → `postgres://…@host/base`.
    Esto no es cosmético: el `messages_json` del hilo se replaya en cada
    turno y se guarda en disco, así que una contraseña que entre ahí queda
    para siempre y viaja a cada llamada del modelo.
    """
    s = (dsn or "").strip()
    m = re.match(r"^(?P<esquema>\w+)://(?:[^@/]*@)?(?P<resto>.*)$", s)
    if not m:
        return Path(s).name or "(local)"
    resto = m.group("resto")
    return f"{m.group('esquema')}://…@{resto}"


def motor(dsn: str) -> str:
    """`postgres` | `sqlite`. Por el esquema, y si no por la extensión."""
    s = (dsn or "").strip().lower()
    if s.startswith(("postgres://", "postgresql://")):
        return "postgres"
    if s.startswith("sqlite://"):
        return "sqlite"
    return "sqlite" if s.endswith((".db", ".sqlite", ".sqlite3")) else "postgres"


def _sin_literales(sql: str) -> str:
    """El SQL con los strings `'…'` vaciados, para escanear solo código.

    Sin esto, `WHERE estado='update pendiente'` y `WHERE nota='a;b'` se
    rechazan: la palabra y el `;` están DENTRO de un dato, no son SQL.
    Con datos en castellano eso pasa seguido, y un falso positivo acá se
    ve como "el relay no me deja consultar mi propia base".

    Maneja el escape estándar `''`. Si las comillas quedan desbalanceadas
    devuelve el original: prefiero escanear de más (rechazar algo válido)
    que escanear de menos (dejar pasar una escritura) — y una comilla
    impar es un error de sintaxis igual.
    """
    fuera, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c != "'":
            fuera.append(c)
            i += 1
            continue
        i += 1                       # abre literal
        while i < n:
            if sql[i] == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2           # '' escapado: sigue adentro
                    continue
                i += 1               # cierra
                break
            i += 1
        else:
            return sql               # nunca cerró → comillas impares
        fuera.append("''")
    return "".join(fuera)


def exigir_lectura(sql: str) -> None:
    """Lanza si `sql` no es una consulta de lectura.

    Dos chequeos y no uno: que EMPIECE con un verbo de lectura no alcanza
    —`WITH x AS (…) INSERT …` empieza con WITH—, así que además se busca
    cualquier verbo de escritura en todo el texto. Es deliberadamente
    conservador: un falso positivo cuesta que el humano marque la
    conexión como escribible; un falso negativo cuesta una tabla.

    El escaneo va sobre el SQL SIN literales (ver `_sin_literales`): lo
    que está entre comillas es un dato, no una instrucción.
    """
    s = (sql or "").strip()
    if not s:
        raise SqlNoPermitido("consulta vacía")
    codigo = _sin_literales(s)
    if ";" in codigo.rstrip(";"):
        raise SqlNoPermitido(
            "una consulta por llamada: encontré un `;` en el medio. "
            "Partila en llamadas separadas.")
    if not _LECTURA.match(s):
        raise SqlNoPermitido(
            "esta conexión es de solo lectura: solo SELECT / WITH / "
            "EXPLAIN / PRAGMA. Si de verdad hace falta escribir, pedíselo "
            "al humano con `ask_human` — lo habilita por conexión en la "
            "Admin UI.")
    if _ESCRITURA.search(codigo):
        raise SqlNoPermitido(
            "la consulta empieza como lectura pero contiene un verbo de "
            "escritura. Esta conexión es de solo lectura.")


def _formatear(columnas: list[str], filas: list[tuple], *,
               truncado: bool) -> str:
    """Filas → tabla de texto acotada.

    Formato tabular y no JSON: para el mismo contenido, JSON gasta ~40%
    más tokens en llaves y comillas, y el modelo lee igual de bien una
    tabla. Los `None` van como `∅` para que se distingan del string
    vacío, que en una base es otra cosa.
    """
    if not filas:
        return "(0 filas)"
    anchos = [len(c) for c in columnas]
    celdas: list[list[str]] = []
    for fila in filas:
        out = []
        for i, v in enumerate(fila):
            txt = "∅" if v is None else str(v)
            if len(txt) > 200:
                txt = txt[:200] + "…"
            txt = txt.replace("\n", "⏎")
            out.append(txt)
            anchos[i] = max(anchos[i], len(txt))
        celdas.append(out)
    anchos = [min(a, 60) for a in anchos]
    linea = " | ".join(c[:anchos[i]].ljust(anchos[i])
                       for i, c in enumerate(columnas))
    sep = "-+-".join("-" * a for a in anchos)
    cuerpo = [linea, sep]
    for fila in celdas:
        cuerpo.append(" | ".join(
            v[:anchos[i]].ljust(anchos[i]) for i, v in enumerate(fila)))
    texto = "\n".join(cuerpo)
    if len(texto) > MAX_CHARS:
        texto = texto[:MAX_CHARS] + f"\n…[recortado a {MAX_CHARS} chars]"
    pie = f"\n({len(filas)} fila{'s' if len(filas) != 1 else ''}"
    pie += f", cortado en {MAX_ROWS}: agregá LIMIT o filtros)" if truncado else ")"
    return texto + pie


async def _sqlite(dsn: str, sql: str, params: list, *,
                  solo_lectura: bool = True) -> tuple[list, list, bool]:
    ruta = dsn[len("sqlite://"):] if dsn.lower().startswith("sqlite://") else dsn
    ruta = os.path.expanduser(ruta.strip())
    if not Path(ruta).is_file():
        raise FileNotFoundError(f"no existe el archivo SQLite: {ruta}")

    cancelled = threading.Event()
    deadline = time.monotonic() + QUERY_TIMEOUT_S

    def _run():
        # `mode=ro` en la URI: la garantía la da SQLite, no nuestro regex.
        # Defensa en profundidad — `exigir_lectura` puede tener un agujero;
        # el driver abierto en solo-lectura, no.
        #
        # Se abre `rw` SOLO si la conexión está marcada escribible en
        # `db_connections`, que es una decisión que tomó el humano. Sin
        # esto una conexión con `escribir=1` a un .db igual fallaba, y el
        # permiso de la Admin UI era mentira para SQLite.
        uri = Path(ruta).resolve().as_uri() + f"?mode={'rw' if not solo_lectura else 'ro'}"
        conn = sqlite3.connect(uri, uri=True, timeout=min(10, QUERY_TIMEOUT_S))
        try:
            conn.set_progress_handler(
                lambda: int(cancelled.is_set() or time.monotonic() >= deadline), 1000)
            conn.row_factory = None
            cur = conn.execute(sql, params or [])
            cols = [d[0] for d in (cur.description or [])]
            filas = cur.fetchmany(MAX_ROWS + 1)
            if not solo_lectura:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("consulta SQLite cancelada antes del commit")
                conn.commit()
                if not cols:
                    # Un DELETE no devuelve columnas. "0 filas" haría
                    # pensar que no pasó nada; el rowcount dice qué pasó.
                    return ["filas_afectadas"], [(cur.rowcount,)]
            return cols, filas
        except sqlite3.OperationalError:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("consulta SQLite interrumpida") from None
            raise
        finally:
            conn.close()

    worker = asyncio.create_task(asyncio.to_thread(_run))
    try:
        cols, filas = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # Esperar el rollback/cierre: cancelar to_thread no detiene SQLite.
        try:
            await worker
        except Exception:
            pass
        raise
    return cols, filas[:MAX_ROWS], len(filas) > MAX_ROWS


async def _postgres(dsn: str, sql: str, params: list, *,
                    solo_lectura: bool = True) -> tuple[list, list, bool]:
    try:
        import asyncpg
    except ImportError as e:  # pragma: no cover - asyncpg es dependencia
        raise RuntimeError(f"asyncpg no está instalado: {e}") from e
    conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=15)
    try:
        # `fetch` de un INSERT sin RETURNING devuelve [] y se pierde el
        # resultado; `execute` devuelve el status ("DELETE 3"), que es lo
        # único que el modelo puede leer para saber si escribió.
        if not solo_lectura and not _LECTURA.match(sql.strip()):
            estado = await conn.execute(sql, *(params or []))
            return ["resultado"], [(estado,)], False
        # La transacción impone lectura incluso ante SELECT INTO o funciones
        # con efectos laterales. El cursor limita lo que llega a memoria.
        async with conn.transaction(readonly=solo_lectura):
            statement = await conn.prepare(sql)
            columnas = [a.name for a in statement.get_attributes()]
            if not columnas:
                estado = await conn.execute(sql, *(params or []))
                return ["resultado"], [(estado,)], False
            cursor = await statement.cursor(*(params or []))
            filas = await cursor.fetch(MAX_ROWS + 1)
    finally:
        await conn.close()
    datos = [tuple(f) for f in filas[:MAX_ROWS]]
    return columnas, datos, len(filas) > MAX_ROWS


async def consultar(dsn: str, sql: str, *, params: Optional[list] = None,
                    solo_lectura: bool = True) -> str:
    """Ejecuta y devuelve la tabla formateada.

    Los errores del MOTOR vuelven como texto porque son **información
    para el modelo**: "column x does not exist" le dice qué corregir; una
    excepción solo le dice que algo salió mal.

    Lo único que sí lanza es `SqlNoPermitido`, y a propósito: un permiso
    denegado no es un resultado, y quien llama tiene que decidir qué
    hacer con eso (la tool `db_query` lo convierte en texto para el
    modelo; la Admin UI podría querer un 403).
    """
    if solo_lectura:
        exigir_lectura(sql)
    kind = motor(dsn)
    try:
        if kind == "sqlite":
            cols, filas, truncado = await asyncio.wait_for(
                _sqlite(dsn, sql, params or [], solo_lectura=solo_lectura),
                timeout=QUERY_TIMEOUT_S)
        else:
            cols, filas, truncado = await asyncio.wait_for(
                _postgres(dsn, sql, params or [], solo_lectura=solo_lectura),
                timeout=QUERY_TIMEOUT_S)
    except asyncio.TimeoutError:
        return (f"error: la consulta pasó {QUERY_TIMEOUT_S:.0f}s y se cortó. "
                "Agregá un LIMIT, filtrá por índice, o pedile al humano que "
                "suba FOURBIS_SQL_TIMEOUT.")
    except FileNotFoundError as e:
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — el error del motor es la respuesta
        return f"error de {kind}: {type(e).__name__}: {e}"
    if not cols:
        return "(la consulta no devolvió columnas)"
    return _formatear(cols, filas, truncado=truncado)


# ---------- catálogo de conexiones ----------
#
# Guardar el DSN y darle al modelo un alias es lo que evita que las
# credenciales entren al historial del hilo. El modelo pide "crm"; el
# relay resuelve.


async def resolver(db: Any, nombre: str, relay_db_path: str = "") -> tuple[str, bool]:
    """`alias | dsn` → `(dsn, solo_lectura)`.

    `relay` es un alias reservado a la propia base del relay. Reservado
    quiere decir dos cosas distintas, y conviene no confundirlas:

    - **A dónde apunta**: SIEMPRE al `relay.db` de esta instalación, lo
      diga lo que diga la fila guardada. Si el alias se pudiera
      re-apuntar, `db_query("relay", …)` terminaría en otra base sin que
      nadie lo note — y el experto cree que está mirando la suya.
    - **Qué puede hacer ahí**: eso SÍ es configurable (2026-08-16, a
      pedido). Arranca en solo lectura y el humano lo habilita marcando
      `escribir` en la conexión `relay` de la Admin UI, igual que
      cualquier otra — o de una vez y para siempre con
      `FOURBIS_SQL_RELAY_WRITE=1` en el `.env`. Ahí el experto puede
      arreglar una fila trabada, limpiar un run zombie o corregir un
      `defaults_json` sin salir del chat.
    """
    n = (nombre or "").strip()
    if not n:
        raise ConexionDesconocida("falta el nombre de la conexión")
    if n.lower() == "relay":
        ruta = relay_db_path or os.environ.get("FOURBIS_DB_PATH", "")
        if not ruta:
            ruta = str(Path.home() / ".4bis" / "relay.db")
        # La RUTA nunca sale de la fila; el PERMISO sí.
        #
        # El env existe porque el toggle de la UI vive en la DB, y una DB
        # nueva (máquina nueva, reset, otro checkout) vuelve a arrancar en
        # solo lectura. Quien ya decidió que quiere escribir no debería
        # tener que volver a decidirlo cada vez.
        if os.environ.get("FOURBIS_SQL_RELAY_WRITE", "").strip().lower() in (
                "1", "on", "true", "yes", "si", "sí"):
            return ruta, False
        fila = await db.get_db_connection("relay")
        return ruta, not (fila and fila.get("escribir"))
    if "://" in n or n.endswith((".db", ".sqlite", ".sqlite3")):
        return n, True          # DSN pegado: siempre lectura
    fila = await db.get_db_connection(n)
    if fila is None:
        conocidas = [c["alias"] for c in await db.list_db_connections()]
        raise ConexionDesconocida(
            f"no conozco el alias {n!r}. Registrados: "
            f"{', '.join(['relay'] + conocidas) or 'ninguno'}. "
            "En vez de rendirte: buscá la cadena de conexión en la "
            "configuración del proyecto (appsettings.json, .env, "
            "docker-compose.yml) y pasala completa en `conexion` — "
            "funciona igual, en solo lectura.")
    return fila["dsn"], not fila.get("escribir")


def describir(filas: list[dict]) -> str:
    """Las conexiones disponibles, sin credenciales."""
    lineas = ["alias    motor      permiso   descripción",
              "-------- ---------- --------- ------------"]
    # `relay` va siempre y va primero, con el permiso que tenga guardado.
    propia = next((f for f in filas if f["alias"] == "relay"), None)
    lineas.append(
        f"{'relay':<8} {'sqlite':<10} "
        f"{('escritura' if propia and propia.get('escribir') else 'lectura'):<9} "
        "la base del propio relay (chats, tokens, veredictos)")
    for f in filas:
        if f["alias"] == "relay":
            continue
        permiso = "escritura" if f.get("escribir") else "lectura"
        lineas.append(
            f"{f['alias']:<8} {motor(f['dsn']):<10} {permiso:<9} "
            f"{(f.get('descripcion') or '')[:60]}")
    if len(lineas) == 3:
        lineas.append("(no hay otras conexiones registradas)")
    # El aviso va SIEMPRE, no solo cuando la tabla está vacía. Visto el
    # 2026-08-17: con solo `relay` en la lista, el experto contestó "no
    # tengo acceso a la base de SampleApp" y se quedó ahí — leyó el listado
    # como el catálogo completo de lo que puede hacer. La tabla es lo que
    # está registrado, no el límite: la cadena la puede conseguir él.
    lineas.append(
        "\nEsta tabla es lo que hay REGISTRADO, no el límite. `db_query` "
        "también acepta una cadena de conexión completa "
        "(postgres://…, mysql://…, o la ruta de un .sqlite). Si el "
        "proyecto guarda la suya en su configuración (appsettings.json, "
        ".env, docker-compose.yml, application.properties), léela con las "
        "tools de archivos y pásala directamente: no hace falta que el "
        "humano la escriba. Una cadena pegada se conecta siempre en solo "
        "lectura.")
    return "\n".join(lineas)
