import asyncio
import json

import pytest

from relay import grafo, orquestador
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    db = Database(path=tmp_path / "test.db")
    await db.init_schema()
    await db.create_task_graph("g", "Comprobar la aplicación", tareas=[
        {"id": "original", "titulo": "Implementar", "idempotente": False}])
    return db


def verdict(pending="Comprobar la vista", done=()):
    return {"verdict": "needs_more", "feedback": "Falta evidencia del navegador",
            "usage": {"plan_correction": {
                "feedback_to_executor": "Corrige el arranque y comprueba la vista.",
                "revised_steps": [
                    {"id": str(i), "description": text, "expected_output": "PASS",
                     "status": status}
                    for i, (text, status) in enumerate([
                        *((d, "done") for d in done), (pending, "pending")])],
                "remove_step_ids": [], "add_step_ids": []}}}


async def test_corrige_y_verifica_sin_repetir_el_nodo_original(db):
    runs = []
    checks = iter([verdict(), {"verdict": "complete"}])

    async def execute(task):
        runs.append(task)
        return {"ok": True, "resultado": "Evidencia comprobada"}

    async def verify(**_):
        return next(checks)

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == "hecho"
    assert [r["id"] for r in runs].count("original") == 1
    assert len(runs) == 2
    assert "Corrige el arranque" in runs[1]["detalle"]
    assert "Comprobar la vista: PASS" in runs[1]["detalle"]
    assert not runs[1]["idempotente"]
    assert json.loads((await db.get_task_graph("g"))["verificacion_json"])["verdict"] == "complete"


async def test_corta_correccion_repetida_aunque_cambie_el_feedback(db):
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        res = verdict()
        res["feedback"] += str(len(runs))
        return res

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    graph = await db.get_task_graph("g")
    assert len(runs) == 2
    assert result["estado"] == graph["estado"] == "fallado"
    assert result["porcentaje"] < 100
    assert any("sin avance" in t["error"] for t in graph["tasks"] if t["error"])


async def test_sigue_si_el_verificador_acredita_un_criterio_anterior(db):
    checks = iter([verdict("A"), verdict("B", done=["A"]), {"verdict": "complete"}])
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        return next(checks)

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == "hecho"
    assert len(runs) == 3


async def test_renombrar_un_pendiente_no_demuestra_progreso(db):
    checks = iter([verdict("A"), verdict("B")])
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        return next(checks)

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == "fallado"
    assert len(runs) == 2


@pytest.mark.parametrize("response", [
    {"verdict": "needs_more", "feedback": "Sin corrección"},
    {"verdict": "needs_more", "usage": {"plan_correction": {"revised_steps": None}}},
])
async def test_correccion_ausente_o_invalida_no_ejecuta_ni_aprueba(db, response):
    async def execute(_):
        return {"ok": True}

    async def verify(**_):
        return response

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == "fallado"
    assert sum(t["intentos"] for t in (await db.get_task_graph("g"))["tasks"]) == 1


async def test_fallo_de_ejecucion_no_se_repite_por_veredicto(db):
    async def execute(_):
        return {"ok": False, "error": "Efecto previo incierto"}

    async def verify(**_):
        return verdict()

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify,
                                         coordinar=lambda *_: asyncio.sleep(0, result="fallar"))
    assert result["estado"] == "fallado"
    assert len((await db.get_task_graph("g"))["tasks"]) == 1


async def test_cancelar_la_correccion_libera_el_nodo_y_no_crea_otra(db):
    started = asyncio.Event()

    async def execute(task):
        if task["id"] != "original":
            started.set()
            await asyncio.Event().wait()
        return {"ok": True}

    async def verify(**_):
        return verdict()

    task = asyncio.create_task(orquestador.correr_grafo(
        db, "g", ejecutar=execute, verificar=verify))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    graph = await db.get_task_graph("g")
    assert len(graph["tasks"]) == 2
    assert all(t["estado"] != grafo.CORRIENDO for t in graph["tasks"])


