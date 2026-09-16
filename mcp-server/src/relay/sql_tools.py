"""Tools SQL nativas, con el permiso efectivo del usuario y del proyecto."""
from pydantic_ai import Tool
from . import dbtool
from . import files
from .execution_policy import ExecutionPolicy


def sql_tools(db, policy: ExecutionPolicy, *, perm=None) -> list[Tool]:
    tools = []

    async def db_connections() -> str:
        """Lista las bases YA REGISTRADAS, sin credenciales.

        Es un atajo, no el límite: `db_query` también acepta una
        cadena de conexión completa. Que una base no esté en esta
        lista no significa que no puedas consultarla.
        """
        try:
            return dbtool.describir(await db.list_db_connections())
        except Exception as e:  # noqa: BLE001
            return f"error listando conexiones: {e}"

    async def db_query(conexion: str, sql: str, limite: int = 0) -> str:
        """Ejecuta una consulta SQL y devuelve las filas.

        Args:
            conexion: el alias de una conexión registrada (`relay`
                para la base del propio relay, o las que liste
                `db_connections`), o una cadena de conexión completa
                — `postgres://usuario:clave@host:5432/base`,
                `mysql://…`, o la ruta de un `.sqlite`.

                La cadena NO tiene que venir del humano. Si el
                proyecto guarda la suya en su configuración
                (`appsettings.json`, `.env`, `docker-compose.yml`,
                `application.properties`), léela con las tools de
                archivos y pásala acá directamente. Nunca respondas
                "no tengo acceso a esa base" sin haber buscado la
                cadena en el repo primero.

                Una cadena pegada se conecta siempre en solo lectura,
                así que no hay riesgo de tocar datos por error.
            sql: la consulta. UNA sola por llamada, sin `;` en el
                medio. Solo lectura salvo que la conexión esté
                marcada como escribible.
            limite: filas máximas. 0 = el default del relay.

        Las conexiones son de SOLO LECTURA por default. Si necesitás
        escribir, no lo intentes con rodeos: pedíselo al humano con
        `ask_human`.
        """
        try:
            dsn, solo_lectura = await dbtool.resolver(
                db, conexion, relay_db_path=str(getattr(db, "path", "")))
        except dbtool.ConexionDesconocida as e:
            return f"error: {e}"
        solo_lectura = solo_lectura or policy.sql_read_only
        if perm is not None and dbtool.motor(dsn) == "sqlite":
            try:
                ruta = dsn[len("sqlite://"):] if dsn.lower().startswith("sqlite://") else dsn
                path = perm.resolve(ruta)
                if not solo_lectura:
                    perm.exigir_escritura("escribir en SQLite")
                    perm.exigir_escribible(path)
                    perm.exigir_libre(path)
                dsn = str(path)
            except (files.FueraDelRepo, files.SinPermiso) as exc:
                return f"error: {exc}"
        if limite and limite > 0:
            sql = sql.rstrip().rstrip(";")
            if " limit " not in sql.lower():
                sql = f"{sql} LIMIT {int(limite)}"
        try:
            out = await dbtool.consultar(dsn, sql, solo_lectura=solo_lectura)
        except dbtool.SqlNoPermitido as e:
            return f"error: {e}"
        # Nunca devolvemos el DSN: si el humano lo pegó en el chat,
        # ya está en el historial una vez; repetirlo en cada result
        # lo multiplica por cada turno posterior.
        #
        # El `escritura` no es decorativo: cuando la conexión puede
        # escribir, el modelo tiene que ver en el resultado contra
        # qué base lo hizo. Es la diferencia entre "lo probé en
        # stage" y "lo hice en producción".
        marca = "" if solo_lectura else " · escritura"
        return f"[{dbtool.redactar(dsn)}{marca}]\n{out}"

    tools.append(Tool(db_connections, takes_ctx=False))
    tools.append(Tool(db_query, takes_ctx=False))

    return tools
