"""Tests de las 4 agregaciones SQL de db.py que alimentan la UI:
metrics_summary, metrics_trends, report_usage y global_search.

Por qué importan: son las únicas piezas del relay que pueden mentir sin
romperse. Un GROUP BY mal escrito o un COALESCE de menos no tira 500 —
devuelve números plausibles pero equivocados en el dashboard de métricas
y en el informe de tokens/costo, que es justo donde nadie los va a
verificar a mano.

Lo que se fija acá:
  - las sumas y errores separados de actividad, cancelación y subdivisión
  - la ventana temporal (`days`): lo viejo NO entra
  - el slice diario de trends: una fila por día, ordenada
  - el filtro por proyecto de report_usage
  - global_search: qué campos matchea y que el cap por tipo se respeta

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_metrics_queries.py -q
"""
from __future__ import annotations

import time

import pytest

from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "metrics.db")
    await d.init_schema()
    return d


async def _chat(db, *, slug=None, author="usuario-demo-a", source="discord",
                status="ok", tokens_in=0, tokens_out=0, tool_calls=0,
                duration_ms=1000, model="minimax", error=None,
                days_ago=0) -> str:
    """Un chat terminado. `days_ago` reescribe started_at para poder
    probar la ventana (create_chat siempre sella 'ahora')."""
    cid = await db.create_chat(project_slug=slug, source=source,
                               author=author, target=slug)
    await db.finish_chat(cid, status=status, tokens_in=tokens_in,
                         tokens_out=tokens_out, tool_calls=tool_calls,
                         duration_ms=duration_ms, model=model, error=error)
    if days_ago:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - days_ago * 86400))
        await db.run("UPDATE chats SET started_at=? WHERE id=?", (ts, cid))
    return cid


# ---------- metrics_summary ----------


async def test_metrics_summary_suma_y_agrupa(db):
    await _chat(db, slug="inventorydemo", tokens_in=100, tokens_out=10,
                tool_calls=3, duration_ms=1000)
    await _chat(db, slug="inventorydemo", tokens_in=200, tokens_out=20,
                tool_calls=4, duration_ms=3000)
    await _chat(db, slug="sample-app", tokens_in=50, tokens_out=5,
                tool_calls=0, duration_ms=2000, model="deepseek",
                status="error", error="timeout")

    m = await db.metrics_summary(days=7)
    t = m["totals"]
    assert t["runs"] == 3
    assert t["tokens_in"] == 350
    assert t["tokens_out"] == 35
    assert t["tool_calls"] == 7
    assert t["duration_ms_avg"] == 2000          # (1000+3000+2000)/3
    assert t["errors"] == 1

    by_model = {r["model"]: r for r in m["by_model"]}
    assert by_model["minimax"]["runs"] == 2
    assert by_model["minimax"]["tokens_in"] == 300
    assert by_model["deepseek"]["runs"] == 1

    # by_project ordena por runs desc y capea en 10
    assert m["by_project"][0]["slug"] == "inventorydemo"
    assert m["by_project"][0]["tokens_in"] == 300

    assert m["error_breakdown"] == [{"error_type": "timeout", "count": 1}]
    # la distribución horaria reparte TODOS los runs (sin perder ninguno)
    assert sum(h["runs"] for h in m["hourly_distribution"]) == 3


async def test_metrics_summary_respeta_la_ventana(db):
    await _chat(db, slug="inventorydemo", tokens_in=100)
    await _chat(db, slug="inventorydemo", tokens_in=999, days_ago=30)

    reciente = await db.metrics_summary(days=7)
    assert reciente["totals"]["runs"] == 1
    assert reciente["totals"]["tokens_in"] == 100     # el viejo no suma

    todo = await db.metrics_summary(days=90)
    assert todo["totals"]["runs"] == 2
    assert todo["totals"]["tokens_in"] == 1099


