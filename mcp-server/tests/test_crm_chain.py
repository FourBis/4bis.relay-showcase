"""Tests de la cadena CRM: cliente → proyecto → git → kanban → issues/PR.

Contexto: la columna `projects.client_id`, su FK y `db.set_project_client()`
existían desde F1, pero NADA los alcanzaba — ni endpoint ni UI. El
eslabón cliente→proyecto estaba roto y con él toda la cadena, porque los
demás eslabones (repo_path, defaults_json.github_project, el panel de
GitHub) ya funcionaban pero colgaban de un proyecto que nunca sabía de
quién era.

Cubre:
  - PUT /admin/api/projects/{slug}/client: vincular, desvincular,
    cliente inexistente, proyecto inexistente, body inválido.
  - GET /admin/api/crm/clients/{cid}: cliente + sus proyectos con los
    datos de git y kanban para seguir bajando.
  - GET /admin/api/projects: expone client_id/client_name.
  - project_count de /admin/api/crm/clients refleja el vínculo.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay.server import DB_KEY, create_app


@pytest.fixture
async def env():
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
            db: Database = app[DB_KEY]
            await db.upsert_project({
                "slug": "inventorydemo", "name": "INVENTORYDEMO", "repo_path": str(tmp),
                "system_prompt": "x", "mcp_servers": [],
                "defaults_json": {
                    "github_project": {"owner": "AuroraDemo", "number": 7},
                },
            })
            await db.upsert_project({
                "slug": "sample-app", "name": "SampleApp", "repo_path": str(tmp),
                "system_prompt": "x", "mcp_servers": [],
            })
            cid = await db.upsert_crm_client(
                ext_id="cmp_acme", name="Acme SA", domain="acme.cl",
                    contacts_json=json.dumps([{"email": "contact@example.test"}]),
                deals_json=json.dumps([
                    {"id": "deal_acme_1", "name": "Portal Acme",
                     "stage": "CLOSED_WON", "amount": 1000},
                ]),
            )
            try:
                yield cli, db, cid
            finally:
                await cli.close()


# ---------- PUT /admin/api/projects/{slug}/client ----------


async def test_link_client_to_project(env):
    cli, db, cid = env
    r = await cli.put("/admin/api/projects/inventorydemo/client",
                      json={"client_id": cid})
    assert r.status == 200
    body = await r.json()
    assert body["client_id"] == cid
    assert body["client_name"] == "Acme SA"

    project = await db.get_project("inventorydemo")
    assert project["client_id"] == cid


async def test_unlink_client_with_null(env):
    cli, db, cid = env
    await cli.put("/admin/api/projects/inventorydemo/client", json={"client_id": cid})
    r = await cli.put("/admin/api/projects/inventorydemo/client",
                      json={"client_id": None})
    assert r.status == 200
    assert (await r.json())["client_id"] is None
    project = await db.get_project("inventorydemo")
    assert project["client_id"] is None


async def test_unlink_with_empty_body(env):
    cli, db, cid = env
    await cli.put("/admin/api/projects/inventorydemo/client", json={"client_id": cid})
    r = await cli.put("/admin/api/projects/inventorydemo/client", json={})
    assert r.status == 200
    assert (await r.json())["client_id"] is None


async def test_link_nonexistent_client_is_404(env):
    """Se valida contra la tabla: un id inventado dejaría el proyecto
    apuntando a la nada sin que nadie se entere."""
    cli, db, _ = env
    r = await cli.put("/admin/api/projects/inventorydemo/client",
                      json={"client_id": 99999})
    assert r.status == 404
    assert "99999" in (await r.json())["error"]
    project = await db.get_project("inventorydemo")
    assert project["client_id"] is None


async def test_link_on_nonexistent_project_is_404(env):
    cli, _, cid = env
    r = await cli.put("/admin/api/projects/no-existe/client",
                      json={"client_id": cid})
    assert r.status == 404


async def test_link_with_non_integer_is_400(env):
    cli, _, _ = env
    r = await cli.put("/admin/api/projects/inventorydemo/client",
                      json={"client_id": "no-soy-un-numero"})
    assert r.status == 400


# ---------- GET /admin/api/crm/clients/{cid} — la cadena ----------


async def test_client_detail_returns_chain(env):
    """Un solo GET devuelve cliente → proyectos → git + kanban."""
    cli, db, cid = env
    await cli.put("/admin/api/projects/inventorydemo/client", json={"client_id": cid})

    r = await cli.get(f"/admin/api/crm/clients/{cid}")
    assert r.status == 200
    body = await r.json()

    assert body["client"]["name"] == "Acme SA"
    assert body["client"]["deals"][0]["stage"] == "CLOSED_WON"

    slugs = [p["slug"] for p in body["projects"]]
    assert slugs == ["inventorydemo"]
    inventorydemo = body["projects"][0]
    # Eslabón git.
    assert "repo_path" in inventorydemo and "has_git" in inventorydemo
    # Eslabón kanban: la entrada a issues/PRs.
    assert inventorydemo["github_project"] == {"owner": "AuroraDemo", "number": 7}


async def test_client_detail_without_projects(env):
    cli, _, cid = env
    r = await cli.get(f"/admin/api/crm/clients/{cid}")
    assert r.status == 200
    body = await r.json()
    assert body["projects"] == []


async def test_client_detail_404(env):
    cli, _, _ = env
    r = await cli.get("/admin/api/crm/clients/99999")
    assert r.status == 404


async def test_client_detail_bad_id_is_400(env):
    cli, _, _ = env
    r = await cli.get("/admin/api/crm/clients/abc")
    assert r.status in (400, 404)


# ---------- listados ----------


async def test_projects_list_exposes_client(env):
    """El listado descartaba client_id, así que la grid no podía mostrar
    de quién era cada proyecto."""
    cli, _, cid = env
    await cli.put("/admin/api/projects/inventorydemo/client", json={"client_id": cid})

    r = await cli.get("/admin/api/projects")
    assert r.status == 200
    rows = {p["slug"]: p for p in (await r.json())["projects"]}
    assert rows["inventorydemo"]["client_id"] == cid
    assert rows["inventorydemo"]["client_name"] == "Acme SA"
    # El que no está vinculado viaja explícito en null, no ausente.
    assert rows["sample-app"]["client_id"] is None
    assert rows["sample-app"]["client_name"] is None


async def test_crm_clients_project_count_tracks_link(env):
    cli, _, cid = env
    r = await cli.get("/admin/api/crm/clients")
    assert (await r.json())["clients"][0]["project_count"] == 0

    await cli.put("/admin/api/projects/inventorydemo/client", json={"client_id": cid})
    await cli.put("/admin/api/projects/sample-app/client", json={"client_id": cid})

    r = await cli.get("/admin/api/crm/clients")
    assert (await r.json())["clients"][0]["project_count"] == 2


# ---------- PUT /admin/api/projects/{slug}/deal ----------
# Un proyecto ES un deal, y el deal cuelga de una company: vincular el
# deal tiene que arrastrar el cliente solo, sin que nadie lo mande.


async def test_link_deal_sets_client_from_the_deal(env):
    cli, db, cid = env
    r = await cli.put("/admin/api/projects/inventorydemo/deal",
                      json={"deal_id": "deal_acme_1"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["deal_id"] == "deal_acme_1"
    assert body["deal_name"] == "Portal Acme"
    # El cliente NO se mandó en el body: sale del dueño del deal.
    assert body["client_id"] == cid

    project = await db.get_project("inventorydemo")
    assert project["deal_id"] == "deal_acme_1"
    assert project["client_id"] == cid


async def test_link_deal_inexistente_es_404_y_no_toca_nada(env):
    cli, db, _ = env
    r = await cli.put("/admin/api/projects/inventorydemo/deal",
                      json={"deal_id": "deal_que_no_existe"})
    assert r.status == 404
    project = await db.get_project("inventorydemo")
    assert project["deal_id"] is None
    assert project["client_id"] is None


async def test_unlink_deal_conserva_el_cliente(env):
    """Se puede saber de quién es un proyecto sin haber cerrado la venta."""
    cli, db, cid = env
    await cli.put("/admin/api/projects/inventorydemo/deal", json={"deal_id": "deal_acme_1"})

    r = await cli.put("/admin/api/projects/inventorydemo/deal", json={"deal_id": None})
    assert r.status == 200
    project = await db.get_project("inventorydemo")
    assert project["deal_id"] is None
    assert project["client_id"] == cid


async def test_detail_expone_el_deal_de_cada_proyecto(env):
    cli, _, cid = env
    await cli.put("/admin/api/projects/inventorydemo/deal", json={"deal_id": "deal_acme_1"})

    r = await cli.get(f"/admin/api/crm/clients/{cid}")
    assert r.status == 200
    projects = {p["slug"]: p for p in (await r.json())["projects"]}
    assert projects["inventorydemo"]["deal_id"] == "deal_acme_1"
    assert projects["inventorydemo"]["deal_name"] == "Portal Acme"
    assert projects["inventorydemo"]["deal_stage"] == "CLOSED_WON"
