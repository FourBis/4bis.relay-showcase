"""Tests del seguimiento por GitHub (fases 0-2 del plan).

Fase 0 — relay/github.py: caché, degradación sin `gh`, parseo del remote
         y aplanado de los items del tablero.
Fase 1 — GET /admin/api/projects/{slug}/github: shape estable y
         `configured:false` en vez de 500 cuando no hay nada.

Nunca se llama al `gh` real: todo pasa por `_gh` faketeado. Un test que
dependa de la red (o del token del que corre la suite) no sirve de check.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_github_panel.py -q
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from relay import github
from relay.db import Database
from relay.server import DB_KEY, create_app


@pytest.fixture(autouse=True)
def _sin_cache():
    github.clear_cache()
    yield
    github.clear_cache()


# ---------- fase 0: caché y degradación ----------


async def test_cachea_y_no_respawnea():
    """Cada llamada a `gh` es un spawn (~200ms en Windows: cargar la
    imagen del exe). Dos lecturas del mismo panel = un solo spawn."""
    llamadas = []

    async def fake_gh(*args, cwd=None):
        llamadas.append(args)
        return 0, '[{"number": 1, "title": "algo"}]'

    with patch.object(github, "_gh", fake_gh):
        a = await github.issues("Foo/bar")
        b = await github.issues("Foo/bar")
    assert a == b == [{"number": 1, "title": "algo"}]
    assert len(llamadas) == 1


async def test_ttl_vencido_vuelve_a_consultar():
    llamadas = []

    async def fake_gh(*args, cwd=None):
        llamadas.append(args)
        return 0, "[]"

    with patch.object(github, "_gh", fake_gh), \
            patch.object(github, "CACHE_TTL_S", -1):
        await github.issues("Foo/bar")
        await github.issues("Foo/bar")
    assert len(llamadas) == 2


async def test_sin_gh_devuelve_none_y_no_reintenta():
    """`gh` ausente o sin auth NO puede romper el panel — y el fallo se
    cachea, si no cada refresh reintenta un spawn que ya sabemos que falla."""
    llamadas = []

    async def fake_gh(*args, cwd=None):
        llamadas.append(args)
        return 127, "gh spawn falló: no existe"

    with patch.object(github, "_gh", fake_gh):
        assert await github.issues("Foo/bar") is None
        assert await github.issues("Foo/bar") is None
    assert len(llamadas) == 1


async def test_salida_no_json_no_explota():
    async def fake_gh(*args, cwd=None):
        return 0, "esto no es json"

    with patch.object(github, "_gh", fake_gh):
        assert await github.pulls("Foo/bar") is None


# ---------- fase 0: parseo del remote ----------


@pytest.mark.parametrize("url,esperado", [
    ("git@github.com:AuroraDemo/website-demo.git", "AuroraDemo/website-demo"),
    ("https://github.com/AuroraDemo/website-demo.git", "AuroraDemo/website-demo"),
    ("https://github.com/AuroraDemo/website-demo", "AuroraDemo/website-demo"),
    ("https://github.com/AuroraDemo/portal-demo.git", "AuroraDemo/portal-demo"),
    ("https://gitlab.com/AuroraDemo/website-demo.git", None),
    ("", None),
])
def test_parse_remote(url, esperado):
    assert github._parse_remote(url) == esperado


# ---------- fase 0: items del tablero ----------

# Payload real de `gh project item-list 15 --owner AuroraDemo --format json`
# (recortado): el issue vive anidado en `content` y el Status es un campo
# custom al mismo nivel del item.
ITEMS_REALES = {
    "items": [
        {"id": "PVTI_1", "title": "Preguntar a Retal Seguro", "status": "Done",
         "content": {"number": 21, "repository": "AuroraDemo/portal-demo",
                     "title": "Preguntar a Retal Seguro", "type": "Issue",
                     "url": "https://github.com/AuroraDemo/portal-demo/issues/21"}},
        {"id": "PVTI_2", "title": "Revisar horas pendientes AuroraDemo",
         "status": "In progress", "assignees": ["usuario-demo"],
         "content": {"number": 23, "repository": "AuroraDemo/portal-demo",
                     "title": "Revisar horas pendientes AuroraDemo", "type": "Issue",
                     "url": "https://github.com/AuroraDemo/portal-demo/issues/23"}},
        {"id": "PVTI_3", "title": "Nota suelta", "content": {}},
    ]
}


async def test_board_items_aplana_el_content():
    async def fake_gh(*args, cwd=None):
        import json as _json
        return 0, _json.dumps(ITEMS_REALES)

    with patch.object(github, "_gh", fake_gh):
        items = await github.board_items("AuroraDemo", 15)

    assert len(items) == 3
    assert items[1] == {
        "title": "Revisar horas pendientes AuroraDemo", "status": "In progress",
        "assignees": ["usuario-demo"], "repository": "AuroraDemo/portal-demo",
        "number": 23, "type": "Issue",
        "url": "https://github.com/AuroraDemo/portal-demo/issues/23",
    }
    # Un item sin content (nota suelta del tablero) no puede romper el aplanado
    assert items[2]["title"] == "Nota suelta"
    assert items[2]["number"] is None


def test_group_by_status_no_esconde_los_sin_estado():
    grupos = github.group_by_status([
        {"title": "a", "status": "Done"},
        {"title": "b", "status": "Done"},
        {"title": "c", "status": ""},
    ])
    assert [i["title"] for i in grupos["Done"]] == ["a", "b"]
    assert [i["title"] for i in grupos["(sin estado)"]] == ["c"]


# ---------- fase 1: el endpoint ----------


@pytest.fixture
async def cli():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        (state / "prompts").mkdir(parents=True)
        # DB propia por test: el conftest aísla ~/.4bis pero con un tmp de
        # SESIÓN, así que una conversación creada en un test le daba 409 al
        # siguiente ("ya hay una conversación abierta para este proyecto").
        with patch.dict("os.environ", {"STATE_DIR": str(state),
                                       "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
                                       "LOG_LEVEL": "WARNING"}, clear=False):
            app = create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            db: Database = app[DB_KEY]
            await db.upsert_project({
                "slug": "demo", "name": "Demo", "repo_path": str(tmp),
            })
            try:
                yield client
            finally:
                await client.close()


async def test_endpoint_sin_remote_no_es_500(cli):
    """Un repo sin remote (o sin `gh`) devuelve 200 con configured:false.
    El tab Proyectos tiene que seguir abriendo igual."""
    r = await cli.get("/admin/api/projects/demo/github")
    assert r.status == 200
    body = await r.json()
    assert body["configured"] is False
    assert body["repo"] is None
    assert body["issues"] == [] and body["pulls"] == []


async def test_endpoint_devuelve_issues_prs_y_tablero(cli):
    async def fake_slug(repo_path):
        return "AuroraDemo/website-demo"

    async def fake_issues(slug, limit=20):
        return [{"number": 7, "title": "Sprint 4", "labels": [],
                 "assignees": [], "url": "u"}]

    async def fake_pulls(slug, limit=20):
        return [{"number": 9, "title": "fix", "isDraft": True, "url": "u"}]

    async def fake_items(owner, number, limit=100):
        return [{"title": "t", "status": "Done", "assignees": [],
                 "repository": "AuroraDemo/website-demo", "number": 7,
                 "type": "Issue", "url": "u"}]

    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({
        "slug": "demo",
        "defaults_json": {"github_project": {"owner": "AuroraDemo", "number": 15}},
    })
    with patch.object(github, "repo_slug", fake_slug), \
            patch.object(github, "issues", fake_issues), \
            patch.object(github, "pulls", fake_pulls), \
            patch.object(github, "board_items", fake_items):
        r = await cli.get("/admin/api/projects/demo/github")

    body = await r.json()
    assert body["configured"] is True
    assert body["repo"] == "AuroraDemo/website-demo"
    assert body["issues"][0]["number"] == 7
    assert body["pulls"][0]["isDraft"] is True
    assert body["board"]["number"] == 15
    assert list(body["board"]["columns"]) == ["Done"]


async def test_endpoint_proyecto_desconocido_404(cli):
    r = await cli.get("/admin/api/projects/no-existe/github")
    assert r.status == 404


# ---------- fase 4: issue → conversación → PR ----------


async def test_conversacion_con_issue_inexistente_no_se_crea(cli):
    """Atar una conversación a un issue que no existe deja un PR con un
    `Closes #N` que no cierra nada — y eso se descubre tarde. Se valida
    contra GitHub ANTES de crear la rama."""
    async def fake_slug(repo_path):
        return "AuroraDemo/website-demo"

    async def sin_issue(slug, number):
        return None

    with patch.object(github, "repo_slug", fake_slug), \
            patch.object(github, "issue", sin_issue):
        r = await cli.post("/conversations",
                           json={"project": "demo", "issue": 999})
    assert r.status == 404
    assert "999" in (await r.json())["error"]


async def test_conversacion_con_issue_lo_guarda(cli):
    async def fake_slug(repo_path):
        return "AuroraDemo/website-demo"

    async def fake_issue(slug, number):
        return {"number": number, "title": "Arreglar el login", "body": "…"}

    with patch.object(github, "repo_slug", fake_slug), \
            patch.object(github, "issue", fake_issue):
        r = await cli.post("/conversations",
                           json={"project": "demo", "issue": 42})
    assert r.status == 201
    body = await r.json()
    assert body["issue_number"] == 42
    db: Database = cli.server.app[DB_KEY]
    conv = await db.get_conversation(body["id"])
    assert conv["issue_number"] == 42


async def test_issue_invalido_es_400(cli):
    r = await cli.post("/conversations",
                       json={"project": "demo", "issue": "cuarenta"})
    assert r.status == 400


def test_issue_block_recorta_y_lista_labels():
    blk = github.issue_block({
        "number": 7, "title": "Arreglar el login",
        "labels": [{"name": "bug"}, {"name": "ui"}],
        "body": "x" * 5000,
    })
    assert "## Issue #7 — Arreglar el login" in blk
    assert "Labels: bug, ui" in blk
    assert "…(recortado)" in blk        # un issue con 40KB de log no entra
    assert len(blk) < 4400


# ---------- fase 5: tablero de empresa ----------


async def test_board_sin_configurar_no_es_error(cli):
    r = await cli.get("/admin/api/github/board")
    assert r.status == 200
    body = await r.json()
    assert body["configured"] is False
    assert body["columns"] == {}


async def test_board_configurado_agrupa_y_lista_repos(cli):
    db: Database = cli.server.app[DB_KEY]
    await db.set_config("GITHUB_BOARD_OWNER", "AuroraDemo")
    await db.set_config("GITHUB_BOARD_NUMBER", "15")

    async def fake_items(owner, number, limit=100):
        assert (owner, number) == ("AuroraDemo", 15)
        return [
            {"title": "a", "status": "Done", "assignees": [],
             "repository": "AuroraDemo/portal-demo", "number": 21,
             "type": "Issue", "url": "u1"},
            {"title": "b", "status": "In progress", "assignees": ["pc"],
             "repository": "AuroraDemo/website-demo", "number": 3,
             "type": "Issue", "url": "u2"},
        ]

    with patch.object(github, "board_items", fake_items):
        r = await cli.get("/admin/api/github/board")
    body = await r.json()
    assert body["configured"] is True and body["total"] == 2
    assert set(body["columns"]) == {"Done", "In progress"}
    # Los tableros cruzan organizaciones: la vista tiene que mostrarlo.
    assert body["repos"] == ["AuroraDemo/portal-demo", "AuroraDemo/website-demo"]


async def test_board_number_no_numerico_es_400(cli):
    r = await cli.put("/admin/api/config",
                      json={"GITHUB_BOARD_NUMBER": "quince"})
    assert r.status == 400


async def test_vincular_tablero_no_pisa_defaults_json(cli):
    """El PATCH de proyectos es REPLACE puro. Este endpoint toca UNA clave:
    si pisara `defaults_json` entero, se llevaría model/timeout puestos."""
    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({"slug": "demo",
                             "defaults_json": {"model": "test", "timeout": 900}})
    r = await cli.put("/admin/api/projects/demo/github-project",
                      json={"owner": "AuroraDemo", "number": 15, "title": "Gestión"})
    assert r.status == 200
    p = await db.get_project("demo")
    assert p["defaults_json"]["model"] == "test"        # no se perdió
    assert p["defaults_json"]["timeout"] == 900
    assert p["defaults_json"]["github_project"]["number"] == 15
    # url derivada cuando el caller no la manda
    assert p["defaults_json"]["github_project"]["url"].endswith("/projects/15")


async def test_listado_expone_el_tablero_de_cada_proyecto(cli):
    """Con 51 proyectos, saber cuáles tienen tablero no puede costar 51
    fetches: el vínculo viaja en el listado."""
    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({"slug": "demo", "defaults_json": {
        "github_project": {"owner": "AuroraDemo", "number": 16,
                           "title": "Example Client", "url": "u"}}})
    await db.upsert_project({"slug": "sin-tablero", "name": "Otro",
                             "repo_path": "C:/x/otro"})
    body = await (await cli.get("/admin/api/projects")).json()
    por_slug = {p["slug"]: p for p in body["projects"]}
    assert por_slug["demo"]["github_project"]["number"] == 16
    assert por_slug["sin-tablero"]["github_project"] is None


async def test_editar_el_proyecto_no_borra_el_tablero(cli):
    """El form de edición manda name/repo_path/night_config y NO
    defaults_json. Si algún día empezara a mandarlo, el vínculo se
    perdería en silencio al guardar cualquier otro campo."""
    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({"slug": "demo", "defaults_json": {
        "github_project": {"owner": "AuroraDemo", "number": 16}}})
    r = await cli.patch("/admin/api/projects/demo",
                        json={"name": "Demo renombrada"})
    assert r.status == 200
    p = await db.get_project("demo")
    assert p["name"] == "Demo renombrada"
    assert p["defaults_json"]["github_project"]["number"] == 16


async def test_quitar_de_la_lista_y_devolver(cli):
    """Hay repos que nunca van a llevar tablero. Sacarlos de la lista de
    pendientes no puede borrar nada más del proyecto ni ser irreversible."""
    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({"slug": "demo", "defaults_json": {"model": "test"}})

    r = await cli.put("/admin/api/projects/demo/github-project",
                      json={"skip": True})
    assert r.status == 200 and (await r.json())["skipped"] is True
    p = await db.get_project("demo")
    assert p["defaults_json"]["github_project_skip"] is True
    assert p["defaults_json"]["model"] == "test"

    # El listado lo expone para que la vista lo filtre.
    body = await (await cli.get("/admin/api/projects")).json()
    assert next(x for x in body["projects"]
                if x["slug"] == "demo")["github_project_skip"] is True

    r = await cli.put("/admin/api/projects/demo/github-project",
                      json={"skip": False})
    assert (await r.json())["skipped"] is False
    p = await db.get_project("demo")
    assert "github_project_skip" not in p["defaults_json"]   # se borra, no queda False
    assert p["defaults_json"]["model"] == "test"


async def test_desvincular_borra_solo_el_mapping(cli):
    db: Database = cli.server.app[DB_KEY]
    await db.upsert_project({"slug": "demo", "defaults_json": {
        "model": "test",
        "github_project": {"owner": "AuroraDemo", "number": 15}}})
    r = await cli.put("/admin/api/projects/demo/github-project",
                      json={"number": None})
    assert r.status == 200 and (await r.json())["linked"] is None
    p = await db.get_project("demo")
    assert "github_project" not in p["defaults_json"]
    assert p["defaults_json"]["model"] == "test"


async def test_crear_tablero_vincula_y_linkea_al_repo(cli):
    creado = {}

    async def fake_create(owner, title):
        creado.update(owner=owner, title=title)
        return {"number": 42, "url": "https://github.com/orgs/AuroraDemo/projects/42"}

    async def fake_slug(repo_path):
        return "AuroraDemo/demo"

    async def fake_link(owner, number, repo):
        creado["linked"] = (owner, number, repo)
        return True, ""

    with patch.object(github, "create_board", fake_create), \
            patch.object(github, "repo_slug", fake_slug), \
            patch.object(github, "link_board", fake_link):
        r = await cli.post("/admin/api/projects/demo/github-project",
                           json={"owner": "AuroraDemo"})
    assert r.status == 201
    body = await r.json()
    assert body["created"]["number"] == 42 and body["linked_to_repo"] is True
    # Sin título explícito usa el nombre del proyecto, no el slug pelado.
    assert creado["title"] == "Demo"
    assert creado["linked"] == ("AuroraDemo", 42, "AuroraDemo/demo")
    db: Database = cli.server.app[DB_KEY]
    assert (await db.get_project("demo"))["defaults_json"]["github_project"]["number"] == 42


async def test_crear_tablero_propaga_el_motivo_del_link_fallido(cli):
    """Caso real: GitHub rechaza linkear un tablero de la org `AuroraDemo` a un
    repo de `ExampleOwner` ("has different owner"). El tablero se crea
    igual, pero el motivo tiene que llegar a la UI — si no, el usuario ve
    "creado" a medias y no sabe si es un bug nuestro."""
    async def fake_create(owner, title):
        return {"number": 24, "url": "u"}

    async def fake_slug(repo_path):
        return "ExampleOwner/ExampleRepo"

    async def link_rechazado(owner, number, repo):
        return False, "'ExampleOwner/ExampleRepo' has different owner from 'AuroraDemo'"

    with patch.object(github, "create_board", fake_create), \
            patch.object(github, "repo_slug", fake_slug), \
            patch.object(github, "link_board", link_rechazado):
        r = await cli.post("/admin/api/projects/demo/github-project",
                           json={"owner": "AuroraDemo"})
    body = await r.json()
    assert r.status == 201                       # el tablero SÍ se creó
    assert body["linked_to_repo"] is False
    assert "different owner" in body["link_error"]


async def test_crear_tablero_sin_gh_es_502(cli):
    async def sin_gh(owner, title):
        return None

    with patch.object(github, "create_board", sin_gh):
        r = await cli.post("/admin/api/projects/demo/github-project",
                           json={"owner": "AuroraDemo"})
    assert r.status == 502
    assert "project" in (await r.json())["error"]


async def test_config_get_devuelve_toda_la_whitelist(cli):
    """El form del tab Config lee de acá: una clave editable que el GET no
    devuelve se guarda pero nunca se muestra de vuelta (pasó con
    GITHUB_BOARD_* — el usuario vio "guardado ✓" y el campo vacío)."""
    await cli.put("/admin/api/config",
                  json={"GITHUB_BOARD_OWNER": "AuroraDemo",
                        "GITHUB_BOARD_NUMBER": "15"})
    body = await (await cli.get("/admin/api/config")).json()
    assert set(body["config"]) == set(body["editable_keys"])
    assert body["config"]["GITHUB_BOARD_OWNER"] == "AuroraDemo"
    assert body["config"]["GITHUB_BOARD_NUMBER"] == "15"
    assert body["config"]["RELAY_HOST"] == "127.0.0.1"   # default sin fila


async def test_listado_expone_discord_channel(cli):
    """Bug 2026-08-02: el listado omitía discord_channel_id → la grid
    mostraba 'sin canal' aunque estuviera seteado (el detalle sí lo daba)."""
    db: Database = cli.server.app[DB_KEY]
    await db.set_project_discord_channel("demo", channel_id="123456789")
    body = await (await cli.get("/admin/api/projects")).json()
    demo = next(p for p in body["projects"] if p["slug"] == "demo")
    assert demo["discord_channel_id"] == "123456789"


async def test_discord_guild_id_editable_valida_y_en_listado(cli):
    """DISCORD_GUILD_ID: no-numérico → 400; válido se guarda, vuelve en el
    GET config y el listado lo expone (para linkear el canal)."""
    r = await cli.put("/admin/api/config", json={"DISCORD_GUILD_ID": "abc"})
    assert r.status == 400
    r = await cli.put("/admin/api/config",
                      json={"DISCORD_GUILD_ID": "123456789012345678"})  # gitleaks:allow -- synthetic ID
    assert r.status == 200
    cfg = await (await cli.get("/admin/api/config")).json()
    assert cfg["config"]["DISCORD_GUILD_ID"] == "123456789012345678"
    assert "DISCORD_GUILD_ID" in cfg["editable_keys"]
    lst = await (await cli.get("/admin/api/projects")).json()
    assert lst["discord_guild_id"] == "123456789012345678"


# ---------- PUT /git-remote (vincular / cambiar el origin) ----------


def _git_init(path: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _origin(path: str) -> str:
    return subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=path,
        capture_output=True, text=True).stdout.strip()


def _git_reescribe_urls() -> bool:
    """¿Este git tiene `url.<base>.insteadOf` configurado?

    2026-08-16: en entornos con proxy (CI, contenedores de agentes) suele
    haber un `url.https://github.com/.insteadOf git@github.com:` global,
    que reescribe la URL al guardarla. `git remote get-url` devuelve la
    reescrita y la aserción sobre la URL SSH falla — sin que haya nada
    roto en el relay. Se detecta y se saltea ese tramo en vez de dejar un
    rojo permanente que no dice nada de nuestro código.
    """
    out = subprocess.run(
        ["git", "config", "--get-regexp", "insteadof"],
        capture_output=True, text=True).stdout
    return "github.com" in out.lower()


async def test_git_remote_url_invalida_es_400(cli):
    r = await cli.put("/admin/api/projects/demo/git-remote",
                      json={"url": "no-es-github.com/x"})
    assert r.status == 400


async def test_git_remote_url_vacia_es_400(cli):
    r = await cli.put("/admin/api/projects/demo/git-remote", json={"url": "  "})
    assert r.status == 400


async def test_git_remote_sin_git_es_409(cli):
    """El repo_path del demo es un tmp sin `.git`: no se puede setear un
    remoto. La URL es válida para que el 409 sea por el .git, no por parse."""
    r = await cli.put("/admin/api/projects/demo/git-remote",
                      json={"url": "https://github.com/AuroraDemo/website-demo"})
    assert r.status == 409


async def test_git_remote_desconocido_es_404(cli):
    r = await cli.put("/admin/api/projects/no-existe/git-remote",
                      json={"url": "https://github.com/AuroraDemo/x"})
    assert r.status == 404


async def test_git_remote_vincula_y_cambia_origin(cli):
    db: Database = cli.server.app[DB_KEY]
    repo = (await db.get_project("demo"))["repo_path"]
    _git_init(repo)
    # Vincular: no había origin → `git remote add`.
    r = await cli.put("/admin/api/projects/demo/git-remote",
                      json={"url": "https://github.com/AuroraDemo/website-demo.git"})
    assert r.status == 200
    assert (await r.json())["repo"] == "AuroraDemo/website-demo"
    assert _origin(repo) == "https://github.com/AuroraDemo/website-demo.git"
    # Cambiar: ya había origin → `git remote set-url` (no falla por existente).
    r2 = await cli.put("/admin/api/projects/demo/git-remote",
                       json={"url": "git@github.com:AuroraDemo/otro.git"})
    assert r2.status == 200
    if _git_reescribe_urls():
        # El git de este entorno reescribe SSH→HTTPS al guardar. Lo que
        # este test verifica —que `set-url` pise el origin existente en
        # vez de fallar por duplicado— se comprueba igual sobre la URL
        # reescrita: lo que importa es que el remote CAMBIÓ de repo.
        assert _origin(repo).endswith("AuroraDemo/otro.git")
    else:
        assert _origin(repo) == "git@github.com:AuroraDemo/otro.git"