async def test_metrics_summary_sin_datos_no_devuelve_nulls(db):
    """El dashboard hace aritmética con esto: COUNT sin filas da 0, pero
    SUM sin filas da NULL si falta el COALESCE."""
    m = await db.metrics_summary(days=7)
    assert m["totals"]["runs"] == 0
    assert m["totals"]["tokens_in"] == 0
    assert m["totals"]["tokens_out"] == 0
    assert m["totals"]["duration_ms_avg"] == 0
    assert m["by_model"] == []


# ---------- metrics_trends ----------


async def test_metrics_trends_una_fila_por_dia_ordenada(db):
    await _chat(db, slug="inventorydemo", tokens_in=100)
    await _chat(db, slug="inventorydemo", tokens_in=200)
    await _chat(db, slug="inventorydemo", tokens_in=50, status="error",
                error="boom", days_ago=1)

    filas = await db.metrics_trends(days=7)
    assert len(filas) == 2
    assert [f["date"] for f in filas] == sorted(f["date"] for f in filas)
    ayer, hoy = filas
    assert ayer["runs"] == 1 and ayer["tokens_in"] == 50 and ayer["errors"] == 1
    assert hoy["runs"] == 2 and hoy["tokens_in"] == 300 and hoy["errors"] == 0


async def test_metrics_trends_filtra_igual_que_summary(db):
    """El gráfico va al lado de los KPIs: si trends no toma los mismos
    filtros de run, la serie contradice a los números que tiene al lado.
    Hasta el 1/9/2026 trends ignoraba project/status."""
    await _chat(db, slug="inventorydemo", tokens_in=100)
    await _chat(db, slug="sample-app", tokens_in=700)
    await _chat(db, slug="inventorydemo", tokens_in=50, status="error", error="boom")

    solo_inventorydemo = await db.metrics_trends(days=7, project="inventorydemo")
    assert sum(f["runs"] for f in solo_inventorydemo) == 2
    assert sum(f["tokens_in"] for f in solo_inventorydemo) == 150

    # mismos totales que el summary con el mismo filtro: es el punto
    resumen = await db.metrics_summary(days=7, project="inventorydemo")
    assert sum(f["runs"] for f in solo_inventorydemo) == resumen["totals"]["runs"]
    assert sum(f["tokens_in"] for f in solo_inventorydemo) == resumen["totals"]["tokens_in"]
    assert sum(f["errors"] for f in solo_inventorydemo) == resumen["totals"]["errors"]


async def test_metrics_trends_status_error_excluye_cancelados(db):
    """Error es un intento fallido; cancelar es una categoría distinta."""
    await _chat(db, slug="inventorydemo")
    await _chat(db, slug="inventorydemo", status="cancelled")
    await _chat(db, slug="inventorydemo", status="error", error="boom")

    con_error = await db.metrics_trends(days=7, status="error")
    assert sum(f["runs"] for f in con_error) == 1

    solo_ok = await db.metrics_trends(days=7, status="ok")
    assert sum(f["runs"] for f in solo_ok) == 1


async def test_metrics_separates_active_cancelled_and_split(db):
    for status in ("ok", "running", "cancelled", "error"):
        await _chat(db, status=status, error="boom" if status == "error" else None)
    split = await _chat(db, status="error", error="subdividido")
    await db.run("UPDATE chats SET phase_at_end='budget_split' WHERE id=?", (split,))
    totals = (await db.metrics_summary())["totals"]
    assert totals["runs"] == 5
    for kind in ("ok", "running", "cancelled", "errors", "split"):
        assert totals[kind] == 1
        assert sum(r[kind] for r in await db.metrics_trends()) == 1
    assert (await db.metrics_summary(status="error"))["totals"]["runs"] == 1
    assert (await db.metrics_summary(status="split"))["totals"]["runs"] == 1
    assert (await db.metrics_summary())["error_breakdown"] == [{"error_type": "boom", "count": 1}]