async def test_alta_y_checkpoint_son_atomicos_si_falla_la_persistencia(db):
    from relay.orchestrator_recovery import continue_verification

    await db.update_task("original", estado=grafo.HECHO)
    await db.set_task_graph_state("g", "hecho")
    await db.run("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON task_graphs "
                 "WHEN NEW.estado='activo' BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    payload = verdict()
    payload["plan_correction"] = payload["usage"]["plan_correction"]
    with pytest.raises(Exception, match="disk failure"):
        await continue_verification(db, "g", await db.get_task_graph("g"), payload)
    graph = await db.get_task_graph("g")
    assert len(graph["tasks"]) == 1
    assert graph["verificacion_json"] is None
    assert graph["estado"] == "hecho"


async def test_las_correcciones_comparten_el_cortafuegos_del_grafo(db, monkeypatch):
    from relay import orchestrator_core

    monkeypatch.setattr(orchestrator_core, "MAX_VUELTAS", 4)
    checks = iter([verdict("A"), verdict("B", done=["A"])])
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        return next(checks)

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == "fallado"
    assert len(runs) == 2
    graph = await db.get_task_graph("g")
    assert "límite global" in graph["tasks"][-1]["error"]


async def test_releer_verificacion_atomica_conserva_el_freno(db):
    from relay.orchestrator_recovery import continue_verification

    await db.update_task("original", estado=grafo.HECHO)
    checkpoint = {"pending": ["comprobar la vista pass"],
                  "seen": [["comprobar la vista pass"]]}
    await db.set_task_graph_verificacion("g", json.dumps({"recovery": checkpoint}))

    async def verify(**_):
        return verdict()

    assert not await orquestador._verificar_al_cerrar(
        db, "g", await db.get_task_graph("g"), {"estado": "hecho"}, verify)
    # Una nueva lectura desde disco representa un proceso que perdió su estado RAM.
    graph = await db.get_task_graph("g")
    saved = json.loads(graph["verificacion_json"])
    assert saved["recovery"] == checkpoint
    assert len(graph["tasks"]) == 2
    assert graph["tasks"][-1]["estado"] == grafo.FALLADO
    assert "sin avance" in graph["tasks"][-1]["error"]
    assert not await continue_verification(db, "g", graph, saved)
    assert await db.get_task_graph("g") == graph


@pytest.mark.parametrize("state,task_state,response,error", [
    ("cancelado", grafo.HECHO, "needs_more", ""),
    ("activo", grafo.ESPERANDO, "needs_more", ""),
    ("hecho", grafo.HECHO, "off_plan", ""),
    ("hecho", grafo.HECHO, "needs_human", ""),
    ("hecho", grafo.HECHO, "needs_more", "Verificador no disponible"),
])
async def test_correccion_no_reabre_cancelaciones_preguntas_ni_errores(
        db, state, task_state, response, error):
    from relay.orchestrator_recovery import continue_verification

    await db.update_task("original", estado=task_state)
    await db.set_task_graph_state("g", state)
    payload = verdict()
    payload.update(verdict=response, error=error,
                   plan_correction=payload["usage"]["plan_correction"])
    before = await db.get_task_graph("g")
    assert not await continue_verification(db, "g", before, payload)
    assert await db.get_task_graph("g") == before


async def test_sin_veredicto_persistido_no_inicia_correccion(db, monkeypatch):
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        return verdict()

    async def unavailable(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(db, "run_tx", unavailable)
    await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert runs == ["original"]
    graph = await db.get_task_graph("g")
    assert graph["verificacion_json"] is None
    assert len(graph["tasks"]) == 1


@pytest.mark.parametrize("stopped", ["paused", "blocked", "cancelled", "finished", "cleaned"])
async def test_detener_durante_verificacion_conserva_correccion_sin_ejecutarla(db, stopped):
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": "."})
    await db.create_conversation(project_slug="demo", conversation_id="conversation")
    await db.update_conversation_task("conversation", state="running")
    await db.run("UPDATE task_graphs SET conversation_id='conversation' WHERE id='g'")
    verifying, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        if len(runs) == 1:
            verifying.set()
            await release.wait()
            return verdict()
        return {"verdict": "complete"}

    runner = asyncio.create_task(orquestador.correr_grafo(
        db, "g", ejecutar=execute, verificar=verify))
    try:
        await asyncio.wait_for(verifying.wait(), 3)
        await db.update_conversation_task("conversation", state=stopped)
        release.set()
        result = await asyncio.wait_for(runner, 3)
    finally:
        release.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert runs == ["original"]
    assert result["estado"] == "activo"
    graph = await db.get_task_graph("g")
    assert [t["estado"] for t in graph["tasks"]] == [grafo.HECHO, grafo.PENDIENTE]
    assert json.loads(graph["verificacion_json"])["recovery"]["pending"]
    if stopped == "paused":
        # Representa la autorización explícita de continuar; no repite el original.
        await db.update_conversation_task("conversation", state="ready")
        result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
        assert result["estado"] == "hecho"
        assert len(runs) == 2


@pytest.mark.parametrize("tope", [1, 2])
async def test_pausa_espera_inicio_bajo_lock_y_no_lanza_el_siguiente_nodo(db, monkeypatch, tope):
    from relay import task_service

    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": "."})
    await db.create_conversation(project_slug="demo", conversation_id="conversation")
    await db.update_conversation_task("conversation", state="running")
    await db.run("UPDATE task_graphs SET conversation_id='conversation' WHERE id='g'")
    await db.add_tasks_to_graph("g", [{"id": "correction", "titulo": "Corrección"}])

    claim_started, release_claim = asyncio.Event(), asyncio.Event()
    execution_started, release_execution = asyncio.Event(), asyncio.Event()
    pause_waiting, runs = asyncio.Event(), []
    claim = db.claim_task_files

    async def blocked_claim(task_id, graph_id, archivos):
        if task_id == "original":
            claim_started.set()
            await release_claim.wait()
        return await claim(task_id, graph_id, archivos)

    monkeypatch.setattr(db, "claim_task_files", blocked_claim)

    async def execute(task):
        runs.append(task["id"])
        execution_started.set()
        await release_execution.wait()
        return {"ok": True}

    async def pause():
        pause_waiting.set()
        async with task_service.control_lock(db, "conversation"):
            await db.update_conversation_task("conversation", state="paused")

    runner = asyncio.create_task(orquestador.correr_grafo(db, "g", ejecutar=execute, tope=tope))
    pauser = None
    try:
        await asyncio.wait_for(claim_started.wait(), 3)
        pauser = asyncio.create_task(pause())
        await asyncio.wait_for(pause_waiting.wait(), 3)
        assert not pauser.done(), "la pausa adelantó el inicio protegido del nodo"

        release_claim.set()
        await asyncio.wait_for(execution_started.wait(), 3)
        await asyncio.wait_for(pauser, 3)
        assert (await db.get_conversation_task("conversation"))["state"] == "paused"
        assert not runner.done(), "la pausa canceló el nodo que ya había iniciado"

        release_execution.set()
        result = await asyncio.wait_for(runner, 3)
    finally:
        release_claim.set()
        release_execution.set()
        for task in (runner, pauser):
            if task is None:
                continue
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (runner, pauser) if task is not None),
                             return_exceptions=True)

    graph = await db.get_task_graph("g")
    assert result["estado"] == "activo"
    assert runs == ["original"]
    assert [task["estado"] for task in graph["tasks"]] == [grafo.HECHO, grafo.PENDIENTE]


