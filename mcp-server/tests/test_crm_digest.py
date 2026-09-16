"""Tests del digest de salud de clientes (silencio + resumen GitHub).

GET /admin/api/crm/health y POST /admin/api/crm/digest. La idea: de los
clientes con proyecto vinculado, ¿a cuáles no les hablamos hace rato?
`github_mod` se mockea (sin `gh` real ni git en disco) y `notify.send`
se reemplaza por un espía después de `start_server()` — el real le
pegaría a un bot que no está corriendo en tests, con reintentos que
harían la suite lenta.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from relay import github as github_mod
from relay.db import Database
from relay.server import DB_KEY, NOTIFY_KEY, create_app

_NOW = datetime.now(timezone.utc)


class _FakeNotify:
    """Espía de NotifyClient: guarda cada llamada, nunca pega a la red."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, agent_id, kind, message, metadata=None):
        self.calls.append({"agent_id": agent_id, "kind": kind,
                           "message": message, "metadata": metadata or {}})
        return True

    async def aclose(self) -> None:
        pass  # el real cierra un httpx.AsyncClient; acá no hay nada que cerrar


@pytest.fixture
async def env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        (state / "prompts").mkdir(parents=True)
        env_vars = {
            "STATE_DIR": str(state),
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "FOURBIS_MODEL": "test",
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            fake_notify = _FakeNotify()
            cli.app[NOTIFY_KEY] = fake_notify  # reemplaza al real post-boot
            db: Database = app[DB_KEY]

            # Cliente silencioso: última actividad hace 30 días, 1 proyecto.
            await db.upsert_project({
                "slug": "aurorademo-portal", "name": "AuroraDemo Portal",
                "repo_path": str(Path(tmp) / "aurorademo-portal"),
                "system_prompt": "x", "mcp_servers": [],
            })
            silent_id = await db.upsert_crm_client(
                ext_id="cmp_aurorademo", name="AuroraDemo", domain="aurora.example.test",
                contacts_json=json.dumps([]),
                deals_json=json.dumps([
                    {"id": "deal-aurorademo", "name": "AuroraDemo Portal",
                     "stage": "CLOSED_WON"},
                ]),
                last_activity_at=(_NOW - timedelta(days=30)).isoformat(),
            )
            await db.set_project_deal(project_slug="aurorademo-portal",
                                      deal_id="deal-aurorademo", client_id=silent_id)

            # Cliente al día: actividad hace 1 día, 1 proyecto.
            await db.upsert_project({
                "slug": "example-client-pms", "name": "Example Client PMS",
                "repo_path": str(Path(tmp) / "example-client-pms"),
                "system_prompt": "x", "mcp_servers": [],
            })
            fresh_id = await db.upsert_crm_client(
                ext_id="cmp_example", name="Example Client", domain="example.com",
                contacts_json=json.dumps([]),
                deals_json=json.dumps([
                    {"id": "deal-example", "name": "Example Client PMS",
                     "stage": "CLOSED_WON"},
                ]),
                last_activity_at=(_NOW - timedelta(days=1)).isoformat(),
            )
            await db.set_project_deal(project_slug="example-client-pms",
                                      deal_id="deal-example", client_id=fresh_id)

            # Cliente sin proyecto vinculado: NO debe aparecer en el digest
            # (es alguien que apareció en un email una vez, no un cliente).
            await db.upsert_crm_client(
                ext_id="cmp_random", name="Alguien random",
                domain="random.test", contacts_json=json.dumps([]),
                deals_json=json.dumps([]), last_activity_at=None,
            )

            try:
                yield cli, db, fake_notify, silent_id, fresh_id
            finally:
                await cli.close()


@pytest.fixture(autouse=True)
def _mock_github(monkeypatch):
    """Sin `gh` ni git real: aurorademo-portal "tiene repo" con 2 issues + 1 PR
    abiertos; example-client-pms no tiene repo (simula "sin git", `repo_slug`
    devuelve None y el cliente queda con open_issues=None)."""
    async def _repo_slug(repo_path: str):
        return "AuroraDemo/aurorademo-portal" if "aurorademo" in repo_path else None

    async def _issues(slug, limit=20):
        return [{"number": 1}, {"number": 2}]

    async def _pulls(slug, limit=20):
        return [{"number": 9}]

    monkeypatch.setattr(github_mod, "repo_slug", _repo_slug)
    monkeypatch.setattr(github_mod, "issues", _issues)
    monkeypatch.setattr(github_mod, "pulls", _pulls)


# ---------- GET /admin/api/crm/health ----------


async def test_health_solo_incluye_clientes_con_proyecto(env):
    cli, *_ = env
    r = await cli.get("/admin/api/crm/health")
    assert r.status == 200
    body = await r.json()
    names = {c["name"] for c in body["clients"]}
    assert names == {"AuroraDemo", "Example Client"}  # "Alguien random" queda afuera


async def test_health_marca_stale_por_dias_configurables(env):
    cli, *_ = env
    r = await cli.get("/admin/api/crm/health?stale_days=7")
    body = await r.json()
    by_name = {c["name"]: c for c in body["clients"]}
    assert by_name["AuroraDemo"]["days_silent"] == 30
    assert by_name["Example Client"]["days_silent"] == 1
    assert body["stale_count"] == 1
    assert body["stale_days"] == 7
    # aurorademo tiene repo → conteo real; example-client no tiene repo → None, no 0
    # (0 diría "sin issues abiertos", que es una afirmación distinta a
    # "no se pudo leer GitHub").
    assert by_name["AuroraDemo"]["open_issues"] == 2
    assert by_name["AuroraDemo"]["open_prs"] == 1
    assert by_name["Example Client"]["open_issues"] is None
    assert by_name["Example Client"]["open_prs"] is None


async def test_health_stale_days_no_entero_es_400(env):
    cli, *_ = env
    r = await cli.get("/admin/api/crm/health?stale_days=abc")
    assert r.status == 400


# ---------- POST /admin/api/crm/digest ----------


async def test_digest_dry_run_no_llama_a_notify(env):
    cli, _, fake_notify, *_ = env
    r = await cli.post("/admin/api/crm/digest", json={"dry_run": True})
    assert r.status == 200
    body = await r.json()
    assert body["dry_run"] is True
    assert body["sent"] is False
    assert "AuroraDemo" in body["text"]
    assert fake_notify.calls == []


async def test_digest_envia_a_discord_con_el_texto(env):
    cli, _, fake_notify, *_ = env
    r = await cli.post("/admin/api/crm/digest",
                       json={"stale_days": 7, "channel": "#clientes"})
    assert r.status == 200
    body = await r.json()
    assert body["sent"] is True
    assert body["stale_count"] == 1
    assert len(fake_notify.calls) == 1
    call = fake_notify.calls[0]
    assert call["metadata"]["discord_channel"] == "#clientes"
    assert "AuroraDemo" in call["message"]
    assert call["kind"] == "progress"


async def test_digest_default_usa_stale_days_y_canal_por_defecto(env):
    cli, _, fake_notify, *_ = env
    r = await cli.post("/admin/api/crm/digest")
    assert r.status == 200
    body = await r.json()
    assert body["stale_days"] == 14  # CRM_STALE_DAYS default
    assert fake_notify.calls[0]["metadata"]["discord_channel"] == "#equipo-demo"


# ---------- GET /admin/api/crm/clients expone last_activity_at ----------
# El badge de silencio en la lista (tab-crm.js) se calcula client-side a
# partir de este campo SIN pegarle a /crm/health (que spawnea `gh`). Si
# el handler de /crm/clients lo pierde al armar el JSON, el badge queda
# mudo en TODOS los clientes sin que ningún test de la capa DB lo note
# (list_crm_clients() sí lo trae; el bug estaba en el handler HTTP).


async def test_clients_list_expone_last_activity_at(env):
    cli, _, _, silent_id, fresh_id = env
    r = await cli.get("/admin/api/crm/clients")
    assert r.status == 200
    by_id = {c["id"]: c for c in (await r.json())["clients"]}
    assert by_id[silent_id]["last_activity_at"] is not None
    assert by_id[fresh_id]["last_activity_at"] is not None
