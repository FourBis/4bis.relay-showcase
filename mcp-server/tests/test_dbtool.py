"""SQL desde el chat: SQLite + Postgres nativos (2026-08-16).

Lo crítico acá no es el formateo de la tabla: es que **una conexión de
solo lectura no escriba** y que **el DSN no entre al historial**. Esas
dos cosas son límites de seguridad, no comodidades:

- Si `exigir_lectura` tiene un agujero, el experto puede hacer un DELETE
  contra la producción de un cliente y eso no tiene deshacer.
- Si `redactar` deja pasar la contraseña, queda en `messages_json`, que
  se guarda en disco y se replaya en CADA turno posterior — o sea que
  además viaja a cada llamada del modelo.

Por eso los tests de esas dos funciones son parametrizados y agresivos,
y el resto (formato, caps) va más liviano.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from relay import dbtool
from relay.db import Database


# ---------- 1. read-only: lo que NO debe pasar ----------


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "insert into t values (1)",
    "  UPDATE t SET x=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "TRUNCATE t",
    "ALTER TABLE t ADD COLUMN x",
    "CREATE TABLE t (x int)",
    "GRANT ALL ON t TO public",
    "REVOKE ALL ON t FROM public",
    "VACUUM",
    "ATTACH DATABASE '/etc/passwd' AS x",
    "COPY t FROM '/etc/passwd'",
])
def test_verbos_de_escritura_se_rechazan(sql):
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura(sql)


@pytest.mark.parametrize("sql", [
    # Postgres permite CTE que termina en escritura: empieza con WITH,
    # pasa el primer chequeo, y escribe igual. Este es EL caso por el que
    # existe el segundo regex.
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "WITH x AS (SELECT id FROM u) DELETE FROM t WHERE id IN (SELECT id FROM x)",
    "WITH x AS (SELECT 1) UPDATE t SET a=1",
])
def test_escritura_escondida_en_un_cte_se_rechaza(sql):
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE t",
    "SELECT 1;SELECT 2",
    "SELECT 1 ; DELETE FROM t ;",
])
def test_multi_statement_se_rechaza(sql):
    """Un `;` en el medio convierte cualquier SELECT en un caballo de Troya."""
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura(sql)


@pytest.mark.parametrize("sql", ["", "   ", "\n\t "])
def test_consulta_vacia_se_rechaza(sql):
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura(sql)


def test_mensaje_de_rechazo_dice_como_seguir():
    """El error es para el modelo: tiene que decirle qué hacer, no solo no."""
    with pytest.raises(dbtool.SqlNoPermitido) as e:
        dbtool.exigir_lectura("DELETE FROM t")
    assert "ask_human" in str(e.value)


# ---------- 2. read-only: lo que SÍ debe pasar ----------


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from chats",
    "  \n SELECT a FROM t WHERE b='delete me'",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "EXPLAIN SELECT 1",
    "PRAGMA table_info(chats)",
    "SHOW TABLES",
    "SELECT 1;",          # `;` final solo: no es multi-statement
    "SELECT 1;   ",
])
def test_lectura_pasa(sql):
    dbtool.exigir_lectura(sql)


@pytest.mark.parametrize("sql", [
    # Lo que está entre comillas es un DATO, no una instrucción. Con
    # datos en castellano esto aparece seguido y el rechazo se ve como
    # "el relay no me deja consultar mi propia base".
    "SELECT * FROM t WHERE nota LIKE '%update%'",
    "SELECT * FROM t WHERE estado='delete pendiente'",
    "SELECT * FROM t WHERE nota='a;b'",
    "SELECT 'DROP TABLE t' AS ejemplo",
    "SELECT * FROM t WHERE x='O''Brien; DELETE'",   # escape '' adentro
])
def test_una_palabra_prohibida_dentro_de_un_literal_no_bloquea(sql):
    dbtool.exigir_lectura(sql)


@pytest.mark.parametrize("sql", [
    # El literal se vacía, pero el código de afuera se escanea igual.
    "SELECT 'x' FROM t; DELETE FROM t",
    "WITH x AS (SELECT 'update') INSERT INTO t SELECT * FROM x",
])
def test_el_vaciado_de_literales_no_abre_la_puerta(sql):
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura(sql)


def test_comillas_impares_escanean_el_texto_crudo():
    """Ante comillas desbalanceadas se escanea de más, no de menos.

    Es sintaxis inválida igual; lo que importa es que el modo degradado
    caiga del lado seguro.
    """
    with pytest.raises(dbtool.SqlNoPermitido):
        dbtool.exigir_lectura("SELECT * FROM t WHERE x=' AND DELETE FROM t")


def test_sin_literales_vacia_solo_los_strings():
    assert dbtool._sin_literales("SELECT 'ab' FROM t") == "SELECT '' FROM t"
    assert dbtool._sin_literales("SELECT 1") == "SELECT 1"


def test_exigir_lectura_no_devuelve_nada_cuando_pasa():
    """Contrato: valida por excepción, no por valor de retorno."""
    assert dbtool.exigir_lectura("SELECT 1") is None


# ---------- 3. redactar: las credenciales no entran al historial ----------


@pytest.mark.parametrize("dsn,esperado", [
    ("postgres://usuario:secreto@host:5432/base", "postgres://…@host:5432/base"),
    ("postgresql://u:p@10.0.0.1/db", "postgresql://…@10.0.0.1/db"),
    ("postgres://host/base", "postgres://…@host/base"),
])
def test_redactar_saca_usuario_y_password(dsn, esperado):
    assert dbtool.redactar(dsn) == esperado


@pytest.mark.parametrize("secreto", ["secreto", "p4ssw0rd!", "hunter2"])
def test_redactar_no_deja_rastro_del_secreto(secreto):
    salida = dbtool.redactar(f"postgres://usuario:{secreto}@host:5432/base")
    assert secreto not in salida
    assert "usuario" not in salida


def test_redactar_de_una_ruta_sqlite_deja_solo_el_nombre():
    """La ruta de un .db puede tener el nombre del cliente o del usuario."""
    assert dbtool.redactar("/home/demo/projects/acme/prod.db") == "prod.db"


def test_redactar_de_vacio_no_explota():
    assert dbtool.redactar("") == "(local)"


# ---------- 4. motor ----------


@pytest.mark.parametrize("dsn,esperado", [
    ("postgres://u:p@h/d", "postgres"),
    ("postgresql://u:p@h/d", "postgres"),
    ("POSTGRES://U:P@H/D", "postgres"),
    ("sqlite:///home/x/relay.db", "sqlite"),
    ("/home/demo/.relay/relay.db", "sqlite"),
    ("C:/Users/demo/relay.sqlite3", "sqlite"),
    ("relay.sqlite", "sqlite"),
])
def test_motor(dsn, esperado):
    assert dbtool.motor(dsn) == esperado


# ---------- 5. ejecución real contra SQLite ----------


@pytest.fixture
def base(tmp_path):
    ruta = tmp_path / "demo.db"
    conn = sqlite3.connect(ruta)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, nombre TEXT)")
    conn.executemany("INSERT INTO t (nombre) VALUES (?)",
                     [("ana",), ("beto",), (None,)])
    conn.commit()
    conn.close()
    return str(ruta)


def _run(coro):
    return asyncio.run(coro)


def test_select_devuelve_las_filas(base):
    out = _run(dbtool.consultar(base, "SELECT id, nombre FROM t ORDER BY id"))
    assert "ana" in out and "beto" in out
    assert "(3 filas)" in out


def test_none_se_ve_distinto_del_string_vacio(base):
    """En una base `NULL` y `''` son cosas distintas; el modelo tiene que verlo."""
    out = _run(dbtool.consultar(base, "SELECT nombre FROM t WHERE id=3"))
    assert "∅" in out


def test_cero_filas_no_es_un_error(base):
    out = _run(dbtool.consultar(base, "SELECT * FROM t WHERE id=999"))
    assert "0 filas" in out


def test_error_del_motor_vuelve_como_texto(base):
    """Es información para el modelo: le dice qué corregir."""
    out = _run(dbtool.consultar(base, "SELECT no_existe FROM t"))
    assert out.startswith("error de sqlite")
    assert "no_existe" in out


def test_archivo_inexistente_vuelve_como_texto(tmp_path):
    out = _run(dbtool.consultar(str(tmp_path / "nada.db"), "SELECT 1"))
    assert out.startswith("error:")
    assert "nada.db" in out


def test_sqlite_de_lectura_se_abre_en_modo_ro(base):
    """Defensa en profundidad: aunque el regex fallara, el driver no escribe.

    Se llama a `_sqlite` directo, saltando `exigir_lectura`, para probar
    la SEGUNDA barrera — la que de verdad garantiza. Si esto se rompe, un
    agujero en el regex se convierte en una tabla borrada.
    """
    with pytest.raises(sqlite3.OperationalError) as e:
        _run(dbtool._sqlite(base, "DELETE FROM t", [], solo_lectura=True))
    assert "readonly" in str(e.value).lower()
    conn = sqlite3.connect(base)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    conn.close()


def test_una_conexion_escribible_sqlite_si_escribe(base):
    """El permiso de la Admin UI tiene que ser verdad también para SQLite.

    Antes `_sqlite` abría siempre `mode=ro`, así que marcar `escribir=1`
    sobre un .db no servía de nada y el error no decía por qué.
    """
    out = _run(dbtool.consultar(base, "DELETE FROM t WHERE nombre='ana'",
                                solo_lectura=False))
    assert "filas_afectadas" in out and "1" in out
    conn = sqlite3.connect(base)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    conn.close()


def test_una_escritura_no_se_reporta_como_cero_filas(base):
    """`(0 filas)` después de un DELETE haría pensar que no pasó nada."""
    out = _run(dbtool.consultar(base, "DELETE FROM t", solo_lectura=False))
    assert "0 filas" not in out
    assert "3" in out


def test_cap_de_filas(base, monkeypatch):
    monkeypatch.setattr(dbtool, "MAX_ROWS", 2)
    out = _run(dbtool.consultar(base, "SELECT id FROM t ORDER BY id"))
    assert "cortado en 2" in out
    assert "LIMIT" in out          # le dice cómo achicar


def test_cap_de_chars(base, monkeypatch):
    monkeypatch.setattr(dbtool, "MAX_CHARS", 40)
    out = _run(dbtool.consultar(base, "SELECT id, nombre FROM t"))
    assert "recortado" in out


def test_los_saltos_de_linea_no_rompen_la_tabla(tmp_path):
    ruta = tmp_path / "nl.db"
    conn = sqlite3.connect(ruta)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('linea1\nlinea2')")
    conn.commit()
    conn.close()
    out = _run(dbtool.consultar(str(ruta), "SELECT x FROM t"))
    assert "⏎" in out
    # cabecera + separador + 1 fila + pie
    assert len([l for l in out.splitlines() if l.strip()]) == 4


# ---------- 6. resolución de alias ----------


class _DbFalsa:
    """Lo mínimo que `resolver` le pide a Database."""

    def __init__(self, filas=None):
        self._filas = filas or []
        self.path = "/tmp/relay-test.db"

    async def list_db_connections(self):
        return list(self._filas)

    async def get_db_connection(self, alias):
        for f in self._filas:
            if f["alias"] == alias:
                return f
        return None


def test_alias_relay_es_reservado_y_de_solo_lectura():
    """El experto puede mirar sus propios chats y veredictos, no tocarlos."""
    dsn, ro = _run(dbtool.resolver(_DbFalsa(), "relay",
                                   relay_db_path="/x/relay.db"))
    assert dsn == "/x/relay.db"
    assert ro is True


def test_el_alias_relay_no_se_puede_re_apuntar():
    """La RUTA gana siempre, aunque la fila diga otra cosa.

    Si el alias se pudiera mover, `db_query("relay", …)` terminaría en
    otra base y el experto creería estar mirando la suya.
    """
    falsa = _DbFalsa([{"alias": "relay", "dsn": "postgres://u:p@h/otra",
                       "escribir": 0}])
    dsn, _ro = _run(dbtool.resolver(falsa, "relay",
                                    relay_db_path="/x/relay.db"))
    assert dsn == "/x/relay.db"


def test_el_permiso_del_relay_si_es_configurable():
    """2026-08-16, a pedido: el humano puede habilitarle la escritura."""
    falsa = _DbFalsa([{"alias": "relay", "dsn": "/x/relay.db", "escribir": 1}])
    dsn, ro = _run(dbtool.resolver(falsa, "relay", relay_db_path="/x/relay.db"))
    assert dsn == "/x/relay.db"
    assert ro is False


def test_sin_fila_el_relay_sigue_siendo_de_lectura():
    """El default no cambió: hay que habilitarlo a mano."""
    _dsn, ro = _run(dbtool.resolver(_DbFalsa(), "relay",
                                    relay_db_path="/x/relay.db"))
    assert ro is True


def test_alias_registrado_devuelve_el_dsn_y_su_permiso():
    falsa = _DbFalsa([{"alias": "crm", "dsn": "postgres://u:p@h/crm",
                       "escribir": 0}])
    dsn, ro = _run(dbtool.resolver(falsa, "crm"))
    assert dsn == "postgres://u:p@h/crm" and ro is True


def test_alias_escribible_devuelve_solo_lectura_false():
    falsa = _DbFalsa([{"alias": "stage", "dsn": "postgres://u:p@h/s",
                       "escribir": 1}])
    _dsn, ro = _run(dbtool.resolver(falsa, "stage"))
    assert ro is False


@pytest.mark.parametrize("pegado", [
    "postgres://u:p@host/base",
    "sqlite:///home/x/otra.db",
    "/home/x/otra.db",
])
def test_dsn_pegado_en_el_chat_es_siempre_de_solo_lectura(pegado):
    """El pedido era que la cadena pueda venir del chat; que además pueda
    escribir, no. Para escribir hace falta un alias que el humano marcó."""
    dsn, ro = _run(dbtool.resolver(_DbFalsa(), pegado))
    assert dsn == pegado and ro is True


def test_alias_desconocido_lista_los_que_hay():
    falsa = _DbFalsa([{"alias": "crm", "dsn": "postgres://u:p@h/crm",
                       "escribir": 0}])
    with pytest.raises(dbtool.ConexionDesconocida) as e:
        _run(dbtool.resolver(falsa, "ventas"))
    msg = str(e.value)
    assert "ventas" in msg and "crm" in msg and "relay" in msg
    assert "u:p" not in msg        # ni siquiera al equivocarse se filtra


def test_nombre_vacio_es_error_claro():
    with pytest.raises(dbtool.ConexionDesconocida):
        _run(dbtool.resolver(_DbFalsa(), ""))


# ---------- 7. describir ----------


def test_describir_no_muestra_credenciales():
    salida = dbtool.describir([
        {"alias": "crm", "dsn": "postgres://u:secreto@h/crm",
         "escribir": 0, "descripcion": "CRM de producción"}])
    assert "secreto" not in salida
    assert "crm" in salida and "postgres" in salida and "lectura" in salida


def test_describir_siempre_incluye_relay():
    assert "relay" in dbtool.describir([])


def test_describir_vacio_explica_como_seguir():
    salida = dbtool.describir([])
    assert "cadena de conexión completa" in salida


def test_describir_avisa_del_dsn_aunque_haya_conexiones():
    """El aviso NO puede depender de que la tabla esté vacía.

    2026-08-17: con `relay` y nada más en la lista, el experto contestó
    "no tengo acceso a la base de SampleApp (Postgres)" y se detuvo — leyó
    el listado como el catálogo completo de lo que podía hacer. El aviso
    de que puede pasar la cadena entera tiene que estar siempre, y tiene
    que decir DÓNDE conseguirla, porque ese era el paso que no daba.
    """
    salida = dbtool.describir([
        {"alias": "crm", "dsn": "postgres://u:p@h/crm", "escribir": 0,
         "descripcion": "CRM"}])
    assert "cadena de conexión completa" in salida
    assert "appsettings.json" in salida and ".env" in salida
    assert "solo lectura" in salida


def test_alias_desconocido_dice_donde_buscar_la_cadena():
    """El error tampoco puede ser un callejón sin salida."""
    with pytest.raises(dbtool.ConexionDesconocida) as e:
        _run(dbtool.resolver(_DbFalsa(), "sample-app"))
    msg = str(e.value)
    assert "appsettings.json" in msg or "docker-compose" in msg


def test_describir_marca_las_escribibles():
    salida = dbtool.describir([
        {"alias": "stage", "dsn": "postgres://u:p@h/s", "escribir": 1,
         "descripcion": ""}])
    assert "escritura" in salida


# ---------- 8. endpoints de la Admin UI ----------
#
# El contrato que importa: `GET` NUNCA devuelve el DSN entero. Si eso se
# rompe, la contraseña de la base de un cliente sale por la API del
# relay y queda en el HTML de la Admin UI.


class EndpointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _client(self):
        from relay.server import create_app
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_get_no_devuelve_el_dsn_entero(self) -> None:
        await self.db.upsert_db_connection(
            "crm", "postgres://u:secreto@h:5432/crm", descripcion="CRM")
        client = await self._client()
        r = await client.get("/admin/api/db-connections")
        self.assertEqual(r.status, 200)
        cuerpo = await r.text()
        self.assertNotIn("secreto", cuerpo)
        # [0] es siempre `relay`; la registrada va después.
        fila = (await r.json())["connections"][1]
        self.assertEqual(fila["alias"], "crm")
        self.assertEqual(fila["motor"], "postgres")
        self.assertFalse(fila["escribir"])

    async def test_post_registra_y_delete_borra(self) -> None:
        client = await self._client()
        r = await client.post("/admin/api/db-connections", json={
            "alias": "crm", "dsn": "postgres://u:p@h/crm",
            "descripcion": "x", "escribir": True})
        self.assertEqual(r.status, 200)
        self.assertTrue((await r.json())["escribir"])
        self.assertIsNotNone(await self.db.get_db_connection("crm"))

        r = await client.delete("/admin/api/db-connections/crm")
        self.assertEqual(r.status, 200)
        self.assertIsNone(await self.db.get_db_connection("crm"))

    async def test_delete_de_lo_que_no_existe_es_404(self) -> None:
        client = await self._client()
        r = await client.delete("/admin/api/db-connections/nada")
        self.assertEqual(r.status, 404)

    async def test_el_alias_relay_no_se_puede_re_apuntar_por_api(self) -> None:
        """Se acepta el POST, pero el DSN que manden se descarta.

        Es la mitad reservada: el permiso se guarda, la ruta no. Si se
        pudiera mover, `db_query("relay", …)` iría a otra base sin que
        nadie lo note.
        """
        client = await self._client()
        r = await client.post("/admin/api/db-connections", json={
            "alias": "relay", "dsn": "postgres://u:p@h/x", "escribir": True})
        self.assertEqual(r.status, 200)
        fila = await self.db.get_db_connection("relay")
        self.assertNotIn("u:p@h", fila["dsn"])
        self.assertEqual(fila["dsn"], str(self.db.path))
        self.assertTrue(fila["escribir"])

    async def test_habilitar_la_escritura_del_relay_llega_al_resolver(self) -> None:
        """El camino entero: la UI marca el check y `db_query` puede escribir."""
        client = await self._client()
        await client.post("/admin/api/db-connections",
                          json={"alias": "relay", "escribir": True})
        _dsn, ro = await dbtool.resolver(self.db, "relay",
                                         relay_db_path=str(self.db.path))
        self.assertFalse(ro)

    async def test_relay_aparece_siempre_en_el_listado(self) -> None:
        """Tenga fila o no: la UI necesita mostrarlo para darle permiso."""
        client = await self._client()
        r = await client.get("/admin/api/db-connections")
        filas = (await r.json())["connections"]
        self.assertEqual(filas[0]["alias"], "relay")
        self.assertTrue(filas[0]["reservada"])
        self.assertFalse(filas[0]["escribir"])

    async def test_post_sin_alias_o_sin_dsn_es_400(self) -> None:
        client = await self._client()
        for cuerpo in ({"dsn": "postgres://u:p@h/x"}, {"alias": "x"}, {}):
            r = await client.post("/admin/api/db-connections", json=cuerpo)
            self.assertEqual(r.status, 400, cuerpo)

    async def test_test_de_una_conexion_sana(self) -> None:
        """El momento de descubrir que el DSN está mal es al cargarlo."""
        ruta = Path(self._tmp.name) / "ok.db"
        sqlite3.connect(ruta).close()
        await self.db.upsert_db_connection("demo", str(ruta))
        client = await self._client()
        r = await client.post("/admin/api/db-connections/demo/test")
        self.assertEqual(r.status, 200)
        cuerpo = await r.json()
        self.assertTrue(cuerpo["ok"])
        self.assertEqual(cuerpo["motor"], "sqlite")

    async def test_test_de_un_dsn_roto_avisa(self) -> None:
        await self.db.upsert_db_connection(
            "roto", str(Path(self._tmp.name) / "no-existe.db"))
        client = await self._client()
        r = await client.post("/admin/api/db-connections/roto/test")
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["ok"])

    async def test_test_de_un_alias_desconocido_es_404(self) -> None:
        client = await self._client()
        r = await client.post("/admin/api/db-connections/fantasma/test")
        self.assertEqual(r.status, 404)


# ---------- 9. las tools quedan colgadas del agente ----------
#
# Que `dbtool` funcione no sirve si el experto no ve las tools. Este es
# el cable, y ya se cortó una vez con las de archivo.


async def _tools_del_agente(proj, db):
    """Nombres de tools con los que se construye el Agent."""
    from unittest.mock import patch

    from pydantic_ai.models.test import TestModel

    from relay import experts

    capturado = {}
    _RealAgent = experts.Agent

    class _SpyAgent:
        def __init__(self, *a, **kw):
            capturado["kw"] = kw
            self._inner = _RealAgent(*a, **kw)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    with patch.object(experts, "Agent", _SpyAgent), \
         patch.object(experts, "build_model",
                      lambda s: TestModel(call_tools=[])), \
         patch.object(experts, "cbm_binary_path", lambda: None):
        await experts.run_expert(
            proj, "hola", db=db, model_override="minimax:MiniMax-M3",
            chat_id="c1", conversation_id="v1")

    nombres = set()
    for ts in capturado["kw"].get("toolsets") or []:
        inner = getattr(ts, "wrapped", ts)
        for t in getattr(inner, "tools", []) or []:
            nombres.add(getattr(t, "name", None) or getattr(t, "__name__", ""))
        d = getattr(inner, "_tools", None) or getattr(inner, "tools", None)
        if isinstance(d, dict):
            nombres |= set(d.keys())
    return nombres


def _proj(tmp, **extra):
    p = {"slug": "demo", "repo_path": str(tmp), "system_prompt": "p",
         "mcp_servers": [], "native_tools": [], "defaults_json": {}}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_las_tools_de_sql_llegan_al_agente(tmp_path):
    db = Database(path=tmp_path / "t.db")
    await db.init_schema()
    nombres = await _tools_del_agente(_proj(tmp_path), db)
    assert "db_query" in nombres
    assert "db_connections" in nombres


@pytest.mark.asyncio
async def test_se_pueden_apagar_por_proyecto(tmp_path):
    """Un proyecto que no toca bases no necesita las tools ocupando prompt."""
    db = Database(path=tmp_path / "t.db")
    await db.init_schema()
    nombres = await _tools_del_agente(
        _proj(tmp_path, defaults_json={"sql_tools": False}), db)
    assert "db_query" not in nombres
    assert "db_connections" not in nombres


# ---------- 10. editar el permiso sin tocar la cadena ----------


class PermisoSinDsnTests(unittest.IsolatedAsyncioTestCase):
    """El GET devuelve el DSN redactado; reenviarlo rompería la conexión.

    Por eso un POST sin `dsn` sobre un alias existente conserva la cadena
    guardada. Es lo que hace posible un toggle de permiso en la UI que no
    sea una trampa.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        from relay.server import create_app
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_post_sin_dsn_conserva_la_cadena(self) -> None:
        await self.db.upsert_db_connection(
            "crm", "postgres://u:secreto@host:5432/crm")
        r = await self.client.post("/admin/api/db-connections",
                                   json={"alias": "crm", "escribir": True})
        self.assertEqual(r.status, 200)
        fila = await self.db.get_db_connection("crm")
        self.assertEqual(fila["dsn"], "postgres://u:secreto@host:5432/crm")
        self.assertTrue(fila["escribir"])

    async def test_reenviar_el_dsn_redactado_seria_el_bug(self) -> None:
        """Lo que este diseño evita, escrito como test.

        Si la UI reenviara lo que el GET le dio, la conexión quedaría
        apuntando a `postgres://…@host/crm` — una cadena que no conecta
        con nada. Acá se comprueba que ese valor NUNCA llega a la DB por
        el camino normal del toggle.
        """
        await self.db.upsert_db_connection("crm", "postgres://u:p@host/crm")
        r = await self.client.get("/admin/api/db-connections")
        redactado = (await r.json())["connections"][1]["dsn"]
        self.assertIn("…", redactado)
        await self.client.post("/admin/api/db-connections",
                               json={"alias": "crm", "escribir": True})
        fila = await self.db.get_db_connection("crm")
        self.assertNotIn("…", fila["dsn"])

    async def test_post_sin_dsn_de_un_alias_nuevo_sigue_siendo_400(self) -> None:
        r = await self.client.post("/admin/api/db-connections",
                                   json={"alias": "nueva"})
        self.assertEqual(r.status, 400)


