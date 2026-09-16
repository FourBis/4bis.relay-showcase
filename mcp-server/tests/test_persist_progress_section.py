"""Sprint 1 (Iter 5.x): el .md del chat vuelca una bitácora compacta
de los tool_call del run, para auditar CÓMO se llegó al veredicto.

Bug medido el 2026-09-06 sobre un nodo de grafo real:
    - 790.142 tokens consumidos en 61 tool calls
    - .md archivado: 1 KB, solo traía el pedido y la respuesta final
    - la columna `chats.progress_events` del mismo chat tenía 161
      eventos (59.701 chars) con la forma
        {"ts": "...", "phase": "tool_call", "tool": "...",
         "tool_calls": N, "message": "..."}
      que ya describían qué archivos abrió el experto y qué vio.

El dato estaba guardado pero no se volcaba. Sin bitácora, revisar el
nodo es leer la respuesta final y adivinar el camino. Con bitácora,
es seguir los tool_call en orden.

Reglas del fix:
    - escribir `write_chat_md(events=...)` con la lista ya parseada;
      `None` o lista vacía conserva el .md anterior (sin sección extra).
    - compactar: solo `phase == "tool_call"`, descartar `thinking` y
      `say`. Mensaje cap a 200 chars. Si hay >50 tool_call, resumir
      los más viejos en una línea y volcar los últimos 50.
    - el heading elegido ("Bitácora de la corrida") no aparece en
      `_MD_SECTIONS` de persist.py, así que `parse_chat_md_turns` lo
      trata como contenido del experto y NO abre un turno nuevo — el
      parseo es invariante.

Este test cubre el contrato. Hoy falla: write_chat_md no acepta
`events` y el .md no incluye la bitácora.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from relay.persist import (  # type: ignore[import-not-found]
    BITACORA_HEADING,
    parse_chat_md_turns,
    write_chat_md,
)


def _run(coro):
    """Helper async en sync — sin pytest-asyncio para mantener el
    test liviano."""
    return asyncio.run(coro)


def _chats_dir(monkeypatch, tmp_path: Path) -> Path:
    chats = tmp_path / "chats"
    chats.mkdir()
    monkeypatch.setenv("FOURBIS_CHATS_DIR", str(chats))
    return chats


def _sample_events() -> list[dict]:
    """Mezcla realista: tool_call + thinking + say."""
    return [
        {"ts": "2026-09-06T18:38:00Z", "phase": "thinking",
         "tool": None, "message": "el usuario pidió un plan"},
        {"ts": "2026-09-06T18:38:01Z", "phase": "tool_call",
         "tool": "read_skill", "tool_calls": 1,
         "message": "leyo skill ponytail"},
        {"ts": "2026-09-06T18:38:02Z", "phase": "say",
         "tool": None, "message": "ok arranco"},
        {"ts": "2026-09-06T18:38:03Z", "phase": "tool_call",
         "tool": "read_file", "tool_calls": 1,
         "message": "leyo server.py"},
    ]


def _kwargs(events=None) -> dict:
    base = dict(
        target="discord:123",
        chat_id="chat-test-1",
        user="arreglá esto",
        content="respuesta final del experto",
        source="discord",
        author="demo-user",
        model="test",
        status="ok",
        duration_ms=240_000,
    )
    if events is not None:
        base["events"] = events
    return base


def test_write_chat_md_incluye_bitacora_de_tool_call(monkeypatch, tmp_path):
    """write_chat_md debe aceptar events= y volcar solo los tool_call
    con ts/tool/message truncado a 200 chars. thinking y say NO deben
    aparecer."""
    _chats_dir(monkeypatch, tmp_path)
    md_path = _run(write_chat_md(**_kwargs(),
                                  events=_sample_events()))

    text = Path(md_path).read_text(encoding="utf-8")

    # El heading existe y NO es uno mapeado a role.
    assert BITACORA_HEADING in text
    assert "## Usuario" in text
    assert "## Respuesta" in text

    # Los tool_call aparecen con sus campos.
    assert "read_skill" in text
    assert "read_file" in text
    assert "2026-09-06T18:38:01Z" in text
    assert "2026-09-06T18:38:03Z" in text
    assert "leyo skill ponytail" in text
    assert "leyo server.py" in text

    # thinking y say NO se vuelcan.
    assert "el usuario pidió un plan" not in text
    assert "ok arranco" not in text


def test_write_chat_md_sin_eventos_no_agrega_seccion(monkeypatch, tmp_path):
    """Compat: si events=None o [], el .md no contiene la sección
    de bitácora (comportamiento previo intacto)."""
    _chats_dir(monkeypatch, tmp_path)

    md_none = _run(write_chat_md(**_kwargs(), events=None))
    md_empty = _run(write_chat_md(**_kwargs(), events=[]))

    for p in (md_none, md_empty):
        text = Path(p).read_text(encoding="utf-8")
        assert BITACORA_HEADING not in text
        assert "## Usuario" in text
        assert "## Respuesta" in text


def test_write_chat_md_trunca_mensajes_largos(monkeypatch, tmp_path):
    """El message de un tool_call se trunca a 200 chars."""
    _chats_dir(monkeypatch, tmp_path)
    long_msg = "x" * 600
    events = [
        {"ts": "2026-09-06T18:38:00Z", "phase": "tool_call",
         "tool": "read_file", "message": long_msg},
    ]

    md_path = _run(write_chat_md(**_kwargs(), events=events))
    text = Path(md_path).read_text(encoding="utf-8")

    # El mensaje entero NO debe aparecer (se trunca a ~200 chars).
    assert long_msg not in text
    # La elipsis de truncado sí aparece.
    assert "…" in text
    # Pero aparece el principio (la línea se mantiene reconocible).
    assert "xxx" in text


def test_write_chat_md_resume_eventos_excedentes(monkeypatch, tmp_path):
    """Con más de 50 tool_call, los más viejos se resumen en una sola
    línea y se vuelcan los últimos 50."""
    _chats_dir(monkeypatch, tmp_path)

    events = []
    for i in range(120):
        events.append({
            "ts": f"2026-09-06T18:{i // 60:02d}:{i % 60:02d}Z",
            "phase": "tool_call", "tool": f"tool_{i:03d}",
            "message": f"msg {i}",
        })

    md_path = _run(write_chat_md(**_kwargs(), events=events))
    text = Path(md_path).read_text(encoding="utf-8")

    # El primero NO está (se resumió).
    assert "tool_000" not in text
    # El último sí.
    assert "tool_119" in text
    # La línea de resumen con el conteo aparece.
    assert "70 eventos tool_call anteriores omitidos" in text


def test_parse_chat_md_turns_es_invariante_con_bitacora(
        monkeypatch, tmp_path):
    """La sección de bitácora NO agrega turnos: parse_chat_md_turns
    sobre el .md con bitácora devuelve los mismos turnos que sin ella.

    Esto valida que BITACORA_HEADING no esté en _MD_SECTIONS."""
    _chats_dir(monkeypatch, tmp_path)

    # Sin bitácora.
    md_sin = _run(write_chat_md(**_kwargs()))
    turns_sin = parse_chat_md_turns(md_sin)

    # Con bitácora (eventos mixtos).
    md_con = _run(write_chat_md(**_kwargs(), events=_sample_events()))
    turns_con = parse_chat_md_turns(md_con)

    # Misma cantidad de turnos.
    assert len(turns_con) == len(turns_sin)
    # Mismos roles.
    assert [t["role"] for t in turns_con] == \
        [t["role"] for t in turns_sin]
    # El contenido de Usuario/Respuesta no se alteró por la bitácora.
    user_sin = next(t for t in turns_sin if t["role"] == "user")
    user_con = next(t for t in turns_con if t["role"] == "user")
    assert user_con["content"] == user_sin["content"]


def test_write_chat_md_eventos_no_dict_se_ignoran(monkeypatch, tmp_path):
    """Defensivo: si llega algo que no es dict (str, None), no rompe
    el render."""
    _chats_dir(monkeypatch, tmp_path)
    events = [
        "raw string colada",
        None,
        {"ts": "2026-09-06T18:38:00Z", "phase": "tool_call",
         "tool": "shell", "message": "ok"},
    ]

    md_path = _run(write_chat_md(**_kwargs(), events=events))
    text = Path(md_path).read_text(encoding="utf-8")

    assert "raw string colada" not in text
    assert "shell" in text
