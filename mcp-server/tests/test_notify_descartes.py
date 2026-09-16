"""El bot rechaza en el CUERPO y con status 200: hay que leerlo.

Medido el 2026-09-04 sobre los dos repos. `NotifyController` del bot
(RelayDemoBot) contesta `Ok(new { discarded = true, reason })` —o sea
HTTP 200— en dos caminos que el relay usa de verdad:

  - `bad_agent_id_prefix`: el dispatcher solo reconoce `chat:`,
    `prompt:` y `night:`; el digest de CRM manda `crm-digest:<fecha>`.
  - `missing_qid_runid`: el handler de night exige `q_id` sea cual sea
    el `kind`, y el `done` de fin de run manda `run_id` sin `q_id`.

`NotifyClient.send` miraba solo `raise_for_status()`, así que los dos
devolvían True y `POST /admin/api/crm/digest` le contestaba
`sent: true` a quien lo pidió mientras en Discord no aparecía nada.

Estos tests NO prueban que el mensaje llegue a Discord —eso depende del
bot— sino que el relay deje de mentir sobre si llegó.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_notify_descartes.py -q
"""
from __future__ import annotations

import httpx
import pytest

from relay.notify import NotifyClient


def _cliente(handler) -> NotifyClient:
    """Un NotifyClient con el transporte pisado, sin red ni reintentos."""
    n = NotifyClient(base_url="http://bot.invalido:1/notify")
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return n


async def test_un_200_con_discarded_no_cuenta_como_enviado():
    """El caso del digest de CRM: 200 OK y el bot no posteó nada."""
    for reason in ("bad_agent_id_prefix", "missing_qid_runid"):
        n = _cliente(lambda req, _r=reason: httpx.Response(
            200, json={"discarded": True, "reason": _r}))
        ok = await n.send(agent_id="crm-digest:2026-09-04", kind="progress",
                          message="hola")
        await n.aclose()
        assert ok is False, f"reportó enviado un notify descartado ({reason})"


async def test_un_200_normal_sigue_contando_como_enviado():
    """La respuesta del camino feliz no tiene `discarded`: no romper eso."""
    for cuerpo in ({"ok": True}, {"discarded": False}, {}):
        n = _cliente(lambda req, _c=cuerpo: httpx.Response(200, json=_c))
        ok = await n.send(agent_id="chat:abc", kind="response", message="hola")
        await n.aclose()
        assert ok is True, f"marcó como fallido un notify que sí se posteó: {cuerpo}"


async def test_una_respuesta_sin_json_no_revienta():
    """El bot podría contestar 200 con texto plano o vacío: eso es éxito."""
    n = _cliente(lambda req: httpx.Response(200, text="OK"))
    ok = await n.send(agent_id="chat:abc", kind="done", message="hola")
    await n.aclose()
    assert ok is True


async def test_el_motivo_del_descarte_queda_en_el_log(caplog):
    """Sin el motivo en el log no hay forma de saber por qué no llegó."""
    n = _cliente(lambda req: httpx.Response(
        200, json={"discarded": True, "reason": "bad_agent_id_prefix"}))
    with caplog.at_level("WARNING", logger="relay.notify"):
        await n.send(agent_id="crm-digest:2026-09-04", kind="progress",
                     message="hola")
    await n.aclose()
    assert "bad_agent_id_prefix" in caplog.text
    assert "crm-digest:2026-09-04" in caplog.text