# ---------- 11. el interruptor de escritura por .env ----------


def test_env_habilita_la_escritura_del_relay(monkeypatch):
    """El toggle de la UI vive en la DB; una DB nueva vuelve a lectura.

    Quien ya decidió que quiere escribir no debería tener que volver a
    decidirlo en cada máquina o cada reset.
    """
    monkeypatch.setenv("FOURBIS_SQL_RELAY_WRITE", "1")
    _dsn, ro = _run(dbtool.resolver(_DbFalsa(), "relay",
                                    relay_db_path="/x/relay.db"))
    assert ro is False


@pytest.mark.parametrize("valor", ["1", "on", "true", "yes", "si", "SÍ"])
def test_el_env_acepta_las_formas_razonables(monkeypatch, valor):
    monkeypatch.setenv("FOURBIS_SQL_RELAY_WRITE", valor)
    assert _run(dbtool.resolver(_DbFalsa(), "relay",
                                relay_db_path="/x"))[1] is False


@pytest.mark.parametrize("valor", ["", "0", "off", "false", "no", "cualquiera"])
def test_lo_que_no_es_un_si_explicito_deja_la_lectura(monkeypatch, valor):
    """Ante la duda no se abre la escritura: un typo no debería."""
    monkeypatch.setenv("FOURBIS_SQL_RELAY_WRITE", valor)
    assert _run(dbtool.resolver(_DbFalsa(), "relay",
                                relay_db_path="/x"))[1] is True


def test_el_env_no_toca_las_demas_conexiones(monkeypatch):
    """Es el interruptor de `relay`, no un 'escribí donde quieras'."""
    monkeypatch.setenv("FOURBIS_SQL_RELAY_WRITE", "1")
    falsa = _DbFalsa([{"alias": "crm", "dsn": "postgres://u:p@h/crm",
                       "escribir": 0}])
    assert _run(dbtool.resolver(falsa, "crm"))[1] is True


def test_el_env_no_mueve_la_ruta(monkeypatch):
    monkeypatch.setenv("FOURBIS_SQL_RELAY_WRITE", "1")
    dsn, _ro = _run(dbtool.resolver(_DbFalsa(), "relay",
                                    relay_db_path="/x/relay.db"))
    assert dsn == "/x/relay.db"