async def test_interrupcion_no_persiste_veredicto_sin_su_correccion(db, monkeypatch):
    import relay.orchestrator_recovery as recovery

    async def execute(task):
        return {"ok": True}

    async def verify(**_):
        return verdict()

    # Simula la caída antes de entrar a la transacción que materializa la corrección.
    async def interrupted(*_, **__):
        raise asyncio.CancelledError()

    monkeypatch.setattr(recovery, "continue_verification", interrupted)
    import relay.orchestrator_core as core
    if hasattr(core, "continue_verification"):
        monkeypatch.setattr(core, "continue_verification", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    persisted = await db.get_task_graph("g")
    assert persisted["verificacion_json"] is None
    assert len(persisted["tasks"]) == 1


async def test_interrupcion_del_verificador_conserva_el_grafo_recuperable(db):
    runs = []
    checks = 0

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise asyncio.CancelledError
        return verdict() if checks == 2 else {"verdict": "complete"}

    with pytest.raises(asyncio.CancelledError):
        await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    persisted = await db.get_task_graph("g")
    assert persisted["estado"] == "activo"
    assert persisted["verificacion_json"] is None
    assert persisted["tasks"][0]["estado"] == grafo.HECHO

    result = await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    assert result["estado"] == (await db.get_task_graph("g"))["estado"] == "hecho"
    assert runs.count("original") == 1
    assert len(runs) == 2


@pytest.mark.parametrize("response", [
    {"verdict": "complete"}, verdict(), {"verdict": "needs_more"},
])
async def test_veredicto_tardio_no_sobrescribe_cancelacion_del_grafo(db, response):
    runs = []

    async def execute(task):
        runs.append(task["id"])
        return {"ok": True}

    async def verify(**_):
        await db.set_task_graph_state("g", "cancelado")
        return response

    await orquestador.correr_grafo(db, "g", ejecutar=execute, verificar=verify)
    graph = await db.get_task_graph("g")
    assert graph["estado"] == "cancelado"
    assert [task["id"] for task in graph["tasks"]] == runs == ["original"]


async def test_cancelacion_antes_del_commit_no_materializa_correccion(db, monkeypatch):
    from relay.orchestrator_recovery import continue_verification

    await db.update_task("original", estado=grafo.HECHO)
    stale = await db.get_task_graph("g")
    payload = verdict()
    payload["plan_correction"] = payload["usage"]["plan_correction"]
    run_tx = db.run_tx
    cancelled = None

    async def cancel_before_transaction(statements):
        nonlocal cancelled
        await db.set_task_graph_state("g", "cancelado")
        cancelled = await db.get_task_graph("g")
        await run_tx(statements)

    monkeypatch.setattr(db, "run_tx", cancel_before_transaction)
    assert not await continue_verification(db, "g", stale, payload)
    assert await db.get_task_graph("g") == cancelled


async def test_veredicto_y_estado_final_se_confirman_juntos(db):
    await db.update_task("original", estado=grafo.HECHO)
    await db.run("CREATE TRIGGER reject_terminal BEFORE UPDATE OF estado ON task_graphs "
                 "WHEN NEW.estado='hecho' BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    before = await db.get_task_graph("g")

    async def verify(**_):
        return {"verdict": "complete", "feedback": "Comprobado"}

    assert not await orquestador._verificar_al_cerrar(
        db, "g", before, {"estado": "hecho"}, verify)
    assert await db.get_task_graph("g") == before


async def test_resume_http_repite_verificacion_interrumpida_sin_repetir_nodos(monkeypatch):
    from pathlib import Path
    from unittest.mock import patch
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    from test_disparador import _Base

    case = _Base()
    await case.asyncSetUp()
    try:
        await case.db.create_task_graph("verify-http", "Verificar", tareas=[
            {"id": "only-http", "titulo": "Trabajo"}], project_slug="demo")
        executed, completed = [], []
        checks = 0

        async def execute(task):
            executed.append(task["id"])
            return {"ok": True}

        async def verify(**_):
            nonlocal checks
            checks += 1
            if checks == 1:
                raise asyncio.CancelledError
            return {"verdict": "complete"}

        with pytest.raises(asyncio.CancelledError):
            await orquestador.correr_grafo(case.db, "verify-http", ejecutar=execute,
                                          verificar=verify)

        async def launch(db, project, graph_id, **_):
            result = await orquestador.correr_grafo(
                db, graph_id, ejecutar=execute, verificar=verify)
            completed.append(result)
            return result

        with patch.object(orquestador, "lanzar", launch):
            response = await case.client.post("/graphs/verify-http/resume")
            assert response.status == 202
            await case._esperar(lambda: completed)
            assert (await case.client.post("/graphs/verify-http/resume")).status == 409
        assert executed == ["only-http"]
        assert checks == 2
        assert (await case.db.get_task_graph("verify-http"))["estado"] == "hecho"
    finally:
        await case.asyncTearDown()
