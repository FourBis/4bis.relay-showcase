from relay import git_process
from relay import expert_models, expert_runner, expert_staged_runner, expert_stages, expert_verdicts
from relay import server_common, server_expert_jobs, server_expert_routes, server_lifecycle
"""Regresiones de concurrencia, persistencia y SQL sin servicios externos."""
import asyncio
import gc
import json
import sqlite3
import threading
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from relay import config, coordination, dbtool, finalization, persist, server
from relay.db import Database


async def test_corrupt_export_does_not_block_later_outputs(db):
    ids = []
    for content in ("broken", "intact"):
        cid = await db.create_chat(project_slug="demo", source="test", author="", target="demo")
        ids.append(cid)
        await db.finish_chat(cid, status="ok", artifact=dict(
            target="demo", chat_id=cid, user="pedido", content=content,
            source="test", author="", model="test", status="ok", duration_ms=1))
    await db.run("UPDATE chat_outputs SET payload='{' WHERE chat_id=?", (ids[0],))
    await finalization.retry_pending(db)
    assert (await db.get_chat(ids[1]))["md_path"]
    broken = (await db.run("SELECT * FROM chat_outputs WHERE chat_id=?", (ids[0],)))[0]
    assert broken["ultimo_error"] and broken["intentos"] == 1


async def test_sqlite_respects_file_veto_even_when_connection_can_write(db, tmp_path):
    from unittest.mock import patch
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from relay import experts
    private = tmp_path / "private"
    private.mkdir()
    path = private / "secret.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE hidden (value)")
        connection.execute("INSERT INTO hidden VALUES ('confidential')")
    def model(messages, info):
        results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
                   and p.tool_name == "db_query"]
        if results:
            return ModelResponse(parts=[TextPart(str(results[-1].content))])
        return ModelResponse(parts=[ToolCallPart("db_query", {
            "conexion": str(path), "sql": "SELECT * FROM hidden"})])
    project = await db.get_project("demo")
    project["defaults_json"] = {"rutas_vedadas": ["private"]}
    with patch.object(expert_models, "build_model", return_value=FunctionModel(model)):
        result = await expert_runner.run_expert(project, "query", db=db, model_override="function")
    output = result["content"]
    assert "vedadas" in output
    assert "confidential" not in output


def test_nested_extra_root_cannot_remove_a_file_veto(tmp_path):
    from relay.files import Permisos, SinPermiso
    private = tmp_path / "private"
    private.mkdir()
    perm = Permisos.para(str(tmp_path), vedadas=["private"], extras=[str(private)])
    with pytest.raises(SinPermiso):
        perm.resolve(str(private / "secret.txt"))


def test_unstructured_verifier_text_cannot_approve():
    from relay import experts
    for text in ("No he podido revisar", "This is not complete yet"):
        verdict, _, steps = expert_verdicts._parse_verifier(text)
        assert verdict != "complete"
        assert not steps


async def test_invalid_verifier_is_reported_as_unverified(monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from relay import experts
    monkeypatch.setattr(expert_models, "build_model", lambda spec: FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("This is not complete yet")])) )
    verdict, feedback, usage, error, steps = await expert_stages._run_verifier(
        user="pedido", plan="1. verificar", executor_result={"content": "listo"},
        model_spec="function", ponytail="")
    assert verdict == "needs_human" and error and not steps
    assert server._resultado_del_trabajo(dict(verifier_verdict=verdict,
                                             stage_errors={"verifier": error})) == "sin_verificar"


async def test_expired_stage_budget_does_not_call_model(monkeypatch):
    from pydantic_ai.models.function import FunctionModel
    from relay import experts
    def forbidden(*args):
        pytest.fail("presupuesto agotado: no debe iniciar otra petición al proveedor")
    monkeypatch.setattr(expert_models, "build_model", lambda spec: FunctionModel(forbidden))
    verdict, _, _, error, steps = await expert_stages._run_verifier(user="pedido", plan="1. probar",
        executor_result={"content": "parcial"}, model_spec="function", ponytail="", deadline=0)
    assert verdict == "needs_human" and "TimeoutError" in error and not steps


