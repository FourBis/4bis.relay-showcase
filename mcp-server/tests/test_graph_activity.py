"""El grafo aparece vivo durante sus nodos y durante el cierre, sin duplicarse."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from relay import logctx, orquestador, server


@pytest.mark.parametrize("node_error", [False, True])
async def test_graph_activity_tracks_node_lifecycle(monkeypatch, node_error):
    app = {server.DB_KEY: object(), server.NOTIFY_KEY: None,
           server.PROGRESS_KEY: {}, server.RUNNING_KEY: {}, server.GRAFOS_KEY: {}}
    ready, release_node, verifying, release_graph = (asyncio.Event() for _ in range(4))
    monkeypatch.setattr(server, "_get_api_key", lambda: "")

    async def lanzar(db, project, graph_id, *, progreso_de):
        async def node():
            callback = progreso_de("node-1")
            assert logctx.current_chat.get() == "node-1"
            await callback(phase="tool_call", tool="read_file", tool_calls=1)
            ready.set()
            await release_node.wait()
            if node_error:
                raise RuntimeError("simulated node failure")

        await asyncio.gather(asyncio.create_task(node()), return_exceptions=True)
        verifying.set()
        await release_graph.wait()
        return {"estado": "fallado" if node_error else "hecho"}

    monkeypatch.setattr(orquestador, "lanzar", lanzar)
    task = asyncio.create_task(server._correr_grafo_bg(
        app, {"slug": "demo"}, "graph-1"))
    app[server.GRAFOS_KEY]["graph-1"] = task

    async def status():
        response = await server.system_active(SimpleNamespace(app=app))
        return json.loads(response.text)

    try:
        await asyncio.wait_for(ready.wait(), 2)
        running = await status()
        assert running["active"] and running["count"] == 1
        assert running["first"]["chat_id"] == "node-1"
        assert running["first"]["graph_id"] == "graph-1"
        assert "steps" not in running["first"]
        assert "node-1" not in app[server.RUNNING_KEY]

        release_node.set()
        await asyncio.wait_for(verifying.wait(), 2)
        assert "node-1" not in app[server.RUNNING_KEY]
        assert app[server.PROGRESS_KEY]["node-1"].finished
        closing = await status()
        assert closing["active"] and closing["count"] == 1
        assert closing["first"]["chat_id"] == "graph-1"

        release_graph.set()
        await asyncio.wait_for(task, 2)
        assert not (await status())["active"]
    finally:
        release_node.set()
        release_graph.set()
        await asyncio.gather(task, return_exceptions=True)


def test_graph_public_keeps_replaced_parent_history():
    def row(id, state, parent=None):
        return {"id": id, "titulo": id, "detalle": "", "estado": state,
                "deps": [], "idempotente": 1, "intentos": 1, "max_intentos": 2,
                "orden": 1, "parent_id": parent, "error": "split" if not parent else ""}

    public = server._grafo_publico({
        "id": "graph", "objetivo": "x", "estado": "fallado",
        "tasks": [row("parent", "fallado"), row("child", "hecho", "parent")]})
    assert public["estado"] == "fallado"  # auditoría sin escrituras retroactivas
    assert public["estado_visible"] == "hecho"
    assert public["progreso"]["hechos"] == public["progreso"]["total"] == 1
    parent = next(t for t in public["tasks"] if t["id"] == "parent")
    assert parent["sustituido"] and parent["estado"] == "fallado"
    assert parent["error"] == "split"


def test_historical_tool_steps_follow_ids_not_arrival_order():
    events = [
        {"phase": "tool_call", "tool": "shell", "tool_call_id": "a", "message": "A"},
        {"phase": "tool_call", "tool": "shell", "tool_call_id": "b", "message": "B"},
        {"phase": "tool_result", "tool": "shell", "tool_call_id": "unknown", "output": "unrelated"},
        {"phase": "tool_result", "tool": "shell", "tool_call_id": "b", "output": "B failed (exit=1)"},
        {"phase": "tool_result", "tool": "shell", "tool_call_id": "a", "output": "A passed (exit=0)"},
    ]
    steps = server._steps_from_progress(json.dumps(events))
    assert [s["output"] for s in steps] == ["A passed (exit=0)", "B failed (exit=1)"]
