"""Test del sync contra el CRM local (trycompai/crm).

`crm.sync_once` hace 3 queries fijas y agrupa contactos/deals por empresa
en memoria. Lo que se testea acá es esa agrupación + el upsert en SQLite,
con una conexión falsa: montar un Postgres para el suite ataría los tests
a Docker. La forma real de las queries se valida corriendo
`python -m relay.crm` contra el CRM de verdad.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from relay import crm as crm_mod  # noqa: E402
from relay import db as db_mod  # noqa: E402

# ---- Fake del Postgres del CRM ------------------------------------------

_NOW = datetime.now(timezone.utc)

COMPANIES = [
    {"id": "cmp_111", "name": "Acme Corp", "domain": "acme.test",
     "website": None, "industry": "SaaS", "city": None, "country": "CL",
     "lastActivityAt": _NOW - timedelta(days=3)},
    {"id": "cmp_222", "name": "Globex SA", "domain": "globex.test",
     "website": None, "industry": None, "city": None, "country": None,
     "lastActivityAt": None},
]

CONTACTS = [
    {"id": "c1", "firstName": "Jane", "lastName": "AcmeBoss",
     "email": "jane@acme.test", "phone": None, "title": "CTO",
     "companyId": "cmp_111"},
    {"id": "c2", "firstName": "Bob", "lastName": "GlobexBuyer",
     "email": "bob@globex.test", "phone": None, "title": None,
     "companyId": "cmp_222"},
]

DEALS = [
    {"id": "d1", "name": "Onboarding Acme", "stage": "QUALIFIED_TO_BUY",
     "amount": Decimal("5000.00"), "currency": "USD",
     "expectedCloseDate": None, "companyId": "cmp_111"},
    {"id": "d2", "name": "Pilot Globex", "stage": "DEMO_BOOKED",
     "amount": Decimal("12000.00"), "currency": "CLP",
     "expectedCloseDate": None, "companyId": "cmp_222"},
]


class _FakeConn:
    """asyncpg.Connection mínima: enruta por la tabla que nombra el SQL."""

    def __init__(self, *, fail: bool = False, companies=None) -> None:
        self.fail = fail
        self.companies = COMPANIES if companies is None else companies
        self.closed = False

    async def fetch(self, sql: str):
        if self.fail:
            raise crm_mod.asyncpg.PostgresError("relation does not exist")
        if "FROM company" in sql:
            return list(self.companies)
        if "FROM contact" in sql:
            return list(CONTACTS)
        if "FROM deal" in sql:
            return list(DEALS)
        raise AssertionError(f"query inesperada: {sql}")

    async def fetchrow(self, sql: str):
        return {"companies": len(COMPANIES), "contacts": len(CONTACTS),
                "deals": len(DEALS)}

    async def fetchval(self, sql: str):
        return COMPANIES[0]["name"]

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_crm(monkeypatch):
    """Parcha `_connect` para devolver la conexión falsa."""
    conns: list[_FakeConn] = []

    async def _fake_connect(dsn=None):
        c = _FakeConn()
        conns.append(c)
        return c

    monkeypatch.setattr(crm_mod, "_connect", _fake_connect)
    return conns


@pytest.fixture
def tmp_db(tmp_path):
    """DB SQLite fresca en tmp_path."""
    path = tmp_path / "test-crm.sqlite"
    db = db_mod.Database(path)
    asyncio.run(db.init_schema())
    yield db


async def test_sync_once_ok(fake_crm, tmp_db):
    """Sync completo: 2 empresas, 2 deals, 2 contactos en DB."""
    stats = await crm_mod.sync_once(tmp_db)
    assert stats == {
        "companies": 2, "deals": 2, "contacts": 2, "errors": 0,
        "removed": 0, "gone": 0,
    }, stats

    clients = await tmp_db.list_crm_clients()
    assert len(clients) == 2
    by_ext = {c["ext_id"]: c for c in clients}
    assert by_ext["cmp_111"]["name"] == "Acme Corp"
    assert by_ext["cmp_222"]["domain"] == "globex.test"
    # Deal del cliente correcto, con el stage del CRM y amount serializable.
    d1 = next(d for d in by_ext["cmp_111"]["deals"] if d["id"] == "d1")
    assert d1["name"] == "Onboarding Acme"
    assert d1["stage"] == "QUALIFIED_TO_BUY"
    assert d1["amount"] == "5000.00"  # Decimal → str vía json_dumps
    # Cada contacto queda bajo SU empresa, con nombre armado.
    c1 = next(c for c in by_ext["cmp_111"]["contacts"] if c["id"] == "c1")
    assert c1["name"] == "Jane AcmeBoss"
    assert [c["id"] for c in by_ext["cmp_222"]["contacts"]] == ["c2"]
    assert all(c["last_sync_status"] == "ok" for c in clients)
    # last_activity_at: ISO string cuando el CRM lo tiene, None si nunca.
    assert by_ext["cmp_111"]["last_activity_at"] is not None
    assert by_ext["cmp_222"]["last_activity_at"] is None
    # La conexión se cierra aunque la query haya ido bien.
    assert all(c.closed for c in fake_crm)


async def test_sync_once_crm_caido(monkeypatch, tmp_db):
    """CRM apagado → CrmError antes de tocar la DB del relay."""
    async def _boom(dsn=None):
        raise crm_mod.CrmError("no se pudo conectar", 503)

    monkeypatch.setattr(crm_mod, "_connect", _boom)
    with pytest.raises(crm_mod.CrmError) as ei:
        await crm_mod.sync_once(tmp_db)
    assert ei.value.status_code == 503
    assert await tmp_db.list_crm_clients() == []


async def test_sync_once_schema_sin_migrar(monkeypatch, tmp_db):
    """Postgres arriba pero sin las tablas → CrmError 503, DB intacta."""
    async def _fake_connect(dsn=None):
        return _FakeConn(fail=True)

    monkeypatch.setattr(crm_mod, "_connect", _fake_connect)
    with pytest.raises(crm_mod.CrmError):
        await crm_mod.sync_once(tmp_db)
    assert await tmp_db.list_crm_clients() == []


async def test_check_devuelve_conteos(fake_crm):
    r = await crm_mod.check()
    assert r["ok"] is True
    assert r["companies"] == 2
    assert r["sample_company"] == "Acme Corp"


async def test_upsert_idempotente(fake_crm, tmp_db):
    """Sync dos veces no duplica filas; actualiza last_sync_at."""
    await crm_mod.sync_once(tmp_db)
    first = await tmp_db.list_crm_clients()
    await asyncio.sleep(0.01)  # que cambie el timestamp
    await crm_mod.sync_once(tmp_db)
    second = await tmp_db.list_crm_clients()
    assert len(first) == len(second) == 2
    assert second[0]["last_sync_at"] >= first[0]["last_sync_at"]


async def test_prune_borra_lo_que_ya_no_esta_pero_respeta_vinculos(
    monkeypatch, tmp_db, tmp_path,
):
    """Borrado en el CRM: se va del snapshot, salvo que tenga proyectos.

    Sin esto el sync solo hacía upsert y los clientes borrados en el CRM
    quedaban para siempre en la tabla del relay.
    """
    def _serve(companies):
        async def _connect(dsn=None):
            return _FakeConn(companies=companies)
        monkeypatch.setattr(crm_mod, "_connect", _connect)

    _serve(COMPANIES)
    await crm_mod.sync_once(tmp_db)
    clients = {c["ext_id"]: c["id"] for c in await tmp_db.list_crm_clients()}

    # Globex queda vinculado a un proyecto; Acme no lo apunta nadie.
    await tmp_db.upsert_project({
        "slug": "inventorydemo", "name": "INVENTORYDEMO", "repo_path": str(tmp_path),
        "system_prompt": "x", "mcp_servers": [],
    })
    await tmp_db.set_project_client(project_slug="inventorydemo",
                                    client_id=clients["cmp_222"])

    # Segunda sync: en el CRM ya solo existe Acme.
    _serve([COMPANIES[0]])
    stats = await crm_mod.sync_once(tmp_db)

    assert stats["removed"] == 0, "Acme sigue en el CRM, no se toca"
    assert stats["gone"] == 1, "Globex ya no está y tiene proyecto → marcado"

    rows = {c["ext_id"]: c for c in await tmp_db.list_crm_clients()}
    assert rows["cmp_222"]["last_sync_status"] == "gone"
    # El proyecto NO se desvinculó: borrar la fila lo habría puesto en NULL.
    assert rows["cmp_222"]["project_count"] == 1

    # Y ahora al revés: se desvincula y la próxima sync sí lo borra.
    await tmp_db.set_project_client(project_slug="inventorydemo", client_id=None)
    stats = await crm_mod.sync_once(tmp_db)
    assert stats["removed"] == 1
    assert [c["ext_id"] for c in await tmp_db.list_crm_clients()] == ["cmp_111"]


async def test_prune_no_vacia_el_snapshot_si_el_crm_vuelve_vacio(
    monkeypatch, tmp_db,
):
    """CRM sin empresas es indistinguible de un sync roto: no borrar nada."""
    async def _connect(dsn=None):
        return _FakeConn()
    monkeypatch.setattr(crm_mod, "_connect", _connect)
    await crm_mod.sync_once(tmp_db)

    async def _empty(dsn=None):
        return _FakeConn(companies=[])
    monkeypatch.setattr(crm_mod, "_connect", _empty)
    stats = await crm_mod.sync_once(tmp_db)

    assert stats["removed"] == 0
    assert len(await tmp_db.list_crm_clients()) == 2


def test_dsn_limpia_el_schema_de_prisma():
    """`?schema=public` es sintaxis de Prisma; libpq la rechaza."""
    assert crm_mod._clean(
        "postgresql://u:p@localhost:5434/crm?schema=public"
    ) == "postgresql://u:p@localhost:5434/crm"


# ---------- digest de salud (idea "silencio" + "resumen") ----------


def test_days_since_none_si_nunca_hubo_actividad():
    assert crm_mod.days_since(None) is None


def test_days_since_cuenta_dias_completos():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    hace_5d = (now - timedelta(days=5)).isoformat()
    assert crm_mod.days_since(hace_5d, now=now) == 5


def test_days_since_acepta_naive_como_utc():
    """El CRM guarda TIMESTAMPTZ; si algo llega sin tz no debe explotar."""
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    naive = (now - timedelta(days=2)).replace(tzinfo=None).isoformat()
    assert crm_mod.days_since(naive, now=now) == 2


def test_render_digest_sin_clientes():
    assert "sin clientes" in crm_mod.render_digest([], stale_days=14)


def test_render_digest_ordena_por_mas_silencio_primero():
    rows = [
        {"name": "Al día", "domain": "x.cl", "days_silent": 1,
         "project_count": 1, "deals": [], "open_issues": 0, "open_prs": 0},
        {"name": "Nunca", "domain": "y.cl", "days_silent": None,
         "project_count": 1, "deals": [], "open_issues": None, "open_prs": None},
        {"name": "Silencioso", "domain": "z.cl", "days_silent": 20,
         "project_count": 1, "deals": [], "open_issues": 2, "open_prs": 1},
    ]
    text = crm_mod.render_digest(rows, stale_days=14)
    # "Nunca" (sin dato, lo peor) y "Silencioso" (20d) van antes que "Al día".
    assert text.index("**Nunca**") < text.index("**Al día**")
    assert text.index("**Silencioso**") < text.index("**Al día**")
    assert "2 sin contacto hace 14+ días" in text
    assert "🔴" in text and "🟢" in text


def test_render_digest_distingue_sin_leer_github_de_cero_abiertos():
    rows = [
        {"name": "SinGh", "domain": "a.cl", "days_silent": 1,
         "project_count": 1, "deals": [], "open_issues": None, "open_prs": None},
        {"name": "ConGh", "domain": "b.cl", "days_silent": 1,
         "project_count": 1, "deals": [], "open_issues": 0, "open_prs": 0},
    ]
    text = crm_mod.render_digest(rows, stale_days=14)
    assert "issues" not in text.split("**SinGh**")[1].split("**ConGh**")[0]
    assert "0 issues, 0 PR" in text


# ---- levantar el stack del CRM (botón "Levantar CRM" del tab) -----------


@pytest.mark.asyncio
async def test_app_up_detecta_puerto_abierto_y_cerrado(monkeypatch):
    """`app_up` es lo que decide si mostrar el botón: `check()` mira el
    Postgres, que sigue arriba con el dev server apagado."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("CRM_APP_URL", f"http://127.0.0.1:{port}")
    assert await crm_mod.app_up() is True
    server.close()
    await server.wait_closed()
    assert await crm_mod.app_up() is False