async def test_metrics_http_ok_split_is_not_counted_as_success(db):
    cid = await _chat(db, status="ok")
    await db.run("UPDATE chats SET phase_at_end='budget_split' WHERE id=?", (cid,))
    totals = (await db.metrics_summary())["totals"]
    assert totals["runs"] == totals["split"] == 1
    assert totals["ok"] == totals["errors"] == 0
    assert (await db.metrics_summary(status="ok"))["totals"]["runs"] == 0
    trend = (await db.metrics_trends())[0]
    assert trend["split"] == 1 and trend["ok"] == 0


# ---------- report_usage ----------


async def test_report_usage_agrupa_y_filtra_por_proyecto(db):
    await _chat(db, slug="inventorydemo", author="usuario-demo-a", tokens_in=100, tool_calls=2)
    await _chat(db, slug="inventorydemo", author="usuario-demo-b", tokens_in=200, tool_calls=1)
    await _chat(db, slug="sample-app", author="usuario-demo-a", tokens_in=50)

    todo = await db.report_usage(days=30)
    por_proyecto = {r["project"]: r for r in todo["by_project"]}
    assert por_proyecto["inventorydemo"]["runs"] == 2
    assert por_proyecto["inventorydemo"]["tokens_in"] == 300
    assert por_proyecto["inventorydemo"]["tool_calls"] == 3
    assert por_proyecto["sample-app"]["runs"] == 1
    assert {r["author"] for r in todo["by_author"]} == {"usuario-demo-a", "usuario-demo-b"}
    assert sum(d["runs"] for d in todo["daily"]) == 3

    solo_inventorydemo = await db.report_usage(days=30, project_slug="inventorydemo")
    assert [r["project"] for r in solo_inventorydemo["by_project"]] == ["inventorydemo"]
    assert sum(d["runs"] for d in solo_inventorydemo["daily"]) == 2
    assert {r["author"] for r in solo_inventorydemo["by_author"]} == {"usuario-demo-a", "usuario-demo-b"}


async def test_report_usage_sin_proyecto_no_pierde_el_run(db):
    """project_slug NULL (run suelto) tiene que caer en el bucket '—',
    no desaparecer del informe."""
    await _chat(db, slug=None, tokens_in=42)
    rep = await db.report_usage(days=30)
    assert [r["project"] for r in rep["by_project"]] == ["—"]
    assert rep["by_project"][0]["tokens_in"] == 42


# ---------- global_search ----------


async def test_global_search_matchea_slug_nombre_y_repo_path(db):
    await db.upsert_project({"slug": "inventorydemo", "name": "Warehouse",
                             "repo_path": "C:/repos/INVENTORYDEMO"})
    await db.upsert_project({"slug": "sample-app", "name": "SampleApp",
                             "repo_path": "C:/repos/sample-app"})

    por_slug = await db.global_search("inventorydemo")
    assert [p["slug"] for p in por_slug["projects"]] == ["inventorydemo"]
    # case-insensitive y por substring del nombre
    assert [p["slug"] for p in (await db.global_search("wareHOUSE"))
            ["projects"]] == ["inventorydemo"]
    # el repo_path también matchea (pegar una ruta en el Cmd+K)
    assert [p["slug"] for p in (await db.global_search("C:/repos/sample"))
            ["projects"]] == ["sample-app"]
    assert (await db.global_search("nada-de-esto"))["projects"] == []


async def test_global_search_encuentra_chats_y_respeta_el_cap(db):
    for _ in range(5):
        await _chat(db, slug="inventorydemo")
    await _chat(db, slug="sample-app")

    res = await db.global_search("inventorydemo", limit=3)
    assert len(res["chats"]) == 3                    # cap por tipo
    assert all(c["project_slug"] == "inventorydemo" for c in res["chats"])
    assert res["q"] == "inventorydemo"

    # los chats se buscan por prefijo de id (uuid pegado en el buscador)
    cid = await _chat(db, slug="sample-app")
    por_id = await db.global_search(cid[:8])
    assert [c["id"] for c in por_id["chats"]] == [cid]