async def test_request_deadline_is_shared_with_planner_and_executor(monkeypatch):
    import time
    from relay import experts
    deadline = time.monotonic() + 100
    async def planner(**kwargs):
        assert kwargs["deadline"] == deadline
        return "TRIVIAL: saludo", {}, ""
    async def executor(project, user, **kwargs):
        assert kwargs["deadline_pedido"] == deadline
        return dict(content="hola", phase_at_end="done", tool_calls=0)
    monkeypatch.setattr(expert_stages, "_run_planner", planner)
    monkeypatch.setattr(expert_runner, "run_expert", executor)
    result = await expert_staged_runner.run_expert_staged(dict(repo_path="", defaults_json={"verifier": False}),
                                            "hola", deadline_pedido=deadline)
    assert result["content"]


async def test_atomic_md_keeps_previous_file_if_replace_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("FOURBIS_CHATS_DIR", str(tmp_path))
    args = dict(target="demo", chat_id="atomic", user="pedido", source="test", author="",
                model="test", status="ok", duration_ms=1, filename="atomic.md")
    from pathlib import Path
    path = Path(await persist.write_chat_md(content="previo", **args))
    before = path.read_bytes()
    def denied(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(persist.os, "replace", denied)
    with pytest.raises(PermissionError):
        await persist.write_chat_md(content="nuevo", **args)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


async def test_jsonl_ignores_non_object_records(tmp_path, monkeypatch):
    with patch.dict(config._runtime, {"FOURBIS_JSONL_DIR": str(tmp_path)}):
        (tmp_path / "demo.jsonl").write_text(
            'null\n[]\n{broken\n{"role":"assistant","chat":"ok"}\n')
        assert await persist.jsonl_tiene_respuesta("demo", "ok")


@pytest.mark.parametrize("status", [0, 128])
async def test_git_warnings_do_not_become_parsed_stdout(monkeypatch, status):
    from relay import git_flow
    monkeypatch.setattr(git_process, "_git_out", AsyncMock(return_value=(status, "", "warning: ignore inaccessible")))
    rc, output = await git_process._git("repo", "status", "--porcelain")
    assert rc == status
    assert output == ("warning: ignore inaccessible" if status else "")


async def test_conversation_uses_durable_output_and_exposes_outcome(app, db, tmp_path):
    conv = await db.create_conversation(project_slug="demo")
    cid = await db.create_chat(project_slug="demo", source="test", author="", target="demo", conversation_id=conv)
    md = tmp_path / "old.md"
    md.write_text("## Usuario\n\npedido\n\n## Respuesta\n\nviejo\n")
    await db.finish_chat(cid, status="ok", md_path=str(md), duration_ms=25,
        stages_json=json.dumps(dict(resultado="pendiente", verifier_verdict="needs_more")),
        artifact=dict(user="pedido", content="durable"))
    response = await server.conversations_get_messages(request(app, {}, id=conv))
    turn = json.loads(response.text)["messages"][1]
    assert turn["content"] == "durable"
    assert turn["run_status"] == "ok" and turn["stages"]["resultado"] == "pendiente"
    assert turn["export_pending"] and turn["duration_ms"] == 25
    await db.run("UPDATE chat_outputs SET payload='{}' WHERE chat_id=?", (cid,))
    response = await server.conversations_get_messages(request(app, {}, id=conv))
    assert json.loads(response.text)["messages"][1]["content"] == "viejo"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_generated_images_are_persisted_and_notified_without_llm_citation(db, monkeypatch, cancelled):
    from relay import attachments
    aid, path, _ = attachments.store(b"image-fixture", mimetype="image/png")
    cid = await db.create_chat(project_slug="demo", source="test", author="", target="demo")
    async def runner(project, user, **kwargs):
        kwargs["rescue"]["image_artifacts"] = {aid: path.name}
        if cancelled:
            raise asyncio.CancelledError()
        return dict(content="listo", image_artifacts={aid: path.name}, phase_at_end="done",
                    three_stage=True, verifier_verdict="complete", model="test")
    monkeypatch.setattr(server.experts, "run_expert_staged", runner)
    monkeypatch.setattr(server_expert_jobs, "_suggest_followups", AsyncMock(return_value=[]))
    notify = AsyncMock()
    await server._run_expert_bg(db=db, notify=notify, running={}, progress={}, chat_id=cid,
        project=await db.get_project("demo"), user="capturar", skills_block="", system_extra="",
        model_override="test", target="demo", source="test", author="", conversation=None)
    sent = notify.send.call_args.kwargs
    assert sent["metadata"]["attachments"] == [aid]
    assert sent["metadata"]["resultado"] == ("sin_verificar" if cancelled else "aprobado")
    assert f"/attachments/{aid}" in sent["message"]
    row = await db.get_chat(cid)
    from pathlib import Path
    assert f"/attachments/{aid}" in Path(row["md_path"]).read_text(encoding="utf-8")
    assert row["status"] == ("cancelled" if cancelled else "ok")


@pytest.fixture
async def db(tmp_path):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    return db


@pytest.fixture
def app(db, monkeypatch):
    app = web.Application()
    app[server.DB_KEY] = db
    app[server.SKILLS_KEY] = AsyncMock()
    app[server.SKILLS_KEY].get_block.return_value = ""
    app[server.RUNNING_KEY] = {}
    app[server.PROGRESS_KEY] = {}
    app[server.NOTIFY_KEY] = AsyncMock()
    app[server.BG_TASKS_KEY] = set()
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(server.relay_config, "default_discord_user", lambda: ("", ""))
    monkeypatch.setattr(server.git_flow, "is_git_repo", AsyncMock(return_value=False))
    monkeypatch.setattr(persist, "append_jsonl", AsyncMock())
    return app


def request(app, body, path="/experts/run", id=None):
    req = make_mocked_request("POST", path, app=app, match_info={"id": id} if id else {})
    req.json = AsyncMock(return_value=body)
    return req


async def test_concurrent_conversation_creation_has_one_winner(app, db):
    results = await asyncio.gather(*(
        server.conversations_create(request(app, {"project": "demo"}, "/conversations"))
        for _ in range(2)))
    assert sorted(r.status for r in results) == [201, 409]
    assert len(await db.run("SELECT id FROM conversations WHERE status='open'")) == 1


async def test_two_project_aliases_cannot_open_two_branches(app, db):
    project = await db.get_project("demo")
    await db.upsert_project({"slug": "alias", "name": "Alias", "repo_path": project["repo_path"]})
    first = await server.conversations_create(request(app, {"project": "demo"}))
    second = await server.conversations_create(request(app, {"project": "alias"}))
    assert first.status == 201
    assert second.status == 409


async def test_active_turn_blocks_another_turn_and_close_until_background_finishes(app, db, monkeypatch):
    conv = await db.create_conversation(project_slug="demo")
    done = asyncio.Event()
    monkeypatch.setattr(server_expert_routes, "_run_expert_bg", lambda **kw: done.wait())
    body = {"target": "demo", "user": "revisar", "conversation": conv}
    first = await server.experts_run(request(app, body))
    assert first.status == 202
    try:
        assert (await server.experts_run(request(app, body))).status == 409
        assert (await server.conversations_close(request(app, {}, id=conv))).status == 409
        assert len(await db.run("SELECT id FROM chats")) == 1
    finally:
        tasks = list(app[server.BG_TASKS_KEY])
        done.set()
        await asyncio.gather(*tasks)
    assert not coordination.busy(db, await db.get_project("demo"))


async def test_stale_history_cannot_overwrite_a_finished_turn(db):
    conv = await db.create_conversation(project_slug="demo")
    await db.save_conversation_messages(conv, '["A"]', expected="")
    with pytest.raises(RuntimeError, match="historial cambió"):
        await db.save_conversation_messages(conv, '["B"]', expected="")
    assert (await db.get_conversation(conv))["messages_json"] == '["A"]'


async def test_export_failure_preserves_terminal_result_and_can_be_retried(db, monkeypatch):
    conv = await db.create_conversation(project_slug="demo")
    chat = await db.create_chat(project_slug="demo", source="test", conversation_id=conv,
                                author="", target="demo")
    artifact = dict(target="demo", chat_id=chat, user="pedido", content="resultado",
                    source="test", author="", model="test", status="ok", duration_ms=12)
    export = persist.write_chat_artifacts
    monkeypatch.setattr(persist, "write_chat_artifacts", AsyncMock(side_effect=OSError("disco lleno")))
    assert await finalization.finish(db, chat, artifact=artifact, status="ok", tokens_in=17) is None
    row = await db.get_chat(chat)
    assert row["status"] == "ok" and row["tokens_in"] == 17
    chats = await db.list_chats_by_conversation(conv)
    assert finalization.turns(chats[0]["output_payload"])[1]["content"] == "resultado"
    monkeypatch.setattr(persist, "write_chat_artifacts", export)
    await finalization.retry_pending(db)
    row = await db.get_chat(chat)
    assert persist.parse_chat_md_turns(row["md_path"])[1]["content"] == "resultado"
    assert (await db.run("SELECT exported FROM chat_outputs WHERE chat_id=?", (chat,)))[0]["exported"] == 1


async def test_sqlite_timeout_stops_worker_and_releases_connection(tmp_path, monkeypatch):
    path = tmp_path / "query.db"
    sqlite3.connect(path).close()
    closed = threading.Event()
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.set()

    monkeypatch.setattr(dbtool.sqlite3, "connect", lambda *a, **kw: connect(*a, factory=Connection, **kw))
    monkeypatch.setattr(dbtool, "QUERY_TIMEOUT_S", 0.05)
    query = "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n"
    result = await dbtool.consultar(str(path), query)
    assert "se cortó" in result
    assert closed.is_set(), "el timeout devolvió antes de detener SQLite"


async def test_postgres_enforces_readonly_and_fetches_only_the_cap(monkeypatch):
    import asyncpg
    from contextlib import asynccontextmanager
    seen = []
    cursor = AsyncMock()
    cursor.fetch.return_value = [(1,), (2,), (3,)]
    statement = AsyncMock()
    statement.get_attributes = lambda: [type("Attr", (), {"name": "n"})()]
    statement.cursor.return_value = cursor
    conn = AsyncMock()
    conn.prepare.return_value = statement

    @asynccontextmanager
    async def transaction(*, readonly):
        seen.append(readonly)
        yield

    conn.transaction = transaction
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(dbtool, "MAX_ROWS", 2)
    cols, rows, truncated = await dbtool._postgres("postgres://unused/db", "SELECT n FROM t", [])
    assert seen == [True]
    cursor.fetch.assert_awaited_once_with(3)
    assert cols == ["n"] and rows == [(1,), (2,)] and truncated
    conn.close.assert_awaited_once()


@pytest.mark.parametrize("member,read_only", [(True, False), (False, True)])
async def test_write_enabled_alias_is_still_readonly_for_restricted_runs(db, tmp_path, member, read_only):
    from relay.execution_policy import ExecutionPolicy, request_role
    from relay.sql_tools import sql_tools
    path = tmp_path / "client.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
    await db.upsert_db_connection("client", str(path), escribir=True)
    token = request_role.set("member" if member else "owner")
    try:
        policy = ExecutionPolicy.for_run({"read_only": read_only})
    finally:
        request_role.reset(token)
    query = next(t.function for t in sql_tools(db, policy) if t.name == "db_query")
    assert "solo lectura" in await query("client", "DELETE FROM t")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    assert not policy.unrestricted_tools


async def test_owner_can_still_write_an_explicitly_writable_alias(db, tmp_path):
    from relay.execution_policy import ExecutionPolicy
    from relay.sql_tools import sql_tools
    path = tmp_path / "client.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
    await db.upsert_db_connection("client", str(path), escribir=True)
    query = next(t.function for t in sql_tools(db, ExecutionPolicy.for_run({})) if t.name == "db_query")
    assert "filas_afectadas" in await query("client", "DELETE FROM t")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


# ---------- que la politica no se lleve puesto el caso normal ----------


async def test_un_nodo_con_hermanos_vivos_conserva_la_shell():
    """`reserved` es coordinacion, no un limite de confianza.

    `archivos_reservados` sale de `files_claimed_by_others`, o sea que
    esta poblado siempre que otro nodo del mismo grafo corre en
    paralelo. Si eso apagara la shell, el nodo de verificacion —el que
    existe justo para correr `npm run build` o `pytest`— se quedaria sin
    ella exactamente cuando el grafo esta sano. No taparia ningun
    agujero: de no escribir esas rutas ya se encarga
    `Permisos.reservadas` en las tools de archivos.
    """
    from relay.execution_policy import ExecutionPolicy

    politica = ExecutionPolicy.for_run({}, ["src/a.py", "src/b.py"])
    assert politica.unrestricted_tools, (
        "un nodo perdio la shell por tener hermanos con archivos tomados")
    assert not politica.read_only and not politica.sql_read_only


async def test_lo_que_si_tiene_que_restringir_sigue_restringiendo():
    """La guarda del test de arriba no puede aflojar los limites reales."""
    from relay.execution_policy import ExecutionPolicy, request_role

    assert not ExecutionPolicy.for_run({"read_only": True}).unrestricted_tools
    assert not ExecutionPolicy.for_run(
        {"rutas_vedadas": ["secrets/"]}).unrestricted_tools

    token = request_role.set("member")
    try:
        p = ExecutionPolicy.for_run({})
        assert not p.unrestricted_tools and p.sql_read_only
    finally:
        request_role.reset(token)


# ---------- el guard y el handler tienen que hablar del mismo repo ----------


async def test_target_y_project_contradictorios_se_rechazan(app, db):
    """El guard reservaba un repo y el handler trabajaba sobre otro.

    `guard_workspace` resolvia `target or project`; `/conversations` y
    `/night-mode/start` miran solo `project`, y `/graphs` hace
    `project or target` —al reves—. Con los dos campos distintos la
    exclusion cubria el workspace equivocado: se reservaba `otro` y se
    abria la conversacion en `demo`, que es como entraban dos al mismo
    repo.
    """
    project = await db.get_project("demo")
    await db.upsert_project({"slug": "otro", "name": "Otro",
                             "repo_path": project["repo_path"]})
    r = await server.conversations_create(
        request(app, {"project": "demo", "target": "otro"}, "/conversations"))
    assert r.status == 400, "el body contradictorio paso el guard"
    cuerpo = json.loads(r.body)
    assert cuerpo["target"] == "otro" and cuerpo["project"] == "demo"
    assert not await db.run("SELECT id FROM conversations WHERE status='open'")


async def test_un_solo_campo_sigue_andando(app, db):
    """La guarda no puede romper el caso normal, que es mandar uno solo."""
    r = await server.conversations_create(
        request(app, {"project": "demo"}, "/conversations"))
    assert r.status == 201, r.body

    dos = await server.conversations_create(
        request(app, {"project": "demo", "target": "demo"}, "/conversations"))
    assert dos.status != 400, "los dos campos IGUALES no son contradiccion"


async def test_el_reintento_no_duplica_la_respuesta_en_el_jsonl(db, monkeypatch):
    """Las dos escrituras del export no son igual de reversibles.

    El .md se sobrescribe; el JSONL es un append. Si la primera vuelta
    escribio los dos archivos y fallo recien al confirmar en SQLite, el
    reintento agregaba la respuesta una segunda vez al historial del
    proyecto — la misma respuesta contada dos veces.
    """
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    artifact = dict(target="demo", chat_id=chat, user="pedido",
                    content="resultado", source="test", author="",
                    model="test", status="ok", duration_ms=12)

    # Los artefactos se escriben bien; lo unico que falla es la
    # confirmacion del export. `finish_chat` tambien usa `run_tx`, asi
    # que el fallo tiene que ser selectivo o se rompe el paso anterior.
    run_tx = db.run_tx

    async def _falla_solo_el_export(sentencias):
        if any("SET exported=1" in sql for sql, _ in sentencias):
            raise sqlite3.OperationalError("locked")
        return await run_tx(sentencias)

    monkeypatch.setattr(db, "run_tx", _falla_solo_el_export)
    await finalization.finish(db, chat, artifact=artifact, status="ok")
    monkeypatch.setattr(db, "run_tx", run_tx)

    await finalization.retry_pending(db)

    lineas = (config.jsonl_dir() / "demo.jsonl").read_text(
        encoding="utf-8").splitlines()
    del_chat = [l for l in lineas if json.loads(l).get("chat") == chat]
    assert len(del_chat) == 1, (
        f"la respuesta quedo {len(del_chat)} veces en el JSONL")
    assert (await db.run("SELECT exported FROM chat_outputs WHERE chat_id=?",
                         (chat,)))[0]["exported"] == 1


# ---------- el drenaje no puede quedarse con una foto vieja ----------


async def test_el_drenaje_espera_a_la_hija_que_nace_durante_el_cierre():
    """Una tarea que termina puede largar otra: el turno que dispara su grafo.

    El drenaje tomaba UNA foto de las tareas y esperaba sobre esa lista,
    asi que la hija nacia despues del `asyncio.wait` y quedaba afuera.
    El cierre seguia igual y le cerraba los clientes MCP/HTTP en la cara.
    """
    from relay.server import BG_TASKS_KEY, _drain_running_experts, _bg_tasks

    app = {BG_TASKS_KEY: set()}
    hija_termino = False

    async def hija():
        nonlocal hija_termino
        await asyncio.sleep(0.15)
        hija_termino = True

    async def madre():
        await asyncio.sleep(0.05)
        t = asyncio.create_task(hija())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    tarea = asyncio.create_task(madre())
    app[BG_TASKS_KEY].add(tarea)
    try:
        await _drain_running_experts(app)
        assert hija_termino, (
            "el drenaje volvio con una hija todavia corriendo")
    finally:
        _bg_tasks.clear()


async def test_el_drenaje_tiene_un_plazo_global(monkeypatch):
    """El plazo no puede ser por vuelta: una cadena de tareas que se
    largan entre si estiraria el cierre indefinidamente."""
    from relay import server as srv

    monkeypatch.setattr(server_lifecycle, "_DRAIN_RUNNING_TIMEOUT_S", 0.3)
    app = {srv.BG_TASKS_KEY: set()}

    async def eterna():
        await asyncio.sleep(30)

    tarea = asyncio.create_task(eterna())
    app[srv.BG_TASKS_KEY].add(tarea)
    t0 = asyncio.get_event_loop().time()
    await srv._drain_running_experts(app)
    assert asyncio.get_event_loop().time() - t0 < 3, "el cierre se colgo"
    assert tarea.cancelled() or tarea.done()


async def test_el_reintento_no_se_come_la_respuesta_por_la_linea_del_usuario(
        db, monkeypatch):
    """El turno HTTP appendea `user` ANTES de correr.

    La primera version de la deduplicacion buscaba cualquier registro
    con ese chat_id, asi que la aguja aparecia siempre: el reintento se
    saltaba la respuesta en TODOS los casos y marcaba `exported=1`.
    Quedaba el pedido sin la respuesta, en silencio.
    """
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    # Lo que hace `experts_run` antes de largar el run.
    await persist.append_jsonl("demo", "user", "pedido", author="",
                               chat=chat)
    artifact = dict(target="demo", chat_id=chat, user="pedido",
                    content="resultado", source="test", author="",
                    model="test", status="ok", duration_ms=12)

    # Falla la exportacion ANTES de escribir nada, no despues.
    escribir = persist.write_chat_artifacts
    monkeypatch.setattr(persist, "write_chat_artifacts",
                        AsyncMock(side_effect=OSError("disco lleno")))
    await finalization.finish(db, chat, artifact=artifact, status="ok")
    monkeypatch.setattr(persist, "write_chat_artifacts", escribir)

    await finalization.retry_pending(db)

    roles = [json.loads(l)["role"] for l in
             (config.jsonl_dir() / "demo.jsonl").read_text(
                 encoding="utf-8").splitlines() if json.loads(l).get("chat") == chat]
    assert roles == ["user", "assistant"], (
        f"el historial exportado quedo incompleto: {roles}")


async def test_el_cierre_normal_y_el_barrido_no_exportan_el_mismo_chat(
        db, monkeypatch):
    """`finish` deja `exported=0` y RECIEN despues escribe los archivos.

    El barrido de pendientes puede levantar la fila justo en esa ventana
    y exportar en paralelo con el cierre normal: los dos caminos hacian
    el append sin excluirse y quedaba `user, assistant, assistant` con
    `exported=1`. La deduplicacion por JSONL no alcanza porque es
    check-then-act: el reintento mira ANTES de que el cierre appendee.
    """
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    await persist.append_jsonl("demo", "user", "pedido", author="", chat=chat)
    artifact = dict(target="demo", chat_id=chat, user="pedido",
                    content="resultado", source="test", author="",
                    model="test", status="ok", duration_ms=12)

    original = persist.write_chat_artifacts
    entro, seguir = asyncio.Event(), asyncio.Event()

    async def lenta(**kw):
        entro.set()
        await seguir.wait()
        return await original(**kw)

    monkeypatch.setattr(persist, "write_chat_artifacts", lenta)

    cierre = asyncio.create_task(
        finalization.finish(db, chat, artifact=artifact, status="ok"))
    await asyncio.wait_for(entro.wait(), timeout=5)
    barrido = asyncio.create_task(finalization.retry_pending(db))
    await asyncio.sleep(0.05)          # que el barrido llegue a la ventana
    seguir.set()
    await asyncio.gather(cierre, barrido)

    roles = [json.loads(l)["role"] for l
             in (config.jsonl_dir() / "demo.jsonl").read_text(
                 encoding="utf-8").splitlines()
             if json.loads(l).get("chat") == chat]
    assert roles == ["user", "assistant"], (
        f"la respuesta quedo {roles.count('assistant')} veces: {roles}")
    fila = await db.get_chat(chat)
    assert fila["md_path"], "se perdio el puntero al .md"


async def test_exportar_dos_veces_seguidas_no_repite_el_append(db):
    """El candado serializa; el re-chequeo adentro es el que decide.

    Dos exportaciones que NO son reintento (`escribir_jsonl=True` las
    dos) appendearian igual aunque se turnen: lo que las frena es mirar
    `exported` una vez adentro del candado.
    """
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    artifact = dict(target="demo", chat_id=chat, user="pedido",
                    content="resultado", source="test", author="",
                    model="test", status="ok", duration_ms=12)

    primero = await finalization.finish(db, chat, artifact=artifact,
                                        status="ok")
    segundo = await finalization.export(db, chat, artifact)

    assert segundo == primero, "la segunda vuelta perdio el puntero al .md"
    lineas = [json.loads(l) for l
              in (config.jsonl_dir() / "demo.jsonl").read_text(
                  encoding="utf-8").splitlines()]
    respuestas = [r for r in lineas
                  if r.get("chat") == chat and r["role"] == "assistant"]
    assert len(respuestas) == 1, f"la respuesta quedo {len(respuestas)} veces"


def test_el_registro_de_candados_no_acumula_loops_cerrados():
    """El `WeakKeyDictionary` que habia aca no retenia menos: retenia todo.

    `asyncio.Lock` se ata a su loop —`self._loop`— recien cuando alguien
    ESPERA por el: el camino sin contencion ni lo mira. Con contencion,
    el lock pasa a referenciar fuerte al loop, o sea que el valor del
    diccionario mantenia viva a su propia clave y la entrada no se
    recolectaba nunca. Medido: 3 corridas dejaban 3 loops cerrados.

    Por eso el test fuerza la contencion. Sin eso pasa con las dos
    implementaciones y no prueba nada.
    """
    async def con_contencion():
        lock = finalization._candado()

        async def tomar():
            async with lock:
                await asyncio.sleep(0.01)

        await asyncio.gather(tomar(), tomar())

    for _ in range(3):
        asyncio.run(con_contencion())
    gc.collect()

    # El barrido corre AL ENTRAR, asi que la entrada del ultimo loop
    # queda hasta la proxima llamada: la invariante es que no se
    # acumulen, no que quede vacio. Con el `WeakKeyDictionary` quedaban
    # los tres.
    assert len(finalization._CANDADOS) <= 1, (
        f"quedaron {len(finalization._CANDADOS)} loops en el registro")


# ---------- 9/9/2026: la exportación pendiente se puede diagnosticar ----------


async def _pendiente(db, chat_id, artifact, monkeypatch, error="disco lleno"):
    escribir = persist.write_chat_artifacts
    monkeypatch.setattr(persist, "write_chat_artifacts",
                        AsyncMock(side_effect=OSError(error)))
    await finalization.finish(db, chat_id, artifact=artifact, status="ok")
    monkeypatch.setattr(persist, "write_chat_artifacts", escribir)


async def test_una_exportacion_fallida_deja_por_que_y_cuando(db, monkeypatch):
    """Antes la fila decia `exported=0` y nada mas.

    El motivo vivia solo en el log del relay, que no se persiste: para
    diagnosticar una exportacion pendiente habia que haber estado
    mirando cuando fallo.
    """
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    artifact = dict(target="demo", chat_id=chat, user="p", content="r",
                    source="test", author="", model="test", status="ok",
                    duration_ms=1)
    await _pendiente(db, chat, artifact, monkeypatch)

    fila = (await db.run("SELECT * FROM chat_outputs WHERE chat_id=?",
                         (chat,)))[0]
    assert fila["exported"] == 0
    assert fila["intentos"] == 1
    assert "disco lleno" in (fila["ultimo_error"] or "")
    # El PRIMER reintento no espera: `None` = en la proxima vuelta. El
    # backoff arranca cuando la falla se repite.
    assert fila["proximo_intento_at"] is None
    await _pendiente(db, chat, artifact, monkeypatch, error="sigue lleno")
    fila = (await db.run("SELECT * FROM chat_outputs WHERE chat_id=?",
                         (chat,)))[0]
    assert fila["intentos"] == 2
    assert fila["proximo_intento_at"], "el segundo fallo no agendo espera"


async def test_el_backoff_no_se_come_un_intento_por_vuelta(db, monkeypatch):
    """Sin esto, un disco lleno gasta un intento en cada barrido, para
    siempre. El reintento tiene que esperar a que le toque."""
    chat = await db.create_chat(project_slug="demo", source="test",
                                author="", target="demo")
    artifact = dict(target="demo", chat_id=chat, user="p", content="r",
                    source="test", author="", model="test", status="ok",
                    duration_ms=1)
    # Dos fallas: la segunda es la que agenda espera.
    await _pendiente(db, chat, artifact, monkeypatch)
    await _pendiente(db, chat, artifact, monkeypatch)

    # Agendado a futuro: el barrido no lo debe tocar todavia.
    llamadas = {"n": 0}
    real = persist.write_chat_artifacts

    async def contando(**kw):
        llamadas["n"] += 1
        return await real(**kw)

    monkeypatch.setattr(persist, "write_chat_artifacts", contando)
    await finalization.retry_pending(db)
    assert llamadas["n"] == 0, "reintento antes de que le tocara"

    # Cuando le toca, si.
    await db.run("UPDATE chat_outputs SET proximo_intento_at=? "
                 "WHERE chat_id=?", ("2000-01-01T00:00:00Z", chat))
    await finalization.retry_pending(db)
    assert llamadas["n"] == 1
    assert (await db.get_chat(chat))["md_path"], "no recupero el .md"


async def test_las_que_nunca_se_intentaron_van_primero(db, monkeypatch):
    """Una exportacion vieja que falla siempre no puede tapar a una nueva.

    `proximo_intento_at IS NULL` es la que nunca se intento: va antes que
    cualquier agendada, y despues se ordena por fecha.
    """
    orden: list = []
    real = persist.write_chat_artifacts

    async def anotando(**kw):
        orden.append(kw.get("chat_id"))
        return await real(**kw)

    ids = []
    for i, prox in enumerate(("1999-01-01T00:00:00Z", None,
                              "1998-01-01T00:00:00Z")):
        cid = await db.create_chat(project_slug="demo", source="test",
                                   author="", target="demo")
        ids.append(cid)
        art = dict(target="demo", chat_id=cid, user="p", content=f"r{i}",
                   source="test", author="", model="test", status="ok",
                   duration_ms=1)
        await _pendiente(db, cid, art, monkeypatch)
        await db.run("UPDATE chat_outputs SET proximo_intento_at=? "
                     "WHERE chat_id=?", (prox, cid))

    monkeypatch.setattr(persist, "write_chat_artifacts", anotando)
    await finalization.retry_pending(db)
    assert orden[0] == ids[1], (
        f"no arranco por la que nunca se intento: {orden}")
    assert orden[1:] == [ids[2], ids[0]], f"no ordeno por fecha: {orden}"