@pytest.mark.asyncio
async def test_start_stack_falla_claro_si_no_esta_el_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("CRM_REPO_PATH", str(tmp_path / "no-existe"))
    with pytest.raises(crm_mod.CrmError) as exc:
        await crm_mod.start_stack()
    assert exc.value.status_code == 400
    assert "CRM_REPO_PATH" in exc.value.message


@pytest.mark.asyncio
async def test_start_stack_larga_bun_dev_detached(monkeypatch, tmp_path):
    """No se spawnea nada real: se verifica que el dev server salga con el
    cwd del repo y que el `docker start` de Postgres vaya primero."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CRM_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("FOURBIS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(crm_mod, "_dev_proc", None)
    monkeypatch.setattr(crm_mod, "_SETTLE_S", 0)
    calls = []

    class _Fake:
        returncode = None
        pid = 4242

        async def communicate(self):
            return (b"crm-postgres\n", b"")

    async def _spawn(*args, **kwargs):
        calls.append((args, kwargs.get("cwd")))
        fake = _Fake()
        if args[0] == "docker":
            fake.returncode = 0
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    r = await crm_mod.start_stack()
    assert r["started"] and r["pid"] == 4242
    assert calls[0][0][:2] == ("docker", "start")
    assert "bun" in calls[1][0] and calls[1][1] == str(tmp_path)
